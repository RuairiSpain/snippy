import io
import json

import pytest
from docx import Document as Docx

from eu_ai_action.documents.narrative import AzureOpenAINarrative, DerivedNarrative, sanitize
from eu_ai_action.models import Document
from tests.conftest import TENANT, scan_agent

PUBLIC = "agent-public-nodisclosure"


def latest(p, agent, dt, lang):
    return p.latest_document(TENANT, agent, dt, lang)


def doc_types_for(pipeline, agent_id):
    return {d["docType"] for d in pipeline.store.query("documents", TENANT, agentId=agent_id) if d["language"] == "en"}


def test_scan_generates_expected_documents(pipeline):
    scan = pipeline.run_scan(TENANT)
    assert scan["status"] == "completed" and scan["agentCount"] == 10
    base = {"register-entry", "ropa-entry", "risk-classification", "literacy-record"}
    # every agent gets the base set; public agents also get the Art. 50(1) check
    # no model evidence: whether the agent generates synthetic content is an open question, so the Art. 50(2) check is created as "unknown"
    marking = {"synthetic-content-marking-check"}
    assert doc_types_for(pipeline, "agent-teams-internal") == base | marking
    assert doc_types_for(pipeline, "agent-public-nodisclosure") == base | marking | {"ai-interaction-disclosure"}
    # keyword matching flags recruiting and credit agents as high-risk candidates, which adds the deployer documents
    high = base | {"oversight-record", "log-retention-attestation", "dpia-input-pack", "input-data-relevance", "instructions-for-use-check"}
    assert doc_types_for(pipeline, "agent-recruiting") >= high
    # both languages exist for every document and nothing is approved yet
    for d in pipeline.store.query("documents", TENANT):
        assert d["state"] == "draft" and d["version"] == 1
    n_en = len([d for d in pipeline.store.query("documents", TENANT) if d["language"] == "en"])
    n_es = len([d for d in pipeline.store.query("documents", TENANT) if d["language"] == "es"])
    assert n_en == n_es


def test_files_rendered_in_all_formats(pipeline):
    pipeline.s.render_formats = "docx,pdf,md"
    scan_agent(pipeline, PUBLIC)
    d = latest(pipeline, PUBLIC, "ai-interaction-disclosure", "en")
    docx = Docx(io.BytesIO(pipeline.files.get(d.file_paths["docx"])))
    text = "\n".join(p.text for p in docx.paragraphs) + "\n".join(c.text for t in docx.tables for r in t.rows for c in r.cells)
    assert "disclosure missing" in text and "Public Help Bot" in text
    assert pipeline.files.get(d.file_paths["pdf"]).startswith(b"%PDF")
    es = latest(pipeline, PUBLIC, "ai-interaction-disclosure", "es").file_paths["md"]
    assert "información ausente" in pipeline.files.get(es).decode()


def test_unreviewed_banner_and_no_compliance_claim(pipeline):
    scan_agent(pipeline, PUBLIC)
    for raw in pipeline.store.query("documents", TENANT):
        d = Document.model_validate(raw)
        md = pipeline.files.get(d.file_paths["md"]).decode()
        assert ("DRAFT" in md) or ("BORRADOR" in md)
        assert not d.counsel_reviewed


def test_human_fields_pending_until_supplied(pipeline):
    scan_agent(pipeline, PUBLIC)
    d = latest(pipeline, PUBLIC, "register-entry", "en")
    assert d.fields["business_purpose"].pending and d.fields["business_purpose"].source == "human"
    assert "[PENDING INPUT]" in pipeline.files.get(d.file_paths["md"]).decode()
    assert any(t["field"] == "business_purpose" for t in pipeline.store.query("tasks", TENANT, status="open"))


def test_changed_input_creates_new_version_and_supersedes(pipeline):
    scan_agent(pipeline, PUBLIC)
    facts = pipeline.store.get("agents", TENANT, PUBLIC)
    facts["inputs"] = {"business_purpose": "Answer product questions", "intended_users": "Website visitors"}
    pipeline.store.upsert("agents", TENANT, facts)
    pipeline.regenerate(TENANT, PUBLIC)
    v2 = latest(pipeline, PUBLIC, "register-entry", "en")
    assert v2.version == 2 and not v2.fields["business_purpose"].pending
    old = pipeline.store.get("documents", TENANT, f"{PUBLIC}.register-entry.en.v1")
    assert old["state"] == "superseded"
    # unchanged inputs: no new version
    pipeline.regenerate(TENANT, PUBLIC)
    assert latest(pipeline, PUBLIC, "register-entry", "en").version == 2
    # tasks for the supplied fields are closed
    open_fields = {t["field"] for t in pipeline.store.query("tasks", TENANT, agentId=PUBLIC, status="open")}
    assert "business_purpose" not in open_fields


def test_rescan_unchanged_creates_no_new_versions(pipeline):
    scan_agent(pipeline, PUBLIC)
    n = len(pipeline.store.query("documents", TENANT))
    scan_agent(pipeline, PUBLIC)
    assert len(pipeline.store.query("documents", TENANT)) == n


def test_autofill_ratio_excludes_human_fields(pipeline):
    scan_agent(pipeline, "agent-public-disclosed")
    scan_agent(pipeline, "agent-hr-faq")
    d = latest(pipeline, "agent-public-disclosed", "ai-interaction-disclosure", "en")
    assert d.autofill_ratio == 1.0
    ropa = latest(pipeline, "agent-hr-faq", "ropa-entry", "en")
    assert ropa.autofill_ratio >= 0.8


def test_derived_narrative_is_factual():
    from tests.unit.test_facts import facts_for

    r = DerivedNarrative().generate(facts_for("agent-hr-faq"), ["agent_description"], "en")
    assert "HR Policy Assistant" in r.values["agent_description"] and r.source == "derived"


@pytest.mark.parametrize(
    "text", ["The agent is compliant with the AI Act.", "Cumple con el Reglamento.", "It is legally approved by the DPO.", "", None, 5]
)
def test_forbidden_claims_become_none(text):
    assert sanitize({"x": text}, ["x"]) == {"x": None}


class FakeClient:
    def __init__(self, content):
        self.content = content
        self.calls = []
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls.append(kw)
                msg = type("M", (), {"content": outer.content})
                return type("R", (), {"choices": [type("Ch", (), {"message": msg})]})

        self.chat = type("Chat", (), {"completions": _C()})


def test_llm_narrative_validates_output_and_records_prompt():
    from tests.unit.test_facts import facts_for

    fake = FakeClient(json.dumps({"agent_description": "Answers HR policy questions for employees."}))
    n = AzureOpenAINarrative("e", "dep", "v", client=fake)
    r = n.generate(facts_for("agent-hr-faq"), ["agent_description"], "en")
    assert r.values["agent_description"].startswith("Answers HR") and r.model == "dep" and r.prompt_hash
    kw = fake.calls[0]
    assert kw["temperature"] == 0 and kw["response_format"]["type"] == "json_schema"
    # the prompt carries facts only, no transcripts
    assert "transcript" not in json.dumps(kw["messages"]).lower()


def test_llm_failure_or_bad_json_yields_pending_not_exception():
    from tests.unit.test_facts import facts_for

    n = AzureOpenAINarrative("e", "dep", "v", client=FakeClient("not json"))
    assert n.generate(facts_for("agent-hr-faq"), ["agent_description"], "en").values == {"agent_description": None}
    n = AzureOpenAINarrative("e", "dep", "v", client=FakeClient(json.dumps({"agent_description": "This agent is compliant."})))
    assert n.generate(facts_for("agent-hr-faq"), ["agent_description"], "en").values == {"agent_description": None}


def test_platform_filled_and_empty_lists_are_findings_not_gaps(pipeline):
    scan_agent(pipeline, "agent-teams-internal")
    scan_agent(pipeline, "agent-no-owner")
    d = latest(pipeline, "agent-teams-internal", "register-entry", "en")
    assert d.fields["platform"].value == "Microsoft Copilot Studio"
    assert d.fields["connectors"].value == "None found" and not d.fields["connectors"].pending
    es = latest(pipeline, "agent-teams-internal", "register-entry", "es")
    assert es.fields["connectors"].value == "Ninguno encontrado"
    # an agent with no registry data keeps owner pending
    assert latest(pipeline, "agent-no-owner", "register-entry", "en").fields["owner"].pending
