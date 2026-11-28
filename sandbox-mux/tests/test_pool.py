import asyncio

import pytest

from sandbox_mux.pool import PoolBusy


async def test_users_are_packed_and_sticky(make_replica, backend):
    r = make_replica(users_per_sandbox=3, max_sandboxes=4)
    placed = {}
    for i in range(6):
        placed[f"u{i}"], _ = await r.pool.acquire(f"u{i}")
    # 6 users, 3 per sandbox -> exactly 2 sandboxes
    assert len(backend.sandboxes) == 2
    assert sorted((await r.store.loads()).values()) == [3, 3]
    # sticky: asking again returns the same sandbox and is not a new binding
    for uid, sid in placed.items():
        again, new = await r.pool.acquire(uid)
        assert again == sid and not new


async def test_concurrent_burst_creates_no_more_sandboxes_than_needed(make_replica, backend):
    r = make_replica(users_per_sandbox=5, max_sandboxes=10)
    results = await asyncio.gather(*(r.pool.acquire(f"u{i}") for i in range(20)))
    assert len({sid for sid, _ in results}) == 4          # 20 users / 5 per sandbox
    assert len(backend.sandboxes) == 4
    assert all(v <= 5 for v in (await r.store.loads()).values())


async def test_replicas_share_limits(make_replica, backend):
    replicas = [make_replica(users_per_sandbox=2, max_sandboxes=3) for _ in range(3)]
    jobs = [replicas[i % 3].pool.acquire(f"u{i}") for i in range(6)]
    results = await asyncio.gather(*jobs)
    assert len(backend.sandboxes) == 3
    assert backend.max_created <= 3
    loads = await replicas[0].store.loads()
    assert sum(loads.values()) == 6 and all(v == 2 for v in loads.values())
    assert len({sid for sid, _ in results}) == 3


async def test_capacity_exhausted_returns_busy_and_never_exceeds_caps(make_replica, backend):
    r = make_replica(users_per_sandbox=2, max_sandboxes=2)
    jobs = [r.pool.acquire(f"u{i}") for i in range(7)]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    ok = [x for x in results if not isinstance(x, Exception)]
    busy = [x for x in results if isinstance(x, PoolBusy)]
    assert len(ok) == 4 and len(busy) == 3
    assert len(backend.sandboxes) == 2
    assert all(v <= 2 for v in (await r.store.loads()).values())
    assert await r.store.waiting_count() == 0


async def test_failed_creation_releases_reservation(make_replica, backend):
    r = make_replica(users_per_sandbox=2, max_sandboxes=1)
    backend.fail_next_create = True
    sid, _ = await r.pool.acquire("a")       # first attempt fails, the retry succeeds
    assert sid in backend.sandboxes
    assert await r.store.creating_count() == 0


async def test_spread_strategy_balances(make_replica, backend):
    r = make_replica(users_per_sandbox=4, max_sandboxes=2, strategy="spread")
    await r.pool.acquire("a")                # creates sandbox 1
    r.cfg.pool.max_sandboxes = 2
    await r.store.register_sandbox("sbx-extra", "t")   # a second ready sandbox
    sid, _ = await r.pool.acquire("b")
    assert sid == "sbx-extra"
