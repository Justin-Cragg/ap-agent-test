"""In-memory ERP used for local demos and tests."""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

from ap_agent.erp.base import ERPConnector
from ap_agent.models import Bill, BillDraft, BillStatus, GLAccount, POLine, PurchaseOrder, Vendor


class InMemoryERP(ERPConnector):
    name = "memory"
    supports_receiving = True

    def __init__(self) -> None:
        self.vendors: dict[str, Vendor] = {}
        self.pos: dict[str, PurchaseOrder] = {}
        self.bills: dict[str, Bill] = {}
        self.accounts: list[GLAccount] = []
        self.payments: list[dict] = []
        self._ids = itertools.count(1000)

    @classmethod
    def with_demo_data(cls) -> "InMemoryERP":
        erp = cls()
        erp.vendors = {
            "V100": Vendor(id="V100", name="Acme Industrial Supply", tax_id="12-3456789", email="billing@acme-supply.example", email_domains=["acme-supply.example"], currency="USD", payment_terms="2/10 Net 30", default_gl_account="5100"),
            "V200": Vendor(id="V200", name="Northwind Cloud Services", email="ar@northwind.example", email_domains=["northwind.example"], currency="USD", payment_terms="Net 30", default_gl_account="6400"),
        }
        erp.pos = {
            "PO-1001": PurchaseOrder(id="9001", number="PO-1001", vendor_id="V100", currency="USD", status="open", lines=[
                POLine(line_id="1", item_code="WID-10", description="Steel widget 10mm", quantity_ordered=Decimal(100), quantity_received=Decimal(100), unit_price=Decimal("12.50")),
                POLine(line_id="2", item_code="BLT-4", description="Hex bolt M4 (box)", quantity_ordered=Decimal(20), quantity_received=Decimal(15), unit_price=Decimal("8.00")),
            ]),
        }
        erp.accounts = [
            GLAccount(id="5100", number="5100", name="Cost of Goods - Materials", type="COGS"),
            GLAccount(id="6100", number="6100", name="Office Supplies", type="Expense"),
            GLAccount(id="6400", number="6400", name="Software & Cloud Subscriptions", type="Expense"),
            GLAccount(id="6500", number="6500", name="Professional Services", type="Expense"),
            GLAccount(id="6700", number="6700", name="Utilities", type="Expense"),
        ]
        erp.bills = {
            "B1": Bill(id="B1", vendor_id="V200", invoice_number="NW-2026-0042", invoice_date=date.today() - timedelta(days=20), due_date=date.today() + timedelta(days=10), total=Decimal("1800.00"), balance=Decimal("1800.00"), currency="USD", status=BillStatus.open),
        }
        return erp

    def search_vendors(self, *, name=None, tax_id=None, email=None):
        out = []
        for v in self.vendors.values():
            if tax_id and v.tax_id and tax_id.replace("-", "") == v.tax_id.replace("-", ""):
                out.append(v)
            elif email and (email.lower() == (v.email or "").lower() or email.split("@")[-1].lower() in v.email_domains):
                out.append(v)
            elif name and (name.lower() in v.name.lower() or v.name.lower() in name.lower()):
                out.append(v)
        return out

    def get_vendor(self, vendor_id):
        return self.vendors.get(vendor_id)

    def get_purchase_order(self, po_number):
        return self.pos.get(po_number)

    def find_bills(self, *, vendor_id, invoice_number=None):
        return [b for b in self.bills.values() if b.vendor_id == vendor_id and (invoice_number is None or b.invoice_number == invoice_number)]

    def list_open_bills(self, *, due_before=None):
        return [b for b in self.bills.values() if b.status in (BillStatus.open, BillStatus.partially_paid) and (due_before is None or (b.due_date and b.due_date <= due_before))]

    def list_gl_accounts(self):
        return list(self.accounts)

    def create_bill(self, draft: BillDraft) -> Bill:
        bill_id = f"B{next(self._ids)}"
        bill = Bill(id=bill_id, vendor_id=draft.vendor_id, invoice_number=draft.invoice_number, invoice_date=draft.invoice_date, due_date=draft.due_date, total=draft.total, balance=draft.total, currency=draft.currency, status=BillStatus.open, po_number=draft.po_number)
        self.bills[bill_id] = bill
        if draft.po_number and draft.po_number in self.pos:
            po = self.pos[draft.po_number]
            for line in draft.lines:
                for pl in po.lines:
                    if line.po_line_id == pl.line_id and line.quantity:
                        pl.quantity_billed += line.quantity
        return bill

    def record_payment(self, bill, amount, payment_date):
        stored = self.bills[bill.id]
        stored.balance = (stored.balance or stored.total) - amount
        stored.status = BillStatus.paid if stored.balance <= 0 else BillStatus.partially_paid
        pid = f"PMT{next(self._ids)}"
        self.payments.append({"id": pid, "bill_id": bill.id, "amount": str(amount), "date": payment_date.isoformat()})
        return pid
