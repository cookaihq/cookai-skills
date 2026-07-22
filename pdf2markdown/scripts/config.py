from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


MAX_DOTENV_BYTES = 1024 * 1024
KEY_NAME = "AIHUB_API_KEY"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Credential:
    value: str
    source_id: str
    fingerprint: str
    locator: dict[str, str]

    @property
    def public_identity(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "fingerprint": self.fingerprint,
            "locator": dict(self.locator),
        }


def parse_dotenv(text: str) -> dict[str, str]:
    values = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split("=", 1)
        if raw_key.strip() != KEY_NAME:
            continue
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError("invalid quoted dotenv value")
            value = value[1:-1]
        values[KEY_NAME] = value
    return values


def _read_bounded_dotenv(path: Path) -> dict[str, str]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(path, follow_symlinks=False)
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return {}
        raise ConfigError("dotenv file is unavailable or unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_DOTENV_BYTES
        ):
            raise ConfigError("dotenv file is not a bounded regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(
                descriptor, min(65536, MAX_DOTENV_BYTES + 1 - size)
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_DOTENV_BYTES:
                raise ConfigError("dotenv file is too large")
        final = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise ConfigError("dotenv file changed while it was read")
        try:
            return parse_dotenv(b"".join(chunks).decode("utf-8"))
        except UnicodeError as exc:
            raise ConfigError("dotenv file must be UTF-8") from exc
    except OSError as exc:
        raise ConfigError("dotenv file changed or became unavailable while read") from exc
    finally:
        os.close(descriptor)


def _lexical_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def resolve_api_key(
    *,
    environ: dict[str, str],
    cwd: Path,
    config_home: Path,
    use_local_key: bool,
) -> Credential:
    dotenv_local = _lexical_absolute_path(cwd / ".env.local")
    dotenv = _lexical_absolute_path(cwd / ".env")
    home_dotenv = _lexical_absolute_path(config_home / ".env")
    candidates = [
        (
            lambda: environ.get(KEY_NAME),
            f"process_environment:{KEY_NAME}",
            {"kind": "process_environment", "name": KEY_NAME},
        ),
        (
            lambda: _read_bounded_dotenv(dotenv_local).get(KEY_NAME),
            f"dotenv:{dotenv_local}:{KEY_NAME}",
            {
                "kind": "dotenv",
                "path": str(dotenv_local),
                "name": KEY_NAME,
            },
        ),
        (
            lambda: _read_bounded_dotenv(dotenv).get(KEY_NAME),
            f"dotenv:{dotenv}:{KEY_NAME}",
            {
                "kind": "dotenv",
                "path": str(dotenv),
                "name": KEY_NAME,
            },
        ),
    ]
    if use_local_key:
        candidates.append(
            (
                lambda: _read_bounded_dotenv(home_dotenv).get(KEY_NAME),
                f"dotenv:{home_dotenv}:{KEY_NAME}",
                {
                    "kind": "dotenv",
                    "path": str(home_dotenv),
                    "name": KEY_NAME,
                },
            )
        )
    for load_candidate, source_id, locator in candidates:
        candidate = load_candidate()
        if not isinstance(candidate, str):
            continue
        value = candidate.strip()
        if not value:
            continue
        return Credential(
            value=value,
            source_id=source_id,
            fingerprint=f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}",
            locator=locator,
        )
    raise ConfigError("AIHUB_API_KEY is not configured")


def mask_key(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"
