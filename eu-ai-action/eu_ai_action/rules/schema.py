from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..models import stable_hash

Op = Literal["eq", "ne", "in", "contains", "missing", "exists", "gt", "lt"]


class Cond(BaseModel):
    fact: str | None = None
    op: Op | None = None
    value: Any = None
    all: list[Cond] | None = None
    any: list[Cond] | None = None
    not_: Cond | None = Field(default=None, alias="not")


Cond.model_rebuild()


class Check(BaseModel):
    pass_when: list[Cond] = Field(default_factory=list)
    unknown_when: list[Cond] = Field(default_factory=list)


class Rule(BaseModel):
    id: str
    regulation: str
    article: str
    applies_from: date
    role: str
    version: int
    applies_when: Cond | None = None
    check: Check | None = None
    evidence: list[str] = Field(default_factory=list)
    document: str
    source_citation: str
    reviewed_by: str = ""

    @property
    def reviewed(self) -> bool:
        return bool(self.reviewed_by.strip())


class RuleSet(BaseModel):
    rules: list[Rule]
    version: str

    def get(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)


def load_rules(rules_dir: str) -> RuleSet:
    rules: list[Rule] = []
    raw_all: list[Any] = []
    for p in sorted(Path(rules_dir).glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        raw_all.append(raw)
        rules.extend(Rule.model_validate(r) for r in raw)
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule ids")
    for r in rules:
        if not r.source_citation:
            raise ValueError(f"rule {r.id} has no source_citation")
    return RuleSet(rules=rules, version=stable_hash(raw_all))
