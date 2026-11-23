# AP Agent

An accounts-payable AI agent built on Claude. It reads the AP mailbox (Gmail or Outlook), processes supplier invoices end-to-end against your ERP (NetSuite, Dynamics 365 Business Central, QuickBooks Online or Xero), answers vendor inquiries and prepares payment runs. Financial controls are enforced in code, not left to the model.

It ships as one container image that runs unchanged on **Google Cloud, AWS or Azure**. Configuration picks the model provider, ERP and mailbox.

```
            ┌──────────── Gmail / Outlook (Graph) ────────────┐
            │                                                  │
   scheduler ─► ap-agent poll ─► extraction (structured output) ─► agent loop (Claude + tools)
 (Cloud Sched./                                                    │
  EventBridge/                         ┌───────── policy engine (code) ─────────┐
  ACA jobs)                            │ duplicates · 2/3-way match · approvals │
                                       └──────────────────┬──────────────────────┘
                                                          ▼
                              ERP adapter: NetSuite · Dynamics BC · QuickBooks · Xero
                                                          │
            approval UI (FastAPI) ◄── approvers ──────────┘     Postgres (state/audit) · S3/GCS/Blob (files)
```

## What it does

| AP function | How |
|---|---|
| **Invoice intake** | Polls the AP inbox and stores PDF/image attachments in object storage. Claude reads each document into a typed schema (vendor, number, dates, PO, lines, tax, totals, confidence). |
| **Vendor validation** | Looks the vendor up in the ERP by name, tax id and sender domain. A sender domain that doesn't match the vendor on file is treated as a fraud signal. |
| **Duplicate detection** | Deterministic. Normalized invoice numbers are checked against both ERP bills and the agent's own registry, and same amount within a time window counts as a "possible" duplicate. Exact duplicates are blocked. |
| **PO matching** | Deterministic 3-way match (invoice ↔ PO ↔ receipt) in NetSuite and Business Central. QuickBooks and Xero have no receipts, so they get a 2-way match. Price, quantity and total tolerances are configurable. |
| **GL coding** | Claude codes non-PO lines from the ERP's chart of accounts and the vendor's default account. |
| **Approval routing** | A tiered approval matrix by amount. Approvers get an email link to a confirmation page that shows the coding and the reasons approval is needed. |
| **Bill posting** | Bills are created in the ERP: NetSuite transforms the PO into a bill, Business Central creates and posts a purchase invoice, QuickBooks creates a bill with a `LinkedTxn` to the PO, Xero creates an `ACCPAY` invoice. |
| **Vendor inquiries** | Answers payment-status and statement questions from ERP data, as in-thread replies (drafts by default). |
| **Payment runs** | Claude proposes a run: what's due before the next run, plus early-payment discounts still open. **A human must approve the run before any payment is recorded in the ERP.** |
| **Fraud controls** | Requests to change bank or remittance details are always escalated. The agent has no tool that can edit vendor master data or bank details. |
| **Audit trail** | Every tool call, decision, approval and posting goes to `audit_log`. |

## Controls the model cannot override

Everything in `src/ap_agent/ap/policy.py` and `config/policy.yaml`:

- **Auto-post only when all of these hold:** the amount is under the limit, the invoice matched within tolerance (or the vendor is on the non-PO allowlist), extraction confidence is high enough, there is no possible duplicate, the currency agrees, and the email wasn't flagged. Anything else goes to approval.
- **Recomputed on submit.** The tool layer recomputes duplicates and the PO match itself, from the extracted document rather than from the model's coding. If the submitted total differs from the document, the invoice goes to approval.
- **Approvals are authenticated.** Each approval link carries a per-approval secret. GET requests never change state, so mail scanners can't approve anything. The approver must be on that tier's list, or you can enforce SSO identity with `IDENTITY_HEADER`.
- **Payments.** The agent can only *propose* a run. Recording payments in the ERP happens in code after approval.
- **Untrusted content.** Email text and attachments are fenced as untrusted data. Prompt-injected instructions can't reach a dangerous action, because no such tool exists.
- **Bank-detail changes.** A keyword control flags these before the model even runs.

## Model provider (per deployment)

| `LLM_PROVIDER` | Client | Auth | Typical host |
|---|---|---|---|
| `anthropic` | Claude API | `ANTHROPIC_API_KEY` | any |
| `bedrock` | Amazon Bedrock | task IAM role | AWS |
| `vertex` | Google Vertex AI | service account (ADC) | Google Cloud |
| `foundry` | Microsoft Foundry | managed identity or API key | Azure |

- **Default model:** `claude-opus-5-5`, with adaptive thinking, `high` effort for the agent loop and `medium` for extraction.
- **Prompt caching** is on.
- **Requests are streamed** so long PDFs don't hit HTTP timeouts.
- **Refusal fallbacks:** if a safety classifier declines a request, the Claude API retries it server-side on a fallback model (`fallbacks="default"`). On Bedrock, Vertex and Foundry the SDK's client-side fallback middleware does the same job, retrying on `LLM_FALLBACK_MODEL` (`claude-opus-5`). If the request is still declined, or the agent runs out of turns, the email is labelled `AP-Needs-Review` for a human. It is never treated as a silent success.

The agent loop runs on the Messages API inside your container. Anthropic's Managed Agents isn't used because it isn't available on Bedrock, Vertex or Foundry.

## Quick start (local, no cloud)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 33 tests, no network or API spend
cp .env.example .env         # set ANTHROPIC_API_KEY, API_TOKEN
ap-agent serve               # http://localhost:8080/healthz
```

`ERP_PROVIDER=memory` and `MAIL_PROVIDER=memory` give you an in-memory ERP with demo vendors, a PO and GL accounts. When you're ready, switch to a real ERP and mailbox through environment variables. Run `docker compose up` for the same setup with Postgres.

Run commands:

- `ap-agent poll` processes the inbox once. This is what the scheduler runs.
- `ap-agent payment-run --date 2026-12-01 --next-run 2026-12-08` proposes a payment run.

API, with bearer `API_TOKEN`:

- `POST /jobs/poll-mailbox`
- `POST /jobs/payment-run`
- `GET /invoices`, `/approvals`, `/audit`
- `/approvals/{id}/{approve|reject}`: the approver UI, authenticated by its link token
- `POST /webhooks/gmail` (Pub/Sub push) and `POST /webhooks/outlook` (Graph notifications, with `clientState` = `API_TOKEN`), for near-real-time processing

## Connecting an ERP

| ERP | `ERP_PROVIDER` | Setup |
|---|---|---|
| NetSuite | `netsuite` | Integration record with OAuth 2.0 client credentials (M2M). Upload a certificate and map it to a role with vendor, PO and vendor bill permissions. Set `NETSUITE_ACCOUNT_ID`, `_CLIENT_ID`, `_CERTIFICATE_ID`, `_PRIVATE_KEY` (PEM) and `_PAYMENT_ACCOUNT_ID`. |
| Dynamics 365 Business Central | `dynamics_bc` | Entra app registration with `API.ReadWrite.All` on Business Central, added under *Microsoft Entra Applications* in BC. Set the tenant, client id and secret, environment, company id and payment journal id. Payments are added as journal lines, and you post the journal in BC. |
| QuickBooks Online | `quickbooks` | Intuit app (Accounting scope). Do the consent flow once and set `QUICKBOOKS_REFRESH_TOKEN` and the realm id. Rotated refresh tokens are saved to the database automatically. |
| Xero | `xero` | Custom Connection (leave `XERO_REFRESH_TOKEN` empty), or a standard OAuth app with a refresh token and `XERO_TENANT_ID`. |

To add an ERP (SAP, Sage Intacct, Dynamics F&O and so on), implement `ERPConnector` in `src/ap_agent/erp/base.py`: eight methods with ERP-neutral models. Then register it in `build_erp`.

## Connecting a mailbox

- **Gmail** (`MAIL_PROVIDER=gmail`): either a Workspace service account with domain-wide delegation for `gmail.modify` (`GMAIL_SERVICE_ACCOUNT_JSON`, `GMAIL_USER`), or an OAuth client plus a refresh token for the AP mailbox.
- **Outlook** (`MAIL_PROVIDER=outlook`): an Entra app with the application permissions `Mail.ReadWrite` and `Mail.Send`. Use an Exchange application access policy to limit it to the AP mailbox.

Processed mail gets the label or category `AP-Processed`, and anything needing a person gets `AP-Needs-Review`. Replies are **drafts** by default (`MAIL_REPLY_MODE=draft`). Switch to `send` once you trust them.

## Deploying

Build and push the image (`docker build -t <registry>/ap-agent:TAG .`), then apply the module for your cloud:

| Cloud | Module | Runs as | Scheduler | Files | Secrets | Claude |
|---|---|---|---|---|---|---|
| Google Cloud | `deploy/terraform/gcp` | Cloud Run service + Cloud Run jobs | Cloud Scheduler | GCS | Secret Manager | Vertex AI |
| AWS | `deploy/terraform/aws` | ECS Fargate + ALB | EventBridge Scheduler → ECS task | S3 | Secrets Manager | Bedrock |
| Azure | `deploy/terraform/azure` | Container App + Container Apps jobs | job cron trigger | Blob Storage | Key Vault | Foundry |

- **Secrets:** create them out of band so their values never reach Terraform state, then map env var → secret id, ARN or URI with `secret_env`. Do the same for `DATABASE_URL`, pointing at Cloud SQL, RDS or Azure Database for PostgreSQL. The modules don't create a database.
- **Model provider:** each module defaults to that cloud's provider. You can still set `LLM_PROVIDER=anthropic` anywhere.
- **Approval UI and SSO:** put the approval UI behind your cloud's SSO and set `IDENTITY_HEADER`. The options are IAP (`X-Goog-Authenticated-User-Email`), ALB OIDC, or Easy Auth (`X-MS-CLIENT-PRINCIPAL-NAME`).
- **Azure:** also grant the managed identity *Cognitive Services User* on the Foundry resource.

## Project layout

```
src/ap_agent/
  config.py            env-driven settings
  models.py            ERP-neutral domain model
  llm/client.py        provider factory, streaming, refusal fallbacks, caching
  agent/               tool definitions (strict schemas), prompts, agent loop
  ap/                  extraction, matching, duplicates, policy engine
  erp/                 netsuite, dynamics (BC), quickbooks, xero, in-memory
  mail/                gmail, outlook (Graph), in-memory
  storage/             SQLAlchemy state + audit, blob stores (local/S3/GCS/Azure)
  pipeline.py          mailbox → extraction → agent; approvals; payment runs
  api.py / cli.py      FastAPI app, CLI entrypoints
config/policy.yaml     approval matrix, tolerances, limits
deploy/terraform/      gcp, aws, azure
```

## Status and known gaps

- **Tests:** the 33 tests cover the controls, the agent loop (with a scripted model), every connector's request and response shapes against mocked HTTP, provider selection and the approval API. **None of it has run against a live ERP, mailbox or cloud account yet.** Run each connector against a sandbox company before production, especially the NetSuite SuiteQL fields, which vary with account customisation.
- **Terraform:** written but not yet `terraform validate`d or applied.
- **Bank details:** detection is keyword- and document-based. Comparing against bank details stored in the ERP needs ERP-specific bank-record access, which isn't built yet.
- **Credit notes:** extracted and flagged, but not yet applied automatically against open bills.
- **Dynamics 365 Finance & Operations:** not covered. The Dynamics adapter is Business Central only.
