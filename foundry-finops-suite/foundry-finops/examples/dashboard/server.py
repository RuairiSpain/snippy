"""Foundry FinOps dashboard – a zero-dependency example web app on top of ``foundry-finops``.

Run (demo data, no Azure needed)::

    pip install foundry-finops
    python server.py                # http://127.0.0.1:8765

Run against Azure (signs in with DefaultAzureCredential – e.g. ``az login``)::

    export FINOPS_SUBSCRIPTION_ID=<sub> FINOPS_RESOURCE_GROUP=rg-ai FINOPS_FOUNDRY_ACCOUNT=contoso-foundry
    export FINOPS_APIM_SERVICE=contoso-ai-gateway          # optional
    export FINOPS_LOGS_TARGET=<app-insights-resource-id>   # optional, for Logs
    export FINOPS_READ_ONLY=1                              # optional, hides writes
    python server.py

The server binds to 127.0.0.1 by default and has no user sign-in of its own – everyone who can
reach it acts with the server's Azure identity. Before sharing it, host it behind Entra ID
authentication (for example App Service Easy Auth) and keep FINOPS_READ_ONLY=1 for viewers.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import mimetypes
import re
import sys
import traceback
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
MAX_BODY_BYTES = 1_000_000
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
sys.path.insert(0, str(HERE.parents[1] / "src"))  # run from a source checkout without installing

from foundry_finops.errors import HttpResponseError  # noqa: E402
from foundry_finops.service import FinOpsService, ReadOnlyError, from_env  # noqa: E402

log = logging.getLogger("finops.dashboard")
STATIC = HERE / "static"
Handler = Callable[[FinOpsService, dict[str, Any], dict[str, str], Any], Any]
ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


def _int(q: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(q.get(key, default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ read APIs
@route("GET", "/api/info")
def info(s, q, p, b): return s.info()
@route("GET", "/api/overview")
def overview(s, q, p, b): return s.overview(_int(q, "days", 30))
@route("GET", "/api/cost")
def cost(s, q, p, b): return s.cost_series(q.get("group_by", "project"), _int(q, "days", 30))
@route("GET", "/api/tokens")
def tokens(s, q, p, b): return s.token_series(q.get("group_by", "deployment"), _int(q, "hours", 168))
@route("GET", "/api/cost-controls")
def ccs(s, q, p, b): return s.list_cost_controls()
@route("GET", "/api/budgets")
def budgets(s, q, p, b): return s.list_budgets()
@route("GET", "/api/tags")
def tags(s, q, p, b): return s.list_tags()
@route("GET", "/api/deployments")
def deployments(s, q, p, b): return s.list_deployments()
@route("GET", "/api/quota")
def quota(s, q, p, b): return s.list_quota()
@route("GET", "/api/alerts")
def alerts(s, q, p, b): return s.list_alerts()
@route("GET", "/api/apim")
def apim(s, q, p, b): return s.apim_overview()
@route("GET", "/api/logs/schema")
def schema(s, q, p, b): return s.schema()
@route("GET", "/api/logs/saved")
def saved(s, q, p, b): return s.saved_queries()
@route("POST", "/api/logs/query")
def kql(s, q, p, b): return s.kql(b.get("query", ""), b.get("timespan", "P7D"))


# ----------------------------------------------------------------- write APIs
@route("POST", "/api/cost-controls")
def cc_upsert(s, q, p, b): return s.upsert_cost_control(b)
@route("DELETE", "/api/cost-controls/(?P<name>[^/]+)")
def cc_delete(s, q, p, b): return s.delete_cost_control(p["name"], q.get("project") or None)
@route("POST", "/api/budgets")
def budget_upsert(s, q, p, b): return s.upsert_budget(b)
@route("DELETE", "/api/budgets/(?P<name>[^/]+)")
def budget_delete(s, q, p, b): return s.delete_budget(p["name"])
@route("PUT", "/api/tags")
def tags_set(s, q, p, b): return s.set_tags(b["scope"], {k: str(v) for k, v in (b.get("tags") or {}).items()})
@route("PUT", "/api/deployments/(?P<name>[^/]+)/capacity")
def capacity(s, q, p, b): return s.set_capacity(p["name"], int(b["capacity"]))
@route("POST", "/api/alerts/metric")
def alert_upsert(s, q, p, b): return s.upsert_metric_alert(b)
@route("DELETE", "/api/alerts/(?P<kind>metric|log|action_group)/(?P<name>[^/]+)")
def alert_delete(s, q, p, b): return s.delete_alert(p["kind"], p["name"])
@route("POST", "/api/apim/products")
def prod_upsert(s, q, p, b): return s.upsert_apim_product(b)
@route("DELETE", "/api/apim/products/(?P<name>[^/]+)")
def prod_delete(s, q, p, b): return s.delete_apim_product(p["name"])
@route("POST", "/api/apim/subscriptions")
def sub_upsert(s, q, p, b): return s.upsert_apim_subscription(b)
@route("DELETE", "/api/apim/subscriptions/(?P<name>[^/]+)")
def sub_delete(s, q, p, b): return s.delete_apim_subscription(p["name"])
@route("PUT", "/api/apim/token-limits")
def tl_set(s, q, p, b): return s.set_token_limit(b.pop("scope"), b)
@route("DELETE", "/api/apim/token-limits")
def tl_delete(s, q, p, b): return s.delete_token_limit(q["scope"])


class App(BaseHTTPRequestHandler):
    service: FinOpsService
    server_version = "FoundryFinOpsDashboard/0.1"
    timeout = 30  # seconds: a stalled client must not hold a thread forever
    #: Host header values accepted. Empty = loopback names only (blocks DNS-rebinding against a local server).
    allowed_hosts: frozenset[str] = frozenset()

    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip().lower()
        host = raw.rsplit(":", 1)[0].strip("[]") if raw.count(":") <= 1 or raw.startswith("[") else raw.strip("[]")
        return host in LOOPBACK_HOSTS or host in self.allowed_hosts

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "Host not allowed"})
        url = urlparse(self.path)
        path = unquote(url.path)
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else self._json(405, {"error": "Method not allowed"})
        if method != "GET" and self.headers.get("X-Requested-With") != "finops-dashboard":
            return self._json(403, {"error": "Missing X-Requested-With header"})  # simple CSRF guard
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body: Any = {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Invalid Content-Length"})
        if length < 0 or length > MAX_BODY_BYTES:
            return self._json(413, {"error": "Request body too large"})
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"error": "Body must be JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "Body must be a JSON object"})
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if m == method and match:
                try:
                    result = fn(self.service, query, match.groupdict(), body)
                    return self._json(200, {"ok": True} if result is None else result)
                except ReadOnlyError as e:
                    return self._json(403, {"error": str(e)})
                except (ValueError, KeyError) as e:
                    return self._json(400, {"error": str(e).strip("'")})
                except HttpResponseError as e:
                    # 401 here means the *server's* Azure identity failed, which the browser can't fix
                    status = e.status_code if 400 <= e.status_code < 600 and e.status_code != 401 else 502
                    return self._json(status, {"error": e.message, "code": e.code})
                except Exception as e:  # keep the server up, log the detail
                    log.error("Unhandled error: %s\n%s", e, traceback.format_exc())
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": f"No route for {method} {path}"})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        try:
            target = (STATIC / rel).resolve()
        except (ValueError, OSError):  # NUL bytes and other unresolvable paths
            target = STATIC / "index.html"
        if STATIC not in target.parents or not target.is_file():
            target = STATIC / "index.html"
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def do_GET(self) -> None: self._dispatch("GET")
    def do_POST(self) -> None: self._dispatch("POST")
    def do_PUT(self) -> None: self._dispatch("PUT")
    def do_DELETE(self) -> None: self._dispatch("DELETE")


def make_server(host: str = "127.0.0.1", port: int = 8765, service: FinOpsService | None = None) -> ThreadingHTTPServer:
    App.service = service or from_env()
    App.allowed_hosts = frozenset({host.lower()} - {"0.0.0.0", "::"})
    srv = ThreadingHTTPServer((host, port), App)
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description="Foundry FinOps dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra Host header value to accept when served behind a proxy (repeatable)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    srv = make_server(args.host, args.port)
    App.allowed_hosts = App.allowed_hosts | {h.lower() for h in args.allow_host}
    log.info("Foundry FinOps dashboard (%s mode) on http://%s:%s", App.service.info()["mode"], args.host, args.port)
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
