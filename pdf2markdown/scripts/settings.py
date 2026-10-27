"""Layered output-directory configuration; no persistent v1 settings writes."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

MAX_SETTINGS_BYTES = 1024 * 1024


class SettingsError(ValueError):
    pass


def _read_bounded_file(path: Path, *, nofollow: bool) -> bytes | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(path, follow_symlinks=not nofollow)
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise SettingsError("settings file is unavailable or unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_SETTINGS_BYTES
        ):
            raise SettingsError("settings file is not a bounded regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_SETTINGS_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_SETTINGS_BYTES:
                raise SettingsError("settings file is too large")
        final = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=not nofollow)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise SettingsError("settings file changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def parse_dotenv(text: str) -> dict[str, str]:
    recognized = {"PDF2MARKDOWN_OUTPUT_DIR"}
    values = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split("=", 1)
        key = raw_key.strip()
        if key not in recognized:
            continue
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise SettingsError("invalid quoted dotenv value")
            value = value[1:-1]
        values[key] = value
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    data = _read_bounded_file(path, nofollow=True)
    if data is None:
        return {}
    try:
        return parse_dotenv(data.decode("utf-8"))
    except UnicodeError as exc:
        raise SettingsError("dotenv file must be UTF-8") from exc


def _nonempty(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError("setting values must be strings")
    return value if value.strip() else None


def resolve_variable(name: str, *, environ: dict[str, str], cwd: Path, config_home: Path, use_local_key: bool) -> str | None:
    value = _nonempty(environ.get(name))
    if value is not None:
        return value
    paths = [cwd / filename for filename in (".env.pdf2markdown", ".env.local", ".env")]
    if use_local_key:
        paths.append(config_home / ".env")
    for path in paths:
        value = _nonempty(_read_dotenv(path).get(name))
        if value is not None:
            return value
    return None
