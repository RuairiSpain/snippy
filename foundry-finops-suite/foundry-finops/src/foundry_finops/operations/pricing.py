"""Azure Retail Prices API (public, no auth) and a price sheet for estimating token cost."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ._base import _Operations


@dataclass
class ModelPrice:
    """USD per 1M tokens."""

    input: float
    output: float
    cached_input: float | None = None


@dataclass
class PriceSheet:
    """Model → price lookup used to *estimate* cost from token counts.

    Retail prices aren't your negotiated prices; load your own with :meth:`from_mapping`
    (e.g. from the price sheet download) for closer estimates. Billed cost always comes
    from Cost Management.
    """

    prices: dict[str, ModelPrice] = field(default_factory=dict)
    currency: str = "USD"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Mapping[str, float]], currency: str = "USD") -> PriceSheet:
        return cls({k.lower(): ModelPrice(**v) for k, v in data.items()}, currency)

    def lookup(self, model: str) -> ModelPrice | None:
        m = model.lower()
        if m in self.prices:
            return self.prices[m]
        best = max((k for k in self.prices if m.startswith(k)), key=len, default=None)
        return self.prices.get(best) if best else None

    def estimate(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
        p = self.lookup(model)
        if p is None:
            return None
        cached_rate = p.cached_input if p.cached_input is not None else p.input
        uncached = max(input_tokens - cached_tokens, 0)
        return round((uncached * p.input + cached_tokens * cached_rate + output_tokens * p.output) / 1_000_000, 6)


class PricingOperations(_Operations):
    """Query public retail meters (unauthenticated)."""

    def retail_prices(self, odata_filter: str, *, currency: str = "USD") -> Iterator[dict[str, Any]]:
        """Iterate retail price items, e.g. ``"serviceName eq 'Foundry Models' and armRegionName eq 'eastus2'"``."""
        url: str | None = self._cloud.retail_prices
        params: dict[str, Any] | None = {
            "api-version": self._v.retail_prices, "$filter": odata_filter, "currencyCode": currency,
        }
        while url:
            body = self._t.request("GET", url, scope="", params=params, authenticate=False).body or {}
            yield from body.get("Items") or []
            url, params = body.get("NextPageLink"), None

    def openai_price_sheet(self, region: str = "eastus2", *, service_name: str = "Foundry Models") -> PriceSheet:
        """Best-effort per-1M-token sheet built from retail meters (Global/Standard, input/output).

        Meter naming changes over time, so check the result and override with your own prices.
        """
        sheet: dict[str, dict[str, float]] = {}
        flt = f"serviceName eq '{service_name}' and armRegionName eq '{region}'"
        for item in self.retail_prices(flt):
            meter = (item.get("meterName") or "").lower()
            unit = (item.get("unitOfMeasure") or "").upper()
            price = float(item.get("retailPrice") or 0)
            mult = 1000 if unit.startswith("1K") else 1 if unit.startswith("1M") else None
            if mult is None or "batch" in meter or "ft" in meter.split():
                continue
            kind = "output" if "outp" in meter else "cached_input" if "cache" in meter else "input" if "inp" in meter else None
            if not kind:
                continue
            model = meter.split(" inp")[0].split(" outp")[0].split(" cached")[0].replace(" glbl", "").strip()
            sheet.setdefault(model, {"input": 0.0, "output": 0.0}).setdefault(kind, price * mult)
            sheet[model][kind] = sheet[model].get(kind) or price * mult
        return PriceSheet.from_mapping({k: v for k, v in sheet.items() if v.get("input") and v.get("output")})
