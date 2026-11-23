from datetime import date
from decimal import Decimal

from ap_agent.ap.duplicates import check_duplicate, normalize_invoice_number
from ap_agent.ap.matching import InvoiceLineIn, MatchStatus, match_invoice
from ap_agent.ap.policy import Action, Policy, Tolerances, decide
from ap_agent.erp.memory import InMemoryERP
from ap_agent.storage.db import Database


def _lines(*specs):
    return [InvoiceLineIn(description=d, quantity=Decimal(q), unit_price=Decimal(p), amount=Decimal(q) * Decimal(p), item_code=c) for d, q, p, c in specs]


def test_three_way_match_ok():
    po = InMemoryERP.with_demo_data().pos["PO-1001"]
    r = match_invoice(_lines(("Steel widget 10mm", "100", "12.50", "WID-10")), Decimal("1250"), po, Tolerances())
    assert r.status == MatchStatus.matched and r.match_type == "three_way"
    assert r.lines[0].po_line_id == "1"


def test_three_way_match_flags_qty_over_received():
    po = InMemoryERP.with_demo_data().pos["PO-1001"]
    # 20 ordered, only 15 received
    r = match_invoice(_lines(("Hex bolt M4 box", "20", "8.00", None)), Decimal("160"), po, Tolerances())
    assert r.status == MatchStatus.variance
    assert any("received" in e for e in r.exceptions)


def test_price_variance_outside_tolerance():
    po = InMemoryERP.with_demo_data().pos["PO-1001"]
    r = match_invoice(_lines(("Steel widget 10mm", "100", "13.00", "WID-10")), Decimal("1300"), po, Tolerances(price_pct=Decimal("2")))
    assert r.status == MatchStatus.variance
    assert any("price" in e for e in r.exceptions)


def test_unmatched_line():
    po = InMemoryERP.with_demo_data().pos["PO-1001"]
    r = match_invoice(_lines(("Consulting hours", "5", "200", None)), Decimal("1000"), po, Tolerances())
    assert r.status == MatchStatus.unmatched


def test_normalize_invoice_number():
    assert normalize_invoice_number("INV-000123 ") == normalize_invoice_number("inv123")
    assert normalize_invoice_number("0042") == "42"
    assert normalize_invoice_number("A10023") == "A10023"


def test_duplicate_exact_and_possible(tmp_path):
    erp = InMemoryERP.with_demo_data()
    db = Database(f"sqlite:///{tmp_path}/d.db")
    exact = check_duplicate(erp, db, vendor_id="V200", invoice_number="NW 2026 0042", total=Decimal("1"), invoice_date=None, window_days=90)
    assert exact.verdict == "exact"
    bill = erp.bills["B1"]
    possible = check_duplicate(erp, db, vendor_id="V200", invoice_number="DIFFERENT-1", total=bill.total, invoice_date=bill.invoice_date, window_days=90)
    assert possible.verdict == "possible"
    none = check_duplicate(erp, db, vendor_id="V100", invoice_number="X-1", total=Decimal("5"), invoice_date=date.today(), window_days=90)
    assert none.verdict == "none"


def _decide(**kw):
    base = dict(amount=Decimal("1000"), vendor_id="V1", vendor_active=True, currency_ok=True, duplicate="none", match=None, extraction_confidence=0.95, coding_complete=True)
    base.update(kw)
    return decide(kw.pop("policy", Policy(non_po_auto_post_vendors=["V1"], auto_post_mode="live")), **base)


def test_policy_auto_posts_small_allowed_non_po():
    assert _decide().action == Action.auto_post


def test_policy_blocks_exact_duplicate_and_inactive_vendor():
    assert _decide(duplicate="exact").action == Action.block
    assert _decide(vendor_active=False).action == Action.block


def test_policy_requires_approval_and_picks_tier():
    p = Policy(auto_post_mode="live", non_po_auto_post_vendors=["V1"], approval_tiers=[{"max_amount": 10000, "approvers": ["mgr@x"]}, {"max_amount": None, "approvers": ["cfo@x"]}])
    d = decide(p, amount=Decimal("25000"), vendor_id="V1", vendor_active=True, currency_ok=True, duplicate="none", match=None, extraction_confidence=0.95, coding_complete=True)
    assert d.action == Action.require_approval and d.approvers == ["cfo@x"]
    d = decide(p, amount=Decimal("3000"), vendor_id="V1", vendor_active=True, currency_ok=True, duplicate="none", match=None, extraction_confidence=0.95, coding_complete=True)
    assert d.action == Action.require_approval and d.approvers == ["mgr@x"]


def test_policy_low_confidence_and_unknown_non_po_vendor():
    assert _decide(extraction_confidence=0.5).action == Action.require_approval
    d = decide(Policy(), amount=Decimal("100"), vendor_id="V9", vendor_active=True, currency_ok=True, duplicate="none", match=None, extraction_confidence=0.95, coding_complete=True)
    assert d.action == Action.require_approval


def test_bank_change_detection():
    p = Policy()
    assert p.mentions_bank_change("Please note our NEW BANK details effective immediately")
    assert not p.mentions_bank_change("Attached is invoice 123")


def test_policy_shadow_mode_and_mfa_threshold():
    d = decide(Policy(non_po_auto_post_vendors=["V1"]), amount=Decimal("100"), vendor_id="V1", vendor_active=True, currency_ok=True, duplicate="none", match=None, extraction_confidence=0.95, coding_complete=True)
    assert d.action == Action.require_approval and d.shadow_auto_post
    d = _decide(amount=Decimal("60000"))
    assert d.requires_mfa


def test_policy_fx_tax_and_1099():
    assert any("FX" in r for r in _decide(fx_known=False).reasons)
    assert any("tax" in r.lower() for r in _decide(tax_ok=False).reasons)
    assert any("1099" in r for r in _decide(tin_missing=True).reasons)


def test_tax_rate_check():
    from ap_agent.ap.controls import tax_rate_ok

    p = Policy(allowed_tax_rates_pct=[Decimal("0"), Decimal("20")])
    assert tax_rate_ok(p, Decimal("100"), Decimal("20"))
    assert not tax_rate_ok(p, Decimal("100"), Decimal("7"))
    assert tax_rate_ok(Policy(), Decimal("100"), Decimal("7"))


def test_entity_policy_overrides():
    p = Policy(auto_post_max_amount=Decimal("2500"), entities={"UK01": {"auto_post_max_amount": "1000"}})
    assert p.for_entity("UK01").auto_post_max_amount == Decimal("1000")
    assert p.for_entity("US01").auto_post_max_amount == Decimal("2500")
