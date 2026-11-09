"""One configurable JSON source for the v1/v2 evidence (Entra access, retention, Foundry, Defender, learning).
The endpoint path comes from settings and must be confirmed in a spike. The response shape the fact extractors expect
is documented in facts/extract.py and in tests/fixtures/agents/*.json; adapt the extractor if the real shape differs."""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from ..models import AgentRef, Evidence
from .base import make_evidence

log = logging.getLogger(__name__)


class GenericJsonSource:
    def __init__(self, name: str, base_url: str, version: str, path: str, token: Callable[[], str], client: httpx.Client | None = None):
        self.name = name
        self.base = f"{base_url.rstrip('/')}/{version}"
        self.path = path
        self._token = token
        self._http = client or httpx.Client(timeout=30)

    def list_agents(self, tenant_id: str) -> list[AgentRef]:
        return []

    def collect(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[Evidence]:
        url = f"{self.base}{self.path.format(agent_id=agent.agent_id)}"
        r = self._http.get(url, headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        payload = r.json()
        payload.pop("@odata.context", None)
        return [make_evidence(tenant_id, agent.agent_id, scan_id, self.name, url, payload)]
