"""Fair queueing, per-source callback secrets, narrow delegated scope, and human approval."""

import json

import pytest

from sandbox_mux import approvals as appr
from sandbox_mux import triggers as trg
from sandbox_mux.auth import uid_for
from sandbox_mux.backends.base import ExecOutcome
from sandbox_mux.config import ApprovalsConfig, Config
from sandbox_mux.service import UserEnv
from sandbox_mux.store import now_ms
from test_triggers import (CBKEYS, EMAIL, GRAPH, NS, TENANT, U, World, cb, consent, delegated_token,  # noqa: F401
                           make_app, subscribe, tcfg)


def with_triggers(**kw) -> Config:
    cfg = tcfg().model_dump()
    cfg["triggers"].update(kw)
    return Config.model_validate(cfg)


async def fire(client, event_id, **over):
    return (await client.post(cb("mail"), json={**EMAIL, "id": event_id, **over}, headers=NS)).json()


# ---- 3. fair queueing and global caps ---------------------------------------------------------------

async def test_users_are_served_in_rotation_so_a_flood_cannot_starve_others(make_app):
    client, app, *_ = await make_app(with_triggers(max_runs_per_user_per_hour=1000, max_queued_per_user=50))
    svc = app.state.ctx["triggers"]
    await subscribe(client, "alice", filters={"subject_contains": ["from-a"]})
    await subscribe(client, "bob", filters={"subject_contains": ["from-b"]})
    for i in range(6):
        await fire(client, f"a{i}", subject="from-a")                      # alice is flooded
    await fire(client, "b0", subject="from-b")                              # bob has one
    runs = {}
    for rid in await svc.pending_run_ids():
        run = await svc._run(rid)
        runs[rid] = run["uid"]
    order = [runs[r] for r in await svc.pending_run_ids()]
    assert order[:2] == [uid_for(TENANT, "alice"), uid_for(TENANT, "bob")]   # bob is second, not seventh
    assert order.count(uid_for(TENANT, "alice")) == 6

    served = []
    while (item := await svc._next()) is not None:
        served.append(item[1])
        await svc._release(item[1], item[0])
    assert served[:2] == [uid_for(TENANT, "alice"), uid_for(TENANT, "bob")] and len(served) == 7


async def test_a_user_with_a_run_in_flight_is_skipped_not_run_twice_at_once(make_app):
    client, app, *_ = await make_app(with_triggers(max_runs_per_user_per_hour=1000))
    svc = app.state.ctx["triggers"]
    await subscribe(client, "alice")
    await subscribe(client, "bob")
    for i in range(2):
        await fire(client, f"e{i}", **{"from": "x@y.com"})
    first = await svc._next()
    second = await svc._next()
    third = await svc._next()
    assert {first[1], second[1]} == {uid_for(TENANT, "alice"), uid_for(TENANT, "bob")} and third is None
    await svc._release(first[1], first[0])
    again = await svc._next()
    assert again and again[1] == first[1]                                    # their next run becomes available


async def run_statuses(client, user):
    sub = (await client.get("/v1/triggers", headers=U(user))).json()[0]
    return (await client.get(f"/v1/triggers/{sub['id']}/runs", headers=U(user))).json()


async def test_one_users_flood_is_capped_and_does_not_touch_another_users_queue(make_app):
    client, app, *_ = await make_app(with_triggers(max_queued_per_user=3, max_runs_per_user_per_hour=1000))
    await subscribe(client, "alice", filters={"subject_contains": ["a"]})
    await subscribe(client, "bob", filters={"subject_contains": ["b"]})
    out = [await fire(client, f"a{i}", subject="a") for i in range(5)]
    assert [o["queued"] for o in out] == [1, 1, 1, 0, 0] and [o["skipped"] for o in out] == [0, 0, 0, 1, 1]
    assert (await fire(client, "b0", subject="b"))["queued"] == 1                  # bob is unaffected
    alice = await run_statuses(client, "alice")
    assert sorted(r["status"] for r in alice) == ["queued"] * 3 + ["skipped"] * 2
    assert {r["error"] for r in alice if r["status"] == "skipped"} == {"too many runs waiting for this user"}


async def test_the_whole_queue_is_capped_and_dropped_runs_are_recorded(make_app):
    client, app, *_ = await make_app(with_triggers(max_queue_depth=3, max_queued_per_user=10,
                                                   max_runs_per_user_per_hour=1000))
    svc = app.state.ctx["triggers"]
    await subscribe(client, "alice", filters={"subject_contains": ["a"]})
    await subscribe(client, "bob", filters={"subject_contains": ["b"]})
    for i in range(3):
        assert (await fire(client, f"a{i}", subject="a"))["queued"] == 1
    late = await fire(client, "b0", subject="b")
    assert late["queued"] == 0 and late["skipped"] == 1
    assert [r["error"] for r in await run_statuses(client, "bob")] == ["the trigger queue is full"]
    assert (await svc.queue_stats())["waiting"] == 3
    item = await svc._next()                                                         # room opens as runs are taken
    assert (await fire(client, "b1", subject="b"))["queued"] == 1 and item


async def test_retries_do_not_count_against_the_queue_caps(make_app):
    client, app, *_ = await make_app(with_triggers(max_queued_per_user=1, max_runs_per_user_per_hour=1000))
    svc = app.state.ctx["triggers"]
    await subscribe(client, "alice")
    await fire(client, "e0")
    rid, uid = await svc._next()
    assert await svc._enqueue(rid, uid, force=True) == "ok"                  # put back after a busy user
    assert await svc._enqueue("other", uid) == "user"                         # but a new run is over the cap


async def test_global_concurrency_cap_and_self_healing_leases(make_app):
    client, app, *_ = await make_app(with_triggers(max_concurrent_runs=1, max_runs_per_user_per_hour=1000))
    svc = app.state.ctx["triggers"]
    await subscribe(client, "alice")
    await subscribe(client, "bob")
    await fire(client, "e0")
    first = await svc._next()
    assert first and await svc._next() is None                                # alice is running, the cap is 1
    assert (await svc.queue_stats())["running"] == 1
    # the worker dies: its lease expires and the slot frees itself
    await svc._r.zadd(svc.k("running"), {first[0]: now_ms() - 1})
    await svc._r.delete(svc.k(f"inflight:{first[1]}"))
    second = await svc._next()
    assert second and second[1] != first[1]


async def test_the_queue_is_shared_across_replicas_through_redis(make_app):
    client, app, *_ = await make_app(with_triggers(max_runs_per_user_per_hour=1000))
    a = app.state.ctx["triggers"]
    other = trg.TriggerService(a.cfg, a.store, a.service)                    # a second replica, same Redis
    await subscribe(client, "alice")
    await fire(client, "e0")
    item = await other._next()
    assert item and await a._next() is None                                   # taken once, by one replica
    await other._release(item[1], item[0])


# ---- 4. a secret for each source -----------------------------------------------------------------------

async def test_the_callback_needs_the_secret_for_that_source(make_app):
    client, app, *_ = await make_app()
    await subscribe(client)
    base = "/v1/triggers/callback/mail"
    assert (await client.post(base, json=EMAIL, headers=NS)).status_code == 403                    # identity alone is not enough
    assert (await client.post(base + "?k=wrong", json=EMAIL, headers=NS)).status_code == 403
    chat_secret = CBKEYS.sign("chat")
    assert (await client.post(f"{base}?k={chat_secret}", json=EMAIL, headers=NS)).status_code == 403   # another source's secret
    assert (await client.post(base + f"?k={CBKEYS.sign('mail')}", json=EMAIL, headers=NS)).status_code == 200
    # an unknown source answers like a wrong secret, so callers learn nothing about which exist
    assert (await client.post("/v1/triggers/callback/nope?k=wrong", json=EMAIL, headers=NS)).status_code == 403
    assert (await client.post(f"/v1/triggers/callback/nope?k={CBKEYS.sign('nope')}", json=EMAIL, headers=NS)).status_code == 404


def test_secrets_are_derived_per_source_and_rotate():
    old, new = trg.CallbackSecrets("old-key-0123456789abcdef"), trg.CallbackSecrets("new-key-0123456789abcdef,old-key-0123456789abcdef")
    assert old.sign("mail") != old.sign("chat") and len(old.sign("mail")) == 43
    assert new.verify("mail", old.sign("mail")) and new.sign("mail") != old.sign("mail")     # old URLs keep working
    assert not trg.CallbackSecrets("new-key-0123456789abcdef").verify("mail", old.sign("mail"))
    assert not old.verify("mail", None) and not old.verify("mail", "")


def test_weak_or_missing_keys_are_refused():
    for bad in ("", "short", " , "):
        with pytest.raises(ValueError):
            trg.CallbackSecrets(bad)
    with pytest.raises(ValueError, match="TRIGGER_CALLBACK_KEY"):
        trg.CallbackSecrets.from_env("TRIGGER_CALLBACK_KEY", {})


async def test_the_service_will_not_start_without_the_key_unless_you_turn_the_check_off(monkeypatch):
    import fakeredis.aioredis
    from asgi_lifespan import LifespanManager
    from sandbox_mux.api import create_app
    from sandbox_mux.backends.fake import FakeSandboxBackend
    from cryptography.fernet import Fernet
    from sandbox_mux.delegation import TokenVault
    monkeypatch.delenv("TRIGGER_CALLBACK_KEY", raising=False)
    mk = lambda cfg: create_app(cfg, backend=FakeSandboxBackend(), redis_client=fakeredis.aioredis.FakeRedis(),
                                run_reaper=False, vault=TokenVault(Fernet.generate_key().decode()))
    with pytest.raises(ValueError, match="TRIGGER_CALLBACK_KEY"):
        async with LifespanManager(mk(tcfg())):
            pass
    async with LifespanManager(mk(with_triggers(require_source_secret=False))):
        pass


# ---- 5a. narrow delegated scope ------------------------------------------------------------------------------

def test_delegation_needs_an_explicit_list_of_aliases_that_exist():
    data = tcfg().model_dump()
    data["triggers"]["delegated_aliases"] = []
    with pytest.raises(ValueError, match="delegated_aliases must list"):
        Config.model_validate(data)
    data["triggers"]["delegated_aliases"] = ["mail-send"]
    with pytest.raises(ValueError, match="unknown obo.resources"):
        Config.model_validate(data)


def broad_cfg() -> Config:
    data = tcfg().model_dump()
    data["obo"]["resources"] = {"graph": [GRAPH], "mail-send": ["https://graph.microsoft.com/Mail.Send"]}
    return Config.model_validate(data)


async def test_unattended_runs_can_only_use_the_listed_read_only_aliases(make_app):
    client, app, backend, world = await make_app(broad_cfg())
    base = {"name": "n", "source": "mail", "instruction": "x"}
    r = await client.post("/v1/triggers", json={**base, "delegated_resources": ["mail-send"]}, headers=U("alice"))
    assert r.status_code == 422 and "Not allowed for unattended runs" in r.json()["detail"] and "approvals" in r.json()["detail"]
    start = await client.post("/v1/triggers/consent/start", json={"resource": "mail-send"}, headers=U("alice"))
    assert start.status_code == 422                                           # no consent for it either
    assert (await consent(client, world)).status_code == 200                   # the allowed one works
    assert (await client.post("/v1/triggers", json={**base, "delegated_resources": ["graph"]}, headers=U("alice"))).status_code == 201


async def test_the_allowlist_is_enforced_again_when_a_token_is_fetched(make_app):
    client, app, *_ = await make_app(broad_cfg())
    from sandbox_mux.delegation import DelegationError
    with pytest.raises(DelegationError) as e:
        await app.state.ctx["delegation"].tokens_for(uid_for(TENANT, "alice"), TENANT, "alice", ["mail-send"])
    assert e.value.status == 403


# ---- 5b. approvals --------------------------------------------------------------------------------------------------

ACTION = {"kind": "send_email", "summary": "Reply to Bob: invoice received",
          "payload": {"to": "bob@partner.com", "body": "Thanks, paid on Friday."}}


def proposing_handler(proposals):
    def h(sid, cmd):
        if "exec runuser" in cmd and "approval" not in cmd and "--blocking" not in cmd:
            return ExecOutcome(0, json.dumps({"reply": "done", "proposed_actions": proposals}), "")
    return h


async def triggered_run_with(client, app, backend, proposals):
    backend.handler = proposing_handler(proposals)
    sub = await subscribe(client)
    await client.post(cb("mail"), json=EMAIL, headers=NS)
    await app.state.ctx["triggers"].drain_once()
    backend.handler = None
    return sub


def test_proposals_are_cleaned_and_bounded():
    ap = ApprovalsConfig(max_actions_per_run=2, max_payload_bytes=256)
    ok = appr.clean_proposals([
        {"kind": "send_email", "summary": "Hi‮ there\x00", "payload": {"to": "a​@b.com", "n": [1, 2], "x": None}},
        {"kind": "Bad Kind!", "summary": "x", "payload": {}},                      # unusable kind
        {"kind": "ok", "summary": "x" * 900, "payload": {}},
        {"kind": "third", "summary": "past the per-run cap", "payload": {}},
    ], ap)
    assert [p["kind"] for p in ok] == ["send_email"]                              # 2nd rejected, 3rd past the cap
    assert ok[0]["summary"] == "Hi there" and ok[0]["payload"] == {"to": "a@b.com", "n": [1, 2], "x": None}
    assert appr.clean_proposals([{"kind": "k", "summary": "s", "payload": {"big": "y" * 500}}], ap) == []
    assert appr.clean_proposals("nope", ap) == [] and appr.clean_proposals([5, None, "x"], ap) == []
    odd = appr.clean_proposals([{"kind": "k", "summary": "s", "payload": {"o": object()}}], ap)
    assert odd[0]["payload"] == {"o": None}                                        # unknown types are dropped, not trusted


async def test_a_triggered_run_can_only_propose_and_the_owner_sees_it(make_app):
    client, app, backend, _ = await make_app()
    sub = await triggered_run_with(client, app, backend, [ACTION, {"kind": "???", "summary": "junk"}])
    runs = (await client.get(f"/v1/triggers/{sub['id']}/runs", headers=U("alice"))).json()
    assert runs[0]["status"] == "succeeded" and len(runs[0]["proposed_actions"]) == 1
    assert not any("send_email" in c for _, c in backend.execs)                   # nothing was sent
    pending = (await client.get("/v1/approvals?status=pending", headers=U("alice"))).json()
    assert len(pending) == 1 and pending[0]["kind"] == "send_email" and pending[0]["payload"]["to"] == "bob@partner.com"
    assert pending[0]["subscription_id"] == sub["id"] and pending[0]["status"] == "pending"
    assert (await client.get("/v1/approvals", headers=U("bob"))).json() == []
    assert (await client.get(f"/v1/approvals/{pending[0]['id']}", headers=U("bob"))).status_code == 404


async def test_approving_runs_the_action_in_the_interactive_session_with_the_users_own_token(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    before = len(backend.execs)
    r = await client.post(f"/v1/approvals/{aid}/approve", json={"obo_resources": ["graph"]}, headers=U("alice"))
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["result"]["reply"].startswith("echo:")

    interactive = {s["pool"] for s in backend.sandboxes.values() if s["pool"] == "agents"}
    run_cmd = [(sid, c) for sid, c in backend.execs[before:] if "exec runuser" in c][0]
    assert backend.sandboxes[run_cmd[0]]["pool"] == "agents" and interactive == {"agents"}     # not the triggers pool
    env = UserEnv(app.state.ctx["service"].cfg, uid_for(TENANT, "alice"))
    req = json.loads(backend.files[(run_cmd[0], env.request_path)])
    assert req["messages"][-1]["content"].startswith("A person approved the action")
    assert req["event"]["approved_action"] == {k: ACTION[k] for k in ("kind", "summary", "payload")}
    assert "data" in req["event"]["note"] and f"OBO_TOKENS_FILE={env.obo_path}" in run_cmd[1]
    token = [json.loads(v) for (s, p), v in backend.files.items() if p == env.obo_path and s == run_cmd[0]][0]["graph"]["access_token"]
    assert jwt_oid(token) == "alice"                                               # the live user's token
    assert (await client.get(f"/v1/approvals/{aid}", headers=U("alice"))).json()["status"] == "approved"


def jwt_oid(token):
    import jwt
    return jwt.decode(token, options={"verify_signature": False})["oid"]


async def test_an_approval_runs_once(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    agent_runs = lambda: sum("exec runuser" in c for _, c in backend.execs)
    before = agent_runs()
    assert (await client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("alice"))).status_code == 200
    assert agent_runs() == before + 1
    again = await client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("alice"))
    assert again.status_code == 409 and "Already approved" in again.json()["detail"]
    assert (await client.post(f"/v1/approvals/{aid}/reject", headers=U("alice"))).status_code == 409
    assert agent_runs() == before + 1                                               # the action ran exactly once


async def test_rejecting_runs_nothing(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    n = len(backend.execs)
    assert (await client.post(f"/v1/approvals/{aid}/reject", headers=U("alice"))).json()["status"] == "rejected"
    assert len(backend.execs) == n and (await client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("alice"))).status_code == 409


async def test_only_the_owner_can_decide(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    for call in (client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("bob")),
                 client.post(f"/v1/approvals/{aid}/reject", headers=U("bob"))):
        assert (await call).status_code == 404
    assert (await client.get(f"/v1/approvals/{aid}", headers=U("alice"))).json()["status"] == "pending"


async def test_proposals_expire_and_pending_ones_are_capped(make_app):
    client, app, backend, _ = await make_app(with_triggers(approvals={"max_pending_per_user": 2, "ttl_days": 1}))
    await triggered_run_with(client, app, backend, [ACTION, {**ACTION, "summary": "two"}, {**ACTION, "summary": "three"}])
    items = (await client.get("/v1/approvals", headers=U("alice"))).json()
    assert len(items) == 2                                                          # the third was dropped
    svc = app.state.ctx["approvals"]
    rec = await svc._load(items[0]["id"])
    rec["expires_ms"] = now_ms() - 1
    await svc._save(rec)
    r = await client.post(f"/v1/approvals/{items[0]['id']}/approve", json={}, headers=U("alice"))
    assert r.status_code == 409 and "expired" in r.json()["detail"]


async def test_a_failed_token_exchange_leaves_the_approval_pending(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    r = await client.post(f"/v1/approvals/{aid}/approve", json={"obo_resources": ["nope"]}, headers=U("alice"))
    assert r.status_code == 422
    assert (await client.get(f"/v1/approvals/{aid}", headers=U("alice"))).json()["status"] == "pending"


async def test_an_approved_action_that_fails_is_recorded_not_retried(make_app):
    client, app, backend, _ = await make_app()
    await triggered_run_with(client, app, backend, [ACTION])
    aid = (await client.get("/v1/approvals", headers=U("alice"))).json()[0]["id"]
    backend.handler = lambda sid, c: ExecOutcome(1, "", "boom") if "exec runuser" in c else None
    r = await client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("alice"))
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["error"]
    assert (await client.post(f"/v1/approvals/{aid}/approve", json={}, headers=U("alice"))).status_code == 409


async def test_interactive_turns_never_create_approvals(make_app):
    client, app, backend, _ = await make_app()
    backend.handler = proposing_handler([ACTION])
    r = await client.post("/v1/agent/invoke", json={"prompt": "hi"}, headers=U("alice"))
    assert r.status_code == 200
    assert (await client.get("/v1/approvals", headers=U("alice"))).json() == []


async def test_approvals_can_be_turned_off(make_app):
    client, app, backend, _ = await make_app(with_triggers(approvals={"enabled": False}))
    assert (await client.get("/v1/approvals", headers=U("alice"))).status_code == 404
    await triggered_run_with(client, app, backend, [ACTION])                       # proposals are ignored
    assert app.state.ctx["approvals"] is None
