"""foundry-finops – a Pythonic SDK over the REST APIs that track and control Microsoft Foundry spend.

Quick start::

    from foundry_finops import FinOpsClient
    fo = FinOpsClient("<subscription-id>", resource_group="rg-ai")
    fo.cost_controls.list("contoso-foundry")
"""

from ._auth import StaticTokenCredential, TokenCredential, default_credential
from ._config import CHINA_CLOUD, PUBLIC_CLOUD, US_GOV_CLOUD, ApiVersions, AzureCloud
from ._ids import ResourceId, apim_service_id, foundry_account_id, foundry_project_id
from ._transport import RetryPolicy
from ._version import __version__
from .client import FinOpsClient
from .errors import (
    AuthenticationError,
    BudgetExceededError,
    ConflictError,
    FinOpsError,
    ForbiddenError,
    HttpResponseError,
    NotFoundError,
    ServiceError,
    ThrottledError,
    is_budget_exceeded,
    raise_for_budget,
)
from .models import (
    ActionGroup,
    ApimProduct,
    ApimSubscription,
    Budget,
    BudgetCategory,
    BudgetNotification,
    BudgetSharing,
    CostControl,
    CostControlAlertEvent,
    CostQuery,
    Deployment,
    Granularity,
    LogAlert,
    MetricAlert,
    QuotaUsage,
    Table,
    Threshold,
    ThresholdType,
    TimeGrain,
)
from .operations.apim import LlmEmitTokenMetric, LlmTokenLimit
from .operations.monitor import FoundryMetrics
from .operations.pricing import ModelPrice, PriceSheet

__all__ = [
    "CHINA_CLOUD", "PUBLIC_CLOUD", "US_GOV_CLOUD",
    "ActionGroup", "ApiVersions", "ApimProduct", "ApimSubscription", "AuthenticationError", "AzureCloud",
    "Budget", "BudgetCategory", "BudgetExceededError", "BudgetNotification", "BudgetSharing",
    "ConflictError", "CostControl", "CostControlAlertEvent", "CostQuery", "Deployment",
    "FinOpsClient", "FinOpsError", "ForbiddenError", "FoundryMetrics", "Granularity", "HttpResponseError",
    "LlmEmitTokenMetric", "LlmTokenLimit", "LogAlert", "MetricAlert", "ModelPrice", "NotFoundError",
    "PriceSheet", "QuotaUsage", "ResourceId", "RetryPolicy", "ServiceError", "StaticTokenCredential",
    "Table", "ThresholdType", "Threshold", "ThrottledError", "TimeGrain", "TokenCredential",
    "__version__", "apim_service_id", "default_credential", "foundry_account_id", "foundry_project_id",
    "is_budget_exceeded", "raise_for_budget",
]
