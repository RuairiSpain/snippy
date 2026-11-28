"""Runs one agent turn for one user on whichever sandbox the user is bound to."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass, field

from .backends import SandboxBackend, SandboxGone
from .config import Config
from .files import FileError, FileRecord, FileService, unique_names
from .limits import snippet as limits_snippet
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
    outputs: list[FileRecord] = field(default_factory=list)
    skipped_outputs: list[dict] = field(default_factory=list)


def username(uid: str) -> str:
    return "u" + uid[:16]


class UserEnv:
    """Shell commands for one user. Names derive from a hex digest, never from user input,
    and are still quoted.

    Rule for unix_user mode: root never reads, writes, chowns or follows a path the user can
    change. A user can replace anything under their home with a symlink, and the data-plane
    file calls and these scripts run as root. So files exchanged with the user's process live
    under `isolation.state_root`, which is owned by root, with the user's group granted read
    access only:

        {state_root}/req/<user>/   request and delegated-token files   root:<user> 0750
        {state_root}/in/<user>/    uploaded input files                root:<user> 0750
        {state_root}/out/<user>/   output bundle, read back by root    root:root   0700

    Output files are collected by a process running as the user, so the kernel applies the
    user's own permissions to whatever they point at.
    """

    BUNDLE = "bundle.tar"

    def __init__(self, cfg: Config, uid: str) -> None:
        self.cfg = cfg
        self.user = username(uid)
        self.unix = cfg.isolation.mode == "unix_user"
        self.home = f"{cfg.isolation.home_root.rstrip('/')}/{self.user}"
        root = cfg.isolation.state_root.rstrip("/")
        if self.unix:
            self.req_dir = f"{root}/req/{self.user}"
        else:
            self.req_dir = f"{self.home}/.agent"        # one UID: nothing to protect
        self.in_dir = f"{root}/in/{self.user}"
        self.out_dir = f"{root}/out/{self.user}"
        self.request_path = f"{self.req_dir}/turn.json"
        self.obo_path = f"{self.req_dir}/obo.json"
        self.bundle_path = f"{self.out_dir}/{self.BUNDLE}"
        self.outputs_dir = f"{self.home}/outputs"       # the agent writes results here

    def _q(self, v: str) -> str:
        return shlex.quote(v)

    def provision(self) -> str:
        u, h = self._q(self.user), self._q(self.home)
        sub = f"{self._q(self.home + '/tmp')} {self._q(self.outputs_dir)}"
        if self.unix:
            return (f"set -eu\n"
                    f"id -u {u} >/dev/null 2>&1 || useradd --create-home --home-dir {h} "
                    f"--shell /bin/bash --user-group {u}\n"
                    f"chmod 700 {h}\n"
                    f"install -d -o {u} -g {u} -m 700 {sub}\n"
                    f"install -d -o root -g {u} -m 0750 {self._q(self.req_dir)} {self._q(self.in_dir)}\n"
                    f"install -d -o root -g root -m 0700 {self._q(self.out_dir)}\n")
        return f"set -eu\ninstall -d -m 700 {h} {sub} {self._q(self.req_dir)}\n"

    def run_turn(self, with_obo: bool = False, with_inputs: bool = False) -> str:
        u, h = self._q(self.user), self._q(self.home)
        env = {"HOME": self.home, "USER": self.user, "LOGNAME": self.user,
               "TMPDIR": f"{self.home}/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", **self.cfg.agent.env}
        if with_obo:
            env["OBO_TOKENS_FILE"] = self.obo_path
        if self.cfg.files.enabled:
            env["OUTPUT_DIR"] = self.outputs_dir
        if with_inputs:
            env["INPUT_DIR"] = self.in_dir
        env_args = " ".join(f"{k}={self._q(v)}" for k, v in env.items())
        argv = " ".join(self._q(a) for a in self.cfg.agent.command)
        req = self._q(self.request_path)
        t = self.cfg.session.turn_timeout_seconds
        runner = f"timeout -k 5 {t} env -i {env_args} {argv} {req}"
        limits = limits_snippet(self.cfg, self.user)
        if not self.unix:
            return f"set -eu\n{limits}cd {h}\nexec {runner}\n"
        # Everything below touches root-owned paths only. Files are root:<user> 0640: the
        # user's process can read them but cannot modify, replace or delete them.
        files = [req] + ([self._q(self.obo_path)] if with_obo else [])
        lines = [
            "set -eu",
            limits.rstrip("\n"),
            f"install -d -o root -g {u} -m 0750 {self._q(self.req_dir)} {self._q(self.in_dir)}",
            f"chgrp -h {u} {' '.join(files)}",
            f"chmod 0640 {' '.join(files)}",
        ]
        if with_inputs:
            lines.append(f"chgrp -hR {u} {self._q(self.in_dir)}")
            lines.append(f"chmod -R u=rwX,g=rX,o= {self._q(self.in_dir)}")
        lines += [f"cd {h}", f"exec runuser -u {u} -- {runner}"]
        return "\n".join(l for l in lines if l) + "\n"

    def reset_inputs(self) -> str:
        """Empty the input directory (root-owned, so nothing in it is user-controlled)."""
        d = self._q(self.in_dir)
        return f"set -eu\ninstall -d -o root -g {self._q(self.user)} -m 0750 {d}\nfind {d} -mindepth 1 -delete\n"

    def collect_outputs(self, max_bytes: int) -> str:
        """Bundle the user's output files into a root-owned tar.

        The tar is written by a process running as the user, so symlinks and hard links to
        files the user cannot read fail inside the kernel; only regular files are listed. The
        redirect target is a root-owned path the user cannot touch. The size cap bounds how
        much the manager will read back; it applies to the archive, which adds a 512-byte header
        per file."""
        u = self._q(self.user)
        out = self._q(self.bundle_path)
        inner = ('cd "$HOME/outputs" 2>/dev/null || exit 0; '
                 "find . -type f -print0 | tar --null --no-recursion --blocking-factor=1 -T - --create --file=-")
        env = f"HOME={self._q(self.home)} PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8"
        return (f"set -u\n"
                f"install -d -o root -g root -m 0700 {self._q(self.out_dir)}\n"
                f"rm -f {out}\n"
                f"runuser -u {u} -- env -i {env} sh -c {self._q(inner)} 2>/dev/null "
                f"| head -c {int(max_bytes) + 1} > {out} || true\n")

    def clear_outputs(self) -> str:
        """Empty the user's output directory, as the user."""
        env = f"HOME={self._q(self.home)} PATH=/usr/local/bin:/usr/bin:/bin"
        inner = 'find "$HOME/outputs" -mindepth 1 -delete 2>/dev/null || true'
        return f"runuser -u {self._q(self.user)} -- env -i {env} sh -c {self._q(inner)}\n"

    def drop_bundle(self) -> str:
        return f"rm -f {self._q(self.bundle_path)}\n"

    def drop_obo(self) -> str:
        return f"rm -f {self._q(self.obo_path)}\n"

    def wipe(self) -> str:
        u, h = self._q(self.user), self._q(self.home)
        kill = f"pkill -KILL -u {u} || true\nuserdel -r -f {u} 2>/dev/null || true\n" if self.unix else ""
        # `rm -rf` does not follow symlinks, so a planted link is removed, not traversed.
        extra = (f"rm -rf {self._q(self.req_dir)} {self._q(self.in_dir)} {self._q(self.out_dir)}\n"
                 if self.unix else "")
        return f"{kill}rm -rf {h}\n{extra}"


class SessionService:
    def __init__(self, cfg: Config, store: Store, backend: SandboxBackend, pool: PoolManager,
                 files: FileService | None = None) -> None:
        self.cfg, self.store, self.backend, self.pool = cfg, store, backend, pool
        self.files = files

    async def run_turn(self, uid: str, conversation_id: str, prompt: str,
                       obo_tokens: dict | None = None,
                       inputs: list[FileRecord] | None = None) -> TurnResult:
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
                        return await self._turn(uid, conversation_id, prompt, span, obo_tokens,
                                                inputs or [])
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

    async def _turn(self, uid: str, conv: str, prompt: str, span,
                    obo_tokens: dict | None = None,
                    inputs: list[FileRecord] | None = None) -> TurnResult:
        s = self.cfg.session
        inputs = inputs or []
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

        staged: list[dict] = []
        try:
            if inputs:
                staged = await self._stage_inputs(sid, env, inputs)

            history = await self.store.history(uid, conv, s.max_history_messages)
            user_msg = {"role": "user", "content": prompt}
            request = {"conversation_id": conv, "messages": [*history, user_msg]}
            if staged:
                request["input_files"] = staged
            await self.backend.write_file(sid, env.request_path, json.dumps(request).encode(),
                                          mode="640")
            if obo_tokens:
                # Root-owned directory, group-readable by this user only. The token is not
                # placed in the command line or environment, which other users could see.
                await self.backend.write_file(sid, env.obo_path, json.dumps(obo_tokens).encode(),
                                              mode="640")
            try:
                out = await asyncio.wait_for(
                    self.backend.exec(sid, env.run_turn(with_obo=bool(obo_tokens),
                                                        with_inputs=bool(staged))),
                    s.turn_timeout_seconds + 30)
            except asyncio.TimeoutError as e:
                raise AgentError("Agent turn timed out") from e
            finally:
                if obo_tokens:
                    await self._drop_obo(sid, env)
            if out.exit_code != 0:
                raise AgentError("Agent process failed",
                                 f"exit {out.exit_code}: {out.stderr[-500:]}")

            result = _parse(out.stdout)
            outputs: list[FileRecord] = []
            skipped: list[dict] = []
            if self.files:
                outputs, skipped = await self._collect_outputs(sid, env, uid, conv)
        finally:
            if staged:
                await self._drop_inputs(sid, env)

        await self.store.append_history(
            uid, conv, [user_msg, {"role": "assistant", "content": result["reply"]}],
            ttl_s=s.history_ttl_seconds, keep=s.max_history_messages * 5)
        await self.store.touch(uid, sid)

        i, o = int(result.get("input_tokens", 0)), int(result.get("output_tokens", 0))
        span.set_attribute("gen_ai.usage.input_tokens", i)
        span.set_attribute("gen_ai.usage.output_tokens", o)
        tm.input_tokens.add(i)
        tm.output_tokens.add(o)
        return TurnResult(result["reply"], sid, i, o, outputs, skipped)

    async def _stage_inputs(self, sid: str, env: UserEnv, inputs: list[FileRecord]) -> list[dict]:
        """Copy the user's uploaded files into the root-owned input directory."""
        assert self.files
        out = await self.backend.exec(sid, env.reset_inputs())
        if out.exit_code != 0:
            raise AgentError("Could not prepare input files", out.stderr[-300:])
        names = unique_names([r.name for r in inputs])
        staged = []
        for rec, name in zip(inputs, names):
            data = await self.files.read(rec)
            path = f"{env.in_dir}/{name}"
            await self.backend.write_file(sid, path, data, mode="640")
            staged.append({"file_id": rec.id, "name": name, "path": path, "size": rec.size,
                           "sha256": rec.sha256, "content_type": rec.content_type})
        return staged

    async def _drop_inputs(self, sid: str, env: UserEnv) -> None:
        try:
            await asyncio.shield(self.backend.exec(sid, env.reset_inputs()))
        except Exception:
            log.exception("could not clear input files on %s", sid)

    async def _collect_outputs(self, sid: str, env: UserEnv, uid: str, conv: str
                               ) -> tuple[list[FileRecord], list[dict]]:
        """Pull files the agent left in $OUTPUT_DIR into durable storage.

        This runs after the agent has already run, so a failure here is reported to the caller
        and never retried: retrying would run the agent, and its side effects, a second time."""
        assert self.files
        try:
            out = await self.backend.exec(
                sid, env.collect_outputs(self.cfg.files.turn_output_bytes))
            if out.exit_code != 0:
                raise RuntimeError(out.stderr[-200:])
            bundle = await self.backend.read_file(sid, env.bundle_path)
            records, skipped = await self.files.store_outputs(uid, conv, bundle)
            fully_stored = not any(x["name"] == "*" or x["reason"] in ("storage quota",)
                                   for x in skipped)
            cleanup = env.drop_bundle() + (env.clear_outputs() if fully_stored else "")
            await self.backend.exec(sid, cleanup)
            return records, skipped
        except Exception as e:
            log.exception("could not collect outputs for %s on %s", uid[:8], sid)
            return [], [{"name": "*", "reason": f"outputs could not be collected ({type(e).__name__})"}]

    async def _drop_obo(self, sid: str, env: UserEnv) -> None:
        """Delete the delegated tokens. A failure is logged loudly: the file stays readable
        by the user until it expires and until the session is wiped."""
        try:
            out = await asyncio.shield(self.backend.exec(sid, env.drop_obo()))
            if out.exit_code != 0:
                log.error("could not delete OBO token file on %s: %s", sid, out.stderr[-200:])
        except Exception:
            log.exception("could not delete OBO token file on %s", sid)

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
