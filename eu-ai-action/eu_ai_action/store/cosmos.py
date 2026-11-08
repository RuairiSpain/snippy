"""Cosmos DB store. Partition key is /tenantId on every container. Not exercised by unit tests."""
from __future__ import annotations

from typing import Any

from .base import CONTAINERS


class CosmosStore:
    def __init__(self, endpoint: str, database: str, credential: Any = None) -> None:
        from azure.cosmos import CosmosClient, PartitionKey
        from azure.identity import DefaultAzureCredential

        client = CosmosClient(endpoint, credential or DefaultAzureCredential())
        db = client.create_database_if_not_exists(database)
        self._containers = {
            name: db.create_container_if_not_exists(id=name, partition_key=PartitionKey(path="/tenantId"))
            for name in CONTAINERS
        }

    def upsert(self, container: str, tenant_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        if doc.get("tenantId") != tenant_id:
            raise ValueError("document tenantId does not match the partition key")
        self._containers[container].upsert_item(doc)
        return doc

    def get(self, container: str, tenant_id: str, doc_id: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return self._containers[container].read_item(doc_id, partition_key=tenant_id)
        except CosmosResourceNotFoundError:
            return None

    def query(self, container: str, tenant_id: str, **equals: Any) -> list[dict[str, Any]]:
        clauses = ["c.tenantId = @tenant"]
        params: list[dict[str, Any]] = [{"name": "@tenant", "value": tenant_id}]
        for i, (k, v) in enumerate(equals.items()):
            if not k.replace("_", "").isalnum():
                raise ValueError(f"invalid field name {k!r}")
            clauses.append(f"c.{k} = @p{i}")
            params.append({"name": f"@p{i}", "value": v})
        query = "SELECT * FROM c WHERE " + " AND ".join(clauses)
        return list(
            self._containers[container].query_items(query=query, parameters=params, partition_key=tenant_id)
        )
