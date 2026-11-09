from __future__ import annotations

from pathlib import Path
from typing import Protocol


class FileStore(Protocol):
    def put(self, tenant_id: str, name: str, data: bytes) -> str: ...

    def get(self, path: str) -> bytes: ...


def _safe(part: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in part)


class LocalFileStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def put(self, tenant_id: str, name: str, data: bytes) -> str:
        rel = f"{_safe(tenant_id)}/{_safe(name)}"
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel

    def get(self, path: str) -> bytes:
        p = (self.root / path).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("path escapes the file root")
        return p.read_bytes()


class BlobFileStore:
    def __init__(self, account: str, container: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        svc = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential())
        self._c = svc.get_container_client(container)

    def put(self, tenant_id: str, name: str, data: bytes) -> str:
        rel = f"{_safe(tenant_id)}/{_safe(name)}"
        self._c.upload_blob(rel, data, overwrite=True)
        return rel

    def get(self, path: str) -> bytes:
        return self._c.download_blob(path).readall()
