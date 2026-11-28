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
        "pool": {"max_sandboxes": 2, "users_per_sandbox": 2,
                 "egress": {"mode": "allowlist", "allow_hosts": ["graph.microsoft.com"]}},
        **over,
    }
    return Config.model_validate(data)


def delegated_token(scope: str, oid: str = "o1", tid: str = TENANT, **claims) -> str:
    """What Entra returns for an OBO exchange: a JWT for the user, with an `scp` claim."""
    body = {"oid": oid, "tid": tid, "scp": scope.rsplit("/", 1)[-1], "aud": "api://x", **claims}
    return jwt.encode({k: v for k, v in body.items() if v is not None}, "k" * 32, algorithm="HS256")


class FakeMsal:
    def __init__(self, fail: dict | None = None, claims: dict | None = None, token=None):
        self.calls, self.fail, self.claims, self.token = [], fail, claims or {}, token

    def acquire_token_on_behalf_of(self, assertion, scopes):
        self.calls.append((assertion, scopes))
        if self.fail:
            return self.fail
        tok = self.token or delegated_token(scopes[0], **self.claims)
        return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600}


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
    # Root-owned file in a root-owned directory, readable by the user's private group only.
    assert backend.modes[(sid, env.obo_path)] == "640"
    token = delegated_token("https://graph.microsoft.com/.default")
    assert token.encode() in backend.files[(sid, env.obo_path)]
    cmds = [c for _, c in backend.execs]
    # The token never appears in a command line (visible to other users via ps).
    assert not any(token in c for c in cmds)
    run = next(c for c in cmds if "runuser" in c)
    assert f"OBO_TOKENS_FILE={env.obo_path}" in run
    # Root-owned directory, group-readable by the user only; root never chowns a path the
    # user controls.
    assert env.obo_path.startswith("/var/lib/smux/req/") and "chown" not in run
    assert f"chgrp -h {env.user} {env.request_path} {env.obo_path}" in run
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


# ---- the token handed to the agent must be this user's delegated token --------------------------

WORKIQ = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"


def workiq_cfg(**obo) -> Config:
    return obo_cfg(obo={"enabled": True, "client_id": "cid", "resources": {"workiq": [WORKIQ]}, **obo},
                   pool={"max_sandboxes": 2, "users_per_sandbox": 2,
                         "egress": {"mode": "allowlist", "allow_hosts": ["workiq.svc.cloud.microsoft"]}})


async def test_work_iq_gets_the_delegated_scope_and_the_users_own_token(make_client):
    msal = FakeMsal()
    client, backend = await make_client(workiq_cfg(), msal, Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "meetings?", "obo_resources": ["workiq"]})
    assert r.status_code == 200
    assert msal.calls[0][1] == [WORKIQ]
    env = UserEnv(workiq_cfg(), Principal(TENANT, "o1").uid)
    stored = [v for (s, p), v in backend.files.items() if p == env.obo_path][0]
    assert delegated_token(WORKIQ).encode() in stored


@pytest.mark.parametrize("claims, what", [
    ({"scp": None, "roles": ["WorkIQ.Read.All"]}, "not a delegated"),       # application permission
    ({"idtyp": "app"}, "not a delegated"),
    ({"scp": None}, "not a delegated"),
    ({"oid": "someone-else"}, "does not belong"),
    ({"tid": "other-tenant"}, "does not belong"),
])
async def test_a_token_that_is_not_this_users_delegated_token_is_never_handed_over(make_client, claims, what):
    client, backend = await make_client(workiq_cfg(), FakeMsal(claims=claims),
                                        Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "x", "obo_resources": ["workiq"]})
    assert r.status_code == 502 and what in r.json()["detail"]
    assert backend.sandboxes == {} and backend.files == {}                  # no slot taken, nothing written


async def test_opaque_tokens_fail_closed_unless_verification_is_turned_off(make_client):
    client, backend = await make_client(workiq_cfg(), FakeMsal(token="opaque-token"),
                                        Principal(TENANT, "o1", user_token()))
    r = await client.post("/v1/agent/invoke", json={"prompt": "x", "obo_resources": ["workiq"]})
    assert r.status_code == 502 and "verify_delegated" in r.json()["detail"]

    client2, _ = await make_client(workiq_cfg(verify_delegated=False), FakeMsal(token="opaque-token"),
                                   Principal(TENANT, "o1", user_token()))
    assert (await client2.post("/v1/agent/invoke",
                               json={"prompt": "x", "obo_resources": ["workiq"]})).status_code == 200
