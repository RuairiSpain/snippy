import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from foundry_finops.service import LiveService, Settings
from mcp_server.app import McpApp, make_handler
from mcp_server.auth import AuthConfig, CredentialCache, TokenValidator
from mcp_server.tools import TOOLS

TENANT, CLIENT = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    class _K:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def token(**over):
    now = int(time.time())
    claims = {"aud": f"api://{CLIENT}", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT,
              "oid": "user-1", "preferred_username": "ruairi@contoso.com", "scp": "access_as_user",
              "iat": now, "exp": now + 600}
    claims.update(over)
    return jwt.encode(claims, KEY, algorithm="RS256")


def serve(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


def rpc(url, method, params=None, mid=1, auth=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        body["id"] = mid
    return requests.post(url, data=json.dumps(body), headers=headers, timeout=10)


class DemoProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = serve(McpApp(auth=None, settings=Settings(demo=True)))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_handshake(self):
        r = rpc(self.url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("Mcp-Session-Id", r.headers)
        self.assertEqual(rpc(self.url, "notifications/initialized", mid=None).status_code, 202)

    def test_tools_list_schemas(self):
        tools = rpc(self.url, "tools/list").json()["result"]["tools"]
        self.assertEqual(len(tools), len(TOOLS))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", t["annotations"])

    def test_every_read_tool_runs(self):
        args = {"run_kql_query": {"query": "CostDaily | summarize Cost=sum(Cost) by Team"}}
        for t in TOOLS:
            if not t.read_only:
                continue
            with self.subTest(tool=t.name):
                res = rpc(self.url, "tools/call", {"name": t.name, "arguments": args.get(t.name, {})}).json()["result"]
                self.assertFalse(res["isError"], res)
                json.loads(res["content"][0]["text"])

    def test_write_and_validation_error(self):
        ok = rpc(self.url, "tools/call", {"name": "upsert_cost_control", "arguments": {
            "name": "mcp-cap", "amount": 25, "time_grain": "Daily", "block_threshold_pct": 100}}).json()["result"]
        self.assertFalse(ok["isError"])
        bad = rpc(self.url, "tools/call", {"name": "upsert_cost_control", "arguments": {
            "name": "x", "amount": 5, "filters": {"agent": ["a"]}}}).json()["result"]
        self.assertTrue(bad["isError"])
        self.assertIn("project", bad["content"][0]["text"])

    def test_unknown_method_and_tool(self):
        self.assertEqual(rpc(self.url, "nope").json()["error"]["code"], -32601)
        self.assertTrue(rpc(self.url, "tools/call", {"name": "nope"}).json()["result"]["isError"])


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = AuthConfig(TENANT, CLIENT, public_base_url="https://finops-mcp.contoso.com")
        cls.app = McpApp(auth=cfg, settings=Settings(demo=True), validator=TokenValidator(cfg, FakeJwks()))
        cls.srv, cls.url = serve(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_missing_token_gets_challenge(self):
        r = rpc(self.url, "tools/list")
        self.assertEqual(r.status_code, 401)
        self.assertIn('resource_metadata="https://finops-mcp.contoso.com/.well-known/oauth-protected-resource"',
                      r.headers["WWW-Authenticate"])

    def test_metadata(self):
        meta = requests.get(self.url.replace("/mcp", "/.well-known/oauth-protected-resource")).json()
        self.assertEqual(meta["authorization_servers"], [f"https://login.microsoftonline.com/{TENANT}/v2.0"])
        self.assertEqual(meta["scopes_supported"], [f"api://{CLIENT}/access_as_user"])

    def test_valid_token(self):
        self.assertEqual(rpc(self.url, "tools/list", auth=token()).status_code, 200)

    def test_bad_tokens(self):
        self.assertEqual(rpc(self.url, "tools/list", auth=token(aud="api://other")).status_code, 401)
        self.assertEqual(rpc(self.url, "tools/list", auth=token(iss="https://evil/v2.0")).status_code, 401)
        self.assertEqual(rpc(self.url, "tools/list", auth=token(exp=int(time.time()) - 3600)).status_code, 401)
        self.assertEqual(rpc(self.url, "tools/list", auth=token(scp="other")).status_code, 403)

    def test_live_service_uses_user_obo_credential(self):
        seen = []
        cfg = AuthConfig(TENANT, CLIENT)
        cache = CredentialCache(cfg, factory=lambda tok: seen.append(tok) or f"cred-for-{len(seen)}")
        app = McpApp(auth=cfg, settings=Settings(subscription_id="sub", resource_group="rg", foundry_account="acct"),
                     validator=TokenValidator(cfg, FakeJwks()), credentials=cache)
        user = app.validator.validate(f"Bearer {token()}")
        svc = app.service_for(user, {"resource_group": "rg2"})
        self.assertIsInstance(svc, LiveService)
        self.assertEqual(svc.settings.resource_group, "rg2")
        self.assertEqual(svc.fo._transport.credential, "cred-for-1")
        app.service_for(user, {})
        self.assertEqual(len(seen), 1)  # cached per token


if __name__ == "__main__":
    unittest.main()
