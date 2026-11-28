"""Background maintenance. Every replica runs the loop; one replica at a time holds the
leader lock and does the work. All operations are also safe if two run at once, because
the state changes are atomic in Redis. The lock only avoids duplicated effort."""

from __future__ import annotations

import asyncio
import logging
import time

from .backends import SandboxBackend
from .config import Config
from .pool import PoolManager
from .service import SessionService, TurnInProgress
from .store import Store, now_ms

log = logging.getLogger(__name__)


class Reaper:
    def __init__(self, cfg: Config, store: Store, backend: SandboxBackend,
                 pool: PoolManager, service: SessionService, interval_s: float = 30.0) -> None:
        self.cfg, self.store, self.backend = cfg, store, backend
        self.pool, self.service, self.interval = pool, service, interval_s
        self._token: str | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                if await self._lead():
                    await self.sweep()
            except Exception:
                log.exception("reaper sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except asyncio.TimeoutError:
                pass
        if self._token:
            await self.store.release_lock("leader", self._token)

    async def _lead(self) -> bool:
        ttl = self.interval * 3
        if self._token and await self.store.renew_lock("leader", self._token, ttl):
            return True
        self._token = await self.store.acquire_lock("leader", ttl)
        return self._token is not None

    async def sweep(self) -> None:
        await self.evict_idle_users()
        await self.drain_empty_sandboxes()
        await self.reconcile()
        await self.pool.warm()

    async def evict_idle_users(self) -> None:
        cutoff = now_ms() - self.cfg.session.user_idle_ttl_seconds * 1000
        for uid in await self.store.idle_users(cutoff):
            try:
                if not await self.service.end_session(uid):
                    # No binding (or wipe failed). Without a binding there is nothing to
                    # retry, so drop the stale activity entry.
                    if not (await self.store.binding(uid)).get("sandbox"):
                        await self.store.r.zrem(self.store.k("useractive"), uid)
            except TurnInProgress:
                continue
            except Exception:
                log.exception("evicting %s failed", uid[:8])

    async def drain_empty_sandboxes(self) -> None:
        p = self.cfg.pool
        states = await self.store.states()
        loads = await self.store.loads()
        ready = [s for s, v in states.items() if v == "ready"]
        spare = len(ready) - p.min_sandboxes
        if spare <= 0:
            return
        cutoff = now_ms() - p.empty_ttl_seconds * 1000
        for sid in sorted(ready, key=lambda s: loads.get(s, 0)):
            if spare <= 0:
                break
            if loads.get(sid, 0) != 0 or await self.store.sandbox_last_active_ms(sid) > cutoff:
                continue
            if await self.store.try_drain(sid):
                try:
                    await self.backend.delete(sid)
                except Exception:
                    log.exception("delete of %s failed; will retry", sid)
                    continue
                await self.store.forget_sandbox(sid)
                spare -= 1
                log.info("drained empty sandbox %s", sid)

        # Retry sandboxes stuck in 'draining' after a failed delete.
        for sid, st in (await self.store.states()).items():
            if st == "draining":
                try:
                    await self.backend.delete(sid)
                    await self.store.forget_sandbox(sid)
                except Exception:
                    log.exception("retry delete of %s failed", sid)

    async def reconcile(self) -> None:
        """Compare Redis with what actually exists in the sandbox group."""
        actual = {i.id: i for i in await self.backend.list(self.pool.labels)}
        known = await self.store.states()
        for sid in known:
            if sid not in actual:
                log.warning("sandbox %s is registered but no longer exists; forgetting it", sid)
                await self.store.forget_sandbox(sid)
        grace = self.cfg.pool.create_timeout_seconds + 180
        for sid, info in actual.items():
            if sid in known or info.created_at is None:
                continue
            # Created by a replica that died before registering it. Only delete when it is
            # older than any live reservation could be.
            if time.time() - info.created_at > grace:
                log.warning("deleting orphan sandbox %s", sid)
                try:
                    await self.backend.delete(sid)
                except Exception:
                    log.exception("orphan delete of %s failed", sid)
