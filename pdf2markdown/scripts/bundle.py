from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime

import settings
import pdf_source
import strict_json


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 8 * 1024 * 1024


class BundleStateError(ValueError):
    pass


def decode_json_object(data: bytes) -> dict:
    try:
        value = strict_json.loads(data)
    except strict_json.StrictJsonError as exc:
        raise BundleStateError("bundle JSON could not be read") from exc
    if not isinstance(value, dict):
        raise BundleStateError("bundle JSON must be an object")
    return value


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def atomic_write_json(name: str, value: dict, *, dir_fd: int) -> None:
    data = _json_bytes(value)
    descriptor = None
    temporary_name = None
    for counter in range(1000):
        candidate = f".{name}.{os.getpid()}.{counter}"
        try:
            descriptor = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=dir_fd,
            )
            temporary_name = candidate
            break
        except FileExistsError:
            continue
    if descriptor is None or temporary_name is None:
        raise BundleStateError("could not allocate atomic state file")
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
        os.fsync(dir_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def _open_private_file(name: str, *, dir_fd: int, writable: bool = False) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise BundleStateError("bundle state file is missing or unsafe") from exc
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_size > MAX_STATE_BYTES
    ):
        os.close(descriptor)
        raise BundleStateError("bundle state file is not a bounded private file")
    return descriptor


def _read_private_file(name: str, *, dir_fd: int) -> bytes:
    descriptor = _open_private_file(name, dir_fd=dir_fd)
    try:
        opened = os.fstat(descriptor)
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_STATE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_STATE_BYTES:
                raise BundleStateError("bundle state file exceeds its size limit")
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise BundleStateError("bundle state changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_json(name: str, *, dir_fd: int) -> dict:
    return decode_json_object(_read_private_file(name, dir_fd=dir_fd))


def read_history(*, state_fd: int) -> list[dict]:
    try:
        events = [
            decode_json_object(line)
            for line in _read_private_file("history.ndjson", dir_fd=state_fd).splitlines()
        ]
    except BundleStateError as exc:
        raise BundleStateError("bundle history could not be read") from exc
    if not events or not all(isinstance(event, dict) for event in events):
        raise BundleStateError("bundle history uses an unknown schema")
    return events


def append_history(event: dict, *, state_fd: int) -> None:
    data = _json_bytes(event)
    descriptor = _open_private_file(
        "history.ndjson", dir_fd=state_fd, writable=True
    )
    try:
        if os.fstat(descriptor).st_size + len(data) > MAX_STATE_BYTES:
            raise BundleStateError("bundle history exceeds its size limit")
        os.lseek(descriptor, 0, os.SEEK_END)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        current = os.stat("history.ndjson", dir_fd=state_fd, follow_symlinks=False)
        final = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (final.st_dev, final.st_ino):
            raise BundleStateError("bundle history identity changed during append")
        os.fsync(state_fd)
    finally:
        os.close(descriptor)


def publication_state(snapshot: dict) -> str:
    return (
        "blocked"
        if snapshot["publishing"]["mode"] == "upload"
        and snapshot["publishing"]["publisher_binding"] is None
        else "not_requested"
    )


def _valid_manifest_base(manifest) -> bool:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "generation",
        "conversion_state",
        "publication_state",
        "source",
        "settings_snapshot",
        "conversion_attempts",
        "final_markdown",
        "artifacts",
    }:
        return False
    source = manifest.get("source")
    origin = source.get("origin") if isinstance(source, dict) else None
    return (
        type(manifest.get("schema_version")) is int
        and manifest["schema_version"] == SCHEMA_VERSION
        and type(manifest.get("generation")) is int
        and manifest["generation"] >= 1
        and manifest.get("conversion_state") == "preparing"
        and manifest.get("publication_state") in {"not_requested", "blocked"}
        and isinstance(source, dict)
        and set(source)
        == {"original_name", "origin", "physical_path", "sha256", "size_bytes"}
        and isinstance(source.get("original_name"), str)
        and bool(source["original_name"])
        and pdf_source.valid_origin(origin)
        and source.get("physical_path") == "01-source/source.pdf"
        and isinstance(source.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is not None
        and type(source.get("size_bytes")) is int
        and source["size_bytes"] >= 0
        and manifest.get("conversion_attempts") == []
        and manifest.get("final_markdown") is None
        and manifest.get("artifacts") == {"source_pdf": "01-source/source.pdf"}
    )


def _valid_private_state(private_state) -> bool:
    return (
        isinstance(private_state, dict)
        and set(private_state)
        == {"schema_version", "generation", "source_uploads", "result_urls"}
        and type(private_state.get("schema_version")) is int
        and private_state["schema_version"] == SCHEMA_VERSION
        and type(private_state.get("generation")) is int
        and private_state["generation"] >= 1
        and private_state.get("source_uploads") == []
        and private_state.get("result_urls") == []
    )


def _is_timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def valid_settings_history(history, manifest: dict, source_sha256: str) -> bool:
    if not history or not isinstance(history[0], dict):
        return False
    started = history[0]
    if (
        set(started)
        != {
            "schema_version",
            "event",
            "generation",
            "at",
            "source_sha256",
            "settings_snapshot",
        }
        or type(started.get("schema_version")) is not int
        or started["schema_version"] != SCHEMA_VERSION
        or started.get("event") != "bundle_started"
        or type(started.get("generation")) is not int
        or started["generation"] != 1
        or not _is_timestamp(started.get("at"))
        or started.get("source_sha256") != source_sha256
    ):
        return False
    try:
        settings.validate_snapshot(started.get("settings_snapshot"))
    except settings.SettingsError:
        return False

    generation = 1
    current_snapshot = started["settings_snapshot"]
    index = 1
    while index < len(history):
        if index + 2 >= len(history):
            return False
        intent, prepared, committed = history[index : index + 3]
        if not all(isinstance(event, dict) for event in (intent, prepared, committed)):
            return False
        operation_id = f"settings-override-{generation + 1}"
        expected_hash = settings.snapshot_hash(current_snapshot)
        overridden_fields = intent.get("overridden_fields")
        updated_snapshot = intent.get("settings_snapshot")
        try:
            settings.validate_snapshot_transition(
                current_snapshot, updated_snapshot, overridden_fields
            )
            updated_hash = settings.snapshot_hash(updated_snapshot)
        except settings.SettingsError:
            return False
        if not _valid_override_events(
            intent,
            prepared,
            committed,
            operation_id=operation_id,
            generation=generation,
            previous_hash=expected_hash,
            updated_hash=updated_hash,
            updated_snapshot=updated_snapshot,
            overridden_fields=overridden_fields,
        ):
            return False
        generation += 1
        current_snapshot = updated_snapshot
        index += 3
    return (
        manifest.get("generation") == generation
        and manifest.get("settings_snapshot") == current_snapshot
        and manifest.get("publication_state") == publication_state(current_snapshot)
    )


def _valid_override_events(
    intent,
    prepared,
    committed,
    *,
    operation_id,
    generation,
    previous_hash,
    updated_hash,
    updated_snapshot,
    overridden_fields,
) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "overridden_fields",
            "previous_settings_hash",
            "settings_snapshot",
            "settings_snapshot_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent["schema_version"] == SCHEMA_VERSION
        and intent.get("event") == "settings_override_intent"
        and intent.get("operation_id") == operation_id
        and type(intent.get("expected_generation")) is int
        and intent["expected_generation"] == generation
        and type(intent.get("new_generation")) is int
        and intent["new_generation"] == generation + 1
        and _is_timestamp(intent.get("at"))
        and intent.get("previous_settings_hash") == previous_hash
        and intent.get("settings_snapshot_hash") == updated_hash
        and set(prepared)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "settings_snapshot_hash",
        }
        and type(prepared.get("schema_version")) is int
        and prepared["schema_version"] == SCHEMA_VERSION
        and prepared.get("event") == "settings_override_prepared"
        and prepared.get("operation_id") == operation_id
        and type(prepared.get("expected_generation")) is int
        and prepared["expected_generation"] == generation
        and type(prepared.get("new_generation")) is int
        and prepared["new_generation"] == generation + 1
        and _is_timestamp(prepared.get("at"))
        and prepared.get("settings_snapshot_hash") == updated_hash
        and set(committed)
        == {
            "schema_version",
            "event",
            "operation_id",
            "previous_generation",
            "generation",
            "at",
            "overridden_fields",
            "settings_snapshot",
            "settings_snapshot_hash",
        }
        and type(committed.get("schema_version")) is int
        and committed["schema_version"] == SCHEMA_VERSION
        and committed.get("event") == "settings_override_committed"
        and committed.get("operation_id") == operation_id
        and type(committed.get("previous_generation")) is int
        and committed["previous_generation"] == generation
        and type(committed.get("generation")) is int
        and committed["generation"] == generation + 1
        and _is_timestamp(committed.get("at"))
        and committed.get("overridden_fields") == overridden_fields
        and committed.get("settings_snapshot") == updated_snapshot
        and committed.get("settings_snapshot_hash") == updated_hash
    )


def _source_identity(*, root_fd: int) -> tuple[str, int]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_dir = source_file = None
    try:
        source_dir = os.open("01-source", directory_flags, dir_fd=root_fd)
        source_dir_info = os.fstat(source_dir)
        if (
            not stat.S_ISDIR(source_dir_info.st_mode)
            or stat.S_IMODE(source_dir_info.st_mode) != 0o700
        ):
            raise BundleStateError("source directory is unsafe")
        before = os.stat("source.pdf", dir_fd=source_dir, follow_symlinks=False)
        source_file = os.open("source.pdf", file_flags, dir_fd=source_dir)
        opened = os.fstat(source_file)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise BundleStateError("saved source is unsafe")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_file, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        final = os.fstat(source_file)
        current = os.stat("source.pdf", dir_fd=source_dir, follow_symlinks=False)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise BundleStateError("saved source changed while it was read")
        return digest.hexdigest(), size
    except OSError as exc:
        raise BundleStateError("saved source is missing or unsafe") from exc
    finally:
        for descriptor in (source_file, source_dir):
            if descriptor is not None:
                os.close(descriptor)


def commit_settings_override(
    *,
    root_fd: int,
    state_fd: int,
    expected_generation: int,
    updated_snapshot: dict,
    overridden_fields: list[str],
    at: str,
) -> dict:
    manifest = read_json("manifest.json", dir_fd=root_fd)
    private_state = read_json("private.json", dir_fd=state_fd)
    history = read_history(state_fd=state_fd)
    if not _valid_manifest_base(manifest) or not _valid_private_state(private_state):
        raise BundleStateError("work bundle state schema is invalid")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise BundleStateError("work bundle source is invalid")
    source_hash = source.get("sha256")
    digest, size = _source_identity(root_fd=root_fd)
    if (
        type(manifest.get("generation")) is not int
        or manifest["generation"] != expected_generation
        or type(private_state.get("generation")) is not int
        or private_state["generation"] != expected_generation
        or not isinstance(source_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
        or type(source.get("size_bytes")) is not int
        or digest != source_hash
        or size != source.get("size_bytes")
        or not valid_settings_history(history, manifest, source_hash)
    ):
        raise BundleStateError("work bundle changed before settings override")
    try:
        settings.validate_snapshot_transition(
            manifest["settings_snapshot"], updated_snapshot, overridden_fields
        )
    except settings.SettingsError as exc:
        raise BundleStateError("settings override transition is invalid") from exc
    new_generation = expected_generation + 1
    operation_id = f"settings-override-{new_generation}"
    updated_hash = settings.snapshot_hash(updated_snapshot)
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "settings_override_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "overridden_fields": overridden_fields,
        "previous_settings_hash": settings.snapshot_hash(
            manifest["settings_snapshot"]
        ),
        "settings_snapshot": updated_snapshot,
        "settings_snapshot_hash": updated_hash,
    }
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "settings_override_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "settings_snapshot_hash": updated_hash,
    }
    append_history(intent, state_fd=state_fd)
    append_history(prepared, state_fd=state_fd)
    private_state["generation"] = new_generation
    atomic_write_json("private.json", private_state, dir_fd=state_fd)
    manifest["generation"] = new_generation
    manifest["settings_snapshot"] = updated_snapshot
    manifest["publication_state"] = publication_state(updated_snapshot)
    atomic_write_json("manifest.json", manifest, dir_fd=root_fd)
    committed = _committed_event(intent, at=at)
    append_history(committed, state_fd=state_fd)
    return manifest


def _committed_event(intent: dict, *, at: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "settings_override_committed",
        "operation_id": intent.get("operation_id"),
        "previous_generation": intent.get("expected_generation"),
        "generation": intent.get("new_generation"),
        "at": at,
        "overridden_fields": intent.get("overridden_fields"),
        "settings_snapshot": intent.get("settings_snapshot"),
        "settings_snapshot_hash": intent.get("settings_snapshot_hash"),
    }


def recover_pending_settings_override(
    *, root_fd: int, state_fd: int, committed_at: str
) -> dict | None:
    manifest = read_json("manifest.json", dir_fd=root_fd)
    private_state = read_json("private.json", dir_fd=state_fd)
    history = read_history(state_fd=state_fd)
    if not _valid_manifest_base(manifest) or not _valid_private_state(private_state):
        raise BundleStateError("pending work bundle state schema is invalid")
    final_event = history[-1].get("event")
    if final_event == "settings_override_intent":
        prefix = history[:-1]
        intent = history[-1]
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "event": "settings_override_prepared",
            "operation_id": intent.get("operation_id"),
            "expected_generation": intent.get("expected_generation"),
            "new_generation": intent.get("new_generation"),
            "at": intent.get("at"),
            "settings_snapshot_hash": intent.get("settings_snapshot_hash"),
        }
        prepared_was_saved = False
    elif final_event == "settings_override_prepared" and len(history) >= 2:
        prefix = history[:-2]
        intent = history[-2]
        prepared = history[-1]
        prepared_was_saved = True
    else:
        return None
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    updated_snapshot = intent.get("settings_snapshot")
    overridden_fields = intent.get("overridden_fields")
    if (
        type(expected_generation) is not int
        or type(new_generation) is not int
        or new_generation != expected_generation + 1
        or not prefix
    ):
        raise BundleStateError("pending settings override is inconsistent")
    previous_snapshot = prefix[-1].get("settings_snapshot")
    try:
        settings.validate_snapshot(previous_snapshot)
        settings.validate_snapshot(updated_snapshot)
    except settings.SettingsError as exc:
        raise BundleStateError("pending settings snapshot is invalid") from exc
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise BundleStateError("work bundle source is invalid")
    source_hash = source.get("sha256")
    if (
        type(manifest.get("generation")) is not int
        or manifest["generation"] not in {expected_generation, new_generation}
        or not isinstance(source_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
        or type(source.get("size_bytes")) is not int
    ):
        raise BundleStateError("pending work bundle state is invalid")
    previous_manifest = dict(manifest)
    previous_manifest["generation"] = expected_generation
    previous_manifest["settings_snapshot"] = previous_snapshot
    previous_manifest["publication_state"] = publication_state(previous_snapshot)
    desired_manifest = dict(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["settings_snapshot"] = updated_snapshot
    desired_manifest["publication_state"] = publication_state(updated_snapshot)
    committed = _committed_event(intent, at=committed_at)
    if (
        not valid_settings_history(prefix, previous_manifest, source_hash)
        or not valid_settings_history(
            [*prefix, intent, prepared, committed], desired_manifest, source_hash
        )
        or (manifest != previous_manifest and manifest != desired_manifest)
        or private_state["generation"] not in {
            expected_generation,
            new_generation,
        }
        or (
            manifest == desired_manifest
            and private_state.get("generation") != new_generation
        )
    ):
        raise BundleStateError("pending settings override cannot be recovered safely")
    digest, size = _source_identity(root_fd=root_fd)
    if digest != source_hash or size != source.get("size_bytes"):
        raise BundleStateError("saved source does not match pending settings override")
    if not prepared_was_saved:
        append_history(prepared, state_fd=state_fd)
    if private_state["generation"] != new_generation:
        private_state["generation"] = new_generation
        atomic_write_json("private.json", private_state, dir_fd=state_fd)
    if manifest != desired_manifest:
        atomic_write_json("manifest.json", desired_manifest, dir_fd=root_fd)
    append_history(committed, state_fd=state_fd)
    return {
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "previous_snapshot": previous_snapshot,
        "settings_snapshot": updated_snapshot,
        "overridden_fields": overridden_fields,
    }
