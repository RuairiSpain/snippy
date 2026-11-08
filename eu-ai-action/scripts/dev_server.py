"""Local server: the portal plus the same API logic, backed by fixtures and an in-memory store.
Run from eu-ai-action/:  python scripts/dev_server.py [port]
Not for production: authentication is disabled."""
from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eu_ai_action.api import Api, ApiError  # noqa: E402
from eu_ai_action.runtime import build_pipeline  # noqa: E402
from eu_ai_action.settings import Settings  # noqa: E402

PORTAL = Path(__file__).resolve().parent.parent / "portal"
TENANT = "00000000-0000-0000-0000-000000000001"


def make_api() -> Api:
    s = Settings(_env_file=None, use_fixtures=True, use_memory_store=True, auth_disabled=True, target_tenant_id=TENANT,
                 require_counsel_review_for_release=False, files_dir=".data/dev-files", fixtures_dir="tests/fixtures")
    return Api(s, build_pipeline(s))


API = make_api()
ROUTES = [
    ("POST", r"/api/tenants/([^/]+)/scans", lambda t, q, b: API.start_scan({}, t)),
    ("GET", r"/api/tenants/([^/]+)/agents", lambda t, q, b: API.list_agents({}, t)),
    ("GET", r"/api/tenants/([^/]+)/tasks", lambda t, q, b: API.list_tasks({}, t)),
    ("PATCH", r"/api/tenants/([^/]+)/defaults", lambda t, q, b: API.patch_defaults({}, t, b)),
    ("GET", r"/api/tenants/([^/]+)/agents/([^/]+)", lambda t, a, q, b: API.get_agent({}, t, a)),
    ("PATCH", r"/api/tenants/([^/]+)/agents/([^/]+)/inputs", lambda t, a, q, b: API.patch_inputs({}, t, a, b)),
    ("POST", r"/api/tenants/([^/]+)/agents/([^/]+)/generate", lambda t, a, q, b: API.regenerate({}, t, a)),
    ("POST", r"/api/tenants/([^/]+)/documents/([^/]+)/transition", lambda t, d, q, b: API.transition_document({}, t, d, str(b.get("action", "")))),
]


class H(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body: dict = {}
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = json.loads(self.rfile.read(n))
        try:
            m = re.fullmatch(r"/api/tenants/([^/]+)/documents/([^/]+)/file", u.path)
            if method == "GET" and m:
                data, ctype = API.get_file({}, m[1], m[2], q.get("format", "pdf"))
                return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{m[2]}.{q.get("format", "pdf")}"'})
            for meth, pat, fn in ROUTES:
                m = re.fullmatch(pat, u.path)
                if meth == method and m:
                    return self._send(200, json.dumps(fn(*m.groups(), q, body), ensure_ascii=False).encode(), "application/json")
            if method == "GET" and not u.path.startswith("/api"):
                f = PORTAL / ("index.html" if u.path == "/" else u.path.lstrip("/"))
                if f.is_file() and PORTAL in f.resolve().parents:
                    ct = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}.get(f.suffix, "application/octet-stream")
                    return self._send(200, f.read_bytes(), ct + "; charset=utf-8")
            self._send(404, b'{"error":"not found"}', "application/json")
        except ApiError as e:
            self._send(e.status, json.dumps({"error": e.message}).encode(), "application/json")

    def do_GET(self) -> None: self._dispatch("GET")  # noqa: E704
    def do_POST(self) -> None: self._dispatch("POST")  # noqa: E704
    def do_PATCH(self) -> None: self._dispatch("PATCH")  # noqa: E704

    def log_message(self, *a) -> None: pass  # noqa: E704


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"portal on http://127.0.0.1:{port}/?tenant={TENANT}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
