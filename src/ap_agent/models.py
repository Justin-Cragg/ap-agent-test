"""ERP-neutral domain model shared by every connector and by the agent."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class Vendor(BaseModel):
    id: str
    name: str
    tax_id: str | None = None
    email: str | None = None
    email_domains: list[str] = Field(default_factory=list)
    currency: str | None = None
    payment_terms: str | None = None
    active: bool = True
    default_gl_account: str | None = None
    is_1099: bool = False


class POLine(BaseModel):
    line_id: str
    item_code: str | None = None
    description: str = ""
    quantity_ordered: Decimal
    quantity_received: Decimal | None = None  # None = ERP has no receiving (2-way only)
    quantity_billed: Decimal = Decimal("0")
    unit_price: Decimal


class PurchaseOrder(BaseModel):
    id: str
    number: str
    vendor_id: str
    currency: str | None = None
    status: str | None = None
    lines: list[POLine] = Field(default_factory=list)

    @property
    def supports_receiving(self) -> bool:
        return any(line.quantity_received is not None for line in self.lines)


class GLAccount(BaseModel):
    id: str
    number: str
    name: str
    type: str | None = None


class BillStatus(str, Enum):
    draft = "draft"
    open = "open"
    partially_paid = "partially_paid"
    paid = "paid"
    void = "void"
    unknown = "unknown"


class Bill(BaseModel):
    id: str
    vendor_id: str
    invoice_number: str
    invoice_date: date | None = None
    due_date: date | None = None
    total: Decimal
    balance: Decimal | None = None
    currency: str | None = None
    status: BillStatus = BillStatus.unknown
    po_number: str | None = None


class BillLine(BaseModel):
    description: str
    amount: Decimal
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    gl_account: str | None = None  # ERP account id/number for expense lines
    po_line_id: str | None = None  # set for lines billed against a PO line
    item_code: str | None = None


class BillDraft(BaseModel):
    vendor_id: str
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    currency: str | None = None
    total: Decimal
    tax_total: Decimal = Decimal("0")
    po_id: str | None = None
    po_number: str | None = None
    lines: list[BillLine]
    memo: str | None = None
    subsidiary_id: str | None = None


# --------------------------------------------------------------------- extraction


class ExtractedLine(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = None
    amount: float
    item_code: str | None = None
    po_line_reference: str | None = None


class ExtractedInvoice(BaseModel):
    """What the extraction step reads off an invoice document."""

    is_invoice: bool = Field(description="False for statements, remittances, quotes, marketing etc.")
    document_type: str = Field(description="invoice, credit_note, statement, remittance, quote or other")
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    vendor_address: str | None = None
    remit_to_bank_details_present: bool = False
    remit_bank_account: str | None = Field(default=None, description="Bank account number or IBAN printed for payment, exactly as printed")
    remit_bank_routing: str | None = Field(default=None, description="Routing / sort code / BIC printed for payment")
    invoice_number: str | None = None
    invoice_date: str | None = Field(default=None, description="ISO 8601 date")
    due_date: str | None = Field(default=None, description="ISO 8601 date")
    po_number: str | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code")
    subtotal: float | None = None
    tax_total: float | None = None
    total: float | None = None
    payment_terms: str | None = None
    lines: list[ExtractedLine] = Field(default_factory=list)
    confidence: float = Field(description="0-1 overall confidence in the extraction")
    notes: str | None = Field(default=None, description="Anything unusual or illegible")
