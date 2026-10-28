from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import contextmanager

from . import __version__, private_files


SCHEMA_VERSION = 1
TASK_SCHEMA_VERSION = 2
SKILL_VERSION = __version__
MANIFEST_NAME = "manifest.json"
PRIVATE_RELATIVE = Path(".state") / "private.json"


class StateError(ValueError):
    pass


@contextmanager
def task_lock(task_dir: Path):
    """Lock before reading mutable task state; the OS releases it after a crash."""
    if os.name == "nt":
        import win32api
        import win32event
        identity = hashlib.sha256(os.path.normcase(str(task_dir.resolve())).encode()).hexdigest()
        handle = win32event.CreateMutex(None, False, "Local\\video-to-markdown-" + identity)
        acquired = False
        try:
            acquired = win32event.WaitForSingleObject(handle, 0) in (0, 0x80)
            if not acquired:
                raise StateError("task is already running in another process")
            yield
        finally:
            if acquired:
                win32event.ReleaseMutex(handle)
            win32api.CloseHandle(handle)
    else:
        import importlib
        fcntl = importlib.import_module("fcntl")
        # Check the schema without mutating legacy tasks. Mutable data is re-read
        # by the command after acquiring the lock.
        if read_json(task_dir / MANIFEST_NAME).get("schema_version") != TASK_SCHEMA_VERSION:
            raise StateError("old tasks are preserved but cannot resume in v2; no transcription was resubmitted")
        with (task_dir / ".state" / "task.lock").open("a+b") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError("task is already running in another process") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name == ".state":
        private_files.protect(path.parent, 0o700)
    temporary = path.parent / (".%s.%s.%s" % (path.name, os.getpid(), uuid.uuid4().hex))
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            mode,
        )
        os.fchmod(descriptor, mode)
        if os.name == "nt" and mode == 0o600:
            private_files.protect(temporary, mode)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        private_files.replace_file(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, canonical_json(value), mode=mode)


def read_json(path: Path) -> Any:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise StateError("state path is not a regular single-link file: %s" % path)
        data = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise StateError("cannot read state file: %s" % path) from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StateError("state file changed while being read: %s" % path)
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("invalid JSON state: %s" % path) from exc


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def initialize_task(task_dir: Path, *, settings_snapshot: dict) -> tuple[dict, dict]:
    for relative in (
        "assets/frames",
        "assets/motion",
        "source",
        ".state/batches",
    ):
        (task_dir / relative).mkdir(parents=True, exist_ok=True)
    private_files.protect(task_dir / ".state", 0o700)
    task_id = str(uuid.uuid4())
    created = utc_now()
    manifest = {
        "schema_version": TASK_SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        "task_id": task_id,
        "task_dir_name": task_dir.name,
        "created_at": created,
        "updated_at": created,
        "stage": "created",
        "source": None,
        "settings": settings_snapshot,
        "asr": {
            "provider": settings_snapshot.get("asr_provider", "aihub-audio"),
            "state": "not_started",
            "file_id": None,
            "uploaded_at": None,
            "task_id": None,
            "url_expiry_notice": None,
        },
        "batches": [],
        "artifacts": {
            "document": "document.md",
            "transcript": "transcript.json",
            "visual_observations": "visual-observations.json",
            "review_report": "review-report.json",
        },
        "validation": {"complete": False, "checked_at": None, "blockers": []},
    }
    private = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "generation": 1,
        "transcription": {
            "default_provider": settings_snapshot.get("asr_provider", "aihub-audio"),
            "provider": settings_snapshot.get("asr_provider", "aihub-audio"),
            "status": "new", "attempted": [], "automatic_switches": 0, "providers": {},
        },
    }
    atomic_write_json(task_dir / MANIFEST_NAME, manifest)
    atomic_write_json(task_dir / PRIVATE_RELATIVE, private)
    return manifest, private


def load_task(task_dir_value: str | os.PathLike[str]) -> tuple[Path, dict, dict]:
    task_dir = Path(task_dir_value).expanduser().resolve()
    try:
        private_files.verify_private(task_dir / ".state", 0o700)
        private_files.verify_private(task_dir / PRIVATE_RELATIVE, 0o600)
    except (OSError, ValueError) as exc:
        raise StateError(str(exc)) from exc
    manifest = read_json(task_dir / MANIFEST_NAME)
    private = read_json(task_dir / PRIVATE_RELATIVE)
    if not isinstance(manifest, dict) or not isinstance(private, dict):
        raise StateError("task state must be JSON objects")
    if manifest.get("schema_version") != TASK_SCHEMA_VERSION:
        raise StateError("unsupported task schema: old tasks are preserved but cannot resume in v2; no transcription was resubmitted")
    if private.get("schema_version") != TASK_SCHEMA_VERSION:
        raise StateError("unsupported private state schema")
    if manifest.get("task_id") != private.get("task_id"):
        raise StateError("manifest and private state task IDs differ")
    return task_dir, manifest, private


def save_manifest(task_dir: Path, manifest: dict) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(task_dir / MANIFEST_NAME, manifest)


def save_private(task_dir: Path, private: dict) -> None:
    private["generation"] = int(private.get("generation", 0)) + 1
    atomic_write_json(task_dir / PRIVATE_RELATIVE, private)


def create_empty_artifacts(task_dir: Path) -> None:
    if not (task_dir / "visual-observations.json").exists():
        atomic_write_json(
            task_dir / "visual-observations.json",
            {"schema_version": SCHEMA_VERSION, "revisions": []},
        )
    if not (task_dir / "document.md").exists():
        atomic_write_bytes(
            task_dir / "document.md",
            b"# Video document\n\n",
            mode=0o644,
        )
