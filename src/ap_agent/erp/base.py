"""The ERP port. Every ERP adapter implements this interface; the agent and the
AP controls only ever see these ERP-neutral methods and models."""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any, Callable

import httpx

from ap_agent.models import Bill, BillDraft, GLAccount, PurchaseOrder, Vendor

log = logging.getLogger(__name__)


class ERPError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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
    def create_bill(self, draft: BillDraft, idempotency_key: str) -> Bill:
        """Create (and, where the ERP distinguishes, post/approve) a vendor bill.
        ``idempotency_key`` is stable across retries of the same posting."""

    @abstractmethod
    def create_credit(self, draft: BillDraft, idempotency_key: str) -> Bill:
        """Create a vendor credit (credit note). Amounts in the draft are positive."""

    @abstractmethod
    def record_payment(self, bill: Bill, amount: Decimal, payment_date: date, idempotency_key: str) -> str:
        """Record a payment against a bill; returns the ERP payment id. Only ever
        called by the payment executor after a human approved the run."""

    def vendor_bank_accounts(self, vendor_id: str) -> list[str] | None:
        """Account numbers / IBANs on the vendor record, or None if the ERP API
        doesn't expose them (then only call-back-verified accounts count)."""
        return None

    def exchange_rate(self, from_ccy: str, to_ccy: str, on: date) -> Decimal | None:
        """ERP exchange rate (1 from_ccy = rate to_ccy), or None if unavailable."""
        return None


RETRY_STATUSES = {429, 502, 503, 504}


class OAuthHTTP:
    """httpx wrapper: bearer-token caching, one re-auth on 401, and retries with
    backoff on rate limits / transient errors.

    POSTs are retried on 429 (the request was rejected, not processed) but only
    retried on 5xx when the caller marks them idempotent."""

    def __init__(self, base_url: str, token_fn: Callable[[], tuple[str, int]], headers: dict[str, str] | None = None, transport: httpx.BaseTransport | None = None, max_attempts: int = 5, sleep: Callable[[float], None] = time.sleep):
        self._token_fn = token_fn
        self._token: str | None = None
        self._expires = 0.0
        self.max_attempts = max_attempts
        self._sleep = sleep
        self.http = httpx.Client(base_url=base_url, timeout=60, headers=headers or {}, transport=transport)

    def _bearer(self, force: bool = False) -> str:
        if force or not self._token or time.time() > self._expires - 60:
            self._token, ttl = self._token_fn()
            self._expires = time.time() + ttl
        return self._token

    def request(self, method: str, url: str, *, idempotent: bool | None = None, **kw: Any) -> httpx.Response:
        safe = method in ("GET", "HEAD") if idempotent is None else idempotent
        extra_headers = kw.pop("headers", {})
        reauthed = False
        for attempt in range(1, self.max_attempts + 1):
            headers = {**extra_headers, "Authorization": f"Bearer {self._bearer()}"}
            try:
                resp = self.http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as e:
                if not safe or attempt == self.max_attempts:
                    raise ERPError(f"{method} {url} transport error: {e}") from e
                self._sleep(self._backoff(attempt, None))
                continue
            if resp.status_code == 401 and not reauthed:
                self._bearer(force=True)
                reauthed = True
                continue
            retryable = resp.status_code == 429 or (resp.status_code in RETRY_STATUSES and safe)
            if retryable and attempt < self.max_attempts:
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.info("%s %s -> %s, retrying in %.1fs", method, url, resp.status_code, delay)
                self._sleep(delay)
                continue
            break
        if resp.status_code >= 400:
            raise ERPError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}", resp.status_code)
        return resp

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, float(retry_after))
            except ValueError:
                pass
        return min(30.0, 0.5 * 2 ** attempt) * (0.5 + random.random() / 2)

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


def normalize_account(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())
