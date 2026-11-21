"""Capture the connector tool description from a running MCP server (initialize → initialized → tools/list).

    python scripts/capture_tools.py --url http://127.0.0.1:8000/mcp --out plugin/appPackage/tools/connector-tools.json
    python scripts/capture_tools.py --url https://finops-mcp.contoso.com/mcp --token "$TOKEN" --out ...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--token")
    a = ap.parse_args()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if a.token:
        h["Authorization"] = f"Bearer {a.token}"
    init = requests.post(a.url, headers=h, timeout=30, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "capture-tools", "version": "1"}}})
    init.raise_for_status()
    sid = init.headers.get("Mcp-Session-Id")
    if sid:
        h["Mcp-Session-Id"] = sid
    requests.post(a.url, headers=h, timeout=30,
                  json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
    listed = requests.post(a.url, headers=h, timeout=30, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed.raise_for_status()
    payload = listed.json()
    if "error" in payload:
        raise SystemExit(f"tools/list failed: {payload['error']}")
    tools = payload["result"]["tools"]
    keep = ("name", "title", "description", "inputSchema", "annotations")
    doc = {"tools": [{k: t[k] for k in keep if k in t} for t in tools]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(tools)} tools from {init.json()['result']['serverInfo']['name']} → {a.out}")


if __name__ == "__main__":
    main()
