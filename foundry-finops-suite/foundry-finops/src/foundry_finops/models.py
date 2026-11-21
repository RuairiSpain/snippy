"""Typed models for requests and responses.

Every model is a plain dataclass with ``to_dict()`` (ARM request body) and ``from_dict()``
(ARM response). Unknown response fields are preserved in ``raw`` so nothing is lost when
the service adds properties.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from .errors import DependencyMissingError


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


# ============================================================================ shared
def iso_utc(value: datetime | date | str) -> str:
    """Format a date/datetime as the ISO-8601 UTC string ARM expects (``...Z``)."""
    if isinstance(value, str):
        v = value.strip()
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            return f"{v}T00:00:00Z"
        if len(v) == 16 and "T" in v:  # <input type=datetime-local>
            return f"{v}:00Z"
        return v
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{value.isoformat()}T00:00:00Z"


def _props(d: Mapping[str, Any]) -> dict[str, Any]:
    return dict(d.get("properties") or {})


@dataclass
class ArmResource:
    """Fields shared by every ARM resource."""

    id: str | None = None
    name: str | None = None
    type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        """JSON-serialisable dict for UIs/exports (not the ARM request body)."""
        d = asdict(self)
        d.pop("raw", None)
        out: dict[str, Any] = _jsonable(d)
        return out


def _jsonable(o: Any) -> Any:
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


# ===================================================================== tabular data
@dataclass
class Column:
    name: str
    type: str = "string"


@dataclass
class Table:
    """Tabular result shared by Cost Management, Log Analytics and metrics.

    Iterating yields one ``dict`` per row. Convert with :meth:`to_records`,
    :meth:`to_pandas`, :meth:`to_csv` or :meth:`to_json`.
    """

    columns: list[Column]
    rows: list[list[Any]]
    name: str = "PrimaryResult"

    def __iter__(self) -> Iterator[dict[str, Any]]:
        names = self.column_names
        for row in self.rows:
            yield dict(zip(names, row))

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_records(self) -> list[dict[str, Any]]:
        return list(self)

    def to_pandas(self) -> Any:
        try:
            import pandas as pd
        except ImportError as exc:
            raise DependencyMissingError("pip install 'foundry-finops[pandas]'") from exc
        return pd.DataFrame(self.rows, columns=self.column_names)

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(self.column_names)
        writer.writerows(
            [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in row] for row in self.rows
        )
        return buf.getvalue()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [asdict(c) for c in self.columns],
            "rows": _jsonable(self.rows),
        }

    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]], name: str = "PrimaryResult") -> Table:
        names: list[str] = []
        for r in records:
            for k in r:
                if k not in names:
                    names.append(k)
        cols = [Column(n, _infer_type([r.get(n) for r in records])) for n in names]
        return cls(cols, [[r.get(n) for n in names] for r in records], name)


def _infer_type(values: Sequence[Any]) -> str:
    sample = next((v for v in values if v is not None), None)
    if isinstance(sample, bool):
        return "bool"
    if isinstance(sample, int):
        return "long"
    if isinstance(sample, float):
        return "real"
    if isinstance(sample, (datetime, date)):
        return "datetime"
    return "string"


# ================================================================ Foundry cost controls
class BudgetCategory(_StrEnum):
    COST = "Cost"  #: spending budget in USD
    TOKENS = "Tokens"


class TimeGrain(_StrEnum):
    DAILY = "Daily"
    WEEKLY = "Weekly"
    MONTHLY = "Monthly"
    QUARTERLY = "Quarterly"  # Cost Management budgets only
    ANNUALLY = "Annually"  # Cost Management budgets only


class ThresholdType(_StrEnum):
    PERCENTAGE = "Percentage"
    ABSOLUTE = "Absolute"


class CostControlFilterName(_StrEnum):
    PROJECT = "project"
    DEPLOYMENT = "deployment"
    AGENT = "agent"
    SESSION_ID = "sessionId"
    TOKEN_TYPE = "tokenType"  # values: input / output (Tokens budgets only)


class BudgetSharing(_StrEnum):
    """How matching usage shares the budget (the ``grouping`` field)."""

    SHARED = "shared"  #: one budget for everything (grouping omitted)
    PER_AGENT = "agent"
    PER_SESSION = "sessionId"
    PER_PROJECT = "project"  # Foundry-resource cost controls only


@dataclass
class Threshold:
    """A ``Send alert`` or ``Block usage`` threshold."""

    threshold: float
    threshold_type: ThresholdType = ThresholdType.PERCENTAGE
    name: str | None = None
    #: Alerts only: resource ID of the Foundry connection to an Event Grid topic.
    event_grid_connection_id: str | None = None
    enabled: bool = True

    def _default_name(self, kind: str) -> str:
        unit = "Percent" if self.threshold_type == ThresholdType.PERCENTAGE else "Absolute"
        prefix = "Actual" if kind == "alert" else "Block"
        return self.name or f"{prefix}_GreaterThanOrEqualTo_{self.threshold:g}_{unit}"


@dataclass
class CostControl(ArmResource):
    """A Foundry cost control (``Microsoft.CognitiveServices/accounts[/projects]/costControls``).

    Example – $500/month per agent in the ``fraud-risk`` project, alert at 80 %, block at 100 %::

        CostControl(
            name="fraud-risk-monthly",
            category=BudgetCategory.COST,
            amount=500,
            time_grain=TimeGrain.MONTHLY,
            start_date="2026-12-01T00:00:00Z",
            end_date="2027-07-01T00:00:00Z",
            filters={"project": ["fraud-risk"]},
            sharing=BudgetSharing.PER_AGENT,
            alert=Threshold(80, event_grid_connection_id=conn_id),
            block=Threshold(100),
        )
    """

    category: BudgetCategory = BudgetCategory.COST
    amount: float = 0
    time_grain: TimeGrain = TimeGrain.MONTHLY
    start_date: str | None = None
    end_date: str | None = None
    #: filter name -> values (names, not resource IDs). Combined with AND.
    filters: dict[str, list[str]] = field(default_factory=dict)
    sharing: BudgetSharing = BudgetSharing.SHARED
    alert: Threshold | None = None
    block: Threshold | None = None
    #: Owner scope – populated from the ID: ``account`` or ``project``.
    project: str | None = None

    # ------------------------------------------------------------- validation
    def validate(self) -> None:
        import re

        if self.name and not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", self.name):
            raise ValueError("Cost control name must be 1-63 letters, numbers, hyphens or underscores")
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if self.time_grain in (TimeGrain.QUARTERLY, TimeGrain.ANNUALLY):
            raise ValueError("Cost controls reset Daily, Weekly or Monthly; Quarterly/Annually are for budgets")
        if self.category == BudgetCategory.TOKENS and int(self.amount) != self.amount:
            raise ValueError("Token limits must be whole numbers")
        if "agent" in self.filters and len(self.filters.get("project", [])) != 1:
            raise ValueError("An agent filter needs a project filter with exactly one project")
        if "tokenType" in self.filters and self.category != BudgetCategory.TOKENS:
            raise ValueError("tokenType filters apply only to Tokens budgets")
        if len(self.filters.get("sessionId", [])) > 20:
            raise ValueError("A session filter accepts up to 20 session IDs")
        if self.project and self.sharing == BudgetSharing.PER_PROJECT:
            raise ValueError("Only a Foundry resource's cost controls can use a per-project budget")
        for t in (self.alert, self.block):
            if t and t.threshold_type == ThresholdType.PERCENTAGE and not 0 < t.threshold <= 100:
                raise ValueError("Percentage thresholds must be between 0 and 100")
            if t and t.threshold_type == ThresholdType.ABSOLUTE and t.threshold > self.amount:
                raise ValueError("Absolute thresholds can't exceed the amount")
        if self.alert and not self.alert.event_grid_connection_id:
            raise ValueError("An alert threshold needs event_grid_connection_id")
        if self.alert and self.block and _pct(self.alert, self.amount) > _pct(self.block, self.amount):
            raise ValueError("The alert threshold must be at or below the block threshold")

    # ---------------------------------------------------------------- mapping
    def to_dict(self) -> dict[str, Any]:
        self.validate()
        props: dict[str, Any] = {
            "category": str(self.category),
            "amount": int(self.amount) if self.category == BudgetCategory.TOKENS else self.amount,
            "timeGrain": str(self.time_grain),
        }
        if self.start_date or self.end_date:
            props["timePeriod"] = {
                k: iso_utc(v)
                for k, v in (("startDate", self.start_date), ("endDate", self.end_date))
                if v is not None
            }
        conditions = [
            {"additionalInfo": {"name": str(k), "operator": "In", "values": list(v)}}
            for k, v in self.filters.items()
            if v
        ]
        if len(conditions) == 1:
            props["filter"] = conditions[0]
        elif conditions:
            props["filter"] = {"and": conditions}
        if self.sharing != BudgetSharing.SHARED:
            props["grouping"] = [{"name": str(self.sharing), "type": "AdditionalInfoKey"}]
        if self.alert:
            props["notifications"] = {
                self.alert._default_name("alert"): {
                    "enabled": self.alert.enabled,
                    "operator": "GreaterThanOrEqualTo",
                    "threshold": self.alert.threshold,
                    "thresholdType": str(self.alert.threshold_type),
                    "eventGridConnectionId": self.alert.event_grid_connection_id,
                }
            }
        if self.block:
            props["providerProperties"] = {
                "actions": {
                    self.block._default_name("block"): {
                        "type": "Block",
                        "operator": "GreaterThanOrEqualTo",
                        "threshold": self.block.threshold,
                        "thresholdType": str(self.block.threshold_type),
                    }
                }
            }
        return {"properties": props}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CostControl:
        p = _props(d)
        filters: dict[str, list[str]] = {}
        f = p.get("filter") or {}
        for cond in f.get("and", [f] if f else []):
            info = cond.get("additionalInfo") or {}
            if info.get("name"):
                filters[info["name"]] = list(info.get("values") or [])
        grouping = (p.get("grouping") or [{}])[0].get("name")
        alert = block = None
        for key, n in (p.get("notifications") or {}).items():
            alert = Threshold(
                float(n.get("threshold", 0)),
                _enum(ThresholdType, n.get("thresholdType"), ThresholdType.PERCENTAGE),
                key,
                n.get("eventGridConnectionId"),
                bool(n.get("enabled", True)),
            )
            break
        for key, a in ((p.get("providerProperties") or {}).get("actions") or {}).items():
            block = Threshold(
                float(a.get("threshold", 0)), _enum(ThresholdType, a.get("thresholdType"), ThresholdType.PERCENTAGE), key
            )
            break
        tp = p.get("timePeriod") or {}
        rid = d.get("id") or ""
        project = None
        if "/projects/" in rid:
            project = rid.split("/projects/")[1].split("/")[0]
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            type=d.get("type"),
            raw=dict(d),
            category=_enum(BudgetCategory, p.get("category"), BudgetCategory.COST),
            amount=float(p.get("amount", 0) or 0),
            time_grain=_enum(TimeGrain, p.get("timeGrain"), TimeGrain.MONTHLY),
            start_date=tp.get("startDate"),
            end_date=tp.get("endDate"),
            filters=filters,
            sharing=_enum(BudgetSharing, grouping, BudgetSharing.SHARED),
            alert=alert,
            block=block,
            project=project,
        )


def _enum(cls: Any, value: Any, default: Any) -> Any:
    """Parse a service value into an enum, falling back so a new service value can't break listing."""
    try:
        return cls(value) if value else default
    except ValueError:
        return default


def _pct(t: Threshold, amount: float) -> float:
    return t.threshold if t.threshold_type == ThresholdType.PERCENTAGE else t.threshold / amount * 100


@dataclass
class CostControlAlertEvent:
    """Parsed ``costControl.alert`` CloudEvent delivered through Event Grid."""

    source: str
    time: str
    subject: str
    unit: str
    threshold: str
    action: str
    severity: str
    total_consumed: float
    limit: float
    remaining: float
    window_end: str | None = None
    project_name: str | None = None
    agent_name: str | None = None
    model: str | None = None
    deployment_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def dedupe_key(self) -> tuple[Any, ...]:
        """Foundry sends one event per request over a threshold – notify once per key."""
        return (self.source, self.limit, self.threshold, self.window_end, self.agent_name, self.project_name)

    @classmethod
    def from_cloud_event(cls, event: Mapping[str, Any]) -> CostControlAlertEvent:
        if event.get("type") != "costControl.alert":
            raise ValueError(f"Not a cost control alert: {event.get('type')!r}")
        d = dict(event.get("data") or {})
        return cls(
            source=event.get("source", ""),
            time=event.get("time", ""),
            subject=event.get("subject", ""),
            unit=d.get("unit", ""),
            threshold=str(d.get("threshold", "")),
            action=d.get("action", ""),
            severity=d.get("severity", ""),
            total_consumed=float(d.get("totalConsumed", 0) or 0),
            limit=float(d.get("limit", 0) or 0),
            remaining=float(d.get("remaining", 0) or 0),
            window_end=d.get("windowEnd"),
            project_name=d.get("projectName"),
            agent_name=d.get("agentName"),
            model=d.get("model"),
            deployment_name=d.get("deploymentName"),
            data=d,
        )


# ========================================================== Cost Management budgets
@dataclass
class BudgetNotification:
    threshold: float
    contact_emails: list[str] = field(default_factory=list)
    contact_groups: list[str] = field(default_factory=list)  #: action group resource IDs
    contact_roles: list[str] = field(default_factory=list)
    threshold_type: str = "Actual"  #: Actual | Forecasted
    operator: str = "GreaterThan"  #: EqualTo | GreaterThan | GreaterThanOrEqualTo
    enabled: bool = True
    locale: str = "en-us"
    name: str | None = None

    def key(self) -> str:
        return self.name or f"{self.threshold_type}_{self.operator}_{self.threshold:g}_Percent"


@dataclass
class Budget(ArmResource):
    """An Azure Cost Management budget. Alerts on billed cost; it can't block requests."""

    amount: float = 0
    time_grain: TimeGrain = TimeGrain.MONTHLY
    start_date: str | None = None
    end_date: str | None = None
    #: Cost Management filter expression (``{"and": [{"tags": {...}}, {"dimensions": {...}}]}``).
    filter: dict[str, Any] | None = None
    notifications: list[BudgetNotification] = field(default_factory=list)
    current_spend: float | None = None
    forecast_spend: float | None = None
    currency: str | None = None
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if not self.name:
            raise ValueError("budget.name is required")
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        for kind in ("Actual", "Forecasted"):
            if sum(1 for n in self.notifications if n.threshold_type == kind) > 5:
                raise ValueError(f"A budget allows at most 5 {kind} notifications")
        for n in self.notifications:
            if not (n.contact_emails or n.contact_groups or n.contact_roles):
                raise ValueError(f"Notification at {n.threshold:g}% needs an email, action group or role to notify")
            if n.threshold_type not in ("Actual", "Forecasted"):
                raise ValueError("Notification threshold_type must be 'Actual' or 'Forecasted'")
        if not self.start_date:
            today = datetime.now(timezone.utc).date()
            self.start_date = iso_utc(date(today.year, today.month, 1))
        props: dict[str, Any] = {
            "category": "Cost",
            "amount": self.amount,
            "timeGrain": str(self.time_grain),
            "timePeriod": {"startDate": iso_utc(self.start_date)},
        }
        if self.end_date:
            props["timePeriod"]["endDate"] = iso_utc(self.end_date)
        if self.filter:
            props["filter"] = self.filter
        if self.notifications:
            props["notifications"] = {
                n.key(): {
                    "enabled": n.enabled,
                    "operator": n.operator,
                    "threshold": n.threshold,
                    "thresholdType": n.threshold_type,
                    "contactEmails": n.contact_emails,
                    "contactGroups": n.contact_groups,
                    "contactRoles": n.contact_roles,
                    "locale": n.locale,
                }
                for n in self.notifications
            }
        body: dict[str, Any] = {"properties": props}
        if self.etag:
            body["eTag"] = self.etag
        return body

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Budget:
        p = _props(d)
        tp = p.get("timePeriod") or {}
        notes = [
            BudgetNotification(
                threshold=float(n.get("threshold", 0)),
                contact_emails=list(n.get("contactEmails") or []),
                contact_groups=list(n.get("contactGroups") or []),
                contact_roles=list(n.get("contactRoles") or []),
                threshold_type=n.get("thresholdType", "Actual"),
                operator=n.get("operator", "GreaterThan"),
                enabled=bool(n.get("enabled", True)),
                locale=n.get("locale", "en-us"),
                name=k,
            )
            for k, n in (p.get("notifications") or {}).items()
        ]
        cs = p.get("currentSpend") or {}
        fs = p.get("forecastSpend") or {}
        grain = p.get("timeGrain", "Monthly")
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            type=d.get("type"),
            raw=dict(d),
            amount=float(p.get("amount", 0) or 0),
            time_grain=TimeGrain(grain) if grain in TimeGrain._value2member_map_ else TimeGrain.MONTHLY,
            start_date=tp.get("startDate"),
            end_date=tp.get("endDate"),
            filter=p.get("filter"),
            notifications=notes,
            current_spend=cs.get("amount"),
            forecast_spend=fs.get("amount"),
            currency=cs.get("unit"),
            etag=d.get("eTag"),
        )


# ======================================================== Cost Management queries
class Granularity(_StrEnum):
    NONE = "None"
    DAILY = "Daily"
    MONTHLY = "Monthly"


@dataclass
class CostQuery:
    """Builder for a Cost Management ``query`` request.

    >>> q = (CostQuery.month_to_date()
    ...        .group_by_tag("project")
    ...        .group_by("MeterSubCategory")
    ...        .where_tag("deployment", ["gpt-5.4-mini"]))
    """

    timeframe: str = "MonthToDate"
    start: str | None = None
    end: str | None = None
    granularity: Granularity = Granularity.DAILY
    cost_type: str = "ActualCost"  #: ActualCost | AmortizedCost | Usage
    metric: str = "Cost"  #: Cost | PreTaxCost | CostUSD | PreTaxCostUSD
    grouping: list[dict[str, str]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)

    # constructors
    @classmethod
    def month_to_date(cls) -> CostQuery:
        return cls()

    @classmethod
    def between(cls, start: date | datetime | str, end: date | datetime | str) -> CostQuery:
        return cls(timeframe="Custom", start=iso_utc(start), end=iso_utc(end))

    @classmethod
    def last_days(cls, days: int) -> CostQuery:
        from datetime import timedelta

        if days < 1:
            raise ValueError("days must be at least 1")
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        # `days` whole calendar days ending with today (a 23:59:59 start would drop most of the first day)
        return cls.between(today - timedelta(days=days - 1), today.replace(hour=23, minute=59, second=59))

    # fluent modifiers
    def daily(self) -> CostQuery:
        self.granularity = Granularity.DAILY
        return self

    def total(self) -> CostQuery:
        self.granularity = Granularity.NONE
        return self

    def amortized(self) -> CostQuery:
        self.cost_type = "AmortizedCost"
        return self

    def group_by(self, dimension: str) -> CostQuery:
        """Group by a dimension: ResourceId, ResourceGroupName, MeterCategory, MeterSubCategory, Meter, …"""
        self.grouping.append({"type": "Dimension", "name": dimension})
        return self

    def group_by_tag(self, tag: str) -> CostQuery:
        """Group by a tag such as ``project``, ``deployment`` or ``agent.name`` (Foundry auto-tags)."""
        self.grouping.append({"type": "TagKey", "name": tag})
        return self

    def where(self, dimension: str, values: Sequence[str]) -> CostQuery:
        self.filters.append({"dimensions": {"name": dimension, "operator": "In", "values": list(values)}})
        return self

    def where_tag(self, tag: str, values: Sequence[str]) -> CostQuery:
        self.filters.append({"tags": {"name": tag, "operator": "In", "values": list(values)}})
        return self

    def to_dict(self) -> dict[str, Any]:
        dataset: dict[str, Any] = {
            "granularity": str(self.granularity),
            "aggregation": {"totalCost": {"name": self.metric, "function": "Sum"}},
        }
        if self.grouping:
            dataset["grouping"] = self.grouping
        if len(self.filters) == 1:
            dataset["filter"] = self.filters[0]
        elif self.filters:
            dataset["filter"] = {"and": self.filters}
        body: dict[str, Any] = {"type": self.cost_type, "timeframe": self.timeframe, "dataset": dataset}
        if self.timeframe == "Custom":
            body["timePeriod"] = {"from": self.start, "to": self.end}
        return body


# ============================================================= Foundry resources
@dataclass
class FoundryAccount(ArmResource):
    location: str | None = None
    kind: str | None = None
    sku: str | None = None
    endpoint: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FoundryAccount:
        p = _props(d)
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            d.get("location"), d.get("kind"), (d.get("sku") or {}).get("name"),
            p.get("endpoint"), dict(d.get("tags") or {}),
        )


@dataclass
class FoundryProject(ArmResource):
    location: str | None = None
    display_name: str | None = None
    description: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FoundryProject:
        p = _props(d)
        name = d.get("name") or ""
        return cls(
            d.get("id"), name.split("/")[-1], d.get("type"), dict(d),
            d.get("location"), p.get("displayName"), p.get("description"), dict(d.get("tags") or {}),
        )


@dataclass
class Deployment(ArmResource):
    """A model deployment. ``capacity`` is the TPM allocation in thousands for Standard SKUs."""

    model_name: str | None = None
    model_version: str | None = None
    model_format: str | None = None
    sku_name: str | None = None
    capacity: int | None = None
    rai_policy: str | None = None
    provisioning_state: str | None = None
    rate_limits: list[dict[str, Any]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def tokens_per_minute(self) -> int | None:
        for rl in self.rate_limits:
            if rl.get("key") == "token":
                return int(rl.get("count", 0))
        return self.capacity * 1000 if self.capacity is not None and self.sku_name != "ProvisionedManaged" else None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Deployment:
        p = _props(d)
        m = p.get("model") or {}
        sku = d.get("sku") or {}
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            m.get("name"), m.get("version"), m.get("format"),
            sku.get("name"), sku.get("capacity"), p.get("raiPolicyName"),
            p.get("provisioningState"), list(p.get("rateLimits") or []), dict(d.get("tags") or {}),
        )


@dataclass
class QuotaUsage:
    """One row of ``Microsoft.CognitiveServices/locations/usages`` (subscription quota)."""

    name: str
    display_name: str
    current_value: float
    limit: float
    unit: str

    @property
    def available(self) -> float:
        return max(self.limit - self.current_value, 0)

    @property
    def percent_used(self) -> float:
        return round(self.current_value / self.limit * 100, 2) if self.limit else 0.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(available=self.available, percent_used=self.percent_used)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> QuotaUsage:
        n = d.get("name") or {}
        return cls(
            n.get("value", ""), n.get("localizedValue", n.get("value", "")),
            float(d.get("currentValue", 0) or 0), float(d.get("limit", 0) or 0), d.get("unit", ""),
        )


# ========================================================================= metrics
@dataclass
class MetricPoint:
    timestamp: str
    value: float | None


@dataclass
class MetricSeries:
    metric: str
    unit: str
    dimensions: dict[str, str]
    points: list[MetricPoint]

    @property
    def total(self) -> float:
        return sum(p.value or 0 for p in self.points)


def metric_series_to_table(series: Sequence[MetricSeries]) -> Table:
    dim_names = sorted({k for s in series for k in s.dimensions})
    cols = [Column("TimeGenerated", "datetime"), Column("Metric")]
    cols += [Column(d) for d in dim_names] + [Column("Value", "real")]
    rows = [
        [p.timestamp, s.metric, *[s.dimensions.get(d) for d in dim_names], p.value]
        for s in series
        for p in s.points
    ]
    return Table(cols, rows, "Metrics")


# ===================================================================== alerts
@dataclass
class MetricAlert(ArmResource):
    """Azure Monitor metric alert, e.g. on ``ProcessedPromptTokens`` per deployment."""

    description: str = ""
    severity: int = 2
    enabled: bool = True
    scopes: list[str] = field(default_factory=list)
    metric_name: str = "TokenTransaction"
    metric_namespace: str = "Microsoft.CognitiveServices/accounts"
    operator: str = "GreaterThan"
    threshold: float = 0
    time_aggregation: str = "Total"
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    window_size: str = "PT1H"
    evaluation_frequency: str = "PT15M"
    action_group_ids: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        crit = {
            "criterionType": "StaticThresholdCriterion",
            "name": "c1",
            "metricName": self.metric_name,
            "metricNamespace": self.metric_namespace,
            "operator": self.operator,
            "threshold": self.threshold,
            "timeAggregation": self.time_aggregation,
            "dimensions": self.dimensions,
        }
        return {
            "location": "global",
            "tags": self.tags,
            "properties": {
                "description": self.description,
                "severity": self.severity,
                "enabled": self.enabled,
                "scopes": self.scopes,
                "evaluationFrequency": self.evaluation_frequency,
                "windowSize": self.window_size,
                "criteria": {
                    "odata.type": "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria",
                    "allOf": [crit],
                },
                "autoMitigate": True,
                "actions": [{"actionGroupId": a} for a in self.action_group_ids],
            },
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> MetricAlert:
        p = _props(d)
        crit = ((p.get("criteria") or {}).get("allOf") or [{}])[0]
        return cls(
            id=d.get("id"), name=d.get("name"), type=d.get("type"), raw=dict(d),
            description=p.get("description", ""), severity=int(p.get("severity", 2)),
            enabled=bool(p.get("enabled", True)), scopes=list(p.get("scopes") or []),
            metric_name=crit.get("metricName", ""), metric_namespace=crit.get("metricNamespace", ""),
            operator=crit.get("operator", "GreaterThan"), threshold=float(crit.get("threshold", 0) or 0),
            time_aggregation=crit.get("timeAggregation", "Total"), dimensions=list(crit.get("dimensions") or []),
            window_size=p.get("windowSize", "PT1H"), evaluation_frequency=p.get("evaluationFrequency", "PT15M"),
            action_group_ids=[a.get("actionGroupId") for a in p.get("actions") or [] if a.get("actionGroupId")],
            tags=dict(d.get("tags") or {}),
        )


@dataclass
class ActionGroup(ArmResource):
    short_name: str = ""
    enabled: bool = True
    emails: dict[str, str] = field(default_factory=dict)  #: receiver name -> address
    webhooks: dict[str, str] = field(default_factory=dict)  #: receiver name -> URI
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": "Global",
            "tags": self.tags,
            "properties": {
                "groupShortName": (self.short_name or (self.name or "ag"))[:12],
                "enabled": self.enabled,
                "emailReceivers": [
                    {"name": n, "emailAddress": e, "useCommonAlertSchema": True} for n, e in self.emails.items()
                ],
                "webhookReceivers": [
                    {"name": n, "serviceUri": u, "useCommonAlertSchema": True} for n, u in self.webhooks.items()
                ],
            },
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ActionGroup:
        p = _props(d)
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            p.get("groupShortName", ""), bool(p.get("enabled", True)),
            {r["name"]: r.get("emailAddress", "") for r in p.get("emailReceivers") or []},
            {r["name"]: r.get("serviceUri", "") for r in p.get("webhookReceivers") or []},
            dict(d.get("tags") or {}),
        )


@dataclass
class LogAlert(ArmResource):
    """Scheduled query (log search) alert – e.g. KQL over App Insights token traces per team."""

    location: str = ""
    description: str = ""
    severity: int = 2
    enabled: bool = True
    scopes: list[str] = field(default_factory=list)
    query: str = ""
    operator: str = "GreaterThan"
    threshold: float = 0
    time_aggregation: str = "Count"
    metric_measure_column: str | None = None
    window_size: str = "PT1H"
    evaluation_frequency: str = "PT15M"
    action_group_ids: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        crit: dict[str, Any] = {
            "query": self.query,
            "timeAggregation": self.time_aggregation,
            "operator": self.operator,
            "threshold": self.threshold,
            "failingPeriods": {"numberOfEvaluationPeriods": 1, "minFailingPeriodsToAlert": 1},
        }
        if self.metric_measure_column:
            crit["metricMeasureColumn"] = self.metric_measure_column
        return {
            "location": self.location,
            "tags": self.tags,
            "kind": "LogAlert",
            "properties": {
                "description": self.description,
                "severity": self.severity,
                "enabled": self.enabled,
                "scopes": self.scopes,
                "evaluationFrequency": self.evaluation_frequency,
                "windowSize": self.window_size,
                "criteria": {"allOf": [crit]},
                "actions": {"actionGroups": self.action_group_ids},
            },
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LogAlert:
        p = _props(d)
        crit = ((p.get("criteria") or {}).get("allOf") or [{}])[0]
        return cls(
            id=d.get("id"), name=d.get("name"), type=d.get("type"), raw=dict(d),
            location=d.get("location", ""), description=p.get("description", ""),
            severity=int(p.get("severity", 2)), enabled=bool(p.get("enabled", True)),
            scopes=list(p.get("scopes") or []), query=crit.get("query", ""),
            operator=crit.get("operator", "GreaterThan"), threshold=float(crit.get("threshold", 0) or 0),
            time_aggregation=crit.get("timeAggregation", "Count"),
            metric_measure_column=crit.get("metricMeasureColumn"),
            window_size=p.get("windowSize", "PT1H"), evaluation_frequency=p.get("evaluationFrequency", "PT15M"),
            action_group_ids=list((p.get("actions") or {}).get("actionGroups") or []),
            tags=dict(d.get("tags") or {}),
        )


# ======================================================================= APIM
@dataclass
class ApimProduct(ArmResource):
    display_name: str = ""
    description: str = ""
    state: str = "published"
    subscription_required: bool = True
    approval_required: bool = False
    subscriptions_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "displayName": self.display_name or self.name,
            "description": self.description,
            "state": self.state,
            "subscriptionRequired": self.subscription_required,
        }
        if self.subscription_required:
            props["approvalRequired"] = self.approval_required
            if self.subscriptions_limit is not None:
                props["subscriptionsLimit"] = self.subscriptions_limit
        return {"properties": props}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ApimProduct:
        p = _props(d)
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            p.get("displayName", ""), p.get("description") or "", p.get("state", "published"),
            bool(p.get("subscriptionRequired", True)), bool(p.get("approvalRequired", False)),
            p.get("subscriptionsLimit"),
        )


@dataclass
class ApimSubscription(ArmResource):
    """APIM subscription – the usual unit for per-team / per-app token limits and chargeback."""

    display_name: str = ""
    scope: str = ""  #: e.g. ``/products/{productId}`` or ``/apis/{apiId}`` or ``/apis``
    state: str = "active"
    owner_id: str | None = None
    allow_tracing: bool = False

    def to_dict(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "displayName": self.display_name or self.name,
            "scope": self.scope,
            "state": self.state,
            "allowTracing": self.allow_tracing,
        }
        if self.owner_id:
            props["ownerId"] = self.owner_id
        return {"properties": props}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ApimSubscription:
        p = _props(d)
        scope = p.get("scope", "")
        if "/products/" in scope:
            scope = "/products/" + scope.split("/products/")[1]
        elif "/apis/" in scope:
            scope = "/apis/" + scope.split("/apis/")[1]
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            p.get("displayName", ""), scope, p.get("state", "active"), p.get("ownerId"),
            bool(p.get("allowTracing", False)),
        )


@dataclass
class ApimApi(ArmResource):
    display_name: str = ""
    path: str = ""
    service_url: str | None = None
    api_type: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ApimApi:
        p = _props(d)
        return cls(
            d.get("id"), d.get("name"), d.get("type"), dict(d),
            p.get("displayName", ""), p.get("path", ""), p.get("serviceUrl"), p.get("type"),
        )


def dumps(obj: Any) -> str:
    """``json.dumps`` that understands the models in this module."""
    if hasattr(obj, "to_json"):
        obj = obj.to_json()
    return json.dumps(_jsonable(obj), indent=2, default=str)
