"""Runs one agent turn for one user on whichever sandbox the user is bound to."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass

from .backends import SandboxBackend, SandboxGone
from .config import Config
from .pool import PoolBusy, PoolManager
from .store import Store
from . import telemetry as tm

log = logging.getLogger(__name__)


class TurnInProgress(Exception):
    """The same user already has a turn running."""


class AgentError(Exception):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass
class TurnResult:
    reply: str
    sandbox_id: str
    input_tokens: int
    output_tokens: int


def username(uid: str) -> str:
    return "u" + uid[:16]


class UserEnv:
    """Shell commands for one user. Names derive from a hex digest, never from user input,
    and are still quoted."""

    def __init__(self, cfg: Config, uid: str) -> None:
        self.cfg = cfg
        self.user = username(uid)
        self.home = f"{cfg.isolation.home_root.rstrip('/')}/{self.user}"
        self.request_path = f"{self.home}/.agent/turn.json"
        self.unix = cfg.isolation.mode == "unix_user"

    def provision(self) -> str:
        u, h = shlex.quote(self.user), shlex.quote(self.home)
        sub = f"{shlex.quote(self.home + '/.agent')} {shlex.quote(self.home + '/tmp')}"
        if self.unix:
            return (f"set -eu\n"
                    f"id -u {u} >/dev/null 2>&1 || useradd --create-home --home-dir {h} "
                    f"--shell /bin/bash --user-group {u}\n"
                    f"chmod 700 {h}\n"
                    f"install -d -o {u} -g {u} -m 700 {sub}\n")
        return f"set -eu\ninstall -d -m 700 {h} {sub}\n"

    def run_turn(self) -> str:
        u, h = shlex.quote(self.user), shlex.quote(self.home)
        env = {"HOME": self.home, "USER": self.user, "LOGNAME": self.user,
               "TMPDIR": f"{self.home}/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", **self.cfg.agent.env}
        env_args = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        argv = " ".join(shlex.quote(a) for a in self.cfg.agent.command)
        req = shlex.quote(self.request_path)
        t = self.cfg.session.turn_timeout_seconds
        runner = f"timeout -k 5 {t} env -i {env_args} {argv} {req}"
        if self.unix:
            return f"set -eu\nchown {u}:{u} {req}\ncd {h}\nexec runuser -u {u} -- {runner}\n"
        return f"set -eu\ncd {h}\nexec {runner}\n"

    def wipe(self) -> str:
        u, h = shlex.quote(self.user), shlex.quote(self.home)
        kill = f"pkill -KILL -u {u} || true\nuserdel -r -f {u} 2>/dev/null || true\n" if self.unix else ""
        return f"{kill}rm -rf {h}\n"


class SessionService:
    def __init__(self, cfg: Config, store: Store, backend: SandboxBackend, pool: PoolManager) -> None:
        self.cfg, self.store, self.backend, self.pool = cfg, store, backend, pool

    async def run_turn(self, uid: str, conversation_id: str, prompt: str) -> TurnResult:
        s = self.cfg.session
        # Turns stay inside the sandbox timeout plus margin for provisioning and resume.
        lock_ttl = s.turn_timeout_seconds + self.cfg.pool.create_timeout_seconds + 60
        token = await self.store.acquire_lock(f"lock:user:{uid}", lock_ttl)
        if not token:
            raise TurnInProgress()
        started = time.monotonic()
        outcome = "ok"
        with tm.tracer.start_as_current_span("agent.turn") as span:
            span.set_attribute("enduser.id", uid)
            span.set_attribute("agent.conversation_id", conversation_id)
            try:
                for attempt in (1, 2):
                    try:
                        return await self._turn(uid, conversation_id, prompt, span)
                    except SandboxGone as e:
                        # The sandbox disappeared. Its users' homes went with it; forget it and
                        # place the user again once.
                        b = await self.store.binding(uid)
                        if b.get("sandbox"):
                            await self.store.forget_sandbox(b["sandbox"])
                        log.warning("sandbox gone for user %s (attempt %d): %s", uid[:8], attempt, e)
                        if attempt == 2:
                            raise AgentError("Sandbox unavailable") from e
                raise AssertionError("unreachable")
            except PoolBusy:
                outcome = "busy"
                raise
            except BaseException:
                outcome = "error"
                raise
            finally:
                tm.turns.add(1, {"outcome": outcome})
                tm.turn_duration.record(time.monotonic() - started, {"outcome": outcome})
                await self.store.release_lock(f"lock:user:{uid}", token)

    async def _turn(self, uid: str, conv: str, prompt: str, span) -> TurnResult:
        s = self.cfg.session
        t0 = time.monotonic()
        sid, _ = await self.pool.acquire(uid)
        tm.queue_wait.record(time.monotonic() - t0)
        span.set_attribute("azure.sandbox.id", sid)

        await self.backend.ensure_running(sid)
        env = UserEnv(self.cfg, uid)

        binding = await self.store.binding(uid)
        if binding.get("provisioned") != sid:
            out = await self.backend.exec(sid, env.provision())
            if out.exit_code != 0:
                raise AgentError("Could not prepare the user workspace", out.stderr[-500:])
            await self.store.mark_provisioned(uid, sid)

        history = await self.store.history(uid, conv, s.max_history_messages)
        user_msg = {"role": "user", "content": prompt}
        request = {"conversation_id": conv, "messages": [*history, user_msg]}
        await self.backend.write_file(sid, env.request_path, json.dumps(request).encode())

        try:
            out = await asyncio.wait_for(self.backend.exec(sid, env.run_turn()),
                                         s.turn_timeout_seconds + 30)
        except asyncio.TimeoutError as e:
            raise AgentError("Agent turn timed out") from e
        if out.exit_code != 0:
            raise AgentError("Agent process failed", f"exit {out.exit_code}: {out.stderr[-500:]}")

        result = _parse(out.stdout)
        await self.store.append_history(
            uid, conv, [user_msg, {"role": "assistant", "content": result["reply"]}],
            ttl_s=s.history_ttl_seconds, keep=s.max_history_messages * 5)
        await self.store.touch(uid, sid)

        i, o = int(result.get("input_tokens", 0)), int(result.get("output_tokens", 0))
        span.set_attribute("gen_ai.usage.input_tokens", i)
        span.set_attribute("gen_ai.usage.output_tokens", o)
        tm.input_tokens.add(i)
        tm.output_tokens.add(o)
        return TurnResult(result["reply"], sid, i, o)

    async def end_session(self, uid: str) -> bool:
        """Unbind the user and delete their home. Called by the API and by the reaper."""
        lock = f"lock:user:{uid}"
        token = await self.store.acquire_lock(lock, 120)
        if not token:
            raise TurnInProgress()
        try:
            b = await self.store.binding(uid)
            sid = b.get("sandbox")
            if not sid:
                return False
            try:
                await self.backend.ensure_running(sid)
                out = await self.backend.exec(sid, UserEnv(self.cfg, uid).wipe())
                if out.exit_code != 0:
                    # Keep the binding so the wipe is retried; do not free a slot whose
                    # files may still be there.
                    log.warning("wipe failed for %s on %s: %s", uid[:8], sid, out.stderr[-200:])
                    return False
            except SandboxGone:
                pass
            return await self.store.unbind(uid, sid)
        finally:
            await self.store.release_lock(lock, token)


def _parse(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    try:
        data = json.loads(lines[-1])
        if isinstance(data, dict) and isinstance(data.get("reply"), str):
            return data
    except (IndexError, json.JSONDecodeError):
        pass
    raise AgentError("Agent returned malformed output", stdout[-300:])
