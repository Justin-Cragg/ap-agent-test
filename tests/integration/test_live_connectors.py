"""Live checks against ERP / mailbox *sandboxes*. Skipped unless credentials are
present. Read-only by default; set AP_IT_ALLOW_WRITES=1 to also post a test
bill (sandbox companies only).

    pytest -m integration tests/integration
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from ap_agent.config import Settings
from ap_agent.erp import build_erp
from ap_agent.models import BillDraft, BillLine
from ap_agent.storage.db import Database
from ap_agent.storage.secrets import DBSecretStore

pytestmark = pytest.mark.integration

REQUIRED = {
    "netsuite": ["NETSUITE_ACCOUNT_ID", "NETSUITE_CLIENT_ID", "NETSUITE_CERTIFICATE_ID", "NETSUITE_PRIVATE_KEY"],
    "dynamics_bc": ["DYNAMICS_TENANT_ID", "DYNAMICS_CLIENT_ID", "DYNAMICS_CLIENT_SECRET", "DYNAMICS_COMPANY_ID"],
    "quickbooks": ["QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_SECRET", "QUICKBOOKS_REALM_ID", "QUICKBOOKS_REFRESH_TOKEN"],
    "xero": ["XERO_CLIENT_ID", "XERO_CLIENT_SECRET"],
}


def _erp(provider, tmp_path):
    missing = [v for v in REQUIRED[provider] if not os.environ.get(v)]
    if missing:
        pytest.skip(f"{provider}: set {', '.join(missing)}")
    settings = Settings(erp_provider=provider, quickbooks_sandbox=True)
    db = Database(f"sqlite:///{tmp_path}/it.db")
    return build_erp(settings, DBSecretStore(db, "", allow_plaintext=True), "it")


@pytest.mark.parametrize("provider", list(REQUIRED))
def test_read_paths(provider, tmp_path):
    erp = _erp(provider, tmp_path)
    accounts = erp.list_gl_accounts()
    assert accounts, "no postable GL accounts returned"
    bills = erp.list_open_bills()
    if bills:
        b = bills[0]
        assert erp.get_vendor(b.vendor_id) is not None
        assert any(x.id == b.id for x in erp.find_bills(vendor_id=b.vendor_id))
    po_number = os.environ.get(f"AP_IT_{provider.upper()}_PO")
    if po_number:
        po = erp.get_purchase_order(po_number)
        assert po is not None and po.lines


@pytest.mark.parametrize("provider", list(REQUIRED))
def test_post_bill_idempotently(provider, tmp_path):
    if os.environ.get("AP_IT_ALLOW_WRITES") != "1":
        pytest.skip("set AP_IT_ALLOW_WRITES=1 (sandbox only)")
    erp = _erp(provider, tmp_path)
    vendor_id = os.environ[f"AP_IT_{provider.upper()}_VENDOR_ID"]
    gl = os.environ[f"AP_IT_{provider.upper()}_GL"]
    number = f"IT-{date.today():%Y%m%d}-{os.getpid()}"
    draft = BillDraft(vendor_id=vendor_id, invoice_number=number, invoice_date=date.today(), total=Decimal("1.00"), lines=[BillLine(description="integration test", amount=Decimal("1.00"), gl_account=gl)])
    first = erp.create_bill(draft, f"it-{number}")
    second = erp.create_bill(draft, f"it-{number}")
    assert first.id == second.id
    assert len(erp.find_bills(vendor_id=vendor_id, invoice_number=number)) == 1
