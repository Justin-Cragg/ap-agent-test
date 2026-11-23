"""HTTP API (FastAPI). Runs identically on Cloud Run, ECS, Azure Container Apps
or Kubernetes.

Every endpoint except /healthz and the webhook handshakes requires an
authenticated principal (see ap_agent.auth) and a specific permission;
data is filtered to the legal entities the principal may see. Browser forms
carry CSRF tokens. Requests are rate-limited per principal.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from prometheus_client import generate_latest
from pydantic import BaseModel
from sqlalchemy import func, select

from ap_agent.agent.tools import SubmitInvoice
from ap_agent.auth import RBAC, Authenticator, AuthError, Principal, create_api_key, revoke_api_key
from ap_agent.config import get_settings
from ap_agent.observability import REGISTRY, setup_logging, setup_tracing
from ap_agent.pipeline import APService
from ap_agent.storage.db import ApprovalRecord, AuditRecord, EmailRecord, InvoiceRecord, PolicyVersion, ReviewItem

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s)
    setup_tracing(s, app)
    yield


app = FastAPI(title="AP Agent", version="0.2.0", lifespan=_lifespan)


@lru_cache
def service() -> APService:
    return APService(get_settings())


@lru_cache
def authenticator() -> Authenticator:
    s = get_settings()
    return Authenticator(s, service().db, RBAC.load(s.rbac_file))


# ---------------------------------------------------------------- auth + rate limit

_hits: dict[str, list[float]] = defaultdict(list)


def current_principal(request: Request) -> Principal:
    try:
        p = authenticator().authenticate(dict(request.headers))
    except AuthError as e:
        raise HTTPException(401, str(e))
    limit = get_settings().rate_limit_per_minute
    now = time.monotonic()
    window = [t for t in _hits[p.actor] if now - t < 60]
    if len(window) >= limit:
        raise HTTPException(429, "rate limit exceeded")
    window.append(now)
    _hits[p.actor] = window
    request.state.principal = p
    return p


def require(permission: str) -> Callable[..., Principal]:
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise HTTPException(403, f"missing permission {permission}")
        return p

    return dep


def _entity_guard(p: Principal, entity: str) -> None:
    if not p.sees(entity):
        raise HTTPException(403, "no access to this entity")


def _csrf(p: Principal) -> str:
    secret = (get_settings().session_secret or "dev-session-secret").encode()
    return hmac.new(secret, p.actor.encode(), hashlib.sha256).hexdigest()


def _check_csrf(p: Principal, token: str) -> None:
    if not hmac.compare_digest(token or "", _csrf(p)):
        raise HTTPException(403, "invalid CSRF token")


def _visible(p: Principal, q, model):
    return q if "*" in p.entities else q.where(model.entity.in_(list(p.entities)))


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/me")
def me(p: Principal = Depends(current_principal)):
    return {"id": p.id, "kind": p.kind, "email": p.email, "roles": sorted(p.roles), "permissions": sorted(p.permissions), "entities": sorted(p.entities), "mfa": p.mfa}


@app.get("/metrics", dependencies=[Depends(require("metrics:read"))])
def metrics():
    return Response(generate_latest(REGISTRY), media_type="text/plain; version=0.0.4")


# ---------------------------------------------------------------- jobs (schedulers)


@app.post("/jobs/poll")
def job_poll(limit: int = 50, p: Principal = Depends(require("jobs:run"))):
    return {"queued": service().poll_mailboxes(limit), "results": service().work()}


@app.post("/jobs/work")
def job_work(max_jobs: int = 100, p: Principal = Depends(require("jobs:run"))):
    return {"results": service().work(max_jobs=max_jobs)}


@app.post("/jobs/payment-run")
def job_payment_run(entity: str | None = None, payment_date: date | None = None, next_run_date: date | None = None, p: Principal = Depends(require("jobs:run"))):
    svc = service()
    entity = entity or next(iter(svc.runtimes))
    _entity_guard(p, entity)
    svc.db.enqueue("propose_payment_run", {"entity": entity, "payment_date": payment_date.isoformat() if payment_date else None, "next_run_date": next_run_date.isoformat() if next_run_date else None}, entity=entity)
    return {"results": svc.work(kinds=["propose_payment_run"])}


@app.post("/jobs/purge")
def job_purge(p: Principal = Depends(require("jobs:run"))):
    return {"purged": service().purge_expired(p.actor)}


# ---------------------------------------------------------------- push webhooks


def _webhook_ok(secret: str) -> bool:
    expected = get_settings().webhook_secret
    return bool(expected) and hmac.compare_digest(secret or "", expected)


@app.post("/webhooks/gmail")
def gmail_push(background: BackgroundTasks, token: str = Query(default="")):
    # Pub/Sub push subscription URL: https://.../webhooks/gmail?token=<WEBHOOK_SECRET>
    if not _webhook_ok(token):
        raise HTTPException(401)
    background.add_task(service().poll_and_process, 20)
    return {"accepted": True}


@app.post("/webhooks/outlook")
async def outlook_push(request: Request, background: BackgroundTasks, validationToken: str | None = Query(default=None)):
    if validationToken is not None:  # Graph subscription handshake
        return PlainTextResponse(validationToken)
    body = await request.json()
    states = [n.get("clientState", "") for n in body.get("value", [])]
    if not states or not all(_webhook_ok(s) for s in states):
        raise HTTPException(401, "bad clientState")
    background.add_task(service().poll_and_process, 20)
    return {"accepted": True}


# ---------------------------------------------------------------- approvals

_PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;line-height:1.5}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:1rem}}
button{{padding:.6rem 1.2rem;margin-right:.5rem}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.3rem;text-align:left;font-size:.9rem}}input[type=text]{{width:7rem}}</style></head>
<body><h1>{title}</h1>{content}</body></html>"""


def _approval_view(appr: ApprovalRecord) -> tuple[str, list[dict]]:
    lines_out: list[dict] = []
    text = [f"Entity: {appr.entity}", f"Type: {appr.kind}", f"Amount (functional currency): {appr.amount}", f"Requires MFA: {appr.requires_mfa}", "Reasons:", *[f"  - {r}" for r in appr.reasons]]
    svc = service()
    with svc.db.session() as s:
        if appr.kind == "invoice":
            inv = s.get(InvoiceRecord, appr.subject_id)
            if inv and inv.pending_bill:
                pb = inv.pending_bill
                text += ["", f"Vendor: {pb['vendor_id']}  Number: {pb['invoice_number']}  Date: {pb['invoice_date']}  Due: {pb.get('due_date')}  Currency: {pb.get('currency')}  Total: {pb.get('total')}", f"PO: {pb.get('po_number')}", f"Agent's notes: {pb.get('coding_rationale', '')}"]
                lines_out = pb["lines"]
        else:
            from ap_agent.storage.db import PaymentRunRecord

            run = s.get(PaymentRunRecord, appr.subject_id)
            if run:
                text += ["", f"Payment date: {run.payment_date}  Total: {run.total}", run.rationale or "", "Bills:"] + [f"  {i['invoice_number']}  vendor {i['vendor_id']}  {i['amount']}  {i['reason']}" for i in run.items]
    return "\n".join(text), lines_out


@app.get("/approvals")
def list_approvals(status: str = "pending", p: Principal = Depends(current_principal)):
    if not (p.can("approvals:decide") or p.can("invoices:read")):
        raise HTTPException(403)
    svc = service()
    with svc.db.session() as s:
        rows = s.scalars(_visible(p, select(ApprovalRecord).where(ApprovalRecord.status == status), ApprovalRecord)).all()
    return [{"id": a.id, "entity": a.entity, "kind": a.kind, "subject_id": a.subject_id, "amount": a.amount, "approvers": a.approvers, "reasons": a.reasons, "status": a.status,
             "you_can_decide": p.can("approvals:decide", a.entity) and svc._approver_matches(p, a.approvers)} for a in rows]


@app.get("/approvals/{approval_id}", response_class=HTMLResponse)
def approval_page(approval_id: str, token: str | None = None, p: Principal = Depends(current_principal)):
    with service().db.session() as s:
        appr = s.get(ApprovalRecord, approval_id)
    if appr is None or not p.sees(appr.entity):
        raise HTTPException(404)
    detail, lines = _approval_view(appr)
    if appr.status != "pending":
        return _PAGE.format(title=f"Already {appr.status}", content=f"<pre>{html.escape(detail)}</pre>")
    rows = "".join(
        f"<tr><td>{html.escape(str(ln['description'])[:60])}</td><td>{html.escape(str(ln['amount']))}</td><td>{html.escape(str(ln.get('po_line_id') or ''))}</td>"
        f"<td><input type=text name='gl_{i}' value='{html.escape(str(ln.get('gl_account') or ''))}'></td></tr>"
        for i, ln in enumerate(lines)
    )
    table = f"<table><tr><th>Line</th><th>Amount</th><th>PO line</th><th>GL account (edit to correct)</th></tr>{rows}</table>" if rows else ""
    form = f"""<form method="post" action="/approvals/{approval_id}/decision">
<input type="hidden" name="csrf" value="{_csrf(p)}"><input type="hidden" name="token" value="{html.escape(token or '')}">
{table}<p>Signed in as {html.escape(p.email or p.id)}{' (MFA)' if p.mfa else ''}</p>
<p><label>Comment <input name="comment" size=50></label></p>
<button name="action" value="approve">Approve</button><button name="action" value="reject">Reject</button></form>"""
    return _PAGE.format(title=f"Approve {appr.kind.replace('_', ' ')}?", content=f"<pre>{html.escape(detail)}</pre>{form}")


@app.post("/approvals/{approval_id}/decision")
async def approval_decide_form(approval_id: str, request: Request, p: Principal = Depends(current_principal)):
    form = await request.form()
    _check_csrf(p, str(form.get("csrf", "")))
    action = form.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400)
    with service().db.session() as s:
        appr = s.get(ApprovalRecord, approval_id)
    if appr is None:
        raise HTTPException(404)
    _, lines = _approval_view(appr)
    corrections = [{"line": str(i), "gl_account": str(form[f"gl_{i}"]).strip()} for i, ln in enumerate(lines) if f"gl_{i}" in form and str(form[f"gl_{i}"]).strip() and str(form[f"gl_{i}"]).strip() != str(ln.get("gl_account") or "")]
    result = _decide(approval_id, p, action == "approve", str(form.get("comment") or "") or None, str(form.get("token") or "") or None, corrections)
    return HTMLResponse(_PAGE.format(title="Recorded", content=f"<pre>{html.escape(str(result))}</pre>"))


class DecisionIn(BaseModel):
    approve: bool
    comment: str | None = None
    corrections: list[dict[str, str]] = []


@app.post("/api/approvals/{approval_id}")
def approval_decide_json(approval_id: str, body: DecisionIn, p: Principal = Depends(current_principal)):
    return _decide(approval_id, p, body.approve, body.comment, None, body.corrections)


def _decide(approval_id, p, approve, comment, token, corrections):
    try:
        return service().decide_approval(approval_id, p, approve, comment=comment, token=token, corrections=corrections)
    except KeyError:
        raise HTTPException(404)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- invoices + review queue


@app.get("/invoices")
def list_invoices(status: str | None = None, limit: int = 100, p: Principal = Depends(require("invoices:read"))):
    with service().db.session() as s:
        q = _visible(p, select(InvoiceRecord), InvoiceRecord).order_by(InvoiceRecord.created_at.desc()).limit(min(limit, 500))
        if status:
            q = q.where(InvoiceRecord.status == status)
        return [{"id": r.id, "entity": r.entity, "type": r.document_type, "vendor_id": r.vendor_id, "invoice_number": r.invoice_number, "total": r.total, "currency": r.currency, "status": r.status, "erp_bill_id": r.erp_bill_id, "created_at": r.created_at} for r in s.scalars(q)]


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, p: Principal = Depends(require("invoices:read"))):
    with service().db.session() as s:
        r = s.get(InvoiceRecord, invoice_id)
    if r is None or not p.sees(r.entity):
        raise HTTPException(404)
    return {"id": r.id, "entity": r.entity, "status": r.status, "type": r.document_type, "extracted": r.extracted, "match_result": r.match_result, "pending_bill": r.pending_bill, "erp_bill_id": r.erp_bill_id}


@app.post("/invoices/{invoice_id}/submit")
def submit_invoice_manual(invoice_id: str, body: SubmitInvoice, p: Principal = Depends(require("invoices:submit"))):
    try:
        return service().submit_manually(invoice_id, p, body)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/review")
def list_review(status: str = "open", p: Principal = Depends(require("review:read"))):
    with service().db.session() as s:
        rows = s.scalars(_visible(p, select(ReviewItem).where(ReviewItem.status == status), ReviewItem).order_by(ReviewItem.created_at)).all()
    return [{"id": r.id, "entity": r.entity, "kind": r.kind, "subject_type": r.subject_type, "subject_id": r.subject_id, "reason": r.reason, "detail": r.detail, "assigned_to": r.assigned_to, "created_at": r.created_at} for r in rows]


class ResolveIn(BaseModel):
    resolution: str


@app.post("/review/{item_id}/resolve")
def resolve_review(item_id: str, body: ResolveIn, p: Principal = Depends(require("review:resolve"))):
    try:
        service().resolve_review(p, item_id, body.resolution)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@app.get("/ui/review", response_class=HTMLResponse)
def review_ui(p: Principal = Depends(require("review:read"))):
    items = list_review("open", p)
    rows = "".join(f"<tr><td>{html.escape(i['kind'])}</td><td>{html.escape(i['entity'])}</td><td>{html.escape(i['subject_type'])} {html.escape(i['subject_id'][:10])}</td><td>{html.escape(i['reason'][:200])}</td><td>{i['created_at']:%Y-%m-%d}</td></tr>" for i in items)
    return _PAGE.format(title=f"AP review queue ({len(items)})", content=f"<table><tr><th>Kind</th><th>Entity</th><th>Subject</th><th>Reason</th><th>Opened</th></tr>{rows}</table>")


# ---------------------------------------------------------------- internal Q&A


class AskIn(BaseModel):
    question: str
    entity: str | None = None


@app.post("/ask")
def ask(body: AskIn, p: Principal = Depends(require("ask"))):
    svc = service()
    entity = body.entity or next(iter(svc.runtimes))
    _entity_guard(p, entity)
    try:
        result = svc.ask(p, body.question, entity)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"answer": result.summary, "status": result.status}


@app.get("/ask", response_class=HTMLResponse)
def ask_ui(p: Principal = Depends(require("ask"))):
    ents = [c for c in service().runtimes if p.sees(c)]
    opts = "".join(f"<option>{html.escape(c)}</option>" for c in ents)
    content = f"""<form id=f><p><select name=entity>{opts}</select></p><p><textarea name=q rows=4 cols=70 placeholder="e.g. Has Acme's invoice INV-5001 been paid?"></textarea></p><button>Ask</button></form><pre id=a></pre>
<script>document.getElementById('f').onsubmit=async e=>{{e.preventDefault();const f=e.target;document.getElementById('a').textContent='...';
const r=await fetch('/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:f.q.value,entity:f.entity.value}})}});
const j=await r.json();document.getElementById('a').textContent=j.answer||JSON.stringify(j);}}</script>"""
    return _PAGE.format(title="AP assistant", content=content)


# ---------------------------------------------------------------- vendor & finance admin


class ContactIn(BaseModel):
    email: str


@app.post("/vendors/{entity}/{vendor_id}/contacts")
def add_contact(entity: str, vendor_id: str, body: ContactIn, p: Principal = Depends(require("vendors:contacts:write"))):
    _entity_guard(p, entity)
    try:
        return {"id": service().add_vendor_contact(p, entity, vendor_id, body.email)}
    except KeyError:
        raise HTTPException(404, "vendor not found")


class BankIn(BaseModel):
    account: str
    callback_phone_source: str  # e.g. "erp_vendor_record", "signed_contract" - never "invoice_or_email"
    called_number: str
    contact_name: str
    notes: str = ""


@app.post("/vendors/{entity}/{vendor_id}/bank-accounts")
def verify_bank(entity: str, vendor_id: str, body: BankIn, p: Principal = Depends(require("vendors:bank:verify"))):
    _entity_guard(p, entity)
    try:
        rid = service().verify_bank_account(p, entity, vendor_id, body.account, body.model_dump(exclude={"account"}))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "vendor not found")
    return {"id": rid}


class FxIn(BaseModel):
    base: str
    quote: str
    rate: Decimal
    as_of: date


@app.post("/fx-rates")
def add_fx(body: FxIn, p: Principal = Depends(require("fx:write"))):
    service().add_fx_rate(p, body.base, body.quote, body.rate, body.as_of)
    return {"ok": True}


class DelegationIn(BaseModel):
    delegate: str
    starts_at: datetime
    ends_at: datetime


@app.post("/delegations")
def delegate(body: DelegationIn, p: Principal = Depends(require("delegations:write"))):
    try:
        return {"id": service().delegate(p, body.delegate, body.starts_at, body.ends_at)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- policy (two-person rule)


@app.get("/policy")
def get_policy(p: Principal = Depends(current_principal)):
    if not (p.can("policy:propose") or p.can("policy:approve") or p.can("audit:read")):
        raise HTTPException(403)
    svc = service()
    with svc.db.session() as s:
        versions = s.scalars(select(PolicyVersion).order_by(PolicyVersion.version.desc()).limit(20)).all()
    return {"active": svc.policies.active().model_dump(mode="json"), "versions": [{"id": v.id, "version": v.version, "status": v.status, "proposed_by": v.proposed_by, "approved_by": v.approved_by, "note": v.note, "created_at": v.created_at} for v in versions]}


class PolicyIn(BaseModel):
    content: dict[str, Any]
    note: str | None = None


@app.post("/policy/proposals")
def propose_policy(body: PolicyIn, p: Principal = Depends(require("policy:propose"))):
    try:
        rec = service().policies.propose(body.content, p.actor, body.note)
    except Exception as e:
        raise HTTPException(400, f"invalid policy: {e}")
    return {"id": rec.id, "version": rec.version, "status": rec.status}


@app.post("/policy/proposals/{version_id}/decision")
def decide_policy(version_id: str, approve: bool, p: Principal = Depends(require("policy:approve"))):
    try:
        rec = service().policies.decide(version_id, p.actor, approve)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"version": rec.version, "status": rec.status}


# ---------------------------------------------------------------- API keys


class KeyIn(BaseModel):
    name: str
    scopes: list[str]
    entities: list[str] | None = None
    expires_at: datetime | None = None


@app.post("/apikeys")
def new_key(body: KeyIn, p: Principal = Depends(require("apikeys:manage"))):
    try:
        raw = create_api_key(service().db, body.name, body.scopes, p.actor, body.entities, body.expires_at)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": body.name, "key": raw, "note": "Shown once. Store it in your secret manager."}


@app.delete("/apikeys/{name}")
def delete_key(name: str, p: Principal = Depends(require("apikeys:manage"))):
    if not revoke_api_key(service().db, name, p.actor):
        raise HTTPException(404)
    return {"revoked": name}


# ---------------------------------------------------------------- audit & reports


@app.get("/audit")
def audit(entity: str | None = None, limit: int = 200, p: Principal = Depends(require("audit:read"))):
    with service().db.session() as s:
        q = select(AuditRecord).order_by(AuditRecord.seq.desc()).limit(min(limit, 1000))
        if entity:
            q = q.where(AuditRecord.entity == entity)
        return [{"seq": a.seq, "ts": a.ts, "actor": a.actor, "action": a.action, "entity": a.entity, "detail": a.detail, "hash": a.hash} for a in s.scalars(q)]


@app.get("/audit/verify")
def audit_verify(p: Principal = Depends(require("audit:read"))):
    ok, n, problem = service().db.verify_audit()
    return {"ok": ok, "rows": n, "problem": problem}


@app.get("/reports/shadow")
def shadow_report(p: Principal = Depends(require("reports:read"))):
    return service().shadow_report()


@app.get("/reports/costs")
def cost_report(p: Principal = Depends(require("reports:read"))):
    with service().db.session() as s:
        q = _visible(p, select(EmailRecord.entity, func.count(), func.sum(EmailRecord.cost_usd)), EmailRecord).group_by(EmailRecord.entity)
        rows = s.execute(q).all()
        inv = dict(s.execute(_visible(p, select(InvoiceRecord.entity, func.count()), InvoiceRecord).group_by(InvoiceRecord.entity)).all())
    return [{"entity": e, "emails": n, "llm_cost_usd": float(c or 0), "invoices": inv.get(e, 0), "cost_per_invoice_usd": round(float(c or 0) / inv[e], 4) if inv.get(e) else None} for e, n, c in rows]
