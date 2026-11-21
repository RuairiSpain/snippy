from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .._config import ApiVersions, AzureCloud
from .._ids import resource_group_scope, segment, subscription_scope
from .._transport import Response, Transport


class _Operations:
    """Base for operation groups: holds the transport and builds ARM URLs."""

    def __init__(
        self,
        transport: Transport,
        *,
        subscription_id: str,
        resource_group: str | None,
        cloud: AzureCloud,
        api_versions: ApiVersions,
    ) -> None:
        self._t = transport
        self._sub = subscription_id
        self._rg = resource_group
        self._cloud = cloud
        self._v = api_versions

    def __repr__(self) -> str:
        return f"<{type(self).__name__} subscription={self._sub!r} resource_group={self._rg!r}>"

    # ------------------------------------------------------------------ scope
    @staticmethod
    def _seg(value: str | None, what: str = "name") -> str:
        """Encode a user-supplied name as a single URL path segment."""
        return segment(value, what)

    def _rg_or_default(self, resource_group: str | None) -> str:
        rg = resource_group or self._rg
        if not rg:
            raise ValueError("resource_group is required (pass it or set a default on FinOpsClient)")
        return rg

    @property
    def subscription_scope(self) -> str:
        return subscription_scope(self._sub)

    def resource_group_scope(self, resource_group: str | None = None) -> str:
        return resource_group_scope(self._sub, self._rg_or_default(resource_group))

    # ------------------------------------------------------------------- http
    def _url(self, path: str) -> str:
        """Build an ARM URL. Absolute URLs are only accepted for the cloud's own ARM host.

        The bearer token is attached to every request, so a caller-supplied ``scope`` such as
        ``https://evil.example`` must never be able to redirect it to another host.
        """
        base = self._cloud.resource_manager.rstrip("/")
        if path.lower().startswith(("http://", "https://")):
            if path != base and not path.startswith(base + "/"):
                raise ValueError(f"Refusing to send Azure credentials to {path.split('?')[0]!r}")
            return path
        if not path.startswith("/"):
            path = "/" + path
        if "?" in path or "#" in path or any(seg in ("..", ".") for seg in path.split("/")):
            raise ValueError(f"Invalid resource path: {path!r}")
        return f"{base}{path}"

    def _arm(
        self,
        method: str,
        path: str,
        api_version: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> Response:
        q = {"api-version": api_version, **(params or {})}
        return self._t.request(
            method, self._url(path), scope=self._cloud.arm_scope, params=q,
            json_body=body, headers=headers, expected=expected,
        )

    def _list(self, path: str, api_version: str, params: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        q = {"api-version": api_version, **(params or {})}
        return self._t.paged(self._url(path), scope=self._cloud.arm_scope, params=q)
