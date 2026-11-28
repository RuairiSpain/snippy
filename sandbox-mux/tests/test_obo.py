import fakeredis
import fakeredis.aioredis
import httpx
import jwt
import pytest
from asgi_lifespan import LifespanManager

from sandbox_mux.api import create_app
from sandbox_mux.auth import Principal
from sandbox_mux.backends.base import ExecOutcome
from sandbox_mux.backends.fake import FakeSandboxBackend
from sandbox_mux.config import Config
from sandbox_mux.obo import OboBroker, OboError, check_same_user, extract_assertion
from sandbox_mux.service import UserEnv

TENANT = "11111111-1111-1111-1111-111111111111"


def user_token(oid="o1", tid=TENANT) -> str:
    return jwt.encode({"oid": oid, "tid": tid}, "k" * 32, algorithm="HS256")


def obo_cfg(**over) -> Config:
    data = {
        "backend": "fake",
        "auth": {"mode": "entra", "tenant_id": TENANT, "audience": "api://mux"},
        "obo": {"enabled": True, "client_id": "cid",
                "resources": {"graph": ["https://graph.microsoft.com/.default"],
                              "storage": ["https://storage.azure.com/user_impersonation"]}},
        "pool": {"max_sandboxes": 2, "users_per_sandbox": 2},
        **over,
    }
    return Config.model_validate(data)


class FakeMsal:
    def __init__(self, fail: dict | None = None):
        self.calls, self.fail = [], fail

    def acquire_token_on_behalf_of(self, assertion, scopes):
        self.calls.append((assertion, scopes))
        if self.fail:
            return self.fail
        return {"access_token": f"AT-{scopes[0]}", "token_type": "Bearer", "expires_in": 3600}


class StubAuth:
    def __init__(self, principal):
        self.principal = principal

    async def authenticate(self, *a, **k):
        return self.principal


@pytest.fixture
async def make_client():
    contexts = []

    async def _make(cfg, msal, principal):
        backend = FakeSandboxBackend()
        app = create_app(cfg, backend=backend, redis_client=fakeredis.aioredis.FakeRedis(),
                         run_reaper=False, obo_broker=OboBroker(cfg, lambda: msal))
        lm = LifespanManager(app)
        await lm.__aenter__()
        contexts.append(lm)
        app.state.auth = StubAuth(principal)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        return client, backend

    yield _make
    for lm in contexts:
        await lm.__aexit__(None, None, None)


async def test_tokens_reach_only_the_users_file_and_are_deleted(make_client):
    tok = user_token()
    msal = FakeMsal()
    client, backend = await make_client(obo_cfg(), msal, Principal(TENANT, "o1", assertion=tok))
    r = await client.post("/v1/agent/invoke",
                          json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 200
    assert msal.calls == [(tok, ["https://graph.microsoft.com/.default"])]

    uid = Principal(TENANT, "o1").uid
    env = UserEnv(obo_cfg(), uid)
    sid = r.json()["sandbox_id"]
    # Written owner-only, with the delegated token in it.
    assert backend.modes[(sid, env.obo_path)] == "600"
    assert b"AT-https://graph.microsoft.com/.default" in backend.files[(sid, env.obo_path)]
    cmds = [c for _, c in backend.execs]
    # The token never appears in a command line (visible to other users via ps).
    assert not any("AT-https" in c for c in cmds)
    run = next(c for c in cmds if "runuser" in c)
    assert f"OBO_TOKENS_FILE={env.obo_path}" in run
    assert f"chown {env.user}:{env.user} {env.request_path} {env.obo_path}" in run
    # And the file is removed straight after the turn.
    assert cmds[-1].strip() == f"rm -f {env.obo_path}"


async def test_tokens_are_deleted_when_the_agent_fails(make_client):
    client, backend = await make_client(obo_cfg(), FakeMsal(), Principal(TENANT, "o1", user_token()))
    backend.handler = lambda sid, c: ExecOutcome(1, "", "boom") if "runuser" in c else None
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 502
    assert [c for _, c in backend.execs][-1].startswith("rm -f ")


async def test_no_obo_means_no_token_file_and_no_exchange(make_client):
    msal = FakeMsal()
    client, backend = await make_client(obo_cfg(), msal, Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi"})
    assert r.status_code == 200 and msal.calls == []
    assert not any("obo.json" in p for _, p in backend.files)
    assert not any("OBO_TOKENS_FILE" in c for _, c in backend.execs)


async def test_unknown_resource_alias_is_rejected_before_any_exchange(make_client):
    msal = FakeMsal()
    client, backend = await make_client(obo_cfg(), msal, Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["keyvault"]})
    assert r.status_code == 422 and msal.calls == [] and backend.sandboxes == {}


async def test_too_many_resources_rejected(make_client):
    cfg = obo_cfg(obo={"enabled": True, "client_id": "c", "max_resources_per_turn": 1,
                       "resources": {"a": ["s1"], "b": ["s2"]}})
    client, _ = await make_client(cfg, FakeMsal(), Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["a", "b"]})
    assert r.status_code == 422


async def test_consent_required_returns_403_with_claims_challenge_and_uses_no_sandbox(make_client):
    msal = FakeMsal(fail={"error": "invalid_grant", "error_description": "AADSTS50076 ...",
                          "claims": '{"access_token":{"capolids":{"essential":true}}}'})
    client, backend = await make_client(obo_cfg(), msal, Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 403 and "x-claims-challenge" in r.headers
    assert "AADSTS" not in r.text          # upstream error text is not echoed
    assert backend.sandboxes == {}


async def test_other_exchange_errors_are_502(make_client):
    client, _ = await make_client(obo_cfg(), FakeMsal(fail={"error": "invalid_client"}),
                                  Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 502


async def test_request_without_a_user_token_is_401(make_client):
    client, _ = await make_client(obo_cfg(), FakeMsal(), Principal(TENANT, "o1", assertion=None))
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 401


async def test_obo_requested_while_disabled_is_422():
    cfg = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}})
    app = create_app(cfg, backend=FakeSandboxBackend(),
                     redis_client=fakeredis.aioredis.FakeRedis(), run_reaper=False)
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.post("/v1/agent/invoke", headers={"X-Dev-User": "a"},
                             json={"prompt": "hi", "obo_resources": ["graph"]})
    assert r.status_code == 422


def test_assertion_must_belong_to_the_authenticated_user():
    check_same_user(user_token("o1"), TENANT, "o1")
    with pytest.raises(OboError) as e:
        check_same_user(user_token("someone-else"), TENANT, "o1")
    assert e.value.status == 403
    with pytest.raises(OboError):
        check_same_user(user_token("o1", tid="other-tenant"), TENANT, "o1")
    with pytest.raises(OboError) as e:
        check_same_user("not-a-jwt", TENANT, "o1")
    assert e.value.status == 400


def test_extract_assertion_handles_bearer_and_raw_values():
    assert extract_assertion({"Authorization": "Bearer abc"}, "Authorization") == "abc"
    assert extract_assertion({"X-User-Token": "abc"}, "X-User-Token") == "abc"
    assert extract_assertion({}, "Authorization") is None


def test_principal_repr_never_contains_the_token():
    assert "SECRET" not in repr(Principal("t", "o", assertion="SECRET"))


def test_obo_needs_unix_user_isolation_and_entra_auth():
    with pytest.raises(ValueError, match="unix_user"):
        obo_cfg(isolation={"mode": "home_only"})
    with pytest.raises(ValueError, match="entra"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                               "obo": {"enabled": True, "client_id": "c", "resources": {"g": ["s"]}}})
    with pytest.raises(ValueError, match="client_id"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "entra", "tenant_id": "t",
                                                           "audience": "a"},
                               "obo": {"enabled": True, "resources": {"g": ["s"]}}})
