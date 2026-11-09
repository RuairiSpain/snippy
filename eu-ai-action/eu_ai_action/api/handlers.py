"""Framework-free route logic. function_app.py and scripts/dev_server.py only translate HTTP to these calls."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..export import build_bundle, collect_approved, verify_document
from ..export.bundle import md_hash
from ..models import AgentFacts, Document, now_iso
from ..notifications import notify_open_tasks
from ..review import TransitionError, transition
from ..services import Pipeline
from ..settings import Settings
from .auth import AuthError, authorize

# Inputs that steer which duties apply (read by the rules as input.<name>).
YES_NO_INPUTS = {
    "used_in_workplace", "decisions_about_persons", "dpia_required", "substantial_modification",
    "publishes_public_interest_content", "iso42001",
}
CONTROL_INPUTS = YES_NO_INPUTS | {"risk_tier", "explanation_requests"}
RISK_TIERS = {"minimal", "limited", "high", "prohibited-concern"}
TENANT_DEFAULTS = {"controller_name", "controller_contact"}
PROFILE_FIELDS = {
    "country", "is_public_body", "provides_public_services", "credit_or_insurance_pricing",
    "is_financial_entity", "is_nis2_entity", "is_employer", "iso42001_in_scope",
}
MAX_INPUT_LEN = 2000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class Api:
    def __init__(self, settings: Settings, pipeline: Pipeline) -> None:
        self.s, self.p = settings, pipeline
        self.human_fields = {f.name for dt in pipeline.doc_types.values() for f in dt.fields if f.source == "human"}
        self.labels = {f.name: f.label.get("en", "") for dt in pipeline.doc_types.values() for f in dt.fields if f.source == "human" and f.label}
        self.editable = self.human_fields | CONTROL_INPUTS

    def _check_input(self, key: str, value: Any) -> str:
        """Validates one input key and value. Keys may be prefixed with a subject id (incident or request): 'INC-1:field'."""
        field = key.split(":", 1)[1] if ":" in key else key
        if ":" in key and not ID_RE.match(key.split(":", 1)[0]):
            raise ApiError(400, f"invalid subject in {key!r}")
        if field not in self.editable:
            raise ApiError(400, f"unknown input field: {key}")
        if not isinstance(value, str) or len(value) > MAX_INPUT_LEN:
            raise ApiError(400, f"{key} must be a string of at most {MAX_INPUT_LEN} characters")
        v = value.strip()
        if field in YES_NO_INPUTS and v and v.lower() not in {"yes", "no"}:
            raise ApiError(400, f"{key} must be yes or no")
        if field == "risk_tier" and v and v.lower() not in RISK_TIERS:
            raise ApiError(400, f"risk_tier must be one of: {', '.join(sorted(RISK_TIERS))}")
        if field == "explanation_requests" and v and not all(ID_RE.match(x.strip()) for x in v.split(",")):
            raise ApiError(400, "explanation_requests must be comma-separated ids (letters, digits, - and _)")
        return v

    def _user(self, headers: Mapping[str, str], tenant_id: str, _trusted: bool = False, write: bool = False) -> str:
        if _trusted:
            return "mcp"
        try:
            return authorize(headers, tenant_id, self.s, write)
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
        self._user(headers, tenant_id, write=True)
        return self.p.run_scan(tenant_id)

    def profile_status(self, tenant_id: str) -> dict[str, Any]:
        profile = (self.p.store.get("tenants", tenant_id, tenant_id) or {}).get("profile", {})
        missing = sorted(PROFILE_FIELDS - set(profile))
        return {"profile": profile, "missing": missing, "complete": not missing}

    def get_profile(self, headers: Mapping[str, str], tenant_id: str) -> dict[str, Any]:
        self._user(headers, tenant_id)
        return self.profile_status(tenant_id)

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
            "inputLabels": self.labels,
        }

    def patch_inputs(self, headers: Mapping[str, str], tenant_id: str, agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._user(headers, tenant_id, write=True)
        a = self._agent(tenant_id, agent_id)
        clean = {k: self._check_input(k, v) for k, v in body.items()}
        for k, v in clean.items():
            if v:
                a.inputs[k] = v
            else:
                a.inputs.pop(k, None)
        a.updated_at = now_iso()
        self.p.store.upsert("agents", tenant_id, a.to_doc())
        self.p.refresh_agent(tenant_id, a)
        return self.get_agent(headers, tenant_id, agent_id)

    def patch_profile(self, headers: Mapping[str, str], tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Organisation profile: decides which regulations apply (DORA, NIS2, FRIA scope, Spain-specific documents)."""
        self._user(headers, tenant_id, write=True)
        tenant = self.p.store.get("tenants", tenant_id, tenant_id) or {"id": tenant_id, "tenantId": tenant_id, "defaults": {}}
        profile = tenant.setdefault("profile", {})
        for k, v in body.items():
            if k not in PROFILE_FIELDS:
                raise ApiError(400, f"unknown profile field: {k}")
            v = str(v).strip()
            if k == "country":
                if v and not re.fullmatch(r"[A-Za-z]{2}", v):
                    raise ApiError(400, "country must be a two-letter code")
            elif v and v.lower() not in {"yes", "no"}:
                raise ApiError(400, f"{k} must be yes or no")
            if v:
                profile[k] = v
            else:
                profile.pop(k, None)
        self.p.store.upsert("tenants", tenant_id, tenant)
        for raw in self.p.store.query("agents", tenant_id):
            self.p.refresh_agent(tenant_id, AgentFacts.model_validate(raw))
        return self.profile_status(tenant_id)

    def patch_defaults(self, headers: Mapping[str, str], tenant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._user(headers, tenant_id, write=True)
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
        self._user(headers, tenant_id, write=True)
        self._agent(tenant_id, agent_id)
        self.p.regenerate(tenant_id, agent_id)
        return self.get_agent(headers, tenant_id, agent_id)

    def transition_document(self, headers: Mapping[str, str], tenant_id: str, doc_id: str, action: str) -> dict[str, Any]:
        user = self._user(headers, tenant_id, write=True)
        doc = self._doc(tenant_id, doc_id)
        try:
            new, audit = transition(doc, action, user, self.s.require_counsel_review_for_release)
        except TransitionError as e:
            raise ApiError(409, str(e)) from e
        if action == "approve":
            new = self.p.rerender(tenant_id, new)  # re-renders with the approved banner and stores the file
            new.file_hash = md_hash(self.p, new)
            audit["fileHash"] = new.file_hash  # the hash the auditor verifies against
        self.p.store.upsert("documents", tenant_id, new.to_doc())
        self.p.store.upsert("audit", tenant_id, audit)
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

    # ---- audit, export, notifications ---------------------------------------------------------
    def audit_pack(self, headers: Mapping[str, str], tenant_id: str) -> dict[str, Any]:
        """Read-only view for auditors: approved documents with approver, rule-set version, hashes, and the audit log."""
        self._user(headers, tenant_id)
        approved = collect_approved(self.p, tenant_id)
        return {
            "generatedAt": now_iso(),
            "ruleSetVersion": self.p.rule_set.version,
            "documents": [
                {"id": d.id, "agentId": d.agent_id, "type": d.doc_type, "language": d.language, "version": d.version, "subject": d.subject,
                 "approvedBy": d.approved_by, "approvedAt": d.approved_at, "counselReviewed": d.counsel_reviewed, "fileHash": d.file_hash}
                for d in approved
            ],
            "auditLog": sorted(self.p.store.query("audit", tenant_id), key=lambda a: a["at"]),
        }

    def verify(self, headers: Mapping[str, str], tenant_id: str, doc_id: str) -> dict[str, Any]:
        self._user(headers, tenant_id)
        try:
            return verify_document(self.p, tenant_id, doc_id)
        except KeyError as e:
            raise ApiError(404, "document not found") from e

    def export_bundle(self, headers: Mapping[str, str], tenant_id: str, cm_export: bytes | None = None) -> bytes:
        self._user(headers, tenant_id)
        if cm_export is not None and len(cm_export) > 10 * 1024 * 1024:
            raise ApiError(400, "Compliance Manager export is larger than 10 MB")
        try:
            return build_bundle(self.p, tenant_id, cm_export)
        except Exception as e:  # noqa: BLE001 - a malformed workbook is a client error
            if cm_export is not None and "zip" in str(e).lower() or "xlsx" in str(e).lower():
                raise ApiError(400, "the Compliance Manager export could not be read as an .xlsx file") from e
            raise

    def notify(self, headers: Mapping[str, str], tenant_id: str, portal_url: str = "") -> dict[str, Any]:
        self._user(headers, tenant_id, write=True)
        try:
            return notify_open_tasks(self.p, tenant_id, self.s.teams_webhook_url, portal_url)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
