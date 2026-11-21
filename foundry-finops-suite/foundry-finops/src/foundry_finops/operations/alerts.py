"""Azure Monitor alerts: metric alerts (tokens), log search alerts (KQL) and action groups."""

from __future__ import annotations

from collections.abc import Iterator

from ..models import ActionGroup, LogAlert, MetricAlert
from ._base import _Operations


class AlertOperations(_Operations):
    """CRUD for alert rules and action groups in a resource group.

    Typical uses: alert when a deployment's hourly ``TokenTransaction`` exceeds a threshold,
    or when a team's tokens (KQL over traces) cross a limit. Budget alerts live on
    :class:`~foundry_finops.models.Budget`; real-time cost-control alerts go to Event Grid.
    """

    def _p(self, kind: str, resource_group: str | None) -> str:
        return f"{self.resource_group_scope(resource_group)}/providers/Microsoft.Insights/{kind}"

    # ----------------------------------------------------------- metric alerts
    def list_metric_alerts(self, resource_group: str | None = None) -> Iterator[MetricAlert]:
        for d in self._list(self._p("metricAlerts", resource_group), self._v.metric_alerts):
            yield MetricAlert.from_dict(d)

    def get_metric_alert(self, name: str, resource_group: str | None = None) -> MetricAlert:
        url = f"{self._p('metricAlerts', resource_group)}/{self._seg(name)}"
        return MetricAlert.from_dict(self._arm("GET", url, self._v.metric_alerts).body)

    def create_or_update_metric_alert(self, alert: MetricAlert, resource_group: str | None = None) -> MetricAlert:
        url = f"{self._p('metricAlerts', resource_group)}/{self._seg(alert.name)}"
        return MetricAlert.from_dict(self._arm("PUT", url, self._v.metric_alerts, body=alert.to_dict()).body)

    def delete_metric_alert(self, name: str, resource_group: str | None = None) -> None:
        self._arm("DELETE", f"{self._p('metricAlerts', resource_group)}/{self._seg(name)}", self._v.metric_alerts)

    # -------------------------------------------------------------- log alerts
    def list_log_alerts(self, resource_group: str | None = None) -> Iterator[LogAlert]:
        for d in self._list(self._p("scheduledQueryRules", resource_group), self._v.scheduled_query_rules):
            yield LogAlert.from_dict(d)

    def get_log_alert(self, name: str, resource_group: str | None = None) -> LogAlert:
        url = f"{self._p('scheduledQueryRules', resource_group)}/{self._seg(name)}"
        return LogAlert.from_dict(self._arm("GET", url, self._v.scheduled_query_rules).body)

    def create_or_update_log_alert(self, alert: LogAlert, resource_group: str | None = None) -> LogAlert:
        if not alert.location:
            raise ValueError("Log alerts need a location (the region of the target workspace)")
        url = f"{self._p('scheduledQueryRules', resource_group)}/{self._seg(alert.name)}"
        body = alert.to_dict()
        return LogAlert.from_dict(self._arm("PUT", url, self._v.scheduled_query_rules, body=body).body)

    def delete_log_alert(self, name: str, resource_group: str | None = None) -> None:
        url = f"{self._p('scheduledQueryRules', resource_group)}/{self._seg(name)}"
        self._arm("DELETE", url, self._v.scheduled_query_rules)

    # ----------------------------------------------------------- action groups
    def list_action_groups(self, resource_group: str | None = None) -> Iterator[ActionGroup]:
        for d in self._list(self._p("actionGroups", resource_group), self._v.action_groups):
            yield ActionGroup.from_dict(d)

    def create_or_update_action_group(self, group: ActionGroup, resource_group: str | None = None) -> ActionGroup:
        url = f"{self._p('actionGroups', resource_group)}/{self._seg(group.name)}"
        return ActionGroup.from_dict(self._arm("PUT", url, self._v.action_groups, body=group.to_dict()).body)

    def delete_action_group(self, name: str, resource_group: str | None = None) -> None:
        self._arm("DELETE", f"{self._p('actionGroups', resource_group)}/{self._seg(name)}", self._v.action_groups)
