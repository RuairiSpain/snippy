# Changelog

## 0.1.0 – 2026-11-21
- First release: `FinOpsClient` with foundry, cost_controls, cost, metrics, logs, quota, tags, alerts, apim and
  pricing operation groups.
- Typed models with ARM round-tripping; `Table` with CSV/JSON/pandas export.
- Retry policy honouring `Retry-After`; cost-control blocks (`x-ms-budget-cause`) raise `BudgetExceededError`
  and are never retried.
- APIM `llm-token-limit` / `llm-emit-token-metric` builders and policy upsert.
- OpenTelemetry usage tracking (`finops_context`, `UsageTracker`) and KQL library.
- Reporting facade (`service`), illustrative demo data with a KQL-subset engine, and a CLI.
