"""Azure Resource Manager tags (team / cost-centre tagging) on any scope."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ._base import _Operations


class TagOperations(_Operations):
    """Read and write tags through ``Microsoft.Resources/tags/default`` on any resource ID.

    Foundry also stamps *billing* tags (``project``, ``deployment``, ``agent.name``) on model
    charges in Cost Management; those aren't ARM tags and can't be edited. Tags you set on
    the Foundry resource, resource group or subscription show up in cost data (enable
    tag inheritance for resource-group and subscription tags).
    """

    @staticmethod
    def _tags(scope: str) -> str:
        return f"{scope.rstrip('/')}/providers/Microsoft.Resources/tags/default"

    def get(self, scope: str) -> dict[str, str]:
        body = self._arm("GET", self._tags(scope), self._v.tags).body or {}
        return dict((body.get("properties") or {}).get("tags") or {})

    def replace(self, scope: str, tags: Mapping[str, str]) -> dict[str, str]:
        """Replace every tag on the scope."""
        body = {"properties": {"tags": dict(tags)}}
        resp = self._arm("PUT", self._tags(scope), self._v.tags, body=body).body or {}
        return dict((resp.get("properties") or {}).get("tags") or {})

    def merge(self, scope: str, tags: Mapping[str, str]) -> dict[str, str]:
        """Add or update the given tags, keeping the others."""
        return self._patch(scope, "Merge", tags)

    def remove(self, scope: str, *names: str) -> dict[str, str]:
        """Remove tags by name (values are ignored)."""
        current = self.get(scope)
        return self._patch(scope, "Delete", {n: current.get(n, "") for n in names if n in current})

    def clear(self, scope: str) -> None:
        self._arm("DELETE", self._tags(scope), self._v.tags)

    def _patch(self, scope: str, op: str, tags: Mapping[str, str]) -> dict[str, str]:
        body = {"operation": op, "properties": {"tags": dict(tags)}}
        resp = self._arm("PATCH", self._tags(scope), self._v.tags, body=body).body or {}
        return dict((resp.get("properties") or {}).get("tags") or {})

    # --------------------------------------------------------------- discovery
    def tag_names(self) -> Iterator[dict[str, Any]]:
        """Every tag name/value in the subscription with resource counts."""
        yield from self._list(f"{self.subscription_scope}/tagNames", self._v.resources)

    def resources_with_tag(self, name: str, value: str | None = None) -> Iterator[dict[str, Any]]:
        flt = f"tagName eq '{name}'" + (f" and tagValue eq '{value}'" if value is not None else "")
        yield from self._list(f"{self.subscription_scope}/resources", self._v.resources, {"$filter": flt})

    def list_tagged(self, resource_type: str = "Microsoft.CognitiveServices/accounts") -> list[dict[str, Any]]:
        """Resources of one type with their tags – handy for a tag inventory grid."""
        out = []
        for r in self._list(
            f"{self.subscription_scope}/resources", self._v.resources,
            {"$filter": f"resourceType eq '{resource_type}'"},
        ):
            out.append({"id": r.get("id"), "name": r.get("name"), "type": r.get("type"),
                        "location": r.get("location"), "tags": r.get("tags") or {}})
        return out
