"""The agent's tool surface.

Every tool input is a Pydantic model (used both to generate the strict JSON
schema the model sees and to validate what comes back before anything runs).
Mutating tools route through the policy engine; there is deliberately no tool
that edits vendor master data, changes bank details, or releases payments.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from ap_agent.ap.duplicates import check_duplicate, normalize_invoice_number
from ap_agent.ap.matching import InvoiceLineIn, MatchResult, match_invoice
from ap_agent.ap.policy import Action, Policy, decide
from ap_agent.erp.base import ERPConnector, ERPError
from ap_agent.mail.base import EmailMessage, MailboxConnector
from ap_agent.models import BillDraft, BillLine, BillStatus
from ap_agent.storage.db import ApprovalRecord, Database, InvoiceRecord, PaymentRunRecord

log = logging.getLogger(__name__)


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
    po_number: str


class CheckDuplicate(BaseModel):
    invoice_id: str = Field(description="Agent invoice id from the task")
    vendor_id: str


class MatchToPO(BaseModel):
    invoice_id: str
    po_number: str


class ListGL(BaseModel):
    search: str | None = Field(description="Optional substring filter on account name/number")


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
    total: float = Field(description="Invoice total including tax")
    tax_total: float
    lines: list[CodedLine]
    coding_rationale: str = Field(description="Short explanation of GL coding / match decisions for the approver")


class BillStatusQuery(BaseModel):
    vendor_id: str
    invoice_number: str | None = Field(description="Specific invoice; omit to list all of the vendor's bills")


class ReplyToSender(BaseModel):
    body: str = Field(description="Plain-text reply to the vendor. Professional, concise, signed as Accounts Payable.")


class FlagForReview(BaseModel):
    category: Literal["bank_detail_change", "suspected_fraud", "vendor_not_found", "unreadable", "dispute", "duplicate", "policy_exception", "other"]
    reason: str
    invoice_id: str | None


class ListOpenBills(BaseModel):
    due_within_days: int = Field(description="Include bills due within this many days")


class ProposedPayment(BaseModel):
    bill_id: str
    vendor_id: str
    amount: float
    reason: str = Field(description="e.g. 'due 2026-12-01' or 'take 2% discount by 2026-11-30'")


class ProposePaymentRun(BaseModel):
    payment_date: str = Field(description="YYYY-MM-DD")
    payments: list[ProposedPayment]
    rationale: str


# --------------------------------------------------------------------- context


@dataclass
class ToolContext:
    erp: ERPConnector
    mailbox: MailboxConnector
    db: Database
    policy: Policy
    reply_mode: str = "draft"
    email: EmailMessage | None = None
    invoice_ids: list[str] = field(default_factory=list)
    replied: bool = False
    flags: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


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
    if rec is None:
        raise ToolError(f"Invoice {invoice_id} not found")
    return rec


class ToolError(Exception):
    pass


# --------------------------------------------------------------------- handlers


def lookup_vendor(ctx: ToolContext, a: LookupVendor):
    vendors = ctx.erp.search_vendors(name=a.name, tax_id=a.tax_id, email=a.sender_email)
    out = []
    for v in vendors[:10]:
        d = v.model_dump(mode="json")
        d["sender_domain_matches"] = bool(ctx.email and ctx.email.sender_domain in v.email_domains)
        out.append(d)
    return {"candidates": out, "note": "Pick one only if name/tax id clearly match. sender_domain_matches=false is a fraud signal."}


def get_purchase_order(ctx: ToolContext, a: GetPO):
    po = ctx.erp.get_purchase_order(a.po_number)
    if po is None:
        return {"found": False}
    return {"found": True, "supports_receiving": po.supports_receiving, **po.model_dump(mode="json")}


def tool_check_duplicate(ctx: ToolContext, a: CheckDuplicate):
    rec = _invoice(ctx, a.invoice_id)
    result = check_duplicate(ctx.erp, ctx.db, vendor_id=a.vendor_id, invoice_number=rec.invoice_number or "", total=Decimal(str(rec.total or 0)), invoice_date=_d(rec.invoice_date), window_days=ctx.policy.duplicate_window_days, exclude_invoice_id=rec.id)
    return result.model_dump()


def tool_match(ctx: ToolContext, a: MatchToPO):
    rec = _invoice(ctx, a.invoice_id)
    po = ctx.erp.get_purchase_order(a.po_number)
    if po is None:
        return {"found": False, "message": f"PO {a.po_number} not found in ERP"}
    lines, subtotal = _extracted_lines(rec)
    result = match_invoice(lines, subtotal, po, ctx.policy.tolerances)
    with ctx.db.session() as s, s.begin():
        s.get(InvoiceRecord, rec.id).match_result = result.model_dump(mode="json")
    return {"po_vendor_id": po.vendor_id, **result.model_dump(mode="json")}


def list_gl_accounts(ctx: ToolContext, a: ListGL):
    accts = ctx.erp.list_gl_accounts()
    if a.search:
        q = a.search.lower()
        accts = [x for x in accts if q in x.name.lower() or q in x.number.lower()]
    return [x.model_dump() for x in accts[:200]]


def submit_invoice(ctx: ToolContext, a: SubmitInvoice):
    """Policy-gated: auto-posts, routes for approval, or blocks."""
    rec = _invoice(ctx, a.invoice_id)
    vendor = ctx.erp.get_vendor(a.vendor_id)
    if vendor is None:
        raise ToolError(f"Vendor {a.vendor_id} not found")
    if rec.status not in ("received", "needs_review"):
        raise ToolError(f"Invoice already {rec.status}")

    total = Decimal(str(a.total))
    po = ctx.erp.get_purchase_order(a.po_number) if a.po_number else None
    if a.po_number and po is None:
        raise ToolError(f"PO {a.po_number} not found")
    if po and po.vendor_id != vendor.id:
        raise ToolError("PO belongs to a different vendor")

    # Recompute everything the decision depends on; never trust the model's claims.
    dup = check_duplicate(ctx.erp, ctx.db, vendor_id=vendor.id, invoice_number=a.invoice_number, total=total, invoice_date=_d(a.invoice_date), window_days=ctx.policy.duplicate_window_days, exclude_invoice_id=rec.id)
    match: MatchResult | None = None
    if po:
        # Match on what the document says (extraction), not on the model's coding.
        ext_lines, subtotal = _extracted_lines(rec)
        match = match_invoice(ext_lines, subtotal, po, ctx.policy.tolerances)

    line_sum = sum((Decimal(str(ln.amount)) for ln in a.lines), Decimal("0")) + Decimal(str(a.tax_total))
    extracted_total = Decimal(str(rec.total or 0))
    reasons_extra = []
    if abs(line_sum - total) > Decimal("0.05"):
        reasons_extra.append(f"Lines + tax ({line_sum}) do not sum to total ({total})")
    if rec.total is not None and abs(extracted_total - total) > Decimal("0.01"):
        reasons_extra.append(f"Submitted total {total} differs from extracted total {extracted_total}")

    bill_lines = [BillLine(description=ln.description, amount=Decimal(str(ln.amount)), quantity=_dec(ln.quantity), unit_price=_dec(ln.unit_price), gl_account=ln.gl_account or vendor.default_gl_account, po_line_id=ln.po_line_id, item_code=ln.item_code) for ln in a.lines]
    if a.tax_total and ctx.policy.tax_gl_account:
        # Otherwise the ERP's own tax engine computes tax on the lines.
        bill_lines.append(BillLine(description="Tax", amount=Decimal(str(a.tax_total)), gl_account=ctx.policy.tax_gl_account))
    coding_complete = all(ln.gl_account or ln.po_line_id for ln in bill_lines)
    vendor_currency = (po.currency if po else None) or vendor.currency

    decision = decide(
        ctx.policy, amount=total, vendor_id=vendor.id, vendor_active=vendor.active,
        currency_ok=not vendor_currency or vendor_currency.upper() == a.currency.upper(),
        duplicate=dup.verdict, match=match,
        extraction_confidence=float((rec.extracted or {}).get("confidence", 0)),
        coding_complete=coding_complete,
    )
    if reasons_extra and decision.action == Action.auto_post:
        decision.action = Action.require_approval
        decision.approvers = ctx.policy.approvers_for(total)
    decision.reasons.extend(reasons_extra)
    if ctx.flags:
        if decision.action == Action.auto_post:
            decision.action = Action.require_approval
            decision.approvers = ctx.policy.approvers_for(total)
        decision.reasons.append("Email was flagged for review: " + "; ".join(f["category"] for f in ctx.flags))

    draft = BillDraft(vendor_id=vendor.id, invoice_number=a.invoice_number, invoice_date=_d(a.invoice_date), due_date=_d(a.due_date), currency=a.currency, total=total, tax_total=Decimal(str(a.tax_total)), po_id=po.id if po else None, po_number=po.number if po else None, lines=bill_lines, memo=f"AP agent invoice {rec.id}")

    with ctx.db.session() as s, s.begin():
        r = s.get(InvoiceRecord, rec.id)
        r.vendor_id, r.invoice_number, r.invoice_number_norm = vendor.id, a.invoice_number, normalize_invoice_number(a.invoice_number)
        r.po_number, r.currency = a.po_number, a.currency
        r.match_result = match.model_dump(mode="json") if match else None
        r.pending_bill = draft.model_dump(mode="json") | {"coding_rationale": a.coding_rationale}

    out: dict[str, Any] = {"decision": decision.action.value, "reasons": decision.reasons, "duplicate": dup.model_dump()}
    if match:
        out["match"] = match.model_dump(mode="json")

    if decision.action == Action.block:
        _set_status(ctx.db, rec.id, "duplicate" if dup.verdict == "exact" else "blocked")
        ctx.actions.append(f"blocked invoice {rec.id}")
    elif decision.action == Action.require_approval:
        approval_id = request_approval(ctx, kind="invoice", subject_id=rec.id, amount=total, approvers=decision.approvers, reasons=decision.reasons, summary=f"{vendor.name} invoice {a.invoice_number} for {a.currency} {total}\n{a.coding_rationale}")
        _set_status(ctx.db, rec.id, "awaiting_approval")
        out["approval_id"] = approval_id
        out["approvers"] = decision.approvers
        ctx.actions.append(f"routed invoice {rec.id} for approval")
    else:
        bill = post_bill(ctx.erp, ctx.db, rec.id, draft, actor="agent")
        out["erp_bill_id"] = bill.id
        ctx.actions.append(f"posted invoice {rec.id} as ERP bill {bill.id}")
    ctx.db.audit("agent", "submit_invoice", rec.id, decision=out)
    return out


def bill_status(ctx: ToolContext, a: BillStatusQuery):
    vendor = ctx.erp.get_vendor(a.vendor_id)
    if vendor is None:
        raise ToolError("Vendor not found")
    if ctx.email and ctx.email.sender_domain not in vendor.email_domains:
        return {"allowed": False, "message": "Sender's email domain is not on file for this vendor; do not disclose payment information. Flag for review."}
    bills = ctx.erp.find_bills(vendor_id=vendor.id, invoice_number=a.invoice_number)
    with ctx.db.session() as s:
        q = select(InvoiceRecord).where(InvoiceRecord.vendor_id == vendor.id)
        if a.invoice_number:
            q = q.where(InvoiceRecord.invoice_number_norm == normalize_invoice_number(a.invoice_number))
        pending = [{"invoice_number": r.invoice_number, "status": r.status, "received": r.created_at.date().isoformat()} for r in s.scalars(q).all() if r.status != "posted"]
    return {"allowed": True, "erp_bills": [b.model_dump(mode="json", include={"invoice_number", "invoice_date", "due_date", "total", "balance", "currency", "status"}) for b in bills[:25]], "in_process": pending}


def reply_to_sender(ctx: ToolContext, a: ReplyToSender):
    if ctx.email is None:
        raise ToolError("No email in this task to reply to")
    if ctx.replied:
        raise ToolError("Already replied once in this task")
    ref = ctx.mailbox.reply(ctx.email, a.body, send=ctx.reply_mode == "send")
    ctx.replied = True
    ctx.db.audit("agent", "reply", ctx.email.id, mode=ctx.reply_mode, body=a.body)
    ctx.actions.append(f"{'sent' if ctx.reply_mode == 'send' else 'drafted'} reply to {ctx.email.sender}")
    return {"ok": True, "mode": ctx.reply_mode, "ref": ref}


def flag_for_review(ctx: ToolContext, a: FlagForReview):
    ctx.flags.append(a.model_dump())
    if a.invoice_id and a.invoice_id in ctx.invoice_ids:
        _set_status(ctx.db, a.invoice_id, "needs_review")
    ctx.db.audit("agent", "flag", a.invoice_id or (ctx.email.id if ctx.email else None), **a.model_dump())
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
            items.append({"bill_id": b.id, "vendor_id": b.vendor_id, "invoice_number": b.invoice_number, "amount": str(amt), "reason": p.reason})
    if problems:
        raise ToolError("Fix these and resubmit: " + "; ".join(problems))
    total = sum((Decimal(i["amount"]) for i in items), Decimal("0"))
    with ctx.db.session() as s, s.begin():
        run = PaymentRunRecord(payment_date=a.payment_date, items=items, total=total, rationale=a.rationale)
        s.add(run)
    approval_id = request_approval(ctx, kind="payment_run", subject_id=run.id, amount=total, approvers=ctx.policy.payment_run_approvers, reasons=["Payment runs always require approval"], summary=f"Payment run {a.payment_date}: {len(items)} bills, total {total}\n{a.rationale}")
    ctx.actions.append(f"proposed payment run {run.id}")
    return {"payment_run_id": run.id, "total": str(total), "approval_id": approval_id, "status": "awaiting_approval"}


# --------------------------------------------------------------------- shared


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


def request_approval(ctx: ToolContext, *, kind: str, subject_id: str, amount: Decimal, approvers: list[str], reasons: list[str], summary: str) -> str:
    from ap_agent.config import get_settings

    with ctx.db.session() as s, s.begin():
        appr = ApprovalRecord(kind=kind, subject_id=subject_id, amount=amount, approvers=approvers, reasons=reasons)
        s.add(appr)
    base = get_settings().public_base_url.rstrip("/")
    body = (
        f"Approval needed ({kind}).\n\n{summary}\n\nWhy approval is required:\n- " + "\n- ".join(reasons)
        + f"\n\nApprove: {base}/approvals/{appr.id}/approve?token={appr.token}\nReject:  {base}/approvals/{appr.id}/reject?token={appr.token}\n"
    )
    try:
        ctx.mailbox.send(approvers, f"[AP approval] {summary.splitlines()[0][:120]}", body)
    except Exception:  # notification failure must not lose the approval record
        log.exception("Failed to send approval notification %s", appr.id)
    ctx.db.audit("system", "approval_requested", appr.id, kind=kind, subject=subject_id, approvers=approvers, reasons=reasons)
    return appr.id


def post_bill(erp: ERPConnector, db: Database, invoice_id: str, draft: BillDraft, actor: str):
    bill = erp.create_bill(draft)
    with db.session() as s, s.begin():
        r = s.get(InvoiceRecord, invoice_id)
        r.erp_bill_id, r.status = bill.id, "posted"
    db.audit(actor, "bill_posted", invoice_id, erp_bill_id=bill.id, erp=erp.name)
    return bill


TOOLS: list[Tool] = [
    Tool("lookup_vendor", "Search the ERP vendor master by name, tax id and/or sender email. Returns candidates with sender_domain_matches.", LookupVendor, lookup_vendor),
    Tool("get_purchase_order", "Fetch a PO with lines, ordered/received/billed quantities and unit prices.", GetPO, get_purchase_order),
    Tool("check_duplicate", "Check the ERP and the agent's registry for duplicates of an invoice. Returns verdict none|possible|exact.", CheckDuplicate, tool_check_duplicate),
    Tool("match_invoice_to_po", "Run the deterministic 2-way/3-way match of an invoice against a PO using policy tolerances.", MatchToPO, tool_match),
    Tool("list_gl_accounts", "List postable GL expense/asset accounts, optionally filtered.", ListGL, list_gl_accounts),
    Tool("submit_invoice", "Submit a fully coded invoice. The policy engine then auto-posts it to the ERP, routes it for human approval, or blocks it. Call at most once per invoice.", SubmitInvoice, submit_invoice),
    Tool("get_bill_status", "Look up the status, due date and balance of a vendor's bills (for vendor inquiries). Only discloses to the vendor's known email domains.", BillStatusQuery, bill_status),
    Tool("reply_to_sender", "Reply in-thread to the email's sender. One reply per task. Never include internal notes, approver names or bank details.", ReplyToSender, reply_to_sender),
    Tool("flag_for_review", "Flag the email/invoice for a human AP specialist.", FlagForReview, flag_for_review),
    Tool("list_open_bills", "List open ERP bills due within N days, with vendor terms.", ListOpenBills, list_open_bills),
    Tool("propose_payment_run", "Propose a payment run. Records it and sends it to approvers; nothing is paid until a human approves.", ProposePaymentRun, propose_payment_run),
]

EMAIL_TOOLS = [t for t in TOOLS if t.name not in ("list_open_bills", "propose_payment_run")]
PAYMENT_TOOLS = [t for t in TOOLS if t.name in ("list_open_bills", "get_bill_status", "lookup_vendor", "propose_payment_run")]


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
