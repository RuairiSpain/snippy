"""In-memory backend for tests and local development. Runs no real code."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from typing import Callable

from .base import ExecOutcome, SandboxGone, SandboxInfo

Handler = Callable[[str, str], ExecOutcome | None]


class FakeSandboxBackend:
    """Records every call. exec() answers agent turns with an echo reply.

    `handler(sandbox_id, command)` may return an ExecOutcome to override the default.
    """

    def __init__(self, create_delay: float = 0.0, handler: Handler | None = None) -> None:
        self.sandboxes: dict[str, dict] = {}
        self.execs: list[tuple[str, str]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.modes: dict[tuple[str, str], str | None] = {}
        self.create_delay = create_delay
        self.fail_next_create = False
        self.egress_applied: list[str] = []
        self.fail_set_egress = False
        self.handler = handler
        self._ids = itertools.count(1)
        self.max_created = 0
        self.cfg = None            # set on a view made by with_config()

    def with_config(self, cfg) -> "_FakeView":
        return _FakeView(self, cfg)

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def create(self, labels: dict[str, str]) -> str:
        return await self._create(labels, self.cfg)

    async def _create(self, labels: dict[str, str], cfg) -> str:
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError("simulated create failure")
        sid = f"sbx-{next(self._ids)}"
        self.sandboxes[sid] = {
            "state": "Running", "labels": dict(labels), "created": time.time(),
            # what the pool that created it asked for, so tests can tell the pools apart
            "pool": cfg.pool.name if cfg else None,
            "egress_mode": cfg.pool.egress.mode if cfg else None,
            "egress_fp": cfg.pool.egress.fingerprint() if cfg else None,
            "connections": cfg.connectors.ids if cfg else None,
            "limits": cfg.limits.mode if cfg else None}
        self.max_created = max(self.max_created, len(self.sandboxes))
        return sid

    def _need(self, sid: str) -> dict:
        if sid not in self.sandboxes:
            raise SandboxGone(sid)
        return self.sandboxes[sid]

    async def ensure_running(self, sandbox_id: str) -> None:
        self._need(sandbox_id)["state"] = "Running"

    async def exec(self, sandbox_id: str, command: str) -> ExecOutcome:
        self._need(sandbox_id)
        self.execs.append((sandbox_id, command))
        if self.handler and (r := self.handler(sandbox_id, command)):
            return r
        if "exec runuser" in command or ("turn.json" in command and "timeout" in command):
            m = re.search(r"(/\S*turn\.json)", command)
            req = {}
            if m and (sandbox_id, m.group(1).strip("'")) in self.files:
                req = json.loads(self.files[(sandbox_id, m.group(1).strip("'"))])
            last = (req.get("messages") or [{}])[-1].get("content", "")
            return ExecOutcome(0, json.dumps({"reply": f"echo: {last}", "input_tokens": 1,
                                              "output_tokens": 1}), "")
        return ExecOutcome(0, "", "")

    async def write_file(self, sandbox_id: str, path: str, data: bytes,
                         mode: str | None = None) -> None:
        self._need(sandbox_id)
        self.files[(sandbox_id, path)] = data
        self.modes[(sandbox_id, path)] = mode

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        self._need(sandbox_id)
        return self.files.get((sandbox_id, path), b"")

    async def set_egress(self, sandbox_id: str) -> None:
        await self._set_egress(sandbox_id, self.cfg)

    async def _set_egress(self, sandbox_id: str, cfg) -> None:
        self._need(sandbox_id)
        if self.fail_set_egress:
            raise RuntimeError("simulated egress failure")
        self.egress_applied.append(sandbox_id)
        if cfg:
            self.sandboxes[sandbox_id]["egress_fp"] = cfg.pool.egress.fingerprint()
            self.sandboxes[sandbox_id]["egress_mode"] = cfg.pool.egress.mode

    async def delete(self, sandbox_id: str) -> None:
        self.sandboxes.pop(sandbox_id, None)

    async def list(self, labels: dict[str, str]) -> list[SandboxInfo]:
        return [SandboxInfo(i, s["state"], s["created"]) for i, s in self.sandboxes.items()
                if all(s["labels"].get(k) == v for k, v in labels.items())]


class _FakeView:
    """The fake backend as seen by one pool: it creates sandboxes with that pool's settings and
    forwards everything else to the shared backend."""

    def __init__(self, parent: FakeSandboxBackend, cfg) -> None:
        self._p, self.cfg = parent, cfg

    def with_config(self, cfg) -> "_FakeView":
        return _FakeView(self._p, cfg)

    async def create(self, labels: dict[str, str]) -> str:
        return await self._p._create(labels, self.cfg)

    async def set_egress(self, sandbox_id: str) -> None:
        await self._p._set_egress(sandbox_id, self.cfg)

    def __getattr__(self, name):
        return getattr(self._p, name)
