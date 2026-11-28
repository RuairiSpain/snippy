from __future__ import annotations

from ..config import Config
from .base import ExecOutcome, SandboxBackend, SandboxGone, SandboxInfo


def make_backend(cfg: Config) -> SandboxBackend:
    if cfg.backend == "fake":
        from .fake import FakeSandboxBackend

        return FakeSandboxBackend()
    from .azure import AzureSandboxBackend  # imported lazily: needs the Azure SDK

    return AzureSandboxBackend(cfg)


__all__ = ["ExecOutcome", "SandboxBackend", "SandboxGone", "SandboxInfo", "make_backend"]
