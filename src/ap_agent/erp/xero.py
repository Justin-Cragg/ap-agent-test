"""Xero adapter (Accounting API 2.0).

Auth, either:
* Custom Connection (machine-to-machine, client credentials) - leave
  XERO_REFRESH_TOKEN empty; or
* standard OAuth 2.0 app - do the consent flow once and set
  XERO_REFRESH_TOKEN + XERO_TENANT_ID. Xero rotates refresh tokens, so new
  ones are persisted via ``token_store``.

Bills are ``ACCPAY`` invoices. Xero has no goods receipts, so matching is 2-way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Callable

import httpx

from ap_agent.erp.base import ERPConnector, ERPError, OAuthHTTP, dec, parse_date
from ap_agent.models import Bill, BillDraft, BillStatus, GLAccount, POLine, PurchaseOrder, Vendor

TOKEN_URL = "https://identity.xero.com/connect/token"
SCOPES = "accounting.transactions accounting.contacts accounting.settings.read"
_STATUS = {"DRAFT": BillStatus.draft, "SUBMITTED": BillStatus.draft, "AUTHORISED": BillStatus.open, "PAID": BillStatus.paid, "VOIDED": BillStatus.void, "DELETED": BillStatus.void}


def _q(value: str) -> str:
    return value.replace('"', '\\"')


class XeroERP(ERPConnector):
    name = "xero"
    supports_receiving = False

    def __init__(self, client_id: str, client_secret: str, tenant_id: str, refresh_token: str, token_get: Callable[[str], str | None], token_set: Callable[[str, str], None], bank_account_code: str = "", transport: httpx.BaseTransport | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.initial_refresh = refresh_token
        self.token_get, self.token_set = token_get, token_set
        self.bank_account_code = bank_account_code
        self._transport = transport
        headers = {"Accept": "application/json"}
        if tenant_id:
            headers["xero-tenant-id"] = tenant_id
        self.api = OAuthHTTP("https://api.xero.com/api.xro/2.0", self._fetch_token, headers=headers, transport=transport)

    def _fetch_token(self) -> tuple[str, int]:
        refresh = self.token_get("xero_refresh_token") or self.initial_refresh
        data = {"grant_type": "refresh_token", "refresh_token": refresh} if refresh else {"grant_type": "client_credentials", "scope": SCOPES}
        with httpx.Client(transport=self._transport, timeout=30) as http:
            resp = http.post(TOKEN_URL, data=data, auth=(self.client_id, self.client_secret))
        if resp.status_code >= 400:
            raise ERPError(f"Xero token error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if refresh and body.get("refresh_token") and body["refresh_token"] != refresh:
            self.token_set("xero_refresh_token", body["refresh_token"])
        return body["access_token"], int(body.get("expires_in", 1800))

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _vendor(c: dict) -> Vendor:
        email = c.get("EmailAddress") or None
        return Vendor(id=c["ContactID"], name=c.get("Name", ""), tax_id=c.get("TaxNumber") or None, email=email, email_domains=[email.split("@")[-1].lower()] if email else [], currency=c.get("DefaultCurrency"), active=c.get("ContactStatus", "ACTIVE") == "ACTIVE", default_gl_account=c.get("PurchasesDefaultAccountCode"))

    def search_vendors(self, *, name=None, tax_id=None, email=None):
        found: dict[str, Vendor] = {}
        if name:
            for c in self.api.get("/Contacts", params={"searchTerm": name, "summaryOnly": "false"}).get("Contacts", []):
                found[c["ContactID"]] = self._vendor(c)
        if email:
            domain = email.split("@")[-1]
            for c in self.api.get("/Contacts", params={"where": f'EmailAddress!=null&&EmailAddress.EndsWith("@{_q(domain)}")'}).get("Contacts", []):
                found[c["ContactID"]] = self._vendor(c)
        if tax_id:
            for c in self.api.get("/Contacts", params={"where": f'TaxNumber=="{_q(tax_id)}"'}).get("Contacts", []):
                found[c["ContactID"]] = self._vendor(c)
        return list(found.values())

    def get_vendor(self, vendor_id):
        rows = self.api.get(f"/Contacts/{vendor_id}").get("Contacts", [])
        return self._vendor(rows[0]) if rows else None

    def get_purchase_order(self, po_number):
        try:
            rows = self.api.get(f"/PurchaseOrders/{po_number}").get("PurchaseOrders", [])
        except ERPError:
            return None
        if not rows:
            return None
        po = rows[0]
        lines = [POLine(line_id=ln.get("LineItemID", str(i)), item_code=ln.get("ItemCode"), description=ln.get("Description", ""), quantity_ordered=dec(ln.get("Quantity", 1)), quantity_received=None, unit_price=dec(ln.get("UnitAmount"))) for i, ln in enumerate(po.get("LineItems", []))]
        return PurchaseOrder(id=po["PurchaseOrderID"], number=po.get("PurchaseOrderNumber", po_number), vendor_id=po["Contact"]["ContactID"], currency=po.get("CurrencyCode"), status=po.get("Status"), lines=lines)

    @staticmethod
    def _bill(inv: dict) -> Bill:
        return Bill(id=inv["InvoiceID"], vendor_id=inv["Contact"]["ContactID"], invoice_number=inv.get("InvoiceNumber", ""), invoice_date=parse_date(inv.get("DateString") or inv.get("Date")), due_date=parse_date(inv.get("DueDateString") or inv.get("DueDate")), total=dec(inv.get("Total")), balance=dec(inv.get("AmountDue")), currency=inv.get("CurrencyCode"), status=_STATUS.get(inv.get("Status", ""), BillStatus.unknown))

    def find_bills(self, *, vendor_id, invoice_number=None):
        where = f'Type=="ACCPAY"&&Contact.ContactID==guid("{_q(vendor_id)}")'
        if invoice_number:
            where += f'&&InvoiceNumber=="{_q(invoice_number)}"'
        return [self._bill(i) for i in self.api.get("/Invoices", params={"where": where}).get("Invoices", [])]

    def list_open_bills(self, *, due_before=None):
        where = 'Type=="ACCPAY"&&Status=="AUTHORISED"'
        if due_before:
            where += f"&&DueDate<=DateTime({due_before.year},{due_before.month},{due_before.day})"
        return [self._bill(i) for i in self.api.get("/Invoices", params={"where": where}).get("Invoices", [])]

    def list_gl_accounts(self):
        rows = self.api.get("/Accounts", params={"where": 'Status=="ACTIVE"&&(Class=="EXPENSE"||Class=="ASSET")'}).get("Accounts", [])
        return [GLAccount(id=a["Code"], number=a["Code"], name=a.get("Name", ""), type=a.get("Type")) for a in rows if a.get("Code")]

    # ------------------------------------------------------------------ writes
    def create_bill(self, draft: BillDraft) -> Bill:
        inv = {
            "Type": "ACCPAY",
            "Contact": {"ContactID": draft.vendor_id},
            "InvoiceNumber": draft.invoice_number,
            "Date": draft.invoice_date.isoformat(),
            "LineAmountTypes": "Exclusive" if draft.tax_total else "NoTax",
            "Status": "AUTHORISED",
            "LineItems": [{"Description": ln.description[:4000], "Quantity": float(ln.quantity or 1), "UnitAmount": float(ln.unit_price if ln.unit_price is not None else ln.amount), "AccountCode": ln.gl_account, **({"ItemCode": ln.item_code} if ln.item_code and ln.po_line_id else {})} for ln in draft.lines],
        }
        if draft.due_date:
            inv["DueDate"] = draft.due_date.isoformat()
        if draft.currency:
            inv["CurrencyCode"] = draft.currency
        if draft.po_number:
            inv["Reference"] = draft.po_number
        resp = self.api.post("/Invoices", json={"Invoices": [inv]}).json()
        return self._bill(resp["Invoices"][0])

    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date) -> str:
        if not self.bank_account_code:
            raise ERPError("XERO_BANK_ACCOUNT_CODE is not configured")
        body = {"Payments": [{"Invoice": {"InvoiceID": bill.id}, "Account": {"Code": self.bank_account_code}, "Date": payment_date.isoformat(), "Amount": float(amount)}]}
        return self.api.request("PUT", "/Payments", json=body).json()["Payments"][0]["PaymentID"]
