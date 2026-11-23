"""AP controls, enforced in code.

The model proposes; this module decides. Nothing the model says (or an email
says) can post a bill above the auto-post limit, skip an approval tier, post a
duplicate, or release a payment - those paths do not exist in the tool layer.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ap_agent.ap.matching import MatchResult, MatchStatus


class Tolerances(BaseModel):
    price_pct: Decimal = Decimal("2.0")
    quantity_pct: Decimal = Decimal("0")
    total_abs: Decimal = Decimal("5.00")
    total_pct: Decimal = Decimal("1.0")


class ApprovalTier(BaseModel):
    max_amount: Decimal | None = None  # None = unlimited
    approvers: list[str]


class Policy(BaseModel):
    auto_post_enabled: bool = True
    auto_post_max_amount: Decimal = Decimal("2500")
    non_po_auto_post_vendors: list[str] = Field(default_factory=list)
    po_required_above: Decimal | None = Decimal("5000")
    min_extraction_confidence: float = 0.85
    duplicate_window_days: int = 90
    # If set, invoice tax is posted as a separate line to this account; if not,
    # the ERP's tax engine calculates tax on the bill lines.
    tax_gl_account: str | None = None
    tolerances: Tolerances = Field(default_factory=Tolerances)
    approval_tiers: list[ApprovalTier] = Field(default_factory=lambda: [ApprovalTier(max_amount=None, approvers=["ap-manager@example.com"])])
    payment_run_approvers: list[str] = Field(default_factory=lambda: ["controller@example.com"])
    # Phrases that indicate a change to remittance / bank details: always a human.
    bank_change_keywords: list[str] = Field(default_factory=lambda: [
        "new bank", "bank details", "banking details", "change of bank", "updated account", "account number has changed",
        "new account number", "remit to new", "routing number", "iban", "swift", "wire instructions", "payment instructions",
    ])

    @classmethod
    def load(cls, path: Path | None) -> "Policy":
        if path and Path(path).exists():
            return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})
        return cls()

    def approvers_for(self, amount: Decimal) -> list[str]:
        for tier in sorted(self.approval_tiers, key=lambda t: (t.max_amount is None, t.max_amount or 0)):
            if tier.max_amount is None or amount <= tier.max_amount:
                return tier.approvers
        return self.approval_tiers[-1].approvers

    def mentions_bank_change(self, text: str) -> bool:
        low = text.lower()
        return any(k in low for k in self.bank_change_keywords)


class Action(str, Enum):
    auto_post = "auto_post"
    require_approval = "require_approval"
    block = "block"


class Decision(BaseModel):
    action: Action
    reasons: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)


def decide(
    policy: Policy,
    *,
    amount: Decimal,
    vendor_id: str,
    vendor_active: bool,
    currency_ok: bool,
    duplicate: str,  # "none" | "possible" | "exact"
    match: MatchResult | None,
    extraction_confidence: float,
    coding_complete: bool,
) -> Decision:
    if duplicate == "exact":
        return Decision(action=Action.block, reasons=["Exact duplicate of an existing bill"])
    if not vendor_active:
        return Decision(action=Action.block, reasons=["Vendor is inactive or on hold in the ERP"])

    reasons: list[str] = []
    if duplicate == "possible":
        reasons.append("Possible duplicate of an existing bill")
    if not currency_ok:
        reasons.append("Invoice currency differs from vendor/PO currency")
    if extraction_confidence < policy.min_extraction_confidence:
        reasons.append(f"Low extraction confidence ({extraction_confidence:.2f})")
    if not coding_complete:
        reasons.append("One or more lines have no GL account")
    if match is None:
        if policy.po_required_above is not None and amount > policy.po_required_above:
            reasons.append(f"No PO on an invoice above {policy.po_required_above}")
        if vendor_id not in policy.non_po_auto_post_vendors:
            reasons.append("Non-PO invoice from a vendor not on the non-PO auto-post list")
    elif match.status != MatchStatus.matched:
        reasons.append(f"PO match {match.status.value}: " + "; ".join(match.exceptions[:5]))
    if not policy.auto_post_enabled:
        reasons.append("Auto-posting disabled")
    if amount > policy.auto_post_max_amount:
        reasons.append(f"Amount {amount} exceeds auto-post limit {policy.auto_post_max_amount}")

    if reasons:
        return Decision(action=Action.require_approval, reasons=reasons, approvers=policy.approvers_for(amount))
    return Decision(action=Action.auto_post, reasons=["Within policy: matched/allowed, under auto-post limit"])
