"""The agent's tool surface.

Every tool input is a Pydantic model (used both to generate the strict JSON
schema the model sees and to validate what comes back before anything runs).

Permissions are enforced here, in code:

* Tool *sets* differ by task: email processing, payment-run preparation and
  internal Q&A each get only the tools they need (``EMAIL_TOOLS``,
  ``PAYMENT_TOOLS``, ``ASK_TOOLS``). The Q&A set is read-only.
* Every read of vendor-specific data is recorded in ``ctx.vendors_disclosed``.
  A free-text reply to an email sender is only allowed if the sender is
  authorised for *every* vendor whose data the task read (DMARC-verified and on
  the vendor's contact list) - so prompt-injected "tell me about vendor X"
  requests can't leak.
* POs, duplicates and bills are scoped to the vendor named in the call.
* Mutating tools route through the policy engine. There is no tool that edits
  vendor master data, changes bank details, approves anything, or pays.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from ap_agent.ap.controls import bank_account_status, sender_authorized_for, tax_rate_ok, to_functional
from ap_agent.ap.duplicates import check_duplicate, normalize_invoice_number
from ap_agent.ap.matching import InvoiceLineIn, MatchResult, match_invoice
from ap_agent.ap.policy import Action, Policy, decide
from ap_agent.erp.base import ERPConnector, ERPError
from ap_agent.mail.base import EmailMessage, MailboxConnector
from ap_agent.models import BillDraft, BillLine, PurchaseOrder, Vendor
from ap_agent.storage.db import ApprovalRecord, CodingHistory, Database, InvoiceRecord, PaymentRunRecord

log = logging.getLogger(__name__)

ACK_TEMPLATE = (
    "Thank you for your email. It has been received by Accounts Payable and will be processed; "
    "we'll be in touch if we need anything further.\n\nAccounts Payable, {company}"
)
INTERNAL_TEMPLATE = (
    "Thanks - the AP mailbox is for suppliers. For questions about invoices or payments, please use the "
    "AP assistant at {portal}/ask, which checks your access.\n\nAccounts Payable"
)


# --------------------------------------------------------------------- schema


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> strict-tool-compatible schema (inline refs, no
    titles/defaults, every property required, no additional properties)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [fix(n) for n in node]
        return node

    return fix(raw)


# --------------------------------------------------------------------- inputs


class LookupVendor(BaseModel):
    name: str | None = Field(description="Vendor name as printed on the invoice")
    tax_id: str | None = Field(description="Tax / VAT / EIN number if printed")
    sender_email: str | None = Field(description="Email address the invoice came from")


class GetPO(BaseModel):
    vendor_id: str = Field(description="The vendor the PO must belong to")
    po_number: str


class CheckDuplicate(BaseModel):
    invoice_id: str = Field(description="Agent invoice id from the task")
    vendor_id: str


class MatchToPO(BaseModel):
    invoice_id: str
    vendor_id: str
    po_number: str


class ListGL(BaseModel):
    search: str | None = Field(description="Optional substring filter on account name/number")


class CodingHistoryQuery(BaseModel):
    vendor_id: str


class CodedLine(BaseModel):
    description: str
    amount: float = Field(description="Line amount excluding tax")
    quantity: float | None
    unit_price: float | None
    gl_account: str | None = Field(description="GL account id from list_gl_accounts (required for non-PO lines)")
    po_line_id: str | None = Field(description="PO line id when billed against a PO")
    item_code: str | None


class SubmitInvoice(BaseModel):
    invoice_id: str
    vendor_id: str
    po_number: str | None
    invoice_number: str
    invoice_date: str = Field(description="YYYY-MM-DD")
    due_date: str | None = Field(description="YYYY-MM-DD; derive from payment terms if not printed")
    currency: str
    total: float = Field(description="Document total including tax (positive, also for credit notes)")
    tax_total: float
    lines: list[CodedLine]
    coding_rationale: str = Field(description="Short explanation of GL coding / match decisions for the approver")


class BillStatusQuery(BaseModel):
    vendor_id: str
    invoice_number: str | None = Field(description="Specific invoice; omit to list all of the vendor's bills")


class ReplyToSender(BaseModel):
    kind: Literal["acknowledgement", "answer", "internal_redirect"] = Field(
        description="acknowledgement: fixed receipt text (body ignored); answer: your text, allowed only for authorised senders; internal_redirect: fixed text pointing staff to the AP assistant"
    )
    body: str = Field(description="Plain-text reply for kind=answer; empty otherwise")


class FlagForReview(BaseModel):
    category: Literal["bank_detail_change", "suspected_fraud", "vendor_not_found", "unreadable", "dispute", "duplicate", "policy_exception", "entity_unknown", "other"]
    reason: str
    invoice_id: str | None


class ListOpenBills(BaseModel):
    due_within_days: int = Field(description="Include bills due within this many days")


class ProposedPayment(BaseModel):
    bill_id: str
    vendor_id: str
    amount: float = Field(description="Amount to pay; may be less than the balance for a partial payment")
    reason: str = Field(description="e.g. 'due 2026-12-01' or 'take 2% discount by 2026-11-30'")


class ProposePaymentRun(BaseModel):
    payment_date: str = Field(description="YYYY-MM-DD")
    payments: list[ProposedPayment]
    rationale: str


class SearchInvoices(BaseModel):
    vendor_id: str | None
    invoice_number: str | None
    status: str | None = Field(description="received, awaiting_approval, posted, rejected, blocked, needs_review, duplicate")
    limit: int = Field(description="Max rows, up to 50")


class APSummary(BaseModel):
    as_of: str | None = Field(description="YYYY-MM-DD, default today")


# --------------------------------------------------------------------- context


@dataclass
class ToolContext:
    erp: ERPConnector
    mailbox: MailboxConnector | None
    db: Database
    policy: Policy
    entity: str = "default"
    functional_currency: str = "USD"
    company: str = "Accounts Payable"
    portal_url: str = ""
    bank_pepper: str = "dev-pepper"
    reply_mode: str = "draft"
    email: EmailMessage | None = None
    actor: str = "agent"
    invoice_ids: list[str] = field(default_factory=list)
    replied: bool = False
    flags: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    vendors_disclosed: set[str] = field(default_factory=set)
    on_review: Callable[..., Any] | None = None
    enqueue: Callable[..., Any] | None = None
    notify: Callable[[list[str], str, str], Any] | None = None

    def disclose(self, *vendor_ids: str) -> None:
        self.vendors_disclosed.update(v for v in vendor_ids if v)


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Any]

    def definition(self, eager: bool) -> dict[str, Any]:
        d = {"name": self.name, "description": self.description, "strict": True, "input_schema": strict_schema(self.input_model)}
        if eager:
            d["eager_input_streaming"] = True
        return d


class ToolError(Exception):
    pass


def _dump(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    elif isinstance(obj, list):
        obj = [o.model_dump(mode="json") if isinstance(o, BaseModel) else o for o in obj]
    return json.dumps(obj, default=str)


def _invoice(ctx: ToolContext, invoice_id: str) -> InvoiceRecord:
    if invoice_id not in ctx.invoice_ids:
        raise ToolError(f"Unknown invoice_id {invoice_id}; use an id given in this task")
    with ctx.db.session() as s:
        rec = s.get(InvoiceRecord, invoice_id)
    if rec is None or rec.entity != ctx.entity:
        raise ToolError(f"Invoice {invoice_id} not found")
    return rec


def _vendor(ctx: ToolContext, vendor_id: str) -> Vendor:
    v = ctx.erp.get_vendor(vendor_id)
    if v is None:
        raise ToolError(f"Vendor {vendor_id} not found")
    return v


def _po_for_vendor(ctx: ToolContext, vendor_id: str, po_number: str) -> PurchaseOrder | None:
    po = ctx.erp.get_purchase_order(po_number)
    if po is None or po.vendor_id != vendor_id:
        return None  # don't reveal that another vendor's PO exists
    ctx.disclose(vendor_id)
    return po


# --------------------------------------------------------------------- handlers


def lookup_vendor(ctx: ToolContext, a: LookupVendor):
    vendors = ctx.erp.search_vendors(name=a.name, tax_id=a.tax_id, email=a.sender_email)
    out = []
    for v in vendors[:10]:
        # Vendor-master basics only; contact email and tax id are withheld, and
        # transactional data (POs, bills) is what counts toward disclosure scope.
        d = v.model_dump(mode="json", exclude={"tax_id", "email"})
        d["tax_id_matches"] = bool(a.tax_id and v.tax_id and a.tax_id.replace("-", "") == v.tax_id.replace("-", ""))
        d["sender_domain_matches"] = bool(ctx.email and ctx.email.sender_domain in v.email_domains)
        d["sender_authorized"] = bool(ctx.email and sender_authorized_for(ctx.db, ctx.policy, ctx.entity, ctx.email, v))
        out.append(d)
    return {"candidates": out, "note": "Pick one only if name/tax id clearly match. sender_domain_matches=false is a fraud signal; sender_authorized=false means no payment details may be shared with the sender."}


def get_purchase_order(ctx: ToolContext, a: GetPO):
    po = _po_for_vendor(ctx, a.vendor_id, a.po_number)
    if po is None:
        return {"found": False, "message": f"No PO {a.po_number} for vendor {a.vendor_id}"}
    return {"found": True, "supports_receiving": po.supports_receiving, **po.model_dump(mode="json")}


def tool_check_duplicate(ctx: ToolContext, a: CheckDuplicate):
    rec = _invoice(ctx, a.invoice_id)
    ctx.disclose(a.vendor_id)
    result = check_duplicate(ctx.erp, ctx.db, vendor_id=a.vendor_id, invoice_number=rec.invoice_number or "", total=Decimal(str(rec.total or 0)), invoice_date=_d(rec.invoice_date), window_days=ctx.policy.duplicate_window_days, exclude_invoice_id=rec.id, entity=ctx.entity, document_type=rec.document_type)
    return result.model_dump()


def tool_match(ctx: ToolContext, a: MatchToPO):
    rec = _invoice(ctx, a.invoice_id)
    po = _po_for_vendor(ctx, a.vendor_id, a.po_number)
    if po is None:
        return {"found": False, "message": f"No PO {a.po_number} for vendor {a.vendor_id}"}
    lines, subtotal = _extracted_lines(rec)
    result = match_invoice(lines, subtotal, po, ctx.policy.tolerances)
    with ctx.db.session() as s, s.begin():
        s.get(InvoiceRecord, rec.id).match_result = result.model_dump(mode="json")
    return result.model_dump(mode="json")


def list_gl_accounts(ctx: ToolContext, a: ListGL):
    accts = ctx.erp.list_gl_accounts()
    if a.search:
        q = a.search.lower()
        accts = [x for x in accts if q in x.name.lower() or q in x.number.lower()]
    return [x.model_dump() for x in accts[:200]]


def coding_history(ctx: ToolContext, a: CodingHistoryQuery):
    with ctx.db.session() as s:
        rows = s.scalars(select(CodingHistory).where(CodingHistory.entity == ctx.entity, CodingHistory.vendor_id == a.vendor_id).order_by(CodingHistory.created_at.desc()).limit(40)).all()
    return {"history": [{"description": r.description, "gl_account": r.gl_account, "corrected_from": r.agent_gl_account if r.corrected_by else None, "amount": str(r.amount)} for r in rows],
            "note": "Lines a human corrected show corrected_from (the account the agent originally chose). Prefer the human's choice for similar lines."}


def submit_invoice(ctx: ToolContext, a: SubmitInvoice):
    """Policy-gated: queues the ERP posting, routes it for human approval, or blocks it."""
    rec = _invoice(ctx, a.invoice_id) if ctx.actor == "agent" else _load_invoice(ctx, a.invoice_id)
    vendor = _vendor(ctx, a.vendor_id)
    ctx.disclose(vendor.id)
    if rec.status not in ("received", "needs_review"):
        raise ToolError(f"Invoice already {rec.status}")
    is_credit = rec.document_type == "credit_note"

    total = Decimal(str(a.total))
    po = None
    if a.po_number and not is_credit:
        po = _po_for_vendor(ctx, vendor.id, a.po_number)
        if po is None:
            raise ToolError(f"No PO {a.po_number} for vendor {vendor.id}")

    # Recompute everything the decision depends on; never trust the model's claims.
    inv_date = _d(a.invoice_date) or date.today()
    dup = check_duplicate(ctx.erp, ctx.db, vendor_id=vendor.id, invoice_number=a.invoice_number, total=total, invoice_date=inv_date, window_days=ctx.policy.duplicate_window_days, exclude_invoice_id=rec.id, entity=ctx.entity, document_type=rec.document_type)
    match: MatchResult | None = None
    if po:
        ext_lines, ext_subtotal = _extracted_lines(rec)  # match on the document, not the coding
        match = match_invoice(ext_lines, ext_subtotal, po, ctx.policy.tolerances)

    extra: list[str] = []
    line_sum = sum((Decimal(str(ln.amount)) for ln in a.lines), Decimal("0")) + Decimal(str(a.tax_total))
    if abs(line_sum - total) > Decimal("0.05"):
        extra.append(f"Lines + tax ({line_sum}) do not sum to total ({total})")
    if rec.total is not None and abs(Decimal(str(rec.total)) - total) > Decimal("0.01"):
        extra.append(f"Submitted total {total} differs from extracted total {rec.total}")
    if ctx.actor != "agent":
        extra.append(f"Submitted manually by {ctx.actor}")

    ext = rec.extracted or {}
    bank = bank_account_status(ctx.db, ctx.erp, entity=ctx.entity, vendor_id=vendor.id, fingerprint=ext.get("bank_fingerprint"), pepper=ctx.bank_pepper)
    flagged = [f["category"] for f in ctx.flags]
    if bank == "unverified":
        flagged.append("bank_detail_change")
        _flag(ctx, "bank_detail_change", f"Invoice prints bank account ending {ext.get('bank_last4')} that isn't verified for this vendor", rec.id)

    bill_lines = [BillLine(description=ln.description, amount=Decimal(str(ln.amount)), quantity=_dec(ln.quantity), unit_price=_dec(ln.unit_price), gl_account=ln.gl_account or vendor.default_gl_account, po_line_id=None if is_credit else ln.po_line_id, item_code=ln.item_code) for ln in a.lines]
    if a.tax_total and ctx.policy.tax_gl_account:
        bill_lines.append(BillLine(description="Tax", amount=Decimal(str(a.tax_total)), gl_account=ctx.policy.tax_gl_account))
    coding_complete = all(ln.gl_account or ln.po_line_id for ln in bill_lines)
    vendor_currency = (po.currency if po else None) or vendor.currency
    functional = to_functional(ctx.db, ctx.erp, total, a.currency, ctx.functional_currency, inv_date)
    subtotal = total - Decimal(str(a.tax_total))

    decision = decide(
        ctx.policy, amount=functional if functional is not None else total, vendor_id=vendor.id, vendor_active=vendor.active,
        currency_ok=not vendor_currency or vendor_currency.upper() == a.currency.upper(),
        duplicate=dup.verdict, match=match, extraction_confidence=float(ext.get("confidence", 0)),
        coding_complete=coding_complete, fx_known=functional is not None, tax_ok=tax_rate_ok(ctx.policy, subtotal, Decimal(str(a.tax_total))),
        tin_missing=ctx.policy.require_tin_for_1099 and vendor.is_1099 and not vendor.tax_id,
        is_credit=is_credit, flagged=sorted(set(flagged)), extra_reasons=extra,
    )

    draft = BillDraft(vendor_id=vendor.id, invoice_number=a.invoice_number, invoice_date=inv_date, due_date=_d(a.due_date), currency=a.currency, total=total, tax_total=Decimal(str(a.tax_total)), po_id=po.id if po else None, po_number=po.number if po else None, lines=bill_lines, memo=f"AP agent {rec.document_type} {rec.id}")
    with ctx.db.session() as s, s.begin():
        r = s.get(InvoiceRecord, rec.id)
        r.vendor_id, r.invoice_number, r.invoice_number_norm = vendor.id, a.invoice_number, normalize_invoice_number(a.invoice_number)
        r.po_number, r.currency, r.functional_total = a.po_number, a.currency, functional
        r.match_result = match.model_dump(mode="json") if match else None
        r.pending_bill = draft.model_dump(mode="json") | {"coding_rationale": a.coding_rationale}
        if ctx.actor == "agent":
            r.agent_coding = {"lines": [ln.model_dump(mode="json") for ln in bill_lines]}
        r.agent_decision = "auto_post" if decision.shadow_auto_post else decision.action.value
        r.submitted_by = None if ctx.actor == "agent" else ctx.actor

    out: dict[str, Any] = {"decision": decision.action.value, "reasons": decision.reasons, "duplicate": dup.model_dump()}
    if match:
        out["match"] = match.model_dump(mode="json")

    if decision.action == Action.block:
        _set_status(ctx.db, rec.id, "duplicate" if dup.verdict == "exact" else "blocked")
        ctx.actions.append(f"blocked {rec.document_type} {rec.id}")
    elif decision.action == Action.require_approval:
        approval_id = request_approval(ctx, kind="invoice", subject_id=rec.id, amount=functional if functional is not None else total, approvers=decision.approvers, reasons=decision.reasons, requires_mfa=decision.requires_mfa, shadow=decision.shadow_auto_post,
                                       summary=f"{vendor.name} {rec.document_type.replace('_', ' ')} {a.invoice_number} for {a.currency} {total}\n{a.coding_rationale}")
        _set_status(ctx.db, rec.id, "awaiting_approval")
        out.update(approval_id=approval_id)
        ctx.actions.append(f"routed {rec.document_type} {rec.id} for approval")
    else:
        _set_status(ctx.db, rec.id, "approved")
        if ctx.enqueue:
            ctx.enqueue("post_invoice", {"invoice_id": rec.id, "actor": ctx.actor}, entity=ctx.entity, dedupe_key=f"post:{rec.id}")
        out["status"] = "queued for ERP posting"
        ctx.actions.append(f"approved by policy; queued {rec.id} for posting")
    ctx.db.audit(ctx.actor, "submit_invoice", rec.id, entity=ctx.entity, decision=out)
    return out


def bill_status(ctx: ToolContext, a: BillStatusQuery):
    vendor = _vendor(ctx, a.vendor_id)
    if ctx.email is not None and not sender_authorized_for(ctx.db, ctx.policy, ctx.entity, ctx.email, vendor):
        return {"allowed": False, "message": "The sender isn't a verified contact for this vendor; don't disclose payment information. Send an acknowledgement and flag for review if it looks suspicious."}
    ctx.disclose(vendor.id)
    bills = ctx.erp.find_bills(vendor_id=vendor.id, invoice_number=a.invoice_number)
    with ctx.db.session() as s:
        q = select(InvoiceRecord).where(InvoiceRecord.entity == ctx.entity, InvoiceRecord.vendor_id == vendor.id)
        if a.invoice_number:
            q = q.where(InvoiceRecord.invoice_number_norm == normalize_invoice_number(a.invoice_number))
        pending = [{"invoice_number": r.invoice_number, "status": r.status, "received": r.created_at.date().isoformat()} for r in s.scalars(q).all() if r.status != "posted"]
    return {"allowed": True, "erp_bills": [b.model_dump(mode="json", include={"invoice_number", "invoice_date", "due_date", "total", "balance", "currency", "status"}) for b in bills[:25]], "in_process": pending}


def reply_to_sender(ctx: ToolContext, a: ReplyToSender):
    if ctx.email is None or ctx.mailbox is None:
        raise ToolError("No email in this task to reply to")
    if ctx.replied:
        raise ToolError("Already replied once in this task")
    if a.kind == "acknowledgement":
        body = ACK_TEMPLATE.format(company=ctx.company)
    elif a.kind == "internal_redirect":
        body = INTERNAL_TEMPLATE.format(portal=ctx.portal_url.rstrip("/"))
    else:
        unauthorized = []
        for vid in sorted(ctx.vendors_disclosed):
            v = ctx.erp.get_vendor(vid)
            if v is None or not sender_authorized_for(ctx.db, ctx.policy, ctx.entity, ctx.email, v):
                unauthorized.append(vid)
        if unauthorized:
            ctx.db.audit("system", "reply_blocked", ctx.email.id, reason="disclosure scope", vendors=unauthorized)
            raise ToolError("Reply blocked: this task read data for vendor(s) the sender isn't a verified contact of. Use kind=acknowledgement instead.")
        if not a.body.strip():
            raise ToolError("Empty reply body")
        body = a.body
    ref = ctx.mailbox.reply(ctx.email, body, send=ctx.reply_mode == "send")
    ctx.replied = True
    ctx.db.audit("agent", "reply", ctx.email.id, kind=a.kind, mode=ctx.reply_mode, body=body)
    ctx.actions.append(f"{'sent' if ctx.reply_mode == 'send' else 'drafted'} {a.kind} reply to {ctx.email.sender}")
    return {"ok": True, "mode": ctx.reply_mode, "ref": ref}


def _flag(ctx: ToolContext, category: str, reason: str, invoice_id: str | None) -> None:
    ctx.flags.append({"category": category, "reason": reason, "invoice_id": invoice_id})
    subject_type, subject_id = ("invoice", invoice_id) if invoice_id else ("email", ctx.email.id if ctx.email else "n/a")
    ctx.db.open_review(kind=category, subject_type=subject_type, subject_id=subject_id, reason=reason, entity=ctx.entity, sender=ctx.email.sender if ctx.email else None)


def flag_for_review(ctx: ToolContext, a: FlagForReview):
    invoice_id = a.invoice_id if a.invoice_id in ctx.invoice_ids else None
    _flag(ctx, a.category, a.reason, invoice_id)
    if invoice_id:
        _set_status(ctx.db, invoice_id, "needs_review")
    ctx.actions.append(f"flagged: {a.category}")
    return {"ok": True}


def list_open_bills(ctx: ToolContext, a: ListOpenBills):
    due_before = date.today() + timedelta(days=max(0, min(a.due_within_days, 120)))
    bills = ctx.erp.list_open_bills(due_before=due_before)
    vendors: dict[str, Any] = {}
    out = []
    for b in bills[:500]:
        if b.vendor_id not in vendors:
            vendors[b.vendor_id] = ctx.erp.get_vendor(b.vendor_id)
        v = vendors[b.vendor_id]
        ctx.disclose(b.vendor_id)
        out.append({**b.model_dump(mode="json"), "vendor_name": v.name if v else None, "payment_terms": v.payment_terms if v else None, "vendor_active": v.active if v else False})
    return {"today": date.today().isoformat(), "bills": out}


def propose_payment_run(ctx: ToolContext, a: ProposePaymentRun):
    """Records a proposal and sends it for approval. Never pays anything."""
    open_bills = {b.id: b for b in ctx.erp.list_open_bills()}
    items, problems = [], []
    for p in a.payments:
        b = open_bills.get(p.bill_id)
        amt = Decimal(str(p.amount))
        if b is None:
            problems.append(f"{p.bill_id}: not an open bill")
        elif b.vendor_id != p.vendor_id:
            problems.append(f"{p.bill_id}: vendor mismatch")
        elif amt <= 0 or amt > (b.balance if b.balance is not None else b.total):
            problems.append(f"{p.bill_id}: amount {amt} exceeds open balance")
        else:
            items.append({"bill_id": b.id, "vendor_id": b.vendor_id, "invoice_number": b.invoice_number, "amount": str(amt), "partial": amt < (b.balance or b.total), "reason": p.reason})
    if problems:
        raise ToolError("Fix these and resubmit: " + "; ".join(problems))
    total = sum((Decimal(i["amount"]) for i in items), Decimal("0"))
    with ctx.db.session() as s, s.begin():
        run = PaymentRunRecord(entity=ctx.entity, payment_date=a.payment_date, items=items, total=total, rationale=a.rationale)
        s.add(run)
    approval_id = request_approval(ctx, kind="payment_run", subject_id=run.id, amount=total, approvers=ctx.policy.payment_run_approvers, reasons=["Payment runs always require approval"], requires_mfa=ctx.policy.require_mfa_for_payment_approval, summary=f"Payment run {a.payment_date}: {len(items)} bills, total {total}\n{a.rationale}")
    ctx.actions.append(f"proposed payment run {run.id}")
    return {"payment_run_id": run.id, "total": str(total), "approval_id": approval_id, "status": "awaiting_approval"}


# ---- internal Q&A (read-only)


def search_invoices(ctx: ToolContext, a: SearchInvoices):
    with ctx.db.session() as s:
        q = select(InvoiceRecord).where(InvoiceRecord.entity == ctx.entity).order_by(InvoiceRecord.created_at.desc()).limit(max(1, min(a.limit, 50)))
        if a.vendor_id:
            q = q.where(InvoiceRecord.vendor_id == a.vendor_id)
        if a.invoice_number:
            q = q.where(InvoiceRecord.invoice_number_norm == normalize_invoice_number(a.invoice_number))
        if a.status:
            q = q.where(InvoiceRecord.status == a.status)
        rows = s.scalars(q).all()
    return [{"invoice_id": r.id, "vendor_id": r.vendor_id, "invoice_number": r.invoice_number, "type": r.document_type, "total": str(r.total) if r.total is not None else None, "currency": r.currency, "status": r.status, "erp_bill_id": r.erp_bill_id, "received": r.created_at.date().isoformat()} for r in rows]


def internal_bill_status(ctx: ToolContext, a: BillStatusQuery):
    _vendor(ctx, a.vendor_id)
    return {"erp_bills": [b.model_dump(mode="json") for b in ctx.erp.find_bills(vendor_id=a.vendor_id, invoice_number=a.invoice_number)[:50]]}


def ap_summary(ctx: ToolContext, a: APSummary):
    today = _d(a.as_of) or date.today()
    buckets = {"current": Decimal(0), "1-30": Decimal(0), "31-60": Decimal(0), "61-90": Decimal(0), "90+": Decimal(0)}
    for b in ctx.erp.list_open_bills():
        bal = b.balance if b.balance is not None else b.total
        days = (today - b.due_date).days if b.due_date else 0
        key = "current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
        buckets[key] += bal
    with ctx.db.session() as s:
        by_status = dict(s.execute(select(InvoiceRecord.status, func.count()).where(InvoiceRecord.entity == ctx.entity).group_by(InvoiceRecord.status)).all())
        pending = s.scalar(select(func.count()).select_from(ApprovalRecord).where(ApprovalRecord.entity == ctx.entity, ApprovalRecord.status == "pending"))
    return {"as_of": today.isoformat(), "aging_open_balance": {k: str(v) for k, v in buckets.items()}, "agent_invoices_by_status": by_status, "pending_approvals": pending}


# --------------------------------------------------------------------- shared


def _load_invoice(ctx: ToolContext, invoice_id: str) -> InvoiceRecord:
    with ctx.db.session() as s:
        rec = s.get(InvoiceRecord, invoice_id)
    if rec is None or rec.entity != ctx.entity:
        raise ToolError("Invoice not found")
    return rec


def _extracted_lines(rec: InvoiceRecord) -> tuple[list[InvoiceLineIn], Decimal]:
    ext = rec.extracted or {}
    lines = [
        InvoiceLineIn(
            description=ln.get("description") or "",
            quantity=_dec(ln.get("quantity")),
            unit_price=_dec(ln.get("unit_price")),
            amount=Decimal(str(ln.get("amount") or 0)),
            item_code=ln.get("item_code"),
            po_line_reference=ln.get("po_line_reference"),
        )
        for ln in ext.get("lines", [])
    ]
    subtotal = ext.get("subtotal")
    if subtotal is None:
        subtotal = (ext.get("total") or 0) - (ext.get("tax_total") or 0)
    return lines, Decimal(str(subtotal))


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _dec(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _set_status(db: Database, invoice_id: str, status: str) -> None:
    with db.session() as s, s.begin():
        s.get(InvoiceRecord, invoice_id).status = status


def request_approval(ctx: ToolContext, *, kind: str, subject_id: str, amount: Decimal, approvers: list[str], reasons: list[str], summary: str, requires_mfa: bool = False, shadow: bool = False) -> str:
    with ctx.db.session() as s, s.begin():
        appr = ApprovalRecord(entity=ctx.entity, kind=kind, subject_id=subject_id, amount=amount, approvers=approvers, reasons=reasons, requires_mfa=requires_mfa, shadow_auto_post=shadow)
        s.add(appr)
    base = ctx.portal_url.rstrip("/")
    body = (
        f"Approval needed ({kind}, entity {ctx.entity}).\n\n{summary}\n\nWhy approval is required:\n- " + "\n- ".join(reasons)
        + f"\n\nReview and decide (sign-in required): {base}/approvals/{appr.id}?token={appr.token}\n"
    )
    emails = [a for a in approvers if "@" in a and not a.startswith("group:")]
    if ctx.notify and emails:
        try:
            ctx.notify(emails, f"[AP approval] {summary.splitlines()[0][:120]}", body)
        except Exception:  # notification failure must not lose the approval record
            log.exception("Failed to send approval notification %s", appr.id)
    ctx.db.audit("system", "approval_requested", appr.id, kind=kind, subject=subject_id, approvers=approvers, reasons=reasons, entity=ctx.entity)
    return appr.id


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("lookup_vendor", "Search the ERP vendor master by name, tax id and/or sender email. Returns candidates with tax_id_matches, sender_domain_matches and sender_authorized.", LookupVendor, lookup_vendor),
    Tool("get_purchase_order", "Fetch a PO for a given vendor, with lines and ordered/received/billed quantities. Returns found=false if the PO isn't that vendor's.", GetPO, get_purchase_order),
    Tool("check_duplicate", "Check the ERP and the agent's registry for duplicates of an invoice. Returns verdict none|possible|exact.", CheckDuplicate, tool_check_duplicate),
    Tool("match_invoice_to_po", "Run the deterministic 2-way/3-way match of an invoice against the vendor's PO using policy tolerances.", MatchToPO, tool_match),
    Tool("list_gl_accounts", "List postable GL expense/asset accounts, optionally filtered.", ListGL, list_gl_accounts),
    Tool("get_vendor_coding_history", "Recent final GL coding for this vendor's bills, including human corrections. Use it to code consistently.", CodingHistoryQuery, coding_history),
    Tool("submit_invoice", "Submit a fully coded invoice or credit note. The policy engine then queues it for ERP posting, routes it for human approval, or blocks it. Call at most once per document.", SubmitInvoice, submit_invoice),
    Tool("get_bill_status", "Status, due date and balance of a vendor's bills, for vendor inquiries. Returns allowed=false unless the sender is a verified contact of that vendor.", BillStatusQuery, bill_status),
    Tool("reply_to_sender", "Reply in-thread to the sender, once per task. kind=acknowledgement and internal_redirect send fixed text; kind=answer sends your text and is only allowed if the sender is a verified contact of every vendor whose data you looked at.", ReplyToSender, reply_to_sender),
    Tool("flag_for_review", "Put the email/invoice in the human AP review queue.", FlagForReview, flag_for_review),
    Tool("list_open_bills", "List open ERP bills due within N days, with vendor terms.", ListOpenBills, list_open_bills),
    Tool("propose_payment_run", "Propose a payment run (full or partial amounts). Records it and sends it to approvers; nothing is paid until a human approves.", ProposePaymentRun, propose_payment_run),
    Tool("search_invoices", "Search invoices and credit notes the AP agent has received for this entity.", SearchInvoices, search_invoices),
    Tool("get_vendor_bills", "All ERP bills for a vendor (internal staff view).", BillStatusQuery, internal_bill_status),
    Tool("ap_summary", "AP aging of open ERP balances, agent invoice counts by status and pending approvals.", APSummary, ap_summary),
]}

EMAIL_TOOLS = [TOOLS[n] for n in ("lookup_vendor", "get_purchase_order", "check_duplicate", "match_invoice_to_po", "list_gl_accounts", "get_vendor_coding_history", "submit_invoice", "get_bill_status", "reply_to_sender", "flag_for_review")]
PAYMENT_TOOLS = [TOOLS[n] for n in ("list_open_bills", "lookup_vendor", "propose_payment_run")]
ASK_TOOLS = [TOOLS[n] for n in ("lookup_vendor", "get_purchase_order", "search_invoices", "get_vendor_bills", "list_open_bills", "ap_summary", "get_vendor_coding_history")]


def run_tool(tools: dict[str, Tool], ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Validate then execute. Returns (content, is_error)."""
    tool = tools.get(name)
    if tool is None:
        return f"Unknown tool {name}", True
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as e:
        return json.dumps({"INVALID_INPUT": str(e)[:2000], "received": raw_input}, default=str), True
    try:
        return _dump(tool.handler(ctx, args)), False
    except ToolError as e:
        return f"Error: {e}", True
    except ERPError as e:
        log.warning("ERP error in %s: %s", name, e)
        return f"ERP error: {e}", True
