"""Evaluates rules over agent facts. Structured conditions only: no eval, no model calls."""
from __future__ import annotations

from datetime import date

from ..models import AgentFacts, DutyResult
from .schema import Cond, Rule, RuleSet


def _fact(facts: AgentFacts, key: str):
    fv = facts.facts.get(key)
    return None if fv is None else fv.value


def eval_cond(c: Cond, facts: AgentFacts) -> bool:
    if c.all is not None:
        return all(eval_cond(x, facts) for x in c.all)
    if c.any is not None:
        return any(eval_cond(x, facts) for x in c.any)
    if c.not_ is not None:
        return not eval_cond(c.not_, facts)
    assert c.fact and c.op, "leaf condition needs fact and op"
    v = _fact(facts, c.fact)
    empty = v is None or v == "" or v == []
    match c.op:
        case "missing":
            return empty
        case "exists":
            return not empty
        case "eq":
            return v == c.value
        case "ne":
            return v != c.value
        case "in":
            return v in (c.value or [])
        case "contains":
            return (not empty) and c.value in v
        case "gt":
            return (not empty) and v > c.value
        case "lt":
            return (not empty) and v < c.value
    raise ValueError(f"unknown op {c.op}")


def evaluate_rule(rule: Rule, facts: AgentFacts, rule_set_version: str, today: date | None = None) -> DutyResult:
    today = today or date.today()
    base = dict(
        id=f"{facts.agent_id}.{rule.id}",
        tenant_id=facts.tenant_id,
        agent_id=facts.agent_id,
        rule_id=rule.id,
        rule_version=rule.version,
        rule_set_version=rule_set_version,
        regulation=rule.regulation,
        article=rule.article,
        counsel_reviewed=rule.reviewed,
        evidence_refs=[
            facts.facts[k].evidence_id for k in rule.evidence if k in facts.facts and facts.facts[k].evidence_id
        ],
    )
    # Applicability: a missing fact used in applies_when makes the duty "applies" with unknown result (never silently skipped).
    if rule.applies_when is not None and not eval_cond(rule.applies_when, facts):
        return DutyResult(**base, applicability="not_applicable", result="none", reasons=["applies_when not met"])
    reasons: list[str] = []
    if today < rule.applies_from:
        reasons.append(f"duty applies from {rule.applies_from.isoformat()}")
    if rule.check is None:
        return DutyResult(**base, applicability="applies", result="none", reasons=reasons + ["document required"])
    unknown = [c for c in rule.check.unknown_when if eval_cond(c, facts)]
    if unknown:
        return DutyResult(**base, applicability="applies", result="unknown", reasons=reasons + ["evidence missing or inconclusive"])
    if all(eval_cond(c, facts) for c in rule.check.pass_when):
        return DutyResult(**base, applicability="applies", result="pass", reasons=reasons)
    return DutyResult(**base, applicability="applies", result="fail", reasons=reasons + ["pass conditions not met"])


def evaluate_rules(rule_set: RuleSet, facts: AgentFacts, today: date | None = None) -> list[DutyResult]:
    return [evaluate_rule(r, facts, rule_set.version, today) for r in rule_set.rules]
