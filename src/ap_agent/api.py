"""HTTP API (FastAPI). Runs identically on Cloud Run, ECS/App Runner, Azure
Container Apps or Kubernetes.

Triggers (all clouds): a scheduler calls ``POST /jobs/poll-mailbox`` every few
minutes (Cloud Scheduler, EventBridge Scheduler, Container Apps job / Logic
App) and ``POST /jobs/payment-run`` on payment days. Push notifications (Gmail
Pub/Sub, Graph change notifications) can call ``/webhooks/*`` to poll
immediately instead of waiting for the schedule.
"""

from __future__ import annotations

import hmac
import html
import logging
from datetime import date
from functools import lru_cache

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select

from ap_agent.config import get_settings
from ap_agent.pipeline import APService
from ap_agent.storage.db import ApprovalRecord, AuditRecord, InvoiceRecord

log = logging.getLogger(__name__)
app = FastAPI(title="AP Agent", version="0.1.0")


@lru_cache
def service() -> APService:
    return APService(get_settings())


def require_token(authorization: str = Header(default="")) -> None:
    expected = get_settings().api_token
    if not expected:
        raise HTTPException(503, "API_TOKEN not configured")
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(401, "unauthorized")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- jobs


@app.post("/jobs/poll-mailbox", dependencies=[Depends(require_token)])
def poll_mailbox(limit: int = 25):
    return {"results": service().poll_mailbox(limit)}


@app.post("/jobs/payment-run", dependencies=[Depends(require_token)])
def payment_run(payment_date: date | None = None, next_run_date: date | None = None):
    return service().propose_payment_run(payment_date, next_run_date).__dict__


# ---------------------------------------------------------------- push webhooks


@app.post("/webhooks/gmail", dependencies=[Depends(require_token)])
def gmail_push():
    # Pub/Sub push subscription (configure it with the bearer token / OIDC auth).
    return {"results": service().poll_mailbox(10)}


@app.post("/webhooks/outlook")
async def outlook_push(request: Request, validationToken: str | None = Query(default=None)):
    # Graph subscription validation handshake echoes the token as text/plain.
    if validationToken is not None:
        return PlainTextResponse(validationToken)
    # Create the Graph subscription with clientState = API_TOKEN; verify it here.
    body = await request.json()
    expected = get_settings().api_token
    states = [n.get("clientState", "") for n in body.get("value", [])]
    if not expected or not states or not all(hmac.compare_digest(s, expected) for s in states):
        raise HTTPException(401, "bad clientState")
    return {"results": service().poll_mailbox(10)}


# ---------------------------------------------------------------- approvals

_PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><title>AP approval</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;line-height:1.5}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:1rem}}button{{padding:.6rem 1.2rem;margin-right:.5rem}}</style></head>
<body><h1>{title}</h1><pre>{detail}</pre>{form}</body></html>"""


def _approval_detail(appr: ApprovalRecord) -> str:
    svc = service()
    lines = [f"Type: {appr.kind}", f"Amount: {appr.amount}", "Reasons:", *[f"  - {r}" for r in appr.reasons]]
    if appr.kind == "invoice":
        with svc.db.session() as s:
            inv = s.get(InvoiceRecord, appr.subject_id)
        if inv and inv.pending_bill:
            pb = inv.pending_bill
            lines += ["", f"Vendor: {pb['vendor_id']}  Invoice: {pb['invoice_number']}  Date: {pb['invoice_date']}  Due: {pb.get('due_date')}", f"PO: {pb.get('po_number')}", "Lines:"]
            lines += [f"  {ln['description'][:60]}  {ln['amount']}  GL {ln.get('gl_account')}  PO line {ln.get('po_line_id')}" for ln in pb["lines"]]
            lines += ["", f"Agent's coding notes: {pb.get('coding_rationale', '')}"]
    return "\n".join(lines)


@app.get("/approvals/{approval_id}/{action}", response_class=HTMLResponse)
def approval_page(approval_id: str, action: str, token: str):
    # GET never changes state (mail scanners pre-fetch links); it renders a confirmation form.
    if action not in ("approve", "reject"):
        raise HTTPException(404)
    with service().db.session() as s:
        appr = s.get(ApprovalRecord, approval_id)
    if appr is None or not hmac.compare_digest(token, appr.token):
        raise HTTPException(404)
    if appr.status != "pending":
        return _PAGE.format(title=f"Already {appr.status}", detail=html.escape(_approval_detail(appr)), form="")
    idp = get_settings().identity_header
    who = "" if idp else '<p><label>Your email <input name="decided_by" type="email" required></label></p>'
    form = f"""<form method="post" action="/approvals/{approval_id}/{action}">
<input type="hidden" name="token" value="{html.escape(token)}">{who}
<p><label>Comment <input name="comment"></label></p>
<button type="submit">Confirm {action}</button></form>"""
    return _PAGE.format(title=f"{action.title()} {appr.kind.replace('_', ' ')}?", detail=html.escape(_approval_detail(appr)), form=form)


@app.post("/approvals/{approval_id}/{action}")
def approval_decide(approval_id: str, action: str, request: Request, token: str = Form(...), decided_by: str = Form(default=""), comment: str = Form(default="")):
    if action not in ("approve", "reject"):
        raise HTTPException(404)
    idp = get_settings().identity_header
    if idp:
        decided_by = request.headers.get(idp, "").split(":")[-1]  # IAP prefixes "accounts.google.com:"
    if not decided_by:
        raise HTTPException(400, "approver identity required")
    try:
        result = service().decide_approval(approval_id, token, action == "approve", decided_by, comment or None)
    except KeyError:
        raise HTTPException(404)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return HTMLResponse(_PAGE.format(title="Recorded", detail=html.escape(str(result)), form=""))


# ---------------------------------------------------------------- admin reads


@app.get("/invoices", dependencies=[Depends(require_token)])
def list_invoices(status: str | None = None, limit: int = 100):
    with service().db.session() as s:
        q = select(InvoiceRecord).order_by(InvoiceRecord.created_at.desc()).limit(min(limit, 500))
        if status:
            q = q.where(InvoiceRecord.status == status)
        return [{"id": r.id, "vendor_id": r.vendor_id, "invoice_number": r.invoice_number, "total": r.total, "currency": r.currency, "status": r.status, "erp_bill_id": r.erp_bill_id, "created_at": r.created_at} for r in s.scalars(q)]


@app.get("/approvals", dependencies=[Depends(require_token)])
def list_approvals(status: str = "pending"):
    with service().db.session() as s:
        return [{"id": a.id, "kind": a.kind, "subject_id": a.subject_id, "amount": a.amount, "approvers": a.approvers, "reasons": a.reasons, "status": a.status} for a in s.scalars(select(ApprovalRecord).where(ApprovalRecord.status == status))]


@app.get("/audit", dependencies=[Depends(require_token)])
def audit(entity: str | None = None, limit: int = 200):
    with service().db.session() as s:
        q = select(AuditRecord).order_by(AuditRecord.ts.desc()).limit(min(limit, 1000))
        if entity:
            q = q.where(AuditRecord.entity == entity)
        return [{"ts": a.ts, "actor": a.actor, "action": a.action, "entity": a.entity, "detail": a.detail} for a in s.scalars(q)]
