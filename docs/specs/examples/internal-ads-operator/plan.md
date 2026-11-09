# Implementation plan

1. Investigate published behavior using REA's evidence workflow; record sources,
   observations, design decisions and unknowns in the example.
2. Implement a separate Python service with official MCP SDK, FastAPI, SQLite,
   encrypted OAuth token storage and environment-secret fallback.
3. Route both adapters to one service. Read accounts from an explicit manifest;
   bind previews to account policy, credentials mode and current platform state.
4. Require admin approval outside model tools. Claim each approved operation once
   before network I/O. Preserve receipts and require reconciliation after ambiguity.
5. Add provider adapters, explicit capability table, examples and Docker config.
6. Exercise HTTP and both MCP transports with real SDK clients and provider mocks.
   Publish a reviewable branch/PR; never attest to human readiness or live testing.
