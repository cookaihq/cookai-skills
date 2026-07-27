from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from action_registry import (
    AUTHORIZED_ACTION_ORDER, RECOVERY_STATES, allowed_actions, retry_safe,
)
from delivery_schema import (
    BLOCKING_REASONS, OPERATIONS, artifact_digest, build_typed,
)


class RecordError(ValueError):
    pass


RECOVERY_DOMAIN = "s3-upload/recovery-descriptor/v1"
RESULT_DOMAIN = "s3-upload/result/v1"
ACK_DOMAIN = "s3-upload/ack/v1"


def _state(state: str) -> str:
    if state not in RECOVERY_STATES:
        raise RecordError("unregistered recovery state")
    return state


def authorization_required(state: str, *, capabilities_ok: bool) -> Tuple[str, ...]:
    state = _state(state)
    available = set(allowed_actions(state, capabilities_ok=capabilities_ok))
    return tuple(action for action in AUTHORIZED_ACTION_ORDER if action in available)


def _state_fields(state: str, capabilities_ok: bool) -> Dict[str, Any]:
    state = _state(state)
    return {
        "allowed_actions": list(allowed_actions(state, capabilities_ok=capabilities_ok)),
        "recovery_state": state,
        "retry_safe": retry_safe(state, capabilities_ok=capabilities_ok),
    }


def _operation(operation: str) -> str:
    if operation not in OPERATIONS:
        raise RecordError("unregistered operation")
    return operation


def result_hash(body: Dict[str, Any]) -> str:
    material = {key: value for key, value in body.items() if key != "result_hash"}
    return artifact_digest(RESULT_DOMAIN, material)


def build_recovery_descriptor(*, recovery_id: str, root_recovery_id: str, operation: str,
                              operation_id: str, plan_id: str, plan_hash: str,
                              target_contract_hash: str, object_key: str, result_out: str,
                              state: str, capabilities_ok: bool) -> Dict[str, Any]:
    body = {
        "object_key": object_key,
        "operation": _operation(operation),
        "operation_id": operation_id,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "recovery_id": recovery_id,
        "result_out": result_out,
        "root_recovery_id": root_recovery_id,
        "target_contract_hash": target_contract_hash,
    }
    state_fields = _state_fields(state, capabilities_ok)
    if body["operation"] not in state_fields["allowed_actions"]:
        raise RecordError("operation is not permitted by recovery state")
    body.update(state_fields)
    return build_typed("s3-upload.recovery-descriptor", body)


def build_result(*, operation: str, operation_id: str, plan_id: str, plan_hash: str,
                 target_contract_hash: str, recovery_id: str, root_recovery_id: str,
                 state: str, capabilities_ok: bool,
                 blocking_reasons: Iterable[str] = (),
                 predecessor_operation_id: Optional[str] = None,
                 predecessor_result_hash: Optional[str] = None) -> Dict[str, Any]:
    reasons = list(blocking_reasons)
    for reason in reasons:
        if reason not in BLOCKING_REASONS:
            raise RecordError("unregistered blocking reason")
    body = {
        "authorization_required": list(
            authorization_required(state, capabilities_ok=capabilities_ok)
        ),
        "blocking_reasons": reasons,
        "operation": _operation(operation),
        "operation_id": operation_id,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "predecessor_operation_id": predecessor_operation_id,
        "predecessor_result_hash": predecessor_result_hash,
        "recovery_id": recovery_id,
        "result_hash": None,
        "root_recovery_id": root_recovery_id,
        "target_contract_hash": target_contract_hash,
    }
    body.update(_state_fields(state, capabilities_ok))
    body["result_hash"] = result_hash(body)
    return build_typed("s3-upload.result", body)


def build_ack(*, caller: str, plan_id: str, recovery_id: str, root_recovery_id: str,
              predecessor_operation_id: str, result_hash_value: str) -> Dict[str, Any]:
    body = {
        "acknowledged": True,
        "caller": caller,
        "plan_id": plan_id,
        "predecessor_operation_id": predecessor_operation_id,
        "recovery_id": recovery_id,
        "result_hash": result_hash_value,
        "root_recovery_id": root_recovery_id,
    }
    return build_typed("s3-upload.ack", body)
