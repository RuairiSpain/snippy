from __future__ import annotations

import logging
from datetime import date

from ..collector import EvidenceSource, collect_agent
from ..documents import DocType, build_document, render_files
from ..documents.narrative import NarrativeGenerator
from ..facts import extract_facts
from ..models import AgentFacts, AgentRef, Document, DutyResult, FactValue, Task, now_iso
from ..rules import RuleSet
from ..rules.engine import evaluate_rule
from ..settings import Settings
from ..store import FileStore, Store

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        files: FileStore,
        sources: list[EvidenceSource],
        rule_set: RuleSet,
        doc_types: dict[str, DocType],
        narrative: NarrativeGenerator,
        today: date | None = None,
    ) -> None:
        self.s, self.store, self.files, self.sources = settings, store, files, sources
        self.rule_set, self.doc_types, self.narrative, self.today = rule_set, doc_types, narrative, today

    # ---- scan -------------------------------------------------------------------------------
    def list_agents(self, tenant_id: str) -> list[AgentRef]:
        return self.sources[0].list_agents(tenant_id)

    def new_scan(self, tenant_id: str, agent_count: int) -> str:
        scan_id = "scan-" + now_iso().replace(":", "").replace("-", "")
        self.store.upsert(
            "scans", tenant_id,
            {"id": scan_id, "tenantId": tenant_id, "startedAt": now_iso(), "status": "running", "agentCount": agent_count, "errors": []},
        )
        return scan_id

    def finish_scan(self, tenant_id: str, scan_id: str, errors: list[str]) -> dict:
        scan = self.store.get("scans", tenant_id, scan_id) or {"id": scan_id, "tenantId": tenant_id}
        scan.update(finishedAt=now_iso(), status="completed_with_errors" if errors else "completed", errors=errors)
        self.store.upsert("scans", tenant_id, scan)
        return scan

    def run_scan(self, tenant_id: str) -> dict:
        agents = self.list_agents(tenant_id)
        scan_id = self.new_scan(tenant_id, len(agents))
        errors: list[str] = []
        for a in agents:
            errors.extend(self.process_agent(tenant_id, a, scan_id))
        return self.finish_scan(tenant_id, scan_id, errors)

    def process_agent(self, tenant_id: str, agent: AgentRef, scan_id: str) -> list[str]:
        """Collect, extract facts, evaluate rules, generate documents for one agent. Returns error strings."""
        evidence, errors = collect_agent(self.sources, tenant_id, agent, scan_id)
        for e in evidence:
            self.store.upsert("evidence", tenant_id, e.to_doc())
        facts = extract_facts(tenant_id, agent, evidence, scan_id)
        existing = self.store.get("agents", tenant_id, agent.agent_id)
        if existing:
            facts.inputs = dict(existing.get("inputs", {}))
        self.store.upsert("agents", tenant_id, facts.to_doc())
        self.refresh_agent(tenant_id, facts)
        return errors

    # ---- rules and documents ----------------------------------------------------------------
    def human_inputs(self, tenant_id: str, facts: AgentFacts) -> dict[str, str]:
        tenant = self.store.get("tenants", tenant_id, tenant_id) or {}
        return {**tenant.get("defaults", {}), **facts.inputs}

    def org_profile(self, tenant_id: str) -> dict[str, str]:
        return dict((self.store.get("tenants", tenant_id, tenant_id) or {}).get("profile", {}))

    @staticmethod
    def eval_facts(facts: AgentFacts, org: dict[str, str], incident: dict | None = None, extra: dict[str, str] | None = None) -> AgentFacts:
        """Copy of the facts plus synthetic facts: org.* (tenant profile), input.* (human inputs), incident.* (current incident)."""
        f = facts.model_copy(deep=True)
        for k, v in org.items():
            f.facts[f"org.{k}"] = FactValue(value=str(v).strip().lower() if k != "country" else str(v).strip().upper(), source="organisation profile")
        for k, v in {**facts.inputs, **(extra or {})}.items():
            if ":" not in k:
                f.facts[f"input.{k}"] = FactValue(value=str(v).strip().lower(), source="human")
        for k, v in (incident or {}).items():
            f.facts[f"incident.{k}"] = FactValue(value=v, source="defender")
        return f

    def subjects(self, rule, facts: AgentFacts) -> list[tuple[str, dict | None]]:
        if rule.per == "incident":
            return [(str(i.get("id")), i) for i in facts.value("incidents", []) if i.get("id")]
        if rule.per == "request":
            ids = [x.strip() for x in str(facts.inputs.get("explanation_requests", "")).split(",") if x.strip()]
            return [(i, {"id": i}) for i in ids]
        return [("", None)]

    def refresh_agent(self, tenant_id: str, facts: AgentFacts) -> list[Document]:
        org = self.org_profile(tenant_id)
        inputs = self.human_inputs(tenant_id, facts)
        docs: list[Document] = []
        for rule in self.rule_set.rules:
            dt = self.doc_types.get(rule.document)
            for subject, incident in self.subjects(rule, facts):
                ef = self.eval_facts(facts, org, incident)
                duty = evaluate_rule(rule, ef, self.rule_set.version, self.today, subject)
                self.store.upsert("dutyResults", tenant_id, duty.to_doc())
                if duty.applicability != "applies" or dt is None:
                    continue
                for lang in self.s.languages:
                    docs.append(self._generate(tenant_id, facts, duty, dt, inputs, lang, subject, incident, org))
        self._reconcile_tasks(tenant_id, facts, docs)
        return docs

    def regenerate(self, tenant_id: str, agent_id: str) -> list[Document]:
        raw = self.store.get("agents", tenant_id, agent_id)
        if raw is None:
            raise KeyError(agent_id)
        return self.refresh_agent(tenant_id, AgentFacts.model_validate(raw))

    def latest_document(self, tenant_id: str, agent_id: str, doc_type: str, lang: str, subject: str = "") -> Document | None:
        rows = self.store.query("documents", tenant_id, agentId=agent_id, docType=doc_type, language=lang, subject=subject)
        if not rows:
            return None
        return Document.model_validate(max(rows, key=lambda r: r["version"]))

    def _generate(
        self, tenant_id: str, facts: AgentFacts, duty: DutyResult, dt: DocType, inputs: dict[str, str], lang: str,
        subject: str = "", incident: dict | None = None, org: dict[str, str] | None = None,
    ) -> Document:
        rule = self.rule_set.get(duty.rule_id)
        latest = self.latest_document(tenant_id, facts.agent_id, dt.id, lang, subject)
        doc = build_document(
            doc_type=dt, rule=rule, rule_set_version=self.rule_set.version, facts=facts, duty=duty,
            human_inputs=inputs, language=lang, narrative=self.narrative, version=(latest.version + 1) if latest else 1,
            subject=subject, incident=incident, org=org,
        )
        if latest and latest.input_hash == doc.input_hash:
            return latest  # nothing changed
        rendered = render_files(self.s.templates_dir, doc, dt.titles[lang], dt, tuple(self.s.formats))
        for fmt, data in rendered.items():
            doc.file_paths[fmt] = self.files.put(tenant_id, f"{doc.id}.{fmt}", data)
        from ..models import stable_hash

        doc.file_hash = stable_hash(rendered["md"].decode("utf-8"))
        if latest:
            if latest.state == "approved":
                latest.stale = True
            elif latest.state != "superseded":
                latest.state = "superseded"
            self.store.upsert("documents", tenant_id, latest.to_doc())
        self.store.upsert("documents", tenant_id, doc.to_doc())
        return doc

    def rerender(self, tenant_id: str, doc: Document) -> Document:
        """Re-render files for an existing document (after approval, so the footer and banner show the new state)."""
        dt = self.doc_types[doc.doc_type]
        rendered = render_files(self.s.templates_dir, doc, dt.titles[doc.language], dt, tuple(self.s.formats))
        for fmt, data in rendered.items():
            doc.file_paths[fmt] = self.files.put(tenant_id, f"{doc.id}.{fmt}", data)
        self.store.upsert("documents", tenant_id, doc.to_doc())
        return doc

    # ---- tasks ------------------------------------------------------------------------------
    def _reconcile_tasks(self, tenant_id: str, facts: AgentFacts, docs: list[Document]) -> None:
        needed: dict[str, str] = {}
        for d in docs:
            for n, f in d.fields.items():
                if f.source == "human" and f.pending:
                    needed[f"{d.subject}:{n}" if d.subject else n] = d.doc_type
        if facts.value("owner") is None:
            needed["owner"] = "register-entry"
        for row in self.store.query("tasks", tenant_id, agentId=facts.agent_id, kind="missing_input"):
            t = Task.model_validate(row)
            if t.field not in needed and t.status == "open":
                t.status = "done"
                self.store.upsert("tasks", tenant_id, t.to_doc())
        for field, doc_type in needed.items():
            tid = f"{facts.agent_id}.{field}"
            old = self.store.get("tasks", tenant_id, tid)
            if old and old.get("status") == "open":
                continue
            self.store.upsert(
                "tasks", tenant_id,
                Task(id=tid, tenant_id=tenant_id, agent_id=facts.agent_id, field=field, doc_type=doc_type,
                     title=f"Provide '{field}' for {facts.name}").to_doc(),
            )

    # ---- readiness --------------------------------------------------------------------------
    def readiness(self, tenant_id: str, agent_id: str) -> dict:
        duties = [DutyResult.model_validate(d) for d in self.store.query("dutyResults", tenant_id, agentId=agent_id)]
        applicable = [d for d in duties if d.applicability == "applies"]
        counts = {"pass": 0, "fail": 0, "unknown": 0, "none": 0}
        satisfied = 0
        for d in applicable:
            counts[d.result] += 1
            rule = self.rule_set.get(d.rule_id)
            dt = self.doc_types.get(rule.document)
            approved = True
            if dt is not None:
                for lang in self.s.languages:
                    cur = self.latest_document(tenant_id, agent_id, dt.id, lang, d.subject)
                    approved = approved and bool(cur and cur.state == "approved" and not cur.stale)
            if d.result in ("pass", "none") and approved:
                satisfied += 1
        return {"applicable": len(applicable), "satisfied": satisfied, **counts}
