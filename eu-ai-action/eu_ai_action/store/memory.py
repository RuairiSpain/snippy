from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    def upsert(self, container: str, tenant_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        if doc.get("tenantId") != tenant_id:
            raise ValueError("document tenantId does not match the partition key")
        self._data[(container, tenant_id)][doc["id"]] = copy.deepcopy(doc)
        return doc

    def get(self, container: str, tenant_id: str, doc_id: str) -> dict[str, Any] | None:
        d = self._data[(container, tenant_id)].get(doc_id)
        return copy.deepcopy(d) if d is not None else None

    def query(self, container: str, tenant_id: str, **equals: Any) -> list[dict[str, Any]]:
        out = []
        for d in self._data[(container, tenant_id)].values():
            if all(d.get(k) == v for k, v in equals.items()):
                out.append(copy.deepcopy(d))
        return out
