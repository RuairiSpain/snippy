"""Subscription-level model quota (TPM) per region and available models."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..models import QuotaUsage
from ._base import _Operations


class QuotaOperations(_Operations):
    """Read model quota usage. To change what a deployment consumes, use
    :meth:`FoundryOperations.set_capacity`; to raise the regional limit, file a quota request."""

    def usages(self, location: str, *, filter: str | None = None) -> Iterator[QuotaUsage]:
        url = f"{self.subscription_scope}/providers/Microsoft.CognitiveServices/locations/{location}/usages"
        params = {"$filter": filter} if filter else None
        for d in self._list(url, self._v.cognitive_services, params):
            yield QuotaUsage.from_dict(d)

    def models(self, location: str) -> Iterator[dict[str, Any]]:
        """Models available in a region, including SKUs and their capacity limits."""
        url = f"{self.subscription_scope}/providers/Microsoft.CognitiveServices/locations/{location}/models"
        yield from self._list(url, self._v.cognitive_services)

    def account_usages(self, account: str, resource_group: str | None = None) -> Iterator[QuotaUsage]:
        """Usages reported by one Foundry resource."""
        from .._ids import foundry_account_id

        rid = foundry_account_id(self._sub, self._rg_or_default(resource_group), account)
        for d in self._list(f"{rid}/usages", self._v.cognitive_services):
            yield QuotaUsage.from_dict(d)
