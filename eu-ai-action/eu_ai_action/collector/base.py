"""Evidence sources. Metadata only: no source may request conversation transcripts or file contents."""
from __future__ import annotations

import logging
from typing import Protocol

from ..models import AgentRef, Evidence, stable_hash

log = logging.getLogger(__name__)

# Keys that must never appear in collected payloads (metadata-only guarantee).
FORBIDDEN_KEYS = {"transcript", "transcripts", "messages", "conversation", "conversations", "chathistory", "filecontent"}


class EvidenceSource(Protocol):
    name: str

    def list_agents(self, tenant_id: str) -> list[AgentRef]: ...

    def collect(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[Evidence]: ...


def find_forbidden_keys(obj: object, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                hits.append(f"{path}/{k}")
            hits.extend(find_forbidden_keys(v, f"{path}/{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_forbidden_keys(v, f"{path}[{i}]"))
    return hits


def make_evidence(tenant_id: str, agent_id: str, scan_id: str, source: str, endpoint: str, payload: dict) -> Evidence:
    hits = find_forbidden_keys(payload)
    if hits:
        raise ValueError(f"{source} payload contains forbidden content keys: {hits}")
    h = stable_hash(payload)
    return Evidence(
        id=f"{agent_id}.{source}.{scan_id}",
        tenant_id=tenant_id,
        agent_id=agent_id,
        scan_id=scan_id,
        source=source,
        endpoint=endpoint,
        payload=payload,
        payload_hash=h,
    )


def collect_agent(
    sources: list[EvidenceSource], tenant_id: str, agent: AgentRef, scan_id: str
) -> tuple[list[Evidence], list[str]]:
    """Collect from every source. One failing source does not fail the agent."""
    evidence: list[Evidence] = []
    errors: list[str] = []
    for src in sources:
        try:
            evidence.extend(src.collect(tenant_id, agent, scan_id))
        except Exception as exc:  # noqa: BLE001 - isolate per source
            msg = f"{src.name}: {type(exc).__name__}: {exc}"
            log.warning("collector error for %s: %s", agent.agent_id, msg)
            errors.append(msg)
    return evidence, errors
