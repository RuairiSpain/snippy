from datetime import date

import pytest

from eu_ai_action.rules import evaluate_rules, load_rules
from eu_ai_action.settings import Settings
from tests.unit.test_facts import facts_for

RS = load_rules(Settings(_env_file=None).rules_dir)
TODAY = date(2026, 11, 8)


def by_rule(agent_id):
    return {d.rule_id: d for d in evaluate_rules(RS, facts_for(agent_id), TODAY)}


@pytest.mark.parametrize(
    "agent_id,applicability,result",
    [
        ("agent-public-disclosed", "applies", "pass"),
        ("agent-public-nodisclosure", "applies", "fail"),
        ("agent-teams-internal", "not_applicable", "none"),
        ("agent-hr-faq", "not_applicable", "none"),
        ("agent-finance-labels", "not_applicable", "none"),
        ("agent-order-status", "applies", "unknown"),
        ("agent-no-owner", "not_applicable", "none"),
        ("agent-citizen-es", "applies", "pass"),
    ],
)
def test_art_50_1(agent_id, applicability, result):
    d = by_rule(agent_id)["AIA-50-1-disclosure"]
    assert (d.applicability, d.result) == (applicability, result)


def test_register_and_ropa_apply_to_every_agent():
    for a in ("agent-no-owner", "agent-teams-internal", "agent-public-disclosed"):
        r = by_rule(a)
        assert r["AIA-REGISTER-ENTRY"].applicability == "applies"
        assert r["GDPR-30-ROPA"].applicability == "applies"


def test_public_agent_with_no_greeting_is_unknown_not_skipped():
    f = facts_for("agent-public-disclosed")
    f.facts["greetingText"].value = None
    f.facts["aiDisclosureStrength"].value = None
    d = {x.rule_id: x for x in evaluate_rules(RS, f, TODAY)}["AIA-50-1-disclosure"]
    assert d.result == "unknown"


def test_every_rule_has_citation_and_reviewer_field():
    for r in RS.rules:
        assert r.source_citation
        assert hasattr(r, "reviewed_by")
        assert not r.reviewed, "no rule may be marked reviewed without counsel sign-off"


def test_rule_set_version_changes_with_content(tmp_path):
    src = Settings(_env_file=None).rules_dir
    import shutil

    shutil.copytree(src, tmp_path / "r")
    v1 = load_rules(str(tmp_path / "r")).version
    p = tmp_path / "r" / "gdpr.yaml"
    p.write_text(p.read_text().replace("version: 1", "version: 2"))
    assert load_rules(str(tmp_path / "r")).version != v1
