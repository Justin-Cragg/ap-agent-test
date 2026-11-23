"""Deterministic checks used by the tools: bank-account verification, FX
conversion, tax-rate sanity, and who may receive vendor data by email."""

from __future__ import annotations

import hashlib
import hmac
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ap_agent.ap.policy import Policy
from ap_agent.erp.base import ERPConnector, normalize_account
from ap_agent.mail.base import EmailMessage
from ap_agent.models import Vendor
from ap_agent.storage.db import Database, FxRate, VendorBankAccount, VendorContact

# --------------------------------------------------------------------- bank accounts


def bank_fingerprint(pepper: str, account: str) -> str:
    return hmac.new(pepper.encode(), normalize_account(account).encode(), hashlib.sha256).hexdigest()


def redact_bank_details(extracted: dict[str, Any], pepper: str) -> dict[str, Any]:
    """Replace printed bank numbers with an HMAC fingerprint + last 4 before the
    data is stored or shown to the model."""
    out = dict(extracted)
    account = out.pop("remit_bank_account", None)
    out.pop("remit_bank_routing", None)
    if account and normalize_account(account):
        out["bank_fingerprint"] = bank_fingerprint(pepper, account)
        out["bank_last4"] = normalize_account(account)[-4:]
        out["remit_to_bank_details_present"] = True
    return out


def bank_account_status(db: Database, erp: ERPConnector, *, entity: str, vendor_id: str, fingerprint: str | None, pepper: str) -> str:
    """none (no bank details printed) | verified | unverified."""
    if not fingerprint:
        return "none"
    with db.session() as s:
        known = s.scalars(select(VendorBankAccount.fingerprint).where(VendorBankAccount.entity == entity, VendorBankAccount.vendor_id == vendor_id, VendorBankAccount.status == "verified")).all()
    if fingerprint in known:
        return "verified"
    try:
        erp_accounts = erp.vendor_bank_accounts(vendor_id) or []
    except Exception:
        erp_accounts = []
    if any(bank_fingerprint(pepper, a) == fingerprint for a in erp_accounts):
        return "verified"
    return "unverified"


def register_bank_account(db: Database, *, entity: str, vendor_id: str, account: str, pepper: str, verified_by: str, evidence: dict[str, Any]) -> str:
    required = {"callback_phone_source", "called_number", "contact_name"}
    missing = required - {k for k, v in evidence.items() if v}
    if missing:
        raise ValueError(f"Call-back evidence missing: {sorted(missing)}")
    if evidence["callback_phone_source"] == "invoice_or_email":
        raise ValueError("Call-back must use a number from an independent source, not the request itself")
    fp = bank_fingerprint(pepper, account)
    with db.session() as s, s.begin():
        rec = VendorBankAccount(entity=entity, vendor_id=vendor_id, fingerprint=fp, last4=normalize_account(account)[-4:], verified_by=verified_by, evidence=evidence)
        s.add(rec)
    db.audit(verified_by, "bank_account_verified", vendor_id, entity=entity, last4=rec.last4, evidence=evidence)
    return rec.id


# --------------------------------------------------------------------- FX


def to_functional(db: Database, erp: ERPConnector, amount: Decimal, currency: str | None, functional: str, on: date) -> Decimal | None:
    if not currency or currency.upper() == functional.upper():
        return amount
    rate = None
    try:
        rate = erp.exchange_rate(currency.upper(), functional.upper(), on)
    except Exception:
        rate = None
    if rate is None:
        with db.session() as s:
            rec = s.scalar(select(FxRate).where(FxRate.base == currency.upper(), FxRate.quote == functional.upper(), FxRate.as_of <= on.isoformat()).order_by(FxRate.as_of.desc()).limit(1))
        rate = Decimal(str(rec.rate)) if rec else None
    return (amount * rate).quantize(Decimal("0.01")) if rate else None


# --------------------------------------------------------------------- tax


def tax_rate_ok(policy: Policy, subtotal: Decimal, tax: Decimal) -> bool:
    if not policy.allowed_tax_rates_pct or subtotal <= 0:
        return True
    rate = tax / subtotal * 100
    return any(abs(rate - r) <= policy.tax_rate_tolerance_pct for r in policy.allowed_tax_rates_pct)


# --------------------------------------------------------------------- disclosure


def vendor_contacts(db: Database, entity: str, vendor_id: str) -> set[str]:
    with db.session() as s:
        return {e.lower() for e in s.scalars(select(VendorContact.email).where(VendorContact.entity == entity, VendorContact.vendor_id == vendor_id, VendorContact.active.is_(True)))}


def sender_authorized_for(db: Database, policy: Policy, entity: str, email: EmailMessage, vendor: Vendor) -> bool:
    """May this email's sender receive this vendor's payment information?"""
    if policy.disclosure.require_dmarc and not email.sender_authenticated:
        return False
    sender = email.sender.lower()
    if vendor.email and vendor.email.lower() == sender:
        return True
    if sender in vendor_contacts(db, entity, vendor.id):
        return True
    return policy.disclosure.match == "domain" and email.sender_domain in vendor.email_domains
