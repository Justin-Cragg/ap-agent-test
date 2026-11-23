"""ERP adapters. Add a new ERP by implementing ``ERPConnector`` and registering
it in ``build_erp``."""

from __future__ import annotations

from ap_agent.config import Settings
from ap_agent.erp.base import ERPConnector, ERPError
from ap_agent.storage.secrets import SecretStore


def build_erp(settings: Settings, secrets: SecretStore, entity: str = "default") -> ERPConnector:
    """Build the ERP connector for one entity. Rotating tokens are namespaced by entity."""
    p = settings.erp_provider

    def token_get(key: str) -> str | None:
        return secrets.get(f"{entity}.{key}")

    def token_set(key: str, value: str) -> None:
        secrets.set(f"{entity}.{key}", value)

    if p == "memory":
        from ap_agent.erp.memory import InMemoryERP

        return InMemoryERP.with_demo_data()
    if p == "netsuite":
        from ap_agent.erp.netsuite import NetSuiteERP

        return NetSuiteERP(settings.netsuite_account_id, settings.netsuite_client_id, settings.netsuite_certificate_id, settings.netsuite_private_key, settings.netsuite_payment_account_id)
    if p == "dynamics_bc":
        from ap_agent.erp.dynamics import DynamicsBCERP

        return DynamicsBCERP(settings.dynamics_tenant_id, settings.dynamics_client_id, settings.dynamics_client_secret, settings.dynamics_environment, settings.dynamics_company_id, settings.dynamics_payment_journal_id)
    if p == "quickbooks":
        from ap_agent.erp.quickbooks import QuickBooksERP

        return QuickBooksERP(settings.quickbooks_client_id, settings.quickbooks_client_secret, settings.quickbooks_realm_id, settings.quickbooks_refresh_token, token_get, token_set, sandbox=settings.quickbooks_sandbox, bank_account_id=settings.quickbooks_bank_account_id)
    if p == "xero":
        from ap_agent.erp.xero import XeroERP

        return XeroERP(settings.xero_client_id, settings.xero_client_secret, settings.xero_tenant_id, settings.xero_refresh_token, token_get, token_set, bank_account_code=settings.xero_bank_account_code)
    raise ValueError(f"Unknown erp_provider {p!r}")


__all__ = ["ERPConnector", "ERPError", "build_erp"]
