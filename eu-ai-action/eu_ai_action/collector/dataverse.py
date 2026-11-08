"""Copilot Studio agents through the Dataverse Web API.

Table and column names follow Microsoft's published Dataverse schema for Copilot Studio (bots, botcomponents)
but have NOT been checked against a live tenant. Spike M1 must confirm them; see spikes/spike_dataverse.py.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from ..models import AgentRef, Evidence
from .base import make_evidence

log = logging.getLogger(__name__)

BOT_SELECT = "botid,name,schemaname,authenticationmode,language,createdon,modifiedon,statecode,configuration"
COMPONENT_SELECT = "botcomponentid,name,schemaname,componenttype,content,data"


class DataverseSource:
    name = "dataverse"

    def __init__(self, env_url: str, api_version: str, token: Callable[[], str], client: httpx.Client | None = None):
        self.base = f"{env_url.rstrip('/')}/api/data/{api_version}"
        self._token = token
        self._http = client or httpx.Client(timeout=30)

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self._http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json", "OData-Version": "4.0"},
        )
        if r.status_code == 429:
            raise RuntimeError(f"throttled, retry-after={r.headers.get('Retry-After')}")
        r.raise_for_status()
        return r.json()

    def _paged(self, url: str, params: dict | None = None) -> list[dict]:
        rows: list[dict] = []
        data = self._get(url, params)
        rows.extend(data.get("value", []))
        while data.get("@odata.nextLink"):
            data = self._get(data["@odata.nextLink"])
            rows.extend(data.get("value", []))
        return rows

    def list_agents(self, tenant_id: str) -> list[AgentRef]:
        rows = self._paged(f"{self.base}/bots", {"$select": "botid,name"})
        return [AgentRef(agent_id=r["botid"], name=r.get("name", "")) for r in rows]

    def collect(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[Evidence]:
        bot = self._get(f"{self.base}/bots({agent.agent_id})", {"$select": BOT_SELECT})
        bot.pop("@odata.context", None)
        components = self._paged(
            f"{self.base}/botcomponents",
            {"$select": COMPONENT_SELECT, "$filter": f"_parentbotid_value eq {agent.agent_id}"},
        )
        payload = {"bot": bot, "components": components, "channels": [], "notes": ["channels not collected: confirm source in spike M1"]}
        return [make_evidence(tenant_id, agent.agent_id, scan_id, self.name, f"{self.base}/bots({agent.agent_id})", payload)]
