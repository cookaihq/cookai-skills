from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Optional, Tuple

from safe_io import FileSecurityError, atomic_write, lexical_absolute, open_directory


HANDOFF_UNSAFE = "handoff_unsafe"
HANDOFF_WRITE_FAILED = "handoff_write_failed"


class HandoffError(ValueError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


MAX_ARTIFACT_BYTES = 262144


@dataclass(frozen=True)
class HandoffTarget:
    path: str
    parent_device: int
    parent_inode: int
    parent_owner: int
    parent_mode: int
    existing_sha256: Optional[str]


def _protected(path: str, *, project_root: str, config_home: str, state_root: str) -> bool:
    exact = {
        os.path.join(project_root, ".env"),
        os.path.join(project_root, ".env.local"),
    }
    trees = (
        os.path.join(project_root, ".s3-upload"),
        state_root,
        config_home,
    )
    return path in exact or any(
        path == root or path.startswith(root + os.sep) for root in trees
    )


def _existing_bytes(parent_fd: int, name: str,
                    source_identity: Optional[Tuple[int, int]], *,
                    reason: str) -> Optional[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HandoffError("handoff destination is unsafe", reason=reason) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HandoffError("handoff destination is not a regular file", reason=reason)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HandoffError("handoff destination has unsafe ownership or mode", reason=reason)
        if info.st_nlink != 1:
            raise HandoffError("handoff destination is a hardlink alias", reason=reason)
        if source_identity is not None and (info.st_dev, info.st_ino) == source_identity:
            raise HandoffError("handoff destination aliases the upload source", reason=reason)
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise HandoffError("handoff destination is too large", reason=reason)
        data = b""
        while len(data) <= MAX_ARTIFACT_BYTES:
            chunk = os.read(descriptor, MAX_ARTIFACT_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_ARTIFACT_BYTES:
            raise HandoffError("handoff destination is too large", reason=reason)
        return data
    finally:
        os.close(descriptor)


def _existing(parent_fd: int, name: str,
              source_identity: Optional[Tuple[int, int]], *, reason: str) -> Optional[str]:
    data = _existing_bytes(parent_fd, name, source_identity, reason=reason)
    return None if data is None else hashlib.sha256(data).hexdigest()


def read_artifact(target: HandoffTarget) -> Optional[bytes]:
    """Re-read a preflighted destination under the preflight safety rules.

    The target MUST be one that preflight() returned in this process: nothing
    in the type system enforces it, and every safety property here rests on
    it. The parent identity carried by the target is what this function
    re-checks the current parent against, so a HandoffTarget assembled by hand
    would have this function certify a path nobody ever vetted.

    Given such a target, the file is re-opened no-follow and non-blocking
    relative to the preflighted parent, and the regular-file, ownership, mode,
    single-link and size rules are re-checked against whatever is there now --
    a plain open() would follow a symlink into it, accept a loosened file or a
    hardlink alias, and block forever on a FIFO.

    Raises HandoffError if the destination is unsafe; the fstat and read of an
    open descriptor raise OSError, which the caller must be ready for.
    """
    try:
        parent_fd = open_directory(os.path.dirname(target.path))
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff parent directory is unsafe", reason=HANDOFF_UNSAFE) from exc
    try:
        parent = os.fstat(parent_fd)
        if (
            parent.st_dev != target.parent_device
            or parent.st_ino != target.parent_inode
            or parent.st_uid != target.parent_owner
            or stat.S_IMODE(parent.st_mode) != target.parent_mode
        ):
            raise HandoffError("handoff parent changed after preflight", reason=HANDOFF_UNSAFE)
        return _existing_bytes(
            parent_fd, os.path.basename(target.path), None, reason=HANDOFF_UNSAFE,
        )
    finally:
        os.close(parent_fd)


def preflight(path: str, *, project_root: str, config_home: str, state_root: str,
              source_identity: Optional[Tuple[int, int]]) -> HandoffTarget:
    absolute = lexical_absolute(path)
    roots = {
        "project_root": lexical_absolute(project_root),
        "config_home": lexical_absolute(config_home),
        "state_root": lexical_absolute(state_root),
    }
    if _protected(absolute, **roots):
        raise HandoffError("handoff destination is in a protected namespace", reason=HANDOFF_UNSAFE)
    try:
        parent_fd = open_directory(os.path.dirname(absolute))
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff parent directory is unsafe", reason=HANDOFF_UNSAFE) from exc
    try:
        parent = os.fstat(parent_fd)
        mode = stat.S_IMODE(parent.st_mode)
        if parent.st_uid != os.geteuid() or mode & 0o022:
            raise HandoffError(
                "handoff parent must be owned and not group/world-writable", reason=HANDOFF_UNSAFE,
            )
        existing = _existing(
            parent_fd, os.path.basename(absolute), source_identity, reason=HANDOFF_UNSAFE,
        )
    finally:
        os.close(parent_fd)
    return HandoffTarget(absolute, parent.st_dev, parent.st_ino, parent.st_uid, mode, existing)


def commit(target: HandoffTarget, data: bytes) -> str:
    if not isinstance(data, bytes):
        raise HandoffError("handoff payload must be bytes", reason=HANDOFF_WRITE_FAILED)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HandoffError("handoff payload is too large", reason=HANDOFF_WRITE_FAILED)
    digest = hashlib.sha256(data).hexdigest()
    try:
        parent_fd = open_directory(os.path.dirname(target.path))
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff parent directory is unsafe", reason=HANDOFF_WRITE_FAILED) from exc
    try:
        parent = os.fstat(parent_fd)
        if (
            parent.st_dev != target.parent_device
            or parent.st_ino != target.parent_inode
            or parent.st_uid != target.parent_owner
            or stat.S_IMODE(parent.st_mode) != target.parent_mode
        ):
            raise HandoffError("handoff parent changed after preflight", reason=HANDOFF_WRITE_FAILED)
        current = _existing(
            parent_fd, os.path.basename(target.path), None, reason=HANDOFF_WRITE_FAILED,
        )
        if current == digest:
            return "idempotent"
        if current is not None:
            raise HandoffError(
                "handoff artifact is immutable and already exists", reason=HANDOFF_WRITE_FAILED,
            )
        if target.existing_sha256 is not None:
            raise HandoffError(
                "handoff destination changed after preflight", reason=HANDOFF_WRITE_FAILED,
            )
        try:
            atomic_write(target.path, data, mode=0o600, replace=False, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise HandoffError(
                "handoff artifact was created concurrently", reason=HANDOFF_WRITE_FAILED,
            ) from exc
        except (OSError, FileSecurityError) as exc:
            raise HandoffError(
                "handoff artifact could not be written durably", reason=HANDOFF_WRITE_FAILED,
            ) from exc
        return "created"
    finally:
        os.close(parent_fd)
