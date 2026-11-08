"""Framework-free route logic. function_app.py and scripts/dev_server.py only translate HTTP to these calls."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import AgentFacts, Document, now_iso
from ..review import TransitionError, transition
from ..services import Pipeline
from ..settings import Settings
from .auth import AuthError, authorize

EDITABLE_INPUTS = {
    "business_purpose", "intended_users", "controller_name", "controller_contact", "processing_purposes",
    "data_subjects", "recipients", "third_country_transfers", "retention_period",
}
TENANT_DEFAULTS = {"controller_name", "controller_contact"}
MAX_INPUT_LEN = 2000


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class Api:
    def __init__(self, settings: Settings, pipeline: Pipeline) -> None:
        self.s, self.p = settings, pipeline

    def _user(self, headers: Mapping[str, str], tenant_id: str, _trusted: bool = False) -> str:
        if _trusted:
            return "mcp"
        try:
            return authorize(headers, tenant_id, self.s)
        except AuthError as e:
            raise ApiError(e.status, e.message) from e

    def _agent(self, tenant_id: str, agent_id: str) -> AgentFacts:
        raw = self.p.store.get("agents", tenant_id, agent_id)
        if raw is None:
            raise ApiError(404, "agent not found")
        return AgentFacts.model_validate(raw)

    def _doc(self, tenant_id: str, doc_id: str) -> Document:
        raw = self.p.store.get("documents", tenant_id, doc_id)
        if raw is None:
            raise ApiError(404, "document not found")
        return Document.model_validate(raw)

    # ---- internal accessors (MCP tools): caller identity is the function system key, tenant fixed by settings ----
    def list_agents_internal(self, tenant_id: str) -> list[dict[str, Any]]:
        return self.list_agents({}, tenant_id, _trusted=True)

    def get_agent_internal(self, tenant_id: str, agent_id: str) -> dict[str, Any]:
        return self.get_agent({}, tenant_id, agent_id, _trusted=True)

    # ---- routes -----------------------------------------------------------------------------
    def start_scan(self, headers: Mapping[str, str], tenant_id: str) -> dict[str, Any]:
        self._user(headers, tenant_id)
        return self.p.run_scan(tenant_id)

    def list_agents(self, headers: Mapping[str, str], tenant_id: str, _trusted: bool = False) -> list[dict[str, Any]]:
        self._user(headers, tenant_id, _trusted)
        out = []
        for raw in self.p.store.query("agents", tenant_id):
            a = AgentFacts.model_validate(raw)
            out.append(
                {
                    "agentId": a.agent_id, "name": a.name, "owner": a.value("owner"), "isPublicFacing": a.value("isPublicFacing"),
                    "readiness": self.p.readiness(tenant_id, a.agent_id),
                }
            )
        return sorted(out, key=lambda x: x["name"])

    def get_agent(self, headers: Mapping[str, str], tenant_id: str, agent_id: str, _trusted: bool = False) -> dict[str, Any]:
        self._user(headers, tenant_id, _trusted)
        a = self._agent(tenant_id, agent_id)
        docs: dict[str, Any] = {}
        for raw in self.p.store.query("documents", tenant_id, agentId=agent_id):
            d = Document.model_validate(raw)
            key = (d.doc_type, d.language)
            if key not in docs or docs[key].version < d.version:
                docs[key] = d
        return {
            "agent": a.to_doc(),
            "dutyResults": self.p.store.query("dutyResults", tenant_id, agentId=agent_id),
            "documents": [
                {**d.to_doc(), "autofillRatio": round(d.autofill_ratio, 2)}
                for d in sorted(docs.values(), key=lambda x: (x.doc_type, x.language))
            ],
            "readiness": self.p.readiness(tenant_id, agent_id),
        }

    def patch_inputs(self, headers: Mapping[str, str], tenant_id: str, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._user(headers, tenant_id)
        a = self._agent(tenant_id, agent_id)
        bad = [k for k in body if k not in EDITABLE_INPUTS]
        if bad:
            raise ApiError(400, f"unknown input fields: {', '.join(bad)}")
        for k, v in body.items():
            if not isinstance(v, str) or len(v) > MAX_INPUT_LEN:
                raise ApiError(400, f"{k} must be a string of at most {MAX_INPUT_LEN} characters")
            if v.strip():
                a.inputs[k] = v.strip()
            else:
                a.inputs.pop(k, None)
        a.updated_at = now_iso()
        self.p.store.upsert("agents", tenant_id, a.to_doc())
        self.p.refresh_agent(tenant_id, a)
        return self.get_agent(headers, tenant_id, agent_id)

    def patch_defaults(self, headers: Mapping[str, str], tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._user(headers, tenant_id)
        bad = [k for k in body if k not in TENANT_DEFAULTS]
        if bad:
            raise ApiError(400, f"unknown default fields: {', '.join(bad)}")
        tenant = self.p.store.get("tenants", tenant_id, tenant_id) or {"id": tenant_id, "tenantId": tenant_id, "defaults": {}}
        for k, v in body.items():
            if not isinstance(v, str) or len(v) > MAX_INPUT_LEN:
                raise ApiError(400, f"{k} must be a string of at most {MAX_INPUT_LEN} characters")
            tenant["defaults"][k] = v.strip()
        self.p.store.upsert("tenants", tenant_id, tenant)
        for raw in self.p.store.query("agents", tenant_id):
            self.p.refresh_agent(tenant_id, AgentFacts.model_validate(raw))
        return tenant

    def regenerate(self, headers: Mapping[str, str], tenant_id: str, agent_id: str) -> dict[str, Any]:
        self._user(headers, tenant_id)
        self._agent(tenant_id, agent_id)
        self.p.regenerate(tenant_id, agent_id)
        return self.get_agent(headers, tenant_id, agent_id)

    def transition_document(self, headers: Mapping[str, str], tenant_id: str, doc_id: str, action: str) -> dict[str, Any]:
        user = self._user(headers, tenant_id)
        doc = self._doc(tenant_id, doc_id)
        try:
            new, audit = transition(doc, action, user, self.s.require_counsel_review_for_release)
        except TransitionError as e:
            raise ApiError(409, str(e)) from e
        self.p.store.upsert("documents", tenant_id, new.to_doc())
        self.p.store.upsert("audit", tenant_id, audit)
        if action == "approve":
            new = self.p.rerender(tenant_id, new)
        return {**new.to_doc(), "warnings": audit["warnings"]}

    def get_file(self, headers: Mapping[str, str], tenant_id: str, doc_id: str, fmt: str) -> tuple[bytes, str]:
        self._user(headers, tenant_id)
        doc = self._doc(tenant_id, doc_id)
        if fmt not in doc.file_paths:
            raise ApiError(404, "format not available")
        types = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "md": "text/markdown; charset=utf-8",
        }
        return self.p.files.get(doc.file_paths[fmt]), types[fmt]

    def list_tasks(self, headers: Mapping[str, str], tenant_id: str) -> list[dict[str, Any]]:
        self._user(headers, tenant_id)
        return self.p.store.query("tasks", tenant_id, status="open")
