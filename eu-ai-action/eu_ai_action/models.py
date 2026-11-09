"""Typed records shared across the pipeline. Stored documents use camelCase keys."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def to_doc(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class AgentRef(CamelModel):
    agent_id: str
    name: str = ""


class Evidence(CamelModel):
    id: str
    tenant_id: str
    agent_id: str
    scan_id: str
    source: str
    endpoint: str = ""
    retrieved_at: str = Field(default_factory=now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = ""


class FactValue(CamelModel):
    value: Any = None
    source: str | None = None
    evidence_id: str | None = None
    confidence: float = 1.0
    note: str = ""


class AgentFacts(CamelModel):
    id: str  # agent id
    tenant_id: str
    agent_id: str
    name: str
    platform: str = "copilot-studio"
    facts: dict[str, FactValue] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)  # human-supplied values
    last_scan_id: str = ""
    updated_at: str = Field(default_factory=now_iso)

    def value(self, key: str, default: Any = None) -> Any:
        fv = self.facts.get(key)
        return default if fv is None or fv.value is None else fv.value


Result = Literal["pass", "fail", "unknown", "none"]
Applicability = Literal["applies", "not_applicable"]


class DutyResult(CamelModel):
    id: str  # f"{agent_id}.{rule_id}"
    tenant_id: str
    agent_id: str
    rule_id: str
    rule_version: int
    rule_set_version: str
    subject: str = ""
    regulation: str
    article: str
    applicability: Applicability
    result: Result = "none"
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    counsel_reviewed: bool = False
    evaluated_at: str = Field(default_factory=now_iso)


class FieldValue(CamelModel):
    value: str | None = None
    source: Literal["fact", "rule_result", "llm", "derived", "human", "static"] = "static"
    ref: str = ""
    pending: bool = False


DocState = Literal["draft", "in_review", "approved", "rejected", "superseded"]


class Document(CamelModel):
    id: str
    tenant_id: str
    agent_id: str
    doc_type: str
    language: str
    version: int
    subject: str = ""  # incident id or request id for per-subject documents
    state: DocState = "draft"
    stale: bool = False
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    input_hash: str = ""
    rule_id: str = ""
    rule_set_version: str = ""
    counsel_reviewed: bool = False
    llm_model: str = ""
    llm_prompt_hash: str = ""
    file_paths: dict[str, str] = Field(default_factory=dict)  # format -> path
    file_hash: str = ""
    created_at: str = Field(default_factory=now_iso)
    approved_by: str = ""
    approved_at: str = ""

    @property
    def autofill_ratio(self) -> float:
        total = len(self.fields)
        if not total:
            return 1.0
        filled = sum(1 for f in self.fields.values() if not f.pending and f.source != "human")
        human = sum(1 for f in self.fields.values() if f.source == "human")
        auto_total = total - human
        return filled / auto_total if auto_total else 1.0


class Task(CamelModel):
    id: str
    tenant_id: str
    agent_id: str
    kind: Literal["missing_input", "review"] = "missing_input"
    field: str = ""
    doc_type: str = ""
    status: Literal["open", "done"] = "open"
    title: str = ""
    assignee: str = ""
    created_at: str = Field(default_factory=now_iso)
