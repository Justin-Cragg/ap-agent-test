"""AP controls, enforced in code.

The model proposes; this module decides. Nothing the model says (or an email
says) can post a bill above the auto-post limit, skip an approval tier, post a
duplicate, or release a payment - those paths do not exist in the tool layer.

Policy is versioned in the database. A change is proposed by one person and
activated by a different person with ``policy:approve`` (two-person rule);
``config/policy.yaml`` only seeds version 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ap_agent.ap.matching import MatchResult, MatchStatus
from ap_agent.storage.db import Database, PolicyVersion


class Tolerances(BaseModel):
    price_pct: Decimal = Decimal("2.0")
    quantity_pct: Decimal = Decimal("0")
    total_abs: Decimal = Decimal("5.00")
    total_pct: Decimal = Decimal("1.0")


class ApprovalTier(BaseModel):
    max_amount: Decimal | None = None  # None = unlimited; in the entity's functional currency
    approvers: list[str]  # emails or "group:<idp group>"


class Disclosure(BaseModel):
    """Who may receive vendor payment information by email."""

    require_dmarc: bool = True
    # contacts: only the vendor's ERP email + contacts registered by AP (vendor_contacts)
    # domain:   anyone at a domain on file for the vendor
    match: Literal["contacts", "domain"] = "contacts"


class SoD(BaseModel):
    submitter_cannot_approve: bool = True
    distinct_invoice_and_payment_approvers: bool = True


class Policy(BaseModel):
    auto_post_enabled: bool = True
    # shadow: compute decisions and record what *would* have auto-posted, but send
    # everything to a human. Run in shadow until the agreement rate is acceptable.
    auto_post_mode: Literal["live", "shadow"] = "shadow"
    auto_post_max_amount: Decimal = Decimal("2500")
    non_po_auto_post_vendors: list[str] = Field(default_factory=list)
    po_required_above: Decimal | None = Decimal("5000")
    min_extraction_confidence: float = 0.85
    duplicate_window_days: int = 90
    tax_gl_account: str | None = None
    allowed_tax_rates_pct: list[Decimal] = Field(default_factory=list)  # empty = don't check
    tax_rate_tolerance_pct: Decimal = Decimal("0.25")
    require_tin_for_1099: bool = True
    tolerances: Tolerances = Field(default_factory=Tolerances)
    approval_tiers: list[ApprovalTier] = Field(default_factory=lambda: [ApprovalTier(max_amount=None, approvers=["ap-manager@example.com"])])
    payment_run_approvers: list[str] = Field(default_factory=lambda: ["controller@example.com"])
    require_mfa_for_payment_approval: bool = True
    require_mfa_above: Decimal | None = Decimal("50000")
    max_delegation_days: int = 30
    internal_domains: list[str] = Field(default_factory=list)
    disclosure: Disclosure = Field(default_factory=Disclosure)
    sod: SoD = Field(default_factory=SoD)
    bank_change_keywords: list[str] = Field(default_factory=lambda: [
        "new bank", "bank details", "banking details", "change of bank", "updated account", "account number has changed",
        "new account number", "remit to new", "routing number", "iban", "swift", "wire instructions", "payment instructions",
    ])
    entities: dict[str, dict[str, Any]] = Field(default_factory=dict)  # per-entity overrides

    @classmethod
    def load(cls, path: Path | None) -> "Policy":
        if path and Path(path).exists():
            return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})
        return cls()

    def for_entity(self, code: str) -> "Policy":
        over = self.entities.get(code)
        if not over:
            return self
        return Policy.model_validate({**self.model_dump(), **over, "entities": {}})

    def approvers_for(self, amount: Decimal) -> list[str]:
        for tier in sorted(self.approval_tiers, key=lambda t: (t.max_amount is None, t.max_amount or 0)):
            if tier.max_amount is None or amount <= tier.max_amount:
                return tier.approvers
        return self.approval_tiers[-1].approvers

    def mentions_bank_change(self, text: str) -> bool:
        low = text.lower()
        return any(k in low for k in self.bank_change_keywords)

    def is_internal(self, domain: str) -> bool:
        return domain.lower() in {d.lower() for d in self.internal_domains}


class Action(str, Enum):
    auto_post = "auto_post"
    require_approval = "require_approval"
    block = "block"


class Decision(BaseModel):
    action: Action
    reasons: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)
    shadow_auto_post: bool = False
    requires_mfa: bool = False


def decide(
    policy: Policy,
    *,
    amount: Decimal,  # functional currency; None-equivalent handled via fx_known
    vendor_id: str,
    vendor_active: bool,
    currency_ok: bool,
    duplicate: str,  # "none" | "possible" | "exact"
    match: MatchResult | None,
    extraction_confidence: float,
    coding_complete: bool,
    fx_known: bool = True,
    tax_ok: bool = True,
    tin_missing: bool = False,
    is_credit: bool = False,
    flagged: list[str] | None = None,
    extra_reasons: list[str] | None = None,
) -> Decision:
    if duplicate == "exact":
        return Decision(action=Action.block, reasons=["Exact duplicate of an existing bill"])
    if not vendor_active:
        return Decision(action=Action.block, reasons=["Vendor is inactive or on hold in the ERP"])

    reasons: list[str] = list(extra_reasons or [])
    if duplicate == "possible":
        reasons.append("Possible duplicate of an existing bill")
    if not currency_ok:
        reasons.append("Invoice currency differs from vendor/PO currency")
    if not fx_known:
        reasons.append("No FX rate to convert to functional currency")
    if not tax_ok:
        reasons.append("Tax amount doesn't correspond to an allowed tax rate")
    if tin_missing:
        reasons.append("1099 vendor has no tax id (W-9) on file")
    if extraction_confidence < policy.min_extraction_confidence:
        reasons.append(f"Low extraction confidence ({extraction_confidence:.2f})")
    if not coding_complete:
        reasons.append("One or more lines have no GL account")
    if not is_credit:
        if match is None:
            if policy.po_required_above is not None and amount > policy.po_required_above:
                reasons.append(f"No PO on an invoice above {policy.po_required_above}")
            if vendor_id not in policy.non_po_auto_post_vendors:
                reasons.append("Non-PO invoice from a vendor not on the non-PO auto-post list")
        elif match.status != MatchStatus.matched:
            reasons.append(f"PO match {match.status.value}: " + "; ".join(match.exceptions[:5]))
    if flagged:
        reasons.append("Email was flagged for review: " + "; ".join(flagged))
    if not policy.auto_post_enabled:
        reasons.append("Auto-posting disabled")
    if amount > policy.auto_post_max_amount:
        reasons.append(f"Amount {amount} exceeds auto-post limit {policy.auto_post_max_amount}")

    requires_mfa = policy.require_mfa_above is not None and amount > policy.require_mfa_above
    if reasons:
        return Decision(action=Action.require_approval, reasons=reasons, approvers=policy.approvers_for(amount), requires_mfa=requires_mfa)
    if policy.auto_post_mode == "shadow":
        return Decision(action=Action.require_approval, reasons=["Shadow mode: policy would have auto-posted this invoice"], approvers=policy.approvers_for(amount), shadow_auto_post=True, requires_mfa=requires_mfa)
    return Decision(action=Action.auto_post, reasons=["Within policy: matched/allowed, under auto-post limit"])


# --------------------------------------------------------------------- versioning


class PolicyStore:
    def __init__(self, db: Database, seed_file: Path | None, override: Policy | None = None):
        self.db = db
        self.override = override  # tests / pinned policy
        self._cache: tuple[int, Policy] | None = None
        if override is None:
            self._seed(seed_file)

    def _seed(self, seed_file: Path | None) -> None:
        with self.db.session() as s, s.begin():
            if s.scalar(select(func.count()).select_from(PolicyVersion)):
                return
            content = Policy.load(seed_file).model_dump(mode="json")
            s.add(PolicyVersion(version=1, content=content, status="active", proposed_by="bootstrap", approved_by="bootstrap", note=f"seeded from {seed_file}", approved_at=datetime.now(timezone.utc)))
        self.db.audit("system", "policy_seeded", "policy:1")

    def active(self) -> Policy:
        if self.override is not None:
            return self.override
        with self.db.session() as s:
            rec = s.scalar(select(PolicyVersion).where(PolicyVersion.status == "active").order_by(PolicyVersion.version.desc()).limit(1))
        if rec is None:
            return Policy()
        if self._cache and self._cache[0] == rec.version:
            return self._cache[1]
        pol = Policy.model_validate(rec.content)
        self._cache = (rec.version, pol)
        return pol

    def active_version(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.max(PolicyVersion.version)).where(PolicyVersion.status == "active")) or 0

    def propose(self, content: dict[str, Any], proposed_by: str, note: str | None = None) -> PolicyVersion:
        Policy.model_validate(content)  # reject invalid policy up front
        with self.db.session() as s, s.begin():
            version = (s.scalar(select(func.max(PolicyVersion.version))) or 0) + 1
            rec = PolicyVersion(version=version, content=content, status="proposed", proposed_by=proposed_by, note=note)
            s.add(rec)
        self.db.audit(proposed_by, "policy_proposed", f"policy:{version}", note=note)
        return rec

    def decide(self, version_id: str, approver: str, approve: bool) -> PolicyVersion:
        with self.db.session() as s, s.begin():
            rec = s.get(PolicyVersion, version_id)
            if rec is None or rec.status != "proposed":
                raise ValueError("no such pending policy version")
            if rec.proposed_by.lower() == approver.lower():
                raise PermissionError("the proposer can't approve their own policy change")
            if approve:
                for old in s.scalars(select(PolicyVersion).where(PolicyVersion.status == "active")):
                    old.status = "superseded"
                rec.status = "active"
            else:
                rec.status = "rejected"
            rec.approved_by, rec.approved_at = approver, datetime.now(timezone.utc)
        self.db.audit(approver, "policy_activated" if approve else "policy_rejected", f"policy:{rec.version}")
        return rec
