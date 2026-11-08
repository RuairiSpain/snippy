import json
from pathlib import Path

import pytest

from eu_ai_action.collector.base import make_evidence
from eu_ai_action.facts import extract_facts
from eu_ai_action.facts.disclosure import detect_disclosure
from eu_ai_action.models import AgentRef

FIX = Path(__file__).parent.parent / "fixtures" / "agents"


def facts_for(agent_id):
    d = json.loads((FIX / f"{agent_id}.json").read_text(encoding="utf-8"))
    ev = [make_evidence("t", agent_id, "s1", k, f"fixture:{k}", d[k]) for k in ("dataverse", "registry", "purview") if k in d]
    return extract_facts("t", AgentRef(agent_id=agent_id, name=d["name"]), ev, "s1")


@pytest.mark.parametrize(
    "text,strength",
    [
        ("Hello, I'm an AI assistant", "strong"),
        ("Hi! You are talking to an AI-powered agent", "strong"),
        ("Hola, soy un asistente de inteligencia artificial", "strong"),
        ("Soy un asistente de IA", "strong"),
        ("Soy un asistente virtual de pedidos", "weak"),
        ("Your virtual assistant is here", "weak"),
        ("Hello! How can I help you today?", "none"),
        ("Welcome. Ask me anything about AIRPORT transfers", "none"),
        ("", "none"),
    ],
)
def test_disclosure_detector(text, strength):
    assert detect_disclosure(text)[0] == strength


@pytest.mark.parametrize(
    "agent_id,public,strength,owner",
    [
        ("agent-public-disclosed", True, "strong", "Ana Ruiz"),
        ("agent-public-nodisclosure", True, "none", "Ana Ruiz"),
        ("agent-teams-internal", False, "none", "Ana Ruiz"),
        ("agent-hr-faq", False, "strong", "Ana Ruiz"),
        ("agent-order-status", True, "weak", "Ana Ruiz"),
        ("agent-no-owner", False, None, None),
        ("agent-citizen-es", True, "strong", "Marta Gil"),
    ],
)
def test_facts(agent_id, public, strength, owner):
    f = facts_for(agent_id)
    assert f.value("isPublicFacing") is public
    assert f.value("aiDisclosureStrength") == strength
    assert f.value("owner") == owner


def test_provenance_and_categories():
    f = facts_for("agent-hr-faq")
    assert f.facts["greetingText"].evidence_id == "agent-hr-faq.dataverse.s1"
    assert f.value("sensitivityLabels") == ["Confidential"]
    assert "employment data" in f.value("dataCategories")
    assert f.value("knowledgeSources") == [{"type": "SharePointSearchSource", "name": "HR-Policies"}]


def test_finance_agent_connectors_and_data():
    f = facts_for("agent-finance-labels")
    assert f.value("connectors") == ["ERP Reports"]
    assert "financial data" in f.value("dataCategories")


def test_collector_rejects_transcript_keys():
    with pytest.raises(ValueError, match="forbidden"):
        make_evidence("t", "a", "s", "dataverse", "x", {"bot": {}, "transcript": ["hi"]})
