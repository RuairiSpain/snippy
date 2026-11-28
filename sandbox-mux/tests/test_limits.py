import subprocess

import pytest

from sandbox_mux.config import Config, parse_cpu, parse_size
from sandbox_mux.limits import bootstrap_check, snippet
from sandbox_mux.pool import BOOTSTRAP
from sandbox_mux.service import UserEnv


def cfg(**limits) -> Config:
    return Config.model_validate({
        "backend": "fake", "auth": {"mode": "dev"},
        "pool": {"cpu": "4000m", "memory": "8Gi", "users_per_sandbox": 4},
        "limits": limits,
    })


def sh(script: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True, **kw)


def fake_cgroup(tmp_path, controllers="cpu memory pids io"):
    root = tmp_path / "cg"
    root.mkdir()
    (root / "cgroup.controllers").write_text(controllers + "\n")
    (root / "cgroup.subtree_control").write_text("")
    return root


def test_sizes_and_cpu_parse():
    assert parse_size("512Mi") == 512 * 2**20 and parse_size("2G") == 2 * 10**9
    assert parse_size("1.5Gi") == int(1.5 * 2**30) and parse_size("100") == 100
    assert parse_cpu("500m") == 0.5 and parse_cpu("2") == 2.0
    with pytest.raises(ValueError):
        parse_size("lots")


def test_no_limits_by_default_and_nothing_is_emitted():
    c = cfg()
    assert c.limits.mode == "none" and snippet(c, "u1") == "" and bootstrap_check(c) == ""
    assert "cgroup" not in UserEnv(c, "a" * 32).run_turn()


@pytest.mark.parametrize("limits, message", [
    ({"memory": "1Gi"}, "limits.mode is none"),
    ({"mode": "cgroup"}, "at least one"),
    ({"mode": "ulimit", "cpu": 1}, "cannot limit CPU"),
    ({"mode": "cgroup", "memory": "16Gi"}, "larger than the whole sandbox"),
    ({"mode": "cgroup", "cpu": 9}, "larger than the whole sandbox"),
    ({"mode": "cgroup", "cpu": 0}, "greater than 0"),
])
def test_invalid_limits_are_rejected(limits, message):
    with pytest.raises(ValueError, match=message):
        cfg(**limits)


def test_ulimit_requires_per_user_uids():
    with pytest.raises(ValueError, match="unix_user"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                               "isolation": {"mode": "home_only"},
                               "limits": {"mode": "ulimit", "pids": 100}})


def test_overcommit_is_reported():
    c = cfg(mode="cgroup", memory="4Gi", cpu=2)            # 4 users x (4Gi, 2 cores) on (8Gi, 4 cores)
    assert c.overcommit() == {"memory": 2.0, "cpu": 2.0}
    assert cfg(mode="cgroup", memory="2Gi", cpu=1).overcommit() == {"memory": 1.0, "cpu": 1.0}


def test_cgroup_script_writes_each_limit_and_joins_the_cgroup(tmp_path):
    root = fake_cgroup(tmp_path)
    c = cfg(mode="cgroup", cpu=1.5, memory="2Gi", pids=256, cgroup_root=str(root))
    r = sh(snippet(c, "uabc"))
    assert r.returncode == 0, r.stderr
    leaf = root / "smux" / "uabc"
    assert (leaf / "cpu.max").read_text().strip() == "150000 100000"
    assert (leaf / "memory.max").read_text().strip() == str(2 * 2**30)
    assert (leaf / "memory.swap.max").read_text().strip() == "0"
    assert (leaf / "pids.max").read_text().strip() == "256"
    assert (leaf / "cgroup.procs").read_text().strip().isdigit()
    assert (root / "smux" / "cgroup.subtree_control").read_text().strip() == "+cpu +memory +pids"


def test_only_configured_limits_are_written(tmp_path):
    root = fake_cgroup(tmp_path)
    r = sh(snippet(cfg(mode="cgroup", memory="1Gi", cgroup_root=str(root)), "u1"))
    assert r.returncode == 0, r.stderr
    leaf = root / "smux" / "u1"
    assert (leaf / "memory.max").exists() and not (leaf / "cpu.max").exists()
    assert not (leaf / "pids.max").exists()


def test_cgroup_setup_is_idempotent_across_turns(tmp_path):
    root = fake_cgroup(tmp_path)
    s = snippet(cfg(mode="cgroup", cpu=1, memory="1Gi", cgroup_root=str(root)), "u1")
    assert sh(s).returncode == 0 and sh(s).returncode == 0


def test_limits_are_applied_before_the_agent_starts():
    c = cfg(mode="cgroup", cpu=1, memory="1Gi")
    script = UserEnv(c, "a" * 32).run_turn()
    assert script.index("cgroup.procs") < script.index("exec runuser")
    assert script.index("memory.max") < script.index("exec runuser")
    assert sh("true").returncode == 0
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


def test_failure_to_apply_limits_stops_the_turn_instead_of_running_unlimited(tmp_path):
    # Hierarchy missing: with `set -e` the script must abort before reaching `exec runuser`.
    c = cfg(mode="cgroup", memory="1Gi", cgroup_root=str(tmp_path / "missing" / "cg"))
    script = UserEnv(c, "a" * 32).run_turn().replace("exec runuser", "echo REACHED; exec runuser")
    r = sh(script)
    assert r.returncode != 0 and "REACHED" not in r.stdout


def test_ulimit_mode_sets_process_and_address_space_limits():
    s = snippet(cfg(mode="ulimit", pids=100, memory="1Gi"), "u1")
    assert "ulimit -u 100" in s and f"ulimit -v {2**30 // 1024}" in s


def test_bootstrap_rejects_a_sandbox_without_usable_cgroups(tmp_path):
    root = fake_cgroup(tmp_path, controllers="cpu memory pids")
    c = cfg(mode="cgroup", cpu=1, memory="1Gi", cgroup_root=str(root))
    assert sh(bootstrap_check(c)).returncode == 0

    (root / "cgroup.controllers").write_text("cpu memory\n")              # pids missing, none asked
    assert sh(bootstrap_check(cfg(mode="cgroup", pids=10, cgroup_root=str(root)))).returncode == 4
    assert sh(bootstrap_check(c)).returncode == 0                           # cpu+memory still fine

    empty = tmp_path / "none"
    empty.mkdir()
    r = sh(bootstrap_check(cfg(mode="cgroup", cpu=1, cgroup_root=str(empty))))
    assert r.returncode == 4 and "cgroup v2" in r.stderr


def test_bootstrap_script_includes_the_check_and_parses():
    c = cfg(mode="cgroup", cpu=1)
    script = BOOTSTRAP.replace("{home_root}", "/home").replace("{limits_check}", bootstrap_check(c))
    assert "cgroup.controllers" in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0


async def test_a_sandbox_that_cannot_enforce_limits_is_not_registered(make_replica, backend):
    from sandbox_mux.backends.base import ExecOutcome
    from sandbox_mux.pool import PoolBusy
    backend.handler = lambda sid, cmd: (ExecOutcome(4, "", "no cgroup v2") if "cgroup.controllers" in cmd
                                        else None)
    r = make_replica()
    r.cfg.limits = cfg(mode="cgroup", cpu=1).limits
    r.cfg.session.queue_timeout_seconds = 1
    with pytest.raises(PoolBusy):
        await r.pool.acquire("a" * 32)
    assert backend.sandboxes == {}                       # deleted, not left running
    assert await r.store.states() == {}


def test_env_names(monkeypatch, tmp_path):
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"},
                                 "pool": {"cpu": "4", "memory": "8Gi"}}))
    for k, v in {"SANDBOX_MUX_LIMITS_MODE": "cgroup", "SANDBOX_MUX_USER_CPU": "0.5",
                 "SANDBOX_MUX_USER_MEMORY": "1Gi", "SANDBOX_MUX_USER_PIDS": "200"}.items():
        monkeypatch.setenv(k, v)
    c = Config.load(p)
    assert (c.limits.mode, c.limits.cpu, c.limits.memory, c.limits.pids) == ("cgroup", 0.5, "1Gi", 200)
