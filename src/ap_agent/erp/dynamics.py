"""Microsoft Dynamics 365 Business Central adapter (API v2.0).

Auth: Microsoft Entra ID client credentials (app registration with the
``API.ReadWrite.All`` application permission on Dynamics 365 Business Central,
plus the app added under "Microsoft Entra Applications" in BC with a
permission set limited to purchasing - see docs/permissions.md).

Receiving is native (``receivedQuantity`` on PO lines), so 3-way matching is
supported. Purchase invoices are created as drafts with lines, then posted
with the bound action ``Microsoft.NAV.post``.

BC has no idempotency header, so writes reconcile first: an existing invoice
with the same vendor invoice number is reused (a half-built draft from a
crashed attempt is deleted and rebuilt).

One connector = one BC company; configure one entity per company.
Dynamics 365 Finance & Operations uses a different (OData entity) API; add a
sibling adapter implementing ``ERPConnector`` if you run F&O.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from ap_agent.erp.base import ERPConnector, ERPError, OAuthHTTP, dec, parse_date
from ap_agent.models import Bill, BillDraft, BillStatus, GLAccount, POLine, PurchaseOrder, Vendor

_STATUS = {"Draft": BillStatus.draft, "In Review": BillStatus.draft, "Open": BillStatus.open, "Paid": BillStatus.paid, "Canceled": BillStatus.void, "Corrective": BillStatus.void}


def _q(value: str) -> str:
    return value.replace("'", "''")


class DynamicsBCERP(ERPConnector):
    name = "dynamics_bc"
    supports_receiving = True

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, environment: str, company_id: str, payment_journal_id: str = "", transport: httpx.BaseTransport | None = None):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.payment_journal_id = payment_journal_id
        self._transport = transport
        base = f"https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})"
        self.api = OAuthHTTP(base, self._fetch_token, headers={"Accept": "application/json"}, transport=transport)

    def _fetch_token(self) -> tuple[str, int]:
        with httpx.Client(transport=self._transport, timeout=30) as http:
            resp = http.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret, "scope": "https://api.businesscentral.dynamics.com/.default"},
            )
        if resp.status_code >= 400:
            raise ERPError(f"Entra token error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        return body["access_token"], int(body.get("expires_in", 3600))

    def _all(self, url: str, params: dict[str, Any] | None = None, max_rows: int = 50000) -> list[dict]:
        rows: list[dict] = []
        next_url: str | None = url
        while next_url and len(rows) < max_rows:
            data = self.api.get(next_url, params=params)
            rows += data.get("value", [])
            next_url, params = data.get("@odata.nextLink"), None
        return rows

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _vendor(v: dict) -> Vendor:
        email = v.get("email") or None
        return Vendor(id=v["id"], name=v.get("displayName", ""), tax_id=v.get("taxRegistrationNumber") or None, email=email, email_domains=[email.split("@")[-1].lower()] if email else [], currency=v.get("currencyCode") or None, payment_terms=v.get("paymentTermsId") or None, active=not v.get("blocked"), is_1099=bool(v.get("irs1099Code")))

    def search_vendors(self, *, name=None, tax_id=None, email=None):
        filters = []
        if tax_id:
            filters.append(f"taxRegistrationNumber eq '{_q(tax_id)}'")
        if email:
            filters.append(f"contains(email,'@{_q(email.split('@')[-1])}')")
        if name:
            filters.append(f"contains(displayName,'{_q(name)}')")
        if not filters:
            return []
        data = self.api.get("/vendors", params={"$filter": " or ".join(filters), "$top": 20})
        return [self._vendor(v) for v in data.get("value", [])]

    def get_vendor(self, vendor_id):
        try:
            return self._vendor(self.api.get(f"/vendors({vendor_id})"))
        except ERPError:
            return None

    def get_purchase_order(self, po_number):
        data = self.api.get("/purchaseOrders", params={"$filter": f"number eq '{_q(po_number)}'", "$expand": "purchaseOrderLines"})
        rows = data.get("value", [])
        if not rows:
            return None
        po = rows[0]
        lines = [
            POLine(line_id=ln["id"], item_code=ln.get("lineObjectNumber"), description=ln.get("description", ""), quantity_ordered=dec(ln.get("quantity")), quantity_received=dec(ln.get("receivedQuantity")), quantity_billed=dec(ln.get("invoicedQuantity")), unit_price=dec(ln.get("directUnitCost")))
            for ln in po.get("purchaseOrderLines", [])
            if ln.get("lineType") in ("Item", "Account", "Resource", "Fixed Asset", "Charge")
        ]
        return PurchaseOrder(id=po["id"], number=po["number"], vendor_id=po["vendorId"], currency=po.get("currencyCode") or None, status=po.get("status"), lines=lines)

    @staticmethod
    def _bill(b: dict) -> Bill:
        return Bill(id=b["id"], vendor_id=b["vendorId"], invoice_number=b.get("vendorInvoiceNumber") or b.get("number", ""), invoice_date=parse_date(b.get("invoiceDate")), due_date=parse_date(b.get("dueDate")), total=dec(b.get("totalAmountIncludingTax")), balance=dec(b.get("remainingAmount", b.get("totalAmountIncludingTax"))), currency=b.get("currencyCode") or None, status=_STATUS.get(b.get("status", ""), BillStatus.unknown), po_number=b.get("orderNumber") or None)

    def find_bills(self, *, vendor_id, invoice_number=None):
        flt = f"vendorId eq {vendor_id}"
        if invoice_number:
            flt += f" and vendorInvoiceNumber eq '{_q(invoice_number)}'"
        return [self._bill(b) for b in self._all("/purchaseInvoices", {"$filter": flt})]

    def list_open_bills(self, *, due_before=None):
        flt = "status eq 'Open'"
        if due_before:
            flt += f" and dueDate le {due_before.isoformat()}"
        return [self._bill(b) for b in self._all("/purchaseInvoices", {"$filter": flt})]

    def list_gl_accounts(self):
        rows = self._all("/accounts", {"$filter": "category eq 'Expense' or category eq 'Cost of Goods Sold' or category eq 'Assets'"})
        return [GLAccount(id=a["number"], number=a["number"], name=a.get("displayName", ""), type=a.get("category")) for a in rows if not a.get("blocked") and a.get("directPosting", True)]

    def exchange_rate(self, from_ccy: str, to_ccy: str, on: date) -> Decimal | None:
        # BC stores rates against local currency (LCY): exchangeAmount FCY = relationalExchangeAmount LCY.
        rows = self._all("/currencyExchangeRates", {"$filter": f"currencyCode eq '{_q(from_ccy)}' and startingDate le {on.isoformat()}", "$orderby": "startingDate desc", "$top": 1}, max_rows=1)
        if not rows or rows[0].get("relationalCurrencyCode") not in ("", None, to_ccy):
            return None
        r = rows[0]
        return dec(r["relationalExchangeAmount"]) / dec(r["exchangeAmount"]) if dec(r.get("exchangeAmount")) else None

    # ------------------------------------------------------------------ writes
    def _line(self, ln) -> dict:
        if ln.po_line_id:
            return {"lineType": "Item", "lineObjectNumber": ln.item_code, "description": ln.description[:100], "quantity": float(ln.quantity or 1), "directUnitCost": float(ln.unit_price or ln.amount)}
        return {"lineType": "Account", "lineObjectNumber": ln.gl_account, "description": ln.description[:100], "quantity": float(ln.quantity or 1), "directUnitCost": float(ln.unit_price if ln.unit_price is not None else ln.amount)}

    def create_bill(self, draft: BillDraft, idempotency_key: str) -> Bill:
        for existing in self.find_bills(vendor_id=draft.vendor_id, invoice_number=draft.invoice_number):
            if existing.status == BillStatus.draft:
                self.api.request("DELETE", f"/purchaseInvoices({existing.id})")  # half-built by a crashed attempt
            elif existing.status != BillStatus.void:
                return existing
        header = {"vendorId": draft.vendor_id, "vendorInvoiceNumber": draft.invoice_number, "invoiceDate": draft.invoice_date.isoformat()}
        if draft.due_date:
            header["dueDate"] = draft.due_date.isoformat()
        inv = self.api.post("/purchaseInvoices", json=header).json()
        for ln in draft.lines:
            self.api.post(f"/purchaseInvoices({inv['id']})/purchaseInvoiceLines", json=self._line(ln))
        self.api.post(f"/purchaseInvoices({inv['id']})/Microsoft.NAV.post")
        return Bill(id=inv["id"], vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, due_date=draft.due_date, total=draft.total, balance=draft.total, currency=draft.currency, status=BillStatus.open, po_number=draft.po_number)

    def create_credit(self, draft: BillDraft, idempotency_key: str) -> Bill:
        flt = f"vendorId eq {draft.vendor_id} and externalDocumentNumber eq '{_q(draft.invoice_number)}'"
        for cm in self._all("/purchaseCreditMemos", {"$filter": flt}):
            if cm.get("status") != "Draft":
                return Bill(id=cm["id"], vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, total=-draft.total, balance=-draft.total, status=BillStatus.open)
            self.api.request("DELETE", f"/purchaseCreditMemos({cm['id']})")
        cm = self.api.post("/purchaseCreditMemos", json={"vendorId": draft.vendor_id, "creditMemoDate": draft.invoice_date.isoformat(), "externalDocumentNumber": draft.invoice_number}).json()
        for ln in draft.lines:
            self.api.post(f"/purchaseCreditMemos({cm['id']})/purchaseCreditMemoLines", json=self._line(ln.model_copy(update={"po_line_id": None})))
        self.api.post(f"/purchaseCreditMemos({cm['id']})/Microsoft.NAV.post")
        return Bill(id=cm["id"], vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, total=-draft.total, balance=-draft.total, currency=draft.currency, status=BillStatus.open)

    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date, idempotency_key: str) -> str:
        """Adds an applied line to the vendor payment journal. The journal itself is
        posted in Business Central, so bank release stays under BC's own controls."""
        if not self.payment_journal_id:
            raise ERPError("DYNAMICS_PAYMENT_JOURNAL_ID is not configured")
        ref = idempotency_key[:35]
        base = f"/vendorPaymentJournals({self.payment_journal_id})/vendorPayments"
        for line in self._all(base, {"$filter": f"externalDocumentNumber eq '{_q(ref)}'"}):
            return line["id"]
        body = {"vendorId": bill.vendor_id, "postingDate": payment_date.isoformat(), "amount": float(amount), "appliesToInvoiceId": bill.id, "externalDocumentNumber": ref, "description": f"Payment {bill.invoice_number}"[:100]}
        return self.api.post(base, json=body).json()["id"]
