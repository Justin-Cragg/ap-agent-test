"""Orchestration: mailboxes -> queue -> extraction -> agent, plus approvals,
posting, payment execution, review queue and retention. Shared by the HTTP
API, the CLI and scheduled jobs.

Work flows through a database-backed job queue (claiming is atomic, failed
jobs retry with backoff and land in a dead-letter state after
``job_max_attempts``):

    poll           -> process_email (one job per message, deduplicated)
    agent/approval -> post_invoice  (idempotent ERP write)
    scheduler      -> propose_payment_run
    approval       -> execute_payment_run (separate executor process/credentials
                      when PAYMENT_EXECUTION=separate)
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ap_agent.agent.prompts import ASK_SYSTEM, EMAIL_SYSTEM, PAYMENT_RUN_SYSTEM
from ap_agent.agent.runner import Agent, AgentResult, render_email_task
from ap_agent.agent.tools import ASK_TOOLS, EMAIL_TOOLS, PAYMENT_TOOLS, SubmitInvoice, ToolContext, ToolError, submit_invoice
from ap_agent.ap.controls import redact_bank_details, register_bank_account
from ap_agent.ap.duplicates import normalize_invoice_number
from ap_agent.ap.extraction import extract_invoice, is_candidate
from ap_agent.ap.policy import Policy, PolicyStore
from ap_agent.auth import Principal
from ap_agent.config import Settings
from ap_agent.entities import Entity, load_entities, route
from ap_agent.erp import ERPConnector, build_erp
from ap_agent.llm.client import LLM
from ap_agent.mail import EmailMessage, MailboxConnector, build_mailbox
from ap_agent.mail.base import PROCESSED_LABEL, REVIEW_LABEL
from ap_agent.models import BillDraft
from ap_agent.observability import APPROVAL_LATENCY, DEAD_JOBS, EMAILS, JOBS, PAYMENTS, POSTINGS, record_usage
from ap_agent.storage import BlobStore, Database, build_blob_store
from ap_agent.storage.db import ApprovalRecord, CodingHistory, Delegation, EmailRecord, FxRate, InvoiceRecord, PaymentRunRecord, ReviewItem, VendorContact, _now
from ap_agent.storage.secrets import SecretStore, build_secret_store

log = logging.getLogger(__name__)

FINAL_INVOICE_STATES = ("posted", "rejected", "duplicate", "blocked")


@dataclass
class EntityRuntime:
    entity: Entity
    erp: ERPConnector
    mailbox: MailboxConnector | None = None  # entity-specific AP mailbox, if any


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


class APService:
    def __init__(self, settings: Settings, *, llm: LLM | None = None, db: Database | None = None, blobs: BlobStore | None = None, secrets: SecretStore | None = None,
                 policy: Policy | None = None, erp: ERPConnector | None = None, mailbox: MailboxConnector | None = None, entities: list[Entity] | None = None):
        settings.validate_for_runtime()
        self.settings = settings
        self.db = db or Database(settings.database_url, create_tables=settings.is_dev())
        self.secrets = secrets or build_secret_store(settings, self.db)
        self.llm = llm or LLM(settings)
        self.blobs = blobs or build_blob_store(settings)
        self.policies = PolicyStore(self.db, settings.policy_file, override=policy)
        ents = entities if entities is not None else load_entities(settings)
        self.runtimes: dict[str, EntityRuntime] = {}
        for ent in ents:
            ent_erp = erp if erp is not None and len(ents) == 1 else build_erp(ent.settings, self.secrets, ent.code)
            self.runtimes[ent.code] = EntityRuntime(ent, ent_erp, build_mailbox(ent.settings) if ent.has_own_mailbox else None)
        # Mailboxes to poll: address -> (connector, entity it belongs to or None = route by recipient)
        self.mailboxes: dict[str, tuple[MailboxConnector, str | None]] = {}
        for code, rt in self.runtimes.items():
            if rt.mailbox:
                self.mailboxes[rt.mailbox.address or code] = (rt.mailbox, code)
        if any(rt.mailbox is None for rt in self.runtimes.values()):
            shared = mailbox or build_mailbox(settings)
            self.mailboxes[shared.address or "shared"] = (shared, None)
        company = settings.company_name
        self.email_agent = Agent(self.llm, EMAIL_TOOLS, EMAIL_SYSTEM.format(company=company))
        self.payment_agent = Agent(self.llm, PAYMENT_TOOLS, PAYMENT_RUN_SYSTEM.format(company=company))
        self.ask_agent = Agent(self.llm, ASK_TOOLS, ASK_SYSTEM.format(company=company))

    # ------------------------------------------------------------------ helpers
    @property
    def erp(self) -> ERPConnector:  # single-entity convenience
        return next(iter(self.runtimes.values())).erp

    @property
    def mailbox(self) -> MailboxConnector:
        return next(iter(self.mailboxes.values()))[0]

    def runtime(self, entity: str) -> EntityRuntime:
        if entity not in self.runtimes:
            raise KeyError(f"unknown entity {entity}")
        return self.runtimes[entity]

    def policy(self, entity: str) -> Policy:
        return self.policies.active().for_entity(entity)

    def _notifier(self, entity: str):
        rt = self.runtime(entity)
        mb = rt.mailbox or self.mailbox
        return mb.send

    def _ctx(self, entity: str, email: EmailMessage | None = None, mailbox: MailboxConnector | None = None, actor: str = "agent") -> ToolContext:
        rt = self.runtime(entity)
        return ToolContext(
            erp=rt.erp, mailbox=mailbox or rt.mailbox or self.mailbox, db=self.db, policy=self.policy(entity), entity=entity,
            functional_currency=rt.entity.functional_currency, company=rt.entity.name, portal_url=self.settings.public_base_url,
            bank_pepper=self.settings.bank_fingerprint_pepper or "dev-pepper", reply_mode=self.settings.mail_reply_mode, email=email, actor=actor,
            enqueue=self.db.enqueue, notify=self._notifier(entity),
        )

    # ------------------------------------------------------------------ queue
    def poll_mailboxes(self, limit: int = 50) -> int:
        queued = 0
        for address, (mb, entity) in self.mailboxes.items():
            for mid in mb.list_new_messages(self.settings.mail_lookback_days, limit):
                if self.db.enqueue("process_email", {"mailbox": address, "message_id": mid, "entity": entity}, dedupe_key=f"email:{address}:{mid}", max_attempts=self.settings.job_max_attempts):
                    queued += 1
        return queued

    def work(self, kinds: list[str] | None = None, max_jobs: int = 200, worker_id: str | None = None) -> list[dict[str, Any]]:
        """Process queued jobs until the queue is empty or max_jobs is reached."""
        if kinds is None:
            kinds = ["process_email", "post_invoice", "propose_payment_run"]
            if self.settings.payment_execution == "inline":
                kinds.append("execute_payment_run")
        worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        results = []
        for _ in range(max_jobs):
            job = self.db.claim(kinds, worker_id, self.settings.job_visibility_timeout_s)
            if job is None:
                break
            try:
                out = getattr(self, f"_job_{job.kind}")(job.payload)
                self.db.complete(job.id)
                JOBS.labels(job.kind, "done").inc()
                results.append({"job_id": job.id, "kind": job.kind, "status": "done", "result": out})
            except Exception as e:
                log.exception("Job %s (%s) failed", job.id, job.kind, extra={"job_id": job.id})
                status = self.db.fail(job.id, f"{type(e).__name__}: {e}")
                JOBS.labels(job.kind, status).inc()
                if status == "dead":
                    self.db.open_review(kind="job_failed", subject_type="job", subject_id=job.id, reason=f"{job.kind} failed {job.attempts} times: {e}", entity=job.entity or "default", payload=job.payload)
                results.append({"job_id": job.id, "kind": job.kind, "status": status, "error": str(e)})
        DEAD_JOBS.set(self.db.queue_stats().get("dead", 0))
        return results

    def poll_and_process(self, limit: int = 50) -> list[dict[str, Any]]:
        self.poll_mailboxes(limit)
        return self.work()

    # ------------------------------------------------------------------ email
    def _job_process_email(self, payload: dict[str, Any]) -> dict[str, Any]:
        mb, mb_entity = self.mailboxes[payload["mailbox"]]
        mid = payload["message_id"]
        if self.db.email_seen(mid):
            mb.label(mid, PROCESSED_LABEL)
            return {"message_id": mid, "status": "already_processed"}
        email = mb.get_message(mid)
        entity = payload.get("entity") or mb_entity
        if entity is None:
            ent = route(email.recipients, [rt.entity for rt in self.runtimes.values()])
            entity = ent.code if ent else None
        if entity is None:
            first = next(iter(self.runtimes))
            self._record_email(email, mb, first, status="needs_review", summary="Could not determine the legal entity from the recipients")
            self.db.open_review(kind="entity_unknown", subject_type="email", subject_id=email.id, reason="No AP alias matched the recipients", entity=first, recipients=email.recipients)
            mb.label(email, PROCESSED_LABEL)
            mb.label(email, REVIEW_LABEL)
            return {"message_id": mid, "status": "needs_review"}
        return self.process_email(email, entity=entity, mailbox=mb)

    def _record_email(self, email: EmailMessage, mb: MailboxConnector, entity: str, status: str = "processing", summary: str | None = None) -> str | None:
        try:
            with self.db.session() as s, s.begin():
                rec = EmailRecord(entity=entity, provider=mb.name, message_id=email.id, thread_id=email.thread_id, sender=email.sender, sender_authenticated=email.sender_authenticated, subject=email.subject, received_at=email.received_at, status=status, summary=summary)
                s.add(rec)
            return rec.id
        except IntegrityError:
            return None  # another worker got it first

    def process_email(self, email: EmailMessage, *, entity: str = "default", mailbox: MailboxConnector | None = None) -> dict[str, Any]:
        mb = mailbox or self.runtime(entity).mailbox or self.mailbox
        policy = self.policy(entity)
        email_id = self._record_email(email, mb, entity)
        if email_id is None:
            return {"message_id": email.id, "status": "already_processed"}

        invoices, skipped = [], []
        pepper = self.settings.bank_fingerprint_pepper or "dev-pepper"
        for att in email.attachments:
            if not is_candidate(att):
                if not att.content_type.startswith("image/"):  # inline logos etc. are noise
                    skipped.append(f"{att.filename} ({att.content_type})")
                continue
            uri = self.blobs.put(f"{entity}/invoices/{email_id}/{att.filename}", att.data, att.content_type)
            try:
                ext = extract_invoice(self.llm, att)
            except Exception as e:
                log.warning("Extraction failed for %s: %s", att.filename, e)
                skipped.append(f"{att.filename} (extraction failed)")
                continue
            data = redact_bank_details(ext.model_dump(mode="json"), pepper)
            if not ext.is_invoice:
                invoices.append({"attachment": att.filename, "document_type": ext.document_type, "note": "not an invoice", "summary": {k: v for k, v in data.items() if k != "lines"}})
                continue
            doc_type = "credit_note" if ext.document_type == "credit_note" else "invoice"
            with self.db.session() as s, s.begin():
                inv = InvoiceRecord(entity=entity, email_id=email_id, document_type=doc_type, attachment_name=att.filename, blob_uri=uri, invoice_number=ext.invoice_number, invoice_number_norm=normalize_invoice_number(ext.invoice_number), invoice_date=ext.invoice_date, total=ext.total, currency=ext.currency, po_number=ext.po_number, extracted=data)
                s.add(inv)
            invoices.append({"invoice_id": inv.id, "attachment": att.filename, **data})

        ctx = self._ctx(entity, email, mb)
        ctx.invoice_ids = [i["invoice_id"] for i in invoices if "invoice_id" in i]
        bank_change = policy.mentions_bank_change(f"{email.subject}\n{email.body_text}")
        if bank_change:
            ctx.flags.append({"category": "bank_detail_change", "reason": "Keyword control", "invoice_id": None})
            self.db.open_review(kind="bank_detail_change", subject_type="email", subject_id=email_id, reason="Email mentions bank / remittance details", entity=entity, sender=email.sender)
        internal = email.sender_authenticated and policy.is_internal(email.sender_domain)

        result = self.email_agent.run(ctx, render_email_task(email, invoices, skipped, entity_name=self.runtime(entity).entity.name, bank_change=bank_change, internal_sender=internal))
        cost = record_usage(self.settings, "email", result.usage, result.turns)
        with self.db.session() as s, s.begin():
            rec = s.get(EmailRecord, email_id)
            rec.status, rec.summary, rec.usage, rec.cost_usd = result.status, result.summary, result.usage, cost
        if result.status != "done" and not ctx.flags:
            self.db.open_review(kind="agent_needs_review", subject_type="email", subject_id=email_id, reason=result.summary[:2000], entity=entity)
        EMAILS.labels(entity, result.status).inc()
        mb.label(email, PROCESSED_LABEL)
        if result.status != "done":
            mb.label(email, REVIEW_LABEL)
        return {"message_id": email.id, "entity": entity, "cost_usd": cost, **result.__dict__}

    # ------------------------------------------------------------------ posting
    def _job_post_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        invoice_id, actor = payload["invoice_id"], payload.get("actor", "system")
        with self.db.session() as s, s.begin():
            rec = s.get(InvoiceRecord, invoice_id)
            if rec.status == "posted":
                return {"invoice_id": invoice_id, "status": "posted", "erp_bill_id": rec.erp_bill_id}
            if rec.status not in ("approved", "posting"):
                raise ValueError(f"invoice {invoice_id} is {rec.status}, not approved")
            rec.status = "posting"  # a crash after the ERP write retries with the same idempotency key
            entity, pending, agent_coding, doc_type, vendor_id = rec.entity, rec.pending_bill, rec.agent_coding, rec.document_type, rec.vendor_id
        erp = self.runtime(entity).erp
        draft = BillDraft.model_validate({k: v for k, v in pending.items() if k != "coding_rationale"})
        draft.subsidiary_id = draft.subsidiary_id or self.runtime(entity).entity.settings.netsuite_subsidiary_id or None
        key = f"ap-{invoice_id}"
        try:
            bill = erp.create_credit(draft, key) if doc_type == "credit_note" else erp.create_bill(draft, key)
        except Exception:
            POSTINGS.labels(entity, "error").inc()
            raise
        agent_lines = (agent_coding or {}).get("lines", [])
        with self.db.session() as s, s.begin():
            r = s.get(InvoiceRecord, invoice_id)
            r.erp_bill_id, r.status = bill.id, "posted"
            for i, ln in enumerate(draft.lines):
                agent_gl = agent_lines[i].get("gl_account") if i < len(agent_lines) else None
                corrected = pending.get("corrected_by") if agent_gl and agent_gl != ln.gl_account else None
                s.add(CodingHistory(entity=entity, vendor_id=vendor_id, invoice_id=invoice_id, description=ln.description[:500], gl_account=ln.gl_account, agent_gl_account=agent_gl, amount=ln.amount, corrected_by=corrected))
        POSTINGS.labels(entity, "posted").inc()
        self.db.audit(actor, "bill_posted", invoice_id, erp_bill_id=bill.id, erp=erp.name, entity=entity, document_type=doc_type)
        return {"invoice_id": invoice_id, "status": "posted", "erp_bill_id": bill.id}

    def submit_manually(self, invoice_id: str, principal: Principal, payload: SubmitInvoice) -> dict[str, Any]:
        """A clerk corrects/codes an invoice from the review queue. Same policy path;
        the clerk can't then approve it (SoD)."""
        with self.db.session() as s:
            rec = s.get(InvoiceRecord, invoice_id)
        if rec is None or not principal.can("invoices:submit", rec.entity):
            raise PermissionError("not allowed")
        ctx = self._ctx(rec.entity, actor=principal.actor)
        ctx.invoice_ids = [invoice_id]
        try:
            return submit_invoice(ctx, payload.model_copy(update={"invoice_id": invoice_id}))
        except ToolError as e:
            raise ValueError(str(e)) from e

    # ------------------------------------------------------------------ approvals
    def _approver_matches(self, principal: Principal, approvers: list[str]) -> bool:
        email = (principal.email or "").lower()
        names = {a.lower() for a in approvers}
        if email and email in names:
            return True
        if any(a.startswith("group:") and a[6:] in {g.lower() for g in principal.groups} for a in names):
            return True
        now = _now()
        with self.db.session() as s:
            delegators = {d.delegator.lower() for d in s.scalars(select(Delegation).where(Delegation.delegate == email))
                          if _aware(d.starts_at) <= now <= _aware(d.ends_at)}
        return bool(delegators & names)

    def decide_approval(self, approval_id: str, principal: Principal, approve: bool, *, comment: str | None = None, token: str | None = None, corrections: list[dict[str, str]] | None = None) -> dict[str, Any]:
        if principal.kind != "user" or not principal.email:
            raise PermissionError("approvals must be made by a signed-in person")
        with self.db.session() as s:
            appr = s.get(ApprovalRecord, approval_id)
        if appr is None:
            raise KeyError(approval_id)
        if token is not None and token != appr.token:
            raise PermissionError("invalid approval link")
        if appr.status != "pending":
            return {"status": appr.status, "message": "already decided"}
        if not principal.can("approvals:decide", appr.entity):
            raise PermissionError("you don't have approval rights for this entity")
        if not self._approver_matches(principal, appr.approvers):
            raise PermissionError("you are not an approver for this tier (and hold no active delegation)")
        if appr.requires_mfa and not principal.mfa:
            raise PermissionError("multi-factor authentication is required for this approval")
        policy = self.policy(appr.entity)
        self._check_sod(appr, principal, policy)

        with self.db.session() as s, s.begin():
            a = s.get(ApprovalRecord, approval_id)
            a.status = "approved" if approve else "rejected"
            a.decided_by, a.decided_at, a.comment = principal.email, _now(), comment
        APPROVAL_LATENCY.labels(appr.kind).observe((_now() - _aware(appr.created_at)).total_seconds() / 3600)
        self.db.audit(principal.actor, f"approval_{'approved' if approve else 'rejected'}", approval_id, kind=appr.kind, subject=appr.subject_id, comment=comment, mfa=principal.mfa, entity=appr.entity)

        if appr.kind == "invoice":
            return self._finish_invoice(appr, approve, principal, corrections or [])
        return self._finish_payment_run(appr, approve, principal)

    def _check_sod(self, appr: ApprovalRecord, principal: Principal, policy: Policy) -> None:
        email = principal.email
        with self.db.session() as s:
            if appr.kind == "invoice":
                inv = s.get(InvoiceRecord, appr.subject_id)
                if policy.sod.submitter_cannot_approve and inv and inv.submitted_by and inv.submitted_by.lower() == f"user:{email}":
                    raise PermissionError("segregation of duties: you submitted this invoice")
            elif appr.kind == "payment_run" and policy.sod.distinct_invoice_and_payment_approvers:
                run = s.get(PaymentRunRecord, appr.subject_id)
                bill_ids = [i["bill_id"] for i in run.items]
                inv_ids = s.scalars(select(InvoiceRecord.id).where(InvoiceRecord.erp_bill_id.in_(bill_ids))).all()
                if inv_ids and s.scalar(select(ApprovalRecord.id).where(ApprovalRecord.kind == "invoice", ApprovalRecord.subject_id.in_(inv_ids), ApprovalRecord.decided_by == email, ApprovalRecord.status == "approved").limit(1)):
                    raise PermissionError("segregation of duties: you approved an invoice in this payment run")

    def _finish_invoice(self, appr: ApprovalRecord, approve: bool, principal: Principal, corrections: list[dict[str, str]]) -> dict[str, Any]:
        invoice_id = appr.subject_id
        with self.db.session() as s, s.begin():
            rec = s.get(InvoiceRecord, invoice_id)
            if not approve:
                rec.status = "rejected"
                return {"invoice_id": invoice_id, "status": "rejected"}
            if corrections:
                valid = {g.id for g in self.runtime(rec.entity).erp.list_gl_accounts()}
                pending = dict(rec.pending_bill)
                lines = [dict(ln) for ln in pending["lines"]]
                for c in corrections:
                    idx = int(c["line"])
                    if not 0 <= idx < len(lines) or c["gl_account"] not in valid:
                        raise ValueError(f"invalid correction {c}")
                    lines[idx]["gl_account"] = c["gl_account"]
                pending["lines"], pending["corrected_by"] = lines, principal.actor
                rec.pending_bill = pending
            rec.status = "approved"
            entity = rec.entity
        if corrections:
            self.db.audit(principal.actor, "coding_corrected", invoice_id, corrections=corrections)
        self.db.enqueue("post_invoice", {"invoice_id": invoice_id, "actor": principal.actor}, entity=entity, dedupe_key=f"post:{invoice_id}")
        return {"invoice_id": invoice_id, "status": "approved", "next": "queued for ERP posting"}

    def _finish_payment_run(self, appr: ApprovalRecord, approve: bool, principal: Principal) -> dict[str, Any]:
        run_id = appr.subject_id
        with self.db.session() as s, s.begin():
            run = s.get(PaymentRunRecord, run_id)
            run.status = "approved" if approve else "rejected"
            run.approved_by = principal.email if approve else None
            entity = run.entity
        if approve:
            self.db.enqueue("execute_payment_run", {"run_id": run_id}, entity=entity, dedupe_key=f"pay:{run_id}")
        return {"payment_run_id": run_id, "status": "approved" if approve else "rejected"}

    # ------------------------------------------------------------------ payments
    def propose_payment_run(self, payment_date: date | None = None, next_run_date: date | None = None, entity: str | None = None) -> AgentResult:
        entity = entity or next(iter(self.runtimes))
        payment_date = payment_date or date.today()
        next_run_date = next_run_date or payment_date + timedelta(days=7)
        task = f"Prepare the payment run for {self.runtime(entity).entity.name} with payment date {payment_date.isoformat()}. The next payment run after this one is {next_run_date.isoformat()}."
        result = self.payment_agent.run(self._ctx(entity), task)
        record_usage(self.settings, "payment_run", result.usage, result.turns)
        return result

    def _job_propose_payment_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        pd = date.fromisoformat(payload["payment_date"]) if payload.get("payment_date") else None
        nd = date.fromisoformat(payload["next_run_date"]) if payload.get("next_run_date") else None
        return self.propose_payment_run(pd, nd, payload.get("entity")).__dict__

    def _job_execute_payment_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Runs in the payment-executor process, whose ERP credentials are the only
        ones allowed to record payments."""
        run_id = payload["run_id"]
        with self.db.session() as s, s.begin():
            run = s.get(PaymentRunRecord, run_id)
            if run.status not in ("approved", "executing"):
                raise ValueError(f"payment run {run_id} is {run.status}, not approved")
            run.status = "executing"
            entity, items, prior = run.entity, run.items, {r["bill_id"]: r for r in (run.results or [])}
        erp = self.runtime(entity).erp
        pay_date = date.fromisoformat(run.payment_date)
        open_bills = {b.id: b for b in erp.list_open_bills()}
        results = []
        for item in items:
            done = prior.get(item["bill_id"])
            if done and done.get("result") == "recorded":
                results.append(done)
                continue
            bill = open_bills.get(item["bill_id"])
            if bill is None:
                results.append({**item, "result": "skipped: no longer open"})
            else:
                try:
                    pid = erp.record_payment(bill, Decimal(item["amount"]), pay_date, f"pay-{run_id}-{bill.id}")
                    results.append({**item, "result": "recorded", "erp_payment_id": pid})
                    PAYMENTS.labels(entity, "recorded").inc()
                except Exception as e:
                    results.append({**item, "result": f"error: {e}"})
                    PAYMENTS.labels(entity, "error").inc()
            with self.db.session() as s, s.begin():  # persist progress after every payment
                s.get(PaymentRunRecord, run_id).results = results + [prior[i["bill_id"]] for i in items[len(results):] if i["bill_id"] in prior]
        errors = [r for r in results if r["result"].startswith("error")]
        with self.db.session() as s, s.begin():
            r = s.get(PaymentRunRecord, run_id)
            r.status, r.results = ("executed" if not errors else "executing"), results
        self.db.audit("executor", "payment_run_executed", run_id, results=results, entity=entity)
        if errors:
            raise RuntimeError(f"{len(errors)} payments failed; will retry")
        return {"payment_run_id": run_id, "status": "executed", "results": results}

    # ------------------------------------------------------------------ internal Q&A
    def ask(self, principal: Principal, question: str, entity: str) -> AgentResult:
        if not principal.can("ask", entity):
            raise PermissionError("no access to this entity")
        ctx = self._ctx(entity, actor=principal.actor)
        ctx.mailbox = None  # read-only: no email tools
        result = self.ask_agent.run(ctx, f"Question from {principal.email} about entity {self.runtime(entity).entity.name}:\n{question[:4000]}")
        record_usage(self.settings, "ask", result.usage, result.turns)
        self.db.audit(principal.actor, "ask", entity, question=question[:4000], answer=result.summary[:4000])
        return result

    # ------------------------------------------------------------------ admin data
    def add_vendor_contact(self, principal: Principal, entity: str, vendor_id: str, email: str) -> str:
        if not principal.can("vendors:contacts:write", entity):
            raise PermissionError("not allowed")
        vendor = self.runtime(entity).erp.get_vendor(vendor_id)
        if vendor is None:
            raise KeyError(vendor_id)
        with self.db.session() as s, s.begin():
            rec = VendorContact(entity=entity, vendor_id=vendor_id, email=email.lower(), added_by=principal.actor)
            s.add(rec)
        self.db.audit(principal.actor, "vendor_contact_added", vendor_id, entity=entity, email=email, domain_on_file=email.split("@")[-1].lower() in vendor.email_domains)
        return rec.id

    def verify_bank_account(self, principal: Principal, entity: str, vendor_id: str, account: str, evidence: dict[str, Any]) -> str:
        if not principal.can("vendors:bank:verify", entity):
            raise PermissionError("not allowed")
        if not principal.mfa:
            raise PermissionError("multi-factor authentication is required")
        if self.runtime(entity).erp.get_vendor(vendor_id) is None:
            raise KeyError(vendor_id)
        return register_bank_account(self.db, entity=entity, vendor_id=vendor_id, account=account, pepper=self.settings.bank_fingerprint_pepper or "dev-pepper", verified_by=principal.actor, evidence=evidence)

    def add_fx_rate(self, principal: Principal, base: str, quote: str, rate: Decimal, as_of: date) -> None:
        if not principal.can("fx:write"):
            raise PermissionError("not allowed")
        with self.db.session() as s, s.begin():
            s.add(FxRate(base=base.upper(), quote=quote.upper(), rate=rate, as_of=as_of.isoformat(), source=principal.actor))
        self.db.audit(principal.actor, "fx_rate_added", f"{base}/{quote}", rate=str(rate), as_of=as_of.isoformat())

    def delegate(self, principal: Principal, delegate: str, starts_at: datetime, ends_at: datetime) -> str:
        if not principal.can("delegations:write") or not principal.email:
            raise PermissionError("not allowed")
        days = self.policies.active().max_delegation_days
        if ends_at <= starts_at or ends_at - starts_at > timedelta(days=days):
            raise ValueError(f"delegation must be positive and at most {days} days")
        if delegate.lower() == principal.email:
            raise ValueError("can't delegate to yourself")
        with self.db.session() as s, s.begin():
            rec = Delegation(delegator=principal.email, delegate=delegate.lower(), starts_at=starts_at, ends_at=ends_at, created_by=principal.actor)
            s.add(rec)
        self.db.audit(principal.actor, "delegation_created", rec.id, delegate=delegate, starts=starts_at, ends=ends_at)
        return rec.id

    def resolve_review(self, principal: Principal, item_id: str, resolution: str) -> None:
        with self.db.session() as s, s.begin():
            item = s.get(ReviewItem, item_id)
            if item is None or not principal.can("review:resolve", item.entity):
                raise PermissionError("not allowed")
            if item.status != "open":
                return
            item.status, item.resolved_by, item.resolution, item.resolved_at = "resolved", principal.actor, resolution[:4000], _now()
        self.db.audit(principal.actor, "review_resolved", item_id, resolution=resolution[:4000])

    # ------------------------------------------------------------------ reports & retention
    def shadow_report(self) -> dict[str, Any]:
        with self.db.session() as s:
            rows = s.scalars(select(ApprovalRecord).where(ApprovalRecord.shadow_auto_post.is_(True), ApprovalRecord.status != "pending")).all()
            corrected = set(s.scalars(select(CodingHistory.invoice_id).where(CodingHistory.corrected_by.is_not(None))).all())
        agree = [r for r in rows if r.status == "approved" and r.subject_id not in corrected]
        return {"decided": len(rows), "agreed_without_change": len(agree), "agreement_rate": round(len(agree) / len(rows), 3) if rows else None,
                "rejected": sum(r.status == "rejected" for r in rows), "approved_with_corrections": sum(r.status == "approved" and r.subject_id in corrected for r in rows)}

    def purge_expired(self, actor: str = "system") -> int:
        """Delete stored documents and extracted data past the retention period.
        Financial records in the ERP and the audit chain are kept."""
        cutoff = _now() - timedelta(days=365 * self.settings.retention_years)
        n = 0
        with self.db.session() as s:
            rows = s.scalars(select(InvoiceRecord).where(InvoiceRecord.created_at < cutoff, InvoiceRecord.status.in_(FINAL_INVOICE_STATES), InvoiceRecord.blob_uri.is_not(None))).all()
        for r in rows:
            try:
                self.blobs.delete(r.blob_uri)
            except Exception:
                log.exception("blob delete failed for %s", r.id)
                continue
            with self.db.session() as s, s.begin():
                rec = s.get(InvoiceRecord, r.id)
                rec.blob_uri, rec.extracted, rec.pending_bill, rec.agent_coding = None, {}, None, None
            n += 1
        self.db.audit(actor, "retention_purge", None, purged=n, cutoff=cutoff.isoformat())
        return n


__all__ = ["APService"]
