"""QuickBooks Online adapter (Accounting API v3).

Auth: OAuth 2.0 authorization-code app. Do the consent flow once, put the
refresh token in QUICKBOOKS_REFRESH_TOKEN; Intuit rotates refresh tokens, so
each new one is persisted through ``token_store`` (the agent's database).

QBO has no goods-receipt document, so only 2-way (invoice <-> PO) matching is
possible. Bills link to PO lines via ``LinkedTxn``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Callable

import httpx

from ap_agent.erp.base import ERPConnector, ERPError, OAuthHTTP, dec, parse_date
from ap_agent.models import Bill, BillDraft, BillStatus, GLAccount, POLine, PurchaseOrder, Vendor

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
MINOR_VERSION = "75"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class QuickBooksERP(ERPConnector):
    name = "quickbooks"
    supports_receiving = False

    def __init__(self, client_id: str, client_secret: str, realm_id: str, refresh_token: str, token_get: Callable[[str], str | None], token_set: Callable[[str, str], None], sandbox: bool = False, bank_account_id: str = "", transport: httpx.BaseTransport | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.initial_refresh = refresh_token
        self.token_get, self.token_set = token_get, token_set
        self.bank_account_id = bank_account_id
        self._transport = transport
        host = "sandbox-quickbooks.api.intuit.com" if sandbox else "quickbooks.api.intuit.com"
        self.api = OAuthHTTP(f"https://{host}/v3/company/{realm_id}", self._fetch_token, headers={"Accept": "application/json", "Content-Type": "application/json"}, transport=transport)

    def _fetch_token(self) -> tuple[str, int]:
        refresh = self.token_get("quickbooks_refresh_token") or self.initial_refresh
        with httpx.Client(transport=self._transport, timeout=30) as http:
            resp = http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh}, auth=(self.client_id, self.client_secret), headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise ERPError(f"Intuit token error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("refresh_token") and body["refresh_token"] != refresh:
            self.token_set("quickbooks_refresh_token", body["refresh_token"])
        return body["access_token"], int(body.get("expires_in", 3600))

    def _query(self, sql: str) -> dict:
        return self.api.get("/query", params={"query": sql, "minorversion": MINOR_VERSION}).get("QueryResponse", {})

    def _query_all(self, sql: str, entity: str, page: int = 1000, max_rows: int = 100000) -> list[dict]:
        """Paginate with STARTPOSITION / MAXRESULTS."""
        rows: list[dict] = []
        start = 1
        while len(rows) < max_rows:
            batch = self._query(f"{sql} STARTPOSITION {start} MAXRESULTS {page}").get(entity, [])
            rows += batch
            if len(batch) < page:
                break
            start += page
        return rows

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _vendor(v: dict) -> Vendor:
        email = (v.get("PrimaryEmailAddr") or {}).get("Address")
        return Vendor(id=v["Id"], name=v.get("DisplayName") or v.get("CompanyName", ""), tax_id=v.get("TaxIdentifier"), email=email, email_domains=[email.split("@")[-1].lower()] if email else [], currency=(v.get("CurrencyRef") or {}).get("value"), payment_terms=(v.get("TermRef") or {}).get("name"), active=v.get("Active", True), is_1099=bool(v.get("Vendor1099")))

    def search_vendors(self, *, name=None, tax_id=None, email=None):
        found: dict[str, Vendor] = {}
        if name:
            for v in self._query(f"SELECT * FROM Vendor WHERE DisplayName LIKE '%{_q(name)}%' MAXRESULTS 20").get("Vendor", []):
                found[v["Id"]] = self._vendor(v)
        if email or tax_id:
            # QBO can't filter on email/tax id server-side; scan active vendors.
            for v in self._query_all("SELECT * FROM Vendor WHERE Active = true", "Vendor"):
                vend = self._vendor(v)
                if email and email.split("@")[-1].lower() in vend.email_domains:
                    found[vend.id] = vend
                if tax_id and vend.tax_id and vend.tax_id.replace("-", "")[-4:] == tax_id.replace("-", "")[-4:]:
                    found[vend.id] = vend  # QBO masks tax ids except the last 4 digits
        return list(found.values())

    def get_vendor(self, vendor_id):
        try:
            return self._vendor(self.api.get(f"/vendor/{vendor_id}", params={"minorversion": MINOR_VERSION})["Vendor"])
        except ERPError:
            return None

    def get_purchase_order(self, po_number):
        rows = self._query(f"SELECT * FROM PurchaseOrder WHERE DocNumber = '{_q(po_number)}'").get("PurchaseOrder", [])
        if not rows:
            return None
        po = rows[0]
        lines = []
        for ln in po.get("Line", []):
            detail = ln.get("ItemBasedExpenseLineDetail") or ln.get("AccountBasedExpenseLineDetail")
            if not detail:
                continue
            qty = dec(detail.get("Qty", 1))
            price = dec(detail.get("UnitPrice", ln.get("Amount")))
            item = (detail.get("ItemRef") or detail.get("AccountRef") or {}).get("name")
            lines.append(POLine(line_id=str(ln["Id"]), item_code=item, description=ln.get("Description", ""), quantity_ordered=qty, quantity_received=None, quantity_billed=Decimal("0"), unit_price=price))
        return PurchaseOrder(id=po["Id"], number=po_number, vendor_id=po["VendorRef"]["value"], currency=(po.get("CurrencyRef") or {}).get("value"), status=po.get("POStatus"), lines=lines)

    @staticmethod
    def _bill(b: dict) -> Bill:
        total, balance = dec(b.get("TotalAmt")), dec(b.get("Balance"))
        status = BillStatus.paid if balance == 0 else BillStatus.partially_paid if balance < total else BillStatus.open
        return Bill(id=b["Id"], vendor_id=b["VendorRef"]["value"], invoice_number=b.get("DocNumber", ""), invoice_date=parse_date(b.get("TxnDate")), due_date=parse_date(b.get("DueDate")), total=total, balance=balance, currency=(b.get("CurrencyRef") or {}).get("value"), status=status)

    def find_bills(self, *, vendor_id, invoice_number=None):
        sql = f"SELECT * FROM Bill WHERE VendorRef = '{_q(vendor_id)}'"
        if invoice_number:
            sql += f" AND DocNumber = '{_q(invoice_number)}'"
        return [self._bill(b) for b in self._query_all(sql, "Bill")]

    def list_open_bills(self, *, due_before=None):
        sql = "SELECT * FROM Bill WHERE Balance > '0'"
        if due_before:
            sql += f" AND DueDate <= '{due_before.isoformat()}'"
        return [self._bill(b) for b in self._query_all(sql, "Bill")]

    def list_gl_accounts(self):
        rows = self._query_all("SELECT * FROM Account WHERE Classification IN ('Expense', 'Asset') AND Active = true", "Account")
        return [GLAccount(id=a["Id"], number=a.get("AcctNum") or a["Id"], name=a.get("FullyQualifiedName", a.get("Name", "")), type=a.get("AccountType")) for a in rows]

    # ------------------------------------------------------------------ writes
    # Intuit's `requestid` makes POSTs idempotent: a retry with the same id
    # returns the original response instead of creating a second record.

    def _expense_lines(self, draft: BillDraft, link_po: bool) -> list[dict]:
        lines = []
        for ln in draft.lines:
            line = {"DetailType": "AccountBasedExpenseLineDetail", "Amount": float(ln.amount), "Description": ln.description[:4000], "AccountBasedExpenseLineDetail": {"AccountRef": {"value": ln.gl_account}}}
            if link_po and ln.po_line_id and draft.po_id:
                line["LinkedTxn"] = [{"TxnId": draft.po_id, "TxnType": "PurchaseOrder", "TxnLineId": ln.po_line_id}]
            lines.append(line)
        return lines

    def _write(self, path: str, body: dict, key: str) -> dict:
        return self.api.post(path, params={"minorversion": MINOR_VERSION, "requestid": key[:50]}, json=body, idempotent=True).json()

    def create_bill(self, draft: BillDraft, idempotency_key: str) -> Bill:
        body = {"VendorRef": {"value": draft.vendor_id}, "DocNumber": draft.invoice_number[:21], "TxnDate": draft.invoice_date.isoformat(), "Line": self._expense_lines(draft, True), "PrivateNote": (draft.memo or "")[:4000]}
        if draft.due_date:
            body["DueDate"] = draft.due_date.isoformat()
        if draft.currency:
            body["CurrencyRef"] = {"value": draft.currency}
        return self._bill(self._write("/bill", body, idempotency_key)["Bill"])

    def create_credit(self, draft: BillDraft, idempotency_key: str) -> Bill:
        body = {"VendorRef": {"value": draft.vendor_id}, "DocNumber": draft.invoice_number[:21], "TxnDate": draft.invoice_date.isoformat(), "Line": self._expense_lines(draft, False)}
        if draft.currency:
            body["CurrencyRef"] = {"value": draft.currency}
        vc = self._write("/vendorcredit", body, idempotency_key)["VendorCredit"]
        return Bill(id=vc["Id"], vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, total=-draft.total, balance=-draft.total, currency=draft.currency, status=BillStatus.open)

    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date, idempotency_key: str) -> str:
        if not self.bank_account_id:
            raise ERPError("QUICKBOOKS_BANK_ACCOUNT_ID is not configured")
        body = {"VendorRef": {"value": bill.vendor_id}, "PayType": "Check", "CheckPayment": {"BankAccountRef": {"value": self.bank_account_id}}, "TotalAmt": float(amount), "TxnDate": payment_date.isoformat(), "Line": [{"Amount": float(amount), "LinkedTxn": [{"TxnId": bill.id, "TxnType": "Bill"}]}]}
        return self._write("/billpayment", body, idempotency_key)["BillPayment"]["Id"]

    def exchange_rate(self, from_ccy: str, to_ccy: str, on: date) -> Decimal | None:
        try:
            r = self.api.get("/exchangerate", params={"sourcecurrencycode": from_ccy, "asofdate": on.isoformat(), "minorversion": MINOR_VERSION}).get("ExchangeRate", {})
        except ERPError:
            return None
        return dec(r["Rate"]) if r.get("Rate") and r.get("TargetCurrencyCode") == to_ccy else None
