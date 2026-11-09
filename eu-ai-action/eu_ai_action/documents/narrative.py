"""Narrative fields. The model may only describe what the facts show; it never decides applicability or compliance."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from ..models import AgentFacts, stable_hash

log = logging.getLogger(__name__)

# A draft that asserts compliance or legal status is rejected and the field becomes a human task.
FORBIDDEN = re.compile(
    r"\b(complies|compliant|in compliance|conforms|legally|lawful|approved by|cumple|conforme|legalmente|aprobado por)\b",
    re.IGNORECASE,
)


@dataclass
class NarrativeResult:
    values: dict[str, str | None]
    source: str  # "llm" or "derived"
    model: str = ""
    prompt_hash: str = ""
    notes: list[str] = field(default_factory=list)


class NarrativeGenerator(Protocol):
    def generate(self, facts: AgentFacts, names: list[str], language: str) -> NarrativeResult: ...


def sanitize(values: dict[str, object], names: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for n in names:
        v = values.get(n)
        if not isinstance(v, str) or not v.strip() or FORBIDDEN.search(v):
            out[n] = None
        else:
            out[n] = v.strip()
    return out


class DerivedNarrative:
    """Deterministic description built from facts. Used when no model is configured."""

    def generate(self, facts: AgentFacts, names: list[str], language: str) -> NarrativeResult:
        f = facts.value
        channels = ", ".join(f("channels", [])) or ("desconocidos" if language == "es" else "unknown")
        ks, cn = len(f("knowledgeSources", [])), len(f("connectors", []))
        if language == "es":
            text = (
                f"{facts.name} es un agente de Copilot Studio disponible en los canales: {channels}. "
                f"Utiliza {ks} fuente(s) de conocimiento y {cn} conector(es)."
            )
        else:
            text = (
                f"{facts.name} is a Copilot Studio agent available on these channels: {channels}. "
                f"It uses {ks} knowledge source(s) and {cn} connector(s)."
            )
        return NarrativeResult(values=sanitize({n: text for n in names}, names), source="derived")


SYSTEM = (
    "You write short factual descriptions of AI agents for compliance records. "
    "Use only the facts provided. Do not state or imply legal compliance, legal status or approval. "
    "If the facts are not enough for a field, return null for it. Write in {language}. Maximum 60 words per field."
)


class AzureOpenAINarrative:
    """Calls Azure OpenAI with a JSON schema. Not exercised by unit tests except through a fake client."""

    def __init__(self, endpoint: str, deployment: str, api_version: str, temperature: float = 0.0, client=None):
        if client is None:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI

            tp = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
            client = AzureOpenAI(azure_endpoint=endpoint, api_version=api_version, azure_ad_token_provider=tp)
        self.client, self.deployment, self.temperature = client, deployment, temperature

    def generate(self, facts: AgentFacts, names: list[str], language: str) -> NarrativeResult:
        lang = "Spanish" if language == "es" else "English"
        payload = {
            k: facts.value(k)
            for k in ("name", "platform", "channels", "knowledgeSources", "connectors", "instructions", "language")
        }
        schema = {
            "name": "narrative",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": names,
                "properties": {n: {"type": ["string", "null"]} for n in names},
            },
        }
        messages = [
            {"role": "system", "content": SYSTEM.format(language=lang)},
            {"role": "user", "content": json.dumps({"fields": names, "facts": payload}, ensure_ascii=False)},
        ]
        prompt_hash = stable_hash(messages)
        try:
            resp = self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                temperature=self.temperature,
                response_format={"type": "json_schema", "json_schema": schema},
            )
            raw = json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            log.warning("narrative generation failed: %s", exc)
            return NarrativeResult(values={n: None for n in names}, source="llm", model=self.deployment, prompt_hash=prompt_hash, notes=[str(exc)])
        return NarrativeResult(values=sanitize(raw, names), source="llm", model=self.deployment, prompt_hash=prompt_hash)


def make_narrative(settings) -> NarrativeGenerator:
    if settings.narrative_enabled and settings.azure_openai_endpoint and settings.azure_openai_deployment:
        return AzureOpenAINarrative(
            settings.azure_openai_endpoint, settings.azure_openai_deployment, settings.azure_openai_api_version, settings.llm_temperature
        )
    return DerivedNarrative()
