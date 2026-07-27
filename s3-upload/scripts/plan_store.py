from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import secrets
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple

from delivery_schema import (
    DeliverySchemaError, artifact_digest, body_of, build_typed, parse_typed,
    serialize_artifact,
)
from safe_io import (
    FileSecurityError, atomic_write, ensure_directory, lexical_absolute,
    open_directory, read_regular_file,
)
from strict_json import StrictJSONError, canonicalize, loads


class PlanStoreError(ValueError):
    pass


PLAN_DOMAIN = "s3-upload/plan/v1"
TOKEN_DOMAIN = "s3-upload/plan-token/v1"
PLAN_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
LOCK_NAME_RE = re.compile(r"[0-9a-z-]{1,80}\Z")
RECORD_FIELDS = ("acknowledged", "created_at", "plan", "spool", "token_digest")
OPERATION_FIELDS = ("checkpoint_id", "operation_id", "recovery_id", "result_out", "root_recovery_id")
TOMBSTONE_FIELDS = ("acknowledged", "plan_id", "result_hash", "token_digest")
CHUNK = 1024 * 1024
MAX_RECORD_BYTES = 1048576


def new_plan_id() -> str:
    return uuid.uuid4().hex


def plan_hash(body: Dict[str, Any]) -> str:
    material = {
        key: value for key, value in body.items()
        if key not in {"plan_hash", "plan_token"}
    }
    return artifact_digest(PLAN_DOMAIN, material)


def token_digest(plan_id: str, token: str) -> str:
    return artifact_digest(TOKEN_DOMAIN, {"plan_id": plan_id, "token": token})


def build_plan_body(*, dry_run_plan: Dict[str, Any], source_snapshot: Any,
                    target_contract: Dict[str, Any], target_contract_hash: str,
                    caller: str, executable_path: str, cwd: str, state_root: str,
                    recovery_out: str, result_out: str, plan_id: str) -> Dict[str, Any]:
    body = {
        "access": dict(dry_run_plan["access"]),
        "blocking_reasons": list(dry_run_plan["blocking_reasons"]),
        "caller": caller,
        "collision": dict(dry_run_plan["collision"]),
        "contract_key": dict(dry_run_plan["contract_key"]),
        "cwd": lexical_absolute(cwd),
        "executable": bool(dry_run_plan["executable"]),
        "executable_path": executable_path,
        "object_headers": dict(dry_run_plan["headers"]),
        "object_key": dry_run_plan["object_key"],
        "plan_hash": None,
        "plan_id": plan_id,
        "plan_token": None,
        "recovery_out": lexical_absolute(recovery_out),
        "remote_operations": list(dry_run_plan["remote_operations"]),
        "required_capabilities": [dict(entry) for entry in dry_run_plan["capabilities"]],
        "result_out": lexical_absolute(result_out),
        "retention": dict(dry_run_plan["retention"]),
        "source": dict(source_snapshot.as_checkpoint()),
        "state_root": lexical_absolute(state_root),
        "target_contract": deepcopy(target_contract),
        "target_contract_hash": target_contract_hash,
        "target_ref": target_contract["target_ref"],
        "upload_mode": dry_run_plan["upload_mode"],
    }
    body["plan_hash"] = plan_hash(body)
    return body


@dataclass(frozen=True)
class IssuedPlan:
    plan_id: str
    token: Optional[str]
    artifact: Dict[str, Any]


@dataclass(frozen=True)
class ConsumedPlan:
    state: str
    plan_id: str
    record: Optional[Dict[str, Any]]
    tombstone: Optional[Dict[str, Any]]


def _exact(value: Any, fields: Tuple[str, ...], label: str,
           *, str_fields: Tuple[str, ...] = ()) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PlanStoreError(f"{label} does not have the exact field set")
    # Value-type guard (shape hardening): every field named in str_fields must
    # be a string. Some of them have downstream consumers that require it
    # (path construction, secrets.compare_digest); the rest are pinned so a
    # polluted on-disk record surfaces as this store's error contract instead
    # of a bare TypeError later.
    for key in str_fields:
        if not isinstance(value[key], str):
            raise PlanStoreError(f"{label} field {key} must be a string")
    return value


class PlanStore:
    GUARD = b"*\n!.gitignore\n"

    def __init__(self, state_root: str):
        self.state_root = lexical_absolute(state_root)
        self.plans_dir = os.path.join(self.state_root, "plans")
        self.locks_dir = os.path.join(self.state_root, "locks")

    def _prepare(self) -> None:
        try:
            ensure_directory(self.state_root, mode=0o700, exact_mode=False)
            ensure_directory(self.plans_dir, mode=0o700, exact_mode=True)
            ensure_directory(self.locks_dir, mode=0o700, exact_mode=True)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan store directory is unsafe") from exc
        guard = os.path.join(self.plans_dir, ".gitignore")
        try:
            text = read_regular_file(guard, max_bytes=64, secret=True, missing_ok=True)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan store Git ignore guard is unsafe") from exc
        if text is None:
            try:
                atomic_write(guard, self.GUARD, mode=0o600, replace=False)
            except FileExistsError:
                text = read_regular_file(guard, max_bytes=64, secret=True, missing_ok=False)
            except (OSError, FileSecurityError) as exc:
                raise PlanStoreError("plan store Git ignore guard could not be written durably") from exc
        if text is not None and text.encode("utf-8") != self.GUARD:
            raise PlanStoreError("plan store Git ignore guard has unexpected content")

    def _plan_dir(self, plan_id: str) -> str:
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise PlanStoreError("invalid plan id")
        return os.path.join(self.plans_dir, plan_id)

    def _spool(self, parent_fd: int, source_path: str, soft_max_bytes: int) -> Tuple[int, str]:
        temporary = ".spool." + secrets.token_hex(8) + ".tmp"
        digest = hashlib.sha256()
        total = 0
        try:
            source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise PlanStoreError("plan source is unreadable") from exc
        descriptor = None
        published = False
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise PlanStoreError("plan source is not a regular file")
            if before.st_size > soft_max_bytes:
                raise PlanStoreError("plan source exceeds the soft size limit")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            while True:
                chunk = os.read(source_fd, CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > soft_max_bytes:
                    raise PlanStoreError("plan source exceeds the soft size limit")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            after = os.fstat(source_fd)
            if (
                after.st_size != total
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise PlanStoreError("plan source changed while spooling")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(temporary, "spool", src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent_fd)
            published = True
            os.fsync(parent_fd)
            return total, digest.hexdigest()
        except OSError as exc:
            raise PlanStoreError("plan spool could not be written durably") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(source_fd)

    def issue(self, body: Dict[str, Any], *, source_path: str, soft_max_bytes: int,
              created_at: str) -> IssuedPlan:
        plan_id = body["plan_id"]
        stored = dict(body)
        stored["plan_token"] = None
        stored["plan_hash"] = plan_hash(stored)
        if stored["executable"] is not True:
            return IssuedPlan(plan_id, None, build_typed("s3-upload.plan", stored))
        self._prepare()
        directory = self._plan_dir(plan_id)
        try:
            ensure_directory(directory, mode=0o700, exact_mode=True)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan directory is unsafe") from exc
        parent_fd = open_directory(directory)
        try:
            size, digest = self._spool(parent_fd, source_path, soft_max_bytes)
        finally:
            os.close(parent_fd)
        if size != stored["source"]["size"] or digest != stored["source"]["sha256"]:
            raise PlanStoreError("spooled bytes do not match the planned source identity")
        token = plan_id + "." + secrets.token_urlsafe(32)
        record = {
            "acknowledged": False,
            "created_at": created_at,
            "plan": build_typed("s3-upload.plan", stored),
            "spool": {"sha256": digest, "size": size},
            "token_digest": token_digest(plan_id, token),
        }
        _exact(record, RECORD_FIELDS, "plan record")
        try:
            atomic_write(os.path.join(directory, "record.json"), canonicalize(record),
                         mode=0o600, replace=False)
        except (FileExistsError, OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan record could not be written durably") from exc
        handed = dict(stored)
        handed["plan_token"] = token
        return IssuedPlan(plan_id, token, build_typed("s3-upload.plan", handed))

    def _read_json(self, plan_id: str, name: str, fields: Tuple[str, ...],
                   label: str,
                   str_fields: Tuple[str, ...] = ()) -> Optional[Dict[str, Any]]:
        path = os.path.join(self._plan_dir(plan_id), name)
        try:
            text = read_regular_file(path, max_bytes=MAX_RECORD_BYTES, secret=True,
                                     missing_ok=True)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError(f"{label} is unsafe") from exc
        if text is None:
            return None
        try:
            value = loads(text)
        except StrictJSONError as exc:
            raise PlanStoreError(f"{label} is corrupt") from exc
        return _exact(value, fields, label, str_fields=str_fields)

    def load_record(self, plan_id: str) -> Dict[str, Any]:
        record = self._read_json(plan_id, "record.json", RECORD_FIELDS, "plan record",
                                 str_fields=("created_at", "token_digest"))
        if record is None:
            raise PlanStoreError("plan record is unavailable")
        try:
            parse_typed(serialize_artifact(record["plan"]).decode("utf-8"),
                        expected_type="s3-upload.plan")
        except DeliverySchemaError as exc:
            raise PlanStoreError("plan record contains an invalid plan artifact") from exc
        body = body_of(record["plan"])
        if body["plan_hash"] != plan_hash(body):
            raise PlanStoreError("plan record plan_hash does not match its bound facts")
        return record

    def load_tombstone(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return self._read_json(plan_id, "tombstone.json", TOMBSTONE_FIELDS,
                               "plan tombstone",
                               str_fields=("plan_id", "token_digest"))

    def operation_record(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return self._read_json(plan_id, "operation.json", OPERATION_FIELDS,
                               "plan operation record", str_fields=OPERATION_FIELDS)

    def write_operation_record(self, plan_id: str, value: Dict[str, Any]) -> None:
        _exact(value, OPERATION_FIELDS, "plan operation record",
               str_fields=OPERATION_FIELDS)
        try:
            atomic_write(os.path.join(self._plan_dir(plan_id), "operation.json"),
                         canonicalize(value), mode=0o600, replace=False)
        except FileExistsError as exc:
            existing = self.operation_record(plan_id)
            if existing != value:
                raise PlanStoreError("plan operation record already describes another operation") from exc
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan operation record could not be written durably") from exc

    def discard_operation_record(self, plan_id: str) -> None:
        directory = self._plan_dir(plan_id)
        try:
            parent_fd = open_directory(directory)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan directory is unavailable") from exc
        try:
            try:
                os.unlink("operation.json", dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.fsync(parent_fd)
        except OSError as exc:
            raise PlanStoreError("plan operation record could not be discarded") from exc
        finally:
            os.close(parent_fd)

    def consume(self, token: str, *, caller: str, executable_path: str, cwd: str,
                state_root: str) -> ConsumedPlan:
        if not isinstance(token, str) or "." not in token:
            raise PlanStoreError("invalid plan token")
        plan_id = token.split(".", 1)[0]
        self._plan_dir(plan_id)
        tombstone = self.load_tombstone(plan_id)
        if tombstone is not None:
            if not secrets.compare_digest(tombstone["token_digest"],
                                          token_digest(plan_id, token)):
                raise PlanStoreError("invalid plan token")
            return ConsumedPlan("acknowledged", plan_id, None, tombstone)
        try:
            record = self.load_record(plan_id)
        except PlanStoreError:
            # A concurrent acknowledgement writes tombstone.json first and only
            # then removes record.json, so "no tombstone yet, record already
            # gone" is a real mid-cleanup state, not a bad token. Re-reading the
            # tombstone resolves it: a tombstone that has appeared since the
            # probe above is this plan's durable settlement and answers the
            # token exactly as the fast path would have. If the re-read finds
            # nothing, the original refusal stands; a re-read that itself
            # fails propagates as its own PlanStoreError (with the original
            # refusal in its __context__).
            settled = self.load_tombstone(plan_id)
            if settled is None:
                raise
            if not secrets.compare_digest(settled["token_digest"],
                                          token_digest(plan_id, token)):
                raise PlanStoreError("invalid plan token") from None
            return ConsumedPlan("acknowledged", plan_id, None, settled)
        if not secrets.compare_digest(record["token_digest"], token_digest(plan_id, token)):
            raise PlanStoreError("invalid plan token")
        body = body_of(record["plan"])
        if body["state_root"] != self.state_root or lexical_absolute(state_root) != self.state_root:
            raise PlanStoreError("plan token replayed against another state root")
        if body["cwd"] != lexical_absolute(cwd):
            raise PlanStoreError("plan token replayed from another project cwd")
        if body["executable_path"] != executable_path:
            raise PlanStoreError("plan token replayed by another executable")
        if body["caller"] != caller:
            raise PlanStoreError("plan token replayed by another caller")
        return ConsumedPlan("active", plan_id, record, None)

    def spool_bytes(self, plan_id: str, *, expected_size: int, expected_sha256: str) -> bytes:
        directory = self._plan_dir(plan_id)
        try:
            parent_fd = open_directory(directory)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan directory is unavailable") from exc
        descriptor = None
        try:
            descriptor = os.open("spool", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=parent_fd)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise PlanStoreError("spool is unsafe")
            digest = hashlib.sha256()
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise PlanStoreError("spool exceeds the planned size")
                digest.update(chunk)
                chunks.append(chunk)
            if total != expected_size or digest.hexdigest() != expected_sha256:
                raise PlanStoreError("spool does not match the planned source identity")
            return b"".join(chunks)
        except (OSError, FileSecurityError) as exc:
            if isinstance(exc, PlanStoreError):
                raise
            raise PlanStoreError("spool is unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    def _finish(self, plan_id: str, *, acknowledged: bool, result_hash: Optional[str]) -> Dict[str, Any]:
        directory = self._plan_dir(plan_id)
        existing = self.load_tombstone(plan_id)
        if existing is None:
            try:
                record = self.load_record(plan_id)
            except PlanStoreError:
                # A concurrent _finish of the same plan writes tombstone.json
                # and then removes record.json, so a record that vanished after
                # the tombstone probe above can mean the other side already
                # settled this plan. Only a tombstone that records the same
                # settlement may stand in for the write this call was about to
                # make; anything else keeps the refusal. (Same durable-state
                # re-read pattern as write_operation_record.)
                settled = self.load_tombstone(plan_id)
                if settled is None:
                    raise
                if (settled["acknowledged"] is not acknowledged
                        or settled["result_hash"] != result_hash):
                    # No production path currently reaches this refusal:
                    # invalidate() has no production caller, and concurrent
                    # acks of one plan always carry the same settlement.
                    # Kept as pure defence in depth.
                    raise PlanStoreError(
                        "plan tombstone already records another settlement"
                    )
                tombstone = settled
            else:
                tombstone = {
                    "acknowledged": acknowledged,
                    "plan_id": plan_id,
                    "result_hash": result_hash,
                    "token_digest": record["token_digest"],
                }
                _exact(tombstone, TOMBSTONE_FIELDS, "plan tombstone")
                try:
                    atomic_write(os.path.join(directory, "tombstone.json"),
                                 canonicalize(tombstone), mode=0o600, replace=False)
                except FileExistsError as exc:
                    # tombstone.json is create-once, so losing this write means
                    # another _finish of the same plan got there first. Read the
                    # durable bytes back and compare: an identical settlement is
                    # this call's outcome already on disk (finish the idempotent
                    # cleanup below); a different one is refused. A re-read that
                    # itself fails propagates as its own PlanStoreError.
                    settled = self.load_tombstone(plan_id)
                    if settled != tombstone:
                        # No production path currently reaches this refusal:
                        # invalidate() has no production caller, and concurrent
                        # acks of one plan always carry the same settlement.
                        # Kept as pure defence in depth.
                        raise PlanStoreError(
                            "plan tombstone already records another settlement"
                        ) from exc
                    tombstone = settled
                except (OSError, FileSecurityError) as exc:
                    raise PlanStoreError("plan tombstone could not be written durably") from exc
        else:
            tombstone = existing
        try:
            parent_fd = open_directory(directory)
        except (OSError, FileSecurityError) as exc:
            raise PlanStoreError("plan directory is unavailable") from exc
        try:
            for name in ("spool", "record.json"):
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.fsync(parent_fd)
        except OSError as exc:
            raise PlanStoreError("plan directory cleanup failed") from exc
        finally:
            os.close(parent_fd)
        return tombstone

    def acknowledge(self, plan_id: str, *, result_hash: str) -> Dict[str, Any]:
        return self._finish(plan_id, acknowledged=True, result_hash=result_hash)

    def invalidate(self, plan_id: str) -> Dict[str, Any]:
        return self._finish(plan_id, acknowledged=False, result_hash=None)

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        if not isinstance(name, str) or not LOCK_NAME_RE.fullmatch(name):
            raise PlanStoreError("invalid lock name")
        self._prepare()
        path = os.path.join(self.locks_dir, name + ".lock")
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PlanStoreError("plan store lock path is unsafe") from exc
            raise PlanStoreError("plan store lock path could not be opened") from exc
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as exc:
                raise PlanStoreError("plan store lock could not be secured") from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlanStoreError("plan store lock is held") from exc
            except OSError as exc:
                raise PlanStoreError("plan store lock could not be acquired") from exc
            yield
        finally:
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            finally:
                os.close(descriptor)
