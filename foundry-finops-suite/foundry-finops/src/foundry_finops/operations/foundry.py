"""Foundry (``Microsoft.CognitiveServices``) accounts, projects, deployments and connections."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .._ids import foundry_account_id, foundry_project_id
from ..models import Deployment, FoundryAccount, FoundryProject
from ._base import _Operations


class FoundryOperations(_Operations):
    """Discover Foundry resources and manage model deployments.

    Changing a deployment's ``capacity`` is how you raise or lower its TPM allocation
    (quota) – see :meth:`set_capacity`.
    """

    # ---------------------------------------------------------------- accounts
    def list_accounts(self, resource_group: str | None = None) -> Iterator[FoundryAccount]:
        """List Foundry (AIServices) resources in the subscription or one resource group."""
        base = self.resource_group_scope(resource_group) if (resource_group or self._rg) else self.subscription_scope
        for d in self._list(f"{base}/providers/Microsoft.CognitiveServices/accounts", self._v.cognitive_services):
            yield FoundryAccount.from_dict(d)

    def get_account(self, account: str, resource_group: str | None = None) -> FoundryAccount:
        rid = foundry_account_id(self._sub, self._rg_or_default(resource_group), account)
        return FoundryAccount.from_dict(self._arm("GET", rid, self._v.cognitive_services).body)

    def account_id(self, account: str, resource_group: str | None = None) -> str:
        return foundry_account_id(self._sub, self._rg_or_default(resource_group), account)

    # ---------------------------------------------------------------- projects
    def list_projects(self, account: str, resource_group: str | None = None) -> Iterator[FoundryProject]:
        rid = self.account_id(account, resource_group)
        for d in self._list(f"{rid}/projects", self._v.cognitive_services):
            yield FoundryProject.from_dict(d)

    def get_project(self, account: str, project: str, resource_group: str | None = None) -> FoundryProject:
        rid = foundry_project_id(self._sub, self._rg_or_default(resource_group), account, project)
        return FoundryProject.from_dict(self._arm("GET", rid, self._v.cognitive_services).body)

    # ------------------------------------------------------------- deployments
    def list_deployments(self, account: str, resource_group: str | None = None) -> Iterator[Deployment]:
        rid = self.account_id(account, resource_group)
        for d in self._list(f"{rid}/deployments", self._v.cognitive_services):
            yield Deployment.from_dict(d)

    def get_deployment(self, account: str, deployment: str, resource_group: str | None = None) -> Deployment:
        rid = f"{self.account_id(account, resource_group)}/deployments/{deployment}"
        return Deployment.from_dict(self._arm("GET", rid, self._v.cognitive_services).body)

    def create_or_update_deployment(
        self,
        account: str,
        deployment: str,
        *,
        model_name: str,
        model_version: str | None = None,
        model_format: str = "OpenAI",
        sku_name: str = "GlobalStandard",
        capacity: int = 10,
        rai_policy: str | None = None,
        tags: dict[str, str] | None = None,
        resource_group: str | None = None,
        wait: bool = True,
    ) -> Deployment:
        """Create or replace a model deployment. ``capacity`` is in thousands of TPM for Standard SKUs."""
        rid = f"{self.account_id(account, resource_group)}/deployments/{deployment}"
        model: dict[str, Any] = {"format": model_format, "name": model_name}
        if model_version:
            model["version"] = model_version
        props: dict[str, Any] = {"model": model}
        if rai_policy:
            props["raiPolicyName"] = rai_policy
        body = {"sku": {"name": sku_name, "capacity": capacity}, "properties": props, "tags": tags or {}}
        resp = self._arm("PUT", rid, self._v.cognitive_services, body=body)
        if wait:
            self._t.poll(resp, scope=self._cloud.arm_scope)
            return self.get_deployment(account, deployment, resource_group)
        return Deployment.from_dict(resp.body or {})

    def set_capacity(
        self, account: str, deployment: str, capacity: int, *, resource_group: str | None = None, wait: bool = True
    ) -> Deployment:
        """Change a deployment's capacity (TPM allocation) while keeping model, SKU and policy."""
        rid = f"{self.account_id(account, resource_group)}/deployments/{deployment}"
        current = self._arm("GET", rid, self._v.cognitive_services).body
        body = {
            "sku": {**(current.get("sku") or {}), "capacity": capacity},
            "properties": {
                k: v
                for k, v in (current.get("properties") or {}).items()
                if k in ("model", "raiPolicyName", "versionUpgradeOption", "currentCapacity", "parentDeploymentName")
                and k != "currentCapacity"
            },
            "tags": current.get("tags") or {},
        }
        resp = self._arm("PUT", rid, self._v.cognitive_services, body=body)
        if wait:
            self._t.poll(resp, scope=self._cloud.arm_scope)
        return self.get_deployment(account, deployment, resource_group)

    def delete_deployment(self, account: str, deployment: str, resource_group: str | None = None) -> None:
        rid = f"{self.account_id(account, resource_group)}/deployments/{deployment}"
        resp = self._arm("DELETE", rid, self._v.cognitive_services)
        self._t.poll(resp, scope=self._cloud.arm_scope)

    # ------------------------------------------------------------- connections
    def list_connections(self, account: str, resource_group: str | None = None) -> Iterator[dict[str, Any]]:
        """List account connections – used to find the Event Grid connection for cost-control alerts."""
        rid = self.account_id(account, resource_group)
        yield from self._list(f"{rid}/connections", self._v.cognitive_services)

    def list_event_grid_topics(self) -> Iterator[dict[str, Any]]:
        """Event Grid topics in the subscription (cost-control alerts need CloudEvents v1.0 schema)."""
        yield from self._list(
            f"{self.subscription_scope}/providers/Microsoft.EventGrid/topics", self._v.event_grid
        )
