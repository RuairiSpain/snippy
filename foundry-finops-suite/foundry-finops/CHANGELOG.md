# Changelog

## Unreleased – review fixes
**Security / integration**
- Credentials can no longer be sent to a non-ARM host: absolute URLs and `..` in caller-supplied scopes are
  refused; user-supplied names are percent-encoded as single path segments; OData filter values are escaped.
- `DefaultAzureCredential` now uses the sovereign-cloud authority for `US_GOV_CLOUD` / `CHINA_CLOUD`.
- APIM subscription scopes (`/products/x`, `/apis`) are expanded to the full resource ID the service expects.
- Dashboard: Host-header allow-list (DNS rebinding), body-size and Content-Length handling, socket timeout.
- MCP server: tool arguments are validated against `inputSchema` server-side; per-request Azure clients are
  closed (socket leak); `Origin` check when auth is off; 403 challenges use `insufficient_scope`;
  malformed JSON-RPC/Content-Length no longer kills a request thread; credential cache is bounded and keyed by hash.

**Correctness**
- Long-running operations finish on `204`/`200` from a `Location` poll (deleting a deployment used to wait 10 min).
- Deployment create/capacity changes wait for a terminal `provisioningState`.
- One automatic retry on `401` with a fresh token; token-acquisition failures raise `AuthenticationError`;
  `Retry-After` HTTP dates and the Cost Management rate-limit headers are honoured; paging loops are detected.
- `CostQuery.last_days` returns whole calendar days (the first day was almost entirely dropped).
- `create_or_update_export` no longer fails on 29 February; `datetime.utcnow()` removed.
- Cost-control status compares absolute thresholds as a percentage of the amount.
- Price lookup no longer maps `gpt-4o-mini` to `gpt-4o`; cached tokens are clamped to input tokens.
- Budgets are validated client-side (name, amount, contact required, max 5 per type); cost controls reject
  Quarterly/Annually; unknown service enum values no longer break listing.
- CLI: never silently falls back to demo data without `--demo`; works for `cost-controls`/`tokens` without an
  explicit account; friendly errors and exit codes; CSV for list results.
- Live overview degrades per section (`warnings`) instead of failing; forecast uses daily granularity;
  unsupported `group_by` values are rejected instead of mislabelled.

**Follow-up improvements**
- Cost controls report an estimated `usage` (with `usage_note`) per reset window: Cost budgets from billed cost
  filtered by the control's project/deployment/agent tags, Tokens budgets from Azure Monitor metrics. Controls that
  can't be reproduced (per-session, project/agent token budgets) stay "Usage unknown" with a reason.
- `CostControl` supports several alert thresholds (`extra_alerts`, `extra_blocks`); reading and re-saving a control
  no longer drops any. The MCP tool takes `alert_thresholds_pct`.
- Alerts reuse the Event Grid connection of an existing cost control when none is given.
- APIM writes use the ETag they read (`If-Match: <etag>`) instead of `*`, so a concurrent edit yields a conflict
  instead of being overwritten; creates send no `If-Match`.
- Publishing is refused while `pyproject.toml` still contains `<your-org>`; `configure_plugin.py` warns about
  placeholder manifest URLs.

**Tooling**
- `hatchling>=1.27` (required for SPDX `license` + `license-files`); ruff and mypy now pass and mypy gates CI.
- `scripts/configure_plugin.py` builds the package before bumping the manifest version.
- Cowork Dockerfile builds from the suite root so the local SDK is installed; adds a health check.

## 0.1.0 – 2026-11-21
- First release: `FinOpsClient` with foundry, cost_controls, cost, metrics, logs, quota, tags, alerts, apim and
  pricing operation groups.
- Typed models with ARM round-tripping; `Table` with CSV/JSON/pandas export.
- Retry policy honouring `Retry-After`; cost-control blocks (`x-ms-budget-cause`) raise `BudgetExceededError`
  and are never retried.
- APIM `llm-token-limit` / `llm-emit-token-metric` builders and policy upsert.
- OpenTelemetry usage tracking (`finops_context`, `UsageTracker`) and KQL library.
- Reporting facade (`service`), illustrative demo data with a KQL-subset engine, and a CLI.
