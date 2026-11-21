"""Regression tests for the issues found in the code review of the MCP server."""

import http.client
import json
import unittest

import requests

from foundry_finops.service import Settings
from mcp_server.app import McpApp
from mcp_server.auth import AuthConfig, CredentialCache, TokenValidator, User

from .test_mcp import FakeJwks, rpc, serve, token


def call(url, name, arguments, **kw):
    return rpc(url, "tools/call", {"name": name, "arguments": arguments}, **kw).json()["result"]


class ToolArgumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = serve(McpApp(auth=None, settings=Settings(demo=True)))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_schema_is_enforced_server_side(self):
        cases = [
            ("get_cost_breakdown", {"days": 0}, "at least 1"),
            ("get_cost_breakdown", {"group_by": "galaxy"}, "one of"),
            ("set_deployment_capacity", {"deployment": "d", "capacity": -5}, "at least 1"),
            ("set_deployment_capacity", {"deployment": "d", "capacity": True}, "integer"),
            ("upsert_budget", {"name": "b", "amount": 10, "tag_filter": {"name": "team"}}, "tag_filter.values"),
            ("upsert_budget", {"name": "bad name!", "amount": 10}, "must match"),
            ("delete_budget", {}, "name is required"),
            ("list_budgets", {"surprise": 1}, "Unknown argument"),
        ]
        for tool, args, expect in cases:
            with self.subTest(tool=tool, args=args):
                res = call(self.url, tool, args)
                self.assertTrue(res["isError"])
                self.assertIn(expect, res["content"][0]["text"])

    def test_non_object_messages_get_json_rpc_errors(self):
        r = requests.post(self.url, json=[1, "x"], timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(item["error"]["code"] == -32600 for item in r.json()))
        r = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, timeout=10)
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_bad_content_length_does_not_crash_the_server(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Content-Length", "abc")  # `requests` would overwrite this, so send it by hand
        conn.endheaders()
        status = conn.getresponse().status
        conn.close()
        self.assertEqual(status, 400)
        self.assertEqual(requests.get(self.url.replace("/mcp", "/healthz"), timeout=10).status_code, 200)

    def test_browser_origin_is_rejected_without_auth(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        evil = requests.post(self.url, data=body, headers={"Origin": "https://evil.example.com"}, timeout=10)
        self.assertEqual(evil.status_code, 403)
        local = requests.post(self.url, data=body, headers={"Origin": "http://localhost:3000"}, timeout=10)
        self.assertEqual(local.status_code, 200)


class ClientLifecycleTests(unittest.TestCase):
    def test_per_request_client_is_closed(self):
        closed = []

        class Fake:
            settings = Settings(subscription_id="s")

            class fo:  # noqa: N801
                @staticmethod
                def close():
                    closed.append(True)

            def list_budgets(self):
                return []

        app = McpApp(auth=None, settings=Settings(subscription_id="s"))
        app.service_for = lambda user, args: Fake()
        app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_budgets", "arguments": {}}}, None)
        self.assertEqual(closed, [True])


class AuthTests(unittest.TestCase):
    def test_insufficient_scope_challenge(self):
        cfg = AuthConfig("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
                         public_base_url="https://x.example")
        srv, url = serve(McpApp(auth=cfg, settings=Settings(demo=True), validator=TokenValidator(cfg, FakeJwks())))
        try:
            r = rpc(url, "tools/list", auth=token(scp="other"))
            self.assertEqual(r.status_code, 403)
            self.assertIn("insufficient_scope", r.headers["WWW-Authenticate"])
        finally:
            srv.shutdown()

    def test_credential_cache_is_bounded_and_keyed_by_token_hash(self):
        cache = CredentialCache(AuthConfig("t", "c"), factory=lambda tok: object(), max_entries=3)
        users = [User("u", "t", "", "", (), f"tok-{i}", expires_at=0) for i in range(10)]
        creds = [cache.for_user(u) for u in users]
        self.assertLessEqual(len(cache._items), 3)
        self.assertIs(cache.for_user(users[-1]), creds[-1])
        self.assertTrue(all("tok-" not in k for k in cache._items))


if __name__ == "__main__":
    unittest.main()
