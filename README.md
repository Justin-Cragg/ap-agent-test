# AP Agent

An accounts-payable AI agent built on Claude. It reads AP mailboxes (Gmail or Outlook), processes supplier invoices and credit notes end-to-end against your ERP (NetSuite, Dynamics 365 Business Central, QuickBooks Online or Xero), answers vendor inquiries, prepares payment runs, and answers internal staff questions. Financial controls, permissions and data scoping are enforced in code, not left to the model.

It ships as one container image that runs unchanged on **Google Cloud, AWS or Azure**. Configuration picks the model provider, ERP, mailbox and identity provider.

```
 AP mailboxes ──poll/push──► job queue (Postgres) ──► extraction ──► agent loop (Claude + scoped tools)
                                   │                                     │
                                   │                       policy engine (code): duplicates · 2/3-way match ·
                                   │                       FX · tax · bank verification · approvals · shadow mode
                                   ▼                                     ▼
 IdP (SSO) ─► approval / review UI · /ask ──► approvals (RBAC, SoD, MFA) ──► post_invoice job ──► ERP
                                                     └─► payment run ──► payment executor (separate identity) ──► ERP
 Hash-chained audit log · encrypted token store · Prometheus metrics · JSON logs · alerts
```

## What it does

| AP function | How |
|---|---|
| **Intake** | Polls each AP mailbox, or handles push notifications, into a deduplicated job queue. Emails are routed to a legal entity by the AP alias they were sent to. |
| **Extraction** | Claude reads PDF and image invoices into a typed schema. Printed bank numbers are replaced with an HMAC fingerprint and the last 4 digits before anything is stored or shown to the model. |
| **Vendor validation** | Searches the ERP by name, tax id and sender domain. A mismatched sender is treated as a fraud signal. |
| **Duplicates** | Deterministic checks across ERP bills and the agent's registry, per entity and document type. |
| **PO matching** | 3-way (invoice ↔ PO ↔ receipt) in NetSuite and Business Central; 2-way in QuickBooks and Xero. Matching runs on the document's data, not on the model's coding. |
| **GL coding** | Claude codes lines, guided by vendor defaults and **coding history, including approver corrections** (a feedback loop). |
| **Multi-entity / FX / tax** | Each entity has its own ERP connection, functional currency and policy overrides. Approval thresholds apply after FX conversion (ERP rates, or rates entered by controllers). Tax-rate sanity checks. Missing W-9 / TIN flagged for 1099 vendors. |
| **Approvals** | Tiered by amount, with group approvers, delegation, MFA for payments and large invoices, and separation of duties. Approvers can correct GL coding in the approval screen. |
| **Posting** | Idempotent ERP writes from a retrying job (NetSuite `externalId`, QuickBooks `requestid`, Xero `Idempotency-Key`, Business Central reconciliation). Credit notes become vendor credits. |
| **Vendor inquiries** | Status replies go only to DMARC-verified contacts registered for that vendor. Everyone else gets a fixed acknowledgement. |
| **Payment runs** | The agent proposes a run (due dates, early-payment discounts, partial amounts). After approval, a **separate executor process with its own ERP credentials** records the payments. |
| **Internal Q&A** | `/ask`: a read-only assistant over invoices, bills, aging and approvals, limited to the entities the user can see. |
| **Review queue** | Fraud flags, bank-detail changes, disputes, unknown entities, refusals and failed jobs land in `/review`. Clerks resolve items or resubmit corrected coding. |
| **Audit** | Every decision, approval, posting, reply and admin change goes into an append-only, hash-chained log (`/audit/verify`). |

## Access control (summary)

Full detail in [docs/permissions.md](docs/permissions.md).

- **People** sign in through your IdP. Set `AUTH_MODE` to `oidc` (Entra ID, Okta, Google, Auth0), `iap` (GCP), `alb` (AWS) or `easyauth` (Azure). The app verifies the signed assertion itself; a plain identity header is never trusted.
- **Roles** come from IdP groups mapped in `config/rbac.yaml`: `auditor`, `ap_clerk`, `ap_manager`, `controller`, `admin`. Each mapping also limits which entities the person can see. Admins have no financial authority.
- **Machines** use scoped, hashed, revocable API keys (`ap-agent apikey create --name scheduler --scope jobs:run`). Financial-authority scopes can't be granted to keys.
- **The agent's tools** differ by task (email, payment-run or Q&A) and enforce scoping themselves:
  - POs and bills are scoped per vendor.
  - Free-text replies are blocked unless the sender is a verified contact of every vendor whose data the task read.
  - There's no tool that approves, pays, or edits vendor or bank data.
- **Separation-of-duties rules:**
  - A submitter can't approve their own invoice.
  - An invoice approver can't approve the payment run that pays it.
  - Policy changes need two people: a proposer and a different approver.

Threats and the controls against them: [docs/threat-model.md](docs/threat-model.md).

## Model provider (per deployment)

| `LLM_PROVIDER` | Client | Auth | Typical host |
|---|---|---|---|
| `anthropic` | Claude API | `ANTHROPIC_API_KEY` | any |
| `bedrock` | Amazon Bedrock | task IAM role | AWS |
| `vertex` | Google Vertex AI | service account (ADC) | Google Cloud |
| `foundry` | Microsoft Foundry | managed identity or API key | Azure |

- **Default model:** `claude-opus-5-5`, with adaptive thinking, `high` effort for agent tasks and `medium` for extraction.
- **Prompt caching** is on, and requests are streamed.
- **Refusal fallbacks:** a declined request is retried on a fallback model, server-side on the Claude API and through the SDK's client-side middleware elsewhere. Anything still declined goes to the review queue.
- **Data residency:** on the Claude API, `LLM_INFERENCE_GEO` sets where inference runs.
- **Cost tracking:** every task records token usage and estimated cost (`/reports/costs`, Prometheus `ap_llm_cost_usd_total`).

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                        # 73 tests, no network or API spend
cp .env.example .env          # set ANTHROPIC_API_KEY
ap-agent serve                # dev mode: identity via X-Dev-User / X-Dev-Groups headers
curl -H "X-Dev-User: me@example.com" -H "X-Dev-Groups: ap-clerks" localhost:8080/me
```

`ERP_PROVIDER=memory` and `MAIL_PROVIDER=memory` give you a demo ERP and mailbox. `docker compose up` runs the same setup with Postgres.

### Commands

| Command | What it does |
|---|---|
| `ap-agent migrate` | Applies Alembic migrations. Advisory-locked, so it's safe on every start. |
| `ap-agent poll` | Enqueues new mail and processes the queue. Runs on a schedule. |
| `ap-agent work` | Drains the job queue. |
| `ap-agent payment-run [--entity US01 --date … --next-run …]` | Proposes a payment run for approval. |
| `ap-agent execute-payments` | Payment executor. Deploy it with its own identity and ERP credentials. |
| `ap-agent apikey create\|revoke` | Manages scoped API keys. |
| `ap-agent audit-verify` | Checks the audit hash chain. |
| `ap-agent purge` | Deletes stored documents and extracted data past `RETENTION_YEARS`. |

### HTTP endpoints

| Area | Endpoints |
|---|---|
| UI | `/approvals/{id}`, `/ui/review`, `/ask` |
| API | `/me`, `/approvals`, `/api/approvals/{id}`, `/invoices`, `/invoices/{id}/submit`, `/review`, `/vendors/{entity}/{vendor}/contacts`, `/vendors/{entity}/{vendor}/bank-accounts`, `/fx-rates`, `/delegations`, `/policy`, `/policy/proposals`, `/apikeys`, `/audit`, `/audit/verify`, `/reports/shadow`, `/reports/costs`, `/metrics` |
| Jobs | `/jobs/poll`, `/jobs/work`, `/jobs/payment-run`, `/jobs/purge` |
| Webhooks | `/webhooks/gmail?token=…` and `/webhooks/outlook` (`clientState` = `WEBHOOK_SECRET`) |

## Rolling out safely

1. **Start in shadow mode** (`auto_post_mode: shadow`, the default). Every invoice goes to a human, and the approval records what the policy *would* have auto-posted. Watch `/reports/shadow` for the agreement rate.
2. **Measure extraction accuracy** on your own labelled invoices with `python evals/run_extraction_eval.py`. It is billed and asks for confirmation first.
3. **Run the live sandbox tests:** `pytest -m integration` (read-only, or `AP_IT_ALLOW_WRITES=1` on sandbox companies).
4. **Switch to live auto-posting.** Propose a policy change setting `auto_post_mode: live`; a controller approves it, per the two-person rule.

## Connecting an ERP

| ERP | `ERP_PROVIDER` | Notes |
|---|---|---|
| NetSuite | `netsuite` | OAuth 2.0 M2M (certificate). `NETSUITE_SUBSIDIARY_ID` per entity. Writes carry `externalId`. |
| Dynamics 365 Business Central | `dynamics_bc` | Entra app; one entity per BC company. Payments are added as journal lines, and you post the journal in BC. |
| QuickBooks Online | `quickbooks` | OAuth refresh token (rotated tokens are saved to the secret store, per entity). `requestid` makes writes idempotent. |
| Xero | `xero` | Custom Connection, or OAuth with a refresh token. `Idempotency-Key` on writes. |

Least-privilege integration users (agent vs payment executor) are described in [docs/permissions.md](docs/permissions.md). For multiple entities, copy `config/entities.example.yaml` to `config/entities.yaml`.

## Connecting a mailbox

- **Gmail:** a Workspace service account with domain-wide delegation for `gmail.modify`, or an OAuth refresh token.
- **Outlook:** an Entra app with `Mail.ReadWrite` and `Mail.Send`. Restrict it to the AP mailboxes with `deploy/scripts/restrict-outlook-app.ps1`.

Sender authenticity comes from the receiving server's DMARC result. Replies are drafts by default (`MAIL_REPLY_MODE=draft`).

## Deploying

| Cloud | Module | SSO and edge protection | Runs as | Scheduler | Files / secrets | Claude |
|---|---|---|---|---|---|---|
| Google Cloud | `deploy/terraform/gcp` | HTTPS LB + IAP + Cloud Armor (WAF, rate limiting); Cloud Run accepts LB traffic only | Cloud Run service + jobs | Cloud Scheduler | GCS / Secret Manager | Vertex AI |
| AWS | `deploy/terraform/aws` | ALB `authenticate-oidc` + WAF (managed rules, rate limiting); tasks accept ALB traffic only | ECS Fargate service + tasks | EventBridge Scheduler | S3 / Secrets Manager | Bedrock |
| Azure | `deploy/terraform/azure` | Container Apps Easy Auth (Entra ID); optional IP allow-list | Container App + jobs | job cron | Blob / Key Vault | Foundry |

Each module creates:

- separate identities for the agent and the payment executor, with separate secret sets (`secret_env` / `executor_secret_env`);
- poll, payment-run, purge and payment-executor jobs;
- storage lifecycle rules matching the retention period;
- error alerts to `alert_email`.

You supply secrets out of band: `DATABASE_URL` (managed Postgres), `SESSION_SECRET`, `WEBHOOK_SECRET`, `BANK_FINGERPRINT_PEPPER`, and the ERP and mailbox credentials. Outside dev, the app refuses to start without auth, encryption keys or Postgres.

CI (`.github/workflows/ci.yml`) runs:

- lint and tests;
- `pip-audit`;
- `terraform fmt` and `terraform validate` for all three modules;
- a container build with a Trivy scan;
- on `main`: push to GHCR with SBOM/provenance and a keyless cosign signature.

## Status and known gaps

- **Tests:** the 73 tests cover RBAC, SoD, MFA, delegation, DMARC/disclosure scoping, prompt-injection attempts, bank verification, shadow mode, FX, credit notes, idempotent posting, queue retries and dead-lettering, the audit chain, the policy two-person rule, IdP token verification, multi-entity routing, connectors against mocked HTTP, and the API. None of it has run against live ERPs, mailboxes or cloud accounts. Run `pytest -m integration` against sandboxes and let CI validate the Terraform; it hasn't been validated or applied here.
- **Field names:** some ERP API field names come from the documentation and need confirming on your tenant: NetSuite SuiteQL fields and `currencyrate`, BC `purchaseCreditMemos` fields, and the Bedrock IAM action names.
- **Not in the code:** a third-party penetration test, SOC 2 evidence collection, IdP hygiene (MFA enforcement, joiner/mover/leaver processes), and WORM export of the audit log. These are organisational steps; see the threat model.
- **Not yet built:**
  - managed Postgres provisioning in the Terraform modules;
  - an Azure edge WAF (Front Door);
  - Teams or Slack front-ends for `/ask`;
  - Dynamics 365 Finance & Operations;
  - automatic application of credit notes against open bills (credits are posted as vendor credits).
