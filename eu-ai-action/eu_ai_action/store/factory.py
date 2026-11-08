from __future__ import annotations

from ..settings import Settings
from .base import Store
from .files import BlobFileStore, FileStore, LocalFileStore
from .memory import MemoryStore


def make_store(s: Settings) -> Store:
    if s.use_memory_store or not s.cosmos_endpoint:
        return MemoryStore()
    from .cosmos import CosmosStore

    return CosmosStore(s.cosmos_endpoint, s.cosmos_database)


def make_file_store(s: Settings) -> FileStore:
    if s.storage_account_name:
        return BlobFileStore(s.storage_account_name, s.documents_container)
    return LocalFileStore(s.files_dir)
