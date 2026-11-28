"""Durable object storage for user files. Sandboxes are temporary, so files live here."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path
from typing import AsyncIterator, Protocol

from .config import FilesConfig

CHUNK = 1024 * 1024
_KEY = re.compile(r"^[0-9a-f]{32}/[0-9a-f]{32}$")


class BlobStore(Protocol):
    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        """Store the stream under `key`. If the iterator raises, nothing is kept."""

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def read(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def close(self) -> None: ...


def _check_key(key: str) -> str:
    # Keys are `<uid>/<file id>`, both generated here. Anything else is a bug or an attack.
    if not _KEY.match(key):
        raise ValueError("invalid blob key")
    return key


class LocalBlobStore:
    """Files on local disk. For development and tests; not shared between replicas."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".tmp-{secrets.token_hex(8)}")
        try:
            with open(tmp, "wb") as f:
                async for c in chunks:
                    await asyncio.to_thread(f.write, c)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def get(self, key: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        path = self._path(key)
        with open(path, "rb") as f:
            while chunk := await asyncio.to_thread(f.read, CHUNK):
                yield chunk

    async def read(self, key: str) -> bytes:
        return b"".join([c async for c in self.get(key)])

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def close(self) -> None: ...


class AzureBlobStore:
    """Azure Blob Storage through the service's managed identity (DefaultAzureCredential)."""

    def __init__(self, account_url: str, container: str) -> None:
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient

        self._credential = DefaultAzureCredential()
        self._service = BlobServiceClient(account_url, credential=self._credential)
        self._container = self._service.get_container_client(container)

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        # Block blob upload: blocks are not committed until the stream finishes, so an
        # error part-way (for example the size limit) leaves no blob behind.
        await self._container.upload_blob(_check_key(key), chunks, overwrite=False)

    async def get(self, key: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        stream = await self._container.download_blob(_check_key(key))
        async for chunk in stream.chunks():
            yield chunk

    async def read(self, key: str) -> bytes:
        stream = await self._container.download_blob(_check_key(key))
        return await stream.readall()

    async def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            await self._container.delete_blob(_check_key(key))
        except ResourceNotFoundError:
            pass

    async def close(self) -> None:
        await self._service.close()
        await self._credential.close()


def make_blob_store(cfg: FilesConfig) -> BlobStore:
    if cfg.store == "local":
        return LocalBlobStore(cfg.local_path)
    assert cfg.account_url
    return AzureBlobStore(cfg.account_url, cfg.container)
