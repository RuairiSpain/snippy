"""Offline source that replays recorded (or hand-written) evidence from JSON files."""
from __future__ import annotations

import json
from pathlib import Path

from ..models import AgentRef, Evidence
from .base import make_evidence


class FixtureSource:
    name = "fixtures"

    def __init__(self, fixtures_dir: str) -> None:
        self.dir = Path(fixtures_dir) / "agents"

    def _load(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in sorted(self.dir.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            out[d["agent_id"]] = d
        return out

    def list_agents(self, tenant_id: str) -> list[AgentRef]:
        return [AgentRef(agent_id=k, name=v.get("name", k)) for k, v in self._load().items()]

    def collect(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[Evidence]:
        data = self._load()[agent.agent_id]
        out = []
        for source in ("dataverse", "registry", "purview"):
            if source in data:
                out.append(make_evidence(tenant_id, agent.agent_id, scan_id, source, f"fixture:{source}", data[source]))
        return out
