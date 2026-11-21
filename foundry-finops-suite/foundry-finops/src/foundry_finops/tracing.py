"""Client-side usage tracking with OpenTelemetry: team/cost-centre attribution per model call.

Install extras: ``pip install "foundry-finops[tracing]"``.

Two layers:

1. :func:`configure_tracing` – turn on Foundry GenAI tracing (``AIProjectInstrumentor``) and export
   to Application Insights.
2. :class:`UsageTracker` – read the ``usage`` block from each model response, attach FinOps
   attributes (team, cost centre, app, user), estimate cost with a :class:`PriceSheet`, and emit
   OpenTelemetry metrics + a structured ``llm.usage`` log record you can query with KQL
   (see :mod:`foundry_finops.queries`).

Native Foundry metrics stop at deployment/model granularity; this is how you get per-team or
per-user numbers without a gateway.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import DependencyMissingError
from .operations.pricing import PriceSheet

logger = logging.getLogger("foundry_finops.usage")

_ATTRS: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("finops_attrs", default={})

#: Attribute prefix used on spans, metrics and log records.
PREFIX = "finops."


def configure_tracing(
    connection_string: str | None = None,
    *,
    project_client: Any = None,
    enable_content_recording: bool = False,
    service_name: str = "foundry-app",
) -> None:
    """Enable Foundry GenAI tracing and export to Application Insights.

    Pass a connection string, or an ``azure.ai.projects.AIProjectClient`` to read the connection
    string of the project's connected Application Insights resource. Content recording stays
    off by default (prompts can contain personal or confidential data).
    """
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError as exc:
        raise DependencyMissingError("pip install 'foundry-finops[tracing]'") from exc

    if connection_string is None and project_client is not None:
        connection_string = project_client.telemetry.get_application_insights_connection_string()
    connection_string = connection_string or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        raise ValueError("No Application Insights connection string (pass one or set APPLICATIONINSIGHTS_CONNECTION_STRING)")

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    configure_azure_monitor(connection_string=connection_string, logger_name="foundry_finops")

    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    try:
        from azure.ai.projects.telemetry import AIProjectInstrumentor

        AIProjectInstrumentor().instrument(enable_content_recording=enable_content_recording)
    except ImportError:
        logger.info("azure-ai-projects not installed; skipping AIProjectInstrumentor")


@contextlib.contextmanager
def finops_context(**attributes: str) -> Iterator[dict[str, str]]:
    """Attach FinOps attributes (``team="risk"``, ``cost_center="CC123"``) to usage in this block.

    Nested contexts merge. Values also go on the current OpenTelemetry span and baggage when
    OpenTelemetry is installed, so server-side traces can be correlated.
    """
    merged = {**_ATTRS.get(), **{k: str(v) for k, v in attributes.items()}}
    token = _ATTRS.set(merged)
    otel_token = None
    try:
        from opentelemetry import baggage, context, trace

        span = trace.get_current_span()
        ctx = context.get_current()
        for k, v in merged.items():
            span.set_attribute(PREFIX + k, v)
            ctx = baggage.set_baggage(PREFIX + k, v, ctx)
        otel_token = context.attach(ctx)
    except ImportError:
        pass
    try:
        yield merged
    finally:
        if otel_token is not None:
            from opentelemetry import context

            context.detach(otel_token)
        _ATTRS.reset(token)


@dataclass
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float | None = None
    currency: str = "USD"
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def extract_usage(response: Any) -> tuple[str, int, int, int, int]:
    """Return ``(model, input, output, cached, reasoning)`` from an OpenAI/Foundry response or dict.

    Handles Chat Completions (``prompt_tokens``/``completion_tokens``) and Responses
    (``input_tokens``/``output_tokens``) shapes.
    """
    def get(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, Mapping):
            return obj.get(key, default)
        return getattr(obj, key, default)

    usage = get(response, "usage") or {}
    model = get(response, "model") or ""
    inp = get(usage, "input_tokens") if get(usage, "input_tokens") is not None else get(usage, "prompt_tokens", 0)
    out = get(usage, "output_tokens") if get(usage, "output_tokens") is not None else get(usage, "completion_tokens", 0)
    in_details = get(usage, "input_tokens_details") or get(usage, "prompt_tokens_details")
    out_details = get(usage, "output_tokens_details") or get(usage, "completion_tokens_details")
    cached = get(in_details, "cached_tokens", 0) or 0
    reasoning = get(out_details, "reasoning_tokens", 0) or 0
    return str(model), int(inp or 0), int(out or 0), int(cached), int(reasoning)


class UsageTracker:
    """Record token usage and estimated cost with FinOps attributes.

    >>> tracker = UsageTracker(price_sheet=PriceSheet.from_mapping({"gpt-5.4-mini": {"input": 0.25, "output": 2.0}}))
    >>> with finops_context(team="fraud", cost_center="CC-4410"):
    ...     resp = openai_client.responses.create(model="gpt-5.4-mini", input="hi")
    ...     tracker.record(resp, agent="case-review")
    """

    def __init__(self, price_sheet: PriceSheet | None = None, *, meter_name: str = "foundry_finops") -> None:
        self.price_sheet = price_sheet or PriceSheet()
        self._instruments: dict[str, Any] | None = None
        self._meter_name = meter_name

    def _meters(self) -> dict[str, Any]:
        if self._instruments is None:
            try:
                from opentelemetry import metrics

                meter = metrics.get_meter(self._meter_name)
                self._instruments = {
                    "tokens": meter.create_counter("finops.tokens", unit="{token}", description="Tokens by type"),
                    "cost": meter.create_counter("finops.cost", unit="USD", description="Estimated model cost"),
                    "requests": meter.create_counter("finops.requests", unit="{request}"),
                }
            except ImportError:
                self._instruments = {}
        return self._instruments

    def record(self, response: Any, *, model: str | None = None, **attributes: str) -> UsageRecord:
        m, inp, out, cached, reasoning = extract_usage(response)
        model = model or m
        attrs = {**_ATTRS.get(), **{k: str(v) for k, v in attributes.items()}}
        rec = UsageRecord(
            model=model, input_tokens=inp, output_tokens=out, cached_tokens=cached, reasoning_tokens=reasoning,
            cost=self.price_sheet.estimate(model, inp, out, cached), currency=self.price_sheet.currency,
            attributes=attrs,
        )
        self._emit(rec)
        return rec

    def _emit(self, rec: UsageRecord) -> None:
        dims = {"gen_ai.request.model": rec.model, **{PREFIX + k: v for k, v in rec.attributes.items()}}
        meters = self._meters()
        if meters:
            meters["tokens"].add(rec.input_tokens, {**dims, "gen_ai.token.type": "input"})
            meters["tokens"].add(rec.output_tokens, {**dims, "gen_ai.token.type": "output"})
            meters["requests"].add(1, dims)
            if rec.cost is not None:
                meters["cost"].add(rec.cost, dims)
            with contextlib.suppress(ImportError):
                from opentelemetry import trace

                span = trace.get_current_span()
                span.set_attribute("gen_ai.usage.input_tokens", rec.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", rec.output_tokens)
                if rec.cost is not None:
                    span.set_attribute(PREFIX + "cost", rec.cost)
        payload = asdict(rec)
        payload.pop("attributes")
        payload.update({PREFIX + k: v for k, v in rec.attributes.items()})
        payload["total_tokens"] = rec.total_tokens
        # Lands in App Insights `traces` with customDimensions – see queries.TOKENS_BY_TEAM
        logger.info("llm.usage", extra={"custom_dimensions": payload, **payload})
