import asyncio
import hashlib
import io
import os
import re
import subprocess
import tarfile

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
from asgi_lifespan import LifespanManager

from sandbox_mux.api import create_app
from sandbox_mux.backends.base import ExecOutcome
from sandbox_mux.backends.fake import FakeSandboxBackend
from sandbox_mux.blobs import LocalBlobStore
from sandbox_mux.config import Config
from sandbox_mux.files import FileError, safe_name, unique_names
from sandbox_mux.service import UserEnv

ALICE = {"X-Dev-User": "alice"}
BOB = {"X-Dev-User": "bob"}


def files_cfg(tmp_path=None, **files) -> Config:
    return Config.model_validate({
        "backend": "fake", "auth": {"mode": "dev"},
        "pool": {"max_sandboxes": 2, "users_per_sandbox": 4},
        "session": {"queue_timeout_seconds": 1},
        "files": {"enabled": True, "store": "local", "max_file_size": "1000",
                  "max_user_total_size": "10000", "max_output_size_per_turn": "20000", **files},
    })


@pytest.fixture
async def env(tmp_path):
    """Returns (client, backend, blobs, app) with file storage enabled."""
    made = []

    async def _make(cfg=None):
        cfg = cfg or files_cfg()
        backend = FakeSandboxBackend()
        blobs = LocalBlobStore(tmp_path / "blobs")
        app = create_app(cfg, backend=backend, redis_client=fakeredis.aioredis.FakeRedis(),
                         run_reaper=False, blob_store=blobs)
        lm = LifespanManager(app)
        await lm.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        made.append((lm, client))
        return client, backend, blobs, app

    yield _make
    for lm, client in made:
        await client.aclose()
        await lm.__aexit__(None, None, None)


def blob_count(blobs) -> int:
    return sum(1 for p in blobs.root.rglob("*") if p.is_file())


def uid_of(name: str) -> str:
    from sandbox_mux.auth import Principal
    return Principal("dev", name).uid


def make_tar(members: list) -> bytes:
    """members: (name, bytes) for files, or (name, 'symlink', target) / (name, 'dir')."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for m in members:
            info = tarfile.TarInfo(m[0])
            if len(m) == 3 and m[1] == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, m[2]
                t.addfile(info)
            elif len(m) == 2 and m[1] == "dir":
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            else:
                info.size = len(m[1])
                t.addfile(info, io.BytesIO(m[1]))
    # Python pads to 10 KiB; `tar --blocking-factor=1` (what the collector runs) does not.
    raw = buf.getvalue().rstrip(b"\0")
    return raw + b"\0" * (-len(raw) % 512) + b"\0" * 1024


# ---- configuration -------------------------------------------------------------------------

def test_default_limit_is_100mb():
    c = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}})
    assert c.files.max_file_size == "100MB" and c.files.file_bytes == 100_000_000
    assert c.files.enabled is False


def test_limit_can_be_set_in_yaml_or_env(tmp_path, monkeypatch):
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"},
                                 "files": {"max_file_size": "250MB", "max_output_size_per_turn": "1GB"}}))
    monkeypatch.setenv("SANDBOX_MUX_ENV_FILE", str(tmp_path / "none"))
    assert Config.load(p).files.file_bytes == 250_000_000
    monkeypatch.setenv("SANDBOX_MUX_MAX_FILE_SIZE", "5MB")
    assert Config.load(p).files.file_bytes == 5_000_000
    monkeypatch.setenv("SANDBOX_MUX__FILES__MAX_FILE_SIZE", "64MiB")      # generic form wins
    assert Config.load(p).files.file_bytes == 64 * 2**20
    monkeypatch.setenv("SANDBOX_MUX_FILES_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MUX_FILE_RETENTION_DAYS", "30")
    monkeypatch.setenv("SANDBOX_MUX__FILES__STORE", "local")
    c = Config.load(p)
    assert c.files.enabled and c.files.retention_days == 30


@pytest.mark.parametrize("files, message", [
    ({"max_file_size": "2GB", "max_user_total_size": "1GB"}, "larger than files.max_user_total_size"),
    ({"max_file_size": "100MB", "max_output_size_per_turn": "10MB"}, "at least files.max_file_size"),
    ({"max_file_size": "0"}, "greater than zero"),
    ({"max_file_size": "lots"}, "invalid size"),
    ({"enabled": True, "store": "azure_blob"}, "account_url"),
])
def test_invalid_file_settings_are_rejected(files, message):
    with pytest.raises(ValueError, match=message):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}, "files": files})


def test_files_need_separate_unix_accounts():
    with pytest.raises(ValueError, match="unix_user"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                               "isolation": {"mode": "home_only"},
                               "files": {"enabled": True, "store": "local"}})


# ---- names -----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("report.csv", "report.csv"), ("../../etc/passwd", "passwd"), ("a/b\\c.txt", "c.txt"),
    ("  .hidden  ", "hidden"), ("na<me>?.txt", "name.txt"), ("tab\tname\x00.txt", "tabname.txt"),
    ("ünï.txt", "ünï.txt"),
])
def test_names_are_reduced_to_one_safe_component(raw, expected):
    assert safe_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "..", ".", "/", "...", "\x00"])
def test_unusable_names_are_rejected(raw):
    with pytest.raises(FileError):
        safe_name(raw)


def test_duplicate_names_get_suffixes():
    assert unique_names(["a.txt", "a.txt", "A.TXT", "b", "b"]) == ["a.txt", "a (2).txt", "A (3).TXT",
                                                                     "b", "b (2)"]


# ---- upload and download ------------------------------------------------------------------

async def test_upload_list_download_delete_roundtrip(env):
    client, _, blobs, _ = await env()
    body = b"hello,world\n1,2\n"
    r = await client.post("/v1/files?name=data.csv", content=body, headers={**ALICE, "content-type": "text/csv"})
    assert r.status_code == 201
    meta = r.json()
    assert meta["name"] == "data.csv" and meta["size"] == len(body) and meta["kind"] == "input"
    assert meta["sha256"] == hashlib.sha256(body).hexdigest() and meta["content_type"] == "text/csv"
    fid = meta["file_id"]

    assert [f["file_id"] for f in (await client.get("/v1/files", headers=ALICE)).json()] == [fid]
    assert (await client.get(f"/v1/files/{fid}/metadata", headers=ALICE)).json()["file_id"] == fid

    d = await client.get(f"/v1/files/{fid}", headers=ALICE)
    assert d.status_code == 200 and d.content == body
    assert d.headers["content-disposition"].startswith("attachment;")
    assert d.headers["x-content-type-options"] == "nosniff" and "sandbox" in d.headers["content-security-policy"]

    assert (await client.delete(f"/v1/files/{fid}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/v1/files/{fid}", headers=ALICE)).status_code == 404
    assert blob_count(blobs) == 0


async def test_other_users_cannot_see_use_or_delete_a_file(env):
    client, _, blobs, _ = await env()
    fid = (await client.post("/v1/files?name=a.txt", content=b"secret", headers=ALICE)).json()["file_id"]
    for call in (client.get(f"/v1/files/{fid}", headers=BOB),
                 client.get(f"/v1/files/{fid}/metadata", headers=BOB),
                 client.delete(f"/v1/files/{fid}", headers=BOB)):
        assert (await call).status_code == 404
    assert (await client.get("/v1/files", headers=BOB)).json() == []
    r = await client.post("/v1/agent/invoke", headers=BOB, json={"prompt": "x", "inputs": [fid]})
    assert r.status_code == 422
    assert blob_count(blobs) == 1                           # still there for alice


async def test_file_larger_than_the_limit_is_refused_by_header_and_by_stream(env):
    client, _, blobs, app = await env()
    st = app.state.ctx["store"]
    r = await client.post("/v1/files?name=big.bin", content=b"x" * 1001, headers=ALICE)
    assert r.status_code == 413
    assert blob_count(blobs) == 0 and await st.user_usage(uid_of("alice")) == (0, 0)

    async def chunks():                                      # no Content-Length: size only known as it streams
        for _ in range(5):
            yield b"y" * 300
    r = await client.post("/v1/files?name=big2.bin", content=chunks(), headers=ALICE)
    assert r.status_code == 413
    assert blob_count(blobs) == 0 and await st.user_usage(uid_of("alice")) == (0, 0)


async def test_exactly_the_limit_is_accepted(env):
    client, *_ = await env()
    assert (await client.post("/v1/files?name=ok.bin", content=b"x" * 1000, headers=ALICE)).status_code == 201


async def test_empty_file_and_bad_names_are_rejected(env):
    client, *_ = await env()
    assert (await client.post("/v1/files?name=e.txt", content=b"", headers=ALICE)).status_code == 422
    assert (await client.post("/v1/files?name=..", content=b"x", headers=ALICE)).status_code == 422
    r = await client.post("/v1/files?name=../../etc/passwd", content=b"x", headers=ALICE)
    assert r.json()["name"] == "passwd"


async def test_quota_counts_files_and_bytes_and_survives_concurrency(env):
    client, _, blobs, app = await env(files_cfg(max_files_per_user=2))
    codes = await asyncio.gather(*(client.post(f"/v1/files?name=f{i}", content=b"z" * 10, headers=ALICE)
                                   for i in range(6)))
    ok = [c.status_code for c in codes]
    assert ok.count(201) == 2 and ok.count(409) == 4
    assert blob_count(blobs) == 2

    client2, *_ = await env(files_cfg(max_user_total_size="2500", max_file_size="1000",
                                      max_output_size_per_turn="5000"))
    for i in range(2):
        assert (await client2.post(f"/v1/files?name=a{i}", content=b"q" * 1000, headers=ALICE)).status_code == 201
    assert (await client2.post("/v1/files?name=a3", content=b"q" * 1000, headers=ALICE)).status_code == 409


async def test_deleting_frees_quota(env):
    client, *_ = await env(files_cfg(max_files_per_user=1))
    fid = (await client.post("/v1/files?name=a", content=b"1", headers=ALICE)).json()["file_id"]
    assert (await client.post("/v1/files?name=b", content=b"1", headers=ALICE)).status_code == 409
    await client.delete(f"/v1/files/{fid}", headers=ALICE)
    assert (await client.post("/v1/files?name=b", content=b"1", headers=ALICE)).status_code == 201


async def test_file_endpoints_404_when_disabled():
    cfg = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}})
    app = create_app(cfg, backend=FakeSandboxBackend(), redis_client=fakeredis.aioredis.FakeRedis(),
                     run_reaper=False)
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.post("/v1/files?name=a", content=b"x", headers=ALICE)).status_code == 404
            r = await c.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "x", "inputs": ["a" * 32]})
            assert r.status_code == 422


async def test_expired_files_and_stale_reservations_are_purged(env):
    client, _, blobs, app = await env()
    st, files = app.state.ctx["store"], app.state.ctx["files"]
    fid = (await client.post("/v1/files?name=a", content=b"1", headers=ALICE)).json()["file_id"]
    await st.r.zadd(st.k("files:expiry"), {f"{uid_of('alice')}:{fid}": 1})            # long past
    await st.r.hset(st.k(f"userfiles:{uid_of('alice')}"), "f" * 32, 500)               # crashed upload
    await st.r.zadd(st.k("files:pending"), {f"{uid_of('alice')}:{'f' * 32}": 1})
    assert await files.purge_expired() == 1
    assert blob_count(blobs) == 0 and await st.get_file(fid) is None
    assert await st.user_usage(uid_of("alice")) == (0, 0)


# ---- inputs reach the sandbox -------------------------------------------------------------------

async def test_inputs_are_staged_in_a_root_owned_dir_and_cleared_afterwards(env):
    client, backend, *_ = await env()
    a = (await client.post("/v1/files?name=data.csv", content=b"AAA", headers=ALICE)).json()["file_id"]
    b = (await client.post("/v1/files?name=data.csv", content=b"BBB", headers=ALICE)).json()["file_id"]
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go", "inputs": [a, b, a]})
    assert r.status_code == 200
    e = UserEnv(files_cfg(), uid_of("alice"))
    sid = r.json()["sandbox_id"]
    assert e.in_dir.startswith("/var/lib/smux/in/") and not e.in_dir.startswith(e.home)
    assert backend.files[(sid, f"{e.in_dir}/data.csv")] == b"AAA"
    assert backend.files[(sid, f"{e.in_dir}/data (2).csv")] == b"BBB"          # duplicate name, one dir
    assert backend.modes[(sid, f"{e.in_dir}/data.csv")] == "640"
    import json
    req = json.loads(backend.files[(sid, e.request_path)])
    assert [f["name"] for f in req["input_files"]] == ["data.csv", "data (2).csv"]
    cmds = [c for s, c in backend.execs if s == sid]
    run = next(c for c in cmds if "exec runuser" in c)
    assert f"INPUT_DIR={e.in_dir}" in run and f"chgrp -hR {e.user} {e.in_dir}" in run
    assert e.reset_inputs() in cmds[:cmds.index(run)]                          # emptied before staging
    assert e.reset_inputs() in cmds[cmds.index(run) + 1:]                      # and again after the turn


async def test_inputs_are_emptied_even_when_the_agent_fails(env):
    client, backend, *_ = await env()
    backend.handler = lambda sid, c: ExecOutcome(1, "", "boom") if "exec runuser" in c else None
    fid = (await client.post("/v1/files?name=a.txt", content=b"A", headers=ALICE)).json()["file_id"]
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go", "inputs": [fid]})
    assert r.status_code == 502
    assert [c for _, c in backend.execs][-1] == UserEnv(files_cfg(), uid_of("alice")).reset_inputs()


async def test_too_many_inputs_rejected(env):
    client, *_ = await env(files_cfg(max_input_files_per_turn=1))
    ids = [(await client.post(f"/v1/files?name=f{i}", content=b"1", headers=ALICE)).json()["file_id"]
           for i in range(2)]
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go", "inputs": ids})
    assert r.status_code == 422


# ---- outputs come back ---------------------------------------------------------------------------

def bundle_handler(backend, tar_bytes):
    def h(sid, cmd):
        if "bundle.tar" in cmd and "--blocking-factor=1" in cmd:
            backend.files[(sid, UserEnv(files_cfg(), uid_of("alice")).bundle_path)] = tar_bytes
            return ExecOutcome(0, "", "")
        return None
    return h


async def test_outputs_are_stored_returned_and_downloadable(env):
    client, backend, blobs, _ = await env()
    backend.handler = bundle_handler(backend, make_tar([
        ("report.csv", b"a,b\n1,2\n"), ("./sub/deep/notes.txt", b"hi"), ("sub", "dir"),
        ("dup.txt", b"1"), ("other/dup.txt", b"2")]))
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert r.status_code == 200
    outs = {o["name"]: o for o in r.json()["outputs"]}
    assert set(outs) == {"report.csv", "notes.txt", "dup.txt", "dup (2).txt"}
    assert all(o["kind"] == "output" and o["conversation_id"] == "default" for o in outs.values())
    assert r.json()["skipped_outputs"] == []
    d = await client.get(f"/v1/files/{outs['report.csv']['file_id']}", headers=ALICE)
    assert d.content == b"a,b\n1,2\n"
    assert outs["report.csv"]["content_type"] == "text/csv"
    # bundle removed and the output directory emptied once everything is stored
    last = [c for _, c in backend.execs][-1]
    assert "bundle.tar" in last and "find" in last and "-delete" in last
    assert blob_count(blobs) == 4


async def test_unsafe_archive_members_are_skipped_not_stored(env):
    client, backend, blobs, _ = await env()
    backend.handler = bundle_handler(backend, make_tar([
        ("ok.txt", b"fine"), ("link", "symlink", "/etc/passwd"), ("/etc/abs.txt", b"x"),
        ("../escape.txt", b"x"), ("a/../../up.txt", b"x"), ("big.bin", b"x" * 1001),
        ("empty.txt", b"")]))
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert [o["name"] for o in r.json()["outputs"]] == ["ok.txt"]
    reasons = {s["name"]: s["reason"] for s in r.json()["skipped_outputs"]}
    assert reasons["link"] == "not a regular file"
    assert reasons["/etc/abs.txt"] == "unsafe path" and reasons["../escape.txt"] == "unsafe path"
    assert reasons["a/../../up.txt"] == "unsafe path"
    assert "larger than" in reasons["big.bin"] and reasons["empty.txt"] == "empty file"
    assert blob_count(blobs) == 1


async def test_too_many_output_files_are_capped(env):
    client, backend, blobs, _ = await env(files_cfg(max_output_files_per_turn=2))
    backend.handler = bundle_handler(backend, make_tar([(f"f{i}.txt", b"x") for i in range(5)]))
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert len(r.json()["outputs"]) == 2
    assert [s["reason"] for s in r.json()["skipped_outputs"]] == ["too many output files"] * 3


async def test_output_over_the_per_turn_cap_is_dropped_whole(env):
    client, backend, blobs, _ = await env(files_cfg(max_output_size_per_turn="1000"))
    backend.handler = bundle_handler(backend, b"\0" * 1500)       # what `head -c cap+1` would produce
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert r.status_code == 200 and r.json()["outputs"] == [] and blob_count(blobs) == 0
    assert r.json()["skipped_outputs"][0]["name"] == "*"


async def test_output_quota_is_enforced_and_outputs_are_kept_for_next_time(env):
    client, backend, blobs, _ = await env(files_cfg(max_files_per_user=1))
    backend.handler = bundle_handler(backend, make_tar([("a.txt", b"1"), ("b.txt", b"2")]))
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert len(r.json()["outputs"]) == 1 and r.json()["skipped_outputs"][0]["name"] == "b.txt"
    assert blob_count(blobs) == 1


async def test_collection_failure_never_reruns_the_agent(env):
    client, backend, *_ = await env()
    def h(sid, cmd):
        if "--blocking-factor=1" in cmd:
            raise RuntimeError("sandbox hiccup")
    backend.handler = h
    r = await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert r.status_code == 200 and r.json()["reply"] == "echo: go"
    assert r.json()["skipped_outputs"][0]["name"] == "*"
    assert sum("exec runuser" in c for _, c in backend.execs) == 1         # agent ran once


async def test_agent_gets_output_dir_and_no_files_disabled_means_no_collection(env):
    client, backend, *_ = await env()
    await client.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    run = next(c for _, c in backend.execs if "exec runuser" in c)
    assert f"OUTPUT_DIR={UserEnv(files_cfg(), uid_of('alice')).outputs_dir}" in run

    off = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}})
    app = create_app(off, backend=(b2 := FakeSandboxBackend()), redis_client=fakeredis.aioredis.FakeRedis(),
                     run_reaper=False)
    async with LifespanManager(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/agent/invoke", headers=ALICE, json={"prompt": "go"})
    assert not any("--blocking-factor=1" in c for _, c in b2.execs)


# ---- the symlink problem: root never touches a path the user controls ---------------------------

def test_root_side_commands_only_touch_root_owned_paths():
    cfg = files_cfg()
    e = UserEnv(cfg, uid_of("alice"))
    state = cfg.isolation.state_root
    for script in (e.run_turn(with_obo=True, with_inputs=True), e.reset_inputs(), e.drop_obo(),
                   e.drop_bundle()):
        assert "chown" not in script
        for line in script.splitlines():
            if line.startswith(("set ", "cd ", "exec ")):
                continue
            for path in re.findall(r"(?<![\w$])(/[\w./-]+)", line):
                assert path.startswith(state), f"root command touches {path}: {line}"
    # the one place a user path is used by root is the starting directory, before privileges drop
    assert f"cd {e.home}" in e.run_turn() and "exec runuser" in e.run_turn()


def test_request_and_token_files_are_not_under_the_users_home():
    e = UserEnv(files_cfg(), uid_of("alice"))
    for p in (e.request_path, e.obo_path, e.in_dir, e.bundle_path):
        assert p.startswith("/var/lib/smux/") and not p.startswith(e.home)


def test_collector_runs_the_archiver_as_the_user_and_writes_to_a_root_owned_path():
    e = UserEnv(files_cfg(), uid_of("alice"))
    s = e.collect_outputs(5000)
    assert f"runuser -u {e.user} --" in s and f"> {e.bundle_path}" in s
    assert "-type f" in s and "head -c 5001" in s and "--blocking-factor=1" in s
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0
    for script in (e.provision(), e.reset_inputs(), e.clear_outputs(), e.run_turn(True, True), e.wipe()):
        assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="the collector installs root-owned directories")
def test_collector_really_skips_symlinks_and_caps_the_bundle(tmp_path):
    cfg = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                                 "isolation": {"home_root": str(tmp_path / "home"),
                                               "state_root": str(tmp_path / "state")},
                                 "files": {"enabled": True, "store": "local"}})
    e = UserEnv(cfg, uid_of("alice"))
    out = tmp_path / "home" / e.user / "outputs"
    (out / "sub").mkdir(parents=True)
    (out / "a.txt").write_text("alpha")
    (out / "sub" / "b.txt").write_text("beta")
    secret = tmp_path / "other-users-secret.txt"
    secret.write_text("TOP SECRET")
    (out / "evil").symlink_to(secret)                       # the attack: a link to someone else's file
    (out / "evil-dir").symlink_to(tmp_path)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "runuser").write_text('#!/bin/sh\nshift 3\nexec "$@"\n')   # stands in for the privilege drop
    (shim / "runuser").chmod(0o755)
    (tmp_path / "state").mkdir()

    r = subprocess.run(["bash", "-c", e.collect_outputs(1_000_000)], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    data = open(e.bundle_path, "rb").read()
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert sorted(names) == ["./a.txt", "./sub/b.txt"] and b"TOP SECRET" not in data

    r = subprocess.run(["bash", "-c", e.collect_outputs(600)], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})
    assert os.path.getsize(e.bundle_path) == 601            # cap + 1: the manager sees it is truncated
