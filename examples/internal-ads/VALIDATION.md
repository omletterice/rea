# Validation record

Development environment: Python 3.12.14, Node 24.22.1, Linux.

| Check | Observed result |
|---|---|
| `python -m pytest -q` | **47 passed**. REST approval workflows, immutable/idempotent execution, policy/state drift, encrypted credentials, OAuth PKCE/refresh rotation, crash receipts, cross-process database ownership, provider payloads and pagination. |
| Actual MCP transports | Real SDK initialize/list/call over uvicorn TCP and a separate stdio subprocess passed. Twelve tools, no model-facing approval tool. These are automated tests, not only hand-authored JSON envelopes. |
| Google/Meta create → resume | Actual adapter code through shared service, native replies mocked: no mutation during preview, paused creation, saved receipts and separately approved resume passed. |
| Extra connector payloads | TikTok, LinkedIn and Amazon request/identity/pagination checks passed using httpx MockTransport. |
| `ruff check internal_ads bootstrap.py tests` | Passed. |
| `ruff format --check internal_ads bootstrap.py tests` | Passed. |
| `pip check` | No broken requirements. |
| `python -m build --wheel` | Built distributable wheel including operator HTML. |
| Operator JavaScript | Node syntax check passed; HTML served through tested HTTP app. |
| Browser visual/interaction QA | **Not completed**: no browser binary installed; Playwright download returned an invalid/truncated archive. No screenshot or browser-interaction success is claimed. |
| Docker build | **Not executed locally**: no Docker daemon/CLI available. Scoped GitHub CI includes image construction; do not assume that CI has run/passed until its result is available. |
| Existing REA runtime tests | Not run: no REA runtime/contracts changed. New example is independent of Node/REA build. |
| Live providers | **Not verified**: no user ad-platform credentials, approved applications or live account access were available. Mock acceptance does not prove native acceptance, delivery, attribution or permissions. |
| Hosted chat clients | **Not verified**: custom MCP OAuth discovery/PKCE is exercised locally; no ChatGPT/Claude marketplace publication or hosted client connection occurred. |

One known test warning: installed Starlette deprecates its httpx TestClient adapter
in favor of httpx2. The tested adapter still works; the real SDK socket/subprocess
tests additionally verify both production transports.

The service starts with writes disabled. Generated private `.env`, configured
account file, databases, lock files, captures and build artifacts are excluded
from the change. API tokens cannot approve operations. Uncertain writes block
replacement intents until an operator records reconciliation.
