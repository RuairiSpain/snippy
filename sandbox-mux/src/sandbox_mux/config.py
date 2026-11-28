"""Configuration loaded from a YAML file, with REDIS_URL taken from the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


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
    cpu: str = "1000m"
    memory: str = "2048Mi"
    auto_suspend_seconds: int = Field(900, ge=60)
    auto_suspend_mode: Literal["Memory", "Disk"] = "Memory"
    create_timeout_seconds: int = Field(300, ge=10)
    empty_ttl_seconds: int = Field(600, ge=0)

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
    redis: RedisConfig = RedisConfig()

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if self.backend == "azure":
            missing = [k for k in ("subscription_id", "resource_group", "sandbox_group")
                       if not getattr(self.azure, k)]
            if missing:
                raise ValueError(f"azure.{', azure.'.join(missing)} required when backend is azure")
        return self

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        path = Path(path or os.environ.get("SANDBOX_MUX_CONFIG", "config.yaml"))
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)
