"""Configuration loaded from a YAML file, with REDIS_URL taken from the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class AzureConfig(BaseModel):
    subscription_id: str = ""
    resource_group: str = ""
    sandbox_group: str = ""
    region: str = "westus2"
    endpoint: str | None = None


class PoolConfig(BaseModel):
    name: str = Field("agents", pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    min_sandboxes: int = Field(0, ge=0)
    max_sandboxes: int = Field(5, ge=1)
    users_per_sandbox: int = Field(10, ge=1)
    strategy: Literal["pack", "spread"] = "pack"
    disk: str | None = "ubuntu"
    disk_id: str | None = None
    # Size of EACH sandbox, shared by all users on it: scale with users_per_sandbox.
    # cpu: cores or millicores ("2", "1.5", "2000m"). memory/disk: "4096Mi", "4Gi".
    cpu: str = Field("1000m", pattern=r"^\d+(\.\d+)?m?$")
    memory: str = Field("2048Mi", pattern=r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$")
    disk_size: str | None = Field(None, pattern=r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$")
    auto_suspend_seconds: int = Field(900, ge=60)
    auto_suspend_mode: Literal["Memory", "Disk"] = "Memory"
    create_timeout_seconds: int = Field(300, ge=10)
    empty_ttl_seconds: int = Field(600, ge=0)

    @field_validator("cpu", "memory", "disk_size", mode="before")
    @classmethod
    def _numbers_as_text(cls, v: object) -> object:
        # `cpu: 2` in YAML, or SANDBOX_MUX_CPU=2, parses as a number; sizes are strings.
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    @model_validator(mode="after")
    def _check(self) -> "PoolConfig":
        if self.min_sandboxes > self.max_sandboxes:
            raise ValueError("pool.min_sandboxes must not exceed pool.max_sandboxes")
        return self


class SessionConfig(BaseModel):
    turn_timeout_seconds: int = Field(120, ge=1)
    queue_timeout_seconds: int = Field(30, ge=0)
    user_idle_ttl_seconds: int = Field(3600, ge=60)
    max_history_messages: int = Field(40, ge=1)
    history_ttl_seconds: int = Field(604800, ge=60)
    max_prompt_chars: int = Field(32000, ge=1)


class IsolationConfig(BaseModel):
    mode: Literal["unix_user", "home_only"] = "unix_user"
    home_root: str = Field("/home", pattern=r"^/[A-Za-z0-9_./-]*$")


class AgentConfig(BaseModel):
    command: list[str] = ["python3", "/opt/agent/run_turn.py"]
    env: dict[str, str] = {}


class AuthConfig(BaseModel):
    mode: Literal["entra", "dev"] = "entra"
    tenant_id: str = ""
    audience: str = ""
    admin_oids: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> "AuthConfig":
        if self.mode == "entra" and not (self.tenant_id and self.audience):
            raise ValueError("auth.tenant_id and auth.audience are required when auth.mode is entra")
        return self


class OboConfig(BaseModel):
    """Entra On-Behalf-Of: exchange the caller's token for tokens to downstream APIs and
    hand them to the agent process running as that user."""

    enabled: bool = False
    client_id: str = ""            # the app registration that `auth.audience` identifies
    credential: Literal["secret", "certificate", "managed_identity"] = "secret"
    # secret: read from the OBO_CLIENT_SECRET environment variable.
    certificate_path: str | None = None        # PEM file containing the private key
    certificate_thumbprint: str | None = None
    # Header carrying the user's token. Authorization (Bearer) unless your front end
    # forwards it elsewhere.
    assertion_header: str = Field("Authorization", pattern=r"^[A-Za-z0-9-]+$")
    # alias -> scopes. Callers may only request aliases listed here.
    resources: dict[str, list[str]] = {}
    max_resources_per_turn: int = Field(3, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "OboConfig":
        if self.enabled:
            if not self.client_id:
                raise ValueError("obo.client_id is required when obo.enabled is true")
            if not self.resources:
                raise ValueError("obo.resources must list at least one alias when obo is enabled")
            if self.credential == "certificate" and not self.certificate_path:
                raise ValueError("obo.certificate_path is required for credential: certificate")
        return self


class RedisConfig(BaseModel):
    url: str | None = None
    key_prefix: str = "smux"

    def resolved_url(self) -> str:
        url = os.environ.get("REDIS_URL") or self.url
        if not url:
            raise ValueError("Set the REDIS_URL environment variable (or redis.url for local dev)")
        return url


class Config(BaseModel):
    azure: AzureConfig = AzureConfig()
    backend: Literal["azure", "fake"] = "azure"
    pool: PoolConfig = PoolConfig()
    session: SessionConfig = SessionConfig()
    isolation: IsolationConfig = IsolationConfig()
    agent: AgentConfig = AgentConfig()
    auth: AuthConfig = AuthConfig(mode="dev")
    obo: OboConfig = OboConfig()
    redis: RedisConfig = RedisConfig()

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if self.backend == "azure":
            missing = [k for k in ("subscription_id", "resource_group", "sandbox_group")
                       if not getattr(self.azure, k)]
            if missing:
                raise ValueError(f"azure.{', azure.'.join(missing)} required when backend is azure")
        if self.obo.enabled:
            if self.auth.mode != "entra":
                raise ValueError("obo.enabled requires auth.mode: entra")
            if self.isolation.mode != "unix_user":
                raise ValueError(
                    "obo.enabled requires isolation.mode: unix_user. With home_only every user "
                    "shares one UID and could read other users' delegated tokens.")
        return self

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        path = Path(path or os.environ.get("SANDBOX_MUX_CONFIG", "config.yaml"))
        data = yaml.safe_load(path.read_text()) or {}
        env = {**load_env_file(os.environ.get("SANDBOX_MUX_ENV_FILE", ".env")), **os.environ}
        for k in ("REDIS_URL", "OBO_CLIENT_SECRET"):
            if k in env:
                os.environ.setdefault(k, env[k])
        apply_env_overrides(data, env)
        return cls.model_validate(data)


# Short names for the settings operators change most. Each is read as SANDBOX_MUX_<NAME>.
ENV_ALIASES: dict[str, str] = {
    "IDLE_TIMEOUT_SECONDS": "pool.auto_suspend_seconds",        # suspend an idle sandbox
    "SESSION_TTL_SECONDS": "session.user_idle_ttl_seconds",     # remove an idle user's session
    "EMPTY_SANDBOX_TTL_SECONDS": "pool.empty_ttl_seconds",      # delete an empty sandbox
    "HISTORY_TTL_SECONDS": "session.history_ttl_seconds",       # expire stored conversations
    "TURN_TIMEOUT_SECONDS": "session.turn_timeout_seconds",
    "QUEUE_TIMEOUT_SECONDS": "session.queue_timeout_seconds",
    "MIN_SANDBOXES": "pool.min_sandboxes",
    "MAX_SANDBOXES": "pool.max_sandboxes",
    "USERS_PER_SANDBOX": "pool.users_per_sandbox",
    "CPU": "pool.cpu",
    "MEMORY": "pool.memory",
    "DISK_SIZE": "pool.disk_size",
}


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a `.env` file (KEY=VALUE lines, `#` comments, optional quotes and `export`).
    Variables already set in the real environment win. A missing file is not an error.
    Only SANDBOX_MUX_*, REDIS_URL and OBO_CLIENT_SECRET are read from it."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.startswith("SANDBOX_MUX") or key in ("REDIS_URL", "OBO_CLIENT_SECRET"):
            out[key] = value
    return out


def _set_path(data: dict, path: list[str], value: object) -> None:
    for part in path[:-1]:
        nxt = data.get(part)
        if not isinstance(nxt, dict):
            nxt = data[part] = {}
        data = nxt
    data[path[-1]] = value


def apply_env_overrides(data: dict, env: "os._Environ[str] | dict[str, str]") -> None:
    """Environment beats the YAML file. Two forms:

    SANDBOX_MUX_IDLE_TIMEOUT_SECONDS=600        (a name from ENV_ALIASES)
    SANDBOX_MUX__POOL__EMPTY_TTL_SECONDS=300    (any setting: SANDBOX_MUX__<SECTION>__<KEY>)

    Values are parsed as YAML scalars (so `true`, `30` and `[a, b]` work) and then validated
    by the config model, which rejects wrong types and out-of-range values at startup.
    """
    for name, dotted in ENV_ALIASES.items():
        raw = env.get(f"SANDBOX_MUX_{name}")
        if raw not in (None, ""):
            _set_path(data, dotted.split("."), yaml.safe_load(raw))
    for key, raw in env.items():
        if key.startswith("SANDBOX_MUX__") and raw != "":
            parts = [p.lower() for p in key[len("SANDBOX_MUX__"):].split("__") if p]
            if parts:
                _set_path(data, parts, yaml.safe_load(raw))
