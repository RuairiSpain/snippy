"""User files: upload, download, quota, and the hand-off to and from sandboxes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import re
import secrets
import tarfile
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import AsyncIterator

from .blobs import BlobStore
from .config import Config
from .store import Store, now_ms

log = logging.getLogger(__name__)

FILE_ID = r"^[0-9a-f]{32}$"
_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9][\w.+-]*/[A-Za-z0-9][\w.+-]*$")


class FileError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class FileRecord:
    id: str
    uid: str
    name: str
    size: int
    sha256: str
    content_type: str
    kind: str                      # input | output
    created_ms: int
    expires_ms: int
    conversation_id: str | None = None

    @property
    def key(self) -> str:
        return f"{self.uid}/{self.id}"

    def public(self) -> dict:
        d = asdict(self)
        for k in ("uid", "created_ms", "expires_ms"):
            d.pop(k)
        d["file_id"] = d.pop("id")
        d["created_at"] = _iso(self.created_ms)
        d["expires_at"] = _iso(self.expires_ms)
        return d


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def safe_name(name: str) -> str:
    """A display and file name that is a single path component with no surprises."""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip(" .")
    if not name:
        raise FileError(422, "Invalid file name")
    return name[:200]


def unique_names(names: list[str]) -> list[str]:
    """Make names distinct within one directory: a.txt, a (2).txt, ..."""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        cand, i = n, 1
        stem, dot, ext = n.rpartition(".")
        while cand.lower() in seen:
            i += 1
            cand = f"{stem} ({i}).{ext}" if dot and stem else f"{n} ({i})"
        seen.add(cand.lower())
        out.append(cand)
    return out


class FileService:
    def __init__(self, cfg: Config, store: Store, blobs: BlobStore) -> None:
        self.cfg, self.store, self.blobs = cfg, store, blobs
        self.f = cfg.files
        # Whole files are held in memory while moving to or from a sandbox.
        self._transfers = asyncio.Semaphore(self.f.max_concurrent_transfers)

    # ---- upload and download ----------------------------------------------------------

    async def upload(self, uid: str, name: str, content_type: str | None,
                     chunks: AsyncIterator[bytes], declared_size: int | None = None) -> FileRecord:
        name = safe_name(name)
        limit = self.f.file_bytes
        if declared_size is not None and declared_size > limit:
            raise FileError(413, f"File exceeds the {self.f.max_file_size} limit")
        return await self._store_stream(uid, name, _clean_type(content_type), "input", None,
                                        chunks, declared_size)

    async def _store_stream(self, uid: str, name: str, content_type: str, kind: str,
                            conv: str | None, chunks: AsyncIterator[bytes],
                            declared: int | None) -> FileRecord:
        limit = self.f.file_bytes
        fid = secrets.token_hex(16)
        await self._reserve(uid, fid, declared if declared is not None else limit)
        digest, size = hashlib.sha256(), 0
        record: FileRecord | None = None

        async def limited() -> AsyncIterator[bytes]:
            nonlocal size
            async for c in chunks:
                size += len(c)
                if size > limit:
                    raise FileError(413, f"File exceeds the {self.f.max_file_size} limit")
                digest.update(c)
                yield c

        key = f"{uid}/{fid}"
        try:
            async with self._transfers:
                await self.blobs.put(key, limited())
            if size == 0:
                raise FileError(422, "File is empty")
            now = now_ms()
            record = FileRecord(fid, uid, name, size, digest.hexdigest(), content_type, kind, now,
                                now + self.f.retention_days * 86_400_000, conv)
            # Replace the reservation (declared size or the limit) with the real size.
            await self.store.commit_file(asdict(record), record.expires_ms)
            return record
        except BaseException:
            await asyncio.shield(self._abandon(uid, fid, key))
            raise

    async def _abandon(self, uid: str, fid: str, key: str) -> None:
        try:
            await self.blobs.delete(key)
        except Exception:
            log.exception("could not delete partial upload %s", key)
        await self.store.release_file(uid, fid)

    async def _reserve(self, uid: str, fid: str, size: int) -> None:
        r = await self.store.reserve_file(uid, fid, size, max_bytes=self.f.user_total_bytes,
                                          max_files=self.f.max_files_per_user)
        if r == "files":
            raise FileError(409, f"Limit of {self.f.max_files_per_user} stored files reached; "
                                 "delete some first")
        if r == "bytes":
            raise FileError(409, f"Storage limit of {self.f.max_user_total_size} reached; "
                                 "delete some files first")

    async def get(self, uid: str, file_id: str) -> FileRecord:
        """The caller's file. Other users' files look the same as missing ones."""
        raw = await self.store.get_file(file_id)
        if not raw or raw["uid"] != uid:
            raise FileError(404, "File not found")
        return FileRecord(**raw)

    async def list(self, uid: str) -> list[FileRecord]:
        out = []
        for fid in await self.store.user_file_ids(uid):
            raw = await self.store.get_file(fid)
            if raw and raw["uid"] == uid:
                out.append(FileRecord(**raw))
        return sorted(out, key=lambda r: r.created_ms, reverse=True)

    def download(self, record: FileRecord) -> AsyncIterator[bytes]:
        return self.blobs.get(record.key)

    async def delete(self, uid: str, file_id: str) -> None:
        rec = await self.get(uid, file_id)
        await self.blobs.delete(rec.key)
        await self.store.delete_file_record(uid, rec.id)

    # ---- hand-off to a sandbox -----------------------------------------------------------

    async def inputs_for_turn(self, uid: str, ids: list[str]) -> list[FileRecord]:
        ids = list(dict.fromkeys(ids))
        if len(ids) > self.f.max_input_files_per_turn:
            raise FileError(422, f"At most {self.f.max_input_files_per_turn} input files per request")
        out = []
        for fid in ids:
            try:
                out.append(await self.get(uid, fid))
            except FileError:
                raise FileError(422, f"Unknown file id {fid}") from None
        return out

    async def read(self, record: FileRecord) -> bytes:
        async with self._transfers:
            return await self.blobs.read(record.key)

    # ---- hand-off from a sandbox -----------------------------------------------------------

    def unpack_bundle(self, data: bytes) -> tuple[list[tuple[str, bytes]], list[dict]]:
        """Regular files from a tar produced inside a sandbox. The archive is untrusted: it
        is read in memory and never extracted to disk, and only plain files are accepted."""
        skipped: list[dict] = []
        files: list[tuple[str, bytes]] = []
        if len(data) > self.f.turn_output_bytes:
            return [], [{"name": "*", "reason":
                         f"outputs exceed the {self.f.max_output_size_per_turn} per-turn limit"}]
        if len(data) < 512:
            return [], []
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
        except tarfile.TarError:
            return [], [{"name": "*", "reason": "outputs could not be read"}]
        with tar:
            try:
                members = tar.getmembers()
            except tarfile.TarError:
                return [], [{"name": "*", "reason": "outputs could not be read"}]
            for m in members:
                if m.isdir():
                    continue
                shown = m.name[:120]
                if not m.isreg():
                    skipped.append({"name": shown, "reason": "not a regular file"})
                    continue
                parts = [p for p in m.name.split("/") if p not in ("", ".")]
                if m.name.startswith("/") or ".." in parts or not parts or "\x00" in m.name:
                    skipped.append({"name": shown, "reason": "unsafe path"})
                    continue
                if m.size == 0:
                    skipped.append({"name": shown, "reason": "empty file"})
                    continue
                if m.size > self.f.file_bytes:
                    skipped.append({"name": shown,
                                    "reason": f"larger than {self.f.max_file_size}"})
                    continue
                if len(files) >= self.f.max_output_files_per_turn:
                    skipped.append({"name": shown, "reason": "too many output files"})
                    continue
                fobj = tar.extractfile(m)
                if fobj is None:
                    skipped.append({"name": shown, "reason": "unreadable"})
                    continue
                files.append((parts[-1], fobj.read()))
        return files, skipped

    async def store_outputs(self, uid: str, conv: str, bundle: bytes
                            ) -> tuple[list[FileRecord], list[dict]]:
        files, skipped = self.unpack_bundle(bundle)
        names = unique_names([_safe_or(n) for n, _ in files])
        records: list[FileRecord] = []
        for name, (_, data) in zip(names, files):
            async def one(d: bytes = data) -> AsyncIterator[bytes]:
                yield d
            try:
                records.append(await self._store_stream(
                    uid, name, _clean_type(mimetypes.guess_type(name)[0]), "output", conv,
                    one(), len(data)))
            except FileError as e:
                skipped.append({"name": name, "reason": e.detail})
        return records, skipped

    # ---- retention -----------------------------------------------------------------------------

    async def purge_expired(self) -> int:
        n = 0
        for member in await self.store.due_files():
            uid, _, fid = member.partition(":")
            try:
                raw = await self.store.get_file(fid)
                if raw:
                    await self.blobs.delete(f"{raw['uid']}/{fid}")
                await self.store.delete_file_record(uid, fid)
                n += 1
            except Exception:
                log.exception("could not purge file %s", fid)
        for member in await self.store.stale_reservations():
            uid, _, fid = member.partition(":")
            # An upload that never finished (crashed replica): give the quota back.
            if not await self.store.get_file(fid):
                await self.store.release_file(uid, fid)
        return n


def _clean_type(value: str | None) -> str:
    if value:
        value = value.split(";")[0].strip()
        if _CONTENT_TYPE.match(value):
            return value.lower()
    return "application/octet-stream"


def _safe_or(name: str) -> str:
    try:
        return safe_name(name)
    except FileError:
        return "file"
