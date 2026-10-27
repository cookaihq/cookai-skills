"""Checked files, process locks and durable replacement on POSIX and Windows.

POSIX files/directories use 0600/0700. Windows inherits the user's directory
ACLs; chmod is not an ACL privacy guarantee. Windows cannot fsync directories
through Python's os API, but file contents are flushed before os.replace.
"""
import errno
import hashlib
import os
import stat
import tempfile


class UnsafePathError(OSError):
    """The path is not an owned, unlinked regular file or directory."""


def _check_link_and_owner(st, path, ctx):
    reparse = getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(st.st_mode) or reparse:
        raise UnsafePathError("%s: symlink/reparse point not allowed: %r" % (ctx, path))
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise UnsafePathError("%s: unexpected owner: %r" % (ctx, path))


def check_safe_regular_file(path, ctx="path"):
    st = os.lstat(path)
    _check_link_and_owner(st, path, ctx)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise UnsafePathError("%s: single-link regular file required: %r" % (ctx, path))
    return st


def check_safe_dir(path, ctx="dir"):
    st = os.lstat(path)
    _check_link_and_owner(st, path, ctx)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError("%s: directory required: %r" % (ctx, path))
    return st


def open_regular(path, flags, ctx="file"):
    before = check_safe_regular_file(path, ctx) if os.path.lexists(path) else None
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        current = check_safe_regular_file(path, ctx)
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino)
        if identity != (current.st_dev, current.st_ino) or (
            before is not None and identity != (before.st_dev, before.st_ino)
        ):
            raise UnsafePathError("%s: file changed while opening" % ctx)
        return fd
    except BaseException:
        os.close(fd)
        raise


def lock_exclusive(fd):
    """Nonblocking OS lock released on close, including when the process dies."""
    if os.name == "nt":
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError("work bundle is locked") from None
            raise
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def ensure_private_dir(path, ctx="dir"):
    if not os.path.isabs(path):
        raise UnsafePathError("%s: absolute path required" % ctx)
    os.makedirs(path, mode=0o700, exist_ok=True)
    check_safe_dir(path, ctx)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def fsync_dir(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("short file write")
        remaining = remaining[count:]


def read_file_sha256(path, ctx="file"):
    digest = hashlib.sha256()
    with os.fdopen(open_regular(path, os.O_RDONLY, ctx), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit(path, data, ctx, replace):
    if not os.path.isabs(path):
        raise UnsafePathError("%s: absolute path required" % ctx)
    directory = os.path.dirname(path)
    check_safe_dir(directory, ctx + ".parent")
    if os.path.lexists(path):
        if not replace:
            raise UnsafePathError("%s: refusing to overwrite existing path" % ctx)
        check_safe_regular_file(path, ctx)
    # Unique names permit recovery after an interrupted writer, without deleting
    # another writer's partial file. All work-bundle mutations hold the OS lock.
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if not replace and os.path.lexists(path):
            raise UnsafePathError("%s: destination appeared while writing" % ctx)
        os.replace(tmp_path, path)
        fsync_dir(directory)
    finally:
        if os.path.lexists(tmp_path):
            os.unlink(tmp_path)
    digest = read_file_sha256(path, ctx)
    if digest != hashlib.sha256(data).hexdigest():
        raise UnsafePathError("%s: readback SHA-256 mismatch" % ctx)
    return digest


def atomic_write_bytes(path, data, ctx="file"):
    return _commit(path, data, ctx, False)


def atomic_replace_bytes(path, data, ctx="file"):
    return _commit(path, data, ctx, True)


def append_bytes_fsync(path, data, ctx="file"):
    check_safe_dir(os.path.dirname(path), ctx + ".parent")
    fd = open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, ctx)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
