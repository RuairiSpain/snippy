"""Deterministic, clearly illustrative demo data shaped like the real APIs' outputs."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

SUB = "00000000-0000-0000-0000-000000000000"
RG = "rg-ai-demo"
ACCOUNT = "contoso-foundry"
APIM = "contoso-ai-gateway"
ACCOUNT_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.CognitiveServices/accounts/{ACCOUNT}"
APIM_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ApiManagement/service/{APIM}"

#: Illustrative USD prices per 1M tokens – NOT real prices.
PRICES = {
    "gpt-5.4": (2.50, 10.00),
    "gpt-5.4-mini": (0.25, 2.00),
    "o4-mini": (1.10, 4.40),
    "text-embedding-3-large": (0.13, 0.0),
    "model-router": (0.40, 3.00),
}

PROJECTS: dict[str, dict[str, Any]] = {
    "fraud-risk": {"team": "Risk", "agents": ["fraud-case-review", "aml-screening"], "weight": 1.6},
    "customer-care": {"team": "Support", "agents": ["care-triage", "refund-assistant", "kb-answers"], "weight": 2.2},
    "marketing-gen": {"team": "Marketing", "agents": ["campaign-writer"], "weight": 0.9},
    "dev-sandbox": {"team": "Platform", "agents": ["eval-harness"], "weight": 0.4},
}
DEPLOYMENTS = {
    "gpt-5.4": ("gpt-5.4", "GlobalStandard", 450),
    "gpt-5.4-mini": ("gpt-5.4-mini", "GlobalStandard", 1500),
    "o4-mini": ("o4-mini", "DataZoneStandard", 300),
    "text-embedding-3-large": ("text-embedding-3-large", "Standard", 350),
    "model-router": ("model-router", "GlobalStandard", 250),
}
AGENT_MODEL = {
    "fraud-case-review": "gpt-5.4", "aml-screening": "o4-mini", "care-triage": "gpt-5.4-mini",
    "refund-assistant": "gpt-5.4-mini", "kb-answers": "model-router", "campaign-writer": "gpt-5.4",
    "eval-harness": "gpt-5.4-mini",
}


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoStore:
    """In-memory state for demo mode. Writes (create/update/delete) mutate this store."""

    def __init__(self, seed: int = 42, now: datetime | None = None) -> None:
        self.rng = random.Random(seed)
        self.now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
        self.token_usage = self._tokens()
        self.cost_daily = self._costs()
        self.cost_controls = self._cost_controls()
        self.budgets = self._budgets()
        self.tags = self._tags()
        self.deployments = self._deployments()
        self.quota = self._quota()
        self.metric_alerts, self.log_alerts, self.action_groups = self._alerts()
        self.apim_products, self.apim_subscriptions, self.apim_policies = self._apim()
        self.alert_events = self._alert_events()

    # ---------------------------------------------------------------- usage
    def _tokens(self) -> list[dict[str, Any]]:
        rows = []
        start = self.now - timedelta(days=14)
        hours = 14 * 24
        for h in range(hours):
            ts = start + timedelta(hours=h)
            diurnal = 0.35 + 0.65 * max(0.0, 1 - abs(((ts.hour + 1) % 24) - 13) / 9)
            weekday = 0.45 if ts.weekday() >= 5 else 1.0
            growth = 0.8 + 0.4 * h / hours
            for project, meta in PROJECTS.items():
                for agent in meta["agents"]:
                    model = AGENT_MODEL[agent]
                    reqs = int(self.rng.gauss(40, 8) * meta["weight"] * diurnal * weekday * growth)
                    if reqs <= 0:
                        continue
                    inp = reqs * self.rng.randint(900, 2600)
                    out = reqs * self.rng.randint(150, 700)
                    cached = int(inp * self.rng.uniform(0.05, 0.35))
                    if project == "marketing-gen" and h > hours - 30:  # a spike to find in the charts
                        inp, out = inp * 3, out * 4
                    pin, pout = PRICES[model]
                    rows.append({
                        "TimeGenerated": _iso(ts), "Project": project, "Agent": agent, "Team": meta["team"],
                        "Deployment": model, "Model": model, "Requests": reqs, "InputTokens": inp,
                        "OutputTokens": out, "CachedTokens": cached,
                        "EstimatedCost": round(((inp - cached) * pin + cached * pin * 0.5 + out * pout) / 1e6, 4),
                    })
            # embeddings used by customer-care RAG
            reqs = int(120 * diurnal * weekday)
            if reqs:
                inp = reqs * 800
                rows.append({
                    "TimeGenerated": _iso(ts), "Project": "customer-care", "Agent": "kb-indexer", "Team": "Support",
                    "Deployment": "text-embedding-3-large", "Model": "text-embedding-3-large", "Requests": reqs,
                    "InputTokens": inp, "OutputTokens": 0, "CachedTokens": 0,
                    "EstimatedCost": round(inp * 0.13 / 1e6, 4),
                })
        return rows

    def _costs(self) -> list[dict[str, Any]]:
        """Daily billed cost for 60 days (scaled from token usage shape)."""
        rows = []
        for d in range(60, 0, -1):
            day = (self.now - timedelta(days=d)).replace(hour=0)
            weekday = 0.5 if day.weekday() >= 5 else 1.0
            for project, meta in PROJECTS.items():
                for agent in meta["agents"]:
                    model = AGENT_MODEL[agent]
                    base = {"gpt-5.4": 38, "o4-mini": 22, "gpt-5.4-mini": 9, "model-router": 12}[model]
                    cost = base * meta["weight"] * weekday * self.rng.uniform(0.75, 1.25) * (0.85 + 0.3 * (60 - d) / 60)
                    if project == "marketing-gen" and d <= 2:
                        cost *= 3.4
                    rows.append({
                        "TimeGenerated": _iso(day), "Project": project, "Agent": agent, "Team": meta["team"],
                        "Deployment": model, "Model": model, "Meter": f"{model} glbl inp/outp",
                        "Cost": round(cost, 2), "Currency": "USD",
                    })
            rows.append({
                "TimeGenerated": _iso(day), "Project": "customer-care", "Agent": "", "Team": "Support",
                "Deployment": "text-embedding-3-large", "Model": "text-embedding-3-large",
                "Meter": "text-embedding-3-large tokens", "Cost": round(3.1 * weekday * self.rng.uniform(0.8, 1.2), 2),
                "Currency": "USD",
            })
        return rows

    # ------------------------------------------------------------ resources
    def _period(self) -> tuple[str, str]:
        start = self.now.replace(day=1, hour=0)
        return _iso(start), _iso(start.replace(year=start.year + 1))

    def _cost_controls(self) -> list[dict[str, Any]]:
        s, e = self._period()
        conn = f"{ACCOUNT_ID}/connections/eventgrid-finops-alerts"
        return [
            {"name": "fraud-risk-monthly", "project": None, "category": "Cost", "amount": 1500, "time_grain": "Monthly",
             "start_date": s, "end_date": e, "filters": {"project": ["fraud-risk"]}, "sharing": "agent",
             "alert": {"threshold": 80, "threshold_type": "Percentage", "event_grid_connection_id": conn},
             "block": {"threshold": 100, "threshold_type": "Percentage"}, "usage": 1104.2},
            {"name": "marketing-daily-tokens", "project": None, "category": "Tokens", "amount": 40_000_000,
             "time_grain": "Daily", "start_date": s, "end_date": e,
             "filters": {"project": ["marketing-gen"]}, "sharing": "shared",
             "alert": {"threshold": 75, "threshold_type": "Percentage", "event_grid_connection_id": conn},
             "block": None, "usage": 38_900_000},
            {"name": "sandbox-cap", "project": "dev-sandbox", "category": "Cost", "amount": 200, "time_grain": "Weekly",
             "start_date": s, "end_date": e, "filters": {}, "sharing": "shared", "alert": None,
             "block": {"threshold": 100, "threshold_type": "Percentage"}, "usage": 61.5},
        ]

    def _budgets(self) -> list[dict[str, Any]]:
        s, e = self._period()
        return [
            {"name": "ai-platform-monthly", "amount": 25000, "time_grain": "Monthly", "start_date": s, "end_date": e,
             "current_spend": 14230.55, "forecast_spend": 23810.0, "currency": "USD",
             "filter": {"dimensions": {"name": "ResourceGroupName", "operator": "In", "values": [RG]}},
             "notifications": [{"threshold": 80, "threshold_type": "Actual", "contact_emails": ["finops@contoso.com"]},
                               {"threshold": 100, "threshold_type": "Forecasted", "contact_emails": ["finops@contoso.com"]}]},
            {"name": "support-team", "amount": 9000, "time_grain": "Monthly", "start_date": s, "end_date": e,
             "current_spend": 6020.11, "forecast_spend": 9640.0, "currency": "USD",
             "filter": {"tags": {"name": "team", "operator": "In", "values": ["Support"]}},
             "notifications": [{"threshold": 90, "threshold_type": "Actual", "contact_emails": ["support-lead@contoso.com"]}]},
        ]

    def _tags(self) -> list[dict[str, Any]]:
        return [
            {"scope": f"/subscriptions/{SUB}/resourceGroups/{RG}", "name": RG, "type": "Resource group",
             "tags": {"cost-center": "CC-1001", "owner": "ai-platform"}},
            {"scope": ACCOUNT_ID, "name": ACCOUNT, "type": "Microsoft.CognitiveServices/accounts",
             "tags": {"team": "Platform", "env": "prod", "cost-center": "CC-1001"}},
            {"scope": f"{ACCOUNT_ID}/projects/fraud-risk", "name": "fraud-risk", "type": "Foundry project",
             "tags": {"team": "Risk", "cost-center": "CC-4410"}},
            {"scope": f"{ACCOUNT_ID}/projects/customer-care", "name": "customer-care", "type": "Foundry project",
             "tags": {"team": "Support", "cost-center": "CC-2200"}},
            {"scope": f"{ACCOUNT_ID}/projects/marketing-gen", "name": "marketing-gen", "type": "Foundry project",
             "tags": {"team": "Marketing", "cost-center": "CC-3100"}},
            {"scope": f"{ACCOUNT_ID}/projects/dev-sandbox", "name": "dev-sandbox", "type": "Foundry project",
             "tags": {"team": "Platform", "env": "dev"}},
            {"scope": APIM_ID, "name": APIM, "type": "Microsoft.ApiManagement/service",
             "tags": {"team": "Platform", "role": "ai-gateway"}},
        ]

    def _deployments(self) -> list[dict[str, Any]]:
        return [
            {"name": n, "model_name": m, "model_version": "2026-08-01", "sku_name": sku, "capacity": cap,
             "tokens_per_minute": cap * 1000, "provisioning_state": "Succeeded"}
            for n, (m, sku, cap) in DEPLOYMENTS.items()
        ]

    def _quota(self) -> list[dict[str, Any]]:
        used: dict[str, int] = {}
        for d in self.deployments:
            key = f"OpenAI.{d['sku_name']}.{d['model_name']}"
            used[key] = used.get(key, 0) + d["capacity"]
        out = []
        for key, cur in used.items():
            limit = {"GlobalStandard": 5000, "DataZoneStandard": 1000, "Standard": 1000}[key.split(".")[1]]
            out.append({"name": key, "display_name": f"Tokens Per Minute (thousands) - {key.split('.', 2)[2]} - "
                        f"{key.split('.')[1]}", "current_value": cur, "limit": limit, "unit": "Count",
                        "available": limit - cur, "percent_used": round(cur / limit * 100, 1)})
        return out

    def _alerts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        ag = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/microsoft.insights/actionGroups/finops-oncall"
        metric = [
            {"name": "gpt-5.4-hourly-tokens", "description": "gpt-5.4 > 8M tokens/hour", "severity": 2, "enabled": True,
             "scopes": [ACCOUNT_ID], "metric_name": "TokenTransaction", "operator": "GreaterThan", "threshold": 8_000_000,
             "time_aggregation": "Total", "window_size": "PT1H", "evaluation_frequency": "PT15M",
             "dimensions": [{"name": "ModelDeploymentName", "operator": "Include", "values": ["gpt-5.4"]}],
             "action_group_ids": [ag]},
            {"name": "request-spike", "description": "Requests > 50k/hour on any deployment", "severity": 3,
             "enabled": False, "scopes": [ACCOUNT_ID], "metric_name": "AzureOpenAIRequests", "operator": "GreaterThan",
             "threshold": 50000, "time_aggregation": "Total", "window_size": "PT1H", "evaluation_frequency": "PT15M",
             "dimensions": [], "action_group_ids": [ag]},
        ]
        log = [
            {"name": "team-daily-token-cap", "description": "Any team > 150M tokens/day (UsageTracker)", "severity": 2,
             "enabled": True, "location": "westeurope", "scopes": ["<app-insights-resource-id>"],
             "query": "traces | where message == 'llm.usage' | summarize t=sum(toint(customDimensions['total_tokens'])) "
                      "by tostring(customDimensions['finops.team'])",
             "operator": "GreaterThan", "threshold": 150_000_000, "time_aggregation": "Total",
             "metric_measure_column": "t", "window_size": "P1D", "evaluation_frequency": "PT1H",
             "action_group_ids": [ag]},
        ]
        groups = [{"name": "finops-oncall", "short_name": "finops", "enabled": True,
                   "emails": {"finops": "finops@contoso.com"}, "webhooks": {}}]
        return metric, log, groups

    def _apim(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        products = [
            {"name": "team-risk", "display_name": "Risk team", "description": "Fraud & AML agents", "state": "published",
             "subscription_required": True, "approval_required": True},
            {"name": "team-support", "display_name": "Support team", "description": "Customer care", "state": "published",
             "subscription_required": True, "approval_required": False},
            {"name": "team-marketing", "display_name": "Marketing team", "description": "Content generation",
             "state": "published", "subscription_required": True, "approval_required": True},
        ]
        subs = [
            {"name": "risk-prod", "display_name": "Risk – production", "scope": "/products/team-risk", "state": "active"},
            {"name": "support-prod", "display_name": "Support – production", "scope": "/products/team-support",
             "state": "active"},
            {"name": "marketing-dev", "display_name": "Marketing – dev", "scope": "/products/team-marketing",
             "state": "suspended"},
        ]
        limits = {
            "product:team-risk": [{"counter_key": "@(context.Subscription.Id)", "tokens_per_minute": 60000,
                                   "token_quota": 400_000_000, "token_quota_period": "Monthly",
                                   "estimate_prompt_tokens": False}],
            "product:team-support": [{"counter_key": "@(context.Subscription.Id)", "tokens_per_minute": 120000,
                                      "token_quota": None, "token_quota_period": None, "estimate_prompt_tokens": False}],
            "product:team-marketing": [{"counter_key": "@(context.Subscription.Id)", "tokens_per_minute": 30000,
                                        "token_quota": 50_000_000, "token_quota_period": "Daily",
                                        "estimate_prompt_tokens": True}],
        }
        return products, subs, limits

    def _alert_events(self) -> list[dict[str, Any]]:
        out = []
        for i in range(12):
            ts = self.now - timedelta(hours=self.rng.randint(1, 240))
            proj = self.rng.choice(["fraud-risk", "marketing-gen"])
            out.append({
                "TimeGenerated": _iso(ts), "CostControl": "fraud-risk-monthly" if proj == "fraud-risk" else
                "marketing-daily-tokens", "Project": proj,
                "Agent": self.rng.choice(PROJECTS[proj]["agents"]), "Threshold": "80.0%" if i % 3 else "75.0%",
                "Action": "alert", "Severity": "warning", "Unit": "usd" if proj == "fraud-risk" else "tokens",
            })
        return sorted(out, key=lambda r: r["TimeGenerated"])

    # ------------------------------------------------------------ KQL tables
    def tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "TokenUsage": self.token_usage,
            "CostDaily": self.cost_daily,
            "CostControlAlerts": self.alert_events,
            "Deployments": self.deployments,
            "QuotaUsage": self.quota,
        }
