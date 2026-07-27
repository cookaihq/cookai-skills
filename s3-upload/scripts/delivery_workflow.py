from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from artifacts import ArtifactError, CheckpointStore
from delivery_records import build_recovery_descriptor, build_result, result_hash
from delivery_schema import (
    DeliverySchemaError, artifact_digest, body_of, parse_typed, serialize_artifact,
)
from handoff_io import HandoffError, HandoffTarget, _read_artifact, commit, preflight
from operations import DefinitiveNoWrite, execute_single_put
from plan_store import PlanStore, PlanStoreError
from planning import PlanError, derive_contract_key, registry_for_target
from safe_io import lexical_absolute
from source_file import SourceError, SourceSnapshot, VerifiedSource
from target_contract import contract_hash, contract_snapshot


class WorkflowError(RuntimeError):
    pass


class AlreadyHandedOff(WorkflowError):
    pass


class TransportSealed(RuntimeError):
    pass


BOUNDARIES: Tuple[str, ...] = (
    "plan_durable",
    "revalidated",
    "checkpoint_durable",
    "before_recovery_fsync",
    "after_recovery_fsync",
    "before_request",
    "after_request",
    "before_result_fsync",
    "after_result_fsync",
    "before_stdout",
    "before_ack",
    "before_cleanup",
)

STATE_BY_STATUS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "ok": ("terminal_unacknowledged", ()),
    "ambiguous": ("in_flight_unknown", ()),
    "partial_success": ("in_flight_unknown", ()),
    "collision": ("blocked", ("capability_missing",)),
}


class TransportGate:
    def __init__(self, transport: Callable[..., Any]):
        self._transport = transport
        self._armed = False
        self.calls = 0
        self.sealed_violations = 0

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        self._armed = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self._armed:
            self.sealed_violations += 1
            raise TransportSealed("no remote request may precede a durable recovery handoff")
        self.calls += 1
        return self._transport(*args, **kwargs)


class SpooledSource:
    def __init__(self, snapshot: SourceSnapshot, data: bytes):
        self.snapshot = snapshot
        self._data = data

    def __enter__(self) -> "SpooledSource":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._data = b""

    def single_put_bytes(self) -> bytes:
        return self._data


@dataclass(frozen=True)
class PublishOutcome:
    result: Dict[str, Any]
    recovery: Optional[Dict[str, Any]]
    transport_calls: int
    checkpoint_id: Optional[str]


def _no_hook(name: str) -> None:
    return None


def _derived_id(domain: str, plan_id: str, plan_hash: str, action: str) -> str:
    digest = artifact_digest(
        domain, {"action": action, "plan_hash": plan_hash, "plan_id": plan_id}
    )
    return digest.split(":", 1)[1][:32]


def _armed(store: PlanStore, plan_id: str) -> bool:
    try:
        return store.operation_record(plan_id) is not None
    except PlanStoreError:
        return True


def _surviving_checkpoint(project_root: str,
                          checkpoint_id: Optional[str]) -> Optional[str]:
    if checkpoint_id is None:
        return None
    try:
        os.lstat(CheckpointStore(project_root)._path(checkpoint_id))
    except FileNotFoundError:
        return None
    except (ArtifactError, OSError):
        return checkpoint_id
    return checkpoint_id


def _drop_checkpoint(store: PlanStore, plan_id: str, project_root: str,
                     checkpoint_id: str) -> bool:
    try:
        record = store.operation_record(plan_id)
    except PlanStoreError:
        return False
    if record is not None and record["checkpoint_id"] == checkpoint_id:
        return False
    try:
        CheckpointStore(project_root).remove(checkpoint_id)
    except (ArtifactError, FileNotFoundError, OSError):
        return False
    return True


def _read_handoff(target: Optional[HandoffTarget]) -> bytes:
    if target is None:
        return b""
    try:
        raw = _read_artifact(target)
    except (HandoffError, OSError):
        return b""
    return b"" if raw is None else raw


def _durable_result(target: Optional[HandoffTarget], *, plan: Dict[str, Any],
                    operation_id: str,
                    recovery_id: str) -> Optional[Dict[str, Any]]:
    raw = _read_handoff(target)
    if not raw:
        return None
    try:
        item = parse_typed(raw.decode("utf-8"), expected_type="s3-upload.result")
    except (DeliverySchemaError, UnicodeDecodeError):
        return None
    body = body_of(item)
    expected = {
        "operation": "publish",
        "operation_id": operation_id,
        "plan_hash": plan["plan_hash"],
        "plan_id": plan["plan_id"],
        "recovery_id": recovery_id,
        "target_contract_hash": plan["target_contract_hash"],
    }
    if body["result_hash"] != result_hash(body):
        return None
    return item if all(body[key] == value for key, value in expected.items()) else None


def _durable_recovery(target: Optional[HandoffTarget],
                      recovery_id: str) -> Optional[Dict[str, Any]]:
    raw = _read_handoff(target)
    if not raw:
        return None
    try:
        item = parse_typed(raw.decode("utf-8"),
                           expected_type="s3-upload.recovery-descriptor")
    except (DeliverySchemaError, UnicodeDecodeError):
        return None
    return item if body_of(item)["recovery_id"] == recovery_id else None


def _source_identity(plan: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    device = plan["source"]["device"]
    inode = plan["source"]["inode"]
    if device is None or inode is None:
        return None
    return (int(device), int(inode))


def _plan_snapshot(plan: Dict[str, Any]) -> SourceSnapshot:
    item = plan["source"]
    return SourceSnapshot(
        item["path"],
        item["size"],
        int(item["mtime_ns"]),
        None if item["device"] is None else int(item["device"]),
        None if item["inode"] is None else int(item["inode"]),
        item["sha256"],
    )


def _execution_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "executable": True,
        "upload_mode": plan["upload_mode"],
        "collision": plan["collision"],
        "source": {"path": plan["source"]["path"], "size": plan["source"]["size"]},
        "object_key": plan["object_key"],
        "reference_out": None,
        "headers": plan["object_headers"],
        "access": plan["access"],
    }


def publish(*, resolved, store: PlanStore, token: str, transport: Callable[..., Any],
            project_root: str, config_home: str, caller: str, executable_path: str,
            cwd: str, now=None, on_boundary: Optional[Callable[[str], None]] = None,
            uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
            root_recovery_id: Optional[str] = None,
            predecessor_operation_id: Optional[str] = None,
            predecessor_result_hash: Optional[str] = None) -> PublishOutcome:
    gate = TransportGate(transport)
    try:
        return _publish(
            resolved=resolved,
            store=store,
            token=token,
            gate=gate,
            project_root=project_root,
            config_home=config_home,
            caller=caller,
            executable_path=executable_path,
            cwd=cwd,
            now=now,
            on_boundary=on_boundary,
            uuid_factory=uuid_factory,
            root_recovery_id=root_recovery_id,
            predecessor_operation_id=predecessor_operation_id,
            predecessor_result_hash=predecessor_result_hash,
        )
    finally:
        if gate.sealed_violations:
            raise TransportSealed(
                "a remote request preceded the durable recovery handoff"
            )


def _publish(*, resolved, store: PlanStore, token: str, gate: TransportGate,
             project_root: str, config_home: str, caller: str, executable_path: str,
             cwd: str, now=None, on_boundary: Optional[Callable[[str], None]] = None,
             uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
             root_recovery_id: Optional[str] = None,
             predecessor_operation_id: Optional[str] = None,
             predecessor_result_hash: Optional[str] = None) -> PublishOutcome:
    hook = on_boundary or _no_hook
    operation_id = uuid_factory().hex
    recovery_id = uuid_factory().hex
    root = root_recovery_id or recovery_id
    project_root = lexical_absolute(project_root)

    targets: Dict[str, Optional[HandoffTarget]] = {"recovery": None, "result": None}

    def stop(reasons, *, state="blocked", plan=None, contract=None, checkpoint_id=None):
        if plan is not None:
            durable = _durable_result(
                targets["result"], plan=plan, operation_id=operation_id,
                recovery_id=recovery_id,
            )
            if durable is not None:
                return PublishOutcome(
                    durable, _durable_recovery(targets["recovery"], recovery_id),
                    gate.calls, checkpoint_id,
                )
        recovery = _durable_recovery(targets["recovery"], recovery_id)
        return PublishOutcome(
            result=build_result(
                operation="publish",
                operation_id=operation_id,
                plan_id=None if plan is None else plan["plan_id"],
                plan_hash=None if plan is None else plan["plan_hash"],
                target_contract_hash=contract,
                recovery_id=recovery_id,
                root_recovery_id=root,
                state=state,
                capabilities_ok=state in {"known_not_applied", "not_started"},
                blocking_reasons=reasons,
                predecessor_operation_id=predecessor_operation_id,
                predecessor_result_hash=predecessor_result_hash,
            ),
            recovery=recovery,
            transport_calls=gate.calls,
            checkpoint_id=checkpoint_id,
        )

    try:
        consumed = store.consume(token, caller=caller, executable_path=executable_path,
                                 cwd=cwd, state_root=store.state_root)
    except PlanStoreError:
        return stop(["token_invalid"])
    if consumed.state == "acknowledged":
        return stop(["already_acknowledged"], state="terminal_acknowledged")
    plan = body_of(consumed.record["plan"])
    plan_id = plan["plan_id"]
    operation_id = _derived_id(
        "s3-upload/operation-id/v1", plan_id, plan["plan_hash"], "publish"
    )
    recovery_id = _derived_id(
        "s3-upload/recovery-id/v1", plan_id, plan["plan_hash"], "publish"
    )
    root = root_recovery_id or recovery_id
    target_lock = "target-" + plan["target_contract_hash"].split(":", 1)[1]
    with store.lock("project"), store.lock(target_lock), store.lock("plan-" + plan_id):
        identity = _source_identity(plan)
        if identity is not None:
            try:
                targets["result"] = preflight(
                    plan["result_out"], project_root=project_root,
                    config_home=config_home, state_root=store.state_root,
                    source_identity=identity,
                )
            except (HandoffError, OSError):
                targets["result"] = None
        if plan["executable"] is not True:
            return stop(["plan_not_executable"], plan=plan)
        if plan["upload_mode"] != "single-put":
            return stop(["capability_missing"], plan=plan)
        try:
            key = derive_contract_key(resolved.target)
            snapshot = contract_snapshot(
                target_ref=resolved.ref,
                config_scope=resolved.ref.scope,
                project_root=project_root,
                target=resolved.target,
                contract_key=key,
                registry=registry_for_target(resolved.target, key),
            )
        except PlanError:
            return stop(["capability_missing"], plan=plan)
        digest = contract_hash(snapshot)
        if digest != plan["target_contract_hash"] or resolved.ref.text != plan["target_ref"]:
            return stop(["target_contract_drift"], plan=plan, contract=digest)
        if plan["caller"] != caller:
            return stop(["caller_drift"], plan=plan, contract=digest)
        if plan["executable_path"] != executable_path:
            return stop(["executable_drift"], plan=plan, contract=digest)
        if plan["cwd"] != lexical_absolute(cwd):
            return stop(["cwd_drift"], plan=plan, contract=digest)
        if plan["state_root"] != store.state_root:
            return stop(["state_root_drift"], plan=plan, contract=digest)
        try:
            with VerifiedSource.open(plan["source"]["path"],
                                     soft_max_bytes=plan["source"]["size"]) as live:
                current = live.snapshot
        except SourceError:
            return stop(["source_drift"], plan=plan, contract=digest)
        if (
            current.size != plan["source"]["size"]
            or current.sha256 != plan["source"]["sha256"]
            or str(current.device) != plan["source"]["device"]
            or str(current.inode) != plan["source"]["inode"]
        ):
            return stop(["source_drift"], plan=plan, contract=digest)
        if identity is None:
            return stop(["source_drift"], plan=plan, contract=digest)
        try:
            recovery_target = preflight(
                plan["recovery_out"], project_root=project_root, config_home=config_home,
                state_root=store.state_root, source_identity=identity,
            )
            result_target = preflight(
                plan["result_out"], project_root=project_root, config_home=config_home,
                state_root=store.state_root, source_identity=identity,
            )
        except HandoffError:
            return stop(["handoff_unsafe"], plan=plan, contract=digest)
        targets["recovery"] = recovery_target
        targets["result"] = result_target
        hook("revalidated")
        try:
            frozen = store.spool_bytes(
                plan_id,
                expected_size=plan["source"]["size"],
                expected_sha256=plan["source"]["sha256"],
            )
        except PlanStoreError:
            return stop(["source_drift"], plan=plan, contract=digest)
        handoff: Dict[str, Any] = {"checkpoint_id": None}

        def surviving():
            return _surviving_checkpoint(project_root, handoff["checkpoint_id"])

        def rollback_unarmed_checkpoint():
            checkpoint_id = handoff["checkpoint_id"]
            if checkpoint_id is not None and _drop_checkpoint(
                store, plan_id, project_root, checkpoint_id
            ):
                handoff["checkpoint_id"] = None
            return surviving()

        def compose(state, reasons, capabilities_ok):
            return build_result(
                operation="publish",
                operation_id=operation_id,
                plan_id=plan_id,
                plan_hash=plan["plan_hash"],
                target_contract_hash=digest,
                recovery_id=recovery_id,
                root_recovery_id=root,
                state=state,
                capabilities_ok=capabilities_ok,
                blocking_reasons=list(reasons),
                predecessor_operation_id=predecessor_operation_id,
                predecessor_result_hash=predecessor_result_hash,
            )

        def emit(state, reasons=(), *, capabilities_ok=True, checkpoint_id=None):
            durable = _durable_result(
                result_target, plan=plan, operation_id=operation_id,
                recovery_id=recovery_id,
            )
            if durable is not None:
                return PublishOutcome(
                    durable, _durable_recovery(recovery_target, recovery_id),
                    gate.calls, checkpoint_id,
                )
            record = compose(state, reasons, capabilities_ok)
            hook("before_result_fsync")
            try:
                commit(result_target, serialize_artifact(record))
            except HandoffError:
                record = compose(
                    state, list(reasons) + ["handoff_write_failed"], capabilities_ok
                )
                return PublishOutcome(
                    record, _durable_recovery(recovery_target, recovery_id),
                    gate.calls, checkpoint_id,
                )
            hook("after_result_fsync")
            hook("before_stdout")
            return PublishOutcome(
                record, _durable_recovery(recovery_target, recovery_id),
                gate.calls, checkpoint_id,
            )

        def checkpoint_notice(checkpoint_id: str) -> None:
            hook("checkpoint_durable")
            handoff["checkpoint_id"] = checkpoint_id
            descriptor = build_recovery_descriptor(
                recovery_id=recovery_id,
                root_recovery_id=root,
                operation="reconcile",
                operation_id=operation_id,
                plan_id=plan_id,
                plan_hash=plan["plan_hash"],
                target_contract_hash=digest,
                object_key=plan["object_key"],
                result_out=plan["result_out"],
                state="in_flight_unknown",
                capabilities_ok=True,
            )
            try:
                handed_off = store.operation_record(plan_id) is not None
            except PlanStoreError:
                handed_off = True
            if handed_off:
                if _drop_checkpoint(store, plan_id, project_root, checkpoint_id):
                    handoff["checkpoint_id"] = None
                raise AlreadyHandedOff("this plan already began a durable handoff")
            value = {
                "checkpoint_id": checkpoint_id,
                "operation_id": operation_id,
                "recovery_id": recovery_id,
                "result_out": plan["result_out"],
                "root_recovery_id": root,
            }
            try:
                store.write_operation_record(plan_id, value)
            except PlanStoreError as exc:
                try:
                    surviving = store.operation_record(plan_id)
                except PlanStoreError:
                    raise AlreadyHandedOff(
                        "the operation record could not be read back"
                    ) from exc
                if surviving is not None:
                    if surviving != value:
                        raise AlreadyHandedOff(
                            "another operation record survived a failed write"
                        ) from exc
                    try:
                        store.discard_operation_record(plan_id)
                    except PlanStoreError:
                        raise AlreadyHandedOff(
                            "the operation record survived a failed write"
                        ) from exc
                if _drop_checkpoint(store, plan_id, project_root, checkpoint_id):
                    handoff["checkpoint_id"] = None
                raise
            hook("before_recovery_fsync")
            try:
                disposition = commit(recovery_target, serialize_artifact(descriptor))
            except HandoffError as exc:
                try:
                    store.discard_operation_record(plan_id)
                except PlanStoreError:
                    raise AlreadyHandedOff(
                        "the operation record survived a failed handoff"
                    ) from exc
                if _drop_checkpoint(store, plan_id, project_root, checkpoint_id):
                    handoff["checkpoint_id"] = None
                raise
            hook("after_recovery_fsync")
            if disposition == "idempotent":
                if _drop_checkpoint(store, plan_id, project_root, checkpoint_id):
                    handoff["checkpoint_id"] = None
                raise AlreadyHandedOff("this plan already handed a recovery descriptor off")
            gate.arm()
            hook("before_request")

        def checkpoint_created(checkpoint_id: str) -> None:
            handoff["checkpoint_id"] = checkpoint_id

        try:
            outcome = execute_single_put(
                resolved=resolved,
                plan=_execution_plan(plan),
                transport=gate,
                project_root=project_root,
                config_home=config_home,
                now=now,
                checkpoint_notice=checkpoint_notice,
                source=SpooledSource(_plan_snapshot(plan), frozen),
                checkpoint_created=checkpoint_created,
                retain_definitive_checkpoint=True,
                uuid_factory=uuid_factory,
            )
        except AlreadyHandedOff:
            return emit("in_flight_unknown", checkpoint_id=surviving())
        except (HandoffError, PlanStoreError):
            if gate.calls == 0 and not _armed(store, plan_id):
                return stop(["handoff_write_failed"], state="known_not_applied",
                            plan=plan, contract=digest,
                            checkpoint_id=rollback_unarmed_checkpoint())
            if gate.calls:
                hook("after_request")
            return emit("in_flight_unknown", ["handoff_write_failed"],
                        checkpoint_id=surviving())
        except TransportSealed:
            raise
        except DefinitiveNoWrite:
            if gate.calls:
                hook("after_request")
            return emit("known_not_applied", capabilities_ok=False,
                        checkpoint_id=surviving())
        except Exception:
            if gate.calls == 0 and not _armed(store, plan_id):
                return stop([], state="known_not_applied", plan=plan, contract=digest,
                            checkpoint_id=rollback_unarmed_checkpoint())
            if gate.calls:
                hook("after_request")
            return emit("in_flight_unknown", checkpoint_id=surviving())
        if gate.sealed_violations:
            raise TransportSealed(
                "a remote request preceded the durable recovery handoff"
            )
        hook("after_request")
        state, reasons = STATE_BY_STATUS.get(
            outcome.result["status"], ("blocked", ("unclassified_outcome",))
        )
        return emit(state, reasons, checkpoint_id=surviving())
