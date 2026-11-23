"""Identity verification, secrets at rest, audit integrity, queue reliability,
idempotent posting, policy versioning, entity routing and runtime guards."""

import base64
import json
import time
from datetime import date
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from ap_agent import auth
from ap_agent.ap.policy import PolicyStore
from ap_agent.auth import RBAC, Authenticator, AuthError
from ap_agent.entities import Entity, route
from ap_agent.models import BillDraft, BillLine
from ap_agent.storage.db import AuditRecord, Database, InvoiceRecord, ReviewItem, SecretRecord
from ap_agent.storage.secrets import DBSecretStore


RB = RBAC({"ap-managers": {"roles": ["ap_manager"], "entities": ["US01"]}}, {"solo@example.com": {"roles": ["auditor"]}})


class _Key:
    def __init__(self, key):
        self.key = key


def _authn(settings, db, **upd):
    return Authenticator(settings.model_copy(update=upd), db, RB)


def test_oidc_token_verified_and_mapped(settings, tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/a.db")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth, "_jwks_client", lambda url: type("C", (), {"get_signing_key_from_jwt": lambda self, t: _Key(key.public_key())})())
    a = _authn(settings, db, auth_mode="oidc", oidc_issuer="https://idp.example", oidc_audience="ap-agent", oidc_jwks_url="https://idp.example/jwks")
    now = int(time.time())
    good = jwt.encode({"iss": "https://idp.example", "aud": "ap-agent", "exp": now + 60, "email": "Boss@Example.com", "groups": ["ap-managers"], "amr": ["pwd", "mfa"]}, key, algorithm="RS256")
    p = a.authenticate({"Authorization": f"Bearer {good}"})
    assert p.email == "boss@example.com" and "approvals:decide" in p.permissions and p.entities == {"US01"} and p.mfa
    for bad_claims in ({"aud": "other"}, {"iss": "https://evil"}, {"exp": now - 10}):
        bad = jwt.encode({"iss": "https://idp.example", "aud": "ap-agent", "exp": now + 60, "email": "x@example.com", **bad_claims}, key, algorithm="RS256")
        with pytest.raises(AuthError):
            a.authenticate({"Authorization": f"Bearer {bad}"})
    forged = jwt.encode({"iss": "https://idp.example", "aud": "ap-agent", "exp": now + 60, "email": "x@example.com"}, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")
    with pytest.raises(AuthError):
        a.authenticate({"Authorization": f"Bearer {forged}"})


def test_iap_and_alb_assertions(settings, tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path}/a.db")
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(auth, "_jwks_client", lambda url: type("C", (), {"get_signing_key_from_jwt": lambda self, t: _Key(key.public_key())})())
    a = _authn(settings, db, auth_mode="iap", iap_audience="/projects/1/global/backendServices/2")
    tok = jwt.encode({"iss": "https://cloud.google.com/iap", "aud": "/projects/1/global/backendServices/2", "exp": int(time.time()) + 60, "email": "solo@example.com"}, key, algorithm="ES256")
    assert a.authenticate({"x-goog-iap-jwt-assertion": tok}).roles == {"auditor"}
    # A plain identity header (what an attacker bypassing the proxy could send) is ignored.
    with pytest.raises(AuthError):
        a.authenticate({"X-Goog-Authenticated-User-Email": "accounts.google.com:solo@example.com"})

    from cryptography.hazmat.primitives import serialization

    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(auth, "_alb_key", lambda region, kid: pem)
    arn = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/ap/abc"
    a = _authn(settings, db, auth_mode="alb", alb_arn=arn)
    tok = jwt.encode({"email": "solo@example.com", "exp": int(time.time()) + 60}, key, algorithm="ES256", headers={"signer": arn, "kid": "k1"})
    assert a.authenticate({"x-amzn-oidc-data": tok}).email == "solo@example.com"
    other = jwt.encode({"email": "solo@example.com"}, key, algorithm="ES256", headers={"signer": "arn:aws:elasticloadbalancing:us-east-1:999:loadbalancer/app/x/y", "kid": "k1"})
    with pytest.raises(AuthError):
        a.authenticate({"x-amzn-oidc-data": other})


def test_easyauth_and_dev_mode(settings, tmp_path):
    db = Database(f"sqlite:///{tmp_path}/a.db")
    principal = {"claims": [{"typ": "preferred_username", "val": "boss@example.com"}, {"typ": "groups", "val": "ap-managers"}, {"typ": "http://schemas.microsoft.com/claims/authnmethodsreferences", "val": "mfa"}]}
    a = _authn(settings, db, auth_mode="easyauth")
    p = a.authenticate({"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(principal).encode()).decode()})
    assert p.email == "boss@example.com" and "approvals:decide" in p.permissions
    with pytest.raises(AuthError):  # dev headers only work in dev with auth_mode=none
        _authn(settings, db, auth_mode="none", environment="production").authenticate({"X-Dev-User": "x@example.com"})
    with pytest.raises(AuthError):  # bootstrap token disabled outside dev
        _authn(settings, db, environment="production").authenticate({"Authorization": "Bearer test-token"})


def test_unknown_roles_rejected():
    with pytest.raises(ValueError):
        RBAC({"g": {"roles": ["superuser"]}}, {})
    import pathlib
    import tempfile

    import yaml

    p = pathlib.Path(tempfile.mkdtemp()) / "rbac.yaml"
    p.write_text(yaml.safe_dump({"group_roles": {"g": {"roles": ["superuser"]}}}))
    with pytest.raises(ValueError):
        RBAC.load(p)


def test_runtime_guard_refuses_insecure_production_config(settings):
    prod = settings.model_copy(update={"environment": "production"})
    with pytest.raises(RuntimeError) as e:
        prod.validate_for_runtime()
    for needle in ("AUTH_MODE", "TOKEN_ENCRYPTION_KEY", "SESSION_SECRET", "PostgreSQL"):
        assert needle in str(e.value)


def test_secrets_encrypted_at_rest(tmp_path):
    from cryptography.fernet import Fernet

    db = Database(f"sqlite:///{tmp_path}/s.db")
    store = DBSecretStore(db, Fernet.generate_key().decode(), allow_plaintext=False)
    store.set("US01.quickbooks_refresh_token", "rt-secret-value")
    assert store.get("US01.quickbooks_refresh_token") == "rt-secret-value"
    with db.session() as s:
        raw = s.get(SecretRecord, "US01.quickbooks_refresh_token").value
    assert "rt-secret-value" not in raw and raw.startswith("enc:")
    with pytest.raises(RuntimeError):
        DBSecretStore(db, "", allow_plaintext=False)


def test_audit_chain_detects_tampering(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/au.db")
    for i in range(5):
        db.audit("agent", "event", f"x{i}", n=i)
    assert db.verify_audit() == (True, 5, None)
    with db.session() as s, s.begin():
        rec = s.query(AuditRecord).filter_by(seq=3).one()
        rec.detail = {"n": 999}
    ok, _, problem = db.verify_audit()
    assert not ok and "seq 3" in problem
    with db.session() as s, s.begin():
        s.query(AuditRecord).filter_by(seq=3).delete()
    assert not db.verify_audit()[0]


def test_queue_retries_then_dead_letters(service):
    class Boom(Exception):
        pass

    def broken(draft, key):
        raise Boom("ERP down")

    with service.db.session() as s, s.begin():
        inv = InvoiceRecord(status="approved", vendor_id="V100", invoice_number="Z1", pending_bill=BillDraft(vendor_id="V100", invoice_number="Z1", invoice_date=date.today(), total=Decimal("5"), lines=[BillLine(description="x", amount=Decimal("5"), gl_account="6100")]).model_dump(mode="json"))
        s.add(inv)
    service.erp.create_bill = broken
    service.db.enqueue("post_invoice", {"invoice_id": inv.id}, max_attempts=2)
    from sqlalchemy import update

    from ap_agent.storage.db import Job

    assert service.work()[0]["status"] == "queued"  # retried later with backoff
    with service.db.session() as s, s.begin():
        s.execute(update(Job).values(run_after=__import__("datetime").datetime(2000, 1, 1)))
    assert service.work()[0]["status"] == "dead"
    with service.db.session() as s:
        assert s.query(ReviewItem).filter_by(kind="job_failed").count() == 1


def test_posting_is_idempotent_after_crash(service):
    with service.db.session() as s, s.begin():
        inv = InvoiceRecord(status="posting", vendor_id="V100", invoice_number="Z2", pending_bill=BillDraft(vendor_id="V100", invoice_number="Z2", invoice_date=date.today(), total=Decimal("5"), lines=[BillLine(description="x", amount=Decimal("5"), gl_account="6100")]).model_dump(mode="json"))
        s.add(inv)
    service.erp.create_bill(BillDraft.model_validate(inv.pending_bill), f"ap-{inv.id}")  # first attempt reached the ERP, then the worker died
    service.db.enqueue("post_invoice", {"invoice_id": inv.id})
    service.work()
    assert len([b for b in service.erp.bills.values() if b.invoice_number == "Z2"]) == 1
    with service.db.session() as s:
        assert s.get(InvoiceRecord, inv.id).status == "posted"


def test_job_claim_is_exclusive(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/q.db")
    db.enqueue("process_email", {"x": 1}, dedupe_key="email:a:1")
    assert db.enqueue("process_email", {"x": 1}, dedupe_key="email:a:1") is None
    first = db.claim(["process_email"], "w1")
    assert first is not None and db.claim(["process_email"], "w2") is None


def test_policy_two_person_rule(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/p.db")
    store = PolicyStore(db, None)
    assert store.active_version() == 1
    v = store.propose({"auto_post_max_amount": "500"}, "user:alice@example.com")
    with pytest.raises(PermissionError):
        store.decide(v.id, "user:alice@example.com", True)
    store.decide(v.id, "user:bob@example.com", True)
    assert store.active().auto_post_max_amount == Decimal("500") and store.active_version() == 2
    with pytest.raises(Exception):
        store.propose({"auto_post_max_amount": "abc"}, "user:alice@example.com")


def test_entity_routing():
    us = Entity(code="US01", name="US", mailbox_aliases=["ap-us@example.com"])
    uk = Entity(code="UK01", name="UK", mailbox_aliases=["ap-uk@example.com"])
    assert route(["ap-uk@example.com"], [us, uk]).code == "UK01"
    assert route(["someone@example.com"], [us, uk]) is None
    assert route(["ap-us@example.com", "ap-uk@example.com"], [us, uk]) is None  # ambiguous -> human
    assert route(["x@example.com"], [us]).code == "US01"


def test_multi_entity_settings_from_prefixed_env(settings, monkeypatch, tmp_path):
    import yaml

    from ap_agent.entities import load_entities

    f = tmp_path / "entities.yaml"
    f.write_text(yaml.safe_dump({"entities": [{"code": "US01", "name": "US", "env_prefix": "US01_"}, {"code": "UK01", "name": "UK", "functional_currency": "GBP", "env_prefix": "UK01_", "overrides": {"erp_provider": "xero"}}]}))
    monkeypatch.setenv("US01_QUICKBOOKS_REALM_ID", "realm-us")
    ents = load_entities(settings.model_copy(update={"entities_file": f}))
    assert ents[0].settings.quickbooks_realm_id == "realm-us" and ents[1].settings.erp_provider == "xero" and ents[1].functional_currency == "GBP"


def test_retention_purge(service, tmp_path):
    from datetime import datetime, timedelta, timezone

    uri = service.blobs.put("default/x.pdf", b"%PDF", "application/pdf")
    with service.db.session() as s, s.begin():
        old = InvoiceRecord(status="posted", blob_uri=uri, extracted={"a": 1}, created_at=datetime.now(timezone.utc) - timedelta(days=365 * 8))
        new = InvoiceRecord(status="posted", blob_uri=uri, extracted={"a": 1})
        s.add_all([old, new])
    assert service.purge_expired() == 1
    with service.db.session() as s:
        assert s.get(InvoiceRecord, old.id).extracted == {} and s.get(InvoiceRecord, new.id).extracted == {"a": 1}


def test_approver_with_empty_identity_is_refused(service):
    from ap_agent.auth import Principal

    with pytest.raises(PermissionError):
        service.decide_approval("x", Principal(id="", kind="user", email="", permissions={"approvals:decide"}, entities={"*"}), True)
