import io
import json
from types import SimpleNamespace as NS

import pytest
import yaml

from sandbox_mux import image as image_cli
from sandbox_mux.backends.azure import AzureSandboxBackend
from sandbox_mux.backends.base import ExecOutcome
from sandbox_mux.config import Config
from sandbox_mux.reaper import now_ms


def cfg(**pool) -> Config:
    return Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                                  "pool": {"max_sandboxes": 4, "users_per_sandbox": 3, **pool}})


# ---- configuration -----------------------------------------------------------------------------

def test_defaults_are_off():
    p = cfg().pool
    assert p.warmup_command is None and p.min_free_slots == 0 and p.entrypoint is None and p.cmd is None


def test_settings_validate():
    p = cfg(warmup_command=["python3", "-c", "import json"], min_free_slots=5,
            entrypoint=["sleep"], cmd=["infinity"]).pool
    assert p.min_free_slots == 5 and p.warmup_command[0] == "python3"
    with pytest.raises(ValueError, match="min_free_slots"):
        cfg(min_free_slots=13)                              # max 4 x 3 = 12


def test_image_and_slots_from_env(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"}}))
    monkeypatch.setenv("SANDBOX_MUX_ENV_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("SANDBOX_MUX_DISK_ID", "img-123")
    monkeypatch.setenv("SANDBOX_MUX_MIN_FREE_SLOTS", "2")
    monkeypatch.setenv("SANDBOX_MUX__POOL__WARMUP_COMMAND", '["python3", "-m", "agent.warm"]')
    c = Config.load(p).pool
    assert (c.disk_id, c.min_free_slots, c.warmup_command) == ("img-123", 2, ["python3", "-m", "agent.warm"])


async def test_azure_backend_passes_the_image_and_entrypoint():
    seen = {}

    class Poller:
        async def result(self):
            return NS(sandbox_id="sbx-1")

    class Client:
        async def begin_create_sandbox(self, **kw):
            seen.update(kw)
            return Poller()

    b = AzureSandboxBackend(cfg(disk_id="img-9", entrypoint=["sleep"], cmd=["infinity"]))
    b._client = Client()
    await b.create({})
    assert seen["disk_id"] == "img-9" and seen["disk"] is None
    assert seen["entrypoint"] == ["sleep"] and seen["cmd"] == ["infinity"]


# ---- warm-up command ------------------------------------------------------------------------------

async def test_warmup_runs_after_setup_and_before_the_sandbox_takes_users(make_replica, backend):
    r = make_replica(warmup_command=["python3", "-c", "import os; print('a b')"], warmup_timeout_seconds=30)
    sid, _ = await r.pool.acquire("a" * 32)
    cmds = [c for s, c in backend.execs if s == sid]
    assert "exec user is not root" in cmds[0]                      # bootstrap first
    assert "timeout -k 5 30 python3 -c 'import os; print('\"'\"'a b'\"'\"')'" in cmds[1]   # argv is quoted
    assert len(cmds) == 2 and sid in await r.store.states()


async def test_a_failing_warmup_means_the_sandbox_is_deleted_and_never_registered(make_replica, backend):
    backend.handler = lambda sid, c: ExecOutcome(1, "", "ImportError: no module x") if "timeout -k 5" in c else None
    r = make_replica(warmup_command=["python3", "-c", "import x"])
    r.cfg.session.queue_timeout_seconds = 1
    from sandbox_mux.pool import PoolBusy
    with pytest.raises(PoolBusy):
        await r.pool.acquire("a" * 32)
    assert backend.sandboxes == {} and await r.store.states() == {}
    assert await r.store.creating_count() == 0


# ---- warm pool ---------------------------------------------------------------------------------------

async def test_min_free_slots_keeps_headroom_and_refills_as_users_arrive(make_replica, backend):
    r = make_replica(users_per_sandbox=3, max_sandboxes=4, min_free_slots=4)
    await r.pool.warm()
    assert len(backend.sandboxes) == 2                             # 2 x 3 = 6 free >= 4
    for u in "abcd":                                               # 4 users: 2 free left
        await r.pool.acquire(u * 32)
    await r.pool.warm()
    assert len(backend.sandboxes) == 3                             # added one to get back to 5 free
    _, free = await r.pool.free_slots()
    assert free >= 4


async def test_warm_never_exceeds_max_sandboxes(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=2, min_free_slots=2)
    await r.pool.warm()
    await r.pool.warm()
    assert len(backend.sandboxes) == 2


async def test_warm_stops_at_the_first_failure_instead_of_looping(make_replica, backend):
    calls = []

    async def broken(labels):
        calls.append(1)
        raise RuntimeError("image not found")

    backend.create = broken
    r = make_replica(min_sandboxes=3)
    await r.pool.warm()
    assert len(calls) == 1 and await r.store.creating_count() == 0


async def test_empty_sandboxes_are_kept_while_they_provide_the_free_slot_floor(make_replica, backend):
    r = make_replica(users_per_sandbox=2, max_sandboxes=4, min_free_slots=4, empty_ttl_seconds=0)
    await r.pool.warm()
    assert len(backend.sandboxes) == 2
    await r.reaper.drain_empty_sandboxes()
    assert len(backend.sandboxes) == 2                             # 4 free slots needed, 2 x 2 = 4

    r.cfg.pool.min_free_slots = 2
    await r.reaper.drain_empty_sandboxes()
    assert len(backend.sandboxes) == 1                             # one is enough now


# ---- image build command -------------------------------------------------------------------------------

class StubClient:
    def __init__(self, state="Ready"):
        self.calls, self.state = [], state

    def begin_create_disk_image(self, base, **kw):
        self.calls.append((base, kw))
        img = NS(id="img-1", name=kw["name"], status=NS(state=self.state), image=NS(base=base), size_in_mb=812)
        return NS(result=lambda: img)

    def get_sandbox_client(self, sid):
        def begin_commit(**kw):
            self.calls.append(("commit", sid, kw))
            return NS(result=lambda: NS(id="img-2", name=kw["name"], status=NS(state="Ready"),
                                        image=NS(base="x"), size_in_mb=5))
        return NS(begin_commit=begin_commit)

    def list_disk_images(self):
        return [NS(id="i", name="n", status=NS(state="Ready"), image=NS(base="b"), size_in_mb=1)]

    def list_public_disk_images(self):
        return [NS(name="ubuntu"), NS(name="python-3.12")]


def run(argv, stub, tmp_path, environ=None):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"}}))
    out = io.StringIO()
    code = image_cli.main(["--config", str(p), *argv], client_factory=lambda c: stub,
                          environ=environ or {}, out=out)
    return code, out.getvalue()


def test_build_with_a_managed_identity(tmp_path):
    stub = StubClient()
    code, out = run(["build", "--image", "myreg.azurecr.io/agent:1", "--name", "agent-1",
                     "--managed-identity", "system"], stub, tmp_path)
    assert code == 0 and json.loads(out)["id"] == "img-1" and json.loads(out)["state"] == "Ready"
    base, kw = stub.calls[0]
    assert base == "myreg.azurecr.io/agent:1" and kw["managed_identity_client_id"] == "system"
    assert kw["registry_credentials"] is None and kw["polling_timeout"] == 900


def test_registry_password_is_read_from_the_environment_not_the_command_line(tmp_path):
    stub = StubClient()
    code, _ = run(["build", "--image", "r/a:1", "--name", "a", "--registry-user", "bot"], stub, tmp_path,
                  environ={"REGISTRY_TOKEN": "s3cret"})
    creds = stub.calls[0][1]["registry_credentials"]
    assert code == 0 and (creds.username, creds.token) == ("bot", "s3cret")
    assert "--registry-token" not in image_cli.build_parser().format_help().replace("--registry-token-env", "")


def test_missing_registry_token_is_an_error_and_nothing_is_built(tmp_path, capsys):
    stub = StubClient()
    code, _ = run(["build", "--image", "r/a:1", "--name", "a", "--registry-user", "bot"], stub, tmp_path)
    assert code == 2 and stub.calls == [] and "REGISTRY_TOKEN" in capsys.readouterr().err


def test_a_failed_build_exits_nonzero(tmp_path):
    code, out = run(["build", "--image", "r/a:1", "--name", "a"], StubClient(state="Failed"), tmp_path)
    assert code == 1 and json.loads(out)["state"] == "Failed"


def test_commit_list_and_public(tmp_path):
    stub = StubClient()
    code, out = run(["commit", "--sandbox-id", "sbx-1", "--name", "primed"], stub, tmp_path)
    assert code == 0 and json.loads(out)["id"] == "img-2" and stub.calls[0][1] == "sbx-1"
    assert json.loads(run(["list"], stub, tmp_path)[1])[0]["id"] == "i"
    assert json.loads(run(["public"], stub, tmp_path)[1]) == ["ubuntu", "python-3.12"]
