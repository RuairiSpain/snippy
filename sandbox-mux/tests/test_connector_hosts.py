"""egress.allow_connector_hosts: allow exactly the connector endpoints the service reports."""

from types import SimpleNamespace as NS

import pytest

from sandbox_mux.backends.azure import AzureSandboxBackend, build_egress_policy, connector_hosts_from
from sandbox_mux.config import ConnectorsConfig, Config, EgressConfig
from sandbox_mux.pool import PoolBusy
from test_triggers import EMAIL, U, cb, make_app, subscribe, tcfg   # noqa: F401
from test_trigger_pool import interactive_turn, trigger_turn         # noqa: F401

BASE = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/connectorGateways/ns"
MCP = f"{BASE}/mcpserverconfigs/mail"
GH = f"{BASE}/mcpserverconfigs/github"
API = f"{BASE}/connections/crm"


def gc(mcp=None, conn=None):
    return NS(mcp_runtime_url=mcp, connection_runtime_url=conn)


def cfg(egress=None, connectors=None, **top) -> Config:
    return Config.model_validate({
        "backend": "fake", "auth": {"mode": "dev"},
        "pool": {"egress": egress or {"mode": "disabled", "allow_connector_hosts": True}},
        "connectors": {"attach": connectors or [MCP]}, **top})


# ---- configuration -------------------------------------------------------------------------------------

def test_off_by_default():
    assert EgressConfig().allow_connector_hosts is False


@pytest.mark.parametrize("mode, ok", [("disabled", True), ("allowlist", True), ("enabled", False), ("platform_default", False)])
def test_only_deny_by_default_modes_can_use_it(mode, ok):
    extra = {"allow_hosts": ["a.example.com"]} if mode == "allowlist" else {}
    if ok:
        assert EgressConfig(mode=mode, allow_connector_hosts=True, **extra).allow_connector_hosts
    else:
        with pytest.raises(ValueError, match="disabled or allowlist"):
            EgressConfig(mode=mode, allow_connector_hosts=True)


def test_it_needs_connectors_attached_to_the_pool():
    with pytest.raises(ValueError, match="no connectors are attached"):
        Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                               "pool": {"egress": {"mode": "disabled", "allow_connector_hosts": True}}})


def test_it_changes_the_policy_fingerprint_so_live_sandboxes_are_updated():
    assert EgressConfig(mode="disabled").fingerprint() != EgressConfig(mode="disabled", allow_connector_hosts=True).fingerprint()


def test_it_can_be_set_from_the_environment(tmp_path, monkeypatch):
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"}, "connectors": {"attach": [MCP]},
                                 "pool": {"egress": {"mode": "disabled"}}}))
    monkeypatch.setenv("SANDBOX_MUX_ENV_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("SANDBOX_MUX_EGRESS_CONNECTOR_HOSTS", "true")
    assert Config.load(p).pool.egress.allow_connector_hosts is True


# ---- reading the hosts --------------------------------------------------------------------------------------

def test_hosts_come_from_both_runtime_urls_deduplicated_and_lowercased():
    sbx = NS(gateway_connections=[
        gc(mcp="https://Mail-GW.EastUS.example.net/mcp/abc", conn="https://crm.example.net:8443/v1"),
        gc(mcp="https://mail-gw.eastus.example.net/other"),
        gc()])
    assert connector_hosts_from(sbx) == ["crm.example.net", "mail-gw.eastus.example.net"]


@pytest.mark.parametrize("url", [
    "http://gw.example.net/mcp",            # not https
    "https://10.0.0.5/mcp", "https://[::1]/mcp",   # addresses, not names
    "https://localhost/mcp",                 # no dot
    "https://*.example.net/mcp", "ftp://gw.example.net/x", "https://",
    "https://bad_host.example.net/", "not a url", "https://a..b.example.net/",
])
def test_anything_that_is_not_an_https_dns_name_is_ignored(url):
    assert connector_hosts_from(NS(gateway_connections=[gc(mcp=url)])) == []


def test_a_sandbox_with_no_connections_has_no_hosts():
    assert connector_hosts_from(NS(gateway_connections=None)) == [] == connector_hosts_from(NS())


# ---- the policy -----------------------------------------------------------------------------------------------

def rules(policy):
    return [(r["name"], r["match"]["host"]) for r in policy.as_dict()["http"].get("rules", [])]


def test_deny_by_default_policy_gets_exactly_the_connector_hosts():
    p = build_egress_policy(EgressConfig(mode="disabled", allow_connector_hosts=True), ["gw.example.net", "crm.example.net"])
    assert p.as_dict()["http"]["defaultAction"] == "Deny"
    assert rules(p) == [("allow-connector-1", "gw.example.net"), ("allow-connector-2", "crm.example.net")]


def test_allowlist_keeps_its_hosts_first_and_does_not_repeat_one():
    e = EgressConfig(mode="allowlist", allow_hosts=["Workiq.svc.cloud.microsoft", "gw.example.net"], allow_connector_hosts=True)
    assert rules(build_egress_policy(e, ["gw.example.net", "crm.example.net"])) == [
        ("allow-1", "Workiq.svc.cloud.microsoft"), ("allow-2", "gw.example.net"), ("allow-connector-1", "crm.example.net")]


def test_the_hosts_are_ignored_unless_the_option_is_on():
    assert rules(build_egress_policy(EgressConfig(mode="disabled"), ["gw.example.net"])) == []
    assert build_egress_policy(EgressConfig(mode="platform_default"), ["gw.example.net"]) is None


# ---- the Azure backend ---------------------------------------------------------------------------------------

class Poller:
    async def result(self):
        return NS(sandbox_id="sbx-1")


class SandboxClient:
    def __init__(self, c):
        self.c = c

    async def set_egress_policy(self, policy):
        self.c.updates.append(policy)


class Client:
    def __init__(self, gateway_connections):
        self.gcs, self.created, self.updates, self.deleted, self.order = gateway_connections, None, [], [], []

    async def begin_create_sandbox(self, **kw):
        self.created = kw
        self.order.append("create")
        return Poller()

    async def get_sandbox(self, sid):
        self.order.append("get")
        return NS(gateway_connections=self.gcs)

    def get_sandbox_client(self, sid):
        return SandboxClient(self)

    async def delete_sandbox(self, sid):
        self.deleted.append(sid)


async def test_a_new_sandbox_starts_strict_then_is_widened_to_its_connector_hosts():
    c = Client([gc(mcp="https://mail-gw.example.net/mcp", conn="https://crm.example.net/v1")])
    b = AzureSandboxBackend(cfg(connectors=[MCP, API]))
    b._client = c
    assert await b.create({"pool": "x"}) == "sbx-1"
    first = c.created["egress_policy"].as_dict()["http"]
    assert first["defaultAction"] == "Deny" and not first.get("rules")           # nothing allowed at creation
    assert c.created["connections"] == [MCP, API]
    assert len(c.updates) == 1 and rules(c.updates[0]) == [("allow-connector-1", "crm.example.net"), ("allow-connector-2", "mail-gw.example.net")]
    assert c.order == ["create", "get"] and c.deleted == []


async def test_if_the_service_reports_no_endpoint_the_sandbox_is_deleted_and_creation_fails():
    c = Client([])
    b = AzureSandboxBackend(cfg())
    b._client = c
    with pytest.raises(RuntimeError, match="reported no connector endpoints"):
        await b.create({})
    assert c.deleted == ["sbx-1"] and c.updates == []                          # never left running under a policy it did not ask for


async def test_unusable_endpoints_count_as_none():
    c = Client([gc(mcp="http://insecure.example.net/mcp"), gc(conn="https://10.1.2.3/x")])
    b = AzureSandboxBackend(cfg())
    b._client = c
    with pytest.raises(RuntimeError, match="no connector endpoints"):
        await b.create({})
    assert c.deleted == ["sbx-1"]


async def test_without_the_option_there_is_no_lookup_and_no_second_call():
    c = Client([gc(mcp="https://gw.example.net/mcp")])
    b = AzureSandboxBackend(cfg(egress={"mode": "disabled"}))
    b._client = c
    await b.create({})
    assert c.order == ["create"] and c.updates == []


async def test_updating_a_live_sandbox_also_includes_its_connector_hosts():
    c = Client([gc(mcp="https://gw.example.net/mcp")])
    b = AzureSandboxBackend(cfg(egress={"mode": "allowlist", "allow_hosts": ["a.example.com"], "allow_connector_hosts": True}))
    b._client = c
    await b.set_egress("sbx-9")
    assert rules(c.updates[0]) == [("allow-1", "a.example.com"), ("allow-connector-1", "gw.example.net")]
    assert await b.connector_hosts("sbx-9") == ["gw.example.net"]


async def test_updating_fails_closed_when_the_endpoints_cannot_be_found():
    b = AzureSandboxBackend(cfg())
    b._client = Client(None)
    with pytest.raises(RuntimeError, match="no connector endpoints"):
        await b.set_egress("sbx-9")
    assert b._client.updates == []                                           # the existing policy is left as it was


# ---- in the pool ---------------------------------------------------------------------------------------------------

def use_view(r, backend):
    view = backend.with_config(r.cfg)
    r.pool.backend = r.service.backend = r.reaper.backend = view
    return view


async def test_pool_sandboxes_are_registered_with_their_connector_hosts(make_replica, backend):
    r = make_replica()
    r.cfg.connectors = ConnectorsConfig(attach=[MCP, API])
    r.cfg.pool.egress = EgressConfig(mode="allowlist", allow_hosts=["a.example.com"], allow_connector_hosts=True)
    use_view(r, backend)
    sid, _ = await r.pool.acquire("a" * 32)
    assert backend.sandboxes[sid]["egress_hosts"] == ["a.example.com", "crm.gateway.test", "mail.gateway.test"]


async def test_a_sandbox_the_service_gives_no_endpoints_is_never_registered(make_replica, backend):
    r = make_replica()
    r.cfg.connectors = ConnectorsConfig(attach=[MCP])
    r.cfg.pool.egress = EgressConfig(mode="disabled", allow_connector_hosts=True)
    r.cfg.session.queue_timeout_seconds = 1
    use_view(r, backend)
    backend.gateway_hosts = []
    with pytest.raises(PoolBusy):
        await r.pool.acquire("a" * 32)
    assert backend.sandboxes == {} and await r.store.states() == {}


async def test_turning_it_on_later_updates_the_sandboxes_that_already_exist(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=3)
    r.cfg.connectors = ConnectorsConfig(attach=[MCP])
    r.cfg.pool.egress = EgressConfig(mode="disabled")
    use_view(r, backend)
    a, _ = await r.pool.acquire("a" * 32)
    b, _ = await r.pool.acquire("b" * 32)
    assert backend.sandboxes[a]["egress_hosts"] == []
    r.cfg.pool.egress = EgressConfig(mode="disabled", allow_connector_hosts=True)       # the operator opts in
    await r.reaper.reconcile_egress()
    assert backend.egress_applied == [a, b]
    assert backend.sandboxes[a]["egress_hosts"] == backend.sandboxes[b]["egress_hosts"] == ["mail.gateway.test"]
    await r.reaper.reconcile_egress()
    assert backend.egress_applied == [a, b]                                              # applied once, not every sweep


async def test_a_failed_update_leaves_the_old_policy_and_is_retried(make_replica, backend):
    r = make_replica()
    r.cfg.connectors = ConnectorsConfig(attach=[MCP])
    r.cfg.pool.egress = EgressConfig(mode="disabled")
    use_view(r, backend)
    sid, _ = await r.pool.acquire("a" * 32)
    r.cfg.pool.egress = EgressConfig(mode="disabled", allow_connector_hosts=True)
    backend.gateway_hosts = []                                                           # the service reports nothing
    await r.reaper.reconcile_egress()
    assert backend.sandboxes[sid]["egress_hosts"] == [] and backend.egress_applied == []
    backend.gateway_hosts = None
    await r.reaper.reconcile_egress()
    assert backend.sandboxes[sid]["egress_hosts"] == ["mail.gateway.test"]


async def test_each_pool_allows_only_its_own_connectors_hosts(make_app):
    data = tcfg().model_dump()
    data["pool"] = {"name": "agents", "max_sandboxes": 3, "users_per_sandbox": 3,
                    "egress": {"mode": "allowlist", "allow_hosts": ["workiq.svc.cloud.microsoft"], "allow_connector_hosts": True}}
    data["connectors"] = {"attach": [MCP]}
    data["triggers"]["pool"] = {"separate": True, "name": "triggers", "connectors": {"attach": [GH]},
                                "settings": {"egress": {"mode": "allowlist", "allow_hosts": ["graph.microsoft.com"],
                                                        "allow_connector_hosts": True}}}
    client, app, backend, _ = await make_app(Config.model_validate(data))
    await subscribe(client)
    await interactive_turn(client)
    await trigger_turn(client, app)
    by_pool = {s["pool"]: s for s in backend.sandboxes.values()}
    assert by_pool["agents"]["egress_hosts"] == ["workiq.svc.cloud.microsoft", "mail.gateway.test"]
    assert by_pool["triggers"]["egress_hosts"] == ["graph.microsoft.com", "github.gateway.test"]   # not the interactive connector's host


def test_the_triggers_pool_needs_its_own_connectors_for_the_option():
    data = tcfg().model_dump()
    data["connectors"] = {"attach": [MCP]}
    data["triggers"]["pool"] = {"separate": True, "settings": {"egress": {
        "mode": "allowlist", "allow_hosts": ["graph.microsoft.com"], "allow_connector_hosts": True}}}
    with pytest.raises(ValueError, match=r"(?s)triggers\.pool.*no connectors are attached"):
        Config.model_validate(data)                      # the interactive connector is not inherited
    data["triggers"]["pool"]["connectors"] = {"attach": [GH]}
    Config.model_validate(data)


async def test_status_shows_the_option(make_app):
    data = tcfg().model_dump()
    data["auth"]["admin_oids"] = ["alice"]
    data["connectors"] = {"attach": [MCP]}
    data["pool"] = {"name": "agents", "max_sandboxes": 2, "users_per_sandbox": 2,
                    "egress": {"mode": "allowlist", "allow_hosts": ["graph.microsoft.com"], "allow_connector_hosts": True}}
    data["triggers"]["pool"] = {"separate": True, "settings": {"egress": {"mode": "allowlist", "allow_hosts": ["graph.microsoft.com"]}}}
    client, *_ = await make_app(Config.model_validate(data))
    s = (await client.get("/v1/pool/status", headers=U("alice"))).json()
    assert s["egress_connector_hosts"] is True and s["triggers_pool"]["egress_connector_hosts"] is False
