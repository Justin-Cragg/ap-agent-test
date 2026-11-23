"""End-to-end: email -> extraction -> agent loop -> policy -> ERP, with a
scripted model. Verifies the controls hold regardless of what the model asks."""

import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

from ap_agent.agent.runner import echoable_content
from ap_agent.agent.tools import TOOLS, strict_schema
from ap_agent.mail.base import PROCESSED_LABEL, REVIEW_LABEL, Attachment, EmailMessage
from ap_agent.models import ExtractedInvoice, ExtractedLine
from ap_agent.storage.db import ApprovalRecord, InvoiceRecord

from conftest import message, text, tool_use


def _invoice_id(params) -> str:
    return re.search(r'"invoice_id": "([0-9a-f]+)"', params["messages"][0]["content"][0]["text"]).group(1)


def _email(body="Please find our invoice attached.", sender="billing@acme-supply.example"):
    return EmailMessage(id="m1", thread_id="t1", sender=sender, subject="Invoice INV-5001", body_text=body,
                        attachments=[Attachment(id="a1", filename="INV-5001.pdf", content_type="application/pdf", data=b"%PDF-1.4 fake")])


def _extracted(qty_bolts=None, conf=0.97):
    lines = [ExtractedLine(description="Steel widget 10mm", quantity=100, unit_price=12.5, amount=1250, item_code="WID-10")]
    if qty_bolts:
        lines.append(ExtractedLine(description="Hex bolt M4 (box)", quantity=qty_bolts, unit_price=8.0, amount=8.0 * qty_bolts, item_code="BLT-4"))
    sub = sum(ln.amount for ln in lines)
    return ExtractedInvoice(is_invoice=True, document_type="invoice", vendor_name="Acme Industrial Supply", invoice_number="INV-5001", invoice_date=date.today().isoformat(), po_number="PO-1001", currency="USD", subtotal=sub, tax_total=0, total=sub, payment_terms="2/10 Net 30", lines=lines, confidence=conf)


def _submit(params, lines, total):
    return message(tool_use("submit_invoice", invoice_id=_invoice_id(params), vendor_id="V100", po_number="PO-1001", invoice_number="INV-5001",
                            invoice_date=date.today().isoformat(), due_date=(date.today() + timedelta(days=30)).isoformat(), currency="USD",
                            total=total, tax_total=0, lines=lines, coding_rationale="PO-backed materials"))


def _script_happy(fake, lines, total):
    fake.agent_script.extend([
        lambda p: message(tool_use("lookup_vendor", name="Acme Industrial Supply", tax_id=None, sender_email="billing@acme-supply.example")),
        lambda p: message(tool_use("check_duplicate", invoice_id=_invoice_id(p), vendor_id="V100"), tool_use("match_invoice_to_po", invoice_id=_invoice_id(p), po_number="PO-1001")),
        lambda p: _submit(p, lines, total),
        message(text("Posted Acme invoice INV-5001 against PO-1001.")),
    ])


def test_matched_invoice_auto_posts(service, fake_client):
    fake_client.extraction_script.append(_extracted())
    _script_happy(fake_client, [{"description": "Steel widget 10mm", "amount": 1250, "quantity": 100, "unit_price": 12.5, "gl_account": "5100", "po_line_id": "1", "item_code": "WID-10"}], 1250)
    service.mailbox.messages.append(_email())

    [result] = service.poll_mailbox()

    assert result["status"] == "done", result
    bills = [b for b in service.erp.bills.values() if b.invoice_number == "INV-5001"]
    assert len(bills) == 1 and bills[0].po_number == "PO-1001"
    assert service.erp.pos["PO-1001"].lines[0].quantity_billed == 100
    with service.db.session() as s:
        inv = s.query(InvoiceRecord).one()
        assert inv.status == "posted" and inv.erp_bill_id == bills[0].id
    assert PROCESSED_LABEL in service.mailbox.labels["m1"]

    # Every agent request: strict tools, eager streaming, effort, server-side fallbacks, caching.
    agent_calls = [c for c in fake_client.calls if "tools" in c]
    assert all(c["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in c["betas"] for c in agent_calls)
    assert all(c["cache_control"] == {"type": "ephemeral"} and c["output_config"]["effort"] == "high" for c in agent_calls)
    assert all(t["strict"] and t["eager_input_streaming"] for t in agent_calls[0]["tools"])
    # Conversation is append-only: each request extends the previous one.
    for a, b in zip(agent_calls, agent_calls[1:]):
        assert b["messages"][: len(a["messages"])] == a["messages"]

    # Same email again: idempotent.
    assert service.poll_mailbox() == []


def test_variance_routes_to_approval_then_posts(service, fake_client):
    # 20 bolts invoiced, only 15 received -> 3-way match variance
    fake_client.extraction_script.append(_extracted(qty_bolts=20))
    lines = [
        {"description": "Steel widget 10mm", "amount": 1250, "quantity": 100, "unit_price": 12.5, "gl_account": "5100", "po_line_id": "1", "item_code": "WID-10"},
        {"description": "Hex bolt M4 (box)", "amount": 160, "quantity": 20, "unit_price": 8.0, "gl_account": "5100", "po_line_id": "2", "item_code": "BLT-4"},
    ]
    _script_happy(fake_client, lines, 1410)
    service.mailbox.messages.append(_email())
    service.poll_mailbox()

    assert not [b for b in service.erp.bills.values() if b.invoice_number == "INV-5001"]
    with service.db.session() as s:
        appr = s.query(ApprovalRecord).one()
        inv = s.query(InvoiceRecord).one()
    assert inv.status == "awaiting_approval"
    assert any("received" in r for r in appr.reasons)
    assert service.mailbox.sent and "approve" in service.mailbox.sent[0]["body"].lower()

    # Wrong approver is refused; right approver posts the bill.
    import pytest

    with pytest.raises(PermissionError):
        service.decide_approval(appr.id, appr.token, True, "intern@example.com")
    out = service.decide_approval(appr.id, appr.token, True, appr.approvers[0])
    assert out["status"] == "posted"
    assert service.erp.bills[out["erp_bill_id"]].total == 1410


def test_model_cannot_inflate_total(service, fake_client):
    fake_client.extraction_script.append(_extracted())
    # Model submits 2000 although the document says 1250.
    _script_happy(fake_client, [{"description": "Steel widget 10mm", "amount": 2000, "quantity": 100, "unit_price": 20, "gl_account": "5100", "po_line_id": "1", "item_code": "WID-10"}], 2000)
    service.mailbox.messages.append(_email())
    service.poll_mailbox()
    with service.db.session() as s:
        appr = s.query(ApprovalRecord).one()
    assert any("differs from extracted total" in r for r in appr.reasons)
    assert not [b for b in service.erp.bills.values() if b.invoice_number == "INV-5001"]


def test_bank_change_email_forces_review(service, fake_client):
    fake_client.extraction_script.append(_extracted())
    _script_happy(fake_client, [{"description": "Steel widget 10mm", "amount": 1250, "quantity": 100, "unit_price": 12.5, "gl_account": "5100", "po_line_id": "1", "item_code": "WID-10"}], 1250)
    service.mailbox.messages.append(_email(body="Invoice attached. Note our new bank details: IBAN XX00 1234."))
    [result] = service.poll_mailbox()

    assert result["status"] == "needs_review"
    assert REVIEW_LABEL in service.mailbox.labels["m1"]
    first_prompt = fake_client.calls[1]["messages"][0]["content"][0]["text"]
    assert "already been flagged as bank_detail_change" in first_prompt
    with service.db.session() as s:
        assert s.query(InvoiceRecord).one().status == "awaiting_approval"  # never auto-posted


def test_refusal_goes_to_review(service, fake_client):
    fake_client.extraction_script.append(_extracted())
    fake_client.agent_script.append(message(stop_reason="refusal"))
    service.mailbox.messages.append(_email())
    [result] = service.poll_mailbox()
    assert result["status"] == "needs_review"
    assert REVIEW_LABEL in service.mailbox.labels["m1"]


def test_invalid_tool_input_returned_as_error(service, fake_client):
    fake_client.extraction_script.append(_extracted())
    fake_client.agent_script.extend([
        message(tool_use("get_purchase_order", wrong_field=1)),
        message(text("Done.")),
    ])
    service.mailbox.messages.append(_email())
    service.poll_mailbox()
    tool_result = fake_client.calls[-1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "INVALID_INPUT" in tool_result["content"]


def test_vendor_inquiry_reply_and_domain_guard(service, fake_client):
    inquiry = EmailMessage(id="m2", sender="ar@northwind.example", subject="Payment status NW-2026-0042", body_text="When will invoice NW-2026-0042 be paid?")
    fake_client.agent_script.extend([
        message(tool_use("get_bill_status", vendor_id="V200", invoice_number="NW-2026-0042")),
        message(tool_use("reply_to_sender", body="Invoice NW-2026-0042 is approved and due on the due date.\n\nAccounts Payable")),
        message(text("Answered payment status inquiry.")),
    ])
    service.mailbox.messages.append(inquiry)
    service.poll_mailbox()
    status = json.loads(fake_client.calls[1]["messages"][-1]["content"][0]["content"])
    assert status["allowed"] and status["erp_bills"][0]["invoice_number"] == "NW-2026-0042"
    assert service.mailbox.replies[0]["to"] == "ar@northwind.example" and service.mailbox.replies[0]["sent"] is False  # draft mode

    # Same question from a look-alike domain: no disclosure.
    spoof = EmailMessage(id="m3", sender="ar@northwind-payments.example", subject="Payment status", body_text="Status of NW-2026-0042?")
    fake_client.agent_script.extend([message(tool_use("get_bill_status", vendor_id="V200", invoice_number="NW-2026-0042")), message(text("Flagged."))])
    service.mailbox.messages.append(spoof)
    service.poll_mailbox()
    assert json.loads(fake_client.calls[-1]["messages"][-1]["content"][0]["content"])["allowed"] is False


def test_payment_run_requires_approval(service, fake_client):
    fake_client.agent_script.extend([
        message(tool_use("list_open_bills", due_within_days=14)),
        message(tool_use("propose_payment_run", payment_date=date.today().isoformat(), payments=[{"bill_id": "B1", "vendor_id": "V200", "amount": 1800, "reason": "due soon"}], rationale="1 bill")),
        message(text("Proposed.")),
    ])
    result = service.propose_payment_run()
    assert result.status == "done"
    assert not service.erp.payments  # nothing paid yet
    with service.db.session() as s:
        appr = s.query(ApprovalRecord).filter_by(kind="payment_run").one()
    out = service.decide_approval(appr.id, appr.token, True, "controller@example.com")
    assert out["status"] == "executed" and service.erp.payments[0]["bill_id"] == "B1"


def test_payment_run_rejects_overpayment(service, fake_client):
    fake_client.agent_script.extend([
        message(tool_use("propose_payment_run", payment_date=date.today().isoformat(), payments=[{"bill_id": "B1", "vendor_id": "V200", "amount": 99999, "reason": "x"}], rationale="x")),
        message(text("Could not propose.")),
    ])
    service.propose_payment_run()
    err = fake_client.calls[-1]["messages"][-1]["content"][0]
    assert err["is_error"] and "exceeds open balance" in err["content"]


def test_echoable_content_drops_declined_model_internals():
    blocks = [SimpleNamespace(type=t) for t in ("thinking", "text", "tool_use", "fallback", "thinking", "tool_use")]
    out = [b.type for b in echoable_content(blocks)]
    assert out == ["text", "fallback", "thinking", "tool_use"]


def test_tool_schemas_are_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert "$ref" not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for t in TOOLS:
        walk(strict_schema(t.input_model))
