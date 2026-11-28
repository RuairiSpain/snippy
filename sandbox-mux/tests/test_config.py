import pytest
import yaml

from sandbox_mux.config import Config, apply_env_overrides


def load(tmp_path, monkeypatch, yaml_data, env):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"backend": "fake", "auth": {"mode": "dev"}, **yaml_data}))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Config.load(p)


def test_defaults_come_from_the_file(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch, {"pool": {"auto_suspend_seconds": 1200}}, {})
    assert c.pool.auto_suspend_seconds == 1200


def test_friendly_env_names_override_the_file(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch,
             {"pool": {"auto_suspend_seconds": 1200}, "session": {"user_idle_ttl_seconds": 7200}},
             {"SANDBOX_MUX_IDLE_TIMEOUT_SECONDS": "300",
              "SANDBOX_MUX_SESSION_TTL_SECONDS": "900",
              "SANDBOX_MUX_EMPTY_SANDBOX_TTL_SECONDS": "30",
              "SANDBOX_MUX_MAX_SANDBOXES": "8"})
    assert c.pool.auto_suspend_seconds == 300
    assert c.session.user_idle_ttl_seconds == 900
    assert c.pool.empty_ttl_seconds == 30
    assert c.pool.max_sandboxes == 8


def test_generic_double_underscore_form_reaches_any_setting(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch, {}, {
        "SANDBOX_MUX__POOL__STRATEGY": "spread",
        "SANDBOX_MUX__AGENT__COMMAND": '["python3", "/x.py"]',
        "SANDBOX_MUX__AUTH__ADMIN_OIDS": "[a1, b2]",
    })
    assert c.pool.strategy == "spread"
    assert c.agent.command == ["python3", "/x.py"]
    assert c.auth.admin_oids == ["a1", "b2"]


def test_generic_form_wins_over_friendly_name():
    d: dict = {}
    apply_env_overrides(d, {"SANDBOX_MUX_MAX_SANDBOXES": "3", "SANDBOX_MUX__POOL__MAX_SANDBOXES": "9"})
    assert d["pool"]["max_sandboxes"] == 9


def test_config_path_variable_is_not_mistaken_for_a_setting():
    d: dict = {}
    apply_env_overrides(d, {"SANDBOX_MUX_CONFIG": "/etc/x.yaml"})
    assert d == {}


@pytest.mark.parametrize("env, message", [
    ({"SANDBOX_MUX_IDLE_TIMEOUT_SECONDS": "soon"}, "auto_suspend_seconds"),       # not a number
    ({"SANDBOX_MUX_SESSION_TTL_SECONDS": "5"}, "user_idle_ttl_seconds"),          # below the minimum
    ({"SANDBOX_MUX_MIN_SANDBOXES": "9"}, "min_sandboxes"),                        # above max (5)
])
def test_invalid_env_values_fail_at_startup(tmp_path, monkeypatch, env, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, monkeypatch, {}, env)
