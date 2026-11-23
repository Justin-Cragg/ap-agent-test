# Access model

Three kinds of actor touch the system. Each is authenticated differently and gets the smallest set of rights that works.

| Actor | Authenticates with | Gets |
|---|---|---|
| **People** (AP staff, approvers, auditors, admins) | Your identity provider through the configured `AUTH_MODE` (OIDC, IAP, ALB or Easy Auth). The app verifies the signed assertion itself. | Roles mapped from IdP groups in `config/rbac.yaml`, limited to specific legal entities |
| **Machines** (schedulers, integrations, monitoring) | Scoped API keys (`apk_…`), stored as SHA-256 hashes, revocable, with optional expiry | Only the scopes listed on the key. Financial-authority scopes can't be granted to keys. |
| **External senders** (vendors) | The email itself: the receiving server's aligned DMARC result | Nothing in the system. They can get an in-thread reply, which carries payment facts only if they're a registered contact of that vendor. |

## Roles

Defined in `src/ap_agent/auth.py`. The mapping from groups to roles is configuration; the permissions in each role are code.

| Permission | auditor | ap_clerk | ap_manager | controller | admin |
|---|:-:|:-:|:-:|:-:|:-:|
| `invoices:read`, `review:read`, `vendors:read` | ✓ | ✓ | ✓ | ✓ | |
| `audit:read` | ✓ | | | ✓ | ✓ |
| `reports:read` (shadow agreement, cost) | ✓ | | ✓ | ✓ | |
| `review:resolve`, `invoices:submit` (corrected coding) | | ✓ | ✓ | ✓ | |
| `ask` (internal Q&A) | | ✓ | ✓ | ✓ | |
| `approvals:decide` ¹ | | | ✓ | ✓ | |
| `vendors:contacts:write`, `delegations:write` | | | ✓ | ✓ | |
| `vendors:bank:verify` ², `fx:write`, `policy:approve` ³ | | | | ✓ | |
| `apikeys:manage`, `policy:propose`, `jobs:run`, `metrics:read` | | | | | ✓ |

1. **Approving also needs a named tier.** Holding `approvals:decide` isn't enough on its own. The approver must be named in the approval tier for that amount, or be in its `group:` entry, or hold an active delegation from someone who is. Payment runs, and invoices above `require_mfa_above`, also need an MFA-authenticated session.
2. **Bank verification:** requires MFA plus call-back evidence. The number called must come from an independent source, never from the invoice or email.
3. **Policy changes use a two-person rule.** One person proposes and a *different* person activates.

Platform admins have no financial authority. They can run the system but can't approve invoices or payments, verify bank accounts, or activate policy.

## Separation-of-duties rules (enforced in code)

- A person who submitted or corrected an invoice by hand can't approve it.
- A person who approved an invoice can't approve the payment run that pays it.
- A policy proposer can't activate their own proposal.
- The agent can't approve, pay or change vendor master data, because it has no tool for any of these.
- **Payments are recorded by a separate executor process** (`ap-agent execute-payments`) with its own cloud identity and its own ERP credentials. The agent's ERP user can't record payments.

## The agent's tools

The agent acts as the service identity, never on behalf of a person, and gets a different tool set for each task:

| Task | Tools | Writes |
|---|---|---|
| Email processing | `lookup_vendor`, `get_purchase_order`, `check_duplicate`, `match_invoice_to_po`, `list_gl_accounts`, `get_vendor_coding_history`, `submit_invoice`, `get_bill_status`, `reply_to_sender`, `flag_for_review` | **`submit_invoice`:** goes through the policy engine and can only queue a posting, request approval or block. **`reply_to_sender`:** one reply, to the sender only. **`flag_for_review`:** opens a review item. |
| Payment-run preparation | `list_open_bills`, `lookup_vendor`, `propose_payment_run` | **`propose_payment_run`:** creates an approval request. |
| Internal Q&A (`/ask`) | `lookup_vendor`, `get_purchase_order`, `search_invoices`, `get_vendor_bills`, `list_open_bills`, `ap_summary`, `get_vendor_coding_history` | None (read-only). Limited to entities the asker can see. |

Data scoping inside the tools:

- **Vendor scoping:** POs and matches are only returned for the vendor named in the call. Another vendor's PO reads as "not found".
- **Reply check:** every read of vendor transaction data is recorded. A free-text reply is refused unless the sender is DMARC-verified *and* is a registered contact for every vendor whose data was read. Only the fixed acknowledgement text is always allowed.
- **Bank details:** raw bank numbers are replaced with an HMAC fingerprint and the last 4 digits before anything is stored or shown to the model.

## ERP integration users (least privilege)

Create **two** integration users per ERP.

| ERP | Agent user | Payment-executor user |
|---|---|---|
| NetSuite | **Custom role:** Lists → Vendors (View), Accounts (View); Transactions → Purchase Order (View), Bills (Create/Edit), Vendor Credits (Create); SuiteQL / REST Web Services. **Exclude:** Vendor Payments and Bank fields. | Transactions → Pay Bills (Create), Bills (View) |
| Business Central | **Permission set:** Vendors, Purchase Orders and G/L Accounts read-only; Purchase Invoices and Purchase Credit Memos insert/modify/post | Vendor Payment Journal insert only. The journal is posted by people in BC. |
| QuickBooks Online | **No role model in the API.** Use a dedicated company user; the app's code has no payment path in the agent process. | Separate Intuit app/connection whose refresh token only the executor's secrets hold |
| Xero | **Custom Connection scopes:** `accounting.transactions accounting.contacts.read accounting.settings.read` | A separate connection, used only by the executor, for the payments endpoint |

## Mailboxes

- **Outlook:** run `deploy/scripts/restrict-outlook-app.ps1` so the app registration can only reach the AP mailboxes. Without it, `Mail.ReadWrite` covers the whole tenant.
- **Gmail:** domain-wide delegation is granted for `gmail.modify` only, and the service account is only ever used with `subject` set to the AP mailbox.

## Cloud identities

| Identity | Can |
|---|---|
| Agent workload identity | Call the model, read/write its bucket and its `ap-agent-*` secrets, read its config secrets |
| Executor identity | Read only the executor's secrets |
| Scheduler identity | Start the jobs |

Neither the agent nor the executor holds broad project or account roles.
