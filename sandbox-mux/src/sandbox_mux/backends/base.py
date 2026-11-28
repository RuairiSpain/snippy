"""Backend interface. Everything that touches the sandbox SDK sits behind this."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SandboxGone(Exception):
    """The sandbox no longer exists (deleted, or failed)."""


@dataclass
class ExecOutcome:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class SandboxInfo:
    id: str
    state: str
    created_at: float | None  # epoch seconds; None when the service did not report it


class SandboxBackend(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def with_config(self, cfg) -> "SandboxBackend":
        """A view of this backend that creates and updates sandboxes with another pool's settings
        (size, image, egress, connectors). It shares the connection; only the original is
        started and closed."""

    async def create(self, labels: dict[str, str]) -> str:
        """Create a sandbox, wait until it is Running, return its id."""

    async def ensure_running(self, sandbox_id: str) -> None:
        """Resume the sandbox if it is stopped or suspended; return once Running."""

    async def exec(self, sandbox_id: str, command: str) -> ExecOutcome: ...

    async def write_file(self, sandbox_id: str, path: str, data: bytes,
                         mode: str | None = None) -> None: ...

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        """File contents, or b"" if the file does not exist."""

    async def set_egress(self, sandbox_id: str) -> None:
        """Apply the currently configured egress policy to an existing sandbox."""

    async def delete(self, sandbox_id: str) -> None: ...

    async def list(self, labels: dict[str, str]) -> list[SandboxInfo]: ...
