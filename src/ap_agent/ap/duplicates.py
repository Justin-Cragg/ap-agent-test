"""Duplicate-invoice detection across the ERP and the agent's own registry."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ap_agent.erp.base import ERPConnector
from ap_agent.models import BillStatus
from ap_agent.storage.db import Database, InvoiceRecord


def normalize_invoice_number(value: str | None) -> str:
    """'INV-000123 ' -> 'INV123' (case-insensitive, punctuation and leading zeros dropped)."""
    if not value:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return re.sub(r"(?<=[A-Z])0+(?=\d)|^0+(?=\d)", "", s)


class DuplicateCheck(BaseModel):
    verdict: str  # none | possible | exact
    matches: list[str] = Field(default_factory=list)


def check_duplicate(erp: ERPConnector, db: Database, *, vendor_id: str, invoice_number: str, total: Decimal, invoice_date: date | None, window_days: int, exclude_invoice_id: str | None = None) -> DuplicateCheck:
    norm = normalize_invoice_number(invoice_number)
    matches: list[str] = []
    verdict = "none"

    for bill in erp.find_bills(vendor_id=vendor_id):
        if bill.status == BillStatus.void:
            continue
        if normalize_invoice_number(bill.invoice_number) == norm:
            return DuplicateCheck(verdict="exact", matches=[f"ERP bill {bill.id} ({bill.invoice_number}, {bill.total})"])
        same_amount = abs(bill.total - total) < Decimal("0.01")
        near_date = invoice_date and bill.invoice_date and abs((bill.invoice_date - invoice_date).days) <= window_days
        if same_amount and near_date:
            verdict = "possible"
            matches.append(f"ERP bill {bill.id} ({bill.invoice_number}) same amount within {window_days} days")

    with db.session() as s:
        rows = s.scalars(select(InvoiceRecord).where(InvoiceRecord.vendor_id == vendor_id, InvoiceRecord.status.notin_(["rejected", "duplicate"]))).all()
    for rec in rows:
        if rec.id == exclude_invoice_id:
            continue
        if rec.invoice_number_norm == norm:
            return DuplicateCheck(verdict="exact", matches=[f"Agent invoice {rec.id} ({rec.invoice_number}) status={rec.status}"])
        if rec.total is not None and abs(Decimal(str(rec.total)) - total) < Decimal("0.01"):
            verdict = "possible"
            matches.append(f"Agent invoice {rec.id} ({rec.invoice_number}) same amount")
    return DuplicateCheck(verdict=verdict, matches=matches)
