from decimal import Decimal

from fastapi.testclient import TestClient

from ap_agent import api
from ap_agent.storage.db import ApprovalRecord, InvoiceRecord


def test_api_auth_and_approval_confirmation(service, monkeypatch):
    monkeypatch.setattr(api, "service", lambda: service)
    monkeypatch.setattr(api, "get_settings", lambda: service.settings)
    client = TestClient(api.app)

    assert client.post("/jobs/poll-mailbox").status_code == 401
    assert client.post("/jobs/poll-mailbox", headers={"Authorization": "Bearer test-token"}).json() == {"results": []}

    with service.db.session() as s, s.begin():
        inv = InvoiceRecord(status="awaiting_approval", vendor_id="V200", invoice_number="Q1", total=Decimal("10"), pending_bill={"vendor_id": "V200", "invoice_number": "Q1", "invoice_date": "2026-11-01", "total": "10", "lines": [{"description": "x", "amount": "10", "gl_account": "6100"}]})
        s.add(inv)
        s.flush()
        appr = ApprovalRecord(kind="invoice", subject_id=inv.id, amount=Decimal("10"), approvers=["ap-manager@example.com"], reasons=["test"])
        s.add(appr)

    # GET (what an email link scanner would do) only renders a confirmation page.
    page = client.get(f"/approvals/{appr.id}/approve", params={"token": appr.token})
    assert page.status_code == 200 and "Confirm approve" in page.text
    assert client.get(f"/approvals/{appr.id}/approve", params={"token": "bad"}).status_code == 404
    with service.db.session() as s:
        assert s.get(ApprovalRecord, appr.id).status == "pending"

    resp = client.post(f"/approvals/{appr.id}/approve", data={"token": appr.token, "decided_by": "ap-manager@example.com"})
    assert resp.status_code == 200
    with service.db.session() as s:
        assert s.get(InvoiceRecord, inv.id).status == "posted"
