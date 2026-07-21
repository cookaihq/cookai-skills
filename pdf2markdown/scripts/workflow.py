#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_SLUG_BYTES = 200
SUPPORTED_CONVERSION_STATES = frozenset({"preparing"})
SUPPORTED_PUBLICATION_STATES = frozenset({"not_requested"})


class WorkflowError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        return_code: int,
        action_required: str,
        context=None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.return_code = return_code
        self.action_required = action_required
        self.context = {} if context is None else context


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise WorkflowError(
            "invalid_arguments",
            "Command arguments are invalid.",
            return_code=2,
            action_required="correct_command_arguments",
        )


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Create and inspect pdf2markdown work bundles", add_help=False
    )
    subcommands = parser.add_subparsers(
        dest="command", required=True, parser_class=JsonArgumentParser
    )
    start = subcommands.add_parser("start", add_help=False)
    start.add_argument("--source", required=True)
    start.add_argument("--output-dir")
    inspect = subcommands.add_parser("inspect", add_help=False)
    inspect.add_argument("--work-bundle", "--bundle", dest="work_bundle", required=True)
    resume = subcommands.add_parser("resume", add_help=False)
    resume.add_argument("--work-bundle", "--bundle", dest="work_bundle", required=True)
    resume.add_argument("--expected-generation", required=True, type=int)
    return parser


def _absolute(path: str, cwd: Path) -> Path:
    value = Path(path).expanduser()
    return value if value.is_absolute() else cwd / value


def _canonical_bundle_path(path: str, cwd: Path) -> Path:
    candidate = _absolute(path, cwd)
    try:
        candidate_info = os.lstat(candidate)
    except (OSError, RuntimeError):
        candidate_info = None
    try:
        if candidate_info is not None and stat.S_ISLNK(candidate_info.st_mode):
            return candidate.parent.resolve(strict=True) / candidate.name
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return candidate


def _slug(source_name: str) -> str:
    stem = Path(source_name).stem.lower()
    value = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return (value or "document")[:MAX_SOURCE_SLUG_BYTES].rstrip("-")


def _moment(now) -> datetime:
    value = now() if callable(now) else now
    return value if value is not None else datetime.now().astimezone()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_private_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _append_history(path: Path, event: dict) -> None:
    data = (json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _commit_staging_bundle(staging: Path, output_root: Path, base_name: str) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    root_descriptor = os.open(str(output_root), flags)
    try:
        fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        bundle = output_root / base_name
        suffix = 2
        while os.path.lexists(str(bundle)):
            bundle = output_root / f"{base_name}-{suffix}"
            suffix += 1
        os.rename(staging, bundle)
        os.fsync(root_descriptor)
        return bundle
    finally:
        fcntl.flock(root_descriptor, fcntl.LOCK_UN)
        os.close(root_descriptor)


def _isoformat(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.astimezone()
    value = moment.isoformat()
    return value[:-6] + "Z" if value.endswith("+00:00") else value


def _is_timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _copy_source(source_path: Path, destination: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(str(source_path), flags)
    except OSError as exc:
        try:
            path_info = os.lstat(source_path)
        except OSError:
            path_info = None
        if exc.errno in {errno.ELOOP, errno.EMLINK} or (
            path_info is not None and stat.S_ISLNK(path_info.st_mode)
        ):
            raise WorkflowError(
                "unsafe_source_type",
                "Local source must be a regular file and cannot be a symlink.",
                return_code=3,
                action_required="provide_valid_local_pdf",
            ) from None
        raise WorkflowError(
            "source_unreadable",
            "Local source could not be opened for reading.",
            return_code=3,
            action_required="provide_valid_local_pdf",
        ) from None
    try:
        source_info = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_info.st_mode):
            raise WorkflowError(
                "unsafe_source_type",
                "Local source must be a regular file and cannot be a symlink.",
                return_code=3,
                action_required="provide_valid_local_pdf",
            )
        destination_descriptor = os.open(
            str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.fchmod(destination_descriptor, 0o600)
            digest = hashlib.sha256()
            size = 0
            header = bytearray()
            with os.fdopen(source_descriptor, "rb", closefd=False) as source, os.fdopen(
                destination_descriptor, "wb", closefd=False
            ) as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    if len(header) < 5:
                        header.extend(chunk[: 5 - len(header)])
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            final_info = os.fstat(source_descriptor)
            identity_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            if size != source_info.st_size or any(
                getattr(source_info, field) != getattr(final_info, field) for field in identity_fields
            ):
                raise WorkflowError(
                    "source_changed",
                    "Local source changed while it was being copied.",
                    return_code=3,
                    action_required="provide_valid_local_pdf",
                )
            if bytes(header) != b"%PDF-":
                raise WorkflowError(
                    "invalid_pdf",
                    "Local source does not contain PDF bytes.",
                    return_code=3,
                    action_required="provide_valid_local_pdf",
                )
            return digest.hexdigest(), size
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _default_settings_snapshot() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
        "sources": {
            "interaction_mode": "built_in_default",
            "publishing.mode": "built_in_default",
        },
    }


def _is_default_settings_snapshot(value) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and value == _default_settings_snapshot()
    )


def _start(args, *, environ: dict[str, str], cwd: Path, now) -> dict:
    output_value = args.output_dir or environ.get("PDF2MARKDOWN_OUTPUT_DIR") or "pdf2markdown-output"
    output_root = _absolute(output_value, cwd)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_root = output_root.resolve(strict=True)
    source_path = _absolute(args.source, cwd)
    staging = Path(tempfile.mkdtemp(prefix=".pdf2markdown-", dir=str(output_root)))
    staging.chmod(0o700)
    try:
        directories = [
            staging / ".state",
            staging / "01-source",
            staging / "02-pages",
            staging / "03-converted",
            staging / "03-converted" / "attempts",
            staging / "04-review",
            staging / "05-published",
        ]
        for directory in directories:
            _mkdir(directory)
        state_dir = staging / ".state"
        source_dir = staging / "01-source"
        _create_private_file(state_dir / "lock")
        source_hash, source_size = _copy_source(source_path, source_dir / "source.pdf")
        started_at = _moment(now)
        timestamp = started_at.strftime("%Y%m%d-%H%M%S")
        base_name = f"{timestamp}-{_slug(source_path.name)}-{source_hash[:8]}"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generation": 1,
            "conversion_state": "preparing",
            "publication_state": "not_requested",
            "source": {
                "original_name": source_path.name,
                "origin": {"kind": "local", "path": str(source_path)},
                "physical_path": "01-source/source.pdf",
                "sha256": source_hash,
                "size_bytes": source_size,
            },
            "settings_snapshot": _default_settings_snapshot(),
            "conversion_attempts": [],
            "final_markdown": None,
            "artifacts": {"source_pdf": "01-source/source.pdf"},
        }
        private_state = {
            "schema_version": SCHEMA_VERSION,
            "generation": 1,
            "source_uploads": [],
            "result_urls": [],
        }
        _atomic_write_json(state_dir / "private.json", private_state)
        _append_history(
            state_dir / "history.ndjson",
            {
                "schema_version": SCHEMA_VERSION,
                "event": "bundle_started",
                "generation": 1,
                "at": _isoformat(started_at),
                "source_sha256": source_hash,
            },
        )
        _atomic_write_json(staging / "manifest.json", manifest)
        bundle = _commit_staging_bundle(staging, output_root, base_name)
        result = {
            "schema_version": SCHEMA_VERSION,
            "work_bundle": str(bundle),
            "generation": 1,
            "conversion_state": "preparing",
            "publication_state": "not_requested",
            "outcome": "created",
            "action_required": None,
            "action_id": None,
            "evidence_hash": f"sha256:{source_hash}",
            "artifacts": {
                "manifest": "manifest.json",
                "source_pdf": "01-source/source.pdf",
            },
            "errors": [],
        }
        print(f"[pdf2markdown] created work bundle {bundle.name}", file=sys.stderr)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _stat_at(name, *, dir_fd=None):
    if dir_fd is None:
        return os.stat(name, follow_symlinks=False)
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_at(name, flags: int, *, dir_fd=None) -> int:
    if dir_fd is None:
        return os.open(os.fspath(name), flags)
    return os.open(os.fspath(name), flags, dir_fd=dir_fd)


def _open_private_directory(name, *, dir_fd=None) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        path_info = _stat_at(name, dir_fd=dir_fd)
        descriptor = _open_at(name, flags, dir_fd=dir_fd)
    except OSError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle directory is missing or unsafe.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        ) from None
    opened_info = os.fstat(descriptor)
    if (
        (opened_info.st_dev, opened_info.st_ino) != (path_info.st_dev, path_info.st_ino)
        or not stat.S_ISDIR(opened_info.st_mode)
        or stat.S_IMODE(opened_info.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle directories must be private real directories.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        )
    return descriptor


def _open_private_file(name, *, dir_fd: int, max_bytes: int, writable: bool = False):
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        path_info = _stat_at(name, dir_fd=dir_fd)
        descriptor = _open_at(name, flags, dir_fd=dir_fd)
    except OSError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state file is missing or unsafe.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        ) from None
    opened_info = os.fstat(descriptor)
    if (
        (opened_info.st_dev, opened_info.st_ino) != (path_info.st_dev, path_info.st_ino)
        or not stat.S_ISREG(opened_info.st_mode)
        or opened_info.st_nlink != 1
        or stat.S_IMODE(opened_info.st_mode) != 0o600
        or opened_info.st_size > max_bytes
    ):
        os.close(descriptor)
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state files must be bounded private regular files.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        )
    return descriptor, opened_info


def _read_private_file(name, *, dir_fd: int, max_bytes: int = MAX_STATE_BYTES) -> bytes:
    descriptor, opened_info = _open_private_file(
        name, dir_fd=dir_fd, max_bytes=max_bytes
    )
    try:
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise WorkflowError(
                    "invalid_bundle",
                    "Work bundle state file exceeds its size limit.",
                    return_code=4,
                    action_required="repair_or_restore_work_bundle",
                )
        final_info = os.fstat(descriptor)
        if (
            final_info.st_size != size
            or (
                final_info.st_dev,
                final_info.st_ino,
                final_info.st_mtime_ns,
                final_info.st_ctime_ns,
            )
            != (
                opened_info.st_dev,
                opened_info.st_ino,
                opened_info.st_mtime_ns,
                opened_info.st_ctime_ns,
            )
        ):
            raise WorkflowError(
                "invalid_bundle",
                "Work bundle state changed while it was being read.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_json(name, *, dir_fd: int) -> dict:
    try:
        value = json.loads(_read_private_file(name, dir_fd=dir_fd).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state could not be read.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        ) from None
    if not isinstance(value, dict):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state must be a JSON object.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        )
    return value


def _source_digest(name, *, dir_fd: int) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        path_info = _stat_at(name, dir_fd=dir_fd)
        descriptor = _open_at(name, flags, dir_fd=dir_fd)
    except OSError:
        raise WorkflowError(
            "integrity_violation",
            "The saved source PDF is missing or unsafe.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        ) from None
    try:
        opened_info = os.fstat(descriptor)
        if (
            (opened_info.st_dev, opened_info.st_ino) != (path_info.st_dev, path_info.st_ino)
            or not stat.S_ISREG(opened_info.st_mode)
            or opened_info.st_nlink != 1
            or stat.S_IMODE(opened_info.st_mode) != 0o600
        ):
            raise WorkflowError(
                "integrity_violation",
                "The saved source PDF is not a private regular file.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
            )
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        final_info = os.fstat(descriptor)
        if (
            final_info.st_size != size
            or (
                final_info.st_dev,
                final_info.st_ino,
                final_info.st_mtime_ns,
                final_info.st_ctime_ns,
            )
            != (
                opened_info.st_dev,
                opened_info.st_ino,
                opened_info.st_mtime_ns,
                opened_info.st_ctime_ns,
            )
        ):
            raise WorkflowError(
                "integrity_violation",
                "The saved source PDF changed while it was being inspected.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
            )
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _validate_bundle_root_descriptors(root: int, state: int, lock: int) -> None:
    root_info = os.fstat(root)
    state_info = os.fstat(state)
    lock_info = os.fstat(lock)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or not stat.S_ISDIR(state_info.st_mode)
        or stat.S_IMODE(state_info.st_mode) != 0o700
        or not stat.S_ISREG(lock_info.st_mode)
        or lock_info.st_nlink != 1
        or stat.S_IMODE(lock_info.st_mode) != 0o600
        or lock_info.st_size != 0
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle lock is not attached to a private work bundle.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        )


@contextmanager
def _open_bundle_descriptors(bundle: Path, *, locked_descriptors=None):
    opened = []
    try:
        if locked_descriptors is None:
            root = _open_private_directory(bundle)
            opened.append(root)
            state = _open_private_directory(".state", dir_fd=root)
            opened.append(state)
            lock, _lock_info = _open_private_file(
                "lock", dir_fd=state, max_bytes=0
            )
            opened.append(lock)
        else:
            root, state, lock = locked_descriptors
        _validate_bundle_root_descriptors(root, state, lock)

        source = _open_private_directory("01-source", dir_fd=root)
        opened.append(source)
        pages = _open_private_directory("02-pages", dir_fd=root)
        opened.append(pages)
        converted = _open_private_directory("03-converted", dir_fd=root)
        opened.append(converted)
        attempts = _open_private_directory("attempts", dir_fd=converted)
        opened.append(attempts)
        review = _open_private_directory("04-review", dir_fd=root)
        opened.append(review)
        published = _open_private_directory("05-published", dir_fd=root)
        opened.append(published)
        yield {
            "root": root,
            "state": state,
            "lock": lock,
            "source": source,
            "pages": pages,
            "converted": converted,
            "attempts": attempts,
            "review": review,
            "published": published,
        }
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _assert_bundle_descriptors_current(bundle: Path, descriptors: dict) -> None:
    locations = (
        (bundle, None, "root"),
        (".state", descriptors["root"], "state"),
        ("lock", descriptors["state"], "lock"),
        ("01-source", descriptors["root"], "source"),
        ("02-pages", descriptors["root"], "pages"),
        ("03-converted", descriptors["root"], "converted"),
        ("attempts", descriptors["converted"], "attempts"),
        ("04-review", descriptors["root"], "review"),
        ("05-published", descriptors["root"], "published"),
    )
    try:
        for name, parent, descriptor_name in locations:
            current = _stat_at(name, dir_fd=parent)
            opened = os.fstat(descriptors[descriptor_name])
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(errno.ESTALE, "work bundle identity changed")
    except OSError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle identity changed while it was being inspected.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context={"work_bundle": str(bundle)},
        ) from None


def _inspect_open_bundle(bundle: Path, descriptors: dict) -> dict:
    try:
        manifest = _read_json("manifest.json", dir_fd=descriptors["root"])
    except WorkflowError as exc:
        exc.context = {"work_bundle": str(bundle)}
        raise
    required = {
        "schema_version",
        "generation",
        "conversion_state",
        "publication_state",
        "source",
        "settings_snapshot",
        "conversion_attempts",
        "final_markdown",
        "artifacts",
    }
    source = manifest.get("source")
    origin = source.get("origin") if isinstance(source, dict) else None
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or set(manifest) != required
        or type(manifest.get("generation")) is not int
        or manifest["generation"] != 1
        or manifest.get("conversion_state") not in SUPPORTED_CONVERSION_STATES
        or manifest.get("publication_state") not in SUPPORTED_PUBLICATION_STATES
        or not isinstance(source, dict)
        or set(source)
        != {"original_name", "origin", "physical_path", "sha256", "size_bytes"}
        or not isinstance(source.get("original_name"), str)
        or not source["original_name"]
        or not isinstance(origin, dict)
        or set(origin) != {"kind", "path"}
        or origin.get("kind") != "local"
        or not isinstance(origin.get("path"), str)
        or not origin["path"]
        or source.get("physical_path") != "01-source/source.pdf"
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        or type(source.get("size_bytes")) is not int
        or source["size_bytes"] < 0
        or not _is_default_settings_snapshot(manifest.get("settings_snapshot"))
        or manifest.get("conversion_attempts") != []
        or manifest.get("final_markdown") is not None
        or manifest.get("artifacts") != {"source_pdf": "01-source/source.pdf"}
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state uses an unknown or incomplete schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context={"work_bundle": str(bundle)},
        )
    state_context = {
        "work_bundle": str(bundle),
        "generation": manifest["generation"],
        "conversion_state": manifest["conversion_state"],
        "publication_state": manifest["publication_state"],
        "artifacts": {
            "manifest": "manifest.json",
            "source_pdf": "01-source/source.pdf",
        },
    }
    try:
        private_state = _read_json("private.json", dir_fd=descriptors["state"])
        history_data = _read_private_file(
            "history.ndjson", dir_fd=descriptors["state"]
        )
    except WorkflowError as exc:
        exc.context = state_context
        raise
    if (
        type(private_state.get("schema_version")) is not int
        or private_state["schema_version"] != SCHEMA_VERSION
        or set(private_state)
        != {"schema_version", "generation", "source_uploads", "result_urls"}
        or type(private_state.get("generation")) is not int
        or private_state["generation"] != manifest["generation"]
        or private_state.get("source_uploads") != []
        or private_state.get("result_urls") != []
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Private work bundle state uses an unknown or inconsistent schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        )
    try:
        history = [json.loads(line) for line in history_data.decode("utf-8").splitlines()]
    except (UnicodeError, json.JSONDecodeError):
        history = []
    if (
        len(history) != 1
        or not isinstance(history[0], dict)
        or set(history[0])
        != {"schema_version", "event", "generation", "at", "source_sha256"}
        or type(history[0].get("schema_version")) is not int
        or history[0]["schema_version"] != SCHEMA_VERSION
        or history[0].get("event") != "bundle_started"
        or type(history[0].get("generation")) is not int
        or history[0]["generation"] != manifest["generation"]
        or not _is_timestamp(history[0].get("at"))
        or history[0].get("source_sha256") != source.get("sha256")
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle history uses an unknown or inconsistent schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        )
    try:
        digest, size = _source_digest("source.pdf", dir_fd=descriptors["source"])
    except WorkflowError as exc:
        exc.context = state_context
        raise
    if digest != source.get("sha256") or size != source.get("size_bytes"):
        raise WorkflowError(
            "integrity_violation",
            "The saved source PDF does not match the manifest.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        )
    _assert_bundle_descriptors_current(bundle, descriptors)
    print(f"[pdf2markdown] inspected work bundle {bundle.name}", file=sys.stderr)
    return {
        "schema_version": SCHEMA_VERSION,
        "work_bundle": str(bundle),
        "generation": manifest["generation"],
        "conversion_state": manifest["conversion_state"],
        "publication_state": manifest["publication_state"],
        "outcome": "inspected",
        "action_required": None,
        "action_id": None,
        "evidence_hash": f"sha256:{digest}",
        "artifacts": {
            "manifest": "manifest.json",
            "source_pdf": "01-source/source.pdf",
        },
        "errors": [],
    }


def _inspect_bundle(bundle: Path, *, locked_descriptors=None) -> dict:
    try:
        with _open_bundle_descriptors(
            bundle, locked_descriptors=locked_descriptors
        ) as descriptors:
            return _inspect_open_bundle(bundle, descriptors)
    except WorkflowError as exc:
        if not exc.context:
            exc.context = {"work_bundle": str(bundle)}
        raise


def _inspect(args, *, cwd: Path) -> dict:
    return _inspect_bundle(_canonical_bundle_path(args.work_bundle, cwd))


@contextmanager
def _exclusive_bundle_lock(bundle: Path):
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    lock_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    bundle_descriptor = None
    state_descriptor = None
    lock_descriptor = None
    lock_acquired = False
    try:
        try:
            bundle_descriptor = os.open(str(bundle), directory_flags)
            state_descriptor = os.open(".state", directory_flags, dir_fd=bundle_descriptor)
            lock_descriptor = os.open("lock", lock_flags, dir_fd=state_descriptor)
        except OSError:
            raise WorkflowError(
                "invalid_bundle",
                "Work bundle lock is missing or unsafe.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context={"work_bundle": str(bundle)},
            ) from None

        bundle_info = os.fstat(bundle_descriptor)
        state_info = os.fstat(state_descriptor)
        lock_info = os.fstat(lock_descriptor)
        if (
            not stat.S_ISDIR(bundle_info.st_mode)
            or stat.S_IMODE(bundle_info.st_mode) != 0o700
            or not stat.S_ISDIR(state_info.st_mode)
            or stat.S_IMODE(state_info.st_mode) != 0o700
            or not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) != 0o600
        ):
            raise WorkflowError(
                "invalid_bundle",
                "Work bundle lock is not attached to a private work bundle.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context={"work_bundle": str(bundle)},
            )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise WorkflowError(
                    "bundle_locked",
                    "Another writer currently owns the work bundle lock.",
                    return_code=5,
                    action_required="retry_after_writer_finishes",
                    context={"work_bundle": str(bundle)},
                ) from None
            raise

        expected_identities = (
            (bundle, bundle_info),
            (bundle / ".state", state_info),
            (bundle / ".state" / "lock", lock_info),
        )

        def assert_current_identity():
            try:
                current = [os.stat(path, follow_symlinks=False) for path, _info in expected_identities]
            except OSError:
                current = []
            if len(current) != len(expected_identities) or any(
                (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                for actual, (_path, expected) in zip(current, expected_identities)
            ):
                raise WorkflowError(
                    "invalid_bundle",
                    "Work bundle identity changed while its write lock was held.",
                    return_code=4,
                    action_required="repair_or_restore_work_bundle",
                    context={"work_bundle": str(bundle)},
                )

        assert_current_identity()
        try:
            yield (bundle_descriptor, state_descriptor, lock_descriptor)
        except Exception:
            raise
        else:
            assert_current_identity()
    finally:
        if lock_acquired:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        for descriptor in (lock_descriptor, state_descriptor, bundle_descriptor):
            if descriptor is not None:
                os.close(descriptor)


def _resume(args, *, cwd: Path) -> dict:
    bundle = _canonical_bundle_path(args.work_bundle, cwd)
    with _exclusive_bundle_lock(bundle) as locked_descriptors:
        result = _inspect_bundle(bundle, locked_descriptors=locked_descriptors)
        if result["generation"] != args.expected_generation:
            raise WorkflowError(
                "generation_conflict",
                "Expected generation does not match the current work bundle state.",
                return_code=5,
                action_required="inspect_current_generation",
                context=result,
            )
        result["outcome"] = "no_progress"
        print(f"[pdf2markdown] no_progress for work bundle {bundle.name}", file=sys.stderr)
        return result


def main(
    argv=None,
    *,
    environ=None,
    cwd=None,
    config_home=None,
    transport=None,
    now=None,
) -> int:
    del config_home, transport
    try:
        args = _parser().parse_args(argv)
        environment = dict(os.environ if environ is None else environ)
        invocation_cwd = Path(os.getcwd() if cwd is None else cwd)
        if args.command == "start":
            result = _start(args, environ=environment, cwd=invocation_cwd, now=now)
        elif args.command == "inspect":
            result = _inspect(args, cwd=invocation_cwd)
        else:
            result = _resume(args, cwd=invocation_cwd)
        return_code = 0
    except WorkflowError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "work_bundle": exc.context.get("work_bundle"),
            "generation": exc.context.get("generation"),
            "conversion_state": exc.context.get("conversion_state"),
            "publication_state": exc.context.get("publication_state"),
            "outcome": "error",
            "action_required": exc.action_required,
            "action_id": None,
            "evidence_hash": exc.context.get("evidence_hash"),
            "artifacts": exc.context.get("artifacts", {}),
            "errors": [{"code": exc.code, "message": exc.message}],
        }
        return_code = exc.return_code
        print(f"[pdf2markdown] {exc.code}", file=sys.stderr)
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "work_bundle": None,
            "generation": None,
            "conversion_state": None,
            "publication_state": None,
            "outcome": "error",
            "action_required": "inspect_runtime_error",
            "action_id": None,
            "evidence_hash": None,
            "artifacts": {},
            "errors": [{"code": "internal_error", "message": type(exc).__name__}],
        }
        return_code = 1
        print(f"[pdf2markdown] internal_error: {type(exc).__name__}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
