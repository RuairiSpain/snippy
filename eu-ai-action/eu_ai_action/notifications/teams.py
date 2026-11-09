"""Teams notifications for open tasks, through an incoming webhook or a Workflows webhook URL set in TEAMS_WEBHOOK_URL.
The card uses the Adaptive Card message format. Not tested against Teams."""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx

MAX_ITEMS = 20


def check_webhook_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("TEAMS_WEBHOOK_URL must be an https URL")
    host = u.hostname.lower()
    try:
        ipaddress.ip_address(host)
        raise ValueError("TEAMS_WEBHOOK_URL must use a host name, not an IP address")
    except ValueError as e:
        if "IP address" in str(e):
            raise
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("TEAMS_WEBHOOK_URL must not point to a local host")


def build_card(tasks: list[dict[str, Any]], portal_url: str = "") -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "text": f"EU-AI Action: {len(tasks)} open task(s)"},
    ]
    for t in tasks[:MAX_ITEMS]:
        body.append({"type": "TextBlock", "wrap": True, "text": f"- {t.get('title', t.get('id'))}"})
    if len(tasks) > MAX_ITEMS:
        body.append({"type": "TextBlock", "isSubtle": True, "text": f"and {len(tasks) - MAX_ITEMS} more"})
    card: dict[str, Any] = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4", "body": body}
    if portal_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open portal", "url": portal_url}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def notify_open_tasks(pipeline, tenant_id: str, webhook_url: str, portal_url: str = "", client: httpx.Client | None = None) -> dict[str, Any]:
    if not webhook_url:
        return {"sent": False, "reason": "TEAMS_WEBHOOK_URL is not set"}
    check_webhook_url(webhook_url)
    tasks = pipeline.store.query("tasks", tenant_id, status="open")
    if not tasks:
        return {"sent": False, "reason": "no open tasks"}
    r = (client or httpx.Client(timeout=15)).post(webhook_url, json=build_card(tasks, portal_url))
    r.raise_for_status()
    return {"sent": True, "tasks": len(tasks)}
