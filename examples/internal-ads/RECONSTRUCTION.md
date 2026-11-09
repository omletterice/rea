# Adspirer reconstruction evidence

Investigated 2026-11-09. Scope: public documentation and publicly served marketing-page JavaScript; no sign-in, private API probing, credential acquisition or campaign mutation.

## What is established

| Question | Evidence and finding | Status |
| --- | --- | --- |
| Primary workflow | [How it works](https://www.adspirer.com/how-it-works): connect ad accounts, connect an AI assistant, delegate research/reporting/campaign preparation; review before launch. | Observed documentation, not independently exercised |
| Architecture | [Architecture](https://www.adspirer.com/docs/how-it-works): calling AI chooses tools; MCP service authenticates native platform API requests and returns results. Reasoning and brand context can live in the calling AI. | Observed documentation |
| Discovery | [Tool catalog](https://www.adspirer.com/docs/agent-skills/tools): compact top-level discovery and schema tools plus platform routers. Routed operations use exact discovered names and structured arguments. | Observed documentation |
| Campaign preparation | [Workflows](https://www.adspirer.com/docs/agent-skills/workflows): research, inspect existing assets, validate inputs, create paused, verify and review. Performance output includes spend, conversions, CPA, ROAS and CTR. | Observed documentation |
| Public frontend | Ordinary HTML inspection found Next.js chunk paths. Downloaded ten same-origin chunks (716,266 bytes total). Literal strings include `adspirer.ai`, public documentation links and `mcp.adspirer.com`; this is a marketing shell, not recovered backend source. | Observed static text; no runtime execution |
| Actual backend implementation | No authenticated MCP/tool calls or native platform requests observed. Database design, internal code, queue implementation, token lifetime by provider and actual behavior are unknown. | Unresolved |

The architecture docs describe OAuth, encrypted token storage, read/write distinctions and approval before spend-affecting changes. These are product statements, not a security audit. A reconstruction must implement and test its own enforcement. Do not copy the documentation's generalized access/refresh-token lifetimes as provider guarantees.

## REA execution

Repository investigated: `omletterice/rea` at `72d5e72e6d7b7997c4ecb11972fc405be8ec67d2`.

`npx -y rea-agents@latest --help` executed successfully and identified version `6.8.0`. No client configuration or native analysis engine was installed.

Actual static-analysis invocation (absolute paths below abbreviated to `<scratch>`):

```sh
npx -y rea-agents@latest analyze-javascript-application \
  <scratch>/adspirer-public-js \
  --source-path 05-c3ty_6dwfk.js \
  --source-path 20c3urj2vq69t.js \
  --source-path 3valq20rnu8ek.js \
  --detail summary \
  --evidence_output <scratch>/adspirer-rea-evidence.json \
  --analysis-timeout-ms 30000 --format json
```

Result: exit code 1, `capability_unavailable` / `unsupported_provider`. REA inventoried the downloaded directory but could not launch its semantic worker because the environment's `ps -axo pid=,ppid=,pgid=,uid=,stat=,command=` returned `fatal library error, lookup self`. No unchanged retry or substitute process-ownership shim was attempted.

Returned partial Evidence ID:

`ev_51868318562d7388278e292ee5000099e6b50b18d014b6f0deec40568d57ee38`

Directory identity SHA-256:

`269af62ed37e11a59577899d444921fc72c98570c2e27235ca24f5937d199810`

Downloaded HTML SHA-256:

`8db6ddf387b519facc12d15d3d1862ba260fefbce42c2276722b917db8faaf69`

The partial result explicitly marks incomplete graph coverage and seven unselected files. No claim in this report depends on recovered semantic relationships. URL/text inspection above used ordinary Python parsing and must not be called successful REA semantic analysis. Raw captures and full CLI output stayed in scratch; no third-party JavaScript is included in the service deliverable.

## Reconstruction decisions and limits

Implement a separately named internal service with one operation layer shared by REST, local stdio MCP and remote HTTP MCP. Keep account scopes, explicit currency units, durable draft/approval state and change receipts in the server. Use the host chat's model rather than charging for an unnecessary second conversational model. These are reconstruction design decisions, not claims about proprietary internals.

[Current product catalog](https://www.adspirer.com/docs/agent-skills/tools) advertises hundreds of operations across several networks. [Older developer introduction](https://developer.adspirer.com/introduction), dated March 2026, lists a smaller surface. Neither establishes clone parity. Advertise only implemented and tested operations; mark unimplemented networks explicitly. Provider permissions, app registration and live-account verification remain separate from mock/transport tests. Generic MCP support does not guarantee every chat application's connector authentication compatibility.

The published [Ads MCP repository](https://github.com/amekala/ads-mcp) was also inspected through GitHub. Its README points clients to the hosted service, and its recursively listed tree had no Python or TypeScript server files (828 entries at inspection). This bounded inventory does not recover hosted backend internals or establish that every source file is absent under every possible extension.
