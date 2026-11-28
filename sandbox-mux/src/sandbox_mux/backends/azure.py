"""Backend for Azure Container Apps Sandboxes (azure-containerapps-sandbox, async API).

The SDK is a community preview and its surface may change. This is the only module
that imports it.
"""

from __future__ import annotations

import datetime as dt
import logging

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.containerapps.sandbox import endpoint_for_region, validate_egress_policy
from azure.containerapps.sandbox.models import (
    EgressHttpPolicy,
    EgressPolicy,
    EgressRule,
    EgressRuleAction,
    EgressRuleMatch,
    EgressTransportPolicy,
)
from azure.containerapps.sandbox.aio import SandboxGroupClient
from azure.identity.aio import DefaultAzureCredential

from ..config import Config, EgressConfig
from .base import ExecOutcome, SandboxGone, SandboxInfo

log = logging.getLogger(__name__)


def _to_epoch(value: object) -> float | None:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()
    return None


def build_egress_policy(e: EgressConfig) -> EgressPolicy | None:
    """Translate the config into an SDK policy. None means: send no policy."""
    if e.mode == "platform_default":
        return None
    allow = e.mode == "enabled"
    rules = [
        EgressRule(name=f"allow-{i}", match=EgressRuleMatch(host=h),
                   action=EgressRuleAction(type="Allow"))
        for i, h in enumerate(e.allow_hosts, start=1)
    ]
    http = EgressHttpPolicy(
        default_action="Allow" if allow else "Deny",
        rules=rules,
        enforcement_mode="Audit" if e.audit_only else "Enforced",
        traffic_inspection=e.traffic_inspection,
    )
    policy = EgressPolicy(http=http)
    if e.block_non_http:
        policy.transport_rules = EgressTransportPolicy(default_action="Deny", rules=[])
    validate_egress_policy(policy)
    return policy


class AzureSandboxBackend:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._credential: DefaultAzureCredential | None = None
        self._client: SandboxGroupClient | None = None

    async def start(self) -> None:
        a = self._cfg.azure
        self._credential = DefaultAzureCredential()
        self._client = SandboxGroupClient(
            a.endpoint or endpoint_for_region(a.region),
            self._credential,
            subscription_id=a.subscription_id,
            resource_group=a.resource_group,
            sandbox_group=a.sandbox_group,
        )
        await self._client.__aenter__()

    async def close(self) -> None:
        if self._client:
            await self._client.close()
        if self._credential:
            await self._credential.close()

    @property
    def _c(self) -> SandboxGroupClient:
        assert self._client is not None, "backend not started"
        return self._client

    async def create(self, labels: dict[str, str]) -> str:
        p = self._cfg.pool
        poller = await self._c.begin_create_sandbox(
            disk=None if p.disk_id else p.disk,
            disk_id=p.disk_id,
            cpu=p.cpu,
            memory=p.memory,
            disk_size=p.disk_size,
            auto_suspend_seconds=p.auto_suspend_seconds,
            auto_suspend_mode=p.auto_suspend_mode,
            labels=labels,
            egress_policy=build_egress_policy(p.egress),
            polling_timeout=p.create_timeout_seconds,
        )
        sandbox = await poller.result()
        return sandbox.sandbox_id

    async def ensure_running(self, sandbox_id: str) -> None:
        try:
            await self._c.get_sandbox_client(sandbox_id).ensure_running()
        except ResourceNotFoundError as e:
            raise SandboxGone(sandbox_id) from e
        except RuntimeError as e:
            # SDK raises RuntimeError for Deleting/Failed/Disabled sandboxes
            raise SandboxGone(f"{sandbox_id}: {e}") from e

    async def exec(self, sandbox_id: str, command: str) -> ExecOutcome:
        try:
            r = await self._c.get_sandbox_client(sandbox_id).exec(command)
        except ResourceNotFoundError as e:
            raise SandboxGone(sandbox_id) from e
        return ExecOutcome(int(r.exit_code), r.stdout or "", r.stderr or "")

    async def write_file(self, sandbox_id: str, path: str, data: bytes,
                         mode: str | None = None) -> None:
        try:
            await self._c.get_sandbox_client(sandbox_id).write_file(path, data, mode=mode)
        except ResourceNotFoundError as e:
            raise SandboxGone(sandbox_id) from e

    async def set_egress(self, sandbox_id: str) -> None:
        policy = build_egress_policy(self._cfg.pool.egress)
        if policy is None:
            return  # platform_default: there is no way to remove a policy, so leave it
        try:
            await self._c.get_sandbox_client(sandbox_id).set_egress_policy(policy)
        except ResourceNotFoundError as e:
            raise SandboxGone(sandbox_id) from e

    async def delete(self, sandbox_id: str) -> None:
        try:
            await self._c.delete_sandbox(sandbox_id)
        except ResourceNotFoundError:
            return
        except HttpResponseError:
            log.exception("delete of sandbox %s failed", sandbox_id)
            raise

    async def list(self, labels: dict[str, str]) -> list[SandboxInfo]:
        out: list[SandboxInfo] = []
        async for s in self._c.list_sandboxes(labels=labels):
            out.append(SandboxInfo(id=s.id, state=s.state or "", created_at=_to_epoch(s.created_at)))
        return out
