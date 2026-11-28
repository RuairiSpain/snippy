import asyncio
import json
import time

import pytest

from sandbox_mux.service import AgentError, TurnInProgress, UserEnv, username
from sandbox_mux.store import now_ms


async def test_turn_runs_as_the_users_own_account_and_home(make_replica, backend):
    r = make_replica()
    res = await r.service.run_turn("a" * 32, "c1", "hello")
    assert res.reply == "echo: hello"
    env = UserEnv(r.cfg, "a" * 32)
    cmds = [c for _, c in backend.execs]
    assert any("useradd" in c and env.user in c for c in cmds)          # provisioned
    run = next(c for c in cmds if "runuser" in c)
    assert f"runuser -u {env.user}" in run and f"HOME={env.home}" in run
    assert "env -i" in run                                               # clean environment
    assert (res.sandbox_id, env.request_path) in backend.files


async def test_provisioning_happens_once_per_binding(make_replica, backend):
    r = make_replica()
    await r.service.run_turn("b" * 32, "c", "one")
    await r.service.run_turn("b" * 32, "c", "two")
    assert sum("useradd" in c for _, c in backend.execs) == 1


async def test_two_users_on_one_sandbox_get_separate_homes(make_replica, backend):
    r = make_replica(users_per_sandbox=5)
    a = await r.service.run_turn("a" * 32, "c", "x")
    b = await r.service.run_turn("b" * 32, "c", "y")
    assert a.sandbox_id == b.sandbox_id
    ea, eb = UserEnv(r.cfg, "a" * 32), UserEnv(r.cfg, "b" * 32)
    assert ea.home != eb.home and ea.user != eb.user
    runs = [c for _, c in backend.execs if "runuser" in c]
    assert f"HOME={ea.home}" in runs[0] and f"HOME={eb.home}" in runs[1]
    assert ea.home not in runs[1] and eb.home not in runs[0]


async def test_history_is_kept_per_conversation_and_user(make_replica):
    r = make_replica()
    await r.service.run_turn("a" * 32, "c1", "one")
    await r.service.run_turn("a" * 32, "c1", "two")
    await r.service.run_turn("a" * 32, "c2", "other")
    assert [m["content"] for m in await r.store.history("a" * 32, "c1", 10)] == [
        "one", "echo: one", "two", "echo: two"]
    assert len(await r.store.history("a" * 32, "c2", 10)) == 2
    assert await r.store.history("b" * 32, "c1", 10) == []


async def test_concurrent_turn_for_same_user_is_rejected(make_replica, backend):
    r = make_replica()
    gate = asyncio.Event()
    orig = backend.exec

    async def slow(sid, cmd):
        if "runuser" in cmd:
            await gate.wait()
        return await orig(sid, cmd)

    backend.exec = slow
    first = asyncio.create_task(r.service.run_turn("a" * 32, "c", "x"))
    await asyncio.sleep(0.3)
    with pytest.raises(TurnInProgress):
        await r.service.run_turn("a" * 32, "c", "y")
    gate.set()
    await first


async def test_sandbox_deleted_underneath_us_is_replaced(make_replica, backend):
    r = make_replica()
    first = await r.service.run_turn("a" * 32, "c", "one")
    del backend.sandboxes[first.sandbox_id]
    second = await r.service.run_turn("a" * 32, "c", "two")
    assert second.sandbox_id != first.sandbox_id and second.reply == "echo: two"


async def test_malformed_agent_output_is_an_agent_error(make_replica, backend):
    from sandbox_mux.backends.base import ExecOutcome
    backend.handler = lambda sid, c: ExecOutcome(0, "not json", "") if "runuser" in c else None
    r = make_replica()
    with pytest.raises(AgentError):
        await r.service.run_turn("a" * 32, "c", "x")


async def test_idle_user_is_evicted_and_wiped_then_slot_is_reusable(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=1)
    res = await r.service.run_turn("a" * 32, "c", "x")
    await r.redis.zadd(r.store.k("useractive"), {"a" * 32: now_ms() - 10**7})
    await r.reaper.evict_idle_users()
    wipe = [c for _, c in backend.execs if "userdel" in c or "rm -rf" in c]
    assert wipe and username("a" * 32) in wipe[-1]
    assert (await r.store.loads())[res.sandbox_id] == 0
    other = await r.service.run_turn("b" * 32, "c", "hi")      # reuses the freed slot
    assert other.sandbox_id == res.sandbox_id


async def test_empty_sandbox_is_deleted_after_ttl_but_min_is_kept(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=3, min_sandboxes=1, empty_ttl_seconds=0)
    a = await r.service.run_turn("a" * 32, "c", "x")
    b = await r.service.run_turn("b" * 32, "c", "x")
    assert a.sandbox_id != b.sandbox_id
    for u in ("a", "b"):
        await r.service.end_session(u * 32)
    await asyncio.sleep(0.01)
    await r.reaper.drain_empty_sandboxes()
    assert len(backend.sandboxes) == 1                           # min_sandboxes survives


async def test_warm_pool_and_orphan_reconciliation(make_replica, backend):
    r = make_replica(min_sandboxes=2, max_sandboxes=3)
    await r.pool.warm()
    assert len(backend.sandboxes) == 2
    # A sandbox created by a crashed replica (never registered) is removed once old enough.
    orphan = await backend.create(r.pool.labels)
    backend.sandboxes[orphan]["created"] = time.time() - 10_000
    await r.reaper.reconcile()
    assert orphan not in backend.sandboxes
    # A registered sandbox that vanished is forgotten.
    gone = next(iter(await r.store.states()))
    del backend.sandboxes[gone]
    await r.reaper.reconcile()
    assert gone not in await r.store.states()


async def test_only_one_replica_leads(make_replica):
    a, b = make_replica(), make_replica()
    assert await a.reaper._lead() is True
    assert await b.reaper._lead() is False
    assert await a.reaper._lead() is True                        # renews
