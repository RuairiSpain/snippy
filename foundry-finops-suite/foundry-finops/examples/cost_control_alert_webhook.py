"""Azure Functions-style handler for Foundry cost-control alerts delivered by Event Grid (CloudEvents v1.0).

Foundry sends one event per request while usage is over a threshold, so de-duplicate before notifying.
"""

from foundry_finops import CostControlAlertEvent

_seen: set = set()   # use a durable store (Table Storage, Redis) in production


def handle(cloud_event: dict) -> str | None:
    ev = CostControlAlertEvent.from_cloud_event(cloud_event)
    if ev.dedupe_key in _seen:
        return None
    _seen.add(ev.dedupe_key)
    who = ev.agent_name or ev.project_name or "resource"
    return (f"[{ev.severity}] {who} reached {ev.threshold} of its {ev.unit} cost control: "
            f"{ev.total_consumed:,.2f} of {ev.limit:,.2f} used, {ev.remaining:,.2f} left until {ev.window_end}.")
