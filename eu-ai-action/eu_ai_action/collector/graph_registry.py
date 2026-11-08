"""Agent registry / Entra agent identity through Microsoft Graph.

The endpoint is not known until spike M1 confirms it, so the path comes from GRAPH_REGISTRY_PATH
(for example "/agentRegistry/agentInstances/{agent_id}"). With an empty path the source is disabled.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from ..models import AgentRef, Evidence
from .base import make_evidence

log = logging.getLogger(__name__)


class GraphRegistrySource:
    name = "registry"

    def __init__(self, base_url: str, version: str, path: str, token: Callable[[], str], client: httpx.Client | None = None):
        self.base = f"{base_url.rstrip('/')}/{version}"
        self.path = path
        self._token = token
        self._http = client or httpx.Client(timeout=30)

    def list_agents(self, tenant_id: str) -> list[AgentRef]:
        return []  # Dataverse is the list of record in the MVP

    def collect(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[Evidence]:
        if not self.path:
            log.info("registry source disabled: GRAPH_REGISTRY_PATH is empty")
            return []
        url = f"{self.base}{self.path.format(agent_id=agent.agent_id)}"
        r = self._http.get(url, headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        payload = r.json()
        payload.pop("@odata.context", None)
        return [make_evidence(tenant_id, agent.agent_id, scan_id, self.name, url, payload)]
