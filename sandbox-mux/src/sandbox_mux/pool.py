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
install -d -o root -g root -m 0755 {state_root} {state_root}/req {state_root}/in
install -d -o root -g root -m 0700 {state_root}/out
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

    async def _create(self, token: str) -> bool:
        """Create, set up and register one sandbox. Returns False if it failed (the failure is
        logged and the reservation released)."""
        with tracer.start_as_current_span("pool.create_sandbox"):
            sid: str | None = None
            try:
                sid = await asyncio.wait_for(self.backend.create(self.labels),
                                             self.cfg.pool.create_timeout_seconds + 30)
                out = await self.backend.exec(
                    sid, BOOTSTRAP.replace("{home_root}", shlex.quote(self.cfg.isolation.home_root))
                    .replace("{state_root}", shlex.quote(self.cfg.isolation.state_root.rstrip("/")))
                    .replace("{limits_check}", bootstrap_check(self.cfg)))
                if out.exit_code != 0:
                    raise RuntimeError(f"sandbox bootstrap failed: {out.stderr.strip()[:300]}")
                await self._warm_up(sid)
                await self.store.register_sandbox(sid, token, self.cfg.pool.egress.fingerprint(),
                                                  self.cfg.connectors.fingerprint())
                log.info("sandbox %s registered", sid)
                return True
            except BaseException as e:
                await asyncio.shield(self._abandon(sid, token))
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                log.exception("sandbox creation failed")
                # Let the caller's loop retry after a pause instead of hammering the API.
                await asyncio.sleep(1.0)
                return False

    async def _abandon(self, sid: str | None, token: str) -> None:
        await self.store.abort_creation(token)
        if sid:
            try:
                await self.backend.delete(sid)
            except Exception:  # the reaper deletes orphans later
                log.exception("cleanup of %s failed", sid)

    async def _warm_up(self, sid: str) -> None:
        """Run pool.warmup_command once, so the first user does not pay for cold imports."""
        p = self.cfg.pool
        if not p.warmup_command:
            return
        argv = " ".join(shlex.quote(a) for a in p.warmup_command)
        t = p.warmup_timeout_seconds
        out = await asyncio.wait_for(
            self.backend.exec(sid, f"set -eu\nexec timeout -k 5 {t} {argv}\n"), t + 30)
        if out.exit_code != 0:
            raise RuntimeError(f"warmup command failed (exit {out.exit_code}): "
                               f"{(out.stderr or out.stdout).strip()[-300:]}")

    async def free_slots(self) -> tuple[int, int]:
        """(ready sandboxes plus those being created, free user slots among them)."""
        cap = self.cfg.pool.users_per_sandbox
        states, loads = await self.store.states(), await self.store.loads()
        ready = [s for s, v in states.items() if v == "ready"]
        creating = await self.store.creating_count()
        free = sum(max(0, cap - loads.get(s, 0)) for s in ready) + creating * cap
        return len(ready) + creating, free

    async def warm(self) -> None:
        """Keep the pool warm (called by the reaper leader): at least `min_sandboxes` sandboxes
        and at least `min_free_slots` free user slots, within `max_sandboxes`. Stops at the
        first failure so a broken image or quota cannot keep this loop spinning."""
        p = self.cfg.pool
        for _ in range(p.max_sandboxes):
            have, free = await self.free_slots()
            if have >= p.min_sandboxes and free >= p.min_free_slots:
                return
            token = await self.store.reserve_creation(
                max_sandboxes=p.max_sandboxes, reservation_ttl_s=p.create_timeout_seconds + 120)
            if not token or not await self._create(token):
                return
