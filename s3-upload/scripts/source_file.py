from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional

from safe_io import lexical_absolute


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    size: int
    mtime_ns: int
    device: Optional[int]
    inode: Optional[int]
    sha256: str

    def as_checkpoint(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": str(self.mtime_ns),
            "device": None if self.device is None else str(self.device),
            "inode": None if self.inode is None else str(self.inode),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SourcePart:
    number: int
    offset: int
    data: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)

    def as_checkpoint(self) -> Dict[str, Any]:
        return {
            "part_number": self.number,
            "size": self.size,
            "sha256": self.sha256,
        }


class VerifiedSource:
    def __init__(self, handle, snapshot: SourceSnapshot):
        self._handle = handle
        self.snapshot = snapshot

    @classmethod
    def open(cls, path: str, *, soft_max_bytes: int) -> "VerifiedSource":
        absolute = lexical_absolute(path)
        try:
            handle = open(absolute, "rb")
        except OSError as exc:
            raise SourceError("cannot open local source") from exc
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SourceError("local source is not a regular file")
            if info.st_size > soft_max_bytes:
                raise SourceError("local source exceeds the soft size limit")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > soft_max_bytes:
                    raise SourceError("local source exceeds the soft size limit")
                digest.update(chunk)
            if total != info.st_size:
                raise SourceError("local source changed while hashing")
            after = os.fstat(handle.fileno())
            if not _same_identity(info, after):
                raise SourceError("local source changed while hashing")
            handle.seek(0)
            snapshot = SourceSnapshot(
                absolute,
                total,
                after.st_mtime_ns,
                getattr(after, "st_dev", None),
                getattr(after, "st_ino", None),
                digest.hexdigest(),
            )
            return cls(handle, snapshot)
        except Exception:
            handle.close()
            raise

    def __enter__(self) -> "VerifiedSource":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._handle.close()

    def _verify_descriptor(self, total: int, digest: str) -> None:
        info = os.fstat(self._handle.fileno())
        if (
            total != self.snapshot.size
            or digest != self.snapshot.sha256
            or info.st_size != self.snapshot.size
            or info.st_mtime_ns != self.snapshot.mtime_ns
            or (self.snapshot.device is not None and info.st_dev != self.snapshot.device)
            or (self.snapshot.inode is not None and info.st_ino != self.snapshot.inode)
        ):
            raise SourceError("local source changed after initial verification")

    def single_put_bytes(self) -> bytes:
        self._handle.seek(0)
        digest = hashlib.sha256()
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = self._handle.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            digest.update(chunk)
        self._verify_descriptor(total, digest.hexdigest())
        self._handle.seek(0)
        return b"".join(chunks)

    def verify_unchanged(self) -> None:
        self._handle.seek(0)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = self._handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        self._verify_descriptor(total, digest.hexdigest())
        self._handle.seek(0)

    def parts(self, part_size_bytes: int) -> Iterator[SourcePart]:
        if part_size_bytes <= 0:
            raise SourceError("part size must be positive")
        self._handle.seek(0)
        digest = hashlib.sha256()
        total = 0
        number = 1
        while True:
            data = self._handle.read(part_size_bytes)
            if not data:
                break
            digest.update(data)
            part = SourcePart(number, total, data, hashlib.sha256(data).hexdigest())
            total += len(data)
            number += 1
            yield part
        self._verify_descriptor(total, digest.hexdigest())
        self._handle.seek(0)


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def verify_resumable_source(snapshot: Dict[str, Any], acknowledged_parts: Iterable[Dict[str, Any]],
                            *, part_size_bytes: int) -> SourceSnapshot:
    try:
        path = snapshot["path"]
        expected_size = snapshot["size"]
        expected_hash = snapshot["sha256"]
        expected_mtime = int(snapshot["mtime_ns"])
        expected_device = None if snapshot["device"] is None else int(snapshot["device"])
        expected_inode = None if snapshot["inode"] is None else int(snapshot["inode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("invalid source checkpoint") from exc
    with VerifiedSource.open(path, soft_max_bytes=expected_size) as source:
        actual = source.snapshot
        if (
            actual.size != expected_size
            or actual.sha256 != expected_hash
            or actual.mtime_ns != expected_mtime
            or (expected_device is not None and actual.device != expected_device)
            or (expected_inode is not None and actual.inode != expected_inode)
        ):
            raise SourceError("local source changed since checkpoint")
        for expected_number, row in enumerate(acknowledged_parts, 1):
            try:
                number = row["part_number"]
                size = row["size"]
                digest = row["sha256"]
            except (KeyError, TypeError) as exc:
                raise SourceError("invalid acknowledged part checkpoint") from exc
            if number != expected_number:
                raise SourceError("acknowledged part order changed")
            source._handle.seek((number - 1) * part_size_bytes)
            data = source._handle.read(size)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise SourceError("acknowledged part no longer matches the local source")
        return actual
