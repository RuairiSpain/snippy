"""Foundry cost controls (preview) – real-time spending/token budgets that alert or block."""

from __future__ import annotations

from collections.abc import Iterator

from .._ids import foundry_account_id
from ..models import CostControl
from ._base import _Operations


class CostControlOperations(_Operations):
    """CRUD for ``Microsoft.CognitiveServices/accounts[/projects]/costControls``.

    Pass ``project=`` to work with a project's own cost controls; omit it for the Foundry
    resource's cost controls. Changes take effect within about 15 minutes.

    The service documents that concurrent creates on one Foundry resource can fail with
    HTTP 409, so create cost controls one at a time.
    """

    def _base(self, account: str, project: str | None, resource_group: str | None) -> str:
        rid = foundry_account_id(self._sub, self._rg_or_default(resource_group), account)
        if project:
            rid += f"/projects/{self._seg(project)}"
        return f"{rid}/costControls"

    def list(
        self, account: str, *, project: str | None = None, resource_group: str | None = None
    ) -> Iterator[CostControl]:
        for d in self._list(self._base(account, project, resource_group), self._v.cost_controls):
            yield CostControl.from_dict(d)

    def list_all(self, account: str, *, resource_group: str | None = None) -> Iterator[CostControl]:
        """Resource-level cost controls followed by every project's cost controls."""
        from .foundry import FoundryOperations

        yield from self.list(account, resource_group=resource_group)
        foundry = FoundryOperations(
            self._t, subscription_id=self._sub, resource_group=self._rg, cloud=self._cloud, api_versions=self._v
        )
        for proj in foundry.list_projects(account, resource_group):
            if proj.name:
                yield from self.list(account, project=proj.name, resource_group=resource_group)

    def get(
        self, account: str, name: str, *, project: str | None = None, resource_group: str | None = None
    ) -> CostControl:
        url = f"{self._base(account, project, resource_group)}/{self._seg(name)}"
        return CostControl.from_dict(self._arm("GET", url, self._v.cost_controls).body)

    def create_or_update(
        self,
        account: str,
        cost_control: CostControl,
        *,
        project: str | None = None,
        resource_group: str | None = None,
        only_if_new: bool = False,
    ) -> CostControl:
        """PUT a cost control. ``only_if_new=True`` sends ``If-None-Match: *`` (raises ConflictError if it exists)."""
        if not cost_control.name:
            raise ValueError("cost_control.name is required")
        cost_control.project = project or cost_control.project
        url = f"{self._base(account, cost_control.project, resource_group)}/{cost_control.name}"
        headers = {"If-None-Match": "*"} if only_if_new else None
        resp = self._arm("PUT", url, self._v.cost_controls, body=cost_control.to_dict(), headers=headers)
        return CostControl.from_dict(resp.body) if resp.body else self.get(
            account, cost_control.name, project=cost_control.project, resource_group=resource_group
        )

    def update(
        self,
        account: str,
        name: str,
        *,
        project: str | None = None,
        resource_group: str | None = None,
        **properties: object,
    ) -> CostControl:
        """PATCH only the given ARM properties, e.g. ``update(acct, "x", amount=750)``.

        Changing the reset period or sharing restarts the usage count. The budget type
        (``category``) can't be changed.
        """
        if "category" in properties:
            raise ValueError("A cost control's budget type can't be changed; create a new one")
        url = f"{self._base(account, project, resource_group)}/{self._seg(name)}"
        resp = self._arm("PATCH", url, self._v.cost_controls, body={"properties": properties})
        return CostControl.from_dict(resp.body) if resp.body else self.get(
            account, name, project=project, resource_group=resource_group
        )

    def delete(
        self, account: str, name: str, *, project: str | None = None, resource_group: str | None = None
    ) -> None:
        url = f"{self._base(account, project, resource_group)}/{self._seg(name)}"
        self._arm("DELETE", url, self._v.cost_controls)

    def find_event_grid_connection_id(self, account: str, *, resource_group: str | None = None) -> str | None:
        """Return an ``eventGridConnectionId`` already used by an existing cost control.

        Foundry creates this connection when you save an alert in the portal; the docs advise
        reusing it because connections you create yourself are silently skipped.
        """
        for cc in self.list_all(account, resource_group=resource_group):
            if cc.alert and cc.alert.event_grid_connection_id:
                return cc.alert.event_grid_connection_id
        return None
