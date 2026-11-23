"""Oracle NetSuite adapter (SuiteTalk REST + SuiteQL).

Auth: OAuth 2.0 machine-to-machine (client credentials with a JWT client
assertion signed by the private key of a certificate uploaded under
Setup > Integration > OAuth 2.0 Client Credentials (M2M) Setup).

Receiving is native in NetSuite, so 3-way matching is supported. Bills against a
PO are created by transforming the PO into a vendor bill, which keeps the
PO -> bill link (and NetSuite's own billed-quantity tracking) intact.

Field names follow the NetSuite REST record and SuiteQL schemas; customised
accounts should confirm them in the Records Catalog.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from decimal import Decimal

import httpx
import jwt

from ap_agent.erp.base import ERPConnector, ERPError, OAuthHTTP, dec, parse_date
from ap_agent.models import Bill, BillDraft, BillStatus, GLAccount, POLine, PurchaseOrder, Vendor

_STATUS = {"A": BillStatus.open, "B": BillStatus.paid, "C": BillStatus.void, "D": BillStatus.draft, "E": BillStatus.draft}


def _q(value: str) -> str:
    return value.replace("'", "''")


class NetSuiteERP(ERPConnector):
    name = "netsuite"
    supports_receiving = True

    def __init__(self, account_id: str, client_id: str, certificate_id: str, private_key_pem: str, payment_account_id: str = "", transport: httpx.BaseTransport | None = None):
        self.account_host = account_id.lower().replace("_", "-")
        self.client_id = client_id
        self.certificate_id = certificate_id
        self.private_key = private_key_pem
        self.payment_account_id = payment_account_id
        base = f"https://{self.account_host}.suitetalk.api.netsuite.com/services/rest"
        self.token_url = f"{base}/auth/oauth2/v1/token"
        self._transport = transport
        self.api = OAuthHTTP(base, self._fetch_token, headers={"Content-Type": "application/json"}, transport=transport)

    # ------------------------------------------------------------------ auth
    def _fetch_token(self) -> tuple[str, int]:
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": self.client_id, "scope": ["rest_webservices"], "aud": self.token_url, "iat": now, "exp": now + 3600, "jti": uuid.uuid4().hex},
            self.private_key,
            algorithm="PS256",
            headers={"kid": self.certificate_id, "typ": "JWT"},
        )
        with httpx.Client(transport=self._transport, timeout=30) as http:
            resp = http.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                },
            )
        if resp.status_code >= 400:
            raise ERPError(f"NetSuite token error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        return body["access_token"], int(body.get("expires_in", 3600))

    def _suiteql(self, query: str, limit: int = 200, max_rows: int = 20000) -> list[dict]:
        """SuiteQL with offset pagination (queries are read-only, so safe to retry)."""
        rows: list[dict] = []
        offset = 0
        while True:
            page = self.api.post(f"/query/v1/suiteql?limit={limit}&offset={offset}", json={"q": query}, headers={"Prefer": "transient"}, idempotent=True).json()
            rows += page.get("items", [])
            if not page.get("hasMore") or len(rows) >= max_rows:
                return rows
            offset += limit

    def _by_external_id(self, record: str, key: str) -> str | None:
        try:
            return str(self.api.get(f"/record/v1/{record}/eid:{key}")["id"])
        except ERPError as e:
            if e.status == 404:
                return None
            raise

    # ------------------------------------------------------------------ reads
    def _vendor(self, row: dict) -> Vendor:
        email = row.get("email")
        return Vendor(
            id=str(row["id"]),
            name=row.get("companyname") or row.get("entityid") or "",
            tax_id=row.get("taxidnum"),
            email=email,
            email_domains=[email.split("@")[-1].lower()] if email else [],
            currency=row.get("currency_code"),
            payment_terms=row.get("terms_name"),
            active=row.get("isinactive", "F") != "T",
            default_gl_account=str(row["expenseaccount"]) if row.get("expenseaccount") else None,
            is_1099=row.get("is1099eligible") == "T",
        )

    _VENDOR_SQL = (
        "SELECT v.id, v.entityid, v.companyname, v.email, v.taxidnum, v.isinactive, v.expenseaccount, v.is1099eligible, "
        "BUILTIN.DF(v.currency) AS currency_code, BUILTIN.DF(v.terms) AS terms_name FROM vendor v WHERE "
    )

    def search_vendors(self, *, name=None, tax_id=None, email=None):
        clauses = []
        if tax_id:
            clauses.append(f"REPLACE(v.taxidnum, '-', '') = '{_q(tax_id.replace('-', ''))}'")
        if email:
            clauses.append(f"LOWER(v.email) LIKE '%@{_q(email.split('@')[-1].lower())}'")
        if name:
            clauses.append(f"LOWER(v.companyname) LIKE '%{_q(name.lower())}%'")
        if not clauses:
            return []
        return [self._vendor(r) for r in self._suiteql(self._VENDOR_SQL + " OR ".join(clauses), limit=20)]

    def get_vendor(self, vendor_id):
        rows = self._suiteql(self._VENDOR_SQL + f"v.id = {int(vendor_id)}", limit=1)
        return self._vendor(rows[0]) if rows else None

    def get_purchase_order(self, po_number):
        rows = self._suiteql(f"SELECT id FROM transaction WHERE type = 'PurchOrd' AND tranid = '{_q(po_number)}'", limit=1)
        if not rows:
            return None
        po_id = rows[0]["id"]
        rec = self.api.get(f"/record/v1/purchaseOrder/{po_id}?expandSubResources=true")
        lines = [
            POLine(
                line_id=str(item.get("line")),
                item_code=(item.get("item") or {}).get("refName"),
                description=item.get("description") or "",
                quantity_ordered=dec(item.get("quantity")),
                quantity_received=dec(item.get("quantityReceived")),
                quantity_billed=dec(item.get("quantityBilled")),
                unit_price=dec(item.get("rate")),
            )
            for item in rec.get("item", {}).get("items", [])
        ]
        return PurchaseOrder(id=str(po_id), number=po_number, vendor_id=str(rec["entity"]["id"]), currency=(rec.get("currency") or {}).get("refName"), status=(rec.get("status") or {}).get("refName") if isinstance(rec.get("status"), dict) else rec.get("status"), lines=lines)

    _BILL_SQL = (
        "SELECT t.id, t.tranid, t.entity, t.trandate, t.duedate, t.foreigntotal, t.foreignamountunpaid, "
        "t.status, BUILTIN.DF(t.currency) AS currency_code FROM transaction t WHERE t.type = 'VendBill' AND "
    )

    def _bill(self, row: dict) -> Bill:
        status = str(row.get("status") or "")
        return Bill(id=str(row["id"]), vendor_id=str(row["entity"]), invoice_number=row.get("tranid") or "", invoice_date=parse_date(_ns_date(row.get("trandate"))), due_date=parse_date(_ns_date(row.get("duedate"))), total=dec(row.get("foreigntotal")), balance=dec(row.get("foreignamountunpaid")), currency=row.get("currency_code"), status=_STATUS.get(status[-1:], BillStatus.unknown))

    def find_bills(self, *, vendor_id, invoice_number=None):
        where = f"t.entity = {int(vendor_id)}"
        if invoice_number:
            where += f" AND t.tranid = '{_q(invoice_number)}'"
        return [self._bill(r) for r in self._suiteql(self._BILL_SQL + where)]

    def list_open_bills(self, *, due_before=None):
        where = "t.status = 'VendBill:A'"
        if due_before:
            where += f" AND t.duedate <= TO_DATE('{due_before.isoformat()}', 'YYYY-MM-DD')"
        return [self._bill(r) for r in self._suiteql(self._BILL_SQL + where, limit=1000, max_rows=100000)]

    def list_gl_accounts(self):
        rows = self._suiteql("SELECT id, acctnumber, fullname, accttype FROM account WHERE isinactive = 'F' AND accttype IN ('Expense', 'COGS', 'OthExpense', 'FixedAsset', 'OthCurrAsset')", limit=1000)
        return [GLAccount(id=str(r["id"]), number=r.get("acctnumber") or str(r["id"]), name=r.get("fullname") or "", type=r.get("accttype")) for r in rows]

    # ------------------------------------------------------------------ writes
    # Every write carries externalId = idempotency key. NetSuite enforces it as
    # unique per record type, so a retried POST can't create a second bill; we
    # look the record up by eid: first and after any failure.

    def _expense_items(self, lines):
        return {"items": [{"account": {"id": ln.gl_account}, "amount": float(ln.amount), "memo": ln.description[:999]} for ln in lines]}

    def create_bill(self, draft: BillDraft, idempotency_key: str) -> Bill:
        existing = self._by_external_id("vendorBill", idempotency_key)
        if existing is None:
            body: dict = {"externalId": idempotency_key, "tranId": draft.invoice_number, "tranDate": draft.invoice_date.isoformat(), "memo": draft.memo or ""}
            if draft.due_date:
                body["dueDate"] = draft.due_date.isoformat()
            expense = [ln for ln in draft.lines if not ln.po_line_id]
            if expense:
                body["expense"] = self._expense_items(expense)
            try:
                if draft.po_id:
                    # Transform keeps the PO link; restrict to the invoiced quantities.
                    po_lines = {ln.po_line_id: ln for ln in draft.lines if ln.po_line_id}
                    body["item"] = {"items": [{"orderLine": int(lid), "quantity": float(ln.quantity or 0), "rate": float(ln.unit_price or 0)} for lid, ln in po_lines.items()]}
                    resp = self.api.post(f"/record/v1/purchaseOrder/{draft.po_id}/!transform/vendorBill", json=body, idempotent=True)
                else:
                    body["entity"] = {"id": draft.vendor_id}
                    if draft.subsidiary_id:
                        body["subsidiary"] = {"id": draft.subsidiary_id}
                    resp = self.api.post("/record/v1/vendorBill", json=body, idempotent=True)
                existing = resp.headers.get("Location", "").rstrip("/").split("/")[-1]
            except ERPError:
                existing = self._by_external_id("vendorBill", idempotency_key)
                if existing is None:
                    raise
        return Bill(id=existing, vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, due_date=draft.due_date, total=draft.total, balance=draft.total, currency=draft.currency, status=BillStatus.open, po_number=draft.po_number)

    def create_credit(self, draft: BillDraft, idempotency_key: str) -> Bill:
        existing = self._by_external_id("vendorCredit", idempotency_key)
        if existing is None:
            body = {"externalId": idempotency_key, "entity": {"id": draft.vendor_id}, "tranId": draft.invoice_number, "tranDate": draft.invoice_date.isoformat(), "memo": draft.memo or "", "expense": self._expense_items(draft.lines)}
            if draft.subsidiary_id:
                body["subsidiary"] = {"id": draft.subsidiary_id}
            resp = self.api.post("/record/v1/vendorCredit", json=body, idempotent=True)
            existing = resp.headers.get("Location", "").rstrip("/").split("/")[-1]
        return Bill(id=existing, vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, total=-draft.total, balance=-draft.total, currency=draft.currency, status=BillStatus.open)

    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date, idempotency_key: str) -> str:
        existing = self._by_external_id("vendorPayment", idempotency_key)
        if existing:
            return existing
        body = {"externalId": idempotency_key, "tranDate": payment_date.isoformat(), "apply": {"items": [{"doc": {"id": bill.id}, "apply": True, "amount": float(amount)}]}}
        if self.payment_account_id:
            body["account"] = {"id": self.payment_account_id}
        resp = self.api.post(f"/record/v1/vendorBill/{bill.id}/!transform/vendorPayment", json=body, idempotent=True)
        return resp.headers.get("Location", "").rstrip("/").split("/")[-1]

    def exchange_rate(self, from_ccy: str, to_ccy: str, on: date) -> Decimal | None:
        rows = self._suiteql(
            "SELECT cr.exchangerate FROM currencyrate cr JOIN currency t ON cr.transactioncurrency = t.id "
            f"JOIN currency b ON cr.basecurrency = b.id WHERE t.symbol = '{_q(from_ccy)}' AND b.symbol = '{_q(to_ccy)}' "
            f"AND cr.effectivedate <= TO_DATE('{on.isoformat()}', 'YYYY-MM-DD') ORDER BY cr.effectivedate DESC",
            limit=1, max_rows=1,
        )
        return dec(rows[0]["exchangerate"]) if rows else None


def _ns_date(value):
    """SuiteQL returns dates in the account's format (often M/D/YYYY)."""
    if not value or "-" in str(value):
        return value
    m, d, y = str(value).split("/")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
