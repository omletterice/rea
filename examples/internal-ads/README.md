# Riality Ads

A self-hosted internal reconstruction of Adspirer's **connect → research → draft →
review → execute** workflow. Built in your REA fork as an independent example;
REA's CLI and existing analysis tools are unchanged. Your chat supplies the
model/reasoning. This service supplies authenticated platform tools and durable
state, so it works without paying for a second model or Adspirer subscription.

This is an initial implementation, **not feature parity with Adspirer's catalog**.
No live ad accounts were available during development. The automated tests use
simulated accounts/provider responses; native acceptance and permissions still
need verification with your accounts. [Reconstruction evidence](RECONSTRUCTION.md)
separates published behavior, actual REA output and unknown proprietary internals.

## What works

| Capability | Google | Meta | TikTok | LinkedIn | Amazon |
|---|---|---|---|---|---|
| Verify configured account/currency | Yes | Yes | Yes | Yes | Yes |
| Paginated campaign inventory | Yes | Yes | Yes | Yes | Sponsored Products |
| Campaign performance | Yes | Purchase conversions | Yes; ROAS unknown | Yes | Not implemented |
| Native keyword ideas / bid estimates | Yes | — | — | — | — |
| Complete paused campaign bundle | Search + RSA | Website Traffic + existing image | — | — | — |
| Preview/approve pause, resume, budget | Yes | Campaign budgets | Daily-budget campaigns | Daily-budget campaigns | SP daily budgets |
| Browser OAuth onboarding | Yes | Yes | Managed token | Managed token | Managed refresh token |

All connectors are implemented and provider-mocked, **not live-account verified**.
PMax, Shopping, app/video campaigns, bulk editing, creative upload, autonomous
scheduling, GA4/Klaviyo, and ChatGPT Ads are not implemented. No generic raw HTTP
or arbitrary platform-mutation tool bypasses the approval layer. The server can
be extended at `providers_*` and the shared application layer.

## Run locally

Requires Python 3.12+ on Linux/macOS (Unix process locking). From this directory:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python bootstrap.py --demo
.venv/bin/python -m internal_ads.server
```

Open **http://127.0.0.1:8787**. Copy `ADS_ADMIN_KEY` from your local `.env` into the
operator page. Demo accounts and metrics are visibly simulated; it never contacts
ad platforms. Writes start disabled even in demo. Set `ADS_ENABLE_WRITES=true` in
`.env` and restart to exercise draft/approval/execution in the simulation.

For live use, run `bootstrap.py` without `--demo` in a fresh checkout, or edit the
existing configuration explicitly. Set `ADS_DEMO=false`, use a **different database
path**, replace sample IDs, and add credentials. Demo/live databases cannot mix.
Do not run bootstrap against an existing `.env`: it refuses to overwrite keys.

## Configure accounts

`accounts.json` is an explicit allowlist and account policy. Add one object per
native account (see `accounts.example.json`). IDs are digits only: strip Google's
hyphens and Meta's `act_` prefix. `currency` must match the provider. `minor_units`
must match its currency units (INR/USD/EUR/GBP: 2; JPY: 0). No currency conversion
is performed. `daily_budget_limit` is the maximum **per-campaign daily budget**
accepted by this service; it is not an account-wide spend ceiling and cannot
prevent changes made directly in Ads Manager. Platforms can overspend pacing
budgets on individual days. Set native account spending limits separately.

Credentials use each account's `credentials_prefix`. Example `BINDEE_META`:

| Platform | Environment variables after the prefix | Access prerequisites |
|---|---|---|
| Google | `_CLIENT_ID`, `_CLIENT_SECRET`, `_DEVELOPER_TOKEN`; OAuth supplies access/refresh token. Or `_ACCESS_TOKEN` / `_REFRESH_TOKEN` | Your Google Cloud OAuth application, approved Ads developer token and authorized customer. Optional `login_customer_id` in account manifest for manager access. |
| Meta | `_CLIENT_ID`, `_CLIENT_SECRET` for browser OAuth, or `_ACCESS_TOKEN` | Your Meta app and ad-account permissions; `ads_read`, `ads_management`. Page/creative access is also needed for creation. |
| TikTok | `_ACCESS_TOKEN` | Approved Marketing API application and authorized advertiser. |
| LinkedIn | `_ACCESS_TOKEN` | Approved Advertising API access and authorized ad account. |
| Amazon | `_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN` (auto-refresh); or `_CLIENT_ID`, `_ACCESS_TOKEN` | Approved Advertising API application and authorized profile. Configure region `na`, `eu` or `fe`. |

Google/Meta: register this exact platform callback in your provider app:
`ADS_PUBLIC_URL/oauth/platform/callback`. Use **Connect OAuth** on the operator
page. The callback stores tokens encrypted with `ADS_ENCRYPTION_KEY`; Google
refresh and Amazon refresh are automatic. Meta, TikTok and LinkedIn tokens need
renewal according to the provider's actual expiration/revocation behavior. No
platform app registration or API approval can be inherited from Adspirer.

Default provider API versions: Google `v26`, Meta `v24.0`, TikTok Marketing `v1.3`,
LinkedIn `202610`, Amazon SP `v3`. Override Google/Meta/LinkedIn with
`ADS_GOOGLE_VERSION`, `ADS_META_VERSION`, `ADS_LINKEDIN_VERSION` after checking
provider compatibility. Changes invalidate old approvals. TikTok uses the still
published v1.3 reporting surface; migrating to v2 reporting is separate work.

## Connect an AI chat

Remote Streamable HTTP MCP endpoint: **`https://YOUR-HOST/mcp`**.

- For clients supporting OAuth discovery and dynamic registration, add that URL
  and complete this service's consent page. Enter the operator key only on your
  own service, not in the chat. OAuth grants tool access, never operation approval.
- For clients supporting custom headers, send `Authorization: Bearer ADS_API_KEY`.
- For clients that only support stdio, run the local transport below.
- A chat application must support custom MCP servers or API tools; no service can
  make an unsupported chat application execute tools. Hosted-client behavior has
  not been tested in ChatGPT/Claude directories or through vendor review.

Header-capable client example (insert the actual API key locally):

```json
{
  "mcpServers": {
    "riality-ads": {
      "type": "http",
      "url": "https://YOUR-HOST/mcp",
      "headers": {"Authorization": "Bearer YOUR_ADS_API_KEY"}
    }
  }
}
```

Stdio example (absolute paths; set cwd if your client supports it, otherwise set
absolute `ADS_ACCOUNTS_FILE` and `ADS_DATABASE` in its environment):

```json
{
  "mcpServers": {
    "riality-ads": {
      "command": "/absolute/path/examples/internal-ads/.venv/bin/python",
      "args": ["-m", "internal_ads.server", "stdio"],
      "cwd": "/absolute/path/examples/internal-ads"
    }
  }
}
```

The process reads local `.env`; never put the operator key in model context.
Run **one process per database**. Use HTTP clients against the running service
when you want shared state. Do not start a stdio process on the same database as
an HTTP server; a second process is rejected by an OS-held database lease. Separate
processes need separate databases.

Try: “List my connected accounts, verify Bindee's Meta account, then show last
week's spend, purchases, CPA and ROAS. Prepare a ₹500/day Traffic campaign using
this existing page/image, without launching it.”

MCP tools: `get_connections`, `verify_connection`, `list_campaigns`,
`get_performance`, `research_keywords`, `prepare_campaign`, `prepare_change`,
`get_operation`, `execute_operation`, `get_brand_context`, `save_brand_context`,
`get_audit_log`. One shared service owns both MCP and REST behavior.

## HTTP API and approval flow

`/docs` and `/openapi.json` provide generated schemas (bearer authentication).
All `/api/v1/*` routes accept the API key or authorized client token. `/admin/*`
requires the **distinct operator key**, which is never exposed as an MCP tool.
Use `ADS_API_KEY` for integrations and `ADS_ADMIN_KEY` only in the operator UI or
admin requests. Keys must be distinct and at least 32 characters.

Example request files in `requests/` are safe templates with placeholder asset
IDs. All currency amounts are decimal strings in the native account currency.

```sh
curl -H "Authorization: Bearer $ADS_API_KEY" http://127.0.0.1:8787/api/v1/connections
curl -X POST -H "Authorization: Bearer $ADS_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @requests/meta-traffic.json http://127.0.0.1:8787/api/v1/campaigns/prepare
```

1. Prepare returns an immutable plan, digest, ID, expiry and validation scope.
   Reuse its idempotency key for retries of the same intention. Google creation
   runs the native `validateOnly` check; Meta validation is local until execution.
2. Inspect the full preview on the operator page. Approve the exact digest with
   `POST /admin/operations/ID/approve`, body `{"expected_digest":"DIGEST"}`.
   Approval expires 15 minutes after preparation; it never refreshes on replay.
3. Execute from the UI or `POST /api/v1/operations/ID/execute`. A SQLite state
   transition claims it once. Repeat success returns its saved result.
4. New campaign parents are **PAUSED**, with enabled children behind them. A
   separate approved `resume` operation launches the reviewed bundle. Meta uses
   existing uploaded image hashes and page IDs; this service does not upload art.
5. On timeout, cancellation, interruption or partial multi-step creation, inspect
   `GET /api/v1/operations/ID` and native Ads Manager. Receipts survive restart.
   `needs_reconciliation` never auto-retries. After inspecting native objects,
   `POST /admin/operations/ID/reconcile` with a written `note` records resolution;
   it does not assert that a rollback occurred. The account is blocked from new
   preparations/executions until ambiguity is resolved. Create a fresh reviewed operation
   only after resolving any duplication. Pending/approved operations can cancel.

Brand context is persistent planning data, not executable instructions or a way
to change spend guardrails. Reports include attribution and coverage. Follow
`next_cursor` until complete; never sum different currencies. Missing conversion
tracking is `null`, not zero. LinkedIn can round older report periods; inspect the
provider date range. Google controls native page size, so the service returns a
whole provider page rather than silently dropping rows to enforce `limit`.

## Deploy to a server

From this directory after configuring `.env` and accounts:

```sh
docker compose up -d --build
```

The account manifest contains no credentials. Bootstrap makes it readable by the
container; for an existing manifest, run `chmod 644 accounts.json`. The private
`.env` stays mode 600 and is loaded by Compose.

The container runs as a non-root user, binds only to loopback, and keeps SQLite
and encrypted tokens in the `ads-data` volume. Back up the volume **and**
`ADS_ENCRYPTION_KEY`; losing the key makes saved tokens unreadable. Do not run
multiple workers/replicas against this database. For horizontal scaling, replace
SQLite and process-local account serialization with a shared transactional store.

Put an HTTPS reverse proxy in front of `127.0.0.1:8787`. Example Caddy block:

```caddy
ads.YOUR-DOMAIN.com {
    reverse_proxy 127.0.0.1:8787
}
```

Set `ADS_PUBLIC_URL=https://ads.YOUR-DOMAIN.com` and
`ADS_ALLOWED_HOSTS=ads.YOUR-DOMAIN.com,127.0.0.1,localhost`, restart, and update
provider callback URLs. MCP uses normal POST JSON responses; no sticky sessions
are needed. Uvicorn access logs are disabled so OAuth callback codes do not enter
its query logs; configure the reverse proxy to omit/redact query strings too.

No public service has been deployed by this commit and no ad account connected.
A hosting target/domain plus the provider app credentials are required to make
remote chats useful against real accounts. Infrastructure/ad-platform charges
are separate from this open-source service.

## Verify

```sh
.venv/bin/pip install '.[test]'
.venv/bin/python -m pytest -q
```

Tests cover actual REST and MCP SDK flows, OAuth PKCE/refresh, approval separation,
expired/tampered previews, budget/campaign drift, idempotency/concurrent execution,
restart/partial-write receipts and provider-specific payloads/identity/pagination.
Live-account validation, actual ad delivery, real attribution and hosted-client
OAuth compatibility remain unverified. Docker image construction is separate
from the Python transport and application tests.
