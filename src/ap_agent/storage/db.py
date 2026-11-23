"""State store (SQLAlchemy).

SQLite for local development; PostgreSQL in the cloud (Cloud SQL, RDS/Aurora,
Azure Database for PostgreSQL). Outside dev the schema is managed by Alembic
migrations (``ap-agent migrate``); in dev tables are created automatically.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, Text, create_engine, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite drops tzinfo
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Base(DeclarativeBase):
    pass


class EmailRecord(Base):
    __tablename__ = "emails"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    provider: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(512))
    sender: Mapped[str] = mapped_column(String(512))
    sender_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="processing")  # processing|done|needs_review|error
    summary: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InvoiceRecord(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    email_id: Mapped[str | None] = mapped_column(String(64), index=True)
    document_type: Mapped[str] = mapped_column(String(32), default="invoice")  # invoice|credit_note
    attachment_name: Mapped[str | None] = mapped_column(String(512))
    blob_uri: Mapped[str | None] = mapped_column(Text)
    vendor_id: Mapped[str | None] = mapped_column(String(128), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(256), index=True)
    invoice_number_norm: Mapped[str | None] = mapped_column(String(256), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(32))
    total: Mapped[float | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    functional_total: Mapped[float | None] = mapped_column(Numeric(18, 2))
    po_number: Mapped[str | None] = mapped_column(String(128))
    # received|awaiting_approval|posting|posted|rejected|blocked|needs_review|duplicate
    status: Mapped[str] = mapped_column(String(32), default="received")
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_bill: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    agent_coding: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # what the agent proposed (for feedback/shadow)
    agent_decision: Mapped[str | None] = mapped_column(String(32))  # policy decision before shadow override
    submitted_by: Mapped[str | None] = mapped_column(String(256))
    erp_bill_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    kind: Mapped[str] = mapped_column(String(32))  # invoice|payment_run
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    approvers: Mapped[list[str]] = mapped_column(JSON, default=list)  # emails or group:<name>
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|approved|rejected
    token: Mapped[str] = mapped_column(String(128), default=lambda: secrets.token_urlsafe(32))
    requires_mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow_auto_post: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_by: Mapped[str | None] = mapped_column(String(256))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PaymentRunRecord(Base):
    __tablename__ = "payment_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|approved|executing|executed|rejected
    payment_date: Mapped[str] = mapped_column(String(32))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    total: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    rationale: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(256))
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditRecord(Base):
    """Append-only, hash-chained: each row commits to the previous row's hash."""

    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    seq: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(256))  # agent|system|user:<email>|key:<name>
    action: Mapped[str] = mapped_column(String(128))
    entity: Mapped[str | None] = mapped_column(String(128), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class SecretRecord(Base):
    """Encrypted key/value store (rotating OAuth refresh tokens)."""

    __tablename__ = "secrets"
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # Fernet ciphertext outside dev
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    kind: Mapped[str] = mapped_column(String(64))  # bank_detail_change|suspected_fraud|dispute|vendor_not_found|error|...
    subject_type: Mapped[str] = mapped_column(String(32))  # email|invoice|payment_run
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)  # open|resolved
    assigned_to: Mapped[str | None] = mapped_column(String(256))
    resolved_by: Mapped[str | None] = mapped_column(String(256))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VendorContact(Base):
    """Individual vendor contacts allowed to receive payment information by email."""

    __tablename__ = "vendor_contacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    added_by: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorBankAccount(Base):
    """Bank accounts verified by call-back. Only an HMAC fingerprint and last 4 are kept."""

    __tablename__ = "vendor_bank_accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    last4: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default="verified")  # verified|revoked
    verified_by: Mapped[str] = mapped_column(String(256))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    entities: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["*"])
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    version: Mapped[int] = mapped_column(Integer, index=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|active|superseded|rejected
    proposed_by: Mapped[str] = mapped_column(String(256))
    approved_by: Mapped[str | None] = mapped_column(String(256))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(64), index=True)  # process_email|propose_payment_run|execute_payment_run
    entity: Mapped[str] = mapped_column(String(32), default="default")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(512), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)  # queued|running|done|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodingHistory(Base):
    """Final GL coding of posted bills; corrections by humans are marked."""

    __tablename__ = "coding_history"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    entity: Mapped[str] = mapped_column(String(32), default="default", index=True)
    vendor_id: Mapped[str] = mapped_column(String(128), index=True)
    invoice_id: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    gl_account: Mapped[str | None] = mapped_column(String(64))
    agent_gl_account: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(18, 2))
    corrected_by: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    base: Mapped[str] = mapped_column(String(3), index=True)
    quote: Mapped[str] = mapped_column(String(3), index=True)
    rate: Mapped[float] = mapped_column(Numeric(18, 8))  # 1 base = rate quote
    as_of: Mapped[str] = mapped_column(String(10), index=True)
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Delegation(Base):
    """Approval authority delegated for a period (out of office)."""

    __tablename__ = "delegations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    delegator: Mapped[str] = mapped_column(String(320), index=True)
    delegate: Mapped[str] = mapped_column(String(320), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


GENESIS = "0" * 64


def _audit_hash(prev: str, seq: int, ts: datetime, actor: str, action: str, entity: str | None, detail: dict) -> str:
    canonical = json.dumps({"prev": prev, "seq": seq, "ts": _aware(ts).isoformat(), "actor": actor, "action": action, "entity": entity, "detail": detail}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Database:
    def __init__(self, url: str, *, create_tables: bool = True):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.engine = create_engine(url, future=True, pool_pre_ping=True)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.is_postgres = self.engine.dialect.name == "postgresql"
        if create_tables:
            Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.Session()

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, subject: str | None = None, /, **detail: Any) -> None:
        """Append to the hash chain. ``subject`` is the record id the event is about."""
        entity = subject
        detail = json.loads(json.dumps(detail, default=str))
        with self.session() as s, s.begin():
            if self.is_postgres:
                s.execute(text("SELECT pg_advisory_xact_lock(727274)"))  # serialize chain appends
            last = s.scalar(select(AuditRecord).order_by(AuditRecord.seq.desc()).limit(1))
            seq = (last.seq + 1) if last else 1
            prev = last.hash if last else GENESIS
            ts = _now()
            s.add(AuditRecord(seq=seq, ts=ts, actor=actor, action=action, entity=entity, detail=detail, prev_hash=prev, hash=_audit_hash(prev, seq, ts, actor, action, entity, detail)))

    def verify_audit(self) -> tuple[bool, int, str | None]:
        """Recompute the chain. Returns (ok, rows_checked, first_problem)."""
        prev, n = GENESIS, 0
        with self.session() as s:
            for rec in s.scalars(select(AuditRecord).order_by(AuditRecord.seq)).yield_per(500):
                n += 1
                if rec.seq != n:
                    return False, n, f"gap before seq {rec.seq}"
                if rec.prev_hash != prev or rec.hash != _audit_hash(prev, rec.seq, rec.ts, rec.actor, rec.action, rec.entity, rec.detail):
                    return False, n, f"hash mismatch at seq {rec.seq}"
                prev = rec.hash
        return True, n, None

    # ------------------------------------------------------------------ job queue
    def enqueue(self, kind: str, payload: dict[str, Any], *, entity: str = "default", dedupe_key: str | None = None, max_attempts: int = 5) -> str | None:
        try:
            with self.session() as s, s.begin():
                job = Job(kind=kind, payload=payload, entity=entity, dedupe_key=dedupe_key, max_attempts=max_attempts)
                s.add(job)
            return job.id
        except IntegrityError:
            return None  # already queued / processed

    def claim(self, kinds: Iterable[str], worker: str, visibility_timeout_s: int = 1800) -> Job | None:
        kinds = list(kinds)
        now = _now()
        with self.session() as s, s.begin():
            # Recover jobs whose worker died mid-run.
            s.execute(update(Job).where(Job.status == "running", Job.locked_at < now - timedelta(seconds=visibility_timeout_s)).values(status="queued", locked_by=None))
        for _ in range(5):
            with self.session() as s:
                cand = s.scalars(select(Job.id).where(Job.status == "queued", Job.kind.in_(kinds), Job.run_after <= now).order_by(Job.created_at).limit(5)).all()
            if not cand:
                return None
            for job_id in cand:
                with self.session() as s, s.begin():
                    res = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(status="running", locked_by=worker, locked_at=now, attempts=Job.attempts + 1))
                    if res.rowcount == 1:
                        return s.get(Job, job_id)
        return None

    def complete(self, job_id: str) -> None:
        with self.session() as s, s.begin():
            s.execute(update(Job).where(Job.id == job_id).values(status="done", finished_at=_now(), locked_by=None))

    def fail(self, job_id: str, error: str) -> str:
        with self.session() as s, s.begin():
            job = s.get(Job, job_id)
            if job.attempts >= job.max_attempts:
                job.status, job.finished_at = "dead", _now()
            else:
                job.status = "queued"
                job.run_after = _now() + timedelta(seconds=min(3600, 30 * 2 ** job.attempts))
            job.last_error, job.locked_by = error[:4000], None
            return job.status

    def queue_stats(self) -> dict[str, int]:
        from sqlalchemy import func

        with self.session() as s:
            return {status: n for status, n in s.execute(select(Job.status, func.count()).group_by(Job.status))}

    # ------------------------------------------------------------------ misc
    def email_seen(self, message_id: str) -> bool:
        with self.session() as s:
            return s.scalar(select(EmailRecord.id).where(EmailRecord.message_id == message_id)) is not None

    def open_review(self, *, kind: str, subject_type: str, subject_id: str, reason: str, entity: str = "default", **detail: Any) -> str:
        with self.session() as s, s.begin():
            item = ReviewItem(kind=kind, subject_type=subject_type, subject_id=subject_id, reason=reason, entity=entity, detail=json.loads(json.dumps(detail, default=str)))
            s.add(item)
        self.audit("system", "review_opened", item.id, kind=kind, subject=subject_id, reason=reason)
        return item.id
