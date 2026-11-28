import subprocess

import pytest
import yaml

from sandbox_mux.backends.azure import AzureSandboxBackend
from sandbox_mux.config import Config, ConnectorsConfig
from sandbox_mux.service import UserEnv

BASE = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/connectorGateways/ns"
MCP = f"{BASE}/mcpserverconfigs/office365Mcp"
API = f"{BASE}/connections/office365Conn"
GH = f"{BASE}/mcpserverconfigs/github"


def cfg(**connectors) -> Config:
    return Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                                  "pool": {"max_sandboxes": 4, "users_per_sandbox": 1},
                                  "connectors": connectors})


# ---- configuration ---------------------------------------------------------------------------

def test_none_by_default():
    c = cfg()
    assert c.connectors.ids == [] and c.connectors.fingerprint() == ""


def test_attach_by_resource_id_alias_or_a_mix_without_duplicates():
    c = cfg(available={"sharepoint": MCP, "gh": GH}, attach=["sharepoint", API, MCP.upper(), "gh"])
    assert c.connectors.ids == [MCP, API, GH]


def test_fingerprint_ignores_order_and_changes_with_the_set():
    a = cfg(attach=[MCP, API]).connectors.fingerprint()
    assert a and a == cfg(attach=[API, MCP]).connectors.fingerprint()
    assert a != cfg(attach=[MCP]).connectors.fingerprint()


@pytest.mark.parametrize("connectors, message", [
    ({"attach": ["nope"]}, "neither a resource ID"),
    ({"attach": ["/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/x/y"]}, "not a connector resource ID"),
    ({"attach": [f"{BASE}/mcpserverconfigs"]}, "not a connector resource ID"),
    ({"available": {"a b": MCP}}, "invalid alias"),
    ({"available": {"x": "https://evil.example/mcp"}}, "not a connector resource ID"),
    ({"config_file": "/root/.copilot/mcp-config.json"}, "attach is empty"),
])
def test_invalid_connector_settings_are_rejected(connectors, message):
    with pytest.raises(ValueError, match=message):
        cfg(**connectors)


def test_more_than_ten_is_rejected():
    ids = [f"{BASE}/mcpserverconfigs/m{i}" for i in range(11)]
    assert len(cfg(attach=ids[:10]).connectors.ids) == 10
    with pytest.raises(ValueError, match="at most 10"):
        cfg(attach=ids)


def test_config_file_needs_unix_user():
    with pytest.raises(ValueError, match="unix_user"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                               "isolation": {"mode": "home_only"},
                               "connectors": {"attach": [MCP], "config_file": "/root/x.json"}})


def test_selected_from_env_by_alias_or_by_id(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"},
                                 "connectors": {"available": {"sharepoint": MCP, "gh": GH}}}))
    monkeypatch.setenv("SANDBOX_MUX_ENV_FILE", str(tmp_path / "none"))
    assert Config.load(p).connectors.ids == []
    monkeypatch.setenv("SANDBOX_MUX_CONNECTORS", "sharepoint, gh")
    assert Config.load(p).connectors.ids == [MCP, GH]
    monkeypatch.setenv("SANDBOX_MUX_CONNECTORS", f"{API},{MCP}")
    assert Config.load(p).connectors.ids == [API, MCP]
    monkeypatch.setenv("SANDBOX_MUX_CONNECTOR_RETIRE_GRACE_SECONDS", "120")
    assert Config.load(p).connectors.retire_grace_seconds == 120


# ---- creation ------------------------------------------------------------------------------------

async def test_connections_are_passed_when_a_sandbox_is_created():
    seen = []

    class Poller:
        async def result(self):
            return type("S", (), {"sandbox_id": "sbx-1"})()

    class Client:
        async def begin_create_sandbox(self, **kw):
            seen.append(kw)
            return Poller()

    for c, expected in ((cfg(attach=[MCP, API]), [MCP, API]), (cfg(), None)):
        b = AzureSandboxBackend(c)
        b._client = Client()
        await b.create({"pool": "x"})
        assert seen[-1]["connections"] == expected


async def test_each_sandbox_records_the_connector_list_it_was_created_with(make_replica):
    r = make_replica(users_per_sandbox=1)
    r.cfg.connectors = ConnectorsConfig(attach=[MCP])
    sid, _ = await r.pool.acquire("a" * 32)
    assert (await r.store.sandbox_meta(sid))["connections"] == r.cfg.connectors.fingerprint()


# ---- changing the list replaces sandboxes ---------------------------------------------------------

async def test_sandboxes_created_before_this_feature_are_not_retired_when_none_are_configured(make_replica):
    r = make_replica(users_per_sandbox=1)
    await r.service.run_turn("a" * 32, "c", "x")
    await r.reaper.retire_stale_connections()
    assert set((await r.store.states()).values()) == {"ready"}


async def test_changed_list_retires_sandboxes_but_users_keep_working_until_idle(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=4)
    a = await r.service.run_turn("a" * 32, "c", "x")
    b = await r.service.run_turn("b" * 32, "c", "x")
    r.cfg.connectors = ConnectorsConfig(attach=[MCP], retire_grace_seconds=3600)

    await r.reaper.retire_stale_connections()
    assert set((await r.store.states()).values()) == {"retiring"}

    # Existing users stay on their sandbox, with their files.
    again = await r.service.run_turn("a" * 32, "c", "y")
    assert again.sandbox_id == a.sandbox_id
    # A new user gets a sandbox with the new list, not a retiring one.
    c = await r.service.run_turn("c" * 32, "c", "x")
    assert c.sandbox_id not in (a.sandbox_id, b.sandbox_id)
    assert (await r.store.sandbox_meta(c.sandbox_id))["connections"] == r.cfg.connectors.fingerprint()

    # Retiring sandboxes with users in them are left alone ...
    await r.reaper.clean_up_retiring()
    assert {a.sandbox_id, b.sandbox_id} <= set(backend.sandboxes)
    # ... and removed as soon as they are empty, regardless of min_sandboxes.
    await r.service.end_session("a" * 32)
    await r.reaper.clean_up_retiring()
    assert a.sandbox_id not in backend.sandboxes and b.sandbox_id in backend.sandboxes


async def test_grace_period_forces_removal_and_users_are_moved(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=4)
    first = await r.service.run_turn("a" * 32, "c", "x")
    r.cfg.connectors = ConnectorsConfig(attach=[MCP], retire_grace_seconds=0)
    await r.reaper.retire_stale_connections()
    await r.reaper.clean_up_retiring()                    # grace already over
    assert first.sandbox_id not in backend.sandboxes

    nxt = await r.service.run_turn("a" * 32, "c", "y")    # placed on a sandbox with the new list
    assert nxt.sandbox_id != first.sandbox_id and nxt.reply == "echo: y"
    assert (await r.store.sandbox_meta(nxt.sandbox_id))["connections"] == r.cfg.connectors.fingerprint()
    assert [m["content"] for m in await r.store.history("a" * 32, "c", 10)][:2] == ["x", "echo: x"]


async def test_removing_every_connector_also_retires_the_old_sandboxes(make_replica, backend):
    r = make_replica(users_per_sandbox=1)
    r.cfg.connectors = ConnectorsConfig(attach=[MCP])
    sid = (await r.service.run_turn("a" * 32, "c", "x")).sandbox_id
    r.cfg.connectors = ConnectorsConfig()
    await r.reaper.retire_stale_connections()
    assert (await r.store.states())[sid] == "retiring"


async def test_the_warm_pool_is_rebuilt_with_the_new_list(make_replica, backend):
    r = make_replica(min_sandboxes=1, max_sandboxes=3)
    await r.pool.warm()
    old = next(iter(backend.sandboxes))
    r.cfg.connectors = ConnectorsConfig(attach=[API])
    await r.reaper.retire_stale_connections()
    await r.reaper.clean_up_retiring()
    await r.pool.warm()
    assert old not in backend.sandboxes and len(backend.sandboxes) == 1
    new = next(iter(backend.sandboxes))
    assert (await r.store.sandbox_meta(new))["connections"] == r.cfg.connectors.fingerprint()


async def test_retiring_sandboxes_do_not_count_toward_capacity_for_new_users(make_replica, backend):
    r = make_replica(users_per_sandbox=2, max_sandboxes=1)
    await r.service.run_turn("a" * 32, "c", "x")
    r.cfg.connectors = ConnectorsConfig(attach=[MCP], retire_grace_seconds=3600)
    await r.reaper.retire_stale_connections()
    r.cfg.session.queue_timeout_seconds = 1
    from sandbox_mux.pool import PoolBusy
    # max_sandboxes is 1 and it is still in use by a: the new user waits rather than joining it
    with pytest.raises(PoolBusy):
        await r.pool.acquire("b" * 32)


# ---- the MCP client config -----------------------------------------------------------------------

def test_mcp_config_is_copied_by_root_into_the_request_dir_and_exposed_by_env():
    src = "/root/.copilot/mcp-config.json"
    e = UserEnv(cfg(attach=[MCP], config_file=src), "a" * 32)
    s = e.run_turn()
    assert f"install -m 0640 -o root -g {e.user} {src} {e.mcp_path}" in s
    assert f"MCP_CONFIG_FILE={e.mcp_path}" in s
    assert e.mcp_path.startswith("/var/lib/smux/req/") and not e.mcp_path.startswith(e.home)
    assert s.index("install -m 0640") < s.index("exec runuser")
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


def test_no_mcp_config_by_default():
    s = UserEnv(cfg(attach=[MCP]), "a" * 32).run_turn()
    assert "mcp-config" not in s and "MCP_CONFIG_FILE" not in s
