---
name: foundry-finops-reports
description: "Report on Microsoft Foundry spend and usage through the Foundry FinOps connector: billed cost by project, agent, model or team, token usage, cost-control and budget status, quota headroom, gateway token limits, and KQL over tracing data. Use for 'how much did we spend on Foundry', 'cost by team/project/agent', 'token usage this week', 'which cost controls are near their limit', 'FinOps report', 'chargeback', 'quota headroom'. Read-only; for changing budgets, cost controls, tags, capacity or token limits use foundry-finops-governance."
---

# Foundry FinOps reports

Answer questions about Microsoft Foundry cost and usage with the **Foundry FinOps** connector tools. Every call runs
with the signed-in user's own Azure permissions.

## Pick the tool

| Question | Tool | Key arguments |
|---|---|---|
| Headline numbers, "how are we doing" | `get_finops_overview` | `days` |
| Spend by project / agent / deployment / model / team | `get_cost_breakdown` | `group_by`, `days` |
| Tokens or requests over time | `get_token_usage` | `group_by`, `hours` |
| Real-time budgets that alert or block | `list_cost_controls` | – |
| Monthly budgets and forecast | `list_budgets` | – |
| Capacity, TPM, quota headroom | `get_quota_and_deployments` | – |
| Team / cost-centre tags | `list_tags` | – |
| Alert rules | `list_alert_rules` | – |
| Gateway products, keys' scopes, token limits | `get_gateway_overview` | – |
| Anything else in tracing or gateway logs | `list_log_tables_and_queries`, then `run_kql_query` | `query`, `timespan` |

Every tool also accepts `subscription_id`, `resource_group` and `foundry_account` when the user names a different
environment; otherwise the server defaults apply.

## Workflow

1. Resolve the time window from the user's words into `days` or `hours` (this month → days since the 1st).
2. Call the fewest tools that answer the question; run independent calls together.
3. Lead with the answer (total, top item, trend), then a compact table. Show money with currency, tokens in K/M/B.
4. Name the data source and its freshness in one line:
   - Billed cost (Cost Management): 8–24 hours behind usage.
   - Token metrics: near real time.
   - Cost-control usage: an estimate priced as requests run; not reconciled with the bill.
5. Offer one useful next step (a chart, an Excel export, a drill-down by agent, or a cost control for a hot spot).

## Rules

- Never invent numbers, names or prices. If a tool returns an error or an empty result, say so and name the gap.
- A tool error about access means the user lacks an Azure role on that scope (for example Cost Management Reader or
  Foundry User). Say which data was blocked; don't retry with another scope unless the user asks.
- Projects, deployments and agents in cost data come from Foundry's automatic billing tags; models from partners
  and Claude aren't tagged by project yet, so their cost can show as untagged.
- Keep KQL results small with `summarize`; never select raw prompt or response content.
- Don't evaluate or rank individual people. Team, project and agent reporting is fine.

See `references/report-recipes.md` for standard report layouts and `references/kql-cookbook.md` for queries.
