from __future__ import annotations

import errno
import os
import secrets
import stat
from dataclasses import dataclass
from typing import Optional


class FileSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    owner: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def lexical_absolute(path: str) -> str:
    return os.path.abspath(os.path.normpath(os.path.expanduser(path)))


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def open_directory(path: str) -> int:
    absolute = lexical_absolute(path)
    parts = [part for part in absolute.split(os.sep) if part]
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for part in parts:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise FileSecurityError("path component is not a directory")
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise FileSecurityError("path contains a symlink or non-directory component") from exc
        raise
    except Exception:
        os.close(descriptor)
        raise


def validate_directory(path: str, *, exact_mode: Optional[int] = None, owned: bool = True,
                       forbid_group_world_write: bool = True) -> None:
    descriptor = open_directory(path)
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if owned and info.st_uid != os.geteuid():
            raise FileSecurityError("directory is not owned by the effective user")
        if exact_mode is not None and mode != exact_mode:
            raise FileSecurityError(f"directory permissions must be {exact_mode:04o}")
        if forbid_group_world_write and mode & 0o022:
            raise FileSecurityError("directory must not be group/world-writable")
    finally:
        os.close(descriptor)


def read_regular_file(path: str, *, max_bytes: int, secret: bool,
                      missing_ok: bool = False) -> Optional[str]:
    absolute = lexical_absolute(path)
    parent, name = os.path.split(absolute)
    try:
        parent_fd = open_directory(parent)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    descriptor = None
    try:
        try:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise FileSecurityError("file path contains a symlink or unsafe component") from exc
            raise
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FileSecurityError("configuration file must be a regular file")
        if info.st_uid != os.geteuid():
            raise FileSecurityError("configuration file is not owned by the effective user")
        mode = stat.S_IMODE(info.st_mode)
        if secret and mode != 0o600:
            raise FileSecurityError("Secret configuration permissions must be 0600")
        if not secret and mode & 0o022:
            raise FileSecurityError("configuration file must not be group/world-writable")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise FileSecurityError("configuration file is too large")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileSecurityError("configuration file must be UTF-8") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def identity(path: str, *, secret: bool) -> FileIdentity:
    absolute = lexical_absolute(path)
    parent_fd = open_directory(os.path.dirname(absolute))
    descriptor = None
    try:
        descriptor = os.open(os.path.basename(absolute), _file_flags(), dir_fd=parent_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise FileSecurityError("unsafe file identity")
        mode = stat.S_IMODE(info.st_mode)
        if secret and mode != 0o600:
            raise FileSecurityError("Secret configuration permissions must be 0600")
        if not secret and mode & 0o022:
            raise FileSecurityError("configuration file must not be group/world-writable")
        return FileIdentity(
            info.st_dev, info.st_ino, info.st_uid, mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def ensure_directory(path: str, *, mode: int, exact_mode: bool = False) -> None:
    absolute = lexical_absolute(path)
    parent, name = os.path.split(absolute)
    parent_fd = open_directory(parent)
    descriptor = None
    try:
        try:
            os.mkdir(name, mode=mode, dir_fd=parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        info = os.fstat(descriptor)
        actual = stat.S_IMODE(info.st_mode)
        if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
            raise FileSecurityError("directory is unsafe")
        if exact_mode and actual != mode:
            raise FileSecurityError(f"directory permissions must be {mode:04o}")
        if not exact_mode and actual & 0o022:
            raise FileSecurityError("directory must not be group/world-writable")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def read_regular_bytes(path: str, *, max_bytes: int, secret: bool,
                       missing_ok: bool = False) -> Optional[bytes]:
    text = read_regular_file(path, max_bytes=max_bytes, secret=secret, missing_ok=missing_ok)
    return None if text is None else text.encode("utf-8")


def atomic_write(path: str, data: bytes, *, mode: int = 0o600,
                 replace: bool = True, dir_fd: Optional[int] = None) -> None:
    if not isinstance(data, bytes):
        raise TypeError("atomic_write data must be bytes")
    absolute = lexical_absolute(path)
    parent, name = os.path.split(absolute)
    owns_parent_fd = dir_fd is None
    parent_fd = open_directory(parent) if owns_parent_fd else dir_fd
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.link(
                temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_fd)
        published = True
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if owns_parent_fd:
            os.close(parent_fd)
