from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Optional, Tuple

from safe_io import FileSecurityError, atomic_write, lexical_absolute, open_directory


class HandoffError(ValueError):
    pass


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
        os.path.join(project_root, ".s3-upload", "config.json"),
    }
    trees = (
        os.path.join(project_root, ".s3-upload", "targets"),
        os.path.join(project_root, ".s3-upload", "checkpoints"),
        os.path.join(project_root, ".s3-upload"),
        state_root,
        config_home,
    )
    return path in exact or any(
        path == root or path.startswith(root + os.sep) for root in trees
    )


def _existing(parent_fd: int, name: str,
              source_identity: Optional[Tuple[int, int]]) -> Optional[str]:
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HandoffError("handoff destination is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HandoffError("handoff destination is not a regular file")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HandoffError("handoff destination has unsafe ownership or mode")
        if info.st_nlink != 1:
            raise HandoffError("handoff destination is a hardlink alias")
        if source_identity is not None and (info.st_dev, info.st_ino) == source_identity:
            raise HandoffError("handoff destination aliases the upload source")
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise HandoffError("handoff destination is too large")
        data = b""
        while len(data) <= MAX_ARTIFACT_BYTES:
            chunk = os.read(descriptor, MAX_ARTIFACT_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_ARTIFACT_BYTES:
            raise HandoffError("handoff destination is too large")
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(descriptor)


def preflight(path: str, *, project_root: str, config_home: str, state_root: str,
              source_identity: Optional[Tuple[int, int]]) -> HandoffTarget:
    absolute = lexical_absolute(path)
    roots = {
        "project_root": lexical_absolute(project_root),
        "config_home": lexical_absolute(config_home),
        "state_root": lexical_absolute(state_root),
    }
    if _protected(absolute, **roots):
        raise HandoffError("handoff destination is in a protected namespace")
    try:
        parent_fd = open_directory(os.path.dirname(absolute))
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff parent directory is unsafe") from exc
    try:
        parent = os.fstat(parent_fd)
        mode = stat.S_IMODE(parent.st_mode)
        if parent.st_uid != os.geteuid() or mode & 0o022:
            raise HandoffError("handoff parent must be owned and not group/world-writable")
        existing = _existing(parent_fd, os.path.basename(absolute), source_identity)
    finally:
        os.close(parent_fd)
    return HandoffTarget(absolute, parent.st_dev, parent.st_ino, parent.st_uid, mode, existing)


def commit(target: HandoffTarget, data: bytes) -> str:
    if not isinstance(data, bytes):
        raise HandoffError("handoff payload must be bytes")
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HandoffError("handoff payload is too large")
    digest = hashlib.sha256(data).hexdigest()
    try:
        parent_fd = open_directory(os.path.dirname(target.path))
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff parent directory is unsafe") from exc
    try:
        parent = os.fstat(parent_fd)
        if (
            parent.st_dev != target.parent_device
            or parent.st_ino != target.parent_inode
            or parent.st_uid != target.parent_owner
            or stat.S_IMODE(parent.st_mode) != target.parent_mode
        ):
            raise HandoffError("handoff parent changed after preflight")
        current = _existing(parent_fd, os.path.basename(target.path), None)
    finally:
        os.close(parent_fd)
    if current == digest:
        return "idempotent"
    if current is not None:
        raise HandoffError("handoff artifact is immutable and already exists")
    if target.existing_sha256 is not None:
        raise HandoffError("handoff destination changed after preflight")
    try:
        atomic_write(target.path, data, mode=0o600, replace=False)
    except FileExistsError as exc:
        raise HandoffError("handoff artifact was created concurrently") from exc
    except (OSError, FileSecurityError) as exc:
        raise HandoffError("handoff artifact could not be written durably") from exc
    return "created"
