"""v1 and v2 documents: new evidence, organisation profile gating, per-incident and per-request documents."""
import pytest
from docx import Document as Docx

from eu_ai_action.api import Api, ApiError
from eu_ai_action.models import Document
from tests.conftest import TENANT, scan_agent, set_profile
from tests.unit.test_facts import FIX  # noqa: F401  (fixtures dir)

HR, REC, LOAN, PUB = "agent-hr-faq", "agent-recruiting", "agent-loan-prescreen", "agent-public-nodisclosure"


def facts_ext(agent_id):
    import json

    from eu_ai_action.collector.base import make_evidence
    from eu_ai_action.facts import extract_facts
    from eu_ai_action.models import AgentRef

    d = json.loads((FIX / f"{agent_id}.json").read_text(encoding="utf-8"))
    ev = [make_evidence("t", agent_id, "s1", k, f"fixture:{k}", d[k]) for k in d if k not in ("agent_id", "name")]
    return extract_facts("t", AgentRef(agent_id=agent_id, name=d["name"]), ev, "s1")


def types(pipeline, agent, lang="en"):
    return {d["docType"] for d in pipeline.store.query("documents", TENANT, agentId=agent, language=lang)}


def docs(pipeline, agent, doc_type, lang="en", subject=""):
    return pipeline.latest_document(TENANT, agent, doc_type, lang, subject)


# ---- evidence -> facts -------------------------------------------------------------------------
def test_new_evidence_facts():
    hr = facts_ext(HR)
    assert hr.value("literacyGaps") == ["Pia Sanz"] and hr.value("literacyComplete") is False
    assert hr.value("minRetentionDays") == 90 and hr.value("retentionOk") is False
    assert hr.value("overseers") == ["Luis Mora"]
    assert [i["id"] for i in hr.value("incidents")] == ["INC-100"]
    assert facts_ext(REC).value("fineTunedModel") is True
    loan = facts_ext(LOAN)
    assert loan.value("generatesSyntheticContent") is True and loan.value("contentMarking") is True
    assert "5 Access to essential services and credit" in loan.value("suggestedAnnexIII")
    assert "4 Employment and workers management" in facts_ext(REC).value("suggestedAnnexIII")
    # agents without the evidence: unknown, not false
    pub = facts_ext(PUB)
    assert pub.value("literacyComplete") is None and pub.value("retentionOk") is None and pub.value("generatesSyntheticContent") is None


def test_no_incident_means_empty_list_not_missing():
    assert facts_ext(PUB).value("incidents") == []


# ---- rules over the new facts ------------------------------------------------------------------
def result(pipeline, agent, rule, subject=""):
    rid = f"{agent}.{rule}" + (f".{subject}" if subject else "")
    r = pipeline.store.get("dutyResults", TENANT, rid)
    return (r["applicability"], r["result"]) if r else None


def test_literacy_check(pipeline):
    for a in (HR, REC, PUB):
        scan_agent(pipeline, a)
    assert result(pipeline, HR, "AIA-4-LITERACY") == ("applies", "fail")
    assert result(pipeline, REC, "AIA-4-LITERACY") == ("applies", "pass")
    assert result(pipeline, PUB, "AIA-4-LITERACY") == ("applies", "unknown")


def test_high_risk_candidates_and_confirmation(pipeline):
    for a in (HR, REC, LOAN):
        scan_agent(pipeline, a)
    # keyword suggestion alone makes the recruiting and loan agents high-risk candidates; the HR FAQ agent is not
    assert result(pipeline, REC, "AIA-26-2-OVERSIGHT") == ("applies", "pass")
    assert result(pipeline, LOAN, "AIA-26-6-LOG-RETENTION") == ("applies", "unknown")
    assert result(pipeline, REC, "AIA-26-6-LOG-RETENTION") == ("applies", "pass")
    assert result(pipeline, HR, "AIA-26-2-OVERSIGHT")[0] == "not_applicable"
    # a human confirmation overrides the suggestion in both directions
    a = pipeline.store.get("agents", TENANT, REC)
    a["inputs"] = {"risk_tier": "limited"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, REC)
    assert result(pipeline, REC, "AIA-26-2-OVERSIGHT")[0] == "not_applicable"
    a = pipeline.store.get("agents", TENANT, HR)
    a["inputs"] = {"risk_tier": "high"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, HR)
    assert result(pipeline, HR, "AIA-26-6-LOG-RETENTION") == ("applies", "fail")  # 90 days < 183


def test_marking_check_and_unknown_model_evidence(pipeline):
    for a in (HR, REC, LOAN, PUB):
        scan_agent(pipeline, a)
    assert result(pipeline, LOAN, "AIA-50-2-MARKING") == ("applies", "pass")
    assert result(pipeline, HR, "AIA-50-2-MARKING") == ("applies", "fail")
    assert result(pipeline, REC, "AIA-50-2-MARKING")[0] == "not_applicable"  # evidence says it generates no content
    assert result(pipeline, PUB, "AIA-50-2-MARKING") == ("applies", "unknown")  # no evidence: open question


def test_public_facing_unknown_is_not_skipped(pipeline):
    from eu_ai_action.rules import evaluate_rules, load_rules
    from eu_ai_action.settings import Settings

    f = facts_ext("agent-teams-internal")
    f.facts["isPublicFacing"].value = None
    d = {x.rule_id: x for x in evaluate_rules(load_rules(Settings(_env_file=None).rules_dir), f)}["AIA-50-1-disclosure"]
    assert d.applicability == "applies"


# ---- organisation profile gating ---------------------------------------------------------------
def test_without_profile_no_sector_documents(pipeline):
    scan_agent(pipeline, LOAN)
    t = types(pipeline, LOAN)
    assert "serious-incident-report" in t  # AI Act incident does not depend on the profile
    assert not t & {"dora-incident-report", "dora-register-rows", "nis2-incident-report", "nis2-asset-entry", "aesia-incident-notice", "fria", "ens-evidence-pack"}


def test_profile_enables_sector_documents(pipeline):
    set_profile(pipeline, is_financial_entity="yes", is_nis2_entity="yes", country="es", credit_or_insurance_pricing="yes")
    scan_agent(pipeline, LOAN)
    t = types(pipeline, LOAN)
    assert {"dora-incident-report", "dora-register-rows", "nis2-incident-report", "nis2-asset-entry", "aesia-incident-notice", "fria"} <= t
    assert "ens-evidence-pack" not in t  # not a public body


def test_deadlines_are_computed_from_incident_times(pipeline):
    set_profile(pipeline, is_financial_entity="yes", is_nis2_entity="yes", country="ES")
    scan_agent(pipeline, LOAN)
    dora = docs(pipeline, LOAN, "dora-incident-report", subject="INC-200")
    assert dora.fields["initial_deadline"].value == "2026-11-03 15:00 UTC"  # classified 11:00 + 4h
    assert dora.fields["initial_cap"].value == "2026-11-04 09:30 UTC"  # detected 09:30 + 24h
    assert dora.fields["intermediate_deadline"].value == "2026-11-06 11:00 UTC"
    nis2 = docs(pipeline, LOAN, "nis2-incident-report", subject="INC-200")
    assert nis2.fields["early_warning"].value == "2026-11-04 09:30 UTC" and nis2.fields["notification"].value == "2026-11-06 09:30 UTC"
    assert docs(pipeline, LOAN, "serious-incident-report", subject="INC-200").fields["general_deadline"].value == "2026-11-18 09:30 UTC"
    assert dora.fields["initial_deadline"].source == "derived"


def test_incident_documents_follow_incident_flags(pipeline):
    set_profile(pipeline, is_financial_entity="yes")
    scan_agent(pipeline, HR)
    t = types(pipeline, HR)
    assert "breach-notification-draft" in t  # INC-100 affects personal data
    assert "risk-notice-suspension" in t  # severity high
    assert "serious-incident-report" not in t  # not a serious-incident candidate
    breach = docs(pipeline, HR, "breach-notification-draft", subject="INC-100")
    assert breach.fields["notify_by"].value == "2026-11-04 08:00 UTC"
    assert breach.id == f"{HR}.breach-notification-draft.INC-100.en.v1"


def test_subject_inputs_are_per_incident(pipeline):
    set_profile(pipeline, is_financial_entity="yes")
    scan_agent(pipeline, HR)
    a = pipeline.store.get("agents", TENANT, HR)
    a["inputs"] = {"INC-100:breach_nature": "120 employees, HR files"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, HR)
    d = docs(pipeline, HR, "breach-notification-draft", subject="INC-100")
    assert d.fields["breach_nature"].value == "120 employees, HR files" and d.version == 2
    tasks = {t["id"] for t in pipeline.store.query("tasks", TENANT, agentId=HR, status="open")}
    assert f"{HR}.INC-100:breach_measures" not in tasks and f"{HR}.INC-100:measures_taken" in tasks
    assert f"{HR}.INC-100:breach_nature" not in tasks


def test_explanation_requests_create_one_document_each(pipeline):
    scan_agent(pipeline, REC)
    a = pipeline.store.get("agents", TENANT, REC)
    a["inputs"] = {"risk_tier": "high", "decisions_about_persons": "yes", "explanation_requests": "REQ-1, REQ-2"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, REC)
    assert docs(pipeline, REC, "decision-explanation", subject="REQ-1") and docs(pipeline, REC, "decision-explanation", subject="REQ-2")
    assert "affected-person-notice" in types(pipeline, REC) and "automated-decision-safeguards" in types(pipeline, REC)
    a = pipeline.store.get("agents", TENANT, REC)
    a["inputs"]["REQ-1:reviewer_explanation"] = "The score was below the threshold."
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, REC)
    assert docs(pipeline, REC, "decision-explanation", subject="REQ-1").version == 2
    assert docs(pipeline, REC, "decision-explanation", subject="REQ-2").version == 1  # untouched


def test_inputs_gate_worker_and_other_documents(pipeline):
    set_profile(pipeline, is_employer="yes", is_public_body="yes", country="ES")
    scan_agent(pipeline, REC)
    assert "worker-notice" not in types(pipeline, REC)  # needs used_in_workplace
    assert "eu-database-registration" in types(pipeline, REC) and "fria" in types(pipeline, REC) and "ens-evidence-pack" in types(pipeline, REC)
    a = pipeline.store.get("agents", TENANT, REC)
    a["inputs"] = {"used_in_workplace": "yes", "substantial_modification": "yes", "iso42001": "yes", "publishes_public_interest_content": "yes", "dpia_required": "yes"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, REC)
    assert {"worker-notice", "provider-status", "iso42001-impact-assessment", "iso42001-soa-evidence", "public-text-disclosure", "dpia-input-pack"} <= types(pipeline, REC)


def test_provider_status_from_fine_tuned_model(pipeline):
    scan_agent(pipeline, REC)
    assert "provider-status" in types(pipeline, REC)  # fineTunedModel is true in the evidence
    scan_agent(pipeline, HR)
    assert "provider-status" not in types(pipeline, HR)


# ---- every document type renders in both languages ---------------------------------------------
def test_all_document_types_generate_and_render(pipeline):
    pipeline.s.render_formats = "docx,pdf,md"
    set_profile(
        pipeline, country="ES", is_public_body="yes", provides_public_services="yes", credit_or_insurance_pricing="yes",
        is_financial_entity="yes", is_nis2_entity="yes", is_employer="yes", iso42001_in_scope="yes",
    )
    for a in (HR, REC, LOAN, PUB):
        scan_agent(pipeline, a)
    a = pipeline.store.get("agents", TENANT, REC)
    a["inputs"] = {
        "risk_tier": "high", "used_in_workplace": "yes", "decisions_about_persons": "yes", "dpia_required": "yes",
        "substantial_modification": "yes", "publishes_public_interest_content": "yes", "explanation_requests": "REQ-1",
    }
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, REC)
    made = {d["docType"] for d in pipeline.store.query("documents", TENANT)}
    assert made == set(pipeline.doc_types), f"never generated: {set(pipeline.doc_types) - made}"
    for raw in pipeline.store.query("documents", TENANT):
        d = Document.model_validate(raw)
        md = pipeline.files.get(d.file_paths["md"]).decode()
        assert "{{" not in md and "}}" not in md and "None" not in md.replace("None found", "").replace("None observed", "")
        assert ("DRAFT" in md) or ("BORRADOR" in md)
        assert md.startswith("# ")
        assert pipeline.files.get(d.file_paths["pdf"]).startswith(b"%PDF")
        text = "\n".join(p.text for p in Docx(__import__("io").BytesIO(pipeline.files.get(d.file_paths["docx"]))).paragraphs)
        assert len(text) > 20


def test_layout_documents_have_bilingual_content(pipeline):
    scan_agent(pipeline, REC)
    en = pipeline.files.get(docs(pipeline, REC, "risk-classification", "en").file_paths["md"]).decode()
    es = pipeline.files.get(docs(pipeline, REC, "risk-classification", "es").file_paths["md"]).decode()
    assert "Risk classification record" in en and "Registro de clasificación de riesgo" in es
    assert "[PENDING INPUT]" in en and "[PENDIENTE]" in es
    assert "| Possible Annex III areas | 4 Employment" in en


def test_table_syntax_survives_pipes_and_newlines_in_values(pipeline):
    scan_agent(pipeline, PUB)
    a = pipeline.store.get("agents", TENANT, PUB)
    a["inputs"] = {"business_purpose": "line one | with pipe\nline two"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, PUB)
    md = pipeline.files.get(docs(pipeline, PUB, "register-entry").file_paths["md"]).decode()
    assert "| Business purpose | line one / with pipe line two |" in md


# ---- API: profile, inputs, labels --------------------------------------------------------------
def test_api_profile_and_input_validation(settings, pipeline):
    api = Api(settings, pipeline)
    h = {}
    scan_agent(pipeline, REC)
    assert api.get_profile(h, TENANT)["complete"] is False
    st = api.patch_profile(h, TENANT, {"country": "es", "is_employer": "yes"})
    assert st["profile"] == {"country": "es", "is_employer": "yes"} and "is_nis2_entity" in st["missing"]
    for bad in ({"country": "Spain"}, {"is_employer": "maybe"}, {"made_up": "yes"}):
        with pytest.raises(ApiError) as e:
            api.patch_profile(h, TENANT, bad)
        assert e.value.status == 400
    for bad in ({"risk_tier": "scary"}, {"dpia_required": "perhaps"}, {"explanation_requests": "a b!"}, {"bad:field": "x"}, {"INC 1:breach_nature": "x"}, {"unknown_field": "x"}):
        with pytest.raises(ApiError) as e:
            api.patch_inputs(h, TENANT, REC, bad)
        assert e.value.status == 400, bad
    out = api.patch_inputs(h, TENANT, REC, {"risk_tier": "High", "INC-7:measures_taken": "Isolated the agent"})
    assert out["agent"]["inputs"]["risk_tier"] == "High"
    assert out["inputLabels"]["breach_nature"].startswith("Nature of the breach")


def test_profile_route_is_tenant_scoped(settings, pipeline):
    import base64
    import json

    s = settings.model_copy(update={"auth_disabled": False})
    api = Api(s, pipeline)
    p = {"userDetails": "x", "claims": [{"typ": "tid", "val": "22222222-2222-2222-2222-222222222222"}]}
    h = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(p).encode()).decode()}
    for call in (lambda: api.get_profile(h, TENANT), lambda: api.patch_profile(h, TENANT, {})):
        with pytest.raises(ApiError) as e:
            call()
        assert e.value.status == 403
