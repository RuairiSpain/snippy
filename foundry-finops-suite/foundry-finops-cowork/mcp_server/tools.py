"""MCP tool catalogue. Each tool maps to one foundry-finops service call.

Read tools carry ``readOnlyHint: true``; write tools carry ``destructiveHint`` so the host asks the
user to confirm before running them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from foundry_finops.service import FinOpsService

MAX_ROWS = 400

SCOPE_PROPS: dict[str, Any] = {
    "subscription_id": {"type": "string", "description": "Azure subscription ID. Defaults to the server's configured subscription."},
    "resource_group": {"type": "string", "description": "Resource group that holds the Foundry resource. Defaults to the server's configured group."},
    "foundry_account": {"type": "string", "description": "Foundry resource (account) name. Defaults to the server's configured resource."},
}
GROUP_BY = ["project", "agent", "deployment", "model", "team"]


@dataclass
class Tool:
    name: str
    title: str
    description: str
    properties: dict[str, Any]
    handler: Callable[[FinOpsService, dict[str, Any]], Any]
    required: tuple[str, ...] = ()
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {**self.properties, **SCOPE_PROPS},
                **({"required": list(self.required)} if self.required else {}),
                "additionalProperties": False,
            },
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
        }


def _table(t: dict[str, Any]) -> dict[str, Any]:
    """Return tabular data as records, capped so the model's context stays small."""
    cols = [c["name"] for c in t.get("columns", [])]
    rows = t.get("rows", [])
    out = {"columns": cols, "row_count": len(rows), "records": [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]}
    if len(rows) > MAX_ROWS:
        out["truncated"] = f"Showing the first {MAX_ROWS} of {len(rows)} rows – narrow the time range or group by fewer keys."
    if t.get("render"):
        out["render"] = t["render"]
    return out


def _summarise_cost(t: dict[str, Any], key: str) -> dict[str, Any]:
    cols = [c["name"] for c in t["columns"]]
    totals: dict[str, float] = {}
    for r in t["rows"]:
        rec = dict(zip(cols, r))
        totals[str(rec.get(key))] = totals.get(str(rec.get(key)), 0) + float(rec.get("Cost") or 0)
    grand = sum(totals.values()) or 1
    return {
        "total": round(sum(totals.values()), 2),
        "by_" + key: [{"name": k, "cost": round(v, 2), "share_pct": round(v / grand * 100, 1)}
                      for k, v in sorted(totals.items(), key=lambda x: -x[1])],
        "daily": _table(t),
    }


TOOLS: list[Tool] = [
    Tool("get_finops_overview", "FinOps overview",
         "Headline Foundry spend and usage: billed spend month to date, month-end forecast, tokens and requests in the "
         "last 24 hours, cost controls needing attention, budgets, highest quota use, and the top projects, agents and "
         "models by spend. Billed cost lags usage by 8–24 hours; token counts are near real time.",
         {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 30,
                   "description": "Look-back window in days for the top-N rankings."}},
         lambda s, a: s.overview(int(a.get("days", 30)))),
    Tool("get_cost_breakdown", "Cost breakdown",
         "Billed cost from Azure Cost Management grouped by project, agent, deployment, model or team, with totals, share "
         "of spend and a daily series. Project, deployment and agent use the billing tags Foundry adds to model charges; "
         "team uses the configured team tag.",
         {"group_by": {"type": "string", "enum": GROUP_BY, "default": "project"},
          "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}},
         lambda s, a: _summarise_cost(s.cost_series(a.get("group_by", "project"), int(a.get("days", 30))),
                                      a.get("group_by", "project"))),
    Tool("get_token_usage", "Token usage",
         "Input, output and total tokens plus request counts over time, split by deployment or model (Azure Monitor "
         "metrics, near real time). In demo mode project, agent and team splits are also available.",
         {"group_by": {"type": "string", "enum": GROUP_BY, "default": "deployment"},
          "hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 168}},
         lambda s, a: _table(s.token_series(a.get("group_by", "deployment"), int(a.get("hours", 168))))),
    Tool("list_cost_controls", "List cost controls",
         "Foundry cost controls (preview) on the Foundry resource and its projects: budget type and amount, reset period, "
         "what each applies to, alert/block thresholds, estimated usage against the limit and state (On track, Near "
         "threshold, Limit reached).",
         {}, lambda s, a: s.list_cost_controls()),
    Tool("upsert_cost_control", "Create or update a cost control",
         "Create or replace a Foundry cost control that alerts (Event Grid) or blocks requests when spending (USD) or "
         "tokens reach a threshold. Blocked requests get HTTP 429 with x-ms-budget-cause: budget_exceeded. Takes effect "
         "within about 15 minutes. The budget type can't be changed after creation.",
         {"name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,63}$"},
          "project": {"type": "string", "description": "Create the cost control inside this project; omit for the Foundry resource."},
          "category": {"type": "string", "enum": ["Cost", "Tokens"], "default": "Cost"},
          "amount": {"type": "number", "exclusiveMinimum": 0, "description": "USD for Cost, whole tokens for Tokens."},
          "time_grain": {"type": "string", "enum": ["Daily", "Weekly", "Monthly"], "default": "Monthly"},
          "start_date": {"type": "string", "description": "UTC date (YYYY-MM-DD), today or later."},
          "end_date": {"type": "string", "description": "UTC date (YYYY-MM-DD)."},
          "filters": {"type": "object", "description": "Filter name → list of names. Keys: project, deployment, agent, sessionId, tokenType (input/output). An agent filter needs exactly one project.",
                      "additionalProperties": {"type": "array", "items": {"type": "string"}}},
          "sharing": {"type": "string", "enum": ["shared", "agent", "sessionId", "project"], "default": "shared"},
          "alert_threshold_pct": {"type": "number", "minimum": 1, "maximum": 100},
          "event_grid_connection_id": {"type": "string", "description": "Foundry connection resource ID for alerts."},
          "block_threshold_pct": {"type": "number", "minimum": 1, "maximum": 100}},
         lambda s, a: s.upsert_cost_control({
             **{k: a.get(k) for k in ("name", "project", "category", "amount", "time_grain", "start_date", "end_date",
                                      "filters", "sharing")},
             "alert": {"threshold": a["alert_threshold_pct"], "event_grid_connection_id": a.get("event_grid_connection_id")}
             if a.get("alert_threshold_pct") else None,
             "block": {"threshold": a["block_threshold_pct"]} if a.get("block_threshold_pct") else None}),
         required=("name", "amount"), read_only=False, destructive=True),
    Tool("delete_cost_control", "Delete a cost control",
         "Delete a Foundry cost control. Its alerts and blocks stop.",
         {"name": {"type": "string"}, "project": {"type": "string"}},
         lambda s, a: s.delete_cost_control(a["name"], a.get("project")) or {"deleted": a["name"]},
         required=("name",), read_only=False, destructive=True),
    Tool("list_budgets", "List budgets",
         "Azure Cost Management budgets in scope with amount, current and forecast spend, percent used, filters and alert "
         "conditions. Budgets alert on billed cost and can't block requests.",
         {}, lambda s, a: s.list_budgets()),
    Tool("upsert_budget", "Create or update a budget",
         "Create or replace an Azure Cost Management budget with email alerts at actual and forecasted thresholds.",
         {"name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,63}$"},
          "amount": {"type": "number", "exclusiveMinimum": 0},
          "time_grain": {"type": "string", "enum": ["Monthly", "Quarterly", "Annually"], "default": "Monthly"},
          "start_date": {"type": "string", "description": "First day of a month, YYYY-MM-DD."},
          "end_date": {"type": "string"},
          "tag_filter": {"type": "object", "description": "Optional tag filter, e.g. {\"name\":\"team\",\"values\":[\"Support\"]}.",
                         "properties": {"name": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}}},
          "actual_alert_pct": {"type": "number", "default": 80},
          "forecast_alert_pct": {"type": "number", "default": 100},
          "contact_emails": {"type": "array", "items": {"type": "string"}}},
         lambda s, a: s.upsert_budget({
             "name": a["name"], "amount": a["amount"], "time_grain": a.get("time_grain", "Monthly"),
             "start_date": a.get("start_date"), "end_date": a.get("end_date"),
             "filter": {"tags": {"name": a["tag_filter"]["name"], "operator": "In", "values": a["tag_filter"]["values"]}}
             if a.get("tag_filter") else None,
             "notifications": [n for n in (
                 {"threshold": a.get("actual_alert_pct", 80), "threshold_type": "Actual", "contact_emails": a.get("contact_emails", [])},
                 {"threshold": a.get("forecast_alert_pct", 100), "threshold_type": "Forecasted", "contact_emails": a.get("contact_emails", [])},
             ) if n["threshold"]]}),
         required=("name", "amount"), read_only=False, destructive=True),
    Tool("delete_budget", "Delete a budget", "Delete an Azure Cost Management budget.",
         {"name": {"type": "string"}}, lambda s, a: s.delete_budget(a["name"]) or {"deleted": a["name"]},
         required=("name",), read_only=False, destructive=True),
    Tool("get_quota_and_deployments", "Quota and deployments",
         "Regional model quota (thousands of tokens per minute) used and available, and each model deployment's type, "
         "capacity and TPM.",
         {}, lambda s, a: {"quota": s.list_quota(), "deployments": s.list_deployments()}),
    Tool("set_deployment_capacity", "Change deployment capacity",
         "Change a model deployment's capacity (its share of regional quota, in thousands of TPM for Standard types).",
         {"deployment": {"type": "string"}, "capacity": {"type": "integer", "minimum": 1}},
         lambda s, a: s.set_capacity(a["deployment"], int(a["capacity"])),
         required=("deployment", "capacity"), read_only=False, destructive=True),
    Tool("list_tags", "List tags",
         "Tags (team, cost centre, owner, env) on the resource group, Foundry resource, projects and AI gateway.",
         {}, lambda s, a: s.list_tags()),
    Tool("set_tags", "Replace tags",
         "Replace all tags on one Azure scope (resource ID). Pass the full set you want to keep.",
         {"scope": {"type": "string", "description": "Full Azure resource ID."},
          "tags": {"type": "object", "additionalProperties": {"type": "string"}}},
         lambda s, a: s.set_tags(a["scope"], dict(a.get("tags") or {})),
         required=("scope", "tags"), read_only=False, destructive=True),
    Tool("list_alert_rules", "List alert rules",
         "Azure Monitor metric alerts on token metrics, log search alerts (KQL) and the action groups they notify.",
         {}, lambda s, a: s.list_alerts()),
    Tool("get_gateway_overview", "AI gateway overview",
         "Azure API Management AI-gateway products (teams), subscriptions and llm-token-limit policies (tokens per minute "
         "and token quota per period).",
         {}, lambda s, a: s.apim_overview()),
    Tool("set_gateway_token_limit", "Set gateway token limit",
         "Add or replace an llm-token-limit policy at a scope: 'global', or 'product:' followed by the API Management product ID (for example 'product:team-risk'). Over the TPM limit callers get "
         "429; over the quota they get 403.",
         {"scope": {"type": "string"}, "tokens_per_minute": {"type": "integer", "minimum": 1},
          "token_quota": {"type": "integer", "minimum": 1},
          "token_quota_period": {"type": "string", "enum": ["Hourly", "Daily", "Weekly", "Monthly", "Yearly"]},
          "counter_key": {"type": "string", "default": "@(context.Subscription.Id)"}},
         lambda s, a: s.set_token_limit(a["scope"], {k: v for k, v in a.items() if k != "scope"}),
         required=("scope",), read_only=False, destructive=True),
    Tool("list_log_tables_and_queries", "Log tables and example queries",
         "Tables and columns available to run_kql_query, plus ready-made KQL examples for token usage and cost by model, "
         "agent and team.",
         {}, lambda s, a: {"tables": s.schema(), "examples": s.saved_queries()}),
    Tool("run_kql_query", "Run a KQL query",
         "Run a read-only KQL query against the configured Application Insights / Log Analytics data (tracing, APIM token "
         "metrics, usage records). Call list_log_tables_and_queries first to see tables and examples. Use summarize to "
         "keep results small.",
         {"query": {"type": "string"}, "timespan": {"type": "string", "default": "P7D",
                                                    "description": "ISO-8601 duration, e.g. PT24H, P7D, P30D."}},
         lambda s, a: _table(s.kql(a["query"], a.get("timespan", "P7D"))), required=("query",)),
]

BY_NAME = {t.name: t for t in TOOLS}


def call(service: FinOpsService, name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"Unknown tool: {name}")
    result = tool.handler(service, args)
    return {"content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "structuredContent": result if isinstance(result, dict) else {"items": result},
            "isError": False}
