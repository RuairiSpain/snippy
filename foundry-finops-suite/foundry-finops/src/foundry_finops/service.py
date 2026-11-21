"""High-level reporting/management facade shared by the dashboard and the MCP server.

Every method returns JSON-serialisable data. Two implementations share one interface:

* :class:`LiveService` – calls Azure through :class:`~foundry_finops.FinOpsClient`.
* :class:`DemoService` – in-memory illustrative data (``FINOPS_DEMO=1``).

Configure with environment variables (see :class:`Settings`) and build with :func:`from_env`.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from . import queries
from .models import (
    Budget,
    BudgetCategory,
    BudgetNotification,
    BudgetSharing,
    CostControl,
    CostQuery,
    MetricAlert,
    Table,
    Threshold,
    ThresholdType,
    TimeGrain,
)

logger = logging.getLogger("foundry_finops.service")

GROUPINGS = ("project", "agent", "deployment", "model", "team")
#: Cost-control states that need a person's attention.
ATTENTION_STATES = ("Near threshold", "Limit reached", "Over budget")


# ================================================================= settings
@dataclass
class Settings:
    """Runtime configuration, read from ``FINOPS_*`` environment variables."""

    subscription_id: str = ""
    resource_group: str = ""
    foundry_account: str = ""
    apim_service: str = ""
    location: str = "eastus2"
    #: Log Analytics workspace ID (GUID) or Application Insights resource ID.
    logs_target: str = ""
    #: Cost Management tag that holds your team name.
    team_tag: str = "team"
    demo: bool = False
    read_only: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            subscription_id=e("FINOPS_SUBSCRIPTION_ID", ""),
            resource_group=e("FINOPS_RESOURCE_GROUP", ""),
            foundry_account=e("FINOPS_FOUNDRY_ACCOUNT", ""),
            apim_service=e("FINOPS_APIM_SERVICE", ""),
            location=e("FINOPS_LOCATION", "eastus2"),
            logs_target=e("FINOPS_LOGS_TARGET", ""),
            team_tag=e("FINOPS_TEAM_TAG", "team"),
            demo=e("FINOPS_DEMO", "").lower() in ("1", "true", "yes") or not e("FINOPS_SUBSCRIPTION_ID"),
            read_only=e("FINOPS_READ_ONLY", "").lower() in ("1", "true", "yes"),
        )


class ReadOnlyError(PermissionError):
    pass


def _check_group(group_by: str, allowed: tuple[str, ...] = GROUPINGS) -> str:
    if group_by not in allowed:
        raise ValueError(f"group_by must be one of: {', '.join(allowed)} (got {group_by!r})")
    return group_by


# ============================================================ payload helpers
def cost_control_from_payload(p: dict[str, Any]) -> CostControl:
    def thr(d: dict[str, Any] | None) -> Threshold | None:
        if not d or d.get("threshold") in (None, ""):
            return None
        return Threshold(
            float(d["threshold"]),
            ThresholdType(d.get("threshold_type") or "Percentage"),
            event_grid_connection_id=d.get("event_grid_connection_id") or None,
        )

    filters = {k: [v for v in (vals if isinstance(vals, list) else str(vals).split(",")) if str(v).strip()]
               for k, vals in (p.get("filters") or {}).items()}
    return CostControl(
        name=p["name"],
        category=BudgetCategory(p.get("category") or "Cost"),
        amount=float(p["amount"]),
        time_grain=TimeGrain(p.get("time_grain") or "Monthly"),
        start_date=p.get("start_date") or None,
        end_date=p.get("end_date") or None,
        filters={k: [s.strip() for s in v] for k, v in filters.items() if v},
        sharing=BudgetSharing(p.get("sharing") or "shared"),
        alert=thr(p.get("alert")),
        block=thr(p.get("block")),
        project=p.get("project") or None,
    )


def cost_control_to_payload(cc: CostControl, usage: float | None = None) -> dict[str, Any]:
    d = cc.to_json()
    d.update(category=str(cc.category), time_grain=str(cc.time_grain), sharing=str(cc.sharing))
    for k in ("alert", "block"):
        t = getattr(cc, k)
        d[k] = None if t is None else {"threshold": t.threshold, "threshold_type": str(t.threshold_type),
                                       "event_grid_connection_id": t.event_grid_connection_id}
    d["usage"] = usage
    return d


def budget_from_payload(p: dict[str, Any]) -> Budget:
    return Budget(
        name=p["name"], amount=float(p["amount"]), time_grain=TimeGrain(p.get("time_grain") or "Monthly"),
        start_date=p.get("start_date") or None, end_date=p.get("end_date") or None, filter=p.get("filter") or None,
        notifications=[BudgetNotification(
            threshold=float(n["threshold"]), threshold_type=n.get("threshold_type", "Actual"),
            contact_emails=[e for e in (n.get("contact_emails") or []) if e],
            contact_groups=list(n.get("contact_groups") or []),
        ) for n in p.get("notifications") or []],
    )


def _with_status(row: dict[str, Any]) -> dict[str, Any]:
    usage, amount = row.get("usage"), row.get("amount") or 0
    if usage is None or not amount:
        row["percent_used"], row["state"] = None, "Usage unknown"
        return row
    pct = usage / amount * 100

    def as_pct(t: dict[str, Any] | None) -> float | None:
        """Thresholds may be a percentage of the amount or an absolute amount."""
        if not t or t.get("threshold") in (None, ""):
            return None
        value = float(t["threshold"])
        return value / amount * 100 if str(t.get("threshold_type")) == "Absolute" else value

    alert = as_pct(row.get("alert")) or 80
    block = as_pct(row.get("block"))
    row["percent_used"] = round(pct, 1)
    row["state"] = ("Limit reached" if block and pct >= block else "Over budget" if pct >= 100
                    else "Near threshold" if pct >= alert else "On track")
    return row


# =============================================================== interface
class FinOpsService(Protocol):
    settings: Settings

    def info(self) -> dict[str, Any]: ...
    def overview(self, days: int = 30) -> dict[str, Any]: ...
    def cost_series(self, group_by: str = "project", days: int = 30) -> dict[str, Any]: ...
    def token_series(self, group_by: str = "deployment", hours: int = 168) -> dict[str, Any]: ...
    def list_cost_controls(self) -> list[dict[str, Any]]: ...
    def upsert_cost_control(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_cost_control(self, name: str, project: str | None = None) -> None: ...
    def list_budgets(self) -> list[dict[str, Any]]: ...
    def upsert_budget(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_budget(self, name: str) -> None: ...
    def list_tags(self) -> list[dict[str, Any]]: ...
    def set_tags(self, scope: str, tags: dict[str, str]) -> dict[str, str]: ...
    def list_deployments(self) -> list[dict[str, Any]]: ...
    def set_capacity(self, deployment: str, capacity: int) -> dict[str, Any]: ...
    def list_quota(self) -> list[dict[str, Any]]: ...
    def list_alerts(self) -> dict[str, list[dict[str, Any]]]: ...
    def upsert_metric_alert(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_alert(self, kind: str, name: str) -> None: ...
    def apim_overview(self) -> dict[str, Any]: ...
    def upsert_apim_product(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_apim_product(self, name: str) -> None: ...
    def upsert_apim_subscription(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def delete_apim_subscription(self, name: str) -> None: ...
    def set_token_limit(self, scope: str, payload: dict[str, Any]) -> list[dict[str, Any]]: ...
    def delete_token_limit(self, scope: str) -> None: ...
    def schema(self) -> dict[str, list[dict[str, str]]]: ...
    def saved_queries(self) -> dict[str, str]: ...
    def kql(self, query: str, timespan: str = "P7D") -> dict[str, Any]: ...


class _Base:
    settings: Settings

    def _guard(self) -> None:
        if self.settings.read_only:
            raise ReadOnlyError("This dashboard is running in read-only mode (FINOPS_READ_ONLY=1)")


# ==================================================================== demo
class DemoService(_Base):
    """Illustrative data that mirrors the live response shapes. Writes change the in-memory store."""

    def __init__(self, settings: Settings | None = None) -> None:
        from .demo import DemoStore

        self.settings = settings or Settings(demo=True)
        self.store = DemoStore()

    def info(self) -> dict[str, Any]:
        from .demo import data as d

        return {"mode": "demo", "subscription_id": d.SUB, "resource_group": d.RG, "foundry_account": d.ACCOUNT,
                "apim_service": d.APIM, "logs_target": "demo tables", "read_only": self.settings.read_only,
                "note": "Demo mode – all names and numbers are illustrative."}

    # ---- usage & cost
    def _since(self, rows: list[dict[str, Any]], delta: timedelta) -> list[dict[str, Any]]:
        cutoff = (self.store.now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [r for r in rows if r["TimeGenerated"] >= cutoff]

    def overview(self, days: int = 30) -> dict[str, Any]:
        s = self.store
        month_start = s.now.replace(day=1, hour=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        mtd = sum(r["Cost"] for r in s.cost_daily if r["TimeGenerated"] >= month_start)
        days_elapsed = max(s.now.day, 1)
        import calendar

        dim = calendar.monthrange(s.now.year, s.now.month)[1]
        last24 = self._since(s.token_usage, timedelta(hours=24))
        ccs = self.list_cost_controls()
        return {
            "currency": "USD",
            "kpis": {
                "spend_mtd": round(mtd, 2),
                "forecast_month_end": round(mtd / days_elapsed * dim, 2),
                "tokens_24h": sum(r["InputTokens"] + r["OutputTokens"] for r in last24),
                "requests_24h": sum(r["Requests"] for r in last24),
                "cost_controls": len(ccs),
                "cost_controls_attention": sum(1 for c in ccs if c["state"] in ATTENTION_STATES),
                "budgets": len(s.budgets),
                "quota_max_pct": max((q["percent_used"] for q in s.quota), default=0),
            },
            "top_projects": self._top("Project", days),
            "top_agents": self._top("Agent", days),
            "top_models": self._top("Model", days),
        }

    def _top(self, col: str, days: int) -> list[dict[str, Any]]:
        agg: dict[str, float] = defaultdict(float)
        for r in self._since(self.store.cost_daily, timedelta(days=days)):
            if r.get(col):
                agg[r[col]] += r["Cost"]
        return [{"name": k, "cost": round(v, 2)} for k, v in sorted(agg.items(), key=lambda x: -x[1])]

    def cost_series(self, group_by: str = "project", days: int = 30) -> dict[str, Any]:
        col = _check_group(group_by).capitalize()
        agg: dict[tuple[str, str], float] = defaultdict(float)
        for r in self._since(self.store.cost_daily, timedelta(days=days)):
            agg[(r["TimeGenerated"][:10], r.get(col) or "(untagged)")] += r["Cost"]
        rows = [[d, k, round(v, 2), "USD"] for (d, k), v in sorted(agg.items())]
        return Table.from_records([dict(zip(["UsageDate", group_by, "Cost", "Currency"], r)) for r in rows]).to_json()

    def token_series(self, group_by: str = "deployment", hours: int = 168) -> dict[str, Any]:
        col = _check_group(group_by).capitalize()
        bucket = 1 if hours <= 48 else 6 if hours <= 336 else 24
        agg: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
        for r in self._since(self.store.token_usage, timedelta(hours=hours)):
            ts = datetime.fromisoformat(r["TimeGenerated"].replace("Z", "+00:00"))
            ts = ts.replace(hour=ts.hour - ts.hour % bucket) if bucket < 24 else ts.replace(hour=0)
            a = agg[(ts.strftime("%Y-%m-%dT%H:%M:%SZ"), r[col])]
            a[0] += r["InputTokens"]
            a[1] += r["OutputTokens"]
            a[2] += r["Requests"]
        recs = [{"TimeGenerated": t, group_by: k, "InputTokens": v[0], "OutputTokens": v[1],
                 "TotalTokens": v[0] + v[1], "Requests": v[2]} for (t, k), v in sorted(agg.items())]
        return Table.from_records(recs, "Tokens").to_json()

    # ---- cost controls
    def list_cost_controls(self) -> list[dict[str, Any]]:
        return [_with_status(dict(c)) for c in self.store.cost_controls]

    def upsert_cost_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        cc = cost_control_from_payload(payload)
        cc.to_dict()  # validates exactly like the live API path
        row = cost_control_to_payload(cc)
        existing = next((c for c in self.store.cost_controls if c["name"] == cc.name and c.get("project") == cc.project), None)
        if existing:
            if existing["category"] != row["category"]:
                raise ValueError("A cost control's budget type can't be changed; create a new one")
            row["usage"] = existing.get("usage")
            existing.clear()
            existing.update(row)
        else:
            row["usage"] = 0.0
            self.store.cost_controls.append(row)
        return _with_status(dict(row))

    def delete_cost_control(self, name: str, project: str | None = None) -> None:
        self._guard()
        self.store.cost_controls[:] = [c for c in self.store.cost_controls
                                       if not (c["name"] == name and (c.get("project") or None) == (project or None))]

    # ---- budgets
    def list_budgets(self) -> list[dict[str, Any]]:
        return [dict(b, percent_used=round(b["current_spend"] / b["amount"] * 100, 1) if b.get("amount") else None)
                for b in self.store.budgets]

    def upsert_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        b = budget_from_payload(payload)
        b.to_dict()
        row = b.to_json()
        row["time_grain"] = str(b.time_grain)
        old = next((x for x in self.store.budgets if x["name"] == b.name), None)
        row.update(current_spend=(old or {}).get("current_spend", 0.0),
                   forecast_spend=(old or {}).get("forecast_spend", 0.0), currency="USD")
        self.store.budgets[:] = [x for x in self.store.budgets if x["name"] != b.name] + [row]
        return row

    def delete_budget(self, name: str) -> None:
        self._guard()
        self.store.budgets[:] = [b for b in self.store.budgets if b["name"] != name]

    # ---- tags
    def list_tags(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self.store.tags]

    def set_tags(self, scope: str, tags: dict[str, str]) -> dict[str, str]:
        self._guard()
        for t in self.store.tags:
            if t["scope"] == scope:
                t["tags"] = dict(tags)
                return dict(t["tags"])
        raise KeyError(f"Unknown scope {scope}")

    # ---- quota & deployments
    def list_deployments(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.store.deployments]

    def set_capacity(self, deployment: str, capacity: int) -> dict[str, Any]:
        self._guard()
        d = next((x for x in self.store.deployments if x["name"] == deployment), None)
        if d is None:
            raise KeyError(deployment)
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        q = next((q for q in self.store.quota if q["name"].endswith(f".{d['sku_name']}.{d['model_name']}")), None)
        if q is None:
            raise ValueError(f"No quota record for deployment {deployment!r}")
        new_total = q["current_value"] - d["capacity"] + capacity
        if new_total > q["limit"]:
            raise ValueError(f"Not enough quota: {new_total} > {q['limit']} (thousands of TPM)")
        q.update(current_value=new_total, available=q["limit"] - new_total,
                 percent_used=round(new_total / q["limit"] * 100, 1))
        d.update(capacity=capacity, tokens_per_minute=capacity * 1000)
        return dict(d)

    def list_quota(self) -> list[dict[str, Any]]:
        return [dict(q) for q in self.store.quota]

    # ---- alerts
    def list_alerts(self) -> dict[str, list[dict[str, Any]]]:
        return {"metric": [dict(a) for a in self.store.metric_alerts],
                "log": [dict(a) for a in self.store.log_alerts],
                "action_groups": [dict(a) for a in self.store.action_groups]}

    def upsert_metric_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        a = _metric_alert_from_payload(payload, [])
        a.to_dict()
        row = a.to_json()
        self.store.metric_alerts[:] = [x for x in self.store.metric_alerts if x["name"] != a.name] + [row]
        return row

    def delete_alert(self, kind: str, name: str) -> None:
        self._guard()
        target = {"metric": self.store.metric_alerts, "log": self.store.log_alerts,
                  "action_group": self.store.action_groups}[kind]
        target[:] = [x for x in target if x["name"] != name]

    # ---- APIM
    def apim_overview(self) -> dict[str, Any]:
        limits = [{"scope": sc, **lim} for sc, lst in self.store.apim_policies.items() for lim in lst]
        return {"products": [dict(p) for p in self.store.apim_products],
                "subscriptions": [dict(s) for s in self.store.apim_subscriptions], "token_limits": limits}

    def upsert_apim_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        row = {k: payload.get(k) for k in ("name", "display_name", "description", "state", "subscription_required",
                                            "approval_required")}
        self.store.apim_products[:] = [p for p in self.store.apim_products if p["name"] != row["name"]] + [row]
        return row

    def delete_apim_product(self, name: str) -> None:
        self._guard()
        self.store.apim_products[:] = [p for p in self.store.apim_products if p["name"] != name]
        self.store.apim_subscriptions[:] = [s for s in self.store.apim_subscriptions if s["scope"] != f"/products/{name}"]
        self.store.apim_policies.pop(f"product:{name}", None)

    def upsert_apim_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        row = {k: payload.get(k) for k in ("name", "display_name", "scope", "state")}
        self.store.apim_subscriptions[:] = [s for s in self.store.apim_subscriptions if s["name"] != row["name"]] + [row]
        return row

    def delete_apim_subscription(self, name: str) -> None:
        self._guard()
        self.store.apim_subscriptions[:] = [s for s in self.store.apim_subscriptions if s["name"] != name]

    def set_token_limit(self, scope: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._guard()
        from .operations.apim import LlmTokenLimit

        lim = _token_limit_from_payload(payload)
        lim.to_xml()  # validates
        row = {k: v for k, v in vars(lim).items() if not k.endswith("header_name") and k != "tag"}
        self.store.apim_policies[scope] = [row]
        _ = LlmTokenLimit
        return self.store.apim_policies[scope]

    def delete_token_limit(self, scope: str) -> None:
        self._guard()
        self.store.apim_policies.pop(scope, None)

    # ---- logs
    def schema(self) -> dict[str, list[dict[str, str]]]:
        out = {}
        for name, rows in self.store.tables().items():
            first = rows[0] if rows else {}
            out[name] = [{"name": k, "type": _type_of(v)} for k, v in first.items()]
        return out

    def saved_queries(self) -> dict[str, str]:
        return {
            "Tokens per model per day": "TokenUsage\n| where TimeGenerated > ago(14d)\n| summarize Tokens = "
            "sum(InputTokens + OutputTokens) by Model, bin(TimeGenerated, 1d)\n| render timechart",
            "Spend per project (30d)": "CostDaily\n| where TimeGenerated > ago(30d)\n| summarize Cost = sum(Cost) "
            "by Project\n| order by Cost desc\n| render piechart",
            "Daily spend per team": "CostDaily\n| summarize Cost = sum(Cost) by Team, bin(TimeGenerated, 1d)\n"
            "| render columnchart",
            "Top 10 agents by tokens (7d)": "TokenUsage\n| where TimeGenerated > ago(7d)\n| summarize Tokens = "
            "sum(InputTokens + OutputTokens), Requests = sum(Requests) by Agent, Project\n| top 10 by Tokens desc",
            "Cache hit ratio by model": "TokenUsage\n| summarize Cached = sum(CachedTokens), Input = sum(InputTokens)"
            " by Model\n| extend CacheHitPct = round(100.0 * Cached / Input, 1)\n| order by CacheHitPct desc",
            "Estimated cost per agent per hour (48h)": "TokenUsage\n| where TimeGenerated > ago(48h)\n| summarize "
            "Cost = sum(EstimatedCost) by Agent, bin(TimeGenerated, 1h)\n| render timechart",
            "Cost-control alerts": "CostControlAlerts\n| order by TimeGenerated desc",
            "Quota headroom": "QuotaUsage\n| project display_name, current_value, limit, percent_used\n"
            "| order by percent_used desc",
        }

    def kql(self, query: str, timespan: str = "P7D") -> dict[str, Any]:
        from .demo import run

        res = run(query, self.store.tables())
        cols = [{"name": c, "type": _type_of(res.rows[0][i]) if res.rows else "string"} for i, c in enumerate(res.columns)]
        return {"columns": cols, "rows": res.rows, "render": res.render}


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "long"
    if isinstance(v, float):
        return "real"
    if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-" and v[7:8] == "-":
        return "datetime"
    if isinstance(v, (dict, list)):
        return "dynamic"
    return "string"


def _metric_alert_from_payload(p: dict[str, Any], default_scopes: list[str]) -> MetricAlert:
    dims = p.get("dimensions") or []
    if isinstance(dims, str):
        dims = [{"name": "ModelDeploymentName", "operator": "Include", "values": [v.strip() for v in dims.split(",")]}] \
            if dims.strip() else []
    return MetricAlert(
        name=p["name"], description=p.get("description", ""), severity=int(p.get("severity", 2)),
        enabled=bool(p.get("enabled", True)), scopes=list(p.get("scopes") or default_scopes),
        metric_name=p.get("metric_name") or "TokenTransaction", operator=p.get("operator") or "GreaterThan",
        threshold=float(p.get("threshold") or 0), time_aggregation=p.get("time_aggregation") or "Total",
        dimensions=dims, window_size=p.get("window_size") or "PT1H",
        evaluation_frequency=p.get("evaluation_frequency") or "PT15M",
        action_group_ids=list(p.get("action_group_ids") or []),
    )


def _token_limit_from_payload(p: dict[str, Any]) -> Any:
    from .operations.apim import LlmTokenLimit

    def num(k: str) -> int | None:
        v = p.get(k)
        return int(v) if v not in (None, "") else None

    return LlmTokenLimit(
        counter_key=p.get("counter_key") or "@(context.Subscription.Id)",
        tokens_per_minute=num("tokens_per_minute"), token_quota=num("token_quota"),
        token_quota_period=p.get("token_quota_period") or None,
        estimate_prompt_tokens=bool(p.get("estimate_prompt_tokens", False)),
    )


# ==================================================================== live
class LiveService(_Base):
    """Calls Azure. Each method maps to the SDK operations noted in its body."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        from .client import FinOpsClient

        self.settings = settings
        self.fo = client or FinOpsClient(settings.subscription_id, resource_group=settings.resource_group or None)

    @property
    def _acct(self) -> str:
        if not self.settings.foundry_account:
            raise ValueError("Set FINOPS_FOUNDRY_ACCOUNT")
        return self.settings.foundry_account

    @property
    def _account_id(self) -> str:
        return self.fo.foundry.account_id(self._acct)

    def info(self) -> dict[str, Any]:
        s = asdict(self.settings)
        s["mode"] = "live"
        return s

    def overview(self, days: int = 30) -> dict[str, Any]:
        """Headline numbers. A section that fails (RBAC, preview API, no data) is reported under
        ``warnings`` and shown as empty instead of failing the whole overview."""
        import calendar

        warnings: list[str] = []

        def attempt(label: str, fn: Any, default: Any) -> Any:
            try:
                return fn()
            except Exception as exc:
                warnings.append(f"{label}: {_describe(exc)}")
                return default

        mtd = attempt("Cost Management", lambda: self.fo.cost.query(CostQuery.month_to_date().total()), None)
        spend = _sum_cost(mtd) if mtd is not None else 0.0
        currency = next((r.get("Currency") for r in mtd or [] if r.get("Currency")), "USD")
        now = datetime.now(timezone.utc)
        month_end = now.replace(day=calendar.monthrange(now.year, now.month)[1], hour=23, minute=59, second=59)
        forecast = spend
        if mtd is not None:
            try:  # forecast isn't available for every scope/offer, so a failure is not a warning
                fc = self.fo.cost.forecast(
                    CostQuery.between(now.replace(day=1, hour=0, minute=0, second=0), month_end).daily()
                )
                forecast = _sum_cost(fc) or spend
            except Exception:
                logger.debug("Forecast unavailable", exc_info=True)
        tokens = attempt("Azure Monitor metrics", lambda: self.fo.metrics.query(
            self._account_id, ["TokenTransaction", "AzureOpenAIRequests"], hours=24, interval="PT1H"), [])
        ccs = attempt("Cost controls", self.list_cost_controls, [])
        quota = attempt("Quota", self.list_quota, [])
        budgets = attempt("Budgets", self.list_budgets, [])
        tops: dict[str, list[dict[str, Any]]] = {}
        for key, group in (("top_projects", "project"), ("top_agents", "agent"), ("top_models", "model")):
            tops[key] = attempt(f"Cost by {group}", lambda g=group: _top_from_series(self.cost_series(g, days), g), [])
        out: dict[str, Any] = {
            "currency": currency,
            "kpis": {
                "spend_mtd": round(spend, 2), "forecast_month_end": round(forecast, 2),
                "tokens_24h": int(sum(s.total for s in tokens if s.metric == "TokenTransaction")),
                "requests_24h": int(sum(s.total for s in tokens if s.metric == "AzureOpenAIRequests")),
                "cost_controls": len(ccs),
                "cost_controls_attention": sum(1 for c in ccs if c.get("state") in ATTENTION_STATES),
                "budgets": len(budgets),
                "quota_max_pct": max((q["percent_used"] for q in quota), default=0),
            },
            **tops,
        }
        if warnings:
            out["warnings"] = warnings
        return out

    def cost_series(self, group_by: str = "project", days: int = 30) -> dict[str, Any]:
        _check_group(group_by)
        tag = {"project": "project", "agent": "agent.name", "deployment": "deployment",
               "team": self.settings.team_tag}.get(group_by)
        if group_by == "model":
            t = self.fo.cost.cost_by("MeterSubCategory", days=days)
        else:
            scope = self._account_id if group_by in ("project", "agent", "deployment") else None
            t = self.fo.cost.cost_by(tag or group_by, days=days, scope=scope)
        recs = []
        for r in t:
            key = r.get("TagValue") or r.get(tag or "") or r.get("MeterSubCategory") or "(untagged)"
            recs.append({"UsageDate": r.get("UsageDate"), group_by: key,
                         "Cost": round(r.get("Cost") or r.get("PreTaxCost") or 0, 4), "Currency": r.get("Currency")})
        return Table.from_records(recs, "Cost").to_json()

    def token_series(self, group_by: str = "deployment", hours: int = 168) -> dict[str, Any]:
        if group_by not in ("deployment", "model"):
            raise ValueError(
                "Azure Monitor metrics can only be split by 'deployment' or 'model'. Per project, agent or team "
                "token counts need client-side tracing (see UsageTracker) or APIM token metrics: use run_kql_query."
            )
        dim = "ModelName" if group_by == "model" else "ModelDeploymentName"
        if hours < 1:
            raise ValueError("hours must be at least 1")
        interval = "PT1H" if hours <= 48 else "PT6H" if hours <= 336 else "P1D"
        series = self.fo.metrics.query(self._account_id, ["ProcessedPromptTokens", "GeneratedTokens",
                                                          "AzureOpenAIRequests"],
                                       hours=hours, interval=interval, split_by=[dim])
        agg: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for s in series:
            key = s.dimensions.get(dim.lower()) or s.dimensions.get(dim) or "(all)"
            for p in s.points:
                agg[(p.timestamp, key)][s.metric] += p.value or 0
        recs = [{"TimeGenerated": t, group_by: k, "InputTokens": int(v["ProcessedPromptTokens"]),
                 "OutputTokens": int(v["GeneratedTokens"]),
                 "TotalTokens": int(v["ProcessedPromptTokens"] + v["GeneratedTokens"]),
                 "Requests": int(v["AzureOpenAIRequests"])} for (t, k), v in sorted(agg.items())]
        return Table.from_records(recs, "Tokens").to_json()

    # ---- cost controls
    def list_cost_controls(self) -> list[dict[str, Any]]:
        return [_with_status(cost_control_to_payload(cc)) for cc in self.fo.cost_controls.list_all(self._acct)]

    def upsert_cost_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        cc = cost_control_from_payload(payload)
        out = self.fo.cost_controls.create_or_update(self._acct, cc, project=cc.project)
        return _with_status(cost_control_to_payload(out))

    def delete_cost_control(self, name: str, project: str | None = None) -> None:
        self._guard()
        self.fo.cost_controls.delete(self._acct, name, project=project)

    # ---- budgets
    def list_budgets(self) -> list[dict[str, Any]]:
        out = []
        for b in self.fo.cost.list_budgets():
            row = b.to_json()
            row["time_grain"] = str(b.time_grain)
            row["percent_used"] = round((b.current_spend or 0) / b.amount * 100, 1) if b.amount else None
            out.append(row)
        return out

    def upsert_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        return self.fo.cost.create_or_update_budget(budget_from_payload(payload)).to_json()

    def delete_budget(self, name: str) -> None:
        self._guard()
        self.fo.cost.delete_budget(name)

    # ---- tags
    def list_tags(self) -> list[dict[str, Any]]:
        rows = []
        if self.settings.resource_group:
            scope = self.fo.tags.resource_group_scope()
            rows.append({"scope": scope, "name": self.settings.resource_group, "type": "Resource group",
                         "tags": self.fo.tags.get(scope)})
        for rtype in ("Microsoft.CognitiveServices/accounts", "Microsoft.CognitiveServices/accounts/projects",
                      "Microsoft.ApiManagement/service"):
            for r in self.fo.tags.list_tagged(rtype):
                rows.append({"scope": r["id"], "name": r["name"], "type": r["type"], "tags": r["tags"]})
        return rows

    def set_tags(self, scope: str, tags: dict[str, str]) -> dict[str, str]:
        self._guard()
        return self.fo.tags.replace(scope, tags)

    # ---- quota
    def list_deployments(self) -> list[dict[str, Any]]:
        return [dict(d.to_json(), tokens_per_minute=d.tokens_per_minute)
                for d in self.fo.foundry.list_deployments(self._acct)]

    def set_capacity(self, deployment: str, capacity: int) -> dict[str, Any]:
        self._guard()
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        d = self.fo.foundry.set_capacity(self._acct, deployment, capacity)
        return dict(d.to_json(), tokens_per_minute=d.tokens_per_minute)

    def list_quota(self) -> list[dict[str, Any]]:
        return [q.to_json() for q in self.fo.quota.usages(self.settings.location) if q.limit]

    # ---- alerts
    def list_alerts(self) -> dict[str, list[dict[str, Any]]]:
        return {"metric": [a.to_json() for a in self.fo.alerts.list_metric_alerts()],
                "log": [a.to_json() for a in self.fo.alerts.list_log_alerts()],
                "action_groups": [a.to_json() for a in self.fo.alerts.list_action_groups()]}

    def upsert_metric_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        alert = _metric_alert_from_payload(payload, [self._account_id])
        return self.fo.alerts.create_or_update_metric_alert(alert).to_json()

    def delete_alert(self, kind: str, name: str) -> None:
        self._guard()
        {"metric": self.fo.alerts.delete_metric_alert, "log": self.fo.alerts.delete_log_alert,
         "action_group": self.fo.alerts.delete_action_group}[kind](name)

    # ---- APIM
    @property
    def _apim(self) -> str:
        if not self.settings.apim_service:
            raise ValueError("Set FINOPS_APIM_SERVICE")
        return self.settings.apim_service

    def apim_overview(self) -> dict[str, Any]:
        if not self.settings.apim_service:
            return {"products": [], "subscriptions": [], "token_limits": [], "note": "FINOPS_APIM_SERVICE not set"}
        return {
            "products": [p.to_json() for p in self.fo.apim.list_products(self._apim)],
            "subscriptions": [s.to_json() for s in self.fo.apim.list_subscriptions(self._apim)],
            "token_limits": self.fo.apim.token_limit_inventory(self._apim),
        }

    def upsert_apim_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        from .models import ApimProduct

        p = ApimProduct(name=payload["name"], display_name=payload.get("display_name", ""),
                        description=payload.get("description", ""), state=payload.get("state", "published"),
                        subscription_required=bool(payload.get("subscription_required", True)),
                        approval_required=bool(payload.get("approval_required", False)))
        return self.fo.apim.create_or_update_product(self._apim, p).to_json()

    def delete_apim_product(self, name: str) -> None:
        self._guard()
        self.fo.apim.delete_product(self._apim, name)

    def upsert_apim_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        from .models import ApimSubscription

        s = ApimSubscription(name=payload["name"], display_name=payload.get("display_name", ""),
                             scope=payload.get("scope", ""), state=payload.get("state", "active"))
        return self.fo.apim.create_or_update_subscription(self._apim, s).to_json()

    def delete_apim_subscription(self, name: str) -> None:
        self._guard()
        self.fo.apim.delete_subscription(self._apim, name)

    def set_token_limit(self, scope: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._guard()
        self.fo.apim.set_token_limit(self._apim, _token_limit_from_payload(payload), scope)
        return [vars(x) for x in self.fo.apim.get_token_limits(self._apim, scope)]

    def delete_token_limit(self, scope: str) -> None:
        self._guard()
        self.fo.apim.remove_token_limit(self._apim, scope)

    # ---- logs
    def schema(self) -> dict[str, list[dict[str, str]]]:
        common = [{"name": "timestamp", "type": "datetime"}, {"name": "customDimensions", "type": "dynamic"},
                  {"name": "operation_Id", "type": "string"}, {"name": "cloud_RoleName", "type": "string"}]
        return {
            "traces": [{"name": "message", "type": "string"}, *common],
            "dependencies": [{"name": "name", "type": "string"}, {"name": "target", "type": "string"},
                             {"name": "duration", "type": "real"}, {"name": "resultCode", "type": "string"}, *common],
            "requests": [{"name": "name", "type": "string"}, {"name": "resultCode", "type": "string"},
                         {"name": "duration", "type": "real"}, *common],
            "customMetrics": [{"name": "name", "type": "string"}, {"name": "valueSum", "type": "real"},
                              {"name": "valueCount", "type": "long"}, *common],
            "exceptions": [{"name": "type", "type": "string"}, {"name": "outerMessage", "type": "string"}, *common],
        }

    def saved_queries(self) -> dict[str, str]:
        return {k: v.strip() for k, v in queries.ALL.items()}

    def kql(self, query: str, timespan: str = "P7D") -> dict[str, Any]:
        if not self.settings.logs_target:
            raise ValueError("Set FINOPS_LOGS_TARGET to a workspace ID or Application Insights resource ID")
        t = self.fo.logs.query(self.settings.logs_target, query, timespan=timespan)
        render = None
        low = query.lower()
        if "| render" in low:
            tail = low.split("| render")[-1].split()
            render = tail[0] if tail else None
        return {"columns": [{"name": c.name, "type": c.type} for c in t.columns], "rows": t.rows, "render": render}


def _describe(exc: BaseException) -> str:
    """One-line, user-safe description of an exception."""
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}: {message}" if status else str(message)


def _sum_cost(rows: Any) -> float:
    return sum(float(r.get("Cost") or r.get("PreTaxCost") or 0) for r in rows)


def _top_from_series(table: dict[str, Any], key: str) -> list[dict[str, Any]]:
    names = [c["name"] for c in table["columns"]]
    agg: dict[str, float] = defaultdict(float)
    for row in table["rows"]:
        r = dict(zip(names, row))
        agg[str(r.get(key))] += float(r.get("Cost") or 0)
    return [{"name": k, "cost": round(v, 2)} for k, v in sorted(agg.items(), key=lambda x: -x[1])]


def from_env(settings: Settings | None = None) -> FinOpsService:
    """Build a :class:`DemoService` or :class:`LiveService` from ``FINOPS_*`` environment variables."""
    s = settings or Settings.from_env()
    return DemoService(s) if s.demo else LiveService(s)


__all__ = ["DemoService", "FinOpsService", "LiveService", "ReadOnlyError", "Settings", "from_env"]
