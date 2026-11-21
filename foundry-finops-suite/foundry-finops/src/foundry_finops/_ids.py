"""Helpers for building and parsing Azure Resource Manager IDs."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


def _seg(value: str) -> str:
    """Percent-encode one URL path segment so user input can't add or remove path levels."""
    return quote(str(value), safe="")


def segment(value: str | None, what: str = "name") -> str:
    """Validate and encode a user-supplied resource name for use inside an ARM URL."""
    if value is None or not str(value).strip():
        raise ValueError(f"{what} is required")
    return _seg(str(value).strip())


def odata_quote(value: str) -> str:
    """Escape a value for use inside an OData single-quoted string literal."""
    return str(value).replace("'", "''")


def subscription_scope(subscription_id: str) -> str:
    return f"/subscriptions/{_seg(subscription_id)}"


def resource_group_scope(subscription_id: str, resource_group: str) -> str:
    return f"{subscription_scope(subscription_id)}/resourceGroups/{_seg(resource_group)}"


def foundry_account_id(subscription_id: str, resource_group: str, account: str) -> str:
    return (
        f"{resource_group_scope(subscription_id, resource_group)}"
        f"/providers/Microsoft.CognitiveServices/accounts/{_seg(account)}"
    )


def foundry_project_id(subscription_id: str, resource_group: str, account: str, project: str) -> str:
    return f"{foundry_account_id(subscription_id, resource_group, account)}/projects/{_seg(project)}"


def apim_service_id(subscription_id: str, resource_group: str, service: str) -> str:
    return (
        f"{resource_group_scope(subscription_id, resource_group)}"
        f"/providers/Microsoft.ApiManagement/service/{_seg(service)}"
    )


@dataclass(frozen=True)
class ResourceId:
    """Parsed ARM resource ID."""

    id: str
    subscription_id: str | None
    resource_group: str | None
    provider: str | None
    types: tuple[str, ...]
    names: tuple[str, ...]

    @property
    def name(self) -> str | None:
        return self.names[-1] if self.names else None

    @property
    def type(self) -> str | None:
        if not self.provider:
            return None
        return "/".join((self.provider, *self.types))

    @classmethod
    def parse(cls, resource_id: str) -> ResourceId:
        parts = [p for p in resource_id.strip("/").split("/") if p]
        sub = rg = provider = None
        types: list[str] = []
        names: list[str] = []
        i = 0
        while i < len(parts):
            key = parts[i].lower()
            val = parts[i + 1] if i + 1 < len(parts) else None
            if key == "subscriptions" and sub is None:
                sub = val
            elif key == "resourcegroups":
                rg = val
            elif key == "providers":
                provider = val
            elif provider is not None and val is not None:
                types.append(parts[i])
                names.append(val)
            i += 2
        return cls(resource_id, sub, rg, provider, tuple(types), tuple(names))
