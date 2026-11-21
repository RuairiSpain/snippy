"""Azure Monitor: platform metrics (near real-time tokens) and Log Analytics / App Insights KQL."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Column, MetricPoint, MetricSeries, Table, iso_utc, metric_series_to_table
from ._base import _Operations


class FoundryMetrics:
    """Metric names on ``Microsoft.CognitiveServices/accounts`` (see Foundry metrics reference)."""

    INPUT_TOKENS = "ProcessedPromptTokens"
    OUTPUT_TOKENS = "GeneratedTokens"
    TOTAL_TOKENS = "TokenTransaction"
    REQUESTS = "AzureOpenAIRequests"
    LATENCY_TTLT_MS = "AzureOpenAITTLTInMS"
    #: Generic Foundry Models metrics (non-OpenAI models)
    MODEL_INPUT_TOKENS = "InputTokens"
    MODEL_OUTPUT_TOKENS = "OutputTokens"
    MODEL_TOTAL_TOKENS = "TotalTokens"
    MODEL_REQUESTS = "ModelRequests"

    TOKENS = (INPUT_TOKENS, OUTPUT_TOKENS, TOTAL_TOKENS)
    DEPLOYMENT_DIMENSION = "ModelDeploymentName"
    MODEL_DIMENSION = "ModelName"


def _timespan(start: datetime | str | None, end: datetime | str | None, hours: int) -> str:
    end_dt = end or datetime.now(timezone.utc)
    start_dt = start or (end_dt - timedelta(hours=hours) if isinstance(end_dt, datetime) else None)
    return f"{iso_utc(start_dt)}/{iso_utc(end_dt)}"  # type: ignore[arg-type]


class MetricsOperations(_Operations):
    """Read Azure Monitor platform metrics for any resource (Foundry, APIM, …)."""

    def definitions(self, resource_id: str) -> list[dict[str, Any]]:
        url = f"{resource_id}/providers/Microsoft.Insights/metricDefinitions"
        return list((self._arm("GET", url, self._v.metrics).body or {}).get("value") or [])

    def query(
        self,
        resource_id: str,
        metrics: Sequence[str] = FoundryMetrics.TOKENS,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        hours: int = 24,
        interval: str = "PT1H",
        aggregation: str = "Total",
        split_by: Sequence[str] = (FoundryMetrics.DEPLOYMENT_DIMENSION,),
        filter: str | None = None,
        top: int = 100,
    ) -> list[MetricSeries]:
        """Return one :class:`MetricSeries` per metric × dimension combination."""
        params: dict[str, Any] = {
            "metricnames": ",".join(metrics),
            "timespan": _timespan(start, end, hours),
            "interval": interval,
            "aggregation": aggregation,
            "top": top,
        }
        flt = filter or " and ".join(f"{d} eq '*'" for d in split_by)
        if flt:
            params["$filter"] = flt
        url = f"{resource_id}/providers/Microsoft.Insights/metrics"
        body = self._arm("GET", url, self._v.metrics, params=params).body or {}
        agg_key = aggregation.split(",")[0].strip().lower()
        out: list[MetricSeries] = []
        for metric in body.get("value") or []:
            mname = (metric.get("name") or {}).get("value", "")
            for ts in metric.get("timeseries") or []:
                dims = {
                    (m.get("name") or {}).get("value", ""): m.get("value", "")
                    for m in ts.get("metadatavalues") or []
                }
                pts = [MetricPoint(p.get("timeStamp", ""), p.get(agg_key)) for p in ts.get("data") or []]
                out.append(MetricSeries(mname, metric.get("unit", ""), dims, pts))
        return out

    def query_table(self, resource_id: str, metrics: Sequence[str] = FoundryMetrics.TOKENS, **kwargs: Any) -> Table:
        """Same as :meth:`query` but flattened into a :class:`Table` (one row per point)."""
        return metric_series_to_table(self.query(resource_id, metrics, **kwargs))


class LogsOperations(_Operations):
    """Run KQL against a Log Analytics workspace or an Application Insights resource."""

    def _post(self, url: str, query: str, timespan: str | None, extra: dict[str, Any] | None = None) -> list[Table]:
        body: dict[str, Any] = {"query": query}
        if timespan:
            body["timespan"] = timespan
        if extra:
            body.update(extra)
        resp = self._t.request("POST", url, scope=self._cloud.logs_scope, json_body=body)
        tables = []
        for t in (resp.body or {}).get("tables") or []:
            cols = [Column(c.get("name", ""), c.get("type", "string")) for c in t.get("columns") or []]
            tables.append(Table(cols, list(t.get("rows") or []), t.get("name", "PrimaryResult")))
        return tables

    def query_workspace(
        self, workspace_id: str, query: str, *, timespan: str | None = "P1D",
        additional_workspaces: Sequence[str] = (),
    ) -> Table:
        """Query a workspace by its **workspace (customer) ID** GUID. ``timespan`` is ISO-8601 (``P7D``)."""
        extra = {"workspaces": list(additional_workspaces)} if additional_workspaces else None
        url = f"{self._cloud.log_analytics}/v1/workspaces/{workspace_id}/query"
        return _first(self._post(url, query, timespan, extra))

    def query_resource(self, resource_id: str, query: str, *, timespan: str | None = "P1D") -> Table:
        """Resource-centric query, e.g. against the Application Insights resource used for tracing."""
        url = f"{self._cloud.log_analytics}/v1{resource_id}/query"
        return _first(self._post(url, query, timespan))

    def query(self, target: str, query: str, *, timespan: str | None = "P1D") -> Table:
        """Dispatch to :meth:`query_resource` for ARM IDs, else :meth:`query_workspace`."""
        if target.startswith("/subscriptions/"):
            return self.query_resource(target, query, timespan=timespan)
        return self.query_workspace(target, query, timespan=timespan)


def _first(tables: list[Table]) -> Table:
    return tables[0] if tables else Table([], [])
