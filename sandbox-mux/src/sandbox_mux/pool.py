"""Places users onto sandboxes and creates sandboxes when the pool needs to grow."""

from __future__ import annotations

import asyncio
import logging
import random
import shlex
import time

from .backends import SandboxBackend
from .config import Config
from .limits import bootstrap_check
from .store import Store
from .telemetry import tracer

log = logging.getLogger(__name__)


class PoolBusy(Exception):
    """No capacity became available within session.queue_timeout_seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("pool is at capacity")
        self.retry_after = retry_after


# Run once in every new sandbox. Fails fast if the exec user is not root, because
# per-user accounts cannot be created or enforced otherwise.
BOOTSTRAP = r"""
set -eu
[ "$(id -u)" = 0 ] || { echo "exec user is not root" >&2; exit 3; }
mkdir -p {home_root}
chmod 711 {home_root}
# Best effort: hide other users' processes from each other.
mount -o remount,hidepid=2 /proc 2>/dev/null || true
{limits_check}"""


class PoolManager:
    def __init__(self, cfg: Config, store: Store, backend: SandboxBackend) -> None:
        self.cfg, self.store, self.backend = cfg, store, backend
        self._labels = {"managed-by": "sandbox-mux", "pool": cfg.pool.name}

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._labels)

    async def acquire(self, uid: str) -> tuple[str, bool]:
        """Return (sandbox_id, is_new_binding). Waits for capacity up to the queue timeout."""
        p, s = self.cfg.pool, self.cfg.session
        deadline = time.monotonic() + s.queue_timeout_seconds
        delay = 0.15
        try:
            while True:
                a = await self.store.assign(
                    uid, cap=p.users_per_sandbox, max_sandboxes=p.max_sandboxes,
                    strategy=p.strategy,
                    reservation_ttl_s=p.create_timeout_seconds + 120,
                    waiting_ttl_s=s.queue_timeout_seconds + 5,
                )
                if a.kind == "bound":
                    assert a.sandbox_id
                    return a.sandbox_id, a.is_new_binding
                if time.monotonic() >= deadline:
                    if a.kind == "create":
                        assert a.token
                        await self.store.abort_creation(a.token)
                    raise PoolBusy(retry_after=5)
                if a.kind == "create":
                    assert a.token
                    await self._create(a.token)
                    continue  # re-run assignment; the new sandbox is now registered
                await asyncio.sleep(delay + random.random() * delay)
                delay = min(delay * 1.5, 1.0)
        finally:
            await self.store.stop_waiting(uid)

    async def _create(self, token: str) -> None:
        with tracer.start_as_current_span("pool.create_sandbox"):
            sid: str | None = None
            try:
                sid = await asyncio.wait_for(self.backend.create(self.labels),
                                             self.cfg.pool.create_timeout_seconds + 30)
                out = await self.backend.exec(
                    sid, BOOTSTRAP.replace("{home_root}", shlex.quote(self.cfg.isolation.home_root))
                    .replace("{limits_check}", bootstrap_check(self.cfg)))
                if out.exit_code != 0:
                    raise RuntimeError(f"sandbox bootstrap failed: {out.stderr.strip()[:300]}")
                await self.store.register_sandbox(sid, token, self.cfg.pool.egress.fingerprint())
                log.info("sandbox %s registered", sid)
            except BaseException:
                await self.store.abort_creation(token)
                if sid:
                    try:
                        await self.backend.delete(sid)
                    except Exception:  # the reaper deletes orphans later
                        log.exception("cleanup of %s failed", sid)
                log.exception("sandbox creation failed")
                # Let the caller's loop retry after a pause instead of hammering the API.
                await asyncio.sleep(1.0)

    async def warm(self) -> None:
        """Create sandboxes until `min_sandboxes` exist (called by the reaper leader)."""
        p = self.cfg.pool
        while True:
            states = await self.store.states()
            have = sum(1 for v in states.values() if v == "ready") + await self.store.creating_count()
            if have >= p.min_sandboxes:
                return
            token = await self.store.reserve_creation(
                max_sandboxes=p.max_sandboxes, reservation_ttl_s=p.create_timeout_seconds + 120)
            if not token:
                return
            await self._create(token)
