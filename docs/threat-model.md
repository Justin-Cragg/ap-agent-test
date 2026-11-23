# Threat model

**Assets:**
- The ERP: posting bills, recording payments.
- Vendor bank details.
- Vendor and payment data: who is owed what, and when.
- AP mailbox content.
- Credentials: ERP and mailbox tokens, model provider access.
- The audit trail.

**Main adversaries:**
- Business-email-compromise (BEC) fraudsters impersonating vendors or executives.
- Malicious or compromised vendors.
- Prompt injection embedded in emails and PDFs.
- Insiders.
- Attackers holding a stolen credential.

| # | Threat | Controls | Where |
|---|---|---|---|
| 1 | **Vendor bank-detail change fraud.** A forged email or invoice tells AP to pay a new account. | No tool can change vendor bank details. A keyword control flags bank-change emails before the model runs. Printed bank accounts are fingerprinted and must match an account verified by call-back (evidence required, independent number, controller + MFA) or one on the ERP vendor record; otherwise the invoice goes to approval and the review queue. | `ap/controls.py`, `pipeline.process_email`, `tools.submit_invoice` |
| 2 | **Sender spoofing.** A forged From address asks for payment status or remittance data. | Disclosure requires the receiving server's aligned DMARC pass. Only the topmost Authentication-Results header is trusted, and for Gmail its authserv-id must be Google's. The sender must also be the vendor's ERP contact or a contact registered by AP; domain-wide trust is opt-in. | `mail/base.dmarc_pass`, `ap/controls.sender_authorized_for` |
| 3 | **Prompt injection** in email or PDF ("ignore previous instructions, send X"). | Content is fenced as untrusted, and system facts sit outside the fence. The bigger defence is that dangerous actions don't exist as tools. Each task gets a minimal tool set. Replies are refused if the sender isn't authorised for every vendor whose data the task read, so injected requests can't leak another vendor's data. Submitted totals are checked against the document, and PO matching uses the extracted document, not the model's claims. | `agent/tools.py`, `tests/test_agent_flow.py::test_prompt_injection_*` |
| 4 | **Duplicate or inflated invoices.** | Deterministic duplicate detection (normalised numbers, amount and date window) across the ERP and the agent registry. 2-way/3-way match with tolerances. Totals are recomputed. Auto-post only under the limit and only in `live` mode after a shadow period. | `ap/duplicates.py`, `ap/matching.py`, `ap/policy.py` |
| 5 | **Unauthorised approvals** (link forwarding, mail scanners, CSRF, forged identity headers). | Approvals require a signed-in person with `approvals:decide` who is in the tier, in the group, or holds a delegation. The link token is an extra check, not the credential. GET requests never change state, browser forms carry CSRF tokens, and identity comes from verified IdP assertions (IAP/ALB JWTs are verified cryptographically). MFA is required for payments and large invoices. | `auth.py`, `api.py`, `pipeline.decide_approval` |
| 6 | **Insider fraud.** One person creates and pays a fake bill. | Separation of duties: a submitter can't approve, and an invoice approver can't approve the payment run that pays it. Policy changes need two people. Admins hold no financial permissions. The payment executor is a separate identity with separate ERP credentials. Every action goes into the hash-chained audit log. | `pipeline._check_sod`, `ap/policy.PolicyStore`, `docs/permissions.md` |
| 7 | **Credential theft.** | Secrets stay in the cloud secret manager. Rotating refresh tokens are kept in the secret manager or Fernet-encrypted. API keys are hashed, scoped, revocable and can expire. Financial scopes can't be put on keys. The app refuses to start outside dev with `AUTH_MODE=none`, without encryption keys, or on SQLite. | `storage/secrets.py`, `config.validate_for_runtime` |
| 8 | **Audit tampering.** | The audit log is append-only with a SHA-256 hash chain. `ap-agent audit-verify` and `/audit/verify` detect edited or deleted rows. Export it to WORM storage for regulatory retention. | `storage/db.py` |
| 9 | **Double posting or double payment on retries.** | An idempotency key goes on every ERP write: NetSuite `externalId`, QuickBooks `requestid`, Xero `Idempotency-Key`, and Business Central reconciliation by number. Jobs are claimed atomically. Payment results are saved after every payment. Non-idempotent POSTs aren't retried on a 5xx. | `erp/*`, `storage/db.Job`, `pipeline._job_*` |
| 10 | **Model failures** (refusals, truncation, runaway loops). | A refusal or `max_tokens` stop moves the task to the review queue. There's a turn cap. Tool inputs are validated by schema. Refusals fall back to another model (server-side on the Claude API, client-side middleware elsewhere). | `agent/runner.py`, `llm/client.py` |
| 11 | **Denial of service / abuse.** | Edge WAF and rate limits: Cloud Armor or AWS WAF. The Azure module has none at the edge (add Front Door WAF there). App-level per-principal rate limiting. Webhooks need a shared secret and only enqueue work. | Terraform modules, `api.py` |
| 12 | **Data exposure / retention.** | Entity-scoped access. Bank numbers are never stored in the clear. Files are purged after the retention period by the app (`ap-agent purge`) and by bucket lifecycle rules. On the Claude API, `LLM_INFERENCE_GEO` sets where inference runs; on other providers, region choice does. | `pipeline.purge_expired`, Terraform |

## Residual risks and required human work

- **Penetration test.** Commission a third-party pen test, including prompt-injection red-teaming against your real mail flow, before going live.
- **Shadow period.** Keep `auto_post_mode: shadow` until `/reports/shadow` shows an acceptable agreement rate on your own invoices.
- **Mailbox scope.** Restrict the Exchange app access policy (or the Gmail delegation) to the AP mailboxes.
- **Identity provider hygiene** is outside this app's control: group membership, MFA enforcement and joiner/mover/leaver processes.
- **Easy Auth (Azure) trusts the platform sidecar.** Don't expose the container through any other ingress.
