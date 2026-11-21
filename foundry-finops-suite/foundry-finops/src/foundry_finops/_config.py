"""Cloud endpoints and REST API versions.

Every API version is overridable so you can opt into newer (or preview) versions
without waiting for a new release of this package::

    from foundry_finops import ApiVersions, FinOpsClient
    client = FinOpsClient(cred, sub, api_versions=ApiVersions(cost_management="2026-06-01"))
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AzureCloud:
    """Endpoints for an Azure cloud."""

    name: str
    resource_manager: str
    log_analytics: str
    retail_prices: str = "https://prices.azure.com/api/retail/prices"

    @property
    def arm_scope(self) -> str:
        return self.resource_manager.rstrip("/") + "/.default"

    @property
    def logs_scope(self) -> str:
        return self.log_analytics.rstrip("/") + "/.default"


PUBLIC_CLOUD = AzureCloud(
    name="AzureCloud",
    resource_manager="https://management.azure.com",
    log_analytics="https://api.loganalytics.io",
)
US_GOV_CLOUD = AzureCloud(
    name="AzureUSGovernment",
    resource_manager="https://management.usgovcloudapi.net",
    log_analytics="https://api.loganalytics.us",
)
CHINA_CLOUD = AzureCloud(
    name="AzureChinaCloud",
    resource_manager="https://management.chinacloudapi.cn",
    log_analytics="https://api.loganalytics.azure.cn",
)


@dataclass(frozen=True)
class ApiVersions:
    """REST API versions used by each operation group."""

    #: Foundry cost controls (public preview).
    cost_controls: str = "2026-11-15-preview"
    #: Microsoft.CognitiveServices accounts, projects, deployments, usages, connections.
    cognitive_services: str = "2025-06-01"
    #: Microsoft.CostManagement query, forecast, exports and budgets.
    cost_management: str = "2025-03-01"
    #: Microsoft.Insights metrics and metric definitions.
    metrics: str = "2021-05-01"
    metric_alerts: str = "2018-03-01"
    action_groups: str = "2023-01-01"
    scheduled_query_rules: str = "2023-12-01"
    #: Microsoft.Resources tags.
    tags: str = "2021-04-01"
    resources: str = "2021-04-01"
    #: Microsoft.ApiManagement.
    api_management: str = "2024-05-01"
    #: Microsoft.EventGrid topics (cost-control alert destinations).
    event_grid: str = "2022-06-15"
    #: Azure Retail Prices API.
    retail_prices: str = "2023-01-01-preview"
