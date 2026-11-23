from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ap_agent import api
from ap_agent.auth import RBAC, Authenticator, create_api_key
from ap_agent.storage.db import ApprovalRecord, InvoiceRecord

RBAC_MAP = {
    "ap-managers": {"roles": ["ap_manager"], "entities": ["*"]},
    "ap-clerks": {"roles": ["ap_clerk"], "entities": ["*"]},
    "platform-admins": {"roles": ["admin"], "entities": ["*"]},
    "uk-clerks": {"roles": ["ap_clerk"], "entities": ["UK01"]},
}


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(api, "service", lambda: service)
    monkeypatch.setattr(api, "get_settings", lambda: service.settings)
    monkeypatch.setattr(api, "authenticator", lambda: Authenticator(service.settings, service.db, RBAC(RBAC_MAP, {})))
    api._hits.clear()
    return TestClient(api.app)


def as_user(email, group, mfa=True):
    return {"X-Dev-User": email, "X-Dev-Groups": group, "X-Dev-MFA": "true" if mfa else "false"}


def _pending_invoice(service):
    with service.db.session() as s, s.begin():
        inv = InvoiceRecord(status="awaiting_approval", vendor_id="V200", invoice_number="Q1", total=Decimal("10"), pending_bill={"vendor_id": "V200", "invoice_number": "Q1", "invoice_date": "2026-11-01", "total": "10", "lines": [{"description": "x", "amount": "10", "gl_account": "6100"}]})
        s.add(inv)
        s.flush()
        appr = ApprovalRecord(kind="invoice", subject_id=inv.id, amount=Decimal("10"), approvers=["group:ap-managers"], reasons=["test"])
        s.add(appr)
    return inv, appr


def test_unauthenticated_and_permission_checks(client, service):
    assert client.get("/invoices").status_code == 401
    assert client.get("/invoices", headers=as_user("clerk@example.com", "ap-clerks")).status_code == 200
    assert client.post("/jobs/poll", headers=as_user("clerk@example.com", "ap-clerks")).status_code == 403
    assert client.get("/audit", headers=as_user("clerk@example.com", "ap-clerks")).status_code == 403
    me = client.get("/me", headers=as_user("boss@example.com", "ap-managers")).json()
    assert "approvals:decide" in me["permissions"] and "apikeys:manage" not in me["permissions"]


def test_scoped_api_keys(client, service):
    raw = create_api_key(service.db, "scheduler", ["jobs:run"], "test")
    h = {"Authorization": f"Bearer {raw}"}
    assert client.post("/jobs/poll", headers=h).status_code == 200
    assert client.get("/invoices", headers=h).status_code == 403  # scope doesn't include reads
    with pytest.raises(ValueError):
        create_api_key(service.db, "bad", ["approvals:decide"], "test")
    admin = as_user("it@example.com", "platform-admins")
    assert client.delete("/apikeys/scheduler", headers=admin).status_code == 200
    assert client.post("/jobs/poll", headers=h).status_code == 401


def test_approval_page_csrf_group_approver_and_corrections(client, service):
    inv, appr = _pending_invoice(service)
    mgr = as_user("boss@example.com", "ap-managers")
    page = client.get(f"/approvals/{appr.id}", params={"token": appr.token}, headers=mgr)
    assert page.status_code == 200 and "Approve" in page.text
    with service.db.session() as s:
        assert s.get(ApprovalRecord, appr.id).status == "pending"  # GET never decides

    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert client.post(f"/approvals/{appr.id}/decision", data={"csrf": "forged", "action": "approve"}, headers=mgr).status_code == 403
    clerk = as_user("clerk@example.com", "ap-clerks")
    clerk_page = client.get(f"/approvals/{appr.id}", headers=clerk)
    clerk_csrf = clerk_page.text.split('name="csrf" value="')[1].split('"')[0]
    assert client.post(f"/approvals/{appr.id}/decision", data={"csrf": clerk_csrf, "action": "approve"}, headers=clerk).status_code == 403

    r = client.post(f"/approvals/{appr.id}/decision", data={"csrf": csrf, "action": "approve", "token": appr.token, "gl_0": "6400"}, headers=mgr)
    assert r.status_code == 200, r.text
    service.work()
    with service.db.session() as s:
        rec = s.get(InvoiceRecord, inv.id)
        assert rec.status == "posted" and rec.pending_bill["lines"][0]["gl_account"] == "6400"


def test_entity_scoping(client, service):
    _pending_invoice(service)
    uk = as_user("uk@example.com", "uk-clerks")
    assert client.get("/invoices", headers=uk).json() == []  # default-entity data hidden
    assert client.post("/ask", json={"question": "?", "entity": "default"}, headers=uk).status_code == 403


def test_policy_two_person_rule_via_api(client, service):
    service.policies.override = None
    service.policies._seed(None)
    admin = as_user("it@example.com", "platform-admins")
    r = client.post("/policy/proposals", json={"content": {"auto_post_max_amount": "500"}, "note": "tighten"}, headers=admin)
    assert r.status_code == 200
    vid = r.json()["id"]
    assert client.post("/policy/proposals", json={"content": {"auto_post_max_amount": "not-a-number"}}, headers=admin).status_code == 400
    assert client.post(f"/policy/proposals/{vid}/decision", params={"approve": True}, headers=admin).status_code == 403  # admin lacks policy:approve


def test_webhooks_require_secret(client, service):
    assert client.post("/webhooks/gmail").status_code == 401
    assert client.post("/webhooks/gmail", params={"token": "hook-secret"}).status_code == 200
    assert client.post("/webhooks/outlook", params={"validationToken": "abc"}).text == "abc"
    assert client.post("/webhooks/outlook", json={"value": [{"clientState": "wrong"}]}).status_code == 401
    assert client.post("/webhooks/outlook", json={"value": [{"clientState": "hook-secret"}]}).status_code == 200


def test_rate_limit(client, service):
    service.settings.rate_limit_per_minute = 3
    h = as_user("clerk@example.com", "ap-clerks")
    codes = [client.get("/invoices", headers=h).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    service.settings.rate_limit_per_minute = 120
