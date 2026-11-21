"""Streamable-HTTP MCP server (JSON responses) for foundry-finops. Stdlib HTTP + PyJWT; no web framework.

Endpoints
    POST /mcp                                    JSON-RPC 2.0 (initialize, tools/list, tools/call, ping)
    GET  /.well-known/oauth-protected-resource   RFC 9728 metadata pointing at Microsoft Entra ID
    GET  /healthz                                liveness

Environment
    AZURE_TENANT_ID, MCP_CLIENT_ID               Entra app that protects this server
    MCP_CLIENT_SECRET or OBO_USE_MANAGED_IDENTITY=1 (+ OBO_MANAGED_IDENTITY_CLIENT_ID) for on-behalf-of
    MCP_PUBLIC_BASE_URL                          e.g. https://finops-mcp.contoso.com
    FINOPS_SUBSCRIPTION_ID, FINOPS_RESOURCE_GROUP, FINOPS_FOUNDRY_ACCOUNT, FINOPS_APIM_SERVICE,
    FINOPS_LOGS_TARGET, FINOPS_LOCATION, FINOPS_READ_ONLY
    MCP_AUTH=none + FINOPS_DEMO=1                local testing only (refuses to start on a non-loopback host)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import foundry_finops  # noqa: F401
except ImportError:  # running from the monorepo checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "foundry-finops" / "src"))

from foundry_finops.errors import HttpResponseError  # noqa: E402
from foundry_finops.service import DemoService, LiveService, ReadOnlyError, Settings  # noqa: E402

from . import __version__  # noqa: E402
from .auth import AuthConfig, AuthError, CredentialCache, TokenValidator, User  # noqa: E402
from .tools import TOOLS, call  # noqa: E402

log = logging.getLogger("finops.mcp")
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
INSTRUCTIONS = (
    "Foundry FinOps tools report Microsoft Foundry spend, token usage, cost controls, budgets, quota, tags, alerts and "
    "AI-gateway token limits for the signed-in user's Azure subscriptions. Billed cost lags usage by 8-24 hours; token "
    "metrics are near real time; cost-control usage is an estimate. Confirm with the user before any write tool."
)


class McpApp:
    def __init__(self, *, auth: AuthConfig | None, settings: Settings, validator: Any = None,
                 credentials: CredentialCache | None = None, demo: DemoService | None = None) -> None:
        self.auth = auth
        self.settings = settings
        self.validator = validator or (TokenValidator(auth) if auth else None)
        self.credentials = credentials or (CredentialCache(auth) if auth else None)
        self.demo = demo or (DemoService(settings) if settings.demo else None)

    # ----------------------------------------------------------------- service
    def service_for(self, user: User | None, args: dict[str, Any]) -> Any:
        overrides = {k: args.pop(k) for k in ("subscription_id", "resource_group", "foundry_account") if k in args}
        if self.demo is not None:
            return self.demo
        from foundry_finops import FinOpsClient

        s = replace(self.settings, **{k: v for k, v in overrides.items() if v})
        if not s.subscription_id:
            raise ValueError("No subscription. Pass subscription_id or set FINOPS_SUBSCRIPTION_ID on the server.")
        cred = self.credentials.for_user(user) if (user and self.credentials) else None
        client = FinOpsClient(s.subscription_id, cred, resource_group=s.resource_group or None)
        return LiveService(s, client)

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, msg: dict[str, Any], user: User | None) -> dict[str, Any] | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                result: Any = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "foundry-finops", "title": "Foundry FinOps", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = [t.schema() for t in TOOLS if not (self.settings.read_only and not t.read_only)]
                result = {"tools": tools}
            elif method == "tools/call":
                name = params.get("name", "")
                args = dict(params.get("arguments") or {})
                started = time.monotonic()
                try:
                    result = call(self.service_for(user, args), name, args)
                except (ValueError, KeyError, ReadOnlyError, HttpResponseError) as exc:
                    text = exc.message if isinstance(exc, HttpResponseError) else str(exc).strip("'")
                    if isinstance(exc, HttpResponseError) and exc.status_code == 403:
                        text = f"Azure denied access ({text}). The signed-in user needs a role such as Cost Management Reader or Foundry User on this scope."
                    result = {"content": [{"type": "text", "text": text}], "isError": True}
                log.info("tool=%s user=%s ok=%s ms=%d", name, user.oid if user else "-", not result.get("isError"),
                         (time.monotonic() - started) * 1000)
            else:
                return _error(mid, -32601, f"Method not found: {method}")
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("Unhandled error")
            return _error(mid, -32603, f"Internal error: {type(exc).__name__}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def make_handler(app: McpApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"FoundryFinOpsMCP/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, status: int, payload: Any = None, headers: dict[str, str] | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, default=str).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _base_url(self) -> str:
            if app.auth and app.auth.public_base_url:
                return app.auth.public_base_url.rstrip("/")
            return f"https://{self.headers.get('Host', 'localhost')}"

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if self.path.startswith("/.well-known/oauth-protected-resource"):
                if not app.auth:
                    return self._send(404, {"error": "auth disabled"})
                return self._send(200, {
                    "resource": f"{self._base_url()}/mcp",
                    "authorization_servers": [f"https://login.microsoftonline.com/{app.auth.tenant_id}/v2.0"],
                    "scopes_supported": [f"api://{app.auth.client_id}/{app.auth.required_scope}"],
                    "bearer_methods_supported": ["header"],
                })
            if self.path.startswith("/mcp"):
                return self._send(405, {"error": "This server doesn't offer an SSE stream; POST JSON-RPC to /mcp"},
                                  {"Allow": "POST"})
            return self._send(404, {"error": "Not found"})

        def do_DELETE(self) -> None:
            self._send(405, {"error": "Stateless server – no sessions to delete"}, {"Allow": "POST"})

        def do_POST(self) -> None:
            if not self.path.startswith("/mcp"):
                return self._send(404, {"error": "Not found"})
            user = None
            if app.validator:
                try:
                    user = app.validator.validate(self.headers.get("Authorization"))
                except AuthError as exc:
                    meta = f'{self._base_url()}/.well-known/oauth-protected-resource'
                    return self._send(exc.status, {"error": str(exc)}, {
                        "WWW-Authenticate": f'Bearer resource_metadata="{meta}", error="invalid_token"'})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(min(length, 2_000_000)) or b"null")
            except json.JSONDecodeError:
                return self._send(400, _error(None, -32700, "Parse error"))
            headers = {"Mcp-Session-Id": self.headers.get("Mcp-Session-Id") or uuid.uuid4().hex}
            if isinstance(msg, list):
                replies = [r for r in (app.handle(m, user) for m in msg) if r is not None]
                return self._send(200, replies, headers) if replies else self._send(202, None, headers)
            if not isinstance(msg, dict):
                return self._send(400, _error(None, -32600, "Invalid request"))
            reply = app.handle(msg, user)
            return self._send(202, None, headers) if reply is None else self._send(200, reply, headers)

    return Handler


def build_app() -> McpApp:
    settings = Settings.from_env()
    if os.environ.get("MCP_AUTH", "entra").lower() == "none":
        return McpApp(auth=None, settings=settings)
    auth = AuthConfig.from_env()
    if not auth.tenant_id or not auth.client_id:
        raise SystemExit("Set AZURE_TENANT_ID and MCP_CLIENT_ID (or MCP_AUTH=none for local demo testing).")
    settings = replace(settings, demo=os.environ.get("FINOPS_DEMO", "") == "1")
    return McpApp(auth=auth, settings=settings)


def main() -> None:
    ap = argparse.ArgumentParser(description="Foundry FinOps MCP server")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = build_app()
    if app.auth is None and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to serve without authentication on a non-loopback address.")
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    log.info("Foundry FinOps MCP on http://%s:%s/mcp (auth=%s, demo=%s)", args.host, args.port,
             "entra" if app.auth else "none", app.demo is not None)
    srv.serve_forever()


if __name__ == "__main__":
    main()
