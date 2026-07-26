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
from datetime import datetime, timedelta
from pathlib import Path

import aihub_upload
import bundle as bundle_module
import config as config_module
import correction as correction_module
import conversion_attempt as conversion_attempt_module
import doc2x as doc2x_module
import pdf_source
import preflight as preflight_module
import raw_conversion as raw_conversion_module
import review as review_module
import settings as settings_module
import source_staging as source_staging_module


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_SLUG_BYTES = 200
SUPPORTED_CONVERSION_STATES = frozenset(
    {
        "preparing",
        "preflight_pending",
        "preflight_warning",
        "preflight_blocked",
        "ready_to_submit",
        "submitting",
        "submitted",
        "result_downloading",
        "converted",
        "review_pending",
        "local_complete",
        "submission_unknown",
        "awaiting_user",
        "recoverable_error",
        "terminal_error",
    }
)
SUPPORTED_PUBLICATION_STATES = frozenset({"not_requested", "blocked"})


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
    start.add_argument("--interaction-mode", choices=("confirm", "auto"))
    start.add_argument("--publish-mode", choices=("skip", "upload"))
    start.add_argument("--publish-with")
    start.add_argument("--publish-target")
    start.add_argument("--use-local-key", action="store_true")
    inspect = subcommands.add_parser("inspect", add_help=False)
    inspect.add_argument("--work-bundle", "--bundle", dest="work_bundle", required=True)
    advance = subcommands.add_parser("advance", add_help=False)
    advance.add_argument("--work-bundle", "--bundle", dest="work_bundle", required=True)
    advance.add_argument("--expected-generation", required=True, type=int)
    advance.add_argument(
        "--visual-capability", choices=("available", "unavailable"), required=True
    )
    advance.add_argument(
        "--render-dpi", type=int, default=preflight_module.DEFAULT_RENDER_DPI
    )
    advance.add_argument("--use-local-key", action="store_true")
    record = subcommands.add_parser("record", add_help=False)
    record_commands = record.add_subparsers(
        dest="record_command", required=True, parser_class=JsonArgumentParser
    )
    record_preflight = record_commands.add_parser("preflight", add_help=False)
    record_preflight.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_preflight.add_argument("--expected-generation", required=True, type=int)
    record_preflight.add_argument("--action-id", required=True)
    record_preflight.add_argument("--evidence-hash", required=True)
    record_preflight.add_argument("--input", required=True)
    record_decision = record_commands.add_parser("decision", add_help=False)
    record_decision.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_decision.add_argument("--expected-generation", required=True, type=int)
    record_decision.add_argument("--action-id", required=True)
    record_decision.add_argument("--evidence-hash", required=True)
    record_decision.add_argument("--decision", choices=("accept", "decline"), required=True)
    record_decision.add_argument("--basis", required=True)
    record_staging = record_commands.add_parser("source-staging", add_help=False)
    record_staging.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_staging.add_argument("--expected-generation", required=True, type=int)
    record_staging.add_argument("--action-id", required=True)
    record_staging.add_argument("--evidence-hash", required=True)
    record_staging.add_argument("--decision", choices=("retry", "wait"), required=True)
    record_staging.add_argument("--basis", required=True)
    record_conversion = record_commands.add_parser("conversion", add_help=False)
    record_conversion.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_conversion.add_argument("--expected-generation", required=True, type=int)
    record_conversion.add_argument("--action-id", required=True)
    record_conversion.add_argument("--evidence-hash", required=True)
    record_conversion.add_argument("--decision", choices=("retry",), required=True)
    record_conversion.add_argument("--basis", required=True)
    record_review = record_commands.add_parser("review", add_help=False)
    record_review.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_review.add_argument("--expected-generation", required=True, type=int)
    record_review.add_argument("--action-id", required=True)
    record_review.add_argument("--evidence-hash", required=True)
    record_review.add_argument("--input", required=True)
    record_review_decision = record_commands.add_parser(
        "review-decision", add_help=False
    )
    record_review_decision.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_review_decision.add_argument(
        "--expected-generation", required=True, type=int
    )
    record_review_decision.add_argument("--action-id", required=True)
    record_review_decision.add_argument("--evidence-hash", required=True)
    record_review_decision.add_argument("--input", required=True)
    record_correction = record_commands.add_parser("correction", add_help=False)
    record_correction.add_argument(
        "--work-bundle", "--bundle", dest="work_bundle", required=True
    )
    record_correction.add_argument("--expected-generation", required=True, type=int)
    record_correction.add_argument("--action-id", required=True)
    record_correction.add_argument("--evidence-hash", required=True)
    record_correction.add_argument("--input", required=True)
    resume = subcommands.add_parser("resume", add_help=False)
    resume.add_argument("--work-bundle", "--bundle", dest="work_bundle", required=True)
    resume.add_argument("--expected-generation", required=True, type=int)
    resume.add_argument("--interaction-mode", choices=("confirm", "auto"))
    resume.add_argument("--publish-mode", choices=("skip", "upload"))
    resume.add_argument("--publish-with")
    resume.add_argument("--publish-target")
    resume.add_argument("--use-local-key", action="store_true")
    resume.add_argument(
        "--visual-capability", choices=("available", "unavailable")
    )
    resume.add_argument(
        "--render-dpi", type=int, default=preflight_module.DEFAULT_RENDER_DPI
    )
    settings = subcommands.add_parser("settings", add_help=False)
    settings_commands = settings.add_subparsers(
        dest="settings_command", required=True, parser_class=JsonArgumentParser
    )
    settings_commands.add_parser("init", add_help=False)
    settings_status = settings_commands.add_parser("status", add_help=False)
    settings_status.add_argument("--interaction-mode", choices=("confirm", "auto"))
    settings_status.add_argument("--publish-mode", choices=("skip", "upload"))
    settings_status.add_argument("--publish-with")
    settings_status.add_argument("--publish-target")
    settings_status.add_argument("--use-local-key", action="store_true")
    set_mode = settings_commands.add_parser("set-mode", add_help=False)
    set_mode.add_argument("mode", choices=("confirm", "auto"))
    set_publish_mode = settings_commands.add_parser(
        "set-publish-mode", add_help=False
    )
    set_publish_mode.add_argument("mode", choices=("skip", "upload"))
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
    # Delegate to bundle.canonical_json_bytes -- the same encoder
    # bundle.atomic_write_json/append_history use for every other manifest/
    # private/history write -- instead of a separately maintained copy of
    # the same encoding parameters, so this bundle-creation-time writer
    # cannot silently drift from the rest of the codebase.
    data = bundle_module.canonical_json_bytes(value)
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
    # See _atomic_write_json above: delegate to the shared canonical encoder
    # instead of a separately maintained copy of the same parameters.
    data = bundle_module.canonical_json_bytes(event)
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


def _settings_cli(args) -> dict[str, str | None]:
    return {
        "interaction_mode": getattr(args, "interaction_mode", None),
        "publishing.mode": getattr(args, "publish_mode", None),
        "publishing.uploader": getattr(args, "publish_with", None),
        "publishing.target_ref": getattr(args, "publish_target", None),
    }


def _resolve_settings_status(
    args,
    *,
    environ: dict[str, str],
    cwd: Path,
    config_home: Path,
) -> dict:
    settings_path = config_home / "settings.json"
    try:
        return settings_module.status(
            settings_path,
            cli=_settings_cli(args),
            environ=environ,
            cwd=cwd,
            config_home=config_home,
            home_config_authorized=getattr(args, "use_local_key", False),
        )
    except settings_module.SettingsError:
        raise WorkflowError(
            "configuration_invalid",
            "Persistent settings are invalid.",
            return_code=6,
            action_required="repair_settings",
        ) from None


def _start(
    args,
    *,
    environ: dict[str, str],
    cwd: Path,
    config_home: Path,
    transport,
    now,
) -> dict:
    resolved_status = _resolve_settings_status(
        args, environ=environ, cwd=cwd, config_home=config_home
    )
    settings_snapshot = settings_module.snapshot(resolved_status, cwd=cwd)
    publication_state = bundle_module.publication_state(settings_snapshot)
    output_value = args.output_dir or environ.get("PDF2MARKDOWN_OUTPUT_DIR") or "pdf2markdown-output"
    output_root = _absolute(output_value, cwd)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_root = output_root.resolve(strict=True)
    url_source = pdf_source.is_url_input(args.source)
    source_path = None if url_source else _absolute(args.source, cwd)
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
        if url_source:
            try:
                downloaded = pdf_source.download_https_pdf(
                    args.source,
                    source_dir / "source.pdf",
                    transport=transport,
                )
            except pdf_source.PdfSourceError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=3,
                    action_required="provide_public_https_pdf",
                ) from None
            source_hash = downloaded.sha256
            source_size = downloaded.size_bytes
            source_name = downloaded.original_name
            source_origin = downloaded.origin
        else:
            source_hash, source_size = _copy_source(source_path, source_dir / "source.pdf")
            try:
                pdf_source.validate_pdf_identity(source_dir / "source.pdf")
            except pdf_source.PdfSourceError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=3,
                    action_required="provide_valid_local_pdf",
                ) from None
            source_name = source_path.name
            source_origin = {"kind": "local", "path": str(source_path)}
        started_at = _moment(now)
        timestamp = started_at.strftime("%Y%m%d-%H%M%S")
        base_name = f"{timestamp}-{_slug(source_name)}-{source_hash[:8]}"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generation": 1,
            "conversion_state": "preparing",
            "publication_state": publication_state,
            "source": {
                "original_name": source_name,
                "origin": source_origin,
                "physical_path": "01-source/source.pdf",
                "sha256": source_hash,
                "size_bytes": source_size,
            },
            "settings_snapshot": settings_snapshot,
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
                "settings_snapshot": settings_snapshot,
            },
        )
        _atomic_write_json(staging / "manifest.json", manifest)
        bundle = _commit_staging_bundle(staging, output_root, base_name)
        result = {
            "schema_version": SCHEMA_VERSION,
            "work_bundle": str(bundle),
            "generation": 1,
            "conversion_state": "preparing",
            "publication_state": publication_state,
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
        return bundle_module.decode_json_object(
            _read_private_file(name, dir_fd=dir_fd)
        )
    except bundle_module.BundleStateError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state could not be read.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
        ) from None


def _assert_conversion_capacity(
    *, operation: str, descriptors: dict, manifest: dict, private_state: dict,
    at: str, context, **response_inputs
) -> None:
    """Run a conversion operation's local-state capacity admission.

    Call sites must place this before their first history append and before
    their external call. `local_state_capacity_exhausted` is reported with
    `preserve_work_bundle_and_stop` (design.md:448: the caller must keep the
    bundle, not truncate append-only history or rebuild a task that may already
    have been charged); any other failure keeps the repair action the writing
    call would have reported.
    """
    try:
        conversion_attempt_module.assert_local_state_capacity(
            operation=operation,
            manifest=manifest,
            private_state=private_state,
            history_bytes=len(
                # history.ndjson is bounded by bundle's 64 MiB *history*
                # ceiling, not by this module's 8 MiB state ceiling. Reading it
                # at the default would reject a legal bundle with
                # repair_or_restore_work_bundle, which design.md:448 forbids
                # here, and would cap the term the history verdict is computed
                # from at 8 MiB. scripts/review.py:2808 reads it the same way.
                _read_private_file(
                    "history.ndjson",
                    dir_fd=descriptors["state"],
                    max_bytes=bundle_module.MAX_STATE_BYTES,
                )
            ),
            at=at,
            **response_inputs,
        )
    except conversion_attempt_module.ConversionAttemptError as exc:
        raise WorkflowError(
            exc.code,
            exc.message,
            return_code=4,
            action_required=(
                "preserve_work_bundle_and_stop"
                if exc.code == "local_state_capacity_exhausted"
                else "repair_or_restore_work_bundle"
            ),
            context=context,
        ) from None


def _conversion_history_resolver(manifest: dict):
    """Pick the reducer that understands every event this bundle can hold.

    Once a bundle carries a raw conversion record its history mixes raw
    conversion events with conversion attempt operations, and only the raw
    conversion reducer can replay both. `conversion_attempt` cannot make this
    choice itself: it is the lower layer and does not know the raw events.
    Mirrors the layer ladder already used for `prefix_state_resolver`: each
    rung delegates verbatim to the one below when its own events are absent,
    so following the manifest's layering can only widen what replays.
    """
    if "review" in manifest:
        module = review_module
    elif "raw_conversion" in manifest:
        module = raw_conversion_module
    else:
        module = conversion_attempt_module
    return module.resolve_history_state


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
                chunk = source.read(
                    min(1024 * 1024, opened_info.st_size + 1 - size)
                )
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if size > opened_info.st_size:
                    raise WorkflowError(
                        "integrity_violation",
                        "The saved source PDF changed while it was being inspected.",
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
        or set(manifest)
        not in {
            frozenset(required),
            frozenset({*required, "preflight"}),
            frozenset({*required, "preflight", "source_staging"}),
            frozenset(
                {
                    *required,
                    "preflight",
                    "source_staging",
                    "raw_conversion",
                    "raw_conversions",
                }
            ),
            frozenset(
                {
                    *required,
                    "preflight",
                    "source_staging",
                    "raw_conversion",
                    "raw_conversions",
                    "review",
                }
            ),
            frozenset(
                {
                    *required,
                    "preflight",
                    "source_staging",
                    "raw_conversion",
                    "raw_conversions",
                    "review",
                    "corrections",
                }
            ),
        }
        or type(manifest.get("generation")) is not int
        or manifest["generation"] < 1
        or manifest.get("conversion_state") not in SUPPORTED_CONVERSION_STATES
        or (
            manifest.get("conversion_state") in {"review_pending", "local_complete"}
            and "review" not in manifest
        )
        or manifest.get("publication_state") not in SUPPORTED_PUBLICATION_STATES
        or not isinstance(source, dict)
        or set(source)
        != {"original_name", "origin", "physical_path", "sha256", "size_bytes"}
        or not isinstance(source.get("original_name"), str)
        or not source["original_name"]
        or not pdf_source.valid_origin(origin)
        or source.get("physical_path") != "01-source/source.pdf"
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        or type(source.get("size_bytes")) is not int
        or source["size_bytes"] < 0
        or not isinstance(manifest.get("conversion_attempts"), list)
        or (
            "review" not in manifest
            and manifest.get("final_markdown") is not None
        )
        or (
            "review" in manifest
            and not review_module.valid_manifest(manifest)
        )
        or not isinstance(manifest.get("artifacts"), dict)
        or manifest["artifacts"].get("source_pdf") != "01-source/source.pdf"
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle state uses an unknown or incomplete schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context={"work_bundle": str(bundle)},
        )
    try:
        settings_module.validate_snapshot(manifest.get("settings_snapshot"))
    except settings_module.SettingsError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle settings snapshot uses an unknown or incomplete schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context={"work_bundle": str(bundle)},
        ) from None
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
    except WorkflowError as exc:
        exc.context = state_context
        raise
    try:
        history = bundle_module.read_history(state_fd=descriptors["state"])
    except bundle_module.BundleStateError:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle history could not be read safely.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        ) from None
    has_source_staging = "source_staging" in manifest
    has_conversion_attempt = bool(manifest.get("conversion_attempts"))
    has_raw_conversion = "raw_conversion" in manifest
    has_review = "review" in manifest
    if (
        type(private_state.get("schema_version")) is not int
        or private_state["schema_version"] != SCHEMA_VERSION
        or set(private_state)
        != {"schema_version", "generation", "source_uploads", "result_urls"}
        or type(private_state.get("generation")) is not int
        or private_state["generation"] != manifest["generation"]
        or (
            not has_conversion_attempt
            and private_state.get("result_urls") != []
        )
        or (
            not has_source_staging
            and private_state.get("source_uploads") != []
        )
        or (
            has_raw_conversion
            and not raw_conversion_module.valid_private_state(
                private_state, manifest
            )
        )
        or (
            has_conversion_attempt
            and not has_raw_conversion
            and not conversion_attempt_module.valid_private_state(
                private_state, manifest
            )
        )
        or (
            has_source_staging
            and not has_conversion_attempt
            and not source_staging_module.valid_private_state(
                private_state, manifest
            )
        )
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Private work bundle state uses an unknown or inconsistent schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        )
    valid_history = (
        review_module.valid_history(history, manifest, private_state)
        if has_review
        else raw_conversion_module.valid_history(history, manifest, private_state)
        if has_raw_conversion
        else conversion_attempt_module.valid_history(history, manifest, private_state)
        if has_conversion_attempt
        else source_staging_module.valid_history(history, manifest, private_state)
        if has_source_staging
        else (
            bundle_module.valid_settings_history(
                history, manifest, source.get("sha256")
            )
            or preflight_module.valid_preflight_history(
                history, manifest, private_state
            )
            or preflight_module.valid_pending_preflight_history(
                history, manifest, private_state
            )
        )
    )
    if not valid_history:
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle history uses an unknown or inconsistent schema.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=state_context,
        )
    try:
        digest, size = _source_digest(
            "source.pdf", dir_fd=descriptors["source"]
        )
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
    if manifest["conversion_state"] not in {"preparing", "recoverable_error"}:
        try:
            preflight_module.validate_baseline_artifacts(
                descriptors=descriptors, manifest=manifest
            )
        except preflight_module.PreflightError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context=state_context,
            ) from None
    if has_raw_conversion:
        try:
            raw_conversion_module.validate_committed_artifacts(
                descriptors=descriptors, manifest=manifest
            )
        except raw_conversion_module.RawConversionError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context=state_context,
            ) from None
    if has_review:
        try:
            review_module.validate_committed_artifacts(
                descriptors=descriptors,
                manifest=manifest,
                bundle_root=bundle,
            )
        except review_module.ReviewError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context=state_context,
            ) from None
    _assert_bundle_descriptors_current(bundle, descriptors)
    print(f"[pdf2markdown] inspected work bundle {bundle.name}", file=sys.stderr)
    if has_review:
        return review_module.result_from_manifest(
            manifest, work_bundle=str(bundle), outcome="inspected"
        )
    if has_raw_conversion:
        return raw_conversion_module.result_from_manifest(
            manifest, work_bundle=str(bundle), outcome="inspected"
        )
    if has_conversion_attempt:
        return conversion_attempt_module.result_from_manifest(
            manifest, work_bundle=str(bundle), outcome="inspected"
        )
    if has_source_staging:
        return source_staging_module.result_from_manifest(
            manifest, work_bundle=str(bundle), outcome="inspected"
        )
    if manifest["conversion_state"] != "preparing":
        return preflight_module.result_from_manifest(
            manifest, work_bundle=str(bundle), outcome="inspected"
        )
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


def _commit_baseline_block(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    dependencies: list[dict],
    render_dpi: int,
    blocker: dict,
    at: str,
) -> dict:
    history = bundle_module.read_history(state_fd=descriptors["state"])
    if history and history[-1].get("event") == "preflight_baseline_intent":
        preflight_module.abort_pending_baseline(
            descriptors=descriptors,
            manifest=manifest,
            private_state=private_state,
            blocker=blocker,
            at=at,
        )
    return preflight_module.commit_deterministic_block(
        descriptors=descriptors,
        manifest=manifest,
        private_state=private_state,
        dependencies=dependencies,
        render_dpi=render_dpi,
        blocker=blocker,
        at=at,
    )


def _assert_frozen_source_before_recovery(
    *, descriptors: dict, manifest: dict, work_bundle: str
) -> None:
    source = manifest.get("source")
    context = {
        "work_bundle": work_bundle,
        "generation": manifest.get("generation"),
        "conversion_state": manifest.get("conversion_state"),
        "publication_state": manifest.get("publication_state"),
    }
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        or type(source.get("size_bytes")) is not int
        or source["size_bytes"] < 0
    ):
        raise WorkflowError(
            "invalid_bundle",
            "Work bundle source identity is invalid.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=context,
        )
    try:
        digest, size = _source_digest(
            "source.pdf", dir_fd=descriptors["source"]
        )
    except WorkflowError as exc:
        exc.context = context
        raise
    if digest != source["sha256"] or size != source["size_bytes"]:
        raise WorkflowError(
            "integrity_violation",
            "The saved source PDF does not match the manifest.",
            return_code=4,
            action_required="repair_or_restore_work_bundle",
            context=context,
        )
    preflight_state = manifest.get("preflight")
    if isinstance(preflight_state, dict) and (
        isinstance(preflight_state.get("inventory_sha256"), str)
        or preflight_state.get("status") == "deterministic_blocked"
    ):
        try:
            preflight_module.validate_baseline_artifacts(
                descriptors=descriptors, manifest=manifest
            )
        except preflight_module.PreflightError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context=context,
            ) from None


def _advance(
    args,
    *,
    cwd: Path,
    environ: dict[str, str],
    config_home: Path,
    transport,
    now,
) -> dict:
    if not preflight_module.MIN_RENDER_DPI <= args.render_dpi <= (
        preflight_module.MAX_RENDER_DPI
    ):
        raise WorkflowError(
            "invalid_render_dpi",
            "Render DPI must be between 72 and 600.",
            return_code=2,
            action_required="correct_command_arguments",
        )
    bundle = _canonical_bundle_path(args.work_bundle, cwd)
    with _exclusive_bundle_lock(bundle) as locked_descriptors:
        with _open_bundle_descriptors(
            bundle, locked_descriptors=locked_descriptors
        ) as descriptors:
            operation_at = _isoformat(_moment(now))
            _assert_bundle_descriptors_current(bundle, descriptors)
            recovery_manifest = _read_json(
                "manifest.json", dir_fd=descriptors["root"]
            )
            _assert_frozen_source_before_recovery(
                descriptors=descriptors,
                manifest=recovery_manifest,
                work_bundle=str(bundle),
            )
            try:
                recovered_raw = raw_conversion_module.recover_interrupted_adoption(
                    descriptors=descriptors,
                    manifest=recovery_manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    at=operation_at,
                    expected_generation=args.expected_generation,
                    transport=transport,
                )
            except raw_conversion_module.RawConversionError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=(5 if exc.code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if exc.code == "generation_conflict"
                        else "resume_same_conversion_result"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_raw is not None:
                recovered_manifest, _recovered_private = recovered_raw
                outcome = (
                    "raw_conversion_adopted"
                    if recovered_manifest["conversion_state"] == "converted"
                    else recovered_manifest["raw_conversion"]["reason_code"]
                )
                result = raw_conversion_module.result_from_manifest(
                    recovered_manifest, work_bundle=str(bundle), outcome=outcome
                )
                print(
                    f"[pdf2markdown] recovered {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_review = review_module.recover_pending_operation(
                    descriptors=descriptors,
                    manifest=recovery_manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    bundle_root=bundle,
                    expected_generation=args.expected_generation,
                    at=operation_at,
                )
            except (bundle_module.BundleStateError, review_module.ReviewError) as exc:
                code = getattr(exc, "code", "integrity_violation")
                raise WorkflowError(
                    code,
                    "A pending review operation cannot be recovered safely.",
                    return_code=(5 if code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if code == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_review is not None:
                recovered_manifest = recovered_review["manifest"]
                result = review_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=recovered_manifest["review"]["status"],
                )
                if isinstance(recovered_review.get("intent"), dict):
                    raise WorkflowError(
                        "recovered_request_mismatch",
                        "A pending review record was recovered; the advance request was not applied.",
                        return_code=5,
                        action_required="inspect_current_generation",
                        context=result,
                    )
                print(
                    f"[pdf2markdown] recovered {result['outcome']} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_conversion = (
                    conversion_attempt_module.recover_interrupted_attempt(
                        descriptors=descriptors,
                        manifest=recovery_manifest,
                        private_state=_read_json(
                            "private.json", dir_fd=descriptors["state"]
                        ),
                        at=operation_at,
                        expected_generation=args.expected_generation,
                        resolve_history=_conversion_history_resolver(
                            recovery_manifest
                        ),
                    )
                )
            except conversion_attempt_module.ConversionAttemptError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=(5 if exc.code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if exc.code == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_conversion is not None:
                recovered_manifest, _recovered_private = recovered_conversion
                recovered_outcome = (
                    "conversion_submitted"
                    if recovered_manifest["conversion_state"] == "submitted"
                    else recovered_manifest["conversion_state"]
                )
                result = conversion_attempt_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=recovered_outcome,
                )
                print(
                    f"[pdf2markdown] recovered {result['outcome']} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_staging = source_staging_module.recover_interrupted_attempt(
                    descriptors=descriptors,
                    manifest=recovery_manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    at=operation_at,
                    expected_generation=args.expected_generation,
                )
            except (
                bundle_module.BundleStateError,
                source_staging_module.SourceStagingError,
            ) as exc:
                raise WorkflowError(
                    getattr(exc, "code", "integrity_violation"),
                    "A pending source staging operation cannot be recovered safely.",
                    return_code=(5 if getattr(exc, "code", None) == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if getattr(exc, "code", None) == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_staging is not None:
                recovered_manifest, _recovered_private = recovered_staging
                _assert_bundle_descriptors_current(bundle, descriptors)
                inspected = _inspect_open_bundle(bundle, descriptors)
                recovered_state = recovered_manifest["source_staging"]["state"]
                result = source_staging_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=recovered_state,
                )
                print(
                    f"[pdf2markdown] {recovered_state} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                history = bundle_module.read_history(state_fd=descriptors["state"])
                pending_baseline = (
                    history[-1]
                    if history
                    and history[-1].get("event") == "preflight_baseline_intent"
                    else None
                )
                dependency_status = None
                if pending_baseline is not None:
                    dependency_status = preflight_module.check_dependencies(
                        environ=environ, visual_capability=args.visual_capability
                    )
                    if dependency_status["missing"]:
                        manifest = _read_json(
                            "manifest.json", dir_fd=descriptors["root"]
                        )
                        private_state = _read_json(
                            "private.json", dir_fd=descriptors["state"]
                        )
                        preflight_module.abort_pending_baseline(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            blocker={
                                "code": "dependency_missing",
                                "pages": [],
                                "missing": dependency_status["missing"],
                                "dependencies": dependency_status["dependencies"],
                                "evidence": (
                                    "Required preflight capabilities became unavailable "
                                    "before baseline recovery."
                                ),
                            },
                            at=operation_at,
                            reason_code="dependency_missing",
                        )
                        updated_manifest = preflight_module.commit_dependency_missing(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            dependencies=dependency_status["dependencies"],
                            missing=dependency_status["missing"],
                            render_dpi=pending_baseline["render_dpi"],
                            at=operation_at,
                        )
                        _assert_bundle_descriptors_current(bundle, descriptors)
                        result = preflight_module.dependency_result(
                            updated_manifest, work_bundle=str(bundle)
                        )
                        print(
                            f"[pdf2markdown] dependency_missing for work bundle {bundle.name}",
                            file=sys.stderr,
                        )
                        return result
                    if dependency_status["dependencies"] != pending_baseline.get(
                        "dependencies"
                    ):
                        raise WorkflowError(
                            "dependency_drift",
                            "Preflight dependencies changed during baseline recovery.",
                            return_code=5,
                            action_required="restore_preflight_dependencies",
                            context={"work_bundle": str(bundle)},
                        )
                recovered_operation = preflight_module.recover_pending_operation(
                    descriptors=descriptors, at=operation_at
                )
            except (bundle_module.BundleStateError, preflight_module.PreflightError) as exc:
                blocker = (
                    preflight_module.blocker_for_error(exc)
                    if isinstance(exc, preflight_module.PreflightError)
                    else None
                )
                if blocker is not None:
                    try:
                        history = bundle_module.read_history(
                            state_fd=descriptors["state"]
                        )
                        intent = history[-1] if history else None
                        if not isinstance(intent, dict) or intent.get("event") != (
                            "preflight_baseline_intent"
                        ):
                            raise preflight_module.PreflightError(
                                "integrity_violation",
                                "A deterministic recovery failure has no pending baseline intent.",
                            )
                        manifest = _read_json(
                            "manifest.json", dir_fd=descriptors["root"]
                        )
                        private_state = _read_json(
                            "private.json", dir_fd=descriptors["state"]
                        )
                        updated_manifest = _commit_baseline_block(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            dependencies=intent["dependencies"],
                            render_dpi=intent["render_dpi"],
                            blocker=blocker,
                            at=operation_at,
                        )
                    except (
                        bundle_module.BundleStateError,
                        preflight_module.PreflightError,
                    ):
                        pass
                    else:
                        _assert_bundle_descriptors_current(bundle, descriptors)
                        result = preflight_module.result_from_manifest(
                            updated_manifest,
                            work_bundle=str(bundle),
                            outcome="preflight_blocked",
                        )
                        print(
                            f"[pdf2markdown] preflight_blocked for work bundle {bundle.name}",
                            file=sys.stderr,
                        )
                        return result
                code = getattr(exc, "code", "integrity_violation")
                raise WorkflowError(
                    code,
                    "A pending preflight operation cannot be recovered safely.",
                    return_code=4,
                    action_required="repair_or_restore_work_bundle",
                    context={"work_bundle": str(bundle)},
                ) from None
            inspected = _inspect_open_bundle(bundle, descriptors)
            if (
                recovered_operation is not None
                and recovered_operation["event"] == "preflight_baseline_intent"
                and args.render_dpi != recovered_operation["intent"]["render_dpi"]
            ):
                raise WorkflowError(
                    "recovered_request_mismatch",
                    "A different baseline request was recovered; this request was not applied.",
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                )
            replayed_recovery = (
                recovered_operation is not None
                and args.expected_generation
                == recovered_operation["expected_generation"]
            )
            if inspected["generation"] != args.expected_generation and not replayed_recovery:
                raise WorkflowError(
                    "generation_conflict",
                    "Expected generation does not match the current work bundle state.",
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                )
            manifest = _read_json("manifest.json", dir_fd=descriptors["root"])
            review_can_open = manifest["conversion_state"] == "converted" or (
                manifest["conversion_state"] == "awaiting_user"
                and manifest.get("review", {}).get("status") == "review_incomplete"
            )
            if review_can_open:
                if args.visual_capability is None:
                    result = (
                        review_module.result_from_manifest(
                            manifest,
                            work_bundle=str(bundle),
                            outcome=manifest["review"]["status"],
                        )
                        if "review" in manifest
                        else raw_conversion_module.result_from_manifest(
                            manifest,
                            work_bundle=str(bundle),
                            outcome="converted",
                        )
                    )
                    print(
                        f"[pdf2markdown] {result['outcome']} work bundle {bundle.name}",
                        file=sys.stderr,
                    )
                    return result
                try:
                    manifest = review_module.open_review(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=_read_json(
                            "private.json", dir_fd=descriptors["state"]
                        ),
                        bundle_root=bundle,
                        environ=environ,
                        visual_capability=args.visual_capability,
                        expected_generation=args.expected_generation,
                        at=operation_at,
                    )
                except (
                    raw_conversion_module.RawConversionError,
                    review_module.ReviewError,
                ) as exc:
                    raise WorkflowError(
                        exc.code,
                        exc.message,
                        return_code=(
                            5 if exc.code == "generation_conflict" else 4
                        ),
                        action_required=(
                            "inspect_current_generation"
                            if exc.code == "generation_conflict"
                            else "restore_review_dependencies"
                            if exc.code in {"dependency_missing", "dependency_changed"}
                            else "repair_or_restore_work_bundle"
                        ),
                        context=inspected,
                    ) from None
                result = review_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome="review_pending",
                )
                print(
                    f"[pdf2markdown] review_pending for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] in {"review_pending", "local_complete"}:
                result = review_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome=manifest["conversion_state"],
                )
                print(
                    f"[pdf2markdown] {manifest['conversion_state']} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "terminal_error" and isinstance(
                manifest.get("raw_conversion"), dict
            ) and (
                manifest.get("conversion_attempts")
                and manifest["conversion_attempts"][-1].get("state") == "result_ready"
            ):
                outcome = manifest["raw_conversion"]["reason_code"]
                result = raw_conversion_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "submission_unknown":
                result = conversion_attempt_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome="submission_unknown",
                )
                print(
                    f"[pdf2markdown] submission_unknown for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "result_downloading" and manifest.get(
                "conversion_attempts"
            ):
                try:
                    updated_manifest, _updated_private = (
                        raw_conversion_module.adopt_ready_result(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=_read_json(
                                "private.json", dir_fd=descriptors["state"]
                            ),
                            at=operation_at,
                            transport=transport,
                        )
                    )
                except raw_conversion_module.RawConversionError as exc:
                    raise WorkflowError(
                        exc.code,
                        exc.message,
                        return_code=4,
                        action_required="resume_same_conversion_result",
                        context=inspected,
                    ) from None
                outcome = (
                    "raw_conversion_adopted"
                    if updated_manifest["conversion_state"] == "converted"
                    else updated_manifest["raw_conversion"]["reason_code"]
                )
                result = raw_conversion_module.result_from_manifest(
                    updated_manifest, work_bundle=str(bundle), outcome=outcome
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "terminal_error" and manifest.get(
                "conversion_attempts"
            ):
                active_state = manifest["conversion_attempts"][-1]["state"]
                if active_state != "unsafe_result_url":
                    result = conversion_attempt_module.result_from_manifest(
                        manifest,
                        work_bundle=str(bundle),
                        outcome=active_state,
                    )
                    print(
                        f"[pdf2markdown] {active_state} for work bundle {bundle.name}",
                        file=sys.stderr,
                    )
                    return result
            if manifest["conversion_state"] == "awaiting_user" and manifest.get(
                "conversion_attempts"
            ):
                active_state = manifest["conversion_attempts"][-1]["state"]
                outcome = "task_failed" if active_state == "failed" else active_state
                result = conversion_attempt_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest.get("conversion_attempts") and (
                manifest["conversion_state"] == "submitted"
                or (
                    manifest["conversion_state"] == "terminal_error"
                    and manifest["conversion_attempts"][-1]["state"]
                    == "unsafe_result_url"
                )
                or (
                    manifest["conversion_state"] == "recoverable_error"
                    and manifest["conversion_attempts"][-1]["state"]
                    in {
                        "credential_source_missing",
                        "credential_source_changed",
                        "poll_unauthorized",
                        "task_unavailable",
                        "poll_transient",
                        "poll_timeout",
                        "result_pending_timeout",
                    }
                )
                or (
                    manifest["conversion_state"] == "recoverable_error"
                    and manifest["conversion_attempts"][-1]["state"]
                    == "result_ready"
                    and manifest.get("raw_conversion", {}).get("reason_code")
                    == "result_url_unavailable"
                )
            ):
                active_attempt = manifest["conversion_attempts"][-1]
                poll_at = _isoformat(_moment(now))
                try:
                    poll_result = conversion_attempt_module.timeout_before_poll(
                        active_attempt, at=poll_at
                    )
                    waiting_for_backoff = (
                        poll_result is None
                        and conversion_attempt_module.waiting_for_poll_backoff(
                            active_attempt, at=poll_at
                        )
                    )
                except conversion_attempt_module.ConversionAttemptError as exc:
                    raise WorkflowError(
                        exc.code,
                        exc.message,
                        return_code=4,
                        action_required="repair_or_restore_work_bundle",
                        context=inspected,
                    ) from None
                if waiting_for_backoff:
                    result = conversion_attempt_module.result_from_manifest(
                        manifest,
                        work_bundle=str(bundle),
                        outcome="poll_backoff",
                    )
                    print(
                        f"[pdf2markdown] poll_backoff for work bundle {bundle.name}",
                        file=sys.stderr,
                    )
                    return result
                # design.md:305 -- the admission for the two operations that
                # reach this block: an ordinary poll, and the refresh a locally
                # expired result reference triggers on the same task. Both
                # commit through commit_poll_result, whose
                # conversion_poll_result_intent is this operation's first
                # history event, and both may issue the poll GET just below, so
                # a refusal here predates every byte and every ledger entry.
                _assert_conversion_capacity(
                    operation=(
                        conversion_attempt_module.RESULT_REFRESH_OPERATION
                        if manifest["conversion_state"] == "recoverable_error"
                        and active_attempt["state"] == "result_ready"
                        else conversion_attempt_module.ORDINARY_POLL_OPERATION
                    ),
                    descriptors=descriptors,
                    manifest=manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    at=poll_at,
                    context=inspected,
                )
                if poll_result is None:
                    try:
                        credential = config_module.read_exact_api_key(
                            active_attempt["credential"],
                            environ=environ,
                            config_home=config_home,
                            use_local_key=getattr(args, "use_local_key", False),
                        )
                    except config_module.ConfigError as exc:
                        if exc.code not in {
                            "credential_source_missing",
                            "credential_source_changed",
                        }:
                            raise WorkflowError(
                                "configuration_invalid",
                                "The recorded Doc2X credential locator is invalid.",
                                return_code=6,
                                action_required="repair_or_restore_work_bundle",
                                context=inspected,
                            ) from None
                        poll_result = doc2x_module.PollResult(
                            exc.code, None, exc.code, None, None
                        )
                    else:
                        poll_result = doc2x_module.poll_task(
                            task_id=active_attempt["task_id"],
                            api_key=credential.value,
                            transport=transport,
                        )
                try:
                    updated_manifest, _updated_private = (
                        conversion_attempt_module.commit_poll_result(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=_read_json(
                                "private.json", dir_fd=descriptors["state"]
                            ),
                            result=poll_result,
                            at=poll_at,
                        )
                    )
                except conversion_attempt_module.ConversionAttemptError as exc:
                    raise WorkflowError(
                        exc.code,
                        exc.message,
                        return_code=4,
                        action_required="repair_or_restore_work_bundle",
                        context=inspected,
                    ) from None
                outcome = (
                    "result_ready"
                    if poll_result.state == "result_ready"
                    else "result_pending"
                    if poll_result.state == "result_pending"
                    else poll_result.state
                    if poll_result.state
                    in {"unsafe_result_url", "unexpected_result_count"}
                    else "task_failed"
                    if poll_result.state == "failed"
                    else poll_result.state
                    if poll_result.state
                    in {"credential_source_missing", "credential_source_changed"}
                    else poll_result.state
                    if poll_result.state in {"poll_unauthorized", "task_unavailable"}
                    else "poll_transient"
                    if poll_result.state == "poll_transient"
                    else "poll_timeout"
                    if poll_result.state == "poll_timeout"
                    else "result_pending_timeout"
                    if poll_result.state == "result_pending_timeout"
                    else f"conversion_{poll_result.state}"
                )
                result = conversion_attempt_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            source_staging = manifest.get("source_staging")
            if (
                isinstance(source_staging, dict)
                and source_staging.get("state") == "source_upload_started"
            ):
                private_state = _read_json(
                    "private.json", dir_fd=descriptors["state"]
                )
                try:
                    updated_manifest, _updated_private = (
                        source_staging_module.finish_attempt(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            result=aihub_upload.UploadResult(
                                "source_upload_unknown",
                                None,
                                "interrupted_before_result_commit",
                                None,
                                None,
                            ),
                            at=operation_at,
                            expires_at=None,
                        )
                    )
                except source_staging_module.SourceStagingError as exc:
                    raise WorkflowError(
                        exc.code,
                        exc.message,
                        return_code=4,
                        action_required="repair_or_restore_work_bundle",
                        context=inspected,
                    ) from None
                result = source_staging_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome="source_upload_unknown",
                )
                print(
                    f"[pdf2markdown] source_upload_unknown for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if (
                isinstance(source_staging, dict)
                and source_staging.get("state")
                in {
                    "source_upload_rejected",
                    "source_upload_unknown",
                    "source_upload_expired",
                }
            ):
                outcome = source_staging["state"]
                if (
                    source_staging.get("state") == "source_upload_unknown"
                    and source_staging.get("wait_until") is not None
                ):
                    private_state = _read_json(
                        "private.json", dir_fd=descriptors["state"]
                    )
                    wait_moment = _moment(now)
                    try:
                        if source_staging_module.unknown_wait_has_elapsed(
                            manifest=manifest,
                            now=wait_moment,
                        ):
                            manifest = source_staging_module.renew_unknown_action(
                                descriptors=descriptors,
                                manifest=manifest,
                                private_state=private_state,
                                at=_isoformat(wait_moment),
                            )
                            outcome = "source_upload_unknown"
                        else:
                            outcome = "source_upload_waiting"
                    except source_staging_module.SourceStagingError as exc:
                        raise WorkflowError(
                            exc.code,
                            exc.message,
                            return_code=4,
                            action_required="repair_or_restore_work_bundle",
                            context=inspected,
                        ) from None
                result = source_staging_module.result_from_manifest(
                    manifest, work_bundle=str(bundle), outcome=outcome
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "preflight_pending":
                result = preflight_module.result_from_manifest(
                    manifest,
                    work_bundle=str(bundle),
                    outcome="awaiting_preflight",
                )
                print(
                    f"[pdf2markdown] awaiting_preflight for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            stable_outcomes = {
                "preflight_warning": "preflight_warning",
                "preflight_blocked": "preflight_blocked",
                "terminal_error": "preflight_warning_declined",
            }
            if manifest["conversion_state"] in stable_outcomes:
                outcome = stable_outcomes[manifest["conversion_state"]]
                result = preflight_module.result_from_manifest(
                    manifest, work_bundle=str(bundle), outcome=outcome
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if manifest["conversion_state"] == "ready_to_submit":
                source_staging = manifest.get("source_staging")
                if (
                    isinstance(source_staging, dict)
                    and source_staging.get("state") == "source_upload_ready"
                ):
                    ready_private = _read_json(
                        "private.json", dir_fd=descriptors["state"]
                    )
                    try:
                        expired = source_staging_module.ready_is_expired(
                            manifest=manifest,
                            private_state=ready_private,
                            now=_moment(now),
                        )
                        if expired:
                            manifest, ready_private = (
                                source_staging_module.expire_ready_attempt(
                                    descriptors=descriptors,
                                    manifest=manifest,
                                    private_state=ready_private,
                                    at=_isoformat(_moment(now)),
                                )
                            )
                    except source_staging_module.SourceStagingError as exc:
                        raise WorkflowError(
                            exc.code,
                            exc.message,
                            return_code=4,
                            action_required="repair_or_restore_work_bundle",
                            context=inspected,
                        ) from None
                    if expired:
                        if (
                            manifest["settings_snapshot"]["interaction_mode"]
                            == "auto"
                        ):
                            pending = manifest["source_staging"]["pending_action"]
                            manifest = source_staging_module.commit_decision(
                                descriptors=descriptors,
                                manifest=manifest,
                                private_state=ready_private,
                                expected_generation=manifest["generation"],
                                action_id=pending["action_id"],
                                evidence_hash=pending["evidence_hash"],
                                decision="retry",
                                basis="interaction_mode_auto",
                                at=_isoformat(_moment(now)),
                            )
                            source_staging = manifest["source_staging"]
                        else:
                            result = source_staging_module.result_from_manifest(
                                manifest,
                                work_bundle=str(bundle),
                                outcome="source_upload_expired",
                            )
                            print(
                                f"[pdf2markdown] source_upload_expired for work bundle {bundle.name}",
                                file=sys.stderr,
                            )
                            return result
                    else:
                        active_upload = ready_private["source_uploads"][-1]
                        try:
                            credential = config_module.read_exact_api_key(
                                active_upload["credential"],
                                environ=environ,
                                config_home=config_home,
                                use_local_key=getattr(args, "use_local_key", False),
                            )
                        except config_module.ConfigError:
                            result = source_staging_module.result_from_manifest(
                                manifest,
                                work_bundle=str(bundle),
                                outcome="source_upload_ready",
                            )
                            print(
                                f"[pdf2markdown] {result['outcome']} for work bundle {bundle.name}",
                                file=sys.stderr,
                            )
                            return result
                        try:
                            preflight_record = bundle_module.read_json(
                                "preflight.json", dir_fd=descriptors["review"]
                            )
                        except bundle_module.BundleStateError:
                            raise WorkflowError(
                                "integrity_violation",
                                "The saved preflight evidence cannot be read.",
                                return_code=4,
                                action_required="repair_or_restore_work_bundle",
                                context=inspected,
                            ) from None
                        request, request_summary = conversion_attempt_module.build_request(
                            manifest=manifest,
                            source_url=active_upload["url"],
                            preflight_record=preflight_record,
                        )
                        submitted_at = _isoformat(_moment(now))
                        # design.md:305 -- the create path's local-state
                        # capacity admission. It must run here: the very next
                        # statement's begin_attempt appends
                        # conversion_submit_intent, and doc2x create_task
                        # follows it, so this is the last point at which a
                        # refusal leaves every byte and the create ledger
                        # untouched.
                        _assert_conversion_capacity(
                            operation=conversion_attempt_module.CREATE_OPERATION,
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=ready_private,
                            at=submitted_at,
                            context=inspected,
                            credential=credential.public_identity,
                            request=request,
                            request_summary=request_summary,
                        )
                        try:
                            submitting_manifest, submitting_private, _attempt = (
                                conversion_attempt_module.begin_attempt(
                                    descriptors=descriptors,
                                    manifest=manifest,
                                    private_state=ready_private,
                                    credential=credential.public_identity,
                                    request=request,
                                    request_summary=request_summary,
                                    at=submitted_at,
                                )
                            )
                            create_result = doc2x_module.create_task(
                                request=request,
                                api_key=credential.value,
                                transport=transport,
                            )
                            updated_manifest, _updated_private = (
                                conversion_attempt_module.finish_submission(
                                    descriptors=descriptors,
                                    manifest=submitting_manifest,
                                    private_state=submitting_private,
                                    result=create_result,
                                    at=_isoformat(_moment(now)),
                                )
                            )
                        except conversion_attempt_module.ConversionAttemptError as exc:
                            raise WorkflowError(
                                exc.code,
                                exc.message,
                                return_code=4,
                                action_required="repair_or_restore_work_bundle",
                                context=inspected,
                            ) from None
                        outcome = (
                            "conversion_submitted"
                            if create_result.state == "submitted"
                            else "submission_unknown"
                        )
                        result = conversion_attempt_module.result_from_manifest(
                            updated_manifest,
                            work_bundle=str(bundle),
                            outcome=outcome,
                        )
                        print(
                            f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                            file=sys.stderr,
                        )
                        return result
                try:
                    credential = config_module.resolve_api_key(
                        environ=environ,
                        cwd=cwd,
                        config_home=config_home,
                        use_local_key=getattr(args, "use_local_key", False),
                    )
                except config_module.ConfigError:
                    raise WorkflowError(
                        "configuration_invalid",
                        "AIHUB_API_KEY is missing or its selected configuration source is invalid.",
                        return_code=6,
                        action_required="configure_aihub_api_key",
                        context=inspected,
                    ) from None
                private_state = _read_json(
                    "private.json", dir_fd=descriptors["state"]
                )
                request_moment = _moment(now)
                request_at = _isoformat(request_moment)
                try:
                    started_manifest, started_private, _attempt = (
                        source_staging_module.begin_attempt(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            credential=credential,
                            at=request_at,
                        )
                    )
                    source_descriptor = source_staging_module.open_frozen_source(
                        source_fd=descriptors["source"], manifest=started_manifest
                    )
                    try:
                        upload_result = aihub_upload.upload_open_source(
                            source_fd=source_descriptor,
                            source_sha256=started_manifest["source"]["sha256"],
                            source_size=started_manifest["source"]["size_bytes"],
                            api_key=credential.value,
                            transport=transport,
                        )
                    finally:
                        os.close(source_descriptor)
                    completed_moment = _moment(now)
                    expires_at = (
                        _isoformat(completed_moment + timedelta(hours=72))
                        if upload_result.state == "source_upload_ready"
                        else None
                    )
                    updated_manifest, _updated_private = (
                        source_staging_module.finish_attempt(
                            descriptors=descriptors,
                            manifest=started_manifest,
                            private_state=started_private,
                            result=upload_result,
                            at=_isoformat(completed_moment),
                            expires_at=expires_at,
                        )
                    )
                except (source_staging_module.SourceStagingError, aihub_upload.UploadError) as exc:
                    raise WorkflowError(
                        getattr(exc, "code", "integrity_violation"),
                        "The frozen source could not be staged safely.",
                        return_code=4,
                        action_required="repair_or_restore_work_bundle",
                        context=inspected,
                    ) from None
                _assert_bundle_descriptors_current(bundle, descriptors)
                outcome = upload_result.state
                result = source_staging_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                print(
                    f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            recoverable_dependency = (
                manifest["conversion_state"] == "recoverable_error"
                and manifest.get("preflight", {}).get("reason_code")
                == "dependency_missing"
                and manifest.get("preflight", {}).get("resume_state") == "preparing"
            )
            if manifest["conversion_state"] != "preparing" and not recoverable_dependency:
                raise WorkflowError(
                    "invalid_state_transition",
                    "The work bundle cannot build a page baseline from its current state.",
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                )
            if dependency_status is None:
                dependency_status = preflight_module.check_dependencies(
                    environ=environ, visual_capability=args.visual_capability
                )
            if dependency_status["missing"]:
                if recoverable_dependency:
                    recorded = manifest["preflight"]
                    if (
                        recorded.get("missing") != dependency_status["missing"]
                        or recorded.get("dependencies")
                        != dependency_status["dependencies"]
                        or recorded.get("render_dpi") != args.render_dpi
                    ):
                        private_state = _read_json(
                            "private.json", dir_fd=descriptors["state"]
                        )
                        manifest = preflight_module.commit_dependency_missing(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            dependencies=dependency_status["dependencies"],
                            missing=dependency_status["missing"],
                            render_dpi=args.render_dpi,
                            at=operation_at,
                        )
                        _assert_bundle_descriptors_current(bundle, descriptors)
                    result = preflight_module.dependency_result(
                        manifest, work_bundle=str(bundle)
                    )
                    print(
                        f"[pdf2markdown] dependency_missing for work bundle {bundle.name}",
                        file=sys.stderr,
                    )
                    return result
                private_state = _read_json(
                    "private.json", dir_fd=descriptors["state"]
                )
                updated_manifest = preflight_module.commit_dependency_missing(
                    descriptors=descriptors,
                    manifest=manifest,
                    private_state=private_state,
                    dependencies=dependency_status["dependencies"],
                    missing=dependency_status["missing"],
                    render_dpi=args.render_dpi,
                    at=operation_at,
                )
                _assert_bundle_descriptors_current(bundle, descriptors)
                result = preflight_module.dependency_result(
                    updated_manifest, work_bundle=str(bundle)
                )
                print(
                    f"[pdf2markdown] dependency_missing for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            private_state = _read_json(
                "private.json", dir_fd=descriptors["state"]
            )
            try:
                updated_manifest = preflight_module.build_baseline(
                    descriptors=descriptors,
                    manifest=manifest,
                    private_state=private_state,
                    dependencies=dependency_status["dependencies"],
                    fitz=dependency_status["fitz"],
                    render_dpi=args.render_dpi,
                    at=operation_at,
                )
            except preflight_module.PreflightError as exc:
                blocker = preflight_module.blocker_for_error(exc)
                if blocker is not None:
                    updated_manifest = _commit_baseline_block(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        dependencies=dependency_status["dependencies"],
                        render_dpi=args.render_dpi,
                        blocker=blocker,
                        at=operation_at,
                    )
                    _assert_bundle_descriptors_current(bundle, descriptors)
                    result = preflight_module.result_from_manifest(
                        updated_manifest,
                        work_bundle=str(bundle),
                        outcome="preflight_blocked",
                    )
                    print(
                        f"[pdf2markdown] preflight_blocked for work bundle {bundle.name}",
                        file=sys.stderr,
                    )
                    return result
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=4,
                    action_required="inspect_preflight_failure",
                    context=inspected,
                ) from None
            _assert_bundle_descriptors_current(bundle, descriptors)
            result = preflight_module.result_from_manifest(
                updated_manifest,
                work_bundle=str(bundle),
                outcome="awaiting_preflight",
            )
            print(
                f"[pdf2markdown] awaiting_preflight for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result


def _recovered_review_outcome(recovered_review: dict) -> str:
    intent = recovered_review.get("intent")
    if isinstance(intent, dict):
        if intent.get("event") == "correction_record_intent":
            return "correction_applied"
        if (
            intent.get("event") == "review_record_intent"
            and intent.get("request_kind") == "review_decision"
        ):
            return "review_ambiguity_resolved"
    return recovered_review["manifest"]["review"]["status"]


def _recovered_review_command(intent: dict) -> str | None:
    if intent.get("event") == "correction_record_intent":
        return "correction"
    if intent.get("event") != "review_record_intent":
        return None
    return {
        "review": "review",
        "review_decision": "review-decision",
    }.get(intent.get("request_kind", "review"))


def _record(args, *, cwd: Path, environ: dict[str, str], now) -> dict:
    bundle = _canonical_bundle_path(args.work_bundle, cwd)
    with _exclusive_bundle_lock(bundle) as locked_descriptors:
        with _open_bundle_descriptors(
            bundle, locked_descriptors=locked_descriptors
        ) as descriptors:
            _assert_bundle_descriptors_current(bundle, descriptors)
            recovery_manifest = _read_json(
                "manifest.json", dir_fd=descriptors["root"]
            )
            _assert_frozen_source_before_recovery(
                descriptors=descriptors,
                manifest=recovery_manifest,
                work_bundle=str(bundle),
            )
            try:
                recovered_review = review_module.recover_pending_operation(
                    descriptors=descriptors,
                    manifest=recovery_manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    bundle_root=bundle,
                    expected_generation=args.expected_generation,
                    at=_isoformat(_moment(now)),
                )
            except (bundle_module.BundleStateError, review_module.ReviewError) as exc:
                code = getattr(exc, "code", "integrity_violation")
                raise WorkflowError(
                    code,
                    "A pending review operation cannot be recovered safely.",
                    return_code=(5 if code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if code == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_review is not None:
                recovered_manifest = recovered_review["manifest"]
                recovered_intent = recovered_review.get("intent")
                result = review_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=_recovered_review_outcome(recovered_review),
                )
                intent = recovered_intent
                if isinstance(intent, dict):
                    try:
                        replay_payload = (
                            review_module.load_record_input(Path(args.input), cwd=cwd)
                            if args.record_command in {"review", "review-decision"}
                            else correction_module.load_record_input(
                                Path(args.input), cwd=cwd
                            )
                            if args.record_command == "correction"
                            else None
                        )
                    except (review_module.ReviewError, correction_module.CorrectionError):
                        replay_payload = None
                    if not (
                        args.record_command
                        == _recovered_review_command(intent)
                        and args.action_id == intent.get("action_id")
                        and args.evidence_hash == intent.get("evidence_hash")
                        and replay_payload == intent.get("payload")
                    ):
                        raise WorkflowError(
                            "recovered_request_mismatch",
                            "A different review operation was recovered; this request was not applied.",
                            return_code=5,
                            action_required="inspect_current_generation",
                            context=result,
                        )
                print(
                    f"[pdf2markdown] recovered {result['outcome']} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_conversion = (
                    conversion_attempt_module.recover_interrupted_attempt(
                        descriptors=descriptors,
                        manifest=recovery_manifest,
                        private_state=_read_json(
                            "private.json", dir_fd=descriptors["state"]
                        ),
                        at=_isoformat(_moment(now)),
                        expected_generation=args.expected_generation,
                        resolve_history=_conversion_history_resolver(
                            recovery_manifest
                        ),
                    )
                )
            except conversion_attempt_module.ConversionAttemptError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=(5 if exc.code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if exc.code == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_conversion is not None:
                recovered_manifest, _recovered_private = recovered_conversion
                recovered_state = recovered_manifest["conversion_attempts"][-1][
                    "state"
                ]
                recovered_outcome = (
                    "conversion_retry_authorized"
                    if recovered_state == "not_started"
                    else "conversion_submitted"
                    if recovered_state == "submitted"
                    else recovered_state
                )
                result = conversion_attempt_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=recovered_outcome,
                )
                print(
                    f"[pdf2markdown] recovered {recovered_outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_staging = source_staging_module.recover_interrupted_attempt(
                    descriptors=descriptors,
                    manifest=recovery_manifest,
                    private_state=_read_json(
                        "private.json", dir_fd=descriptors["state"]
                    ),
                    at=_isoformat(_moment(now)),
                    expected_generation=args.expected_generation,
                )
            except source_staging_module.SourceStagingError as exc:
                raise WorkflowError(
                    exc.code,
                    "A pending source staging operation cannot be recovered safely.",
                    return_code=(5 if exc.code == "generation_conflict" else 4),
                    action_required=(
                        "inspect_current_generation"
                        if exc.code == "generation_conflict"
                        else "repair_or_restore_work_bundle"
                    ),
                    context={"work_bundle": str(bundle)},
                ) from None
            if recovered_staging is not None:
                recovered_manifest, _recovered_private = recovered_staging
                state = recovered_manifest["source_staging"]["state"]
                outcome = {
                    "source_upload_not_started": "source_upload_retry_authorized",
                    "source_upload_unknown": "source_upload_unknown",
                }.get(state, state)
                result = source_staging_module.result_from_manifest(
                    recovered_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                print(
                    f"[pdf2markdown] recovered {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            try:
                recovered_operation = preflight_module.recover_pending_operation(
                    descriptors=descriptors, at=_isoformat(_moment(now))
                )
            except (bundle_module.BundleStateError, preflight_module.PreflightError) as exc:
                code = getattr(exc, "code", "integrity_violation")
                raise WorkflowError(
                    code,
                    "A pending preflight operation cannot be recovered safely.",
                    return_code=4,
                    action_required="repair_or_restore_work_bundle",
                    context={"work_bundle": str(bundle)},
                ) from None
            inspected = _inspect_open_bundle(bundle, descriptors)
            replayed_recovery = (
                recovered_operation is not None
                and args.expected_generation
                == recovered_operation["expected_generation"]
            )
            if replayed_recovery:
                manifest = recovered_operation["manifest"]
                try:
                    replay_payload = (
                        preflight_module.load_record_input(Path(args.input), cwd=cwd)
                        if args.record_command == "preflight"
                        else None
                    )
                except preflight_module.PreflightError:
                    replay_payload = None
                if not preflight_module.recovered_request_matches(
                    descriptors=descriptors,
                    intent=recovered_operation["intent"],
                    record_command=args.record_command,
                    action_id=args.action_id,
                    evidence_hash=args.evidence_hash,
                    payload=replay_payload,
                    decision=getattr(args, "decision", None),
                    basis=getattr(args, "basis", None),
                ):
                    raise WorkflowError(
                        "recovered_request_mismatch",
                        "A different preflight operation was recovered; this request was not applied.",
                        return_code=5,
                        action_required="inspect_current_generation",
                        context=inspected,
                    )
                if recovered_operation["event"] == "preflight_decision_intent":
                    outcome = (
                        "preflight_warning_accepted"
                        if manifest["conversion_state"] == "ready_to_submit"
                        else "preflight_warning_declined"
                    )
                elif manifest["conversion_state"] == "preflight_warning":
                    outcome = "preflight_warning"
                elif manifest["conversion_state"] == "preflight_blocked":
                    outcome = "preflight_blocked"
                elif (
                    manifest.get("preflight", {}).get("result", {}).get("status")
                    == "warning"
                ):
                    outcome = "preflight_warning_auto_accepted"
                else:
                    outcome = "preflight_recorded"
                result = preflight_module.result_from_manifest(
                    manifest, work_bundle=str(bundle), outcome=outcome
                )
                print(
                    f"[pdf2markdown] recovered {outcome} for work bundle {bundle.name}",
                    file=sys.stderr,
                )
                return result
            if inspected["generation"] != args.expected_generation:
                raise WorkflowError(
                    "generation_conflict",
                    "Expected generation does not match the current work bundle state.",
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                )
            try:
                manifest = _read_json("manifest.json", dir_fd=descriptors["root"])
                private_state = _read_json(
                    "private.json", dir_fd=descriptors["state"]
                )
                if args.record_command == "review":
                    payload = review_module.load_record_input(
                        Path(args.input), cwd=cwd
                    )
                    updated_manifest = review_module.commit_review_record(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        bundle_root=bundle,
                        payload=payload,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        at=_isoformat(_moment(now)),
                    )
                elif args.record_command == "review-decision":
                    payload = review_module.load_record_input(
                        Path(args.input), cwd=cwd
                    )
                    updated_manifest = review_module.commit_review_decision(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        bundle_root=bundle,
                        payload=payload,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        at=_isoformat(_moment(now)),
                    )
                elif args.record_command == "correction":
                    payload = correction_module.load_record_input(
                        Path(args.input), cwd=cwd
                    )
                    updated_manifest = review_module.commit_correction_record(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        payload=payload,
                        bundle_root=bundle,
                        environ=environ,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        at=_isoformat(_moment(now)),
                    )
                elif args.record_command == "conversion":
                    updated_manifest = (
                        conversion_attempt_module.commit_retry_decision(
                            descriptors=descriptors,
                            manifest=manifest,
                            private_state=private_state,
                            expected_generation=args.expected_generation,
                            action_id=args.action_id,
                            evidence_hash=args.evidence_hash,
                            basis=args.basis,
                            at=_isoformat(_moment(now)),
                        )
                    )
                elif args.record_command == "source-staging":
                    updated_manifest = source_staging_module.commit_decision(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        decision=args.decision,
                        basis=args.basis,
                        at=_isoformat(_moment(now)),
                    )
                elif args.record_command == "preflight":
                    payload = preflight_module.load_record_input(
                        Path(args.input), cwd=cwd
                    )
                    updated_manifest = preflight_module.commit_preflight_record(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        payload=payload,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        at=_isoformat(_moment(now)),
                    )
                else:
                    updated_manifest = preflight_module.commit_preflight_decision(
                        descriptors=descriptors,
                        manifest=manifest,
                        private_state=private_state,
                        expected_generation=args.expected_generation,
                        action_id=args.action_id,
                        evidence_hash=args.evidence_hash,
                        decision=args.decision,
                        basis=args.basis,
                        at=_isoformat(_moment(now)),
                    )
            except conversion_attempt_module.ConversionAttemptError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                ) from None
            except source_staging_module.SourceStagingError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=5,
                    action_required="inspect_current_generation",
                    context=inspected,
                ) from None
            except preflight_module.PreflightError as exc:
                action_conflicts = {
                    "preflight_action_mismatch",
                    "evidence_hash_mismatch",
                    "action_already_consumed",
                }
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=(4 if exc.code == "integrity_violation" else 5 if exc.code in action_conflicts else 2),
                    action_required=(
                        "repair_or_restore_work_bundle"
                        if exc.code == "integrity_violation"
                        else "inspect_current_generation"
                        if exc.code in action_conflicts
                        else "correct_preflight_record"
                    ),
                    context=inspected,
                ) from None
            except review_module.ReviewError as exc:
                action_conflicts = {
                    "generation_conflict",
                    "review_action_mismatch",
                    "evidence_hash_mismatch",
                    "action_already_consumed",
                }
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=(
                        4
                        if exc.code == "integrity_violation"
                        else 5
                        if exc.code in action_conflicts
                        else 2
                    ),
                    action_required=(
                        "repair_or_restore_work_bundle"
                        if exc.code == "integrity_violation"
                        else "inspect_current_generation"
                        if exc.code in action_conflicts
                        else "correct_review_record"
                    ),
                    context=inspected,
                ) from None
            except correction_module.CorrectionError as exc:
                raise WorkflowError(
                    exc.code,
                    exc.message,
                    return_code=2,
                    action_required="correct_correction_record",
                    context=inspected,
                ) from None
            _assert_bundle_descriptors_current(bundle, descriptors)
            if args.record_command == "review":
                outcome = updated_manifest["review"]["status"]
            elif args.record_command == "review-decision":
                outcome = "review_ambiguity_resolved"
            elif args.record_command == "correction":
                outcome = "correction_applied"
            elif args.record_command == "conversion":
                outcome = "conversion_retry_authorized"
            elif args.record_command == "source-staging":
                outcome = (
                    "source_upload_retry_authorized"
                    if args.decision == "retry"
                    else "source_upload_waiting"
                )
            elif args.record_command == "decision":
                outcome = (
                    "preflight_warning_accepted"
                    if updated_manifest["conversion_state"] == "ready_to_submit"
                    else "preflight_warning_declined"
                )
            elif updated_manifest["conversion_state"] == "preflight_warning":
                outcome = "preflight_warning"
            elif updated_manifest["conversion_state"] == "preflight_blocked":
                outcome = "preflight_blocked"
            elif (
                updated_manifest.get("preflight", {}).get("result", {}).get("status")
                == "warning"
                and updated_manifest.get("preflight", {}).get("decision", {}).get("source")
                == "interaction_mode_auto"
            ):
                outcome = "preflight_warning_auto_accepted"
            else:
                outcome = "preflight_recorded"
            result = (
                review_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                if "review" in updated_manifest
                else conversion_attempt_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                if updated_manifest.get("conversion_attempts")
                else
                source_staging_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
                if "source_staging" in updated_manifest
                else preflight_module.result_from_manifest(
                    updated_manifest,
                    work_bundle=str(bundle),
                    outcome=outcome,
                )
            )
            print(
                f"[pdf2markdown] {outcome} for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result


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


def _resume(
    args,
    *,
    cwd: Path,
    environ: dict[str, str],
    config_home: Path,
    transport,
    now,
) -> dict:
    bundle = _canonical_bundle_path(args.work_bundle, cwd)
    advance_after_unlock = False
    with _exclusive_bundle_lock(bundle) as locked_descriptors:
        root_descriptor, state_descriptor, _lock_descriptor = locked_descriptors
        resume_manifest = _read_json("manifest.json", dir_fd=root_descriptor)
        resume_private = _read_json("private.json", dir_fd=state_descriptor)
        try:
            with _open_bundle_descriptors(
                bundle, locked_descriptors=locked_descriptors
            ) as review_descriptors:
                if "raw_conversion" in resume_manifest:
                    _assert_frozen_source_before_recovery(
                        descriptors=review_descriptors,
                        manifest=resume_manifest,
                        work_bundle=str(bundle),
                    )
                recovered_review = review_module.recover_pending_operation(
                    descriptors=review_descriptors,
                    manifest=resume_manifest,
                    private_state=resume_private,
                    bundle_root=bundle,
                    expected_generation=args.expected_generation,
                    at=_isoformat(_moment(now)),
                )
        except (bundle_module.BundleStateError, review_module.ReviewError) as exc:
            code = getattr(exc, "code", "integrity_violation")
            raise WorkflowError(
                code,
                "A pending review operation cannot be recovered safely.",
                return_code=(5 if code == "generation_conflict" else 4),
                action_required=(
                    "inspect_current_generation"
                    if code == "generation_conflict"
                    else "repair_or_restore_work_bundle"
                ),
                context={"work_bundle": str(bundle)},
            ) from None
        if recovered_review is not None:
            recovered_manifest = recovered_review["manifest"]
            result = review_module.result_from_manifest(
                recovered_manifest,
                work_bundle=str(bundle),
                outcome=_recovered_review_outcome(recovered_review),
            )
            print(
                f"[pdf2markdown] recovered {result['outcome']} for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
        try:
            with _open_bundle_descriptors(
                bundle, locked_descriptors=locked_descriptors
            ) as recovery_descriptors:
                if (
                    resume_manifest.get("conversion_state") == "result_downloading"
                    or "raw_conversion" in resume_manifest
                ):
                    _assert_frozen_source_before_recovery(
                        descriptors=recovery_descriptors,
                        manifest=resume_manifest,
                        work_bundle=str(bundle),
                    )
                recovered_raw = raw_conversion_module.recover_interrupted_adoption(
                    descriptors=recovery_descriptors,
                    manifest=resume_manifest,
                    private_state=resume_private,
                    at=_isoformat(_moment(now)),
                    expected_generation=args.expected_generation,
                    transport=transport,
                )
        except raw_conversion_module.RawConversionError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=(5 if exc.code == "generation_conflict" else 4),
                action_required=(
                    "inspect_current_generation"
                    if exc.code == "generation_conflict"
                    else "resume_same_conversion_result"
                ),
                context={"work_bundle": str(bundle)},
            ) from None
        if recovered_raw is not None:
            recovered_manifest, _recovered_private = recovered_raw
            outcome = (
                "raw_conversion_adopted"
                if recovered_manifest["conversion_state"] == "converted"
                else recovered_manifest["raw_conversion"]["reason_code"]
            )
            result = raw_conversion_module.result_from_manifest(
                recovered_manifest, work_bundle=str(bundle), outcome=outcome
            )
            print(
                f"[pdf2markdown] recovered {outcome} for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
        try:
            recovered_conversion = conversion_attempt_module.recover_interrupted_attempt(
                descriptors={"root": root_descriptor, "state": state_descriptor},
                manifest=resume_manifest,
                private_state=resume_private,
                at=_isoformat(_moment(now)),
                expected_generation=args.expected_generation,
                resolve_history=_conversion_history_resolver(resume_manifest),
            )
        except conversion_attempt_module.ConversionAttemptError as exc:
            raise WorkflowError(
                exc.code,
                exc.message,
                return_code=(5 if exc.code == "generation_conflict" else 4),
                action_required=(
                    "inspect_current_generation"
                    if exc.code == "generation_conflict"
                    else "repair_or_restore_work_bundle"
                ),
                context={"work_bundle": str(bundle)},
            ) from None
        if recovered_conversion is not None:
            recovered_manifest, _recovered_private = recovered_conversion
            recovered_outcome = (
                "conversion_submitted"
                if recovered_manifest["conversion_state"] == "submitted"
                else recovered_manifest["conversion_state"]
            )
            result = conversion_attempt_module.result_from_manifest(
                recovered_manifest,
                work_bundle=str(bundle),
                outcome=recovered_outcome,
            )
            print(
                f"[pdf2markdown] recovered {result['outcome']} for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
        try:
            recovered_staging = source_staging_module.recover_interrupted_attempt(
                descriptors={"root": root_descriptor, "state": state_descriptor},
                manifest=resume_manifest,
                private_state=resume_private,
                at=_isoformat(_moment(now)),
                expected_generation=args.expected_generation,
            )
        except source_staging_module.SourceStagingError as exc:
            raise WorkflowError(
                exc.code,
                "A pending source staging operation cannot be recovered safely.",
                return_code=(5 if exc.code == "generation_conflict" else 4),
                action_required=(
                    "inspect_current_generation"
                    if exc.code == "generation_conflict"
                    else "repair_or_restore_work_bundle"
                ),
                context={"work_bundle": str(bundle)},
            ) from None
        if recovered_staging is not None:
            recovered_manifest, _recovered_private = recovered_staging
            state = recovered_manifest["source_staging"]["state"]
            result = source_staging_module.result_from_manifest(
                recovered_manifest,
                work_bundle=str(bundle),
                outcome=state,
            )
            print(
                f"[pdf2markdown] recovered {state} for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
        if "review" in resume_manifest:
            prefix_state_resolver = lambda history: review_module.resolve_history_state(
                history,
                manifest_template=resume_manifest,
                private_template=resume_private,
            )
            manifest_transform = review_module.apply_settings_override_transition
        elif "raw_conversion" in resume_manifest:
            prefix_state_resolver = lambda history: raw_conversion_module.resolve_history_state(
                history,
                manifest_template=resume_manifest,
                private_template=resume_private,
            )
            manifest_transform = raw_conversion_module.apply_settings_override_transition
        elif "source_staging" in resume_manifest:
            prefix_state_resolver = lambda history: source_staging_module.resolve_history_state(
                history,
                manifest_template=resume_manifest,
                private_template=resume_private,
            )
            manifest_transform = source_staging_module.apply_settings_override_transition
        elif "preflight" in resume_manifest:
            prefix_state_resolver = preflight_module.reduce_preflight_history
            manifest_transform = None
        else:
            prefix_state_resolver = None
            manifest_transform = None
        try:
            recovered_override = bundle_module.recover_pending_settings_override(
                root_fd=root_descriptor,
                state_fd=state_descriptor,
                committed_at=_isoformat(_moment(now)),
                prefix_state_resolver=prefix_state_resolver,
                manifest_transform=manifest_transform,
            )
        except bundle_module.BundleStateError:
            raise WorkflowError(
                "invalid_bundle",
                "Pending settings override cannot be recovered safely.",
                return_code=4,
                action_required="repair_or_restore_work_bundle",
                context={"work_bundle": str(bundle)},
            ) from None
        result = _inspect_bundle(bundle, locked_descriptors=locked_descriptors)
        cli_overrides = _settings_cli(args)
        if (
            recovered_override is not None
            and args.expected_generation
            == recovered_override["expected_generation"]
            and any(value is not None for value in cli_overrides.values())
        ):
            try:
                replayed_snapshot, replayed_fields = (
                    settings_module.apply_resume_overrides(
                        recovered_override["previous_snapshot"], cli_overrides
                    )
                )
            except settings_module.SettingsError:
                replayed_snapshot, replayed_fields = None, []
            if (
                replayed_snapshot == recovered_override["settings_snapshot"]
                and replayed_fields == recovered_override["overridden_fields"]
            ):
                result["outcome"] = "settings_overridden"
                print(
                    f"[pdf2markdown] recovered settings override for {bundle.name}",
                    file=sys.stderr,
                )
                return result
        if result["generation"] != args.expected_generation:
            raise WorkflowError(
                "generation_conflict",
                "Expected generation does not match the current work bundle state.",
                return_code=5,
                action_required="inspect_current_generation",
                context=result,
            )
        if any(value is not None for value in cli_overrides.values()):
            manifest = _read_json("manifest.json", dir_fd=root_descriptor)
            try:
                updated_snapshot, overridden_fields = (
                    settings_module.apply_resume_overrides(
                        manifest["settings_snapshot"], cli_overrides
                    )
                )
            except settings_module.SettingsError:
                raise WorkflowError(
                    "configuration_invalid",
                    "Explicit settings override is invalid.",
                    return_code=6,
                    action_required="correct_settings_override",
                    context=result,
                ) from None
            recorded_at = _isoformat(_moment(now))
            try:
                committed_manifest = bundle_module.commit_settings_override(
                    root_fd=root_descriptor,
                    state_fd=state_descriptor,
                    expected_generation=manifest["generation"],
                    updated_snapshot=updated_snapshot,
                    overridden_fields=overridden_fields,
                    at=recorded_at,
                    state_validator=(
                        review_module.valid_history
                        if "review" in manifest
                        else raw_conversion_module.valid_history
                        if "raw_conversion" in manifest
                        else source_staging_module.valid_history
                        if "source_staging" in manifest
                        else preflight_module.valid_preflight_history
                        if "preflight" in manifest
                        else None
                    ),
                    manifest_transform=(
                        review_module.apply_settings_override_transition
                        if "review" in manifest
                        else raw_conversion_module.apply_settings_override_transition
                        if "raw_conversion" in manifest
                        else source_staging_module.apply_settings_override_transition
                        if "source_staging" in manifest
                        else None
                    ),
                )
            except bundle_module.BundleStateError:
                raise WorkflowError(
                    "invalid_bundle",
                    "Work bundle changed before settings could be overridden.",
                    return_code=4,
                    action_required="inspect_current_generation",
                    context=result,
                ) from None
            if "review" in committed_manifest:
                result = review_module.result_from_manifest(
                    committed_manifest,
                    work_bundle=str(bundle),
                    outcome="settings_overridden",
                )
            elif "raw_conversion" in committed_manifest:
                result = raw_conversion_module.result_from_manifest(
                    committed_manifest,
                    work_bundle=str(bundle),
                    outcome="settings_overridden",
                )
            else:
                result["generation"] = committed_manifest["generation"]
                result["publication_state"] = committed_manifest["publication_state"]
                result["outcome"] = "settings_overridden"
            print(
                f"[pdf2markdown] settings_overridden for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
        if (
            result["conversion_state"] == "ready_to_submit"
            or result.get("source_upload_state") is not None
            or (
                result["conversion_state"] == "awaiting_user"
                and result.get("review_status") == "review_incomplete"
                and args.visual_capability == "available"
            )
        ):
            advance_after_unlock = True
        else:
            result["outcome"] = "no_progress"
            print(
                f"[pdf2markdown] no_progress for work bundle {bundle.name}",
                file=sys.stderr,
            )
            return result
    if advance_after_unlock:
        return _advance(
            args,
            cwd=cwd,
            environ=environ,
            config_home=config_home,
            transport=transport,
            now=now,
        )
    raise AssertionError("resume progression was not resolved")


def _settings(
    args, *, environ: dict[str, str], cwd: Path, config_home: Path
) -> dict:
    settings_path = config_home / "settings.json"
    try:
        status_arguments = {
            "path": settings_path,
            "cli": _settings_cli(args),
            "environ": environ,
            "cwd": cwd,
            "config_home": config_home,
            "home_config_authorized": getattr(args, "use_local_key", False),
        }
        current_status = settings_module.status(**status_arguments)
        if args.settings_command == "init":
            document = current_status["persisted"]
            if document is None:
                settings_module.atomic_write(
                    settings_path, settings_module.default_document()
                )
                outcome = "settings_initialized"
            else:
                outcome = "settings_unchanged"
        elif args.settings_command == "set-mode":
            document = current_status["persisted"]
            updated = (
                settings_module.default_document() if document is None else document
            )
            updated["interaction_mode"] = args.mode
            settings_module.atomic_write(settings_path, updated)
            outcome = "settings_updated"
        elif args.settings_command == "set-publish-mode":
            document = current_status["persisted"]
            updated = (
                settings_module.default_document() if document is None else document
            )
            updated["publishing"]["mode"] = args.mode
            settings_module.atomic_write(settings_path, updated)
            outcome = "settings_updated"
        else:
            outcome = "settings_status"
        settings_status = (
            settings_module.status(**status_arguments)
            if args.settings_command in {"init", "set-mode", "set-publish-mode"}
            else current_status
        )
    except settings_module.SettingsWriteError:
        raise WorkflowError(
            "settings_write_failed",
            "Persistent settings could not be written atomically.",
            return_code=6,
            action_required="retry_settings_write",
        ) from None
    except settings_module.SettingsError:
        raise WorkflowError(
            "configuration_invalid",
            "Persistent settings are invalid.",
            return_code=6,
            action_required="repair_settings",
        ) from None
    result = {
        "schema_version": SCHEMA_VERSION,
        "work_bundle": None,
        "generation": None,
        "conversion_state": None,
        "publication_state": None,
        "outcome": outcome,
        "action_required": None,
        "action_id": None,
        "evidence_hash": None,
        "artifacts": ({"settings_file": str(settings_path)} if settings_path.exists() else {}),
        "errors": [],
        "settings": settings_status,
    }
    print(f"[pdf2markdown] {outcome}", file=sys.stderr)
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
    try:
        args = _parser().parse_args(argv)
        environment = dict(os.environ if environ is None else environ)
        invocation_cwd = Path(os.getcwd() if cwd is None else cwd)
        if config_home is not None:
            settings_home = _absolute(str(config_home), invocation_cwd)
        else:
            xdg_config_home = environment.get("XDG_CONFIG_HOME", "").strip()
            configured_home = environment.get("HOME", "").strip()
            base_config_home = (
                Path(xdg_config_home).expanduser()
                if xdg_config_home
                else Path(configured_home).expanduser() / ".config"
                if configured_home
                else Path.home() / ".config"
            )
            settings_home = _absolute(
                str(base_config_home / "pdf2markdown"), invocation_cwd
            )
        if args.command == "start":
            result = _start(
                args,
                environ=environment,
                cwd=invocation_cwd,
                config_home=settings_home,
                transport=transport,
                now=now,
            )
        elif args.command == "inspect":
            result = _inspect(args, cwd=invocation_cwd)
        elif args.command == "advance":
            result = _advance(
                args,
                cwd=invocation_cwd,
                environ=environment,
                config_home=settings_home,
                transport=transport,
                now=now,
            )
        elif args.command == "record":
            result = _record(
                args, cwd=invocation_cwd, environ=environment, now=now
            )
        elif args.command == "resume":
            has_resume_overrides = any(
                value is not None for value in _settings_cli(args).values()
            )
            if not has_resume_overrides and args.visual_capability is not None:
                result = _advance(
                    args,
                    cwd=invocation_cwd,
                    environ=environment,
                    config_home=settings_home,
                    transport=transport,
                    now=now,
                )
            else:
                result = _resume(
                    args,
                    cwd=invocation_cwd,
                    environ=environment,
                    config_home=settings_home,
                    transport=transport,
                    now=now,
                )
        else:
            result = _settings(
                args,
                environ=environment,
                cwd=invocation_cwd,
                config_home=settings_home,
            )
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
