# Foundry FinOps for Microsoft 365 Copilot (Cowork plugin + MCP connector)

Lets people ask Copilot Cowork things like *"What did each team spend on Foundry this month?"*,
*"Which cost controls are close to their limit?"* or *"Cap the fraud-risk agents at $500 a month"* – answered
from the same Azure REST APIs that the [`foundry-finops`](../foundry-finops) SDK wraps, **signed in as the user**.

| Folder | What it is |
|---|---|
| `mcp_server/` | Stateless Streamable-HTTP MCP server: Entra token validation, on-behalf-of exchange, 18 tools |
| `plugin/appPackage/` | Cowork plugin: manifest v1.28, connector, captured `tools/connector-tools.json`, two skills |
| `scripts/` | `capture_tools.py` (tools/list → tool file), `configure_plugin.py` (set URL + OAuth ID, bump, zip) |
| `docs/SETUP.md` | Entra app, deployment, Developer Portal OAuth registration, RBAC |
| `tests/` | Protocol, tool and auth tests (`python -m unittest`) |

## Tools

Read: `get_finops_overview`, `get_cost_breakdown`, `get_token_usage`, `list_cost_controls`, `list_budgets`,
`get_quota_and_deployments`, `list_tags`, `list_alert_rules`, `get_gateway_overview`,
`list_log_tables_and_queries`, `run_kql_query`.

Write (marked destructive so Copilot asks before running): `upsert_cost_control`, `delete_cost_control`,
`upsert_budget`, `delete_budget`, `set_deployment_capacity`, `set_tags`, `set_gateway_token_limit`.
`FINOPS_READ_ONLY=1` removes them from `tools/list`.

## Skills

* **foundry-finops-reports** – picks the right tool, states data freshness (billed cost 8–24 h behind; token
  metrics near real time; cost-control totals are estimates), standard report recipes and a KQL cookbook.
* **foundry-finops-governance** – read-first, show-the-diff, confirm, write, re-read; encodes the service rules
  (agent filter needs one project, budget type is immutable, alert ≤ block, 15-minute propagation…).

## Status

The package passes local Cowork validation as a **draft**. It becomes deployable after you deploy the server and
insert your OAuth client registration ID (`docs/SETUP.md`, steps 2–4).
