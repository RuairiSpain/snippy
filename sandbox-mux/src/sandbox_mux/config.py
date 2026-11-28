"""Configuration loaded from a YAML file, with REDIS_URL taken from the environment."""

from __future__ import annotations

import os
import re
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


_UNITS = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
          "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def parse_size(text: str) -> int:
    """Bytes in a quantity like 512Mi, 4Gi, 2G, 100MB or a plain number of bytes.
    K, M, G, T are decimal (100MB = 100,000,000 bytes); Ki, Mi, Gi, Ti are binary."""
    import re
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?B?", text.strip())
    if not m:
        raise ValueError(f"invalid size {text!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2) or ""])


def parse_cpu(text: str) -> float:
    """Cores in a quantity like 2, 1.5 or 2000m."""
    t = text.strip()
    return float(t[:-1]) / 1000 if t.endswith("m") else float(t)


class FilesConfig(BaseModel):
    """Upload of input files and download of output files, kept in storage that outlives
    sandboxes. Sizes accept 100MB, 100Mi, 2GB, or a number of bytes."""

    enabled: bool = False
    store: Literal["azure_blob", "local"] = "azure_blob"
    account_url: str | None = None                 # https://<account>.blob.core.windows.net
    container: str = Field("smux-files", pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    local_path: str = "./data/files"               # store: local (development and tests)
    max_file_size: str = "100MB"                   # per file, uploaded or produced by the agent
    max_user_total_size: str = "2GB"               # all of one user's stored files
    max_files_per_user: int = Field(500, ge=1)
    max_input_files_per_turn: int = Field(10, ge=1)
    max_output_files_per_turn: int = Field(20, ge=1)
    max_output_size_per_turn: str = "200MB"        # total bytes the agent may return in one turn
    retention_days: int = Field(7, ge=1)
    max_concurrent_transfers: int = Field(4, ge=1) # whole files are held in memory per transfer

    @field_validator("max_file_size", "max_user_total_size", "max_output_size_per_turn",
                     mode="before")
    @classmethod
    def _size_text(cls, v: object) -> object:
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    @field_validator("max_file_size", "max_user_total_size", "max_output_size_per_turn")
    @classmethod
    def _size_ok(cls, v: str) -> str:
        if parse_size(v) <= 0:
            raise ValueError("size must be greater than zero")
        return v

    @property
    def file_bytes(self) -> int:
        return parse_size(self.max_file_size)

    @property
    def user_total_bytes(self) -> int:
        return parse_size(self.max_user_total_size)

    @property
    def turn_output_bytes(self) -> int:
        return parse_size(self.max_output_size_per_turn)

    @model_validator(mode="after")
    def _check(self) -> "FilesConfig":
        if self.file_bytes > self.user_total_bytes:
            raise ValueError("files.max_file_size is larger than files.max_user_total_size")
        if self.turn_output_bytes < self.file_bytes:
            raise ValueError("files.max_output_size_per_turn must be at least files.max_file_size")
        if self.enabled and self.store == "azure_blob" and not self.account_url:
            raise ValueError("files.account_url is required for files.store: azure_blob")
        return self


class LimitsConfig(BaseModel):
    """Per-user ceilings inside a shared sandbox. Each is a maximum, not a reservation.

    cgroup   Linux cgroup v2: cpu, memory and process count, enforced for the user's whole
             process tree. The sandbox must expose a writable cgroup v2 hierarchy; each new
             sandbox is checked when it is created and rejected if it does not.
    ulimit   Weaker fallback: process count per user, and an address-space cap that applies
             per process, not in total. No CPU limit.
    none     No limits (default).
    """

    mode: Literal["none", "cgroup", "ulimit"] = "none"
    cpu: float | None = Field(None, gt=0)            # cores, e.g. 0.5 or 2
    memory: str | None = Field(None, pattern=r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$")
    pids: int | None = Field(None, ge=1)             # max processes and threads
    cgroup_root: str = Field("/sys/fs/cgroup", pattern=r"^/[A-Za-z0-9_./-]*$")

    @field_validator("memory", mode="before")
    @classmethod
    def _numbers_as_text(cls, v: object) -> object:
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    @model_validator(mode="after")
    def _check(self) -> "LimitsConfig":
        any_set = self.cpu is not None or self.memory is not None or self.pids is not None
        if any_set and self.mode == "none":
            raise ValueError("limits.cpu/memory/pids are set but limits.mode is none; "
                             "use mode cgroup (or ulimit)")
        if self.mode != "none" and not any_set:
            raise ValueError(f"limits.mode {self.mode} needs at least one of cpu, memory, pids")
        if self.mode == "ulimit" and self.cpu is not None:
            raise ValueError("limits.cpu needs mode cgroup; ulimit cannot limit CPU")
        return self


_CONNECTOR_ID = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Web/connectorGateways/"
    r"[^/]+/(mcpserverconfigs|connections)/[^/]+$", re.IGNORECASE)
MAX_CONNECTORS = 10        # the service accepts at most 10 per sandbox


class ConnectorsConfig(BaseModel):
    """Connectors and MCP servers every sandbox in the pool is created with.

    A connector must first be attached to the sandbox group (`aca sandboxgroup connector add`).
    Each sandbox then opts in when it is created, and the list cannot change afterwards, so
    changing this setting replaces sandboxes instead of editing them (see retire_grace_seconds).

    Connectors authorize as the sandbox group's identity, not as the calling user: everyone on
    every sandbox in the pool gets the same access. Use them for resources the whole pool may use.

    `attach` entries are full resource IDs or names from `available`:
      /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Web/connectorGateways/<ns>/mcpserverconfigs/<name>   (MCP)
      /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Web/connectorGateways/<ns>/connections/<name>        (API)
    """

    available: dict[str, str] = {}              # alias -> resource ID
    attach: list[str] = []                      # aliases or resource IDs, up to 10
    # Where the platform writes the MCP client config inside the sandbox (for example
    # /root/.copilot/mcp-config.json on a Copilot disk). If set, a copy readable by the calling
    # user is placed in their request directory and its path is passed as $MCP_CONFIG_FILE.
    config_file: str | None = Field(None, pattern=r"^/[A-Za-z0-9_./-]*$")
    # How long a sandbox created under an older connector list may keep serving the users
    # already on it, once the list has changed. After this it is deleted even if in use.
    retire_grace_seconds: int = Field(3600, ge=0)

    @field_validator("attach", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> "ConnectorsConfig":
        for alias, rid in self.available.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", alias):
                raise ValueError(f"connectors.available: invalid alias {alias!r}")
            if not _CONNECTOR_ID.match(rid):
                raise ValueError(f"connectors.available[{alias}] is not a connector resource ID")
        _ = self.ids                                    # resolves and validates attach
        if self.config_file and not self.attach:
            raise ValueError("connectors.config_file is set but connectors.attach is empty")
        return self

    @property
    def ids(self) -> list[str]:
        out: list[str] = []
        for item in self.attach:
            if item in self.available:
                rid = self.available[item]
            elif item.startswith("/"):
                rid = item
            else:
                raise ValueError(f"connectors.attach: {item!r} is neither a resource ID nor a "
                                 f"name in connectors.available")
            if not _CONNECTOR_ID.match(rid):
                raise ValueError(f"connectors.attach: {rid!r} is not a connector resource ID")
            if rid.lower() not in (o.lower() for o in out):
                out.append(rid)
        if len(out) > MAX_CONNECTORS:
            raise ValueError(f"at most {MAX_CONNECTORS} connectors can be attached to a sandbox")
        return out

    def fingerprint(self) -> str:
        """Stable hash of the effective list; empty when there are none."""
        import hashlib
        ids = sorted(i.lower() for i in self.ids)
        return hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12] if ids else ""


class EgressConfig(BaseModel):
    """Outbound network policy applied to every sandbox in the pool.

    disabled          deny all outbound HTTP/HTTPS (default, safest)
    allowlist         deny everything except the hosts in `allow_hosts`
    enabled           allow all outbound HTTP/HTTPS
    platform_default  send no policy and take whatever the service defaults to. Docs
                      disagree on that default, so prefer an explicit mode.
    """

    mode: Literal["disabled", "allowlist", "enabled", "platform_default"] = "disabled"
    allow_hosts: list[str] = []
    # Log what the policy would do without blocking. Useful when first writing an allowlist.
    audit_only: bool = False
    traffic_inspection: Literal["Legacy", "Partial", "Full", "None"] | None = None
    # Also deny raw TCP/UDP (non-HTTP). Off by default: not verified that HTTPS through the
    # egress proxy keeps working with it on. Test in your region before enabling.
    block_non_http: bool = False

    @field_validator("allow_hosts", mode="before")
    @classmethod
    def _split(cls, v: object) -> object:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    @field_validator("allow_hosts")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        import re
        for h in v:
            if not re.fullmatch(r"(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?", h):
                raise ValueError(f"invalid host pattern {h!r} (use example.com or *.example.com)")
        return v

    @model_validator(mode="after")
    def _check(self) -> "EgressConfig":
        if self.mode == "allowlist" and not self.allow_hosts:
            raise ValueError("egress.mode allowlist needs allow_hosts; use mode disabled to block all")
        if self.mode != "allowlist" and self.allow_hosts:
            raise ValueError("egress.allow_hosts is only used with egress.mode: allowlist")
        return self

    def fingerprint(self) -> str:
        """Short stable hash of the effective policy, used to detect drift on live sandboxes."""
        import hashlib
        import json
        body = json.dumps(self.model_dump(), sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:12]


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
    egress: EgressConfig = EgressConfig()
    # Overrides for images built from a container image, whose own entrypoint may exit at once
    # (a Python image's default command is `python3`, which ends without a terminal).
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    # Run once as root in each new sandbox, after setup and before it takes users: import
    # heavy modules, compile bytecode, read model files. The page cache and bytecode are shared
    # by every user on the sandbox, and survive a memory-mode suspend. argv, not a shell string.
    warmup_command: list[str] | None = None
    warmup_timeout_seconds: int = Field(120, ge=1)
    # Keep at least this many free user slots across ready sandboxes, so a burst of new users
    # finds a booted sandbox instead of waiting for one. Capped by max_sandboxes.
    min_free_slots: int = Field(0, ge=0)

    @field_validator("cpu", "memory", "disk_size", mode="before")
    @classmethod
    def _numbers_as_text(cls, v: object) -> object:
        # `cpu: 2` in YAML, or SANDBOX_MUX_CPU=2, parses as a number; sizes are strings.
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    @model_validator(mode="after")
    def _check(self) -> "PoolConfig":
        if self.min_sandboxes > self.max_sandboxes:
            raise ValueError("pool.min_sandboxes must not exceed pool.max_sandboxes")
        if self.min_free_slots > self.max_sandboxes * self.users_per_sandbox:
            raise ValueError("pool.min_free_slots is more than max_sandboxes x users_per_sandbox")
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
    # Root-owned area inside the sandbox for files the manager hands to, or collects from,
    # a user's process. Users cannot write to it, which stops symlink tricks.
    state_root: str = Field("/var/lib/smux", pattern=r"^/[A-Za-z0-9_./-]*$")


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
    # After each exchange, check that the token is the signed-in user's delegated token (has
    # `scp`, no app-only role claims, same oid and tenant) and refuse to hand it over otherwise.
    # Turn off only for an API that issues opaque (non-JWT) tokens.
    verify_delegated: bool = True

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
    limits: LimitsConfig = LimitsConfig()
    connectors: ConnectorsConfig = ConnectorsConfig()
    files: FilesConfig = FilesConfig()
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
        if self.connectors.config_file and self.isolation.mode != "unix_user":
            raise ValueError("connectors.config_file requires isolation.mode: unix_user")
        if self.files.enabled and self.isolation.mode != "unix_user":
            raise ValueError("files.enabled requires isolation.mode: unix_user. Without separate "
                             "accounts the sandbox cannot keep one user's files from another.")
        lim = self.limits
        if lim.mode == "ulimit" and self.isolation.mode != "unix_user":
            raise ValueError("limits.mode ulimit needs isolation.mode unix_user (limits are per UID)")
        if lim.memory and parse_size(lim.memory) > parse_size(self.pool.memory):
            raise ValueError("limits.memory is larger than the whole sandbox (pool.memory)")
        if lim.cpu and lim.cpu > parse_cpu(self.pool.cpu):
            raise ValueError("limits.cpu is larger than the whole sandbox (pool.cpu)")
        if self.obo.enabled:
            if self.auth.mode != "entra":
                raise ValueError("obo.enabled requires auth.mode: entra")
            if self.isolation.mode != "unix_user":
                raise ValueError(
                    "obo.enabled requires isolation.mode: unix_user. With home_only every user "
                    "shares one UID and could read other users' delegated tokens.")
            if self.pool.egress.mode == "disabled":
                raise ValueError("obo.enabled is pointless with pool.egress.mode: disabled, the "
                                 "agent could not reach the downstream APIs. Use allowlist and "
                                 "list their hosts.")
        return self

    def overcommit(self) -> dict[str, float]:
        """Ratio of (per-user limit x users_per_sandbox) to the sandbox size. Above 1 the limits
        can add up to more than the sandbox has, so a full sandbox can still run out."""
        out: dict[str, float] = {}
        n = self.pool.users_per_sandbox
        if self.limits.memory:
            out["memory"] = n * parse_size(self.limits.memory) / parse_size(self.pool.memory)
        if self.limits.cpu:
            out["cpu"] = n * self.limits.cpu / parse_cpu(self.pool.cpu)
        return out

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
    "DISK_ID": "pool.disk_id",                                   # private disk image (custom or committed)
    "DISK": "pool.disk",                                         # public image name, e.g. python-3.12
    "MIN_FREE_SLOTS": "pool.min_free_slots",
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
    "CONNECTORS": "connectors.attach",
    "CONNECTOR_RETIRE_GRACE_SECONDS": "connectors.retire_grace_seconds",
    "FILES_ENABLED": "files.enabled",
    "MAX_FILE_SIZE": "files.max_file_size",
    "MAX_USER_STORAGE": "files.max_user_total_size",
    "FILE_RETENTION_DAYS": "files.retention_days",
    "LIMITS_MODE": "limits.mode",
    "USER_CPU": "limits.cpu",
    "USER_MEMORY": "limits.memory",
    "USER_PIDS": "limits.pids",
    "EGRESS_MODE": "pool.egress.mode",
    "EGRESS_ALLOW_HOSTS": "pool.egress.allow_hosts",
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
