from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import unquote_to_bytes

from safe_io import (
    FileSecurityError, atomic_write, ensure_directory, lexical_absolute,
    open_directory, read_regular_file,
)
from strict_json import StrictJSONError, canonicalize, loads
from v2_schema import (
    AccessPolicy, RetentionPolicy, SchemaError, UploadTarget, normalize_endpoint,
    normalize_public_base, parse_reference, validate_object_key,
)


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
UUID4_RE = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class ArtifactError(ValueError):
    pass


class IdentifierRejected(ArtifactError):
    def __init__(self) -> None:
        super().__init__("provider identifier was rejected")


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} must be an object")
    return value


def _exact(value: Dict[str, Any], keys: Sequence[str], label: str) -> None:
    expected = set(keys)
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ArtifactError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ArtifactError(f"{label} is missing fields: {', '.join(missing)}")


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ArtifactError(f"invalid {label}")
    return value


def _decimal(value: Any, label: str, *, nullable: bool = False) -> Optional[str]:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ArtifactError(f"invalid {label}")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ArtifactError(f"invalid {label}")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ArtifactError(f"invalid {label}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ArtifactError(f"invalid {label}") from exc
    return value


def _uuid4(value: Any, label: str) -> str:
    if not isinstance(value, str) or not UUID4_RE.fullmatch(value):
        raise ArtifactError(f"invalid {label}")
    return value


def _strict_percent_decode(value: str) -> str:
    index = 0
    while index < len(value):
        if value[index] == "%":
            if index + 2 >= len(value) or not re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1:index + 3]):
                raise IdentifierRejected()
            index += 3
        else:
            index += 1
    try:
        return unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdentifierRejected() from exc


def validate_provider_identifier(value: Any, credentials: Iterable[str] = ()) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8", "ignore")) > 4096:
        raise IdentifierRejected()
    if any(not 0x21 <= ord(character) <= 0x7E for character in value):
        raise IdentifierRejected()
    decoded = _strict_percent_decode(value)
    for credential in credentials:
        if credential and (credential in value or credential in decoded):
            raise IdentifierRejected()
    return value


def build_object_reference(*, target_ref: str, target: UploadTarget, key: str,
                           version_id: Optional[str] = None,
                           credentials: Iterable[str] = ()) -> Dict[str, Any]:
    ref = parse_reference(target_ref, "target_ref")
    if version_id is not None:
        version_id = validate_provider_identifier(version_id, credentials)
    validate_object_key(key)
    return {
        "schema_version": 1,
        "target_ref": ref.text,
        "target_fingerprint": target.location_fingerprint(),
        "location": {
            "provider": target.provider,
            "endpoint": target.endpoint,
            "addressing": target.addressing,
            "region": target.region,
            "bucket": target.bucket,
            "key": key,
            "version_id": version_id,
        },
        "access": {
            "mode": target.access.mode,
            "public_base_url": target.access.public_base_url,
            "presign_expires_seconds": target.access.presign_expires_seconds,
        },
        "retention": target.retention.result(),
    }


def _validate_reference(value: Any, credentials: Iterable[str] = ()) -> Dict[str, Any]:
    item = _object(value, "Object Reference")
    keys = ("schema_version", "target_ref", "target_fingerprint", "location", "access", "retention")
    _exact(item, keys, "Object Reference")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool):
        raise ArtifactError("Object Reference schema_version must be 1")
    parse_reference(item["target_ref"], "target_ref")
    if not isinstance(item["target_fingerprint"], str) or not FINGERPRINT_RE.fullmatch(item["target_fingerprint"]):
        raise ArtifactError("invalid target_fingerprint")
    location = _object(item["location"], "Object Reference location")
    _exact(location, ("provider", "endpoint", "addressing", "region", "bucket", "key", "version_id"), "Object Reference location")
    for key in ("provider", "region", "bucket"):
        if not isinstance(location[key], str) or not location[key] or any(ord(char) < 0x20 or ord(char) == 0x7F for char in location[key]):
            raise ArtifactError(f"invalid location.{key}")
    if location["endpoint"] != normalize_endpoint(location["endpoint"]):
        raise ArtifactError("location.endpoint is not normalized")
    if location["addressing"] not in {"path", "virtual", "bucket-bound"}:
        raise ArtifactError("invalid location.addressing")
    validate_object_key(location["key"])
    if location["version_id"] is not None:
        validate_provider_identifier(location["version_id"], credentials)
    access = _object(item["access"], "Object Reference access")
    _exact(access, ("mode", "public_base_url", "presign_expires_seconds"), "Object Reference access")
    if access["mode"] == "private":
        if access["public_base_url"] is not None:
            raise ArtifactError("private reference must not contain public_base_url")
        _integer(access["presign_expires_seconds"], 1, 604800, "presign expiry")
    elif access["mode"] == "public":
        if access["presign_expires_seconds"] is not None or not isinstance(access["public_base_url"], str):
            raise ArtifactError("invalid public reference access")
        if access["public_base_url"] != normalize_public_base(access["public_base_url"]):
            raise ArtifactError("public_base_url is not normalized")
    else:
        raise ArtifactError("invalid access mode")
    retention = _object(item["retention"], "Object Reference retention")
    _exact(retention, ("mode", "days", "enforcement"), "Object Reference retention")
    if retention["enforcement"] != "external-unverified":
        raise ArtifactError("invalid retention enforcement")
    if retention["mode"] == "retain":
        if retention["days"] is not None:
            raise ArtifactError("retain requires days=null")
    elif retention["mode"] == "expire":
        _integer(retention["days"], 1, (1 << 53) - 1, "retention days")
    else:
        raise ArtifactError("invalid retention mode")
    return item


def parse_object_reference(text: str, credentials: Iterable[str] = ()) -> Dict[str, Any]:
    try:
        return _validate_reference(loads(text), credentials)
    except (StrictJSONError, SchemaError) as exc:
        raise ArtifactError(f"invalid Object Reference: {exc}") from exc


def serialize_object_reference(reference: Dict[str, Any]) -> bytes:
    return canonicalize(_validate_reference(reference))


def lifecycle_policy_id(target_ref: str, target: UploadTarget) -> str:
    parse_reference(target_ref, "target_ref")
    value = {
        "target_ref": target_ref,
        "provider": target.provider,
        "endpoint": target.endpoint,
        "bucket": target.bucket,
        "prefix": target.prefix,
        "retention": {"mode": target.retention.mode, "days": target.retention.days},
    }
    return "s3-upload-v2-" + hashlib.sha256(canonicalize(value)).hexdigest()


def _validate_upload_plan(value: Any) -> Dict[str, Any]:
    item = _object(value, "upload_plan")
    _exact(item, ("content_type", "cache_control", "content_disposition", "presign_expires_seconds"), "upload_plan")
    if not isinstance(item["content_type"], str) or not item["content_type"]:
        raise ArtifactError("invalid upload_plan.content_type")
    for key in ("cache_control", "content_disposition"):
        if item[key] is not None and not isinstance(item[key], str):
            raise ArtifactError(f"invalid upload_plan.{key}")
    if item["presign_expires_seconds"] is not None:
        _integer(item["presign_expires_seconds"], 1, 604800, "upload plan presign expiry")
    return item


def _validate_source(value: Any) -> Dict[str, Any]:
    item = _object(value, "source")
    _exact(item, ("path", "size", "mtime_ns", "device", "inode", "sha256"), "source")
    if not isinstance(item["path"], str) or not os.path.isabs(item["path"]):
        raise ArtifactError("source.path must be absolute")
    _integer(item["size"], 0, (1 << 53) - 1, "source.size")
    _decimal(item["mtime_ns"], "source.mtime_ns")
    _decimal(item["device"], "source.device", nullable=True)
    _decimal(item["inode"], "source.inode", nullable=True)
    _hash(item["sha256"], "source.sha256")
    return item


def _validate_collision(value: Any, kind: str) -> Dict[str, Any]:
    item = _object(value, "collision")
    _exact(item, ("policy", "base_key", "attempt", "max_attempts"), "collision")
    if item["policy"] not in {"replace", "unique", "reject"}:
        raise ArtifactError("invalid collision policy")
    validate_object_key(item["base_key"])
    attempt = _integer(item["attempt"], 1, 5, "collision.attempt")
    maximum = _integer(item["max_attempts"], 1, 5, "collision.max_attempts")
    if attempt > maximum:
        raise ArtifactError("collision attempt exceeds maximum")
    if kind == "multipart" and maximum != 1:
        raise ArtifactError("multipart collision max_attempts must be 1")
    return item


def _validate_multipart(value: Any, state: str, source_size: int,
                        credentials: Iterable[str]) -> Dict[str, Any]:
    item = _object(value, "multipart")
    keys = ("upload_id", "part_size_bytes", "part_max_attempts", "return_state", "in_flight_part", "acknowledged_parts")
    _exact(item, keys, "multipart")
    part_size = _integer(item["part_size_bytes"], 5242880, 536870912, "part_size_bytes")
    maximum = _integer(item["part_max_attempts"], 1, 5, "part_max_attempts")
    if item["upload_id"] is not None:
        validate_provider_identifier(item["upload_id"], credentials)
    pre_session_states = {"prepared", "initiating", "initiation_unknown", "not_started"}
    if state in pre_session_states and item["upload_id"] is not None:
        raise ArtifactError("multipart upload_id must be null before initiation")
    if state not in pre_session_states and item["upload_id"] is None:
        raise ArtifactError("multipart upload_id is required in this state")
    if item["return_state"] not in {None, "initiated", "uploading", "collision_detected"}:
        raise ArtifactError("invalid multipart return_state")
    if state in {"aborting", "abort_unknown"} and item["return_state"] is None:
        raise ArtifactError("abort state requires return_state")
    if state not in {"aborting", "abort_unknown"} and item["return_state"] is not None:
        raise ArtifactError("return_state is only valid while aborting")
    acknowledged = item["acknowledged_parts"]
    if not isinstance(acknowledged, list):
        raise ArtifactError("acknowledged_parts must be an array")
    expected_number = 1
    acknowledged_numbers = set()
    acknowledged_bytes = 0
    for row in acknowledged:
        part = _object(row, "acknowledged part")
        _exact(part, ("part_number", "size", "sha256", "etag"), "acknowledged part")
        number = _integer(part["part_number"], 1, 10000, "part_number")
        if number != expected_number:
            raise ArtifactError("acknowledged parts must be ordered and contiguous")
        expected_number += 1
        acknowledged_numbers.add(number)
        size = _integer(part["size"], 1, part_size, "part size")
        offset = (number - 1) * part_size
        if offset >= source_size or size != min(part_size, source_size - offset):
            raise ArtifactError("acknowledged part does not match its source range")
        acknowledged_bytes += size
        _hash(part["sha256"], "part sha256")
        validate_provider_identifier(part["etag"], credentials)
    if acknowledged_bytes > source_size:
        raise ArtifactError("acknowledged parts exceed source size")
    in_flight = item["in_flight_part"]
    if in_flight is not None:
        part = _object(in_flight, "in_flight_part")
        _exact(part, ("part_number", "size", "sha256", "attempt"), "in_flight_part")
        number = _integer(part["part_number"], 1, 10000, "in_flight part number")
        if number in acknowledged_numbers:
            raise ArtifactError("in-flight part duplicates an acknowledged part")
        if number != expected_number:
            raise ArtifactError("in-flight part must be the next deterministic range")
        size = _integer(part["size"], 1, part_size, "in-flight part size")
        offset = (number - 1) * part_size
        if offset >= source_size or size != min(part_size, source_size - offset):
            raise ArtifactError("in-flight part does not match its source range")
        _hash(part["sha256"], "in-flight part sha256")
        _integer(part["attempt"], 1, maximum, "in-flight attempt")
    if state in pre_session_states and (acknowledged or in_flight is not None):
        raise ArtifactError("multipart parts must be empty before initiation")
    if state == "initiated" and (acknowledged or in_flight is not None):
        raise ArtifactError("initiated multipart must not contain parts")
    completion_states = {"completing", "completion_unknown", "collision_detected", "complete"}
    if state in completion_states and (
        in_flight is not None or acknowledged_bytes != source_size
    ):
        raise ArtifactError("multipart source must be fully acknowledged in this state")
    if state in {"aborting", "abort_unknown"}:
        return_state = item["return_state"]
        if return_state == "initiated" and (acknowledged or in_flight is not None):
            raise ArtifactError("multipart parts are inconsistent with return_state=initiated")
        if return_state == "collision_detected" and (
            in_flight is not None or acknowledged_bytes != source_size
        ):
            raise ArtifactError(
                "multipart parts are inconsistent with return_state=collision_detected"
            )
    if state == "aborted" and in_flight is not None:
        raise ArtifactError("aborted multipart must not contain an in-flight part")
    return item


def _validate_reference_out(value: Any) -> Dict[str, Any]:
    item = _object(value, "reference_out")
    _exact(item, ("path", "parent_snapshot", "final_snapshot"), "reference_out")
    if not isinstance(item["path"], str) or not os.path.isabs(item["path"]):
        raise ArtifactError("reference_out.path must be absolute")
    parent = _object(item["parent_snapshot"], "reference_out.parent_snapshot")
    _exact(parent, ("device", "inode", "owner", "mode"), "reference_out.parent_snapshot")
    _decimal(parent["device"], "parent device")
    _decimal(parent["inode"], "parent inode")
    _integer(parent["owner"], 0, (1 << 53) - 1, "parent owner")
    _integer(parent["mode"], 0, 0o7777, "parent mode")
    final = _object(item["final_snapshot"], "reference_out.final_snapshot")
    _exact(final, ("state", "identity", "sha256"), "reference_out.final_snapshot")
    if final["state"] == "absent":
        if final["identity"] is not None or final["sha256"] is not None:
            raise ArtifactError("absent reference snapshot must not contain identity or digest")
    elif final["state"] == "existing-reference":
        identity = _object(final["identity"], "reference identity")
        _exact(identity, ("device", "inode", "owner", "mode", "size", "mtime_ns", "ctime_ns"), "reference identity")
        for key in ("device", "inode", "mtime_ns", "ctime_ns"):
            _decimal(identity[key], f"reference identity {key}")
        for key in ("owner", "mode", "size"):
            _integer(identity[key], 0, (1 << 53) - 1, f"reference identity {key}")
        _hash(final["sha256"], "reference snapshot sha256")
    else:
        raise ArtifactError("invalid reference_out state")
    return item


def parse_checkpoint(value: Any, credentials: Iterable[str] = ()) -> Dict[str, Any]:
    item = _object(value, "Recovery Checkpoint")
    keys = (
        "schema_version", "checkpoint_id", "kind", "state", "operation_id", "created_at", "updated_at",
        "target_ref", "target_fingerprint", "object_reference_draft", "upload_plan", "collision", "source",
        "reference_out", "multipart", "delete_scope",
    )
    _exact(item, keys, "Recovery Checkpoint")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool):
        raise ArtifactError("Recovery Checkpoint schema_version must be 1")
    _uuid4(item["checkpoint_id"], "checkpoint_id")
    _uuid4(item["operation_id"], "operation_id")
    if item["checkpoint_id"] == item["operation_id"]:
        raise ArtifactError("checkpoint_id and operation_id must be independent")
    _timestamp(item["created_at"], "created_at")
    _timestamp(item["updated_at"], "updated_at")
    parse_reference(item["target_ref"], "target_ref")
    if not isinstance(item["target_fingerprint"], str) or not FINGERPRINT_RE.fullmatch(item["target_fingerprint"]):
        raise ArtifactError("invalid target_fingerprint")
    reference = _validate_reference(item["object_reference_draft"], credentials)
    if reference["target_ref"] != item["target_ref"] or reference["target_fingerprint"] != item["target_fingerprint"]:
        raise ArtifactError("Object Reference draft is inconsistent with checkpoint Target")
    states = {
        "put": {"prepared", "put_in_flight", "put_unknown", "complete", "not_started"},
        "multipart": {"prepared", "initiating", "initiation_unknown", "initiated", "uploading", "completing", "completion_unknown", "collision_detected", "aborting", "abort_unknown", "complete", "not_started", "aborted"},
        "delete": {"prepared", "delete_in_flight", "delete_unknown", "deleted", "not_deleted", "not_started"},
    }
    kind = item["kind"]
    if kind not in states or item["state"] not in states[kind]:
        raise ArtifactError("invalid checkpoint state for kind")
    if kind in {"put", "multipart"}:
        _validate_upload_plan(item["upload_plan"])
        _validate_collision(item["collision"], kind)
        source = _validate_source(item["source"])
        if item["delete_scope"] is not None:
            raise ArtifactError("upload checkpoint delete_scope must be null")
        if item["reference_out"] is not None:
            _validate_reference_out(item["reference_out"])
        if kind == "put" and item["multipart"] is not None:
            raise ArtifactError("put checkpoint multipart must be null")
        if kind == "multipart":
            _validate_multipart(item["multipart"], item["state"], source["size"], credentials)
    else:
        for field in ("upload_plan", "collision", "source", "reference_out", "multipart"):
            if item[field] is not None:
                raise ArtifactError(f"delete checkpoint {field} must be null")
        expected_scope = "exact-version" if reference["location"]["version_id"] is not None else "current-key"
        if item["delete_scope"] != expected_scope:
            raise ArtifactError("delete_scope is inconsistent with Object Reference")
    return item


@dataclass(frozen=True)
class ReferenceOutputSnapshot:
    value: Dict[str, Any]
    project_root: str
    config_home: str
    source_identity: Optional[Tuple[int, int]]


def _protected(path: str, project_root: str, config_home: str) -> bool:
    project_root = lexical_absolute(project_root)
    config_home = lexical_absolute(config_home)
    exact = {
        os.path.join(project_root, ".env"),
        os.path.join(project_root, ".env.local"),
        os.path.join(project_root, ".s3-upload", "config.json"),
    }
    trees = {
        os.path.join(project_root, ".s3-upload", "targets"),
        os.path.join(project_root, ".s3-upload", "checkpoints"),
        config_home,
    }
    return path in exact or any(path == root or path.startswith(root + os.sep) for root in trees)


def _opened_file_snapshot_at(parent_fd: int, name: str) -> Tuple[os.stat_result, bytes]:
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ArtifactError("existing reference path is unsafe")
        if info.st_nlink != 1:
            raise ArtifactError("existing reference path is a hardlink alias")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ArtifactError("existing reference path is unsafe")
        if info.st_size > 65536:
            raise ArtifactError("existing reference is too large")
        data = b""
        while len(data) <= 65536:
            chunk = os.read(descriptor, 65537 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > 65536:
            raise ArtifactError("existing reference is too large")
        return info, data
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ArtifactError("reference path is unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _opened_file_snapshot(path: str) -> Tuple[os.stat_result, bytes]:
    parent_fd = open_directory(os.path.dirname(path))
    try:
        return _opened_file_snapshot_at(parent_fd, os.path.basename(path))
    finally:
        os.close(parent_fd)


def _atomic_write_at(parent_fd: int, name: str, data: bytes, *, replace: bool) -> None:
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, 0o600)
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


def preflight_reference_output(path: str, *, project_root: str, config_home: str,
                               source_identity: Optional[Tuple[int, int]]) -> ReferenceOutputSnapshot:
    absolute = lexical_absolute(path)
    project_root = lexical_absolute(project_root)
    config_home = lexical_absolute(config_home)
    if _protected(absolute, project_root, config_home):
        raise ArtifactError("reference output is in a protected namespace")
    parent = os.path.dirname(absolute)
    try:
        parent_fd = open_directory(parent)
    except (OSError, FileSecurityError) as exc:
        raise ArtifactError("reference output parent is unsafe") from exc
    try:
        parent_info = os.fstat(parent_fd)
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        if parent_info.st_uid != os.geteuid() or parent_mode & 0o022:
            raise ArtifactError("reference output parent must be owned and not group/world-writable")
    finally:
        os.close(parent_fd)
    parent_snapshot = {
        "device": str(parent_info.st_dev),
        "inode": str(parent_info.st_ino),
        "owner": parent_info.st_uid,
        "mode": parent_mode,
    }
    try:
        info, data = _opened_file_snapshot(absolute)
    except FileNotFoundError:
        final_snapshot = {"state": "absent", "identity": None, "sha256": None}
    except ArtifactError:
        raise
    else:
        if source_identity is not None and (info.st_dev, info.st_ino) == source_identity:
            raise ArtifactError("reference output aliases the upload source")
        try:
            parse_object_reference(data.decode("utf-8"))
        except (UnicodeDecodeError, ArtifactError) as exc:
            raise ArtifactError("existing output is not a valid Object Reference") from exc
        final_snapshot = {
            "state": "existing-reference",
            "identity": {
                "device": str(info.st_dev), "inode": str(info.st_ino), "owner": info.st_uid,
                "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
                "mtime_ns": str(info.st_mtime_ns), "ctime_ns": str(info.st_ctime_ns),
            },
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    value = {"path": absolute, "parent_snapshot": parent_snapshot, "final_snapshot": final_snapshot}
    _validate_reference_out(value)
    return ReferenceOutputSnapshot(value, project_root, config_home, source_identity)


def _same_parent(snapshot: Dict[str, Any], info: os.stat_result) -> bool:
    return (
        str(info.st_dev) == snapshot["device"] and str(info.st_ino) == snapshot["inode"]
        and info.st_uid == snapshot["owner"] and stat.S_IMODE(info.st_mode) == snapshot["mode"]
    )


def write_reference_output(snapshot: ReferenceOutputSnapshot, reference: Dict[str, Any]) -> None:
    intended = serialize_object_reference(reference)
    value = snapshot.value
    path = value["path"]
    if _protected(path, snapshot.project_root, snapshot.config_home):
        raise ArtifactError("reference output is in a protected namespace")
    parent_fd = open_directory(os.path.dirname(path))
    try:
        if not _same_parent(value["parent_snapshot"], os.fstat(parent_fd)):
            raise ArtifactError("reference output parent changed")
        current = None
        try:
            info, current = _opened_file_snapshot_at(parent_fd, os.path.basename(path))
        except FileNotFoundError:
            info = None
        if current == intended:
            return
        original = value["final_snapshot"]
        if original["state"] == "absent":
            if info is not None:
                raise ArtifactError("reference output changed after preflight")
            try:
                _atomic_write_at(parent_fd, os.path.basename(path), intended, replace=False)
            except FileExistsError as exc:
                raise ArtifactError("reference output changed after preflight") from exc
            return
        if info is None:
            raise ArtifactError("reference output changed after preflight")
        identity = original["identity"]
        matches = (
            str(info.st_dev) == identity["device"] and str(info.st_ino) == identity["inode"]
            and info.st_uid == identity["owner"] and stat.S_IMODE(info.st_mode) == identity["mode"]
            and info.st_size == identity["size"] and str(info.st_mtime_ns) == identity["mtime_ns"]
            and str(info.st_ctime_ns) == identity["ctime_ns"]
            and hashlib.sha256(current or b"").hexdigest() == original["sha256"]
        )
        if not matches:
            raise ArtifactError("reference output changed after preflight")
        _atomic_write_at(parent_fd, os.path.basename(path), intended, replace=True)
    finally:
        os.close(parent_fd)


class CheckpointStore:
    GUARD = b"*\n!.gitignore\n"

    def __init__(self, project_root: str):
        self.project_root = lexical_absolute(project_root)
        self.skill_dir = os.path.join(self.project_root, ".s3-upload")
        self.directory = os.path.join(self.skill_dir, "checkpoints")

    def _prepare(self) -> None:
        try:
            ensure_directory(self.skill_dir, mode=0o700, exact_mode=False)
            ensure_directory(self.directory, mode=0o700, exact_mode=True)
        except (OSError, FileSecurityError) as exc:
            raise ArtifactError("checkpoint directory is unsafe") from exc
        guard = os.path.join(self.directory, ".gitignore")
        try:
            text = read_regular_file(guard, max_bytes=64, secret=True, missing_ok=True)
        except (OSError, FileSecurityError) as exc:
            raise ArtifactError("checkpoint Git ignore guard is unsafe") from exc
        if text is None:
            try:
                atomic_write(guard, self.GUARD, mode=0o600, replace=False)
            except FileExistsError:
                text = read_regular_file(guard, max_bytes=64, secret=True, missing_ok=False)
        if text is not None and text.encode("utf-8") != self.GUARD:
            raise ArtifactError("checkpoint Git ignore guard has unexpected content")

    def _path(self, checkpoint_id: str) -> str:
        _uuid4(checkpoint_id, "checkpoint_id")
        return os.path.join(self.directory, checkpoint_id + ".json")

    def create(self, checkpoint: Dict[str, Any]) -> None:
        value = parse_checkpoint(checkpoint)
        self._prepare()
        try:
            atomic_write(self._path(value["checkpoint_id"]), canonicalize(value), mode=0o600, replace=False)
        except FileExistsError as exc:
            raise ArtifactError("checkpoint already exists") from exc

    def replace(self, checkpoint: Dict[str, Any]) -> None:
        value = parse_checkpoint(checkpoint)
        self._prepare()
        path = self._path(value["checkpoint_id"])
        if not os.path.exists(path):
            raise ArtifactError("checkpoint does not exist")
        atomic_write(path, canonicalize(value), mode=0o600, replace=True)

    def load(self, checkpoint_id: str, credentials: Iterable[str] = ()) -> Dict[str, Any]:
        self._prepare()
        path = self._path(checkpoint_id)
        try:
            text = read_regular_file(path, max_bytes=1048576, secret=True, missing_ok=False)
            return parse_checkpoint(loads(text or ""), credentials)
        except (OSError, FileSecurityError, StrictJSONError) as exc:
            raise ArtifactError("checkpoint is unavailable or corrupt") from exc

    def remove(self, checkpoint_id: str) -> None:
        self._prepare()
        parent_fd = open_directory(self.directory)
        try:
            os.unlink(os.path.basename(self._path(checkpoint_id)), dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    @contextmanager
    def lock(self, checkpoint_id: str) -> Iterator[None]:
        self._prepare()
        lock_path = os.path.join(self.directory, checkpoint_id + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArtifactError("checkpoint_locked") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
