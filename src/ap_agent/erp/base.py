"""The ERP port. Every ERP adapter implements this interface; the agent and the
AP controls only ever see these ERP-neutral methods and models."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any, Callable

import httpx

from ap_agent.models import Bill, BillDraft, GLAccount, PurchaseOrder, Vendor


class ERPError(RuntimeError):
    pass


class ERPConnector(ABC):
    name: str = "erp"
    #: True when the ERP tracks goods receipts, enabling 3-way matching.
    supports_receiving: bool = False

    @abstractmethod
    def search_vendors(self, *, name: str | None = None, tax_id: str | None = None, email: str | None = None) -> list[Vendor]: ...

    @abstractmethod
    def get_vendor(self, vendor_id: str) -> Vendor | None: ...

    @abstractmethod
    def get_purchase_order(self, po_number: str) -> PurchaseOrder | None: ...

    @abstractmethod
    def find_bills(self, *, vendor_id: str, invoice_number: str | None = None) -> list[Bill]: ...

    @abstractmethod
    def list_open_bills(self, *, due_before: date | None = None) -> list[Bill]: ...

    @abstractmethod
    def list_gl_accounts(self) -> list[GLAccount]: ...

    @abstractmethod
    def create_bill(self, draft: BillDraft) -> Bill:
        """Create (and, where the ERP distinguishes, post/approve) a vendor bill."""

    @abstractmethod
    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date) -> str:
        """Record a payment against a bill; returns the ERP payment id. Only ever
        called after a human approved the payment run."""


class OAuthHTTP:
    """httpx wrapper with bearer-token caching and one retry on 401."""

    def __init__(self, base_url: str, token_fn: Callable[[], tuple[str, int]], headers: dict[str, str] | None = None, transport: httpx.BaseTransport | None = None):
        self._token_fn = token_fn
        self._token: str | None = None
        self._expires = 0.0
        self.http = httpx.Client(base_url=base_url, timeout=60, headers=headers or {}, transport=transport)

    def _bearer(self, force: bool = False) -> str:
        if force or not self._token or time.time() > self._expires - 60:
            self._token, ttl = self._token_fn()
            self._expires = time.time() + ttl
        return self._token

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        for attempt in (0, 1):
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self._bearer(force=attempt == 1)}"}
            resp = self.http.request(method, url, headers=headers, **kw)
            if resp.status_code != 401:
                break
        if resp.status_code >= 400:
            raise ERPError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw).json()

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)


def dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value)
    if text.startswith("/Date("):  # Xero "/Date(1700000000000+0000)/"
        from datetime import datetime, timezone

        ms = int(text[6:].split("+")[0].split("-")[0].rstrip(")/"))
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    return date.fromisoformat(text[:10])
