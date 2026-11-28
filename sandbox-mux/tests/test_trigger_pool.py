"""Triggered runs execute in their own pool: other sandboxes, other home directories, and their
own egress, connectors and capacity."""

import json

import pytest

from sandbox_mux.config import Config
from sandbox_mux.auth import uid_for
from sandbox_mux.backends.base import ExecOutcome
from sandbox_mux.service import UserEnv, username
from sandbox_mux.store import now_ms

from test_triggers import (EMAIL, GRAPH, NS, TENANT, U, World, consent, make_app,  # noqa: F401
                           subscribe, tcfg)

MCP = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/connectorGateways/ns/mcpserverconfigs/mail"
HOST = "graph.microsoft.com"


def pooled(interactive=None, triggers=None, admins=None, **over) -> Config:
    """Interactive pool: allow-all egress and a connector. Triggers pool: whatever `triggers` says."""
    cfg = tcfg(**over).model_dump()
    cfg["pool"] = {"name": "agents", "max_sandboxes": 3, "users_per_sandbox": 3,
                   "egress": {"mode": "enabled"}, **(interactive or {})}
    cfg["connectors"] = {"attach": [MCP]}
    cfg["auth"]["admin_oids"] = admins or []
    cfg["triggers"]["pool"] = {"separate": True, "name": "triggers",
                               "settings": {"egress": {"mode": "allowlist", "allow_hosts": [HOST]}},
                               **(triggers or {})}
    return Config.model_validate(cfg)


async def interactive_turn(client, user="alice", prompt="hi"):
    # The interactive API needs a user token; the test auth stub accepts X-Dev-User.
    r = await client.post("/v1/agent/invoke", json={"prompt": prompt}, headers=U(user))
    assert r.status_code == 200, r.text
    return r.json()


async def trigger_turn(client, app, user="alice", event=None):
    await client.post("/v1/triggers/callback/mail", json=event or EMAIL, headers=NS)
    await app.state.ctx["triggers"].drain_once()


# ---- configuration ------------------------------------------------------------------------------

def test_the_triggers_pool_inherits_sizes_but_not_egress_or_connectors():
    cfg = pooled(interactive={"cpu": "2000m", "memory": "4096Mi", "disk_id": "img-1", "warmup_command": ["true"]})
    t = cfg.triggers_pool_cfg()
    assert t is not cfg and t.pool.name == "triggers" and cfg.pool.name == "agents"
    assert (t.pool.cpu, t.pool.memory, t.pool.disk_id, t.pool.warmup_command) == ("2000m", "4096Mi", "img-1", ["true"])
    assert t.pool.egress.mode == "allowlist" and t.pool.egress.allow_hosts == [HOST]
    assert cfg.pool.egress.mode == "enabled" and cfg.connectors.ids == [MCP]
    assert t.connectors.ids == []                          # the interactive connector is NOT inherited


def test_with_no_settings_the_triggers_pool_denies_all_egress_and_has_no_connectors():
    data = pooled().model_dump()
    data["triggers"]["pool"] = {"separate": True}
    data["triggers"]["delegation"] = {"enabled": False}
    data["obo"] = {"enabled": False}
    t = Config.model_validate(data).triggers_pool_cfg()
    assert t.pool.egress.mode == "disabled" and t.connectors.ids == [] and t.pool.name == "triggers"


def test_overrides_for_size_limits_and_connectors():
    cfg = pooled(interactive={"cpu": "4", "memory": "8Gi"},
                 triggers={"settings": {"max_sandboxes": 1, "users_per_sandbox": 2, "memory": "2Gi", "cpu": "1",
                                        "egress": {"mode": "allowlist", "allow_hosts": [HOST]}},
                           "limits": {"mode": "cgroup", "cpu": 0.5, "memory": "512Mi", "pids": 64},
                           "connectors": {"attach": [MCP]}})
    t = cfg.triggers_pool_cfg()
    assert (t.pool.max_sandboxes, t.pool.users_per_sandbox, t.pool.memory) == (1, 2, "2Gi")
    assert (t.limits.mode, t.limits.pids) == ("cgroup", 64) and cfg.limits.mode == "none"
    assert t.connectors.ids == [MCP]


def test_the_triggers_pool_cannot_share_a_name_with_the_interactive_pool():
    with pytest.raises(ValueError, match="must differ"):
        pooled(triggers={"name": "agents"})


def test_not_separate_means_the_same_pool():
    cfg = pooled(triggers={"separate": False})
    assert cfg.triggers_pool_cfg() is cfg


def test_bad_triggers_pool_settings_fail_at_startup_with_a_clear_prefix():
    with pytest.raises(ValueError, match="triggers.pool"):
        pooled(triggers={"settings": {"max_sandboxes": 0}})
    with pytest.raises(ValueError, match="triggers.pool"):
        pooled(triggers={"settings": {"egress": {"mode": "allowlist"}}})            # allowlist without hosts
    with pytest.raises(ValueError, match="triggers.pool"):
        pooled(triggers={"settings": {"min_sandboxes": 9, "max_sandboxes": 2}})


def test_delegation_needs_the_triggers_pool_to_reach_the_api():
    with pytest.raises(ValueError, match=r"triggers.pool.*pointless|triggers.pool.settings.egress"):
        pooled(triggers={"settings": {}})                                          # deny-all + delegation
    # without delegation, deny-all is fine: no tokens go to that pool
    data = pooled().model_dump()
    data["triggers"]["pool"] = {"separate": True}
    data["triggers"]["delegation"] = {"enabled": False}
    Config.model_validate(data)


# ---- behaviour -------------------------------------------------------------------------------------

async def test_triggered_and_interactive_runs_use_different_sandboxes_and_homes(make_app):
    client, app, backend, world = await make_app(pooled())
    await subscribe(client)
    a = await interactive_turn(client)
    await trigger_turn(client, app)
    uid = uid_for(TENANT, "alice")
    sandboxes = backend.sandboxes
    assert {s["pool"] for s in sandboxes.values()} == {"agents", "triggers"}
    inter = [sid for sid, s in sandboxes.items() if s["pool"] == "agents"]
    trig = [sid for sid, s in sandboxes.items() if s["pool"] == "triggers"]
    assert inter == [a["sandbox_id"]] and len(trig) == 1 and inter != trig
    env = UserEnv(app.state.ctx["trigger_service"].cfg, uid)
    provisioned = [sid for sid, c in backend.execs if "useradd" in c and env.user in c]
    assert sorted(provisioned) == sorted(inter + trig)                     # the account exists on both, separately
    # the event and the instruction ran in the triggers pool's sandbox only
    for (sid, path), data in backend.files.items():
        if path == env.request_path and sid in trig:
            assert json.loads(data)["event"]["fields"]["subject"] == "Invoice 42"
        if sid in inter:
            assert b"Invoice 42" not in data


async def test_each_pool_is_created_with_its_own_egress_and_connectors(make_app):
    client, app, backend, _ = await make_app(pooled())
    await subscribe(client)
    await interactive_turn(client)
    await trigger_turn(client, app)
    by_pool = {s["pool"]: s for s in backend.sandboxes.values()}
    assert by_pool["agents"]["egress_mode"] == "enabled" and by_pool["agents"]["connections"] == [MCP]
    assert by_pool["triggers"]["egress_mode"] == "allowlist" and by_pool["triggers"]["connections"] == []
    assert by_pool["agents"]["labels"]["pool"] == "agents" and by_pool["triggers"]["labels"]["pool"] == "triggers"


async def test_the_pools_have_separate_capacity(make_app):
    cfg = pooled(interactive={"max_sandboxes": 1, "users_per_sandbox": 1},
                 triggers={"settings": {"max_sandboxes": 1, "users_per_sandbox": 1,
                                        "egress": {"mode": "allowlist", "allow_hosts": [HOST]}}})
    client, app, backend, _ = await make_app(cfg)
    await subscribe(client, "alice")
    await interactive_turn(client, "bob")                       # the interactive pool is now full
    full = await client.post("/v1/agent/invoke", json={"prompt": "x"}, headers=U("carol"))
    assert full.status_code == 429
    await trigger_turn(client, app)                             # triggered work is unaffected
    runs = (await client.get(f"/v1/triggers/{(await client.get('/v1/triggers', headers=U('alice'))).json()[0]['id']}/runs",
                             headers=U("alice"))).json()
    assert runs[0]["status"] == "succeeded"
    # and a flood of triggered runs cannot take interactive slots
    for i in range(3):
        await client.post("/v1/triggers/callback/mail", json={**EMAIL, "id": f"f{i}"}, headers=NS)
    await app.state.ctx["triggers"].drain_once()
    assert len([s for s in backend.sandboxes.values() if s["pool"] == "agents"]) == 1
    assert len([s for s in backend.sandboxes.values() if s["pool"] == "triggers"]) == 1


async def test_a_running_interactive_turn_does_not_block_a_triggered_run(make_app):
    client, app, backend, _ = await make_app(pooled())
    await subscribe(client)
    store = app.state.ctx["store"]
    lock = await store.acquire_lock(f"lock:user:{uid_for(TENANT, 'alice')}", 60)   # alice is mid-conversation
    await trigger_turn(client, app)
    run = (await client.get(f"/v1/triggers/{(await client.get('/v1/triggers', headers=U('alice'))).json()[0]['id']}/runs",
                            headers=U("alice"))).json()[0]
    assert run["status"] == "succeeded"
    await store.release_lock(f"lock:user:{uid_for(TENANT, 'alice')}", lock)


async def test_deleting_a_session_removes_the_user_from_both_pools(make_app):
    client, app, backend, _ = await make_app(pooled())
    await subscribe(client)
    await interactive_turn(client)
    await trigger_turn(client, app)
    uid = uid_for(TENANT, "alice")
    assert (await client.delete("/v1/me/session", headers=U("alice"))).status_code == 204
    wipes = [sid for sid, c in backend.execs if "userdel" in c and username(uid) in c]
    assert sorted(wipes) == sorted(backend.sandboxes)                           # wiped on both
    for store in (app.state.ctx["store"], app.state.ctx["trigger_store"]):
        assert (await store.binding(uid)).get("sandbox") is None


async def test_idle_eviction_runs_per_pool_and_only_wipes_that_pool(make_app):
    client, app, backend, _ = await make_app(pooled())
    await subscribe(client)
    a = await interactive_turn(client)
    await trigger_turn(client, app)
    uid = uid_for(TENANT, "alice")
    tstore, treaper = app.state.ctx["trigger_store"], app.state.ctx["trigger_reaper"]
    await tstore.r.zadd(tstore.k("useractive"), {uid: now_ms() - 10**7})
    before = len(backend.execs)
    await treaper.evict_idle_users()
    wiped = [sid for sid, c in backend.execs[before:] if "userdel" in c]
    assert wiped and a["sandbox_id"] not in wiped                               # the interactive home is untouched
    assert (await app.state.ctx["store"].binding(uid)).get("sandbox") == a["sandbox_id"]


async def test_changing_the_triggers_egress_updates_only_triggers_sandboxes(make_app):
    client, app, backend, _ = await make_app(pooled())
    await subscribe(client)
    a = await interactive_turn(client)
    await trigger_turn(client, app)
    t_cfg = app.state.ctx["trigger_service"].cfg
    from sandbox_mux.config import EgressConfig
    t_cfg.pool.egress = EgressConfig(mode="disabled")           # the operator tightens it
    await app.state.ctx["trigger_reaper"].reconcile_egress()
    await app.state.ctx["reaper"].reconcile_egress()           # the interactive pool is already current
    trig = [sid for sid, s in backend.sandboxes.items() if s["pool"] == "triggers"]
    assert backend.egress_applied == trig
    assert backend.sandboxes[trig[0]]["egress_mode"] == "disabled"
    assert backend.sandboxes[a["sandbox_id"]]["egress_mode"] == "enabled"


async def test_not_separate_runs_triggered_work_in_the_interactive_pool(make_app):
    client, app, backend, _ = await make_app(pooled(triggers={"separate": False}))
    await subscribe(client)
    a = await interactive_turn(client)
    await trigger_turn(client, app)
    assert {s["pool"] for s in backend.sandboxes.values()} == {"agents"}
    assert app.state.ctx["trigger_service"] is app.state.ctx["service"] and app.state.ctx["trigger_reaper"] is None


async def test_a_delegated_run_gets_its_token_in_the_triggers_pool_only(make_app):
    client, app, backend, world = await make_app(pooled())
    await consent(client, world)
    await subscribe(client, delegated_resources=["graph"])
    a = await interactive_turn(client)
    await trigger_turn(client, app)
    trig = next(sid for sid, s in backend.sandboxes.items() if s["pool"] == "triggers")
    env = UserEnv(app.state.ctx["trigger_service"].cfg, uid_for(TENANT, "alice"))
    assert (trig, env.obo_path) in backend.files and (a["sandbox_id"], env.obo_path) not in backend.files


async def test_pool_status_shows_both_pools(make_app):
    client, app, backend, _ = await make_app(pooled(admins=["alice"]))
    await subscribe(client)
    await interactive_turn(client)
    await trigger_turn(client, app)
    s = (await client.get("/v1/pool/status", headers=U("alice"))).json()
    assert s["pool"] == "agents" and s["egress"] == "enabled" and s["connectors"] == 1
    t = s["triggers_pool"]
    assert t["pool"] == "triggers" and t["egress"] == "allowlist" and t["connectors"] == 0
    assert len(s["loads"]) == 1 and len(t["loads"]) == 1 and set(s["loads"]) != set(t["loads"])
    assert (await client.get("/v1/pool/status", headers=U("bob"))).status_code == 403


def test_each_pool_has_its_own_redis_namespace(make_app=None):
    from sandbox_mux.store import Store
    import fakeredis.aioredis as fr
    r = fr.FakeRedis()
    a, b = Store(r, "smux", "agents"), Store(r, "smux", "triggers")
    assert a.k("load") != b.k("load") and a.k("lock:user:x") != b.k("lock:user:x")
