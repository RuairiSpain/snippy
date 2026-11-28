import pytest

from sandbox_mux.backends.azure import AzureSandboxBackend, build_egress_policy
from sandbox_mux.config import Config, EgressConfig


def test_default_is_deny_all():
    assert Config.model_validate({"backend": "fake", "auth": {"mode": "dev"}}).pool.egress.mode == "disabled"


def test_allowlist_needs_hosts_and_hosts_need_allowlist_mode():
    with pytest.raises(ValueError, match="needs allow_hosts"):
        EgressConfig(mode="allowlist")
    with pytest.raises(ValueError, match="only used with"):
        EgressConfig(mode="enabled", allow_hosts=["a.com"])


@pytest.mark.parametrize("bad", ["http://a.com", "a.com/x", "*", "a b.com", "-a.com", "a..com/"])
def test_bad_host_patterns_are_rejected(bad):
    with pytest.raises(ValueError):
        EgressConfig(mode="allowlist", allow_hosts=[bad])


def test_hosts_may_be_comma_separated():
    e = EgressConfig(mode="allowlist", allow_hosts="api.a.com, *.b.com")
    assert e.allow_hosts == ["api.a.com", "*.b.com"]


def test_disabled_denies_all_http():
    d = build_egress_policy(EgressConfig(mode="disabled")).as_dict()
    assert d["http"]["defaultAction"] == "Deny" and not d["http"].get("rules")
    assert d["http"]["enforcementMode"] == "Enforced" and "transportRules" not in d


def test_allowlist_denies_by_default_and_allows_listed_hosts_in_order():
    d = build_egress_policy(EgressConfig(mode="allowlist",
                                         allow_hosts=["api.a.com", "*.b.com"])).as_dict()
    assert d["http"]["defaultAction"] == "Deny"
    assert [(r["match"]["host"], r["action"]["type"]) for r in d["http"]["rules"]] == [
        ("api.a.com", "Allow"), ("*.b.com", "Allow")]


def test_enabled_allows_everything():
    assert build_egress_policy(EgressConfig(mode="enabled")).as_dict()["http"]["defaultAction"] == "Allow"


def test_platform_default_sends_no_policy():
    assert build_egress_policy(EgressConfig(mode="platform_default")) is None


def test_audit_inspection_and_non_http_options():
    d = build_egress_policy(EgressConfig(mode="disabled", audit_only=True, traffic_inspection="Partial",
                                         block_non_http=True)).as_dict()
    assert d["http"]["enforcementMode"] == "Audit" and d["http"]["trafficInspection"] == "Partial"
    assert d["transportRules"]["defaultAction"] == "Deny"


def test_fingerprint_changes_with_policy_only():
    a = EgressConfig(mode="disabled").fingerprint()
    assert a == EgressConfig(mode="disabled").fingerprint()
    assert a != EgressConfig(mode="enabled").fingerprint()
    assert EgressConfig(mode="allowlist", allow_hosts=["a.com"]).fingerprint() != \
        EgressConfig(mode="allowlist", allow_hosts=["b.com"]).fingerprint()


async def test_azure_backend_passes_the_policy_at_creation_and_on_update():
    cfg = Config.model_validate({"backend": "fake", "auth": {"mode": "dev"},
                                 "pool": {"egress": {"mode": "allowlist", "allow_hosts": ["a.com"]}}})
    seen = {}

    class Poller:
        async def result(self):
            return type("S", (), {"sandbox_id": "sbx-1"})()

    class SandboxClient:
        async def set_egress_policy(self, policy):
            seen["updated"] = policy

    class Client:
        async def begin_create_sandbox(self, **kw):
            seen["created"] = kw
            return Poller()

        def get_sandbox_client(self, sid):
            return SandboxClient()

    b = AzureSandboxBackend(cfg)
    b._client = Client()
    assert await b.create({"pool": "x"}) == "sbx-1"
    assert seen["created"]["egress_policy"].as_dict()["http"]["defaultAction"] == "Deny"
    await b.set_egress("sbx-1")
    assert seen["updated"].as_dict()["http"]["rules"][0]["match"]["host"] == "a.com"


# ---- live sandboxes follow config changes ---------------------------------------------

async def test_policy_change_is_applied_to_existing_sandboxes_once(make_replica, backend):
    r = make_replica(users_per_sandbox=1, max_sandboxes=3)
    for u in "ab":
        await r.service.run_turn(u * 32, "c", "x")
    await r.reaper.reconcile_egress()
    assert backend.egress_applied == []            # created under the current policy

    r.cfg.pool.egress = EgressConfig(mode="enabled")     # operator flips the setting
    await r.reaper.reconcile_egress()
    assert sorted(backend.egress_applied) == sorted(backend.sandboxes)
    n = len(backend.egress_applied)
    await r.reaper.reconcile_egress()
    assert len(backend.egress_applied) == n        # not re-applied


async def test_failed_policy_update_is_retried(make_replica, backend):
    r = make_replica()
    await r.service.run_turn("a" * 32, "c", "x")
    r.cfg.pool.egress = EgressConfig(mode="enabled")
    backend.fail_set_egress = True
    await r.reaper.reconcile_egress()
    assert backend.egress_applied == []
    backend.fail_set_egress = False
    await r.reaper.reconcile_egress()
    assert len(backend.egress_applied) == 1


async def test_sandboxes_registered_before_this_feature_get_the_policy(make_replica, backend):
    r = make_replica()
    sid = await backend.create(r.pool.labels)
    await r.store.register_sandbox(sid, "t")       # no fingerprint recorded
    await r.reaper.reconcile_egress()
    assert backend.egress_applied == [sid]
