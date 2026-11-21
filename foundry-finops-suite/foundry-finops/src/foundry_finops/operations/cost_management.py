"""Azure Cost Management: billed cost queries, forecasts, budgets, exports and tag inheritance."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from ..models import Budget, Column, CostQuery, Table
from ._base import _Operations

#: Billing tags that Foundry stamps on model charges.
FOUNDRY_COST_TAGS = ("project", "deployment", "agent.name")


class CostManagementOperations(_Operations):
    """Billed cost (8–24 h latency). ``scope`` defaults to the client's resource group or subscription.

    Scopes can be a subscription, resource group, a single resource ID (e.g. the Foundry
    resource – required to filter by the ``project`` tag), management group or billing scope.
    """

    def _scope(self, scope: str | None) -> str:
        if scope:
            return scope if scope.startswith("/") else f"/{scope}"
        return self.resource_group_scope() if self._rg else self.subscription_scope

    # ------------------------------------------------------------------ query
    def query(self, query: CostQuery | Mapping[str, Any], *, scope: str | None = None) -> Table:
        """Run a Cost Management query and return every page as one :class:`Table`.

        ``UsageDate`` (``yyyymmdd``) is normalised to an ISO date string.
        """
        body = query.to_dict() if isinstance(query, CostQuery) else dict(query)
        url = f"{self._scope(scope)}/providers/Microsoft.CostManagement/query"
        resp = self._arm("POST", url, self._v.cost_management, body=body)
        props = (resp.body or {}).get("properties") or {}
        columns = [Column(c["name"], c.get("type", "String")) for c in props.get("columns") or []]
        rows: list[list[Any]] = list(props.get("rows") or [])
        next_link = props.get("nextLink")
        while next_link:
            page = self._t.request("POST", next_link, scope=self._cloud.arm_scope, json_body=body).body or {}
            pprops = page.get("properties") or {}
            rows.extend(pprops.get("rows") or [])
            next_link = pprops.get("nextLink")
        return _normalise_dates(Table(columns, rows, "Cost"))

    def forecast(
        self, query: CostQuery | Mapping[str, Any], *, scope: str | None = None, include_actual: bool = True
    ) -> Table:
        """Cost forecast for the query's time period (``timeframe`` should be ``Custom``)."""
        body = query.to_dict() if isinstance(query, CostQuery) else dict(query)
        body.setdefault("includeActualCost", include_actual)
        body.setdefault("includeFreshPartialCost", False)
        body["dataset"].pop("grouping", None)  # forecast doesn't support grouping
        url = f"{self._scope(scope)}/providers/Microsoft.CostManagement/forecast"
        props = (self._arm("POST", url, self._v.cost_management, body=body).body or {}).get("properties") or {}
        columns = [Column(c["name"], c.get("type", "String")) for c in props.get("columns") or []]
        return _normalise_dates(Table(columns, list(props.get("rows") or []), "Forecast"))

    # ------------------------------------------------------- Foundry shortcuts
    def cost_by(
        self,
        tag_or_dimension: str,
        *,
        scope: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        days: int = 30,
        daily: bool = True,
        dimension: bool | None = None,
    ) -> Table:
        """Cost grouped by a Foundry tag (``project``/``deployment``/``agent.name``/your tag) or dimension.

        To group by the ``project`` tag, pass the Foundry resource ID as ``scope``. A name that
        starts with a capital letter is treated as a dimension (``MeterSubCategory``); pass
        ``dimension=False`` for a tag whose name is capitalised, or ``True`` to force a dimension.
        """
        if bool(start) != bool(end):
            raise ValueError("Pass both start and end, or neither (then the last `days` days are used)")
        if not start and days < 1:
            raise ValueError("days must be at least 1")
        q = CostQuery.between(start, end) if start and end else CostQuery.last_days(days)
        q = q.daily() if daily else q.total()
        if dimension is None:  # Cost Management dimensions are PascalCase, Foundry/team tags are lower case
            dimension = tag_or_dimension[:1].isupper()
        q = q.group_by(tag_or_dimension) if dimension else q.group_by_tag(tag_or_dimension)
        return self.query(q, scope=scope)

    def dimensions(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        url = f"{self._scope(scope)}/providers/Microsoft.CostManagement/dimensions"
        return list((self._arm("GET", url, self._v.cost_management).body or {}).get("value") or [])

    # ---------------------------------------------------------------- budgets
    def _budgets(self, scope: str | None) -> str:
        return f"{self._scope(scope)}/providers/Microsoft.CostManagement/budgets"

    def list_budgets(self, *, scope: str | None = None) -> Iterator[Budget]:
        for d in self._list(self._budgets(scope), self._v.cost_management):
            yield Budget.from_dict(d)

    def get_budget(self, name: str, *, scope: str | None = None) -> Budget:
        return Budget.from_dict(self._arm("GET", f"{self._budgets(scope)}/{self._seg(name)}", self._v.cost_management).body)

    def create_or_update_budget(self, budget: Budget, *, scope: str | None = None) -> Budget:
        """Create or replace a budget. Up to 5 Actual and 5 Forecasted notifications.

        Pass ``budget.etag`` (from :meth:`get_budget`) for optimistic concurrency.
        """
        if not budget.name:
            raise ValueError("budget.name is required")
        url = f"{self._budgets(scope)}/{budget.name}"
        return Budget.from_dict(self._arm("PUT", url, self._v.cost_management, body=budget.to_dict()).body)

    def delete_budget(self, name: str, *, scope: str | None = None) -> None:
        self._arm("DELETE", f"{self._budgets(scope)}/{self._seg(name)}", self._v.cost_management)

    # ---------------------------------------------------------------- exports
    def _exports(self, scope: str | None) -> str:
        return f"{self._scope(scope)}/providers/Microsoft.CostManagement/exports"

    def list_exports(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        body = self._arm("GET", self._exports(scope), self._v.cost_management).body or {}
        return list(body.get("value") or [])

    def create_or_update_export(
        self,
        name: str,
        *,
        storage_account_id: str,
        container: str,
        root_folder: str = "foundry-finops",
        recurrence: str = "Daily",
        cost_type: str = "ActualCost",
        timeframe: str = "MonthToDate",
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        file_format: str = "Csv",
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Schedule a recurring cost export to a storage account (the recommended bulk-data path)."""
        from ..models import iso_utc

        now = datetime.now(timezone.utc)
        try:
            default_end = now.replace(year=now.year + 1)
        except ValueError:  # 29 February
            default_end = now.replace(year=now.year + 1, day=28)
        body = {
            "properties": {
                "schedule": {
                    "status": "Active",
                    "recurrence": recurrence,
                    "recurrencePeriod": {
                        "from": iso_utc(start or now),
                        "to": iso_utc(end or default_end),
                    },
                },
                "format": file_format,
                "deliveryInfo": {
                    "destination": {
                        "resourceId": storage_account_id,
                        "container": container,
                        "rootFolderPath": root_folder,
                    }
                },
                "definition": {"type": cost_type, "timeframe": timeframe, "dataSet": {"granularity": "Daily"}},
            }
        }
        return dict(self._arm("PUT", f"{self._exports(scope)}/{self._seg(name)}", self._v.cost_management, body=body).body or {})

    def run_export(self, name: str, *, scope: str | None = None) -> None:
        self._arm("POST", f"{self._exports(scope)}/{self._seg(name)}/run", self._v.cost_management)

    def export_history(self, name: str, *, scope: str | None = None) -> list[dict[str, Any]]:
        url = f"{self._exports(scope)}/{self._seg(name)}/runHistory"
        return list((self._arm("GET", url, self._v.cost_management).body or {}).get("value") or [])

    def delete_export(self, name: str, *, scope: str | None = None) -> None:
        self._arm("DELETE", f"{self._exports(scope)}/{self._seg(name)}", self._v.cost_management)

    # --------------------------------------------------------- tag inheritance
    def set_tag_inheritance(self, enabled: bool = True, *, prefer_container_tags: bool = False,
                            scope: str | None = None, api_version: str = "2022-10-05-preview") -> dict[str, Any]:
        """Apply subscription/resource-group tags to usage records (needed for team tags on RG/subscription).

        Only supported at EA enrollment/department, MCA billing profile and subscription scopes.
        """
        url = f"{self._scope(scope)}/providers/Microsoft.CostManagement/settings/taginheritance"
        if not enabled:
            self._arm("DELETE", url, api_version)
            return {}
        body = {"kind": "taginheritance", "properties": {"preferContainerTags": prefer_container_tags}}
        return dict(self._arm("PUT", url, api_version, body=body).body or {})


def _normalise_dates(table: Table) -> Table:
    for idx, col in enumerate(table.columns):
        if col.name in ("UsageDate", "BillingMonth") or col.name.lower() == "usagedate":
            for row in table.rows:
                v = row[idx]
                if isinstance(v, (int, float)) and v > 19000000:
                    s = str(int(v))
                    row[idx] = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                elif isinstance(v, str) and "T" in v:
                    row[idx] = v.split("T")[0]
            col.type = "datetime"
    return table
