"""State store (SQLAlchemy).

SQLite for local development; PostgreSQL in the cloud (Cloud SQL, RDS/Aurora,
Azure Database for PostgreSQL) - just change DATABASE_URL.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class EmailRecord(Base):
    __tablename__ = "emails"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    provider: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(512))
    sender: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="processing")  # processing|done|needs_review|error
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InvoiceRecord(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    email_id: Mapped[str | None] = mapped_column(String(64), index=True)
    attachment_name: Mapped[str | None] = mapped_column(String(512))
    blob_uri: Mapped[str | None] = mapped_column(Text)
    vendor_id: Mapped[str | None] = mapped_column(String(128), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(256), index=True)
    invoice_number_norm: Mapped[str | None] = mapped_column(String(256), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(32))
    total: Mapped[float | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    po_number: Mapped[str | None] = mapped_column(String(128))
    # received|awaiting_approval|approved|posted|rejected|blocked|needs_review|duplicate
    status: Mapped[str] = mapped_column(String(32), default="received")
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_bill: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    erp_bill_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(32))  # invoice|payment_run
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    approvers: Mapped[list[str]] = mapped_column(JSON, default=list)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|approved|rejected
    token: Mapped[str] = mapped_column(String(128), default=lambda: secrets.token_urlsafe(32))
    decided_by: Mapped[str | None] = mapped_column(String(256))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PaymentRunRecord(Base):
    __tablename__ = "payment_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|approved|executed|rejected
    payment_date: Mapped[str] = mapped_column(String(32))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    total: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    rationale: Mapped[str | None] = mapped_column(Text)
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditRecord(Base):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(128))  # agent|system|user:<email>
    action: Mapped[str] = mapped_column(String(128))
    entity: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class KVRecord(Base):
    """Small key/value store, used for rotating OAuth refresh tokens."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, future=True, pool_pre_ping=True)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.Session()

    def audit(self, actor: str, action: str, entity: str | None = None, **detail: Any) -> None:
        with self.session() as s, s.begin():
            s.add(AuditRecord(actor=actor, action=action, entity=entity, detail=json.loads(json.dumps(detail, default=str))))

    # token store -------------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        with self.session() as s:
            rec = s.get(KVRecord, key)
            return rec.value if rec else None

    def kv_set(self, key: str, value: str) -> None:
        with self.session() as s, s.begin():
            rec = s.get(KVRecord, key)
            if rec:
                rec.value = value
            else:
                s.add(KVRecord(key=key, value=value))

    def email_seen(self, message_id: str) -> bool:
        with self.session() as s:
            return s.scalar(select(EmailRecord.id).where(EmailRecord.message_id == message_id)) is not None
