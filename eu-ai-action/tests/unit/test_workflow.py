import pytest

from eu_ai_action.models import Document
from eu_ai_action.review import TransitionError, transition
from tests.conftest import TENANT, scan_agent

PUBLIC = "agent-public-nodisclosure"
FULL = {
    "business_purpose": "Answer product questions", "intended_users": "Visitors", "controller_name": "Contoso SA",
    "controller_contact": "dpo@contoso.example", "processing_purposes": "Customer support", "data_subjects": "Customers",
    "recipients": "None", "third_country_transfers": "None", "retention_period": "30 days",
}


def fill(pipeline):
    scan_agent(pipeline, PUBLIC)
    a = pipeline.store.get("agents", TENANT, PUBLIC)
    a["inputs"] = FULL
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, PUBLIC)
    return pipeline.latest_document(TENANT, PUBLIC, "register-entry", "en")


def test_cannot_submit_with_missing_human_fields(pipeline):
    scan_agent(pipeline, PUBLIC)
    d = pipeline.latest_document(TENANT, PUBLIC, "register-entry", "en")
    with pytest.raises(TransitionError, match="human input missing"):
        transition(d, "submit", "u", False)


def test_full_cycle_and_audit(pipeline):
    d = fill(pipeline)
    d, _ = transition(d, "submit", "u", False)
    d2, audit = transition(d, "approve", "ana@contoso.example", False)
    assert d2.state == "approved" and d2.approved_by == "ana@contoso.example" and d2.approved_at
    assert audit["fromState"] == "in_review" and audit["toState"] == "approved"


def test_invalid_transitions(pipeline):
    d = fill(pipeline)
    with pytest.raises(TransitionError):
        transition(d, "approve", "u", False)  # draft cannot be approved
    with pytest.raises(TransitionError):
        transition(d, "nonsense", "u", False)
    d, _ = transition(d, "submit", "u", False)
    d, _ = transition(d, "reject", "u", False)
    d, _ = transition(d, "reopen", "u", False)
    assert d.state == "draft"


def test_counsel_gate_blocks_release_when_required(pipeline):
    d = fill(pipeline)
    d, _ = transition(d, "submit", "u", True)
    with pytest.raises(TransitionError, match="counsel"):
        transition(d, "approve", "u", True)
    transition(d, "approve", "u", False)  # allowed when the gate is off


def test_approver_identity_required(pipeline):
    d = fill(pipeline)
    d, _ = transition(d, "submit", "u", False)
    with pytest.raises(TransitionError, match="identity"):
        transition(d, "approve", "", False)


def test_approved_doc_marked_stale_when_inputs_change(pipeline):
    d = fill(pipeline)
    d, _ = transition(d, "submit", "u", False)
    d, _ = transition(d, "approve", "u", False)
    pipeline.store.upsert("documents", TENANT, d.to_doc())
    a = pipeline.store.get("agents", TENANT, PUBLIC)
    a["inputs"] = {**FULL, "business_purpose": "Changed purpose"}
    pipeline.store.upsert("agents", TENANT, a)
    pipeline.regenerate(TENANT, PUBLIC)
    old = Document.model_validate(pipeline.store.get("documents", TENANT, d.id))
    assert old.state == "approved" and old.stale
    new = pipeline.latest_document(TENANT, PUBLIC, "register-entry", "en")
    assert new.version == d.version + 1 and new.state == "draft"
