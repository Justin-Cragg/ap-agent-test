"""Runtime configuration.

Everything is read from environment variables (optionally a `.env` file). This is
the cloud-agnostic contract: Cloud Run, ECS/Fargate, App Runner, Azure Container
Apps and Kubernetes all inject secrets from their native secret managers
(Secret Manager, Secrets Manager, Key Vault) as environment variables, so the
application never has to know which cloud it runs on.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ general
    environment: str = "dev"
    log_level: str = "INFO"
    api_token: str = Field(default="", description="Bearer token for the admin/approval API")
    public_base_url: str = "http://localhost:8080"
    # When the approval UI sits behind SSO (Google IAP, AWS ALB OIDC, Azure
    # Easy Auth), name the header carrying the signed-in user's email, e.g.
    # X-Goog-Authenticated-User-Email or X-MS-CLIENT-PRINCIPAL-NAME.
    identity_header: str = ""
    company_name: str = "Your Company"
    ap_mailbox_address: str = ""

    # ------------------------------------------------------------------ LLM
    # anthropic = Claude API, bedrock = Amazon Bedrock, vertex = Google Vertex AI,
    # foundry = Microsoft Foundry (Azure)
    llm_provider: Literal["anthropic", "bedrock", "vertex", "foundry"] = "anthropic"
    llm_model: str = "claude-opus-5-5"
    # Model used when the primary model declines a request (refusal). On the Claude
    # API the server picks it ("default"); elsewhere the SDK middleware uses this.
    llm_fallback_model: str = "claude-opus-5"
    llm_agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_extraction_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 64000
    llm_eager_input_streaming: bool = True
    llm_max_agent_turns: int = 30

    anthropic_api_key: str = ""
    aws_region: str = "us-east-1"
    vertex_project_id: str = ""
    vertex_region: str = "global"
    foundry_resource: str = ""
    foundry_api_key: str = ""

    # ------------------------------------------------------------------ storage
    database_url: str = "sqlite:///./data/ap_agent.db"
    blob_backend: Literal["local", "s3", "gcs", "azure"] = "local"
    blob_bucket: str = "./data/blobs"  # bucket / container name, or local directory
    azure_storage_account_url: str = ""

    # ------------------------------------------------------------------ ERP
    erp_provider: Literal["netsuite", "dynamics_bc", "quickbooks", "xero", "memory"] = "memory"

    netsuite_account_id: str = ""
    netsuite_client_id: str = ""
    netsuite_certificate_id: str = ""
    netsuite_private_key: str = ""  # PEM contents
    netsuite_payment_account_id: str = ""

    dynamics_tenant_id: str = ""
    dynamics_client_id: str = ""
    dynamics_client_secret: str = ""
    dynamics_environment: str = "production"
    dynamics_company_id: str = ""
    dynamics_payment_journal_id: str = ""

    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_realm_id: str = ""
    quickbooks_refresh_token: str = ""  # initial token; rotated tokens are persisted
    quickbooks_sandbox: bool = False
    quickbooks_bank_account_id: str = ""

    xero_client_id: str = ""
    xero_client_secret: str = ""
    xero_tenant_id: str = ""
    xero_refresh_token: str = ""  # leave empty to use a Custom Connection (client credentials)
    xero_bank_account_code: str = ""

    # ------------------------------------------------------------------ mailbox
    mail_provider: Literal["gmail", "outlook", "memory"] = "memory"
    mail_reply_mode: Literal["draft", "send"] = "draft"
    mail_lookback_days: int = 7

    gmail_user: str = ""  # mailbox to act on
    gmail_service_account_json: str = ""  # JSON contents (domain-wide delegation)
    gmail_client_id: str = ""  # or: OAuth installed-app credentials
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""

    outlook_tenant_id: str = ""
    outlook_client_id: str = ""
    outlook_client_secret: str = ""
    outlook_mailbox: str = ""

    # ------------------------------------------------------------------ AP policy
    policy_file: Path | None = Path("config/policy.yaml")


@lru_cache
def get_settings() -> Settings:
    return Settings()
