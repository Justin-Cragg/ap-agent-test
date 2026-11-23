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
    environment: str = "dev"  # dev | staging | production (non-dev enforces auth & encryption)
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    public_base_url: str = "http://localhost:8080"
    company_name: str = "Your Company"
    ap_mailbox_address: str = ""
    # Bootstrap admin key (dev / first setup only). Prefer scoped API keys:
    # `ap-agent apikey create --name scheduler --scope jobs:run`.
    api_token: str = Field(default="", description="Bootstrap admin bearer token; disabled outside dev unless allow_bootstrap_token")
    allow_bootstrap_token: bool = False
    # Shared secret for push webhooks (Graph clientState, Pub/Sub push auth). Not an admin credential.
    webhook_secret: str = ""

    # ------------------------------------------------------------------ identity (humans)
    # none     - dev only: identity from the X-Dev-User header
    # oidc     - Bearer JWT from your IdP (Entra ID, Okta, Google, Auth0...) verified against JWKS
    # iap      - Google Cloud IAP signed header (x-goog-iap-jwt-assertion)
    # alb      - AWS ALB OIDC signed header (x-amzn-oidc-data)
    # easyauth - Azure Container Apps / App Service Easy Auth (X-MS-CLIENT-PRINCIPAL)
    auth_mode: Literal["none", "oidc", "iap", "alb", "easyauth"] = "none"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""  # default: <issuer>/.well-known/openid-configuration -> jwks_uri
    oidc_email_claim: str = "email"
    oidc_groups_claim: str = "groups"
    iap_audience: str = ""  # /projects/NUM/global/backendServices/ID
    alb_arn: str = ""  # expected signer of x-amzn-oidc-data
    rbac_file: Path | None = Path("config/rbac.yaml")
    entities_file: Path | None = Path("config/entities.yaml")
    rate_limit_per_minute: int = 120
    session_secret: str = ""  # HMAC key for CSRF tokens on browser forms

    # ------------------------------------------------------------------ secrets at rest
    # db     - refresh tokens etc. Fernet-encrypted in the database with TOKEN_ENCRYPTION_KEY
    # gcp | aws | azure - stored in Secret Manager / Secrets Manager / Key Vault
    secret_store: Literal["db", "gcp", "aws", "azure"] = "db"
    token_encryption_key: str = ""  # Fernet key: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
    gcp_project_id: str = ""
    azure_key_vault_url: str = ""
    bank_fingerprint_pepper: str = ""  # HMAC key for hashing vendor bank accounts

    # ------------------------------------------------------------------ processing
    payment_execution: Literal["inline", "separate"] = "separate"
    job_max_attempts: int = 5
    job_visibility_timeout_s: int = 1800
    retention_years: int = 7
    llm_inference_geo: str = ""  # Claude API only, e.g. "us"
    # USD per million tokens, for cost-per-invoice reporting
    llm_price_input: float = 4.0
    llm_price_output: float = 20.0
    llm_price_cache_read: float = 0.20
    llm_price_cache_write: float = 5.0
    otel_enabled: bool = False

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
    netsuite_subsidiary_id: str = ""

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


    def is_dev(self) -> bool:
        return self.environment == "dev"

    def validate_for_runtime(self) -> None:
        """Refuse insecure configurations outside dev."""
        if self.is_dev():
            return
        problems = []
        if self.auth_mode == "none":
            problems.append("AUTH_MODE=none is only allowed in dev")
        if self.secret_store == "db" and not self.token_encryption_key:
            problems.append("TOKEN_ENCRYPTION_KEY is required when SECRET_STORE=db")
        if not self.bank_fingerprint_pepper:
            problems.append("BANK_FINGERPRINT_PEPPER is required")
        if not self.session_secret:
            problems.append("SESSION_SECRET is required")
        if self.database_url.startswith("sqlite"):
            problems.append("Use PostgreSQL outside dev")
        if problems:
            raise RuntimeError("Insecure configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
