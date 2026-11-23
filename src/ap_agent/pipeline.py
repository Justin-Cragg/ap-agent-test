"""Orchestration: mailbox -> extraction -> agent, plus approval execution and
payment runs. Shared by the HTTP API, the CLI and scheduled jobs."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ap_agent.agent.prompts import EMAIL_SYSTEM, PAYMENT_RUN_SYSTEM
from ap_agent.agent.runner import Agent, AgentResult, render_email_task
from ap_agent.agent.tools import EMAIL_TOOLS, PAYMENT_TOOLS, ToolContext, post_bill
from ap_agent.ap.duplicates import normalize_invoice_number
from ap_agent.ap.extraction import extract_invoice, is_candidate
from ap_agent.ap.policy import Policy
from ap_agent.config import Settings
from ap_agent.erp import ERPConnector, build_erp
from ap_agent.llm.client import LLM
from ap_agent.mail import EmailMessage, MailboxConnector, build_mailbox
from ap_agent.mail.base import PROCESSED_LABEL, REVIEW_LABEL
from ap_agent.models import Bill, BillDraft, BillStatus
from ap_agent.storage import BlobStore, Database, build_blob_store
from ap_agent.storage.db import ApprovalRecord, EmailRecord, InvoiceRecord, PaymentRunRecord, _now

log = logging.getLogger(__name__)


class APService:
    def __init__(self, settings: Settings, *, llm: LLM | None = None, erp: ERPConnector | None = None, mailbox: MailboxConnector | None = None, db: Database | None = None, blobs: BlobStore | None = None, policy: Policy | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.llm = llm or LLM(settings)
        self.erp = erp or build_erp(settings, self.db)
        self.mailbox = mailbox or build_mailbox(settings)
        self.blobs = blobs or build_blob_store(settings)
        self.policy = policy or Policy.load(settings.policy_file)
        company = settings.company_name
        self.email_agent = Agent(self.llm, EMAIL_TOOLS, EMAIL_SYSTEM.format(company=company))
        self.payment_agent = Agent(self.llm, PAYMENT_TOOLS, PAYMENT_RUN_SYSTEM.format(company=company))

    def _ctx(self, email: EmailMessage | None = None) -> ToolContext:
        return ToolContext(erp=self.erp, mailbox=self.mailbox, db=self.db, policy=self.policy, reply_mode=self.settings.mail_reply_mode, email=email)

    # ------------------------------------------------------------------ inbox
    def poll_mailbox(self, limit: int = 25) -> list[dict[str, Any]]:
        results = []
        for email in self.mailbox.list_new_messages(self.settings.mail_lookback_days, limit):
            if self.db.email_seen(email.id):
                self.mailbox.label(email, PROCESSED_LABEL)
                continue
            try:
                results.append(self.process_email(email))
            except Exception as e:  # one bad email must not stop the batch
                log.exception("Failed processing %s", email.id)
                self._email_status(email.id, "error", str(e)[:2000])
                self.mailbox.label(email, REVIEW_LABEL)
                results.append({"message_id": email.id, "status": "error", "error": str(e)})
        return results

    def process_email(self, email: EmailMessage) -> dict[str, Any]:
        with self.db.session() as s, s.begin():
            rec = EmailRecord(provider=self.mailbox.name, message_id=email.id, thread_id=email.thread_id, sender=email.sender, subject=email.subject, received_at=email.received_at)
            s.add(rec)
        email_id = rec.id

        invoices, skipped = [], []
        for att in email.attachments:
            if not is_candidate(att):
                if not att.content_type.startswith("image/"):  # inline logos etc. are noise
                    skipped.append(f"{att.filename} ({att.content_type})")
                continue
            uri = self.blobs.put(f"invoices/{email_id}/{att.filename}", att.data, att.content_type)
            try:
                ext = extract_invoice(self.llm, att)
            except Exception as e:
                log.warning("Extraction failed for %s: %s", att.filename, e)
                skipped.append(f"{att.filename} (extraction failed)")
                continue
            if not ext.is_invoice:
                invoices.append({"attachment": att.filename, "document_type": ext.document_type, "note": "not an invoice", "summary": ext.model_dump(exclude={"lines"})})
                continue
            with self.db.session() as s, s.begin():
                inv = InvoiceRecord(email_id=email_id, attachment_name=att.filename, blob_uri=uri, invoice_number=ext.invoice_number, invoice_number_norm=normalize_invoice_number(ext.invoice_number), invoice_date=ext.invoice_date, total=ext.total, currency=ext.currency, po_number=ext.po_number, extracted=ext.model_dump(mode="json"))
                s.add(inv)
            invoices.append({"invoice_id": inv.id, "attachment": att.filename, **ext.model_dump(mode="json")})

        ctx = self._ctx(email)
        ctx.invoice_ids = [i["invoice_id"] for i in invoices if "invoice_id" in i]
        bank_change = self.policy.mentions_bank_change(f"{email.subject}\n{email.body_text}") or any(i.get("remit_to_bank_details_present") and self._bank_details_new(i) for i in invoices)
        if bank_change:
            ctx.flags.append({"category": "bank_detail_change", "reason": "Detected by keyword/document control", "invoice_id": None})
            self.db.audit("system", "flag", email_id, category="bank_detail_change")

        result = self.email_agent.run(ctx, render_email_task(email, invoices, skipped, bank_change))
        self._email_status(email.id, result.status, result.summary)
        self.mailbox.label(email, PROCESSED_LABEL)
        if result.status != "done":
            self.mailbox.label(email, REVIEW_LABEL)
        return {"message_id": email.id, **result.__dict__}

    def _bank_details_new(self, invoice: dict[str, Any]) -> bool:
        # Printed bank details are normal on invoices; they matter only when the
        # vendor is new to us. Real comparison against vendor master bank data
        # needs ERP-specific bank-record access; treat unknown vendors as new.
        name = invoice.get("vendor_name")
        return not (name and self.erp.search_vendors(name=name))

    def _email_status(self, message_id: str, status: str, summary: str) -> None:
        with self.db.session() as s, s.begin():
            rec = s.scalar(select(EmailRecord).where(EmailRecord.message_id == message_id))
            if rec:
                rec.status, rec.summary = status, summary

    # ------------------------------------------------------------------ payments
    def propose_payment_run(self, payment_date: date | None = None, next_run_date: date | None = None) -> AgentResult:
        payment_date = payment_date or date.today()
        next_run_date = next_run_date or payment_date + timedelta(days=7)
        task = f"Prepare the payment run for payment date {payment_date.isoformat()}. The next payment run after this one is {next_run_date.isoformat()}."
        return self.payment_agent.run(self._ctx(), task)

    # ------------------------------------------------------------------ approvals
    def decide_approval(self, approval_id: str, token: str | None, approve: bool, decided_by: str, comment: str | None = None, *, trusted: bool = False) -> dict[str, Any]:
        with self.db.session() as s, s.begin():
            appr = s.get(ApprovalRecord, approval_id)
            if appr is None:
                raise KeyError(approval_id)
            if not trusted and token != appr.token:
                raise PermissionError("invalid approval token")
            if appr.status != "pending":
                return {"status": appr.status, "message": "already decided"}
            if not trusted and decided_by and decided_by.lower() not in [a.lower() for a in appr.approvers]:
                raise PermissionError("not an authorised approver")
            appr.status = "approved" if approve else "rejected"
            appr.decided_by, appr.decided_at, appr.comment = decided_by, _now(), comment
            kind, subject_id = appr.kind, appr.subject_id
        self.db.audit(f"user:{decided_by}", f"approval_{'approved' if approve else 'rejected'}", approval_id, kind=kind, subject=subject_id, comment=comment)

        if kind == "invoice":
            return self._finish_invoice(subject_id, approve, decided_by)
        return self._finish_payment_run(subject_id, approve, decided_by)

    def _finish_invoice(self, invoice_id: str, approve: bool, actor: str) -> dict[str, Any]:
        with self.db.session() as s:
            rec = s.get(InvoiceRecord, invoice_id)
        if not approve:
            with self.db.session() as s, s.begin():
                s.get(InvoiceRecord, invoice_id).status = "rejected"
            return {"invoice_id": invoice_id, "status": "rejected"}
        draft = BillDraft.model_validate({k: v for k, v in rec.pending_bill.items() if k != "coding_rationale"})
        bill = post_bill(self.erp, self.db, invoice_id, draft, actor=f"user:{actor}")
        return {"invoice_id": invoice_id, "status": "posted", "erp_bill_id": bill.id}

    def _finish_payment_run(self, run_id: str, approve: bool, actor: str) -> dict[str, Any]:
        with self.db.session() as s:
            run = s.get(PaymentRunRecord, run_id)
        if not approve:
            with self.db.session() as s, s.begin():
                s.get(PaymentRunRecord, run_id).status = "rejected"
            return {"payment_run_id": run_id, "status": "rejected"}
        pay_date = date.fromisoformat(run.payment_date)
        open_bills = {b.id: b for b in self.erp.list_open_bills()}
        results = []
        for item in run.items:
            bill = open_bills.get(item["bill_id"])
            if bill is None:
                results.append({**item, "result": "skipped: no longer open"})
                continue
            try:
                pid = self.erp.record_payment(bill, Decimal(item["amount"]), pay_date)
                results.append({**item, "result": "recorded", "erp_payment_id": pid})
            except Exception as e:
                results.append({**item, "result": f"error: {e}"})
        with self.db.session() as s, s.begin():
            r = s.get(PaymentRunRecord, run_id)
            r.status, r.results = "executed", results
        self.db.audit(f"user:{actor}", "payment_run_executed", run_id, results=results)
        return {"payment_run_id": run_id, "status": "executed", "results": results}


def build_service(settings: Settings) -> APService:
    return APService(settings)


__all__ = ["APService", "Bill", "BillStatus", "build_service"]
