"""The :class:`FinOpsClient` facade."""

from __future__ import annotations

from functools import cached_property
from typing import Any

import requests

from ._auth import TokenCredential, default_credential
from ._config import PUBLIC_CLOUD, ApiVersions, AzureCloud
from ._transport import RetryPolicy, Transport
from .operations import (
    AlertOperations,
    ApimOperations,
    CostControlOperations,
    CostManagementOperations,
    FoundryOperations,
    LogsOperations,
    MetricsOperations,
    PricingOperations,
    QuotaOperations,
    TagOperations,
)


class FinOpsClient:
    """Entry point for every Foundry FinOps REST operation.

    Args:
        subscription_id: Azure subscription ID.
        credential: Any ``TokenCredential``; defaults to ``DefaultAzureCredential()``.
        resource_group: Default resource group for operations that need one.
        cloud: :data:`~foundry_finops.PUBLIC_CLOUD` (default), ``US_GOV_CLOUD`` or ``CHINA_CLOUD``.
        api_versions: Override REST API versions per operation group.
        retry: Retry policy for 408/429/5xx (cost-control blocks are never retried).
        timeout: Per-request timeout in seconds.
        session: Optional ``requests.Session`` for proxies or testing.

    Example::

        from foundry_finops import FinOpsClient, CostQuery

        with FinOpsClient("<subscription-id>", resource_group="rg-ai") as fo:
            for cc in fo.cost_controls.list("contoso-foundry"):
                print(cc.name, cc.amount, cc.time_grain)
            by_project = fo.cost.cost_by("project", scope=fo.foundry.account_id("contoso-foundry"))
            print(by_project.to_pandas())

    Operation groups:

    =====================  ==========================================================
    ``foundry``            accounts, projects, deployments (capacity), connections
    ``cost_controls``      Foundry cost controls – real-time budgets that alert/block
    ``cost``               Cost Management query, forecast, budgets, exports
    ``metrics``            Azure Monitor token/request metrics
    ``logs``               KQL against Log Analytics / Application Insights
    ``quota``              Regional model quota (TPM) usage
    ``tags``               ARM tags on any scope
    ``alerts``             Metric alerts, log alerts, action groups
    ``apim``               APIM products, subscriptions, token-limit/metric policies
    ``pricing``            Retail prices and token cost estimates
    =====================  ==========================================================
    """

    def __init__(
        self,
        subscription_id: str,
        credential: TokenCredential | None = None,
        *,
        resource_group: str | None = None,
        cloud: AzureCloud = PUBLIC_CLOUD,
        api_versions: ApiVersions | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        if not subscription_id:
            raise ValueError("subscription_id is required")
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.cloud = cloud
        self.api_versions = api_versions or ApiVersions()
        self._transport = Transport(
            credential or default_credential(),
            session=session or requests.Session(),
            retry=retry or RetryPolicy(),
            timeout=timeout,
        )

    def _ops(self, cls: type[Any]) -> Any:
        return cls(
            self._transport,
            subscription_id=self.subscription_id,
            resource_group=self.resource_group,
            cloud=self.cloud,
            api_versions=self.api_versions,
        )

    @cached_property
    def foundry(self) -> FoundryOperations:
        return self._ops(FoundryOperations)  # type: ignore[no-any-return]

    @cached_property
    def cost_controls(self) -> CostControlOperations:
        return self._ops(CostControlOperations)  # type: ignore[no-any-return]

    @cached_property
    def cost(self) -> CostManagementOperations:
        return self._ops(CostManagementOperations)  # type: ignore[no-any-return]

    @cached_property
    def metrics(self) -> MetricsOperations:
        return self._ops(MetricsOperations)  # type: ignore[no-any-return]

    @cached_property
    def logs(self) -> LogsOperations:
        return self._ops(LogsOperations)  # type: ignore[no-any-return]

    @cached_property
    def quota(self) -> QuotaOperations:
        return self._ops(QuotaOperations)  # type: ignore[no-any-return]

    @cached_property
    def tags(self) -> TagOperations:
        return self._ops(TagOperations)  # type: ignore[no-any-return]

    @cached_property
    def alerts(self) -> AlertOperations:
        return self._ops(AlertOperations)  # type: ignore[no-any-return]

    @cached_property
    def apim(self) -> ApimOperations:
        return self._ops(ApimOperations)  # type: ignore[no-any-return]

    @cached_property
    def pricing(self) -> PricingOperations:
        return self._ops(PricingOperations)  # type: ignore[no-any-return]

    def close(self) -> None:
        self._transport.session.close()

    def __enter__(self) -> FinOpsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"FinOpsClient(subscription_id={self.subscription_id!r}, resource_group={self.resource_group!r})"
