"""OpenTelemetry setup. Spans and metrics are no-ops unless an exporter is configured."""

from __future__ import annotations

import logging
import os

from opentelemetry import metrics, trace

tracer = trace.get_tracer("sandbox_mux")
meter = metrics.get_meter("sandbox_mux")

turns = meter.create_counter("sandbox_mux.turns", description="Agent turns by outcome")
turn_duration = meter.create_histogram("sandbox_mux.turn.duration", unit="s")
queue_wait = meter.create_histogram("sandbox_mux.queue.wait", unit="s",
                                    description="Time spent waiting for a sandbox slot")
input_tokens = meter.create_counter("gen_ai.client.token.usage.input")
output_tokens = meter.create_counter("gen_ai.client.token.usage.output")


def setup() -> None:
    """Export to Application Insights when APPLICATIONINSIGHTS_CONNECTION_STRING is set.

    Any other exporter can be selected with the standard OTEL_* environment variables.
    """
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
        except ImportError:
            logging.getLogger(__name__).warning(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but "
                "azure-monitor-opentelemetry is not installed (pip install sandbox-mux[otel])")
            return
        configure_azure_monitor()
