"""Deterministic 2-way / 3-way matching of invoice lines to PO lines."""

from __future__ import annotations

from decimal import Decimal
from difflib import SequenceMatcher
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ap_agent.models import POLine, PurchaseOrder

if TYPE_CHECKING:
    from ap_agent.ap.policy import Tolerances


class MatchStatus(str, Enum):
    matched = "matched"
    variance = "variance"
    unmatched = "unmatched"


class InvoiceLineIn(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal
    item_code: str | None = None
    po_line_reference: str | None = None


class LineMatch(BaseModel):
    invoice_line: int
    po_line_id: str | None
    status: MatchStatus
    issues: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    status: MatchStatus
    match_type: str  # two_way | three_way
    po_number: str
    lines: list[LineMatch] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)


def _pct_diff(a: Decimal, b: Decimal) -> Decimal:
    if b == 0:
        return Decimal("0") if a == 0 else Decimal("100")
    return abs(a - b) / abs(b) * 100


def _pair(line: InvoiceLineIn, candidates: list[POLine]) -> POLine | None:
    if line.po_line_reference:
        for pl in candidates:
            if pl.line_id == str(line.po_line_reference).strip():
                return pl
    if line.item_code:
        for pl in candidates:
            if pl.item_code and pl.item_code.lower() == line.item_code.lower():
                return pl
    best, best_score = None, 0.0
    for pl in candidates:
        score = SequenceMatcher(None, line.description.lower(), pl.description.lower()).ratio()
        if line.unit_price is not None and pl.unit_price and _pct_diff(line.unit_price, pl.unit_price) < 5:
            score += 0.25
        if score > best_score:
            best, best_score = pl, score
    return best if best_score >= 0.55 else None


def match_invoice(lines: list[InvoiceLineIn], invoice_total_ex_tax: Decimal, po: PurchaseOrder, tol: "Tolerances") -> MatchResult:
    three_way = po.supports_receiving
    remaining = list(po.lines)
    results: list[LineMatch] = []
    exceptions: list[str] = []
    expected_total = Decimal("0")

    for idx, line in enumerate(lines):
        pl = _pair(line, remaining)
        if pl is None:
            results.append(LineMatch(invoice_line=idx, po_line_id=None, status=MatchStatus.unmatched, issues=["No matching PO line"]))
            exceptions.append(f"Line {idx + 1} '{line.description[:40]}' has no matching PO line")
            continue
        remaining.remove(pl)
        issues: list[str] = []
        qty = line.quantity if line.quantity is not None else (line.amount / pl.unit_price if pl.unit_price else Decimal("1"))
        price = line.unit_price if line.unit_price is not None else (line.amount / qty if qty else line.amount)

        if _pct_diff(price, pl.unit_price) > tol.price_pct:
            issues.append(f"price {price} vs PO {pl.unit_price}")
        open_to_bill = pl.quantity_ordered - pl.quantity_billed
        if qty > open_to_bill * (1 + tol.quantity_pct / 100):
            issues.append(f"qty {qty} exceeds unbilled ordered qty {open_to_bill}")
        if three_way and pl.quantity_received is not None:
            received_unbilled = pl.quantity_received - pl.quantity_billed
            if qty > received_unbilled * (1 + tol.quantity_pct / 100):
                issues.append(f"qty {qty} exceeds received-not-billed qty {received_unbilled}")
        expected_total += pl.unit_price * qty
        status = MatchStatus.variance if issues else MatchStatus.matched
        results.append(LineMatch(invoice_line=idx, po_line_id=pl.line_id, status=status, issues=issues))
        exceptions.extend(f"Line {idx + 1}: {i}" for i in issues)

    total_diff = abs(invoice_total_ex_tax - expected_total)
    if lines and total_diff > tol.total_abs and _pct_diff(invoice_total_ex_tax, expected_total) > tol.total_pct:
        exceptions.append(f"Invoice subtotal {invoice_total_ex_tax} vs PO-expected {expected_total.quantize(Decimal('0.01'))}")

    if not lines or all(r.status == MatchStatus.unmatched for r in results):
        status = MatchStatus.unmatched
    elif exceptions:
        status = MatchStatus.variance
    else:
        status = MatchStatus.matched
    return MatchResult(status=status, match_type="three_way" if three_way else "two_way", po_number=po.number, lines=results, exceptions=exceptions)
