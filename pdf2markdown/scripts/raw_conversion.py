from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from copy import deepcopy
from datetime import datetime

import bundle
import conversion_attempt
import doc2x
import result_archive


SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(
    r"conversion-attempt-[0-9]+-raw-adoption(?:-[0-9]{4})?"
)
RESERVATION_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
DETERMINISTIC_ARCHIVE_REJECTIONS = frozenset(
    {
        "invalid_result_archive",
        "unsafe_archive_path",
        "archive_path_conflict",
        "unsupported_archive_member_type",
        "encrypted_archive_unsupported",
        "unsupported_archive_compression",
        "archive_member_limit_exceeded",
        "archive_member_path_limit_exceeded",
        "archive_path_component_limit_exceeded",
        "archive_path_depth_limit_exceeded",
        "archive_path_component_budget_exceeded",
        "archive_member_size_limit_exceeded",
        "archive_compressed_limit_exceeded",
        "archive_uncompressed_limit_exceeded",
    }
)
RECOVERABLE_ARCHIVE_REJECTIONS = frozenset({"result_url_unavailable"})


class RawConversionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def object_hash(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _active_result(manifest: dict, private_state: dict):
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        manifest.get("conversion_state") != "result_downloading"
        or not isinstance(active, dict)
        or active.get("state") != "result_ready"
        or not isinstance(active.get("attempt_id"), str)
        or not isinstance(active.get("task_id"), str)
        or not isinstance(active.get("result_url_sha256"), str)
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise RawConversionError(
            "invalid_state_transition", "The work bundle has no ready result to adopt."
        )
    matching = [
        item
        for item in private_state.get("result_urls", [])
        if isinstance(item, dict)
        and item.get("attempt_id") == active["attempt_id"]
        and item.get("task_id") == active["task_id"]
        and item.get("url_sha256") == active["result_url_sha256"]
    ]
    if len(matching) != 1 or not doc2x.valid_https_url(matching[0].get("url")):
        raise RawConversionError(
            "integrity_violation", "The private result URL evidence is unavailable."
        )
    expected_hash = "sha256:" + hashlib.sha256(
        matching[0]["url"].encode("utf-8")
    ).hexdigest()
    if expected_hash != active["result_url_sha256"]:
        raise RawConversionError(
            "integrity_violation", "The private result URL evidence is inconsistent."
        )
    return active, matching[0]


def _operation_names(manifest: dict, attempt_id: str) -> tuple[str, str]:
    previous_records = manifest.get("raw_conversions", [])
    operation_number = 1 + sum(
        isinstance(record, dict) and record.get("attempt_id") == attempt_id
        for record in previous_records
    )
    operation_suffix = "" if operation_number == 1 else f"-{operation_number:04d}"
    operation_id = f"{attempt_id}-raw-adoption{operation_suffix}"
    return operation_id, attempt_id


def _reservation(
    manifest: dict,
    private_state: dict,
    *,
    at: str,
    token: str,
) -> dict:
    active, _private_result = _active_result(manifest, private_state)
    attempt_id = active["attempt_id"]
    operation_id, final_name = _operation_names(manifest, attempt_id)
    if RESERVATION_TOKEN_PATTERN.fullmatch(token) is None:
        raise RawConversionError(
            "integrity_violation", "The raw conversion reservation token is invalid."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "raw_conversion_reservation",
        "operation_id": operation_id,
        "reservation_id": f"{operation_id}-reservation-{token}",
        "expected_generation": manifest["generation"],
        "new_generation": manifest["generation"] + 1,
        "at": at,
        "attempt_id": attempt_id,
        "task_id": active["task_id"],
        "request_filename": active["request_summary"]["filename"],
        "interaction_mode": manifest["settings_snapshot"]["interaction_mode"],
        "result_url_sha256": active["result_url_sha256"],
        "staging_name": f".{attempt_id}.raw-{token}.part",
        "owner_marker_name": f".{attempt_id}.raw-{token}.owner",
        "final_name": final_name,
        "limits": result_archive.limits_record(),
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }


def _intent(reservation: dict, *, staging_identity: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "raw_conversion_intent",
        "operation_id": reservation["operation_id"],
        "reservation_id": reservation["reservation_id"],
        "reservation_hash": object_hash(reservation),
        "expected_generation": reservation["expected_generation"],
        "new_generation": reservation["new_generation"],
        "at": reservation["at"],
        "attempt_id": reservation["attempt_id"],
        "task_id": reservation["task_id"],
        "request_filename": reservation["request_filename"],
        "interaction_mode": reservation["interaction_mode"],
        "result_url_sha256": reservation["result_url_sha256"],
        "staging_name": reservation["staging_name"],
        "staging_identity": deepcopy(staging_identity),
        "owner_marker_name": reservation["owner_marker_name"],
        "final_name": reservation["final_name"],
        "limits": deepcopy(reservation["limits"]),
        "previous_manifest_hash": reservation["previous_manifest_hash"],
        "previous_private_hash": reservation["previous_private_hash"],
    }


def _prepared_record(intent: dict, prepared: result_archive.PreparedArchive) -> dict:
    final_base = f"03-converted/attempts/{intent['final_name']}"
    main_path = (
        None
        if prepared.main_markdown_path is None
        else f"{final_base}/raw/{prepared.main_markdown_path}"
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": intent["operation_id"],
        "state": (
            "committed" if prepared.reason_code is None else prepared.reason_code
        ),
        "reason_code": prepared.reason_code,
        "attempt_id": intent["attempt_id"],
        "task_id": intent["task_id"],
        "result_url_sha256": intent["result_url_sha256"],
        "archive_path": f"{final_base}/result.zip",
        "archive_sha256": prepared.archive_sha256,
        "archive_size_bytes": prepared.archive_size_bytes,
        "tree_path": f"{final_base}/raw",
        "tree_sha256": prepared.tree_sha256,
        "member_count": prepared.member_count,
        "total_compressed_bytes": prepared.total_compressed_bytes,
        "total_uncompressed_bytes": prepared.total_uncompressed_bytes,
        "main_markdown_path": main_path,
        "main_markdown_sha256": prepared.main_markdown_sha256,
        "pending_action": None,
        "limits": deepcopy(intent["limits"]),
        "committed_at": intent["at"],
    }
    if (
        prepared.reason_code == "unexpected_result_layout"
        and intent["interaction_mode"] == "confirm"
    ):
        record["pending_action"] = {
            "kind": "resolve_unexpected_result_layout",
            "action_id": f"conversion-decision-{secrets.token_hex(16)}",
            "generation": intent["new_generation"],
            "evidence_hash": object_hash(record),
        }
    return record


def _desired_state(
    manifest: dict, private_state: dict, *, record: dict, new_generation: int
) -> tuple[dict, dict]:
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = (
        "converted" if record["reason_code"] is None else "terminal_error"
    )
    previous_records = manifest.get("raw_conversions", [])
    desired_manifest["raw_conversions"] = [
        *deepcopy(previous_records),
        deepcopy(record),
    ]
    desired_manifest["raw_conversion"] = deepcopy(record)
    artifacts = deepcopy(manifest["artifacts"])
    artifacts["raw_result_zip"] = record["archive_path"]
    artifacts["raw_tree"] = record["tree_path"]
    if record["main_markdown_path"] is not None:
        artifacts["raw_markdown"] = record["main_markdown_path"]
    desired_manifest["artifacts"] = artifacts
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    return desired_manifest, desired_private


def _rejection_record(intent: dict, *, reason_code: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": intent["operation_id"],
        "state": "rejected",
        "reason_code": reason_code,
        "attempt_id": intent["attempt_id"],
        "task_id": intent["task_id"],
        "result_url_sha256": intent["result_url_sha256"],
        "staging_path": (
            f"03-converted/attempts/{intent['staging_name']}"
        ),
        "limits": deepcopy(intent["limits"]),
        "rejected_at": intent["at"],
    }


def _desired_rejection(
    manifest: dict, private_state: dict, *, record: dict, new_generation: int
) -> tuple[dict, dict]:
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = (
        "recoverable_error"
        if record["reason_code"] in RECOVERABLE_ARCHIVE_REJECTIONS
        else "terminal_error"
    )
    previous_records = manifest.get("raw_conversions", [])
    desired_manifest["raw_conversions"] = [
        *deepcopy(previous_records),
        deepcopy(record),
    ]
    desired_manifest["raw_conversion"] = deepcopy(record)
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    return desired_manifest, desired_private


def apply_settings_override_transition(previous: dict, updated: dict) -> dict:
    del previous
    transitioned = deepcopy(updated)
    record = transitioned.get("raw_conversion")
    records = transitioned.get("raw_conversions")
    if (
        not isinstance(record, dict)
        or record.get("reason_code") != "unexpected_result_layout"
        or not isinstance(records, list)
        or not records
        or not isinstance(records[-1], dict)
        or records[-1].get("operation_id") != record.get("operation_id")
    ):
        return transitioned
    mode = transitioned.get("settings_snapshot", {}).get("interaction_mode")
    if mode == "auto":
        record["pending_action"] = None
    elif mode == "confirm":
        pending = record.get("pending_action")
        if isinstance(pending, dict):
            rebound = deepcopy(pending)
            rebound["generation"] = transitioned["generation"]
            record["pending_action"] = rebound
        else:
            evidence_record = deepcopy(record)
            evidence_record["pending_action"] = None
            evidence_hash = object_hash(evidence_record)
            material = (
                f"settings-override:{transitioned['generation']}:{evidence_hash}"
            ).encode("ascii")
            record["pending_action"] = {
                "kind": "resolve_unexpected_result_layout",
                "action_id": "conversion-decision-"
                + hashlib.sha256(material).hexdigest()[:32],
                "generation": transitioned["generation"],
                "evidence_hash": evidence_hash,
            }
    transitioned["raw_conversion"] = record
    transitioned["raw_conversions"][-1] = deepcopy(record)
    return transitioned


def _commit_rejection(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    intent: dict,
    reason_code: str,
    at: str,
) -> tuple[dict, dict]:
    record = _rejection_record(intent, reason_code=reason_code)
    desired_manifest, desired_private = _desired_rejection(
        manifest,
        private_state,
        record=record,
        new_generation=intent["new_generation"],
    )
    try:
        _fsync_attempts(descriptors["attempts"])
    except OSError as exc:
        raise RawConversionError(
            "result_adoption_failed",
            "The rejected result parent could not be made durable.",
        ) from exc
    bundle.atomic_write_json(
        "private.json", desired_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", desired_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "raw_conversion_rejected",
            "operation_id": intent["operation_id"],
            "previous_generation": intent["expected_generation"],
            "generation": intent["new_generation"],
            "at": at,
            "record": record,
            "manifest_hash": object_hash(desired_manifest),
            "private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )
    return desired_manifest, desired_private


def _append_prepared(
    *, descriptors: dict, intent: dict, prepared: result_archive.PreparedArchive
) -> tuple[dict, dict]:
    record = _prepared_record(intent, prepared)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event": "raw_conversion_prepared",
        "operation_id": intent["operation_id"],
        "expected_generation": intent["expected_generation"],
        "new_generation": intent["new_generation"],
        "at": intent["at"],
        "intent_hash": object_hash(intent),
        "record": record,
    }
    bundle.append_history(event, state_fd=descriptors["state"])
    return event, record


def _artifact_values(attempts_fd: int, name: str) -> tuple[str, int, list[dict]]:
    final_fd = _open_dir_at(attempts_fd, name)
    final_info = os.fstat(final_fd)
    try:
        names = set(os.listdir(final_fd))
        if names != {"raw", "result.zip"}:
            raise RawConversionError(
                "integrity_violation", "The prepared conversion has extra artifacts."
            )
        archive_hash, archive_size = _hash_file_at(final_fd, "result.zip")
        raw_fd = _open_dir_at(final_fd, "raw")
        raw_info = os.fstat(raw_fd)
        try:
            records = _tree_records(raw_fd)
            _assert_directory_entry(
                final_fd, "raw", raw_fd, opened=raw_info
            )
        finally:
            os.close(raw_fd)
        _assert_directory_entry(attempts_fd, name, final_fd, opened=final_info)
        return archive_hash, archive_size, records
    finally:
        os.close(final_fd)


def _validate_artifact_name(
    *, attempts_fd: int, name: str, record: dict
) -> None:
    archive_hash, archive_size, records = _artifact_values(attempts_fd, name)
    if (
        archive_hash != record.get("archive_sha256")
        or archive_size != record.get("archive_size_bytes")
        or result_archive.canonical_tree_hash(records) != record.get("tree_sha256")
    ):
        raise RawConversionError(
            "integrity_violation", "The prepared conversion does not match its record."
        )
    main_path = record.get("main_markdown_path")
    if main_path is not None:
        relative = main_path.removeprefix(
            f"03-converted/attempts/{record['attempt_id']}/raw/"
        )
        matching = [item for item in records if item["path"] == relative]
        if (
            len(matching) != 1
            or matching[0]["sha256"] != record.get("main_markdown_sha256")
        ):
            raise RawConversionError(
                "integrity_violation", "The prepared raw Markdown identity is invalid."
            )


def _adopt_prepared(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    intent: dict,
    record: dict,
    at: str,
) -> tuple[dict, dict]:
    attempts_fd = descriptors["attempts"]
    part_exists = _path_kind(attempts_fd, intent["staging_name"]) == "directory"
    final_exists = _path_kind(attempts_fd, intent["final_name"]) == "directory"
    if part_exists == final_exists:
        raise RawConversionError(
            "integrity_violation",
            "Prepared conversion must exist at exactly one adoption path.",
        )
    artifact_name = intent["staging_name"] if part_exists else intent["final_name"]
    _assert_directory_identity(
        attempts_fd,
        artifact_name,
        intent["staging_identity"],
    )
    _validate_artifact_name(
        attempts_fd=attempts_fd, name=artifact_name, record=record
    )
    if part_exists:
        try:
            _rename_staging(
                attempts_fd, intent["staging_name"], intent["final_name"]
            )
        except OSError as exc:
            raise RawConversionError(
                "result_adoption_failed", "The prepared result could not be adopted."
            ) from exc
        _validate_artifact_name(
            attempts_fd=attempts_fd, name=intent["final_name"], record=record
        )
        _assert_directory_identity(
            attempts_fd,
            intent["final_name"],
            intent["staging_identity"],
        )
    try:
        _fsync_attempts(attempts_fd)
    except OSError as exc:
        raise RawConversionError(
            "result_adoption_failed",
            "The prepared result parent could not be made durable.",
        ) from exc
    desired_manifest, desired_private = _desired_state(
        manifest,
        private_state,
        record=record,
        new_generation=intent["new_generation"],
    )
    bundle.atomic_write_json(
        "private.json", desired_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", desired_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "raw_conversion_committed",
            "operation_id": intent["operation_id"],
            "previous_generation": intent["expected_generation"],
            "generation": intent["new_generation"],
            "at": at,
            "manifest_hash": object_hash(desired_manifest),
            "private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )
    return desired_manifest, desired_private


def _rename_staging(attempts_fd: int, staging_name: str, final_name: str) -> None:
    os.rename(
        staging_name,
        final_name,
        src_dir_fd=attempts_fd,
        dst_dir_fd=attempts_fd,
    )


def _fsync_attempts(attempts_fd: int) -> None:
    os.fsync(attempts_fd)


def _directory_identity(parent_fd: int, name: str) -> dict:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation", "The result staging identity is unavailable."
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RawConversionError(
            "integrity_violation", "The result staging identity is unsafe."
        )
    return {"device": info.st_dev, "inode": info.st_ino}


def _assert_directory_identity(parent_fd: int, name: str, expected: dict) -> None:
    if (
        not isinstance(expected, dict)
        or set(expected) != {"device", "inode"}
        or type(expected.get("device")) is not int
        or type(expected.get("inode")) is not int
        or expected["device"] < 0
        or expected["inode"] <= 0
        or _directory_identity(parent_fd, name) != expected
    ):
        raise RawConversionError(
            "integrity_violation", "The result staging directory identity changed."
        )


def _path_kind(parent_fd: int, name: str) -> str | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation", "A result adoption path is unsafe."
        ) from exc
    if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700:
        return "directory"
    if stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600:
        return "file"
    raise RawConversionError(
        "integrity_violation", "A result adoption path has an unsafe type."
    )


def _reservation_marker_payload(reservation: dict) -> bytes:
    return (object_hash(reservation) + "\n").encode("ascii")


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation", "A raw conversion reservation path is unsafe."
        ) from exc
    return True


def _assert_reservation_names_available(attempts_fd: int, reservation: dict) -> None:
    if any(
        _entry_exists(attempts_fd, reservation[name])
        for name in ("staging_name", "owner_marker_name", "final_name")
    ):
        raise RawConversionError(
            "staging_path_conflict",
            "The unique result staging reservation is unavailable.",
        )


def _create_owner_marker(attempts_fd: int, reservation: dict) -> None:
    name = reservation["owner_marker_name"]
    payload = _reservation_marker_payload(reservation)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    created = False
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=attempts_fd)
        created = True
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("reservation marker write made no progress")
            offset += written
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=attempts_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size != len(payload)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("reservation marker identity changed")
        os.fsync(attempts_fd)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                os.unlink(name, dir_fd=attempts_fd)
                os.fsync(attempts_fd)
            except OSError:
                pass
        raise RawConversionError(
            "integrity_violation",
            "The raw conversion owner marker could not be created safely.",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_owner_marker(attempts_fd: int, reservation: dict) -> None:
    name = reservation["owner_marker_name"]
    expected = _reservation_marker_payload(reservation)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        before = os.stat(name, dir_fd=attempts_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=attempts_fd)
        opened = os.fstat(descriptor)
        payload = os.read(descriptor, len(expected) + 1)
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=attempts_fd, follow_symlinks=False)
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation", "The raw conversion owner marker is invalid."
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    stable = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
        or payload != expected
        or any(getattr(opened, field) != getattr(before, field) for field in stable)
        or any(getattr(final, field) != getattr(opened, field) for field in stable)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion owner marker is invalid."
        )


def _create_staging_directory(attempts_fd: int, reservation: dict) -> dict:
    try:
        os.mkdir(reservation["staging_name"], 0o700, dir_fd=attempts_fd)
        os.fsync(attempts_fd)
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation",
            "The reserved result staging directory could not be created.",
        ) from exc
    return _directory_identity(attempts_fd, reservation["staging_name"])


def _ensure_reserved_staging(attempts_fd: int, reservation: dict) -> dict:
    if _entry_exists(attempts_fd, reservation["final_name"]):
        raise RawConversionError(
            "integrity_violation",
            "The reserved final raw conversion path is already occupied.",
        )
    marker_exists = _entry_exists(attempts_fd, reservation["owner_marker_name"])
    staging_exists = _entry_exists(attempts_fd, reservation["staging_name"])
    if not marker_exists:
        if staging_exists:
            raise RawConversionError(
                "integrity_violation",
                "An unowned raw conversion staging directory cannot be recovered.",
            )
        _create_owner_marker(attempts_fd, reservation)
    else:
        _validate_owner_marker(attempts_fd, reservation)
    if not staging_exists:
        identity = _create_staging_directory(attempts_fd, reservation)
    else:
        if _path_kind(attempts_fd, reservation["staging_name"]) != "directory":
            raise RawConversionError(
                "integrity_violation", "The reserved result staging path is unsafe."
            )
        staging_fd = _open_dir_at(attempts_fd, reservation["staging_name"])
        try:
            if os.listdir(staging_fd):
                raise RawConversionError(
                    "integrity_violation",
                    "The reserved result staging directory contains foreign data.",
                )
            identity = _directory_identity(attempts_fd, reservation["staging_name"])
        finally:
            os.close(staging_fd)
    _validate_owner_marker(attempts_fd, reservation)
    return identity


def _clear_owner_marker(attempts_fd: int, reservation: dict) -> None:
    _validate_owner_marker(attempts_fd, reservation)
    try:
        os.unlink(reservation["owner_marker_name"], dir_fd=attempts_fd)
        os.fsync(attempts_fd)
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation",
            "The raw conversion owner marker could not be retired safely.",
        ) from exc


def _finish_reservation_after_intent(
    attempts_fd: int, reservation: dict, intent: dict
) -> None:
    if _entry_exists(attempts_fd, reservation["owner_marker_name"]):
        _assert_directory_identity(
            attempts_fd, intent["staging_name"], intent["staging_identity"]
        )
        _clear_owner_marker(attempts_fd, reservation)


def adopt_ready_result(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    at: str,
    transport=None,
) -> tuple[dict, dict]:
    attempts_fd = descriptors["attempts"]
    active, _private_result = _active_result(manifest, private_state)
    reservation = _reservation(
        manifest,
        private_state,
        at=at,
        token=secrets.token_hex(16),
    )
    _assert_reservation_names_available(attempts_fd, reservation)
    bundle.append_history(reservation, state_fd=descriptors["state"])
    staging_identity = _ensure_reserved_staging(attempts_fd, reservation)
    intent = _intent(reservation, staging_identity=staging_identity)
    bundle.append_history(intent, state_fd=descriptors["state"])
    _finish_reservation_after_intent(attempts_fd, reservation, intent)
    _active, private_result = _active_result(manifest, private_state)
    if conversion_attempt.result_reference_is_expired(_active, at=at):
        return _commit_rejection(
            descriptors=descriptors,
            manifest=manifest,
            private_state=private_state,
            intent=intent,
            reason_code="result_url_unavailable",
            at=at,
        )
    _assert_directory_identity(
        attempts_fd, intent["staging_name"], intent["staging_identity"]
    )
    part_fd = _open_dir_at(attempts_fd, intent["staging_name"])
    try:
        try:
            prepared = result_archive.download_and_prepare(
                private_result["url"],
                part_fd,
                request_filename=intent["request_filename"],
                transport=transport,
            )
        except result_archive.ResultArchiveError as exc:
            if exc.code in (
                DETERMINISTIC_ARCHIVE_REJECTIONS
                | RECOVERABLE_ARCHIVE_REJECTIONS
            ):
                return _commit_rejection(
                    descriptors=descriptors,
                    manifest=manifest,
                    private_state=private_state,
                    intent=intent,
                    reason_code=exc.code,
                    at=at,
                )
            raise RawConversionError(exc.code, exc.message) from exc
    finally:
        os.close(part_fd)
    _prepared_event, record = _append_prepared(
        descriptors=descriptors, intent=intent, prepared=prepared
    )
    return _adopt_prepared(
        descriptors=descriptors,
        manifest=manifest,
        private_state=private_state,
        intent=intent,
        record=record,
        at=at,
    )


def _reservation_token(reservation: dict) -> str | None:
    operation_id = reservation.get("operation_id") if isinstance(reservation, dict) else None
    reservation_id = reservation.get("reservation_id") if isinstance(reservation, dict) else None
    prefix = f"{operation_id}-reservation-"
    if not isinstance(reservation_id, str) or not reservation_id.startswith(prefix):
        return None
    token = reservation_id.removeprefix(prefix)
    return token if RESERVATION_TOKEN_PATTERN.fullmatch(token) is not None else None


def _validate_reservation(
    reservation: dict, manifest: dict, private_state: dict
) -> None:
    token = _reservation_token(reservation)
    if token is None or reservation != _reservation(
        manifest,
        private_state,
        at=reservation.get("at"),
        token=token,
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion reservation is inconsistent."
        )


def _validate_pending_intent(
    intent: dict, reservation: dict, manifest: dict, private_state: dict
) -> None:
    _validate_reservation(reservation, manifest, private_state)
    if intent != _intent(
        reservation,
        staging_identity=intent.get("staging_identity"),
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion intent is inconsistent."
        )


def _remove_directory_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700:
            child = _open_dir_at(directory_fd, name)
            try:
                _remove_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        elif (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
        ):
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise RawConversionError(
                "integrity_violation", "An incomplete extraction member is unsafe."
            )
    os.fsync(directory_fd)


def _remove_raw_if_present(part_fd: int) -> None:
    kind = _path_kind(part_fd, "raw")
    if kind is None:
        return
    if kind != "directory":
        raise RawConversionError(
            "integrity_violation", "The incomplete extraction root is unsafe."
        )
    raw_fd = _open_dir_at(part_fd, "raw")
    try:
        _remove_directory_contents(raw_fd)
    finally:
        os.close(raw_fd)
    os.rmdir("raw", dir_fd=part_fd)
    os.fsync(part_fd)


def _prepare_pending_intent(
    *,
    descriptors: dict,
    intent: dict,
    private_result: dict,
    transport,
) -> result_archive.PreparedArchive:
    attempts_fd = descriptors["attempts"]
    if _path_kind(attempts_fd, intent["final_name"]) is not None:
        raise RawConversionError(
            "integrity_violation",
            "An unprepared result cannot already occupy the final path.",
        )
    part_kind = _path_kind(attempts_fd, intent["staging_name"])
    if part_kind is None:
        raise RawConversionError(
            "integrity_violation", "The owned result staging path is missing."
        )
    if part_kind != "directory":
        raise RawConversionError(
            "integrity_violation", "The result staging path is unsafe."
        )
    _assert_directory_identity(
        attempts_fd, intent["staging_name"], intent["staging_identity"]
    )
    part_fd = _open_dir_at(attempts_fd, intent["staging_name"])
    try:
        archive_kind = _path_kind(part_fd, "result.zip")
        unexpected = set(os.listdir(part_fd)) - {"result.zip", "raw"}
        if unexpected:
            raise RawConversionError(
                "integrity_violation", "The result staging path has unexpected data."
            )
        if archive_kind is None:
            _remove_raw_if_present(part_fd)
            local_archive = False
        elif archive_kind == "file":
            _remove_raw_if_present(part_fd)
            local_archive = True
        else:
            raise RawConversionError(
                "integrity_violation", "The staged result ZIP is unsafe."
            )
        if local_archive:
            try:
                return result_archive.extract_and_verify(
                    part_fd,
                    request_filename=intent["request_filename"],
                )
            except result_archive.ResultArchiveError as exc:
                if exc.code != "invalid_result_archive":
                    raise RawConversionError(exc.code, exc.message) from exc
                _remove_raw_if_present(part_fd)
                if _path_kind(part_fd, "result.zip") != "file":
                    raise RawConversionError(
                        "integrity_violation", "The incomplete result ZIP is unsafe."
                    )
                os.unlink("result.zip", dir_fd=part_fd)
                os.fsync(part_fd)
        try:
            return result_archive.download_and_prepare(
                private_result["url"],
                part_fd,
                request_filename=intent["request_filename"],
                transport=transport,
            )
        except result_archive.ResultArchiveError as exc:
            raise RawConversionError(exc.code, exc.message) from exc
    finally:
        os.close(part_fd)


def recover_interrupted_adoption(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    at: str,
    expected_generation: int,
    transport=None,
) -> tuple[dict, dict] | None:
    history = bundle.read_history(state_fd=descriptors["state"])
    reservation_indexes = [
        index
        for index, event in enumerate(history)
        if isinstance(event, dict)
        and event.get("event") == "raw_conversion_reservation"
    ]
    if not reservation_indexes:
        return None
    first = reservation_indexes[-1]
    suffix = history[first:]
    raw_operation_finished = (
        len(suffix) >= 3
        and isinstance(suffix[1], dict)
        and suffix[1].get("event") == "raw_conversion_intent"
        and isinstance(suffix[2], dict)
        and suffix[2].get("event") == "raw_conversion_rejected"
    ) or (
        len(suffix) >= 4
        and isinstance(suffix[1], dict)
        and suffix[1].get("event") == "raw_conversion_intent"
        and isinstance(suffix[2], dict)
        and suffix[2].get("event") == "raw_conversion_prepared"
        and isinstance(suffix[3], dict)
        and suffix[3].get("event") == "raw_conversion_committed"
    )
    if raw_operation_finished:
        return None
    reservation = suffix[0]
    intent_expected = reservation.get("expected_generation")
    intent_new = reservation.get("new_generation")
    if expected_generation not in {intent_expected, intent_new}:
        raise RawConversionError(
            "generation_conflict",
            "Expected generation does not match the pending raw conversion.",
        )
    committed_record = manifest.get("raw_conversion")
    if (
        isinstance(committed_record, dict)
        and committed_record.get("operation_id") == reservation.get("operation_id")
    ):
        prefix = _prefix_state(manifest, private_state)
    else:
        prefix_private = deepcopy(private_state)
        if prefix_private.get("generation") == intent_new:
            prefix_private["generation"] = intent_expected
        prefix = (manifest, prefix_private)
    if prefix is None:
        raise RawConversionError(
            "integrity_violation", "The raw conversion history has no valid prefix."
        )
    resolved = resolve_history_state(
        history[:first], manifest_template=prefix[0], private_template=prefix[1]
    )
    if resolved != prefix:
        raise RawConversionError(
            "integrity_violation", "The raw conversion history prefix is invalid."
        )
    _validate_reservation(reservation, prefix[0], prefix[1])
    if len(suffix) == 1:
        staging_identity = _ensure_reserved_staging(
            descriptors["attempts"], reservation
        )
        intent = _intent(reservation, staging_identity=staging_identity)
        bundle.append_history(intent, state_fd=descriptors["state"])
        _finish_reservation_after_intent(
            descriptors["attempts"], reservation, intent
        )
        suffix = [reservation, intent]
    elif (
        len(suffix) >= 2
        and isinstance(suffix[1], dict)
        and suffix[1].get("event") == "raw_conversion_intent"
    ):
        intent = suffix[1]
        _validate_pending_intent(intent, reservation, prefix[0], prefix[1])
        _finish_reservation_after_intent(
            descriptors["attempts"], reservation, intent
        )
    else:
        raise RawConversionError(
            "integrity_violation", "The raw conversion reservation suffix is invalid."
        )
    if len(suffix) == 2:
        _active, private_result = _active_result(prefix[0], prefix[1])
        try:
            prepared = _prepare_pending_intent(
                descriptors=descriptors,
                intent=intent,
                private_result=private_result,
                transport=transport,
            )
        except RawConversionError as exc:
            if exc.code in (
                DETERMINISTIC_ARCHIVE_REJECTIONS
                | RECOVERABLE_ARCHIVE_REJECTIONS
            ):
                return _commit_rejection(
                    descriptors=descriptors,
                    manifest=prefix[0],
                    private_state=prefix[1],
                    intent=intent,
                    reason_code=exc.code,
                    at=at,
                )
            raise
        _prepared_event, record = _append_prepared(
            descriptors=descriptors, intent=intent, prepared=prepared
        )
    elif len(suffix) == 3 and suffix[2].get("event") == "raw_conversion_prepared":
        prepared_event = suffix[2]
        record = prepared_event.get("record")
        if (
            not isinstance(record, dict)
            or prepared_event.get("operation_id") != intent.get("operation_id")
            or prepared_event.get("expected_generation") != intent_expected
            or prepared_event.get("new_generation") != intent_new
            or prepared_event.get("intent_hash") != object_hash(intent)
            or not _valid_record(record, intent)
        ):
            raise RawConversionError(
                "integrity_violation", "The prepared raw conversion record is invalid."
            )
    else:
        raise RawConversionError(
            "integrity_violation", "The raw conversion history suffix is invalid."
        )
    desired_manifest, desired_private = _desired_state(
        prefix[0], prefix[1], record=record, new_generation=intent_new
    )
    manifest_is_prefix = manifest == prefix[0]
    manifest_is_desired = manifest == desired_manifest
    private_is_prefix = private_state == prefix[1]
    private_is_desired = private_state == desired_private
    if (
        not (manifest_is_prefix or manifest_is_desired)
        or not (private_is_prefix or private_is_desired)
        or (manifest_is_desired and private_is_prefix)
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion state is partially inconsistent."
        )
    if manifest_is_desired:
        _validate_artifact_name(
            attempts_fd=descriptors["attempts"],
            name=intent["final_name"],
            record=record,
        )
        try:
            _fsync_attempts(descriptors["attempts"])
        except OSError as exc:
            raise RawConversionError(
                "result_adoption_failed",
                "The prepared result parent could not be made durable.",
            ) from exc
        if private_is_prefix:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        bundle.append_history(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "raw_conversion_committed",
                "operation_id": intent["operation_id"],
                "previous_generation": intent_expected,
                "generation": intent_new,
                "at": at,
                "manifest_hash": object_hash(desired_manifest),
                "private_hash": object_hash(desired_private),
            },
            state_fd=descriptors["state"],
        )
        return desired_manifest, desired_private
    return _adopt_prepared(
        descriptors=descriptors,
        manifest=prefix[0],
        private_state=prefix[1],
        intent=intent,
        record=record,
        at=at,
    )


def _prefix_state(manifest: dict, private_state: dict) -> tuple[dict, dict] | None:
    record = manifest.get("raw_conversion")
    records = manifest.get("raw_conversions")
    if (
        not isinstance(record, dict)
        or not isinstance(records, list)
        or not records
        or records[-1] != record
    ):
        return None
    prefix_manifest = deepcopy(manifest)
    previous_records = deepcopy(records[:-1])
    if previous_records:
        previous_record = previous_records[-1]
        prefix_manifest["raw_conversions"] = previous_records
        prefix_manifest["raw_conversion"] = deepcopy(previous_record)
    else:
        prefix_manifest.pop("raw_conversions", None)
        prefix_manifest.pop("raw_conversion", None)
    prefix_manifest["generation"] -= 1
    prefix_manifest["conversion_state"] = "result_downloading"
    artifacts = {
        key: value
        for key, value in prefix_manifest["artifacts"].items()
        if key not in {"raw_result_zip", "raw_tree", "raw_markdown"}
    }
    if previous_records and "archive_path" in previous_record:
        artifacts["raw_result_zip"] = previous_record["archive_path"]
        artifacts["raw_tree"] = previous_record["tree_path"]
        if previous_record.get("main_markdown_path") is not None:
            artifacts["raw_markdown"] = previous_record["main_markdown_path"]
    prefix_manifest["artifacts"] = artifacts
    prefix_private = deepcopy(private_state)
    prefix_private["generation"] -= 1
    return prefix_manifest, prefix_private


def valid_private_state(private_state: dict, manifest: dict) -> bool:
    return (
        isinstance(private_state, dict)
        and set(private_state)
        == {"schema_version", "generation", "source_uploads", "result_urls"}
        and private_state.get("schema_version") == SCHEMA_VERSION
        and type(private_state.get("generation")) is int
        and private_state.get("generation") == manifest.get("generation")
        and isinstance(private_state.get("source_uploads"), list)
        and isinstance(private_state.get("result_urls"), list)
    )


def _valid_record(record: dict, intent: dict) -> bool:
    expected_keys = {
        "schema_version",
        "operation_id",
        "state",
        "reason_code",
        "attempt_id",
        "task_id",
        "result_url_sha256",
        "archive_path",
        "archive_sha256",
        "archive_size_bytes",
        "tree_path",
        "tree_sha256",
        "member_count",
        "total_compressed_bytes",
        "total_uncompressed_bytes",
        "main_markdown_path",
        "main_markdown_sha256",
        "pending_action",
        "limits",
        "committed_at",
    }
    base = f"03-converted/attempts/{intent.get('final_name')}"
    main_path = record.get("main_markdown_path")
    main_hash = record.get("main_markdown_sha256")
    reason = record.get("reason_code")
    pending = record.get("pending_action")
    evidence_record = deepcopy(record) if isinstance(record, dict) else {}
    evidence_record["pending_action"] = None
    pending_valid = (
        pending is None
        if reason is None or intent.get("interaction_mode") == "auto"
        else isinstance(pending, dict)
        and set(pending)
        == {"kind", "action_id", "generation", "evidence_hash"}
        and pending.get("kind") == "resolve_unexpected_result_layout"
        and isinstance(pending.get("action_id"), str)
        and re.fullmatch(r"conversion-decision-[0-9a-f]{32}", pending["action_id"])
        is not None
        and pending.get("generation") == intent.get("new_generation")
        and pending.get("evidence_hash") == object_hash(evidence_record)
    )
    return (
        isinstance(record, dict)
        and set(record) == expected_keys
        and record.get("schema_version") == SCHEMA_VERSION
        and record.get("operation_id") == intent.get("operation_id")
        and record.get("attempt_id") == intent.get("attempt_id")
        and record.get("task_id") == intent.get("task_id")
        and record.get("result_url_sha256") == intent.get("result_url_sha256")
        and record.get("archive_path") == f"{base}/result.zip"
        and record.get("tree_path") == f"{base}/raw"
        and isinstance(record.get("archive_sha256"), str)
        and SHA256_PATTERN.fullmatch(record["archive_sha256"]) is not None
        and isinstance(record.get("tree_sha256"), str)
        and SHA256_PATTERN.fullmatch(record["tree_sha256"]) is not None
        and type(record.get("archive_size_bytes")) is int
        and 0 <= record["archive_size_bytes"] <= result_archive.MAX_ARCHIVE_BYTES
        and type(record.get("member_count")) is int
        and 0 <= record["member_count"] <= result_archive.MAX_MEMBERS
        and type(record.get("total_compressed_bytes")) is int
        and 0 <= record["total_compressed_bytes"] <= result_archive.MAX_TOTAL_COMPRESSED_BYTES
        and type(record.get("total_uncompressed_bytes")) is int
        and 0 <= record["total_uncompressed_bytes"] <= result_archive.MAX_TOTAL_UNCOMPRESSED_BYTES
        and record.get("limits") == intent.get("limits") == result_archive.limits_record()
        and record.get("committed_at") == intent.get("at")
        and pending_valid
        and reason in {None, "unexpected_result_layout"}
        and record.get("state") == ("committed" if reason is None else reason)
        and (
            reason == "unexpected_result_layout"
            and main_path is None
            and main_hash is None
            or reason is None
            and isinstance(main_path, str)
            and main_path.startswith(f"{base}/raw/")
            and isinstance(main_hash, str)
            and SHA256_PATTERN.fullmatch(main_hash) is not None
        )
    )


def _valid_rejection(record: dict, intent: dict) -> bool:
    return (
        isinstance(record, dict)
        and set(record)
        == {
            "schema_version",
            "operation_id",
            "state",
            "reason_code",
            "attempt_id",
            "task_id",
            "result_url_sha256",
            "staging_path",
            "limits",
            "rejected_at",
        }
        and record.get("schema_version") == SCHEMA_VERSION
        and record.get("operation_id") == intent.get("operation_id")
        and record.get("state") == "rejected"
        and record.get("reason_code")
        in (DETERMINISTIC_ARCHIVE_REJECTIONS | RECOVERABLE_ARCHIVE_REJECTIONS)
        and record.get("attempt_id") == intent.get("attempt_id")
        and record.get("task_id") == intent.get("task_id")
        and record.get("result_url_sha256") == intent.get("result_url_sha256")
        and record.get("staging_path")
        == f"03-converted/attempts/{intent.get('staging_name')}"
        and record.get("limits") == intent.get("limits") == result_archive.limits_record()
        and record.get("rejected_at") == intent.get("at")
    )


RAW_RESERVATION_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "reservation_id",
        "expected_generation",
        "new_generation",
        "at",
        "attempt_id",
        "task_id",
        "request_filename",
        "interaction_mode",
        "result_url_sha256",
        "staging_name",
        "owner_marker_name",
        "final_name",
        "limits",
        "previous_manifest_hash",
        "previous_private_hash",
    }
)
RAW_INTENT_KEYS = (
    RAW_RESERVATION_KEYS
    | {"reservation_hash", "staging_identity"}
)
RAW_COMMITTED_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "previous_generation",
        "generation",
        "at",
        "manifest_hash",
        "private_hash",
    }
)
CONVERSION_INTENTS = frozenset(
    {
        "conversion_submit_intent",
        "conversion_submit_result_intent",
        "conversion_retry_intent",
        "conversion_poll_result_intent",
    }
)


def _valid_reservation_shape(reservation: dict) -> bool:
    token = _reservation_token(reservation)
    attempt_id = reservation.get("attempt_id") if isinstance(reservation, dict) else None
    return (
        isinstance(reservation, dict)
        and set(reservation) == RAW_RESERVATION_KEYS
        and reservation.get("schema_version") == SCHEMA_VERSION
        and reservation.get("event") == "raw_conversion_reservation"
        and OPERATION_PATTERN.fullmatch(reservation.get("operation_id", "")) is not None
        and token is not None
        and type(reservation.get("expected_generation")) is int
        and reservation.get("new_generation")
        == reservation.get("expected_generation") + 1
        and _timestamp(reservation.get("at"))
        and reservation.get("interaction_mode") in {"confirm", "auto"}
        and isinstance(attempt_id, str)
        and reservation.get("staging_name") == f".{attempt_id}.raw-{token}.part"
        and reservation.get("owner_marker_name")
        == f".{attempt_id}.raw-{token}.owner"
        and reservation.get("final_name") == attempt_id
        and reservation.get("limits") == result_archive.limits_record()
    )


def _valid_intent_shape(intent: dict, reservation: dict) -> bool:
    identity = intent.get("staging_identity") if isinstance(intent, dict) else None
    return (
        isinstance(intent, dict)
        and set(intent) == RAW_INTENT_KEYS
        and intent.get("schema_version") == SCHEMA_VERSION
        and intent.get("event") == "raw_conversion_intent"
        and OPERATION_PATTERN.fullmatch(intent.get("operation_id", "")) is not None
        and _valid_reservation_shape(reservation)
        and intent.get("reservation_id") == reservation.get("reservation_id")
        and intent.get("reservation_hash") == object_hash(reservation)
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent.get("expected_generation") + 1
        and _timestamp(intent.get("at"))
        and intent.get("interaction_mode") in {"confirm", "auto"}
        and isinstance(identity, dict)
        and set(identity) == {"device", "inode"}
        and type(identity.get("device")) is int
        and type(identity.get("inode")) is int
        and identity["device"] >= 0
        and identity["inode"] > 0
        and intent.get("final_name") == intent.get("attempt_id")
        and intent.get("limits") == result_archive.limits_record()
    )


def _valid_committed_event(
    event: dict,
    *,
    intent: dict,
    event_name: str,
    manifest: dict,
    private_state: dict,
    record: dict | None = None,
) -> bool:
    return (
        isinstance(event, dict)
        and set(event)
        == (RAW_COMMITTED_KEYS | {"record"} if record is not None else RAW_COMMITTED_KEYS)
        and event.get("schema_version") == SCHEMA_VERSION
        and event.get("event") == event_name
        and event.get("operation_id") == intent.get("operation_id")
        and event.get("previous_generation") == intent.get("expected_generation")
        and event.get("generation") == intent.get("new_generation")
        and _timestamp(event.get("at"))
        and event.get("manifest_hash") == object_hash(manifest)
        and event.get("private_hash") == object_hash(private_state)
        and (record is None or event.get("record") == record)
    )


def _apply_raw_operation(
    history: list[dict],
    offset: int,
    manifest: dict,
    private_state: dict,
) -> tuple[dict, dict, int] | None:
    if offset + 2 >= len(history):
        return None
    reservation = history[offset]
    intent = history[offset + 1]
    if not _valid_reservation_shape(reservation) or not _valid_intent_shape(
        intent, reservation
    ):
        return None
    _validate_pending_intent(intent, reservation, manifest, private_state)
    next_event = history[offset + 2]
    if not isinstance(next_event, dict):
        return None
    if next_event.get("event") == "raw_conversion_rejected":
        record = next_event.get("record")
        if not _valid_rejection(record, intent):
            return None
        desired_manifest, desired_private = _desired_rejection(
            manifest,
            private_state,
            record=record,
            new_generation=intent["new_generation"],
        )
        if not _valid_committed_event(
            next_event,
            intent=intent,
            event_name="raw_conversion_rejected",
            manifest=desired_manifest,
            private_state=desired_private,
            record=record,
        ):
            return None
        return desired_manifest, desired_private, offset + 3
    if next_event.get("event") != "raw_conversion_prepared" or offset + 3 >= len(
        history
    ):
        return None
    prepared = next_event
    record = prepared.get("record")
    if (
        set(prepared)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "intent_hash",
            "record",
        }
        or prepared.get("schema_version") != SCHEMA_VERSION
        or prepared.get("operation_id") != intent.get("operation_id")
        or prepared.get("expected_generation") != intent.get("expected_generation")
        or prepared.get("new_generation") != intent.get("new_generation")
        or prepared.get("at") != intent.get("at")
        or prepared.get("intent_hash") != object_hash(intent)
        or not _valid_record(record, intent)
    ):
        return None
    desired_manifest, desired_private = _desired_state(
        manifest,
        private_state,
        record=record,
        new_generation=intent["new_generation"],
    )
    committed = history[offset + 3]
    if not _valid_committed_event(
        committed,
        intent=intent,
        event_name="raw_conversion_committed",
        manifest=desired_manifest,
        private_state=desired_private,
    ):
        return None
    return desired_manifest, desired_private, offset + 4


def resolve_history_state(
    history: list[dict], *, manifest_template: dict, private_template: dict
) -> tuple[dict, dict] | None:
    try:
        first = next(
            (
                index
                for index, event in enumerate(history)
                if isinstance(event, dict)
                and event.get("event") == "raw_conversion_reservation"
            ),
            None,
        )
        if first is None:
            return conversion_attempt.resolve_history_state(
                history,
                manifest_template=manifest_template,
                private_template=private_template,
            )
        prefix = conversion_attempt.resolve_history_state(
            history[:first],
            manifest_template=manifest_template,
            private_template=private_template,
        )
        if prefix is None:
            return None
        current_manifest, current_private = prefix
        offset = first
        while offset < len(history):
            event = history[offset]
            event_name = event.get("event") if isinstance(event, dict) else None
            if event_name == "raw_conversion_reservation":
                transition = _apply_raw_operation(
                    history, offset, current_manifest, current_private
                )
                if transition is None:
                    return None
                current_manifest, current_private, offset = transition
                continue
            if event_name in CONVERSION_INTENTS:
                if offset + 1 >= len(history):
                    return None
                transition = conversion_attempt.apply_committed_operations(
                    history[offset : offset + 2],
                    manifest=current_manifest,
                    private_state=current_private,
                    private_template=private_template,
                )
                if transition is None:
                    return None
                current_manifest, current_private = transition
                offset += 2
                continue
            if offset + 2 >= len(history):
                return None
            transition = bundle.apply_settings_override_events(
                current_manifest,
                current_private,
                history[offset],
                history[offset + 1],
                history[offset + 2],
                manifest_transform=apply_settings_override_transition,
            )
            if transition is None:
                return None
            current_manifest, current_private = transition
            offset += 3
        return current_manifest, current_private
    except (KeyError, TypeError, ValueError, RawConversionError):
        return None


def valid_history(history: list[dict], manifest: dict, private_state: dict) -> bool:
    return valid_private_state(private_state, manifest) and resolve_history_state(
        history, manifest_template=manifest, private_template=private_state
    ) == (manifest, private_state)


def _hash_file_at(parent_fd: int, name: str) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RawConversionError(
                "integrity_violation", "A raw conversion file is unsafe."
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, result_archive.STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            size != opened.st_size
            or any(
                getattr(final, field) != getattr(opened, field)
                for field in stable_fields
            )
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RawConversionError(
                "integrity_violation", "A raw conversion file changed during inspection."
            )
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _open_dir_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise RawConversionError(
            "integrity_violation", "A raw conversion directory is unsafe."
        )
    return descriptor


def _assert_directory_entry(
    parent_fd: int, name: str, descriptor: int, *, opened
) -> None:
    try:
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise RawConversionError(
            "integrity_violation", "A raw conversion directory changed during inspection."
        ) from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(getattr(final, field) != getattr(opened, field) for field in stable_fields)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RawConversionError(
            "integrity_violation", "A raw conversion directory changed during inspection."
        )


def _tree_records(directory_fd: int, *, prefix: str = "") -> list[dict]:
    opened_directory = os.fstat(directory_fd)
    records = []
    for name in sorted(os.listdir(directory_fd)):
        if not isinstance(name, str) or name in {"", ".", ".."}:
            raise RawConversionError(
                "integrity_violation", "A raw conversion path is invalid."
            )
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        path = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(info.st_mode):
            records.append({"path": path, "type": "directory"})
            child = _open_dir_at(directory_fd, name)
            child_info = os.fstat(child)
            try:
                records.extend(_tree_records(child, prefix=path))
                _assert_directory_entry(
                    directory_fd, name, child, opened=child_info
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            digest, size = _hash_file_at(directory_fd, name)
            records.append(
                {"path": path, "type": "file", "size_bytes": size, "sha256": digest}
            )
        else:
            raise RawConversionError(
                "integrity_violation", "A raw conversion member is unsafe."
            )
    final_directory = os.fstat(directory_fd)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(
        getattr(final_directory, field) != getattr(opened_directory, field)
        for field in stable_fields
    ):
        raise RawConversionError(
            "integrity_violation", "A raw conversion tree changed during inspection."
        )
    return records


def _validate_committed_record(*, attempts_fd: int, record: dict) -> None:
    try:
        archive_hash, archive_size, records = _artifact_values(
            attempts_fd, record["attempt_id"]
        )
    except (KeyError, OSError) as exc:
        raise RawConversionError(
            "integrity_violation", "The raw conversion artifacts are missing or unsafe."
        ) from exc
    if (
        archive_hash != record.get("archive_sha256")
        or archive_size != record.get("archive_size_bytes")
        or result_archive.canonical_tree_hash(records) != record.get("tree_sha256")
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion artifacts do not match the manifest."
        )
    if record.get("main_markdown_path") is not None:
        relative = record["main_markdown_path"].removeprefix(
            f"03-converted/attempts/{record['attempt_id']}/raw/"
        )
        matching = [item for item in records if item["path"] == relative]
        if (
            len(matching) != 1
            or matching[0]["sha256"] != record.get("main_markdown_sha256")
        ):
            raise RawConversionError(
                "integrity_violation", "The raw Markdown identity is invalid."
            )


def validate_committed_artifacts(*, descriptors: dict, manifest: dict) -> None:
    latest = manifest.get("raw_conversion")
    raw_records = manifest.get("raw_conversions")
    if (
        not isinstance(latest, dict)
        or not isinstance(raw_records, list)
        or not raw_records
        or raw_records[-1] != latest
        or any(not isinstance(record, dict) for record in raw_records)
    ):
        raise RawConversionError(
            "integrity_violation", "The raw conversion record is missing."
        )
    committed_attempts = [
        record["attempt_id"]
        for record in raw_records
        if "archive_path" in record and isinstance(record.get("attempt_id"), str)
    ]
    if len(committed_attempts) != len(set(committed_attempts)):
        raise RawConversionError(
            "integrity_violation", "A conversion attempt has multiple formal raw trees."
        )
    committed_attempt_ids = set(committed_attempts)
    for record in raw_records:
        attempt_id = record.get("attempt_id")
        if not isinstance(attempt_id, str):
            raise RawConversionError(
                "integrity_violation", "A raw conversion attempt identity is invalid."
            )
        if "archive_path" in record:
            _validate_committed_record(
                attempts_fd=descriptors["attempts"], record=record
            )
        elif (
            attempt_id not in committed_attempt_ids
            and _path_kind(descriptors["attempts"], attempt_id) is not None
        ):
            raise RawConversionError(
                "integrity_violation",
                "A rejected conversion cannot occupy the final artifact path.",
            )


def result_from_manifest(manifest: dict, *, work_bundle: str, outcome: str) -> dict:
    result = conversion_attempt.result_from_manifest(
        manifest, work_bundle=work_bundle, outcome=outcome
    )
    record = manifest.get("raw_conversion")
    result["raw_conversion_state"] = (
        None if not isinstance(record, dict) else record.get("state")
    )
    pending = record.get("pending_action") if isinstance(record, dict) else None
    if isinstance(pending, dict):
        result["action_required"] = pending["kind"]
        result["action_id"] = pending["action_id"]
        result["evidence_hash"] = pending["evidence_hash"]
    return result
