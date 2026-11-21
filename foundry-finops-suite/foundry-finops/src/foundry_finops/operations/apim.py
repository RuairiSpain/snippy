"""Azure API Management as an AI gateway: products, subscriptions, APIs and LLM token policies."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import quoteattr

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring  # policy XML comes from the service

from .._ids import apim_service_id
from ..errors import NotFoundError
from ..models import ApimApi, ApimProduct, ApimSubscription
from ._base import _Operations

EMPTY_POLICY = (
    "<policies>\n  <inbound>\n    <base />\n  </inbound>\n  <backend>\n    <base />\n  </backend>\n"
    "  <outbound>\n    <base />\n  </outbound>\n  <on-error>\n    <base />\n  </on-error>\n</policies>"
)


# =============================================================== policy builders
@dataclass
class LlmTokenLimit:
    """``<llm-token-limit>`` – per-key TPM (429) and/or token quota per period (403).

    >>> LlmTokenLimit(counter_key="@(context.Subscription.Id)", tokens_per_minute=5000,
    ...               token_quota=1_000_000, token_quota_period="Monthly").to_xml()
    """

    counter_key: str = "@(context.Subscription.Id)"
    tokens_per_minute: int | None = None
    token_quota: int | None = None
    token_quota_period: str | None = None  #: Hourly | Daily | Weekly | Monthly | Yearly
    estimate_prompt_tokens: bool = False
    remaining_tokens_header_name: str | None = "x-ratelimit-remaining-tokens"
    remaining_quota_tokens_header_name: str | None = "x-quota-remaining-tokens"
    tokens_consumed_header_name: str | None = "x-tokens-consumed"
    tag: str = "llm-token-limit"  #: or ``azure-openai-token-limit`` for older gateways

    def to_xml(self) -> str:
        if self.tokens_per_minute is None and self.token_quota is None:
            raise ValueError("Set tokens_per_minute, token_quota (+ token_quota_period), or both")
        if self.token_quota is not None and not self.token_quota_period:
            raise ValueError("token_quota needs token_quota_period")
        attrs: list[tuple[str, Any]] = [("counter-key", self.counter_key)]
        if self.tokens_per_minute is not None:
            attrs.append(("tokens-per-minute", self.tokens_per_minute))
        if self.token_quota is not None:
            attrs += [("token-quota", self.token_quota), ("token-quota-period", self.token_quota_period)]
        attrs.append(("estimate-prompt-tokens", str(self.estimate_prompt_tokens).lower()))
        for name, value in (
            ("remaining-tokens-header-name", self.remaining_tokens_header_name),
            ("remaining-quota-tokens-header-name", self.remaining_quota_tokens_header_name if self.token_quota else None),
            ("tokens-consumed-header-name", self.tokens_consumed_header_name),
        ):
            if value:
                attrs.append((name, value))
        return f"<{self.tag} " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs) + " />"

    @classmethod
    def from_element(cls, el: ET.Element) -> LlmTokenLimit:
        g = el.attrib.get

        def num(name: str) -> int | None:  # policy expressions (``@(...)``) aren't plain numbers
            value = g(name, "")
            return int(value) if value.isdigit() else None

        return cls(
            counter_key=g("counter-key", ""),
            tokens_per_minute=num("tokens-per-minute"),
            token_quota=num("token-quota"),
            token_quota_period=g("token-quota-period"),
            estimate_prompt_tokens=g("estimate-prompt-tokens", "false").lower() == "true",
            remaining_tokens_header_name=g("remaining-tokens-header-name"),
            remaining_quota_tokens_header_name=g("remaining-quota-tokens-header-name"),
            tokens_consumed_header_name=g("tokens-consumed-header-name"),
            tag=el.tag,
        )


@dataclass
class LlmEmitTokenMetric:
    """``<llm-emit-token-metric>`` – token counts to Application Insights with up to 5 custom dimensions.

    Keep dimension cardinality low: APIM keeps 100 values per dimension and 1,000 time series
    per namespace and silently drops the rest. Use team/product IDs, not raw user IDs.
    """

    namespace: str = "foundry-finops"
    dimensions: Mapping[str, str | None] = field(
        default_factory=lambda: {
            "API ID": None,
            "Subscription ID": None,
            "Team": "@(context.Request.Headers.GetValueOrDefault(\"x-team\", \"unassigned\"))",
        }
    )
    tag: str = "llm-emit-token-metric"

    DEFAULT_DIMENSIONS = frozenset(
        {"API ID", "Operation ID", "Product ID", "User ID", "Subscription ID", "Location", "Gateway ID", "Backend ID"}
    )

    def to_xml(self) -> str:
        for k, v in self.dimensions.items():
            if v is None and k not in self.DEFAULT_DIMENSIONS:
                raise ValueError(f"Custom dimension {k!r} needs a value expression")
        custom = [k for k, v in self.dimensions.items() if v is not None or k not in self.DEFAULT_DIMENSIONS]
        if len(custom) > 5:
            raise ValueError("APIM allows at most 5 custom dimensions per token-metric policy")
        dims = "".join(
            f"\n  <dimension name={quoteattr(k)}" + (f" value={quoteattr(v)}" if v is not None else "") + " />"
            for k, v in self.dimensions.items()
        )
        return f"<{self.tag} namespace={quoteattr(self.namespace)}>{dims}\n</{self.tag}>"


def upsert_policy_fragment(policy_xml: str | None, fragment: str, *, section: str = "inbound") -> str:
    """Insert ``fragment`` into ``section`` after ``<base />``, replacing an element with the same tag.

    Uses ElementTree, so comments are dropped; policy expressions in attributes are preserved.
    """
    root = _parse_xml(policy_xml or EMPTY_POLICY)
    new = _parse_xml(fragment)
    sec = root.find(section)
    if sec is None:
        sec = ET.SubElement(root, section)
        ET.SubElement(sec, "base")
    for existing in list(sec):
        if existing.tag == new.tag or (
            new.tag.endswith("token-limit") and existing.tag.endswith("token-limit")
        ) or (new.tag.endswith("emit-token-metric") and existing.tag.endswith("emit-token-metric")):
            sec.remove(existing)
    children = list(sec)
    idx = next((i + 1 for i, c in enumerate(children) if c.tag == "base"), 0)
    sec.insert(idx, new)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def _parse_xml(xml: str) -> ET.Element:
    """Parse policy XML safely; malformed or hostile XML becomes a ``ValueError``."""
    try:
        root: ET.Element = _safe_fromstring(xml)
        return root
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Invalid policy XML: {exc}") from exc


def remove_policy_element(policy_xml: str, tag_suffix: str) -> str:
    root = _parse_xml(policy_xml)
    for sec in root:
        for el in list(sec):
            if el.tag.endswith(tag_suffix):
                sec.remove(el)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def find_token_limits(policy_xml: str | None) -> list[LlmTokenLimit]:
    if not policy_xml:
        return []
    try:
        root = _parse_xml(policy_xml)
    except ValueError:
        return []
    return [LlmTokenLimit.from_element(el) for el in root.iter() if re.search(r"token-limit$", el.tag)]


# ================================================================== operations
class ApimOperations(_Operations):
    """Manage an API Management instance used as the AI gateway in front of Foundry.

    Policy scopes: ``scope="global"``, ``"product:{id}"``, ``"api:{id}"`` or ``"operation:{api}/{op}"``.
    """

    # ------------------------------------------------------------- optimistic concurrency
    def _etag(self, resp: Any) -> str | None:
        for k, v in resp.headers.items():
            if k.lower() == "etag":
                return str(v)
        body = resp.body if isinstance(resp.body, dict) else {}
        return body.get("etag") or body.get("eTag")

    def _current_etag(self, url: str, params: Mapping[str, Any] | None = None) -> str | None:
        """ETag of an existing entity, or ``None`` if it doesn't exist yet."""
        try:
            return self._etag(self._arm("GET", url, self._v.api_management, params=params))
        except NotFoundError:
            return None

    def _put(self, url: str, body: Any, etag: str | None = None, *, lookup: bool = True) -> Any:
        """PUT with ``If-Match: <etag>`` when updating, nothing when creating.

        A wildcard ``If-Match: *`` would overwrite whatever another admin changed since we read it;
        a real ETag turns that into a 412 (:class:`~foundry_finops.errors.ConflictError`) instead.
        """
        if etag is None and lookup:
            etag = self._current_etag(url)
        return self._arm("PUT", url, self._v.api_management, body=body, headers={"If-Match": etag} if etag else None)

    def service_id(self, service: str, resource_group: str | None = None) -> str:
        return apim_service_id(self._sub, self._rg_or_default(resource_group), service)

    def list_services(self) -> Iterator[dict[str, Any]]:
        base = self.resource_group_scope() if self._rg else self.subscription_scope
        yield from self._list(f"{base}/providers/Microsoft.ApiManagement/service", self._v.api_management)

    # ------------------------------------------------------------------ APIs
    def list_apis(self, service: str, resource_group: str | None = None) -> Iterator[ApimApi]:
        for d in self._list(f"{self.service_id(service, resource_group)}/apis", self._v.api_management):
            yield ApimApi.from_dict(d)

    # -------------------------------------------------------------- products
    def list_products(self, service: str, resource_group: str | None = None) -> Iterator[ApimProduct]:
        for d in self._list(f"{self.service_id(service, resource_group)}/products", self._v.api_management):
            yield ApimProduct.from_dict(d)

    def get_product(self, service: str, product_id: str, resource_group: str | None = None) -> ApimProduct:
        url = f"{self.service_id(service, resource_group)}/products/{self._seg(product_id)}"
        return ApimProduct.from_dict(self._arm("GET", url, self._v.api_management).body)

    def create_or_update_product(
        self, service: str, product: ApimProduct, *, api_ids: Sequence[str] = (), resource_group: str | None = None
    ) -> ApimProduct:
        """Create or update a product (e.g. one per team) and link it to APIs."""
        url = f"{self.service_id(service, resource_group)}/products/{self._seg(product.name)}"
        resp = self._put(url, product.to_dict())
        for api_id in api_ids:
            self._arm("PUT", f"{url}/apis/{self._seg(api_id)}", self._v.api_management)
        return ApimProduct.from_dict(resp.body or {})

    def delete_product(self, service: str, product_id: str, *, delete_subscriptions: bool = True,
                       resource_group: str | None = None) -> None:
        url = f"{self.service_id(service, resource_group)}/products/{self._seg(product_id)}"
        self._arm("DELETE", url, self._v.api_management, params={"deleteSubscriptions": str(delete_subscriptions).lower()},
                  headers={"If-Match": "*"})

    # --------------------------------------------------------- subscriptions
    def list_subscriptions(self, service: str, resource_group: str | None = None) -> Iterator[ApimSubscription]:
        for d in self._list(f"{self.service_id(service, resource_group)}/subscriptions", self._v.api_management):
            yield ApimSubscription.from_dict(d)

    def get_subscription(self, service: str, sid: str, resource_group: str | None = None) -> ApimSubscription:
        url = f"{self.service_id(service, resource_group)}/subscriptions/{self._seg(sid)}"
        return ApimSubscription.from_dict(self._arm("GET", url, self._v.api_management).body)

    def create_or_update_subscription(
        self, service: str, subscription: ApimSubscription, resource_group: str | None = None
    ) -> ApimSubscription:
        url = f"{self.service_id(service, resource_group)}/subscriptions/{self._seg(subscription.name)}"
        body = subscription.to_dict()
        body["properties"]["scope"] = self._full_scope(service, subscription.scope, resource_group)
        resp = self._put(url, body)
        return ApimSubscription.from_dict(resp.body or {})

    def _full_scope(self, service: str, scope: str, resource_group: str | None) -> str:
        """APIM subscriptions need a full resource ID; accept ``/products/x``, ``/apis/x`` and ``/apis``."""
        if not scope:
            raise ValueError("subscription scope is required, e.g. '/products/<productId>' or '/apis'")
        if scope.startswith("/subscriptions/"):
            return scope
        if scope == "/apis" or scope.startswith(("/products/", "/apis/")):
            return f"{self.service_id(service, resource_group)}{scope}"
        raise ValueError("subscription scope must be '/apis', '/apis/<id>' or '/products/<id>'")

    def delete_subscription(self, service: str, sid: str, resource_group: str | None = None) -> None:
        url = f"{self.service_id(service, resource_group)}/subscriptions/{self._seg(sid)}"
        self._arm("DELETE", url, self._v.api_management, headers={"If-Match": "*"})

    def list_subscription_secrets(self, service: str, sid: str, resource_group: str | None = None) -> dict[str, str]:
        """Return primary/secondary keys. Treat as secrets – never log or persist them."""
        url = f"{self.service_id(service, resource_group)}/subscriptions/{self._seg(sid)}/listSecrets"
        return dict(self._arm("POST", url, self._v.api_management).body or {})

    # -------------------------------------------------------------- policies
    def _policy_url(self, service: str, scope: str, resource_group: str | None) -> str:
        base = self.service_id(service, resource_group)
        kind, _, ident = scope.partition(":")
        if kind == "global":
            return f"{base}/policies/policy"
        if kind == "product":
            return f"{base}/products/{self._seg(ident)}/policies/policy"
        if kind == "api":
            return f"{base}/apis/{self._seg(ident)}/policies/policy"
        if kind == "operation":
            api, _, op = ident.partition("/")
            return f"{base}/apis/{self._seg(api)}/operations/{self._seg(op)}/policies/policy"
        raise ValueError("scope must be 'global', 'product:<id>', 'api:<id>' or 'operation:<api>/<op>'")

    def get_policy_with_etag(
        self, service: str, scope: str = "global", resource_group: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return ``(policy_xml, etag)``; both ``None`` if the scope has no policy."""
        try:
            resp = self._arm("GET", self._policy_url(service, scope, resource_group), self._v.api_management,
                             params={"format": "rawxml"})
        except NotFoundError:
            return None, None
        return ((resp.body or {}).get("properties") or {}).get("value"), self._etag(resp)

    def get_policy(self, service: str, scope: str = "global", resource_group: str | None = None) -> str | None:
        """Return the raw policy XML, or ``None`` if the scope has no policy."""
        return self.get_policy_with_etag(service, scope, resource_group)[0]

    def set_policy(self, service: str, xml: str, scope: str = "global", resource_group: str | None = None,
                   *, etag: str | None = None) -> None:
        """Replace a scope's policy. Pass the ``etag`` you read it with to fail (412) rather than
        overwrite a concurrent change; without one the current ETag is looked up just before writing."""
        _parse_xml(xml)  # fail fast on malformed XML
        body = {"properties": {"format": "rawxml", "value": xml}}
        url = self._policy_url(service, scope, resource_group)
        self._put(url, body, etag, lookup=etag is None)

    def delete_policy(self, service: str, scope: str, resource_group: str | None = None) -> None:
        self._arm("DELETE", self._policy_url(service, scope, resource_group), self._v.api_management,
                  headers={"If-Match": "*"})

    # ------------------------------------------------------ token-limit helpers
    def get_token_limits(self, service: str, scope: str, resource_group: str | None = None) -> list[LlmTokenLimit]:
        return find_token_limits(self.get_policy(service, scope, resource_group))

    def set_token_limit(self, service: str, limit: LlmTokenLimit, scope: str,
                        resource_group: str | None = None) -> str:
        """Add or replace the token-limit element at a scope. Returns the new policy XML."""
        current, etag = self.get_policy_with_etag(service, scope, resource_group)
        xml = upsert_policy_fragment(current, limit.to_xml())
        self.set_policy(service, xml, scope, resource_group, etag=etag)
        return xml

    def remove_token_limit(self, service: str, scope: str, resource_group: str | None = None) -> None:
        current, etag = self.get_policy_with_etag(service, scope, resource_group)
        if current:
            self.set_policy(service, remove_policy_element(current, "token-limit"), scope, resource_group, etag=etag)

    def set_token_metrics(self, service: str, metric: LlmEmitTokenMetric, scope: str,
                          resource_group: str | None = None) -> str:
        current, etag = self.get_policy_with_etag(service, scope, resource_group)
        xml = upsert_policy_fragment(current, metric.to_xml())
        self.set_policy(service, xml, scope, resource_group, etag=etag)
        return xml

    def token_limit_inventory(self, service: str, resource_group: str | None = None) -> list[dict[str, Any]]:
        """Every token-limit policy across global, product and API scopes."""
        scopes = ["global"]
        scopes += [f"product:{p.name}" for p in self.list_products(service, resource_group)]
        scopes += [f"api:{a.name}" for a in self.list_apis(service, resource_group)]
        out = []
        for sc in scopes:
            for lim in self.get_token_limits(service, sc, resource_group):
                out.append({"scope": sc, **{k: v for k, v in vars(lim).items()}})
        return out
