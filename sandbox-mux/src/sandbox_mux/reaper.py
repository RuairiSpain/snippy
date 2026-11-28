"""Background maintenance. Every replica runs the loop; one replica at a time holds the
leader lock and does the work. All operations are also safe if two run at once, because
the state changes are atomic in Redis. The lock only avoids duplicated effort."""

from __future__ import annotations

import asyncio
import logging
import time

from .backends import SandboxBackend, SandboxGone
from .config import Config
from .pool import PoolManager
from .service import SessionService, TurnInProgress
from .store import Store, now_ms

log = logging.getLogger(__name__)


class Reaper:
    def __init__(self, cfg: Config, store: Store, backend: SandboxBackend,
                 pool: PoolManager, service: SessionService, interval_s: float = 30.0,
                 files=None) -> None:
        self.cfg, self.store, self.backend = cfg, store, backend
        self.pool, self.service, self.interval = pool, service, interval_s
        self.files = files
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
        await self.reconcile_egress()
        await self.retire_stale_connections()
        await self.clean_up_retiring()
        if self.files:
            n = await self.files.purge_expired()
            if n:
                log.info("purged %d expired files", n)
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

    async def retire_stale_connections(self) -> None:
        """A sandbox's connectors are fixed when it is created and cannot be edited, so a
        sandbox created under a different connector list is retired: no new users are placed
        on it, and it is deleted once empty (or after connectors.retire_grace_seconds).
        New sandboxes, including the warm pool, then use the current list."""
        want = self.cfg.connectors.fingerprint()
        for sid, st in (await self.store.states()).items():
            if st != "ready":
                continue
            if (await self.store.sandbox_meta(sid)).get("connections", "") != want:
                if await self.store.retire_sandbox(sid):
                    log.info("sandbox %s retired: created with a different connector list", sid)

    async def clean_up_retiring(self) -> None:
        grace_ms = self.cfg.connectors.retire_grace_seconds * 1000
        loads = await self.store.loads()
        for sid, st in (await self.store.states()).items():
            if st != "retiring":
                continue
            expired = now_ms() - await self.store.retiring_since_ms(sid) >= grace_ms
            if loads.get(sid, 0) != 0 and not expired:
                continue
            # Forced removal after the grace period: users still on it are placed on a new
            # sandbox at their next request and start with an empty home.
            if await self.store.try_drain(sid, force=expired):
                try:
                    await self.backend.delete(sid)
                except Exception:
                    log.exception("delete of retired sandbox %s failed; will retry", sid)
                    continue
                await self.store.forget_sandbox(sid)
                log.info("retired sandbox %s deleted%s", sid, " (grace period over)" if expired else "")

    async def reconcile_egress(self) -> None:
        """Apply the configured egress policy to sandboxes created under a different one.

        Without this, switching egress from enabled to disabled would leave every existing
        sandbox open until it was replaced. A failure is retried on the next sweep."""
        want = self.cfg.pool.egress.fingerprint()
        for sid, st in (await self.store.states()).items():
            if st != "ready":
                continue
            meta = await self.store.sandbox_meta(sid)
            if meta.get("egress") == want:
                continue
            try:
                await self.backend.set_egress(sid)
            except SandboxGone:
                continue  # reconcile() forgets it
            except Exception:
                log.exception("could not apply egress policy to %s; will retry", sid)
                continue
            await self.store.set_sandbox_meta(sid, {**meta, "egress": want})
            log.info("egress policy %s applied to %s", want, sid)

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
