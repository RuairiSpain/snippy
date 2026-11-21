# Report recipes

## Monthly FinOps summary
1. `get_finops_overview` (days = days elapsed this month).
2. `get_cost_breakdown` group_by=project, then group_by=model.
3. `list_cost_controls` and `list_budgets`.
Output: KPI line (spend MTD, forecast, % of budget), table of projects (cost, share), table of models,
cost controls that are Near threshold or Limit reached, budgets forecast to exceed 100 %.

## Team chargeback
1. `get_cost_breakdown` group_by=team for the billing month (days = month length).
2. If much cost is "(untagged)", call `list_tags` and list the resources missing a team tag.
Output: team, cost, share %, plus the untagged amount and how to fix it (tag the project or Foundry resource,
turn on tag inheritance).

## Spike investigation
1. `get_token_usage` hours=48 group_by=deployment – find the deployment and hour of the jump.
2. `get_cost_breakdown` days=7 group_by=agent – find the agent behind it (billed cost may lag).
3. Optional `run_kql_query` on tracing data for the agent and window.
Output: when it started, which deployment/agent, size versus the previous 7-day average, and a suggested guardrail
(a per-agent cost control or a gateway token limit).

## Quota headroom
`get_quota_and_deployments` → quota rows above 75 % used and deployments on those models, with capacity and TPM.
