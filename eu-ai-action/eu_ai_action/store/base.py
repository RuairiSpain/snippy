from __future__ import annotations

from typing import Any, Protocol

CONTAINERS = ("tenants", "scans", "evidence", "agents", "dutyResults", "documents", "tasks", "audit")


class Store(Protocol):
    """Document store. Every call is scoped to a tenant (the partition key)."""

    def upsert(self, container: str, tenant_id: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def get(self, container: str, tenant_id: str, doc_id: str) -> dict[str, Any] | None: ...

    def query(self, container: str, tenant_id: str, **equals: Any) -> list[dict[str, Any]]: ...
