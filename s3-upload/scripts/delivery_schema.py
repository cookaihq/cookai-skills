from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional, Tuple

from strict_json import StrictJSONError, canonicalize, loads


ARTIFACT_TYPES = frozenset({
    "s3-upload.probe",
    "s3-upload.plan",
    "s3-upload.object-reference",
    "s3-upload.result",
    "s3-upload.recovery-descriptor",
    "s3-upload.ack",
})

SCHEMA_VERSIONS = {name: 1 for name in ARTIFACT_TYPES}

ENVELOPE_KEYS = ("artifact_type", "schema_version")


class DeliverySchemaError(ValueError):
    pass


def envelope(artifact_type: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if artifact_type not in ARTIFACT_TYPES:
        raise DeliverySchemaError("unregistered artifact_type")
    if not isinstance(body, dict):
        raise DeliverySchemaError("artifact body must be an object")
    for key in ENVELOPE_KEYS:
        if key in body:
            raise DeliverySchemaError(f"artifact body must not set {key}")
    item: Dict[str, Any] = {
        "artifact_type": artifact_type,
        "schema_version": SCHEMA_VERSIONS[artifact_type],
    }
    item.update(body)
    return item


def parse_artifact(text: str, *, expected_type: Optional[str] = None) -> Dict[str, Any]:
    try:
        value = loads(text)
    except StrictJSONError as exc:
        raise DeliverySchemaError("artifact is not strict JSON") from exc
    if not isinstance(value, dict):
        raise DeliverySchemaError("artifact must be an object")
    artifact_type = value.get("artifact_type")
    version = value.get("schema_version")
    if artifact_type is None:
        raise DeliverySchemaError("incompatible artifact: missing artifact_type")
    if not isinstance(artifact_type, str) or artifact_type not in ARTIFACT_TYPES:
        raise DeliverySchemaError("incompatible artifact: unregistered artifact_type")
    if isinstance(version, bool) or not isinstance(version, int):
        raise DeliverySchemaError("incompatible artifact: schema_version must be an integer")
    if version != SCHEMA_VERSIONS[artifact_type]:
        raise DeliverySchemaError("incompatible artifact: unsupported schema_version")
    if expected_type is not None and artifact_type != expected_type:
        raise DeliverySchemaError("incompatible artifact: unexpected artifact_type")
    return value


def serialize_artifact(artifact: Dict[str, Any]) -> bytes:
    if not isinstance(artifact, dict):
        raise DeliverySchemaError("artifact must be an object")
    artifact_type = artifact.get("artifact_type")
    if not isinstance(artifact_type, str) or artifact_type not in ARTIFACT_TYPES:
        raise DeliverySchemaError("unregistered artifact_type")
    version = artifact.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise DeliverySchemaError("schema_version must be an integer")
    if version != SCHEMA_VERSIONS[artifact_type]:
        raise DeliverySchemaError("unsupported schema_version")
    try:
        return canonicalize(artifact)
    except StrictJSONError as exc:
        raise DeliverySchemaError("artifact is not canonically serializable") from exc


OPERATIONS: Tuple[str, ...] = ("publish", "reconcile", "resume", "abort", "ack", "inspect")

BLOCKING_REASONS: Tuple[str, ...] = (
    "abort_unavailable",
    "already_acknowledged",
    "authorization_missing",
    "authorization_stale",
    "caller_drift",
    "capability_missing",
    "collision_unverified",
    "content_mismatch",
    "cwd_drift",
    "executable_drift",
    "handoff_unsafe",
    "handoff_write_failed",
    "plan_not_executable",
    "public_verification_failed",
    "remote_rejected_absent",
    "remote_rejected_denied",
    "remote_rejected_invalid",
    "remote_rejected_unclassified",
    "resume_unavailable",
    "source_drift",
    "state_root_drift",
    "target_contract_drift",
    "token_invalid",
    "unclassified_outcome",
    "verification_capability_missing",
    "verification_incomplete",
)

# How the remote qualified its refusal, as a closed dimension inside
# BLOCKING_REASONS rather than a parallel table: a caller that only knows the
# reason vocabulary must still be able to see that the object is absent, that
# the credential was denied, that the request was malformed, or that the
# refusal could not be placed in any of those -- without a transport status
# code leaking into a public artifact.
REMOTE_REJECTIONS: Tuple[str, ...] = (
    "remote_rejected_absent",
    "remote_rejected_denied",
    "remote_rejected_invalid",
    "remote_rejected_unclassified",
)

OUTCOMES: Tuple[str, ...] = (
    "adopted", "blocked", "created", "not_applied", "reconciled", "rejected",
    "unknown",
)

DISPOSITIONS: Tuple[str, ...] = ("adopted", "created", "reconciled")

VERIFICATION_CHANNELS: Tuple[str, ...] = (
    "anonymous_public_get", "authenticated_full_get",
)

# The write certainty an outcome is allowed to claim. None is not "unset": it
# is the claim that this operation's write is unknown, which is exactly what
# reconciled has to keep and what created must never be folded into.
OUTCOME_WRITE_CERTAINTY: Dict[str, Optional[bool]] = {
    "adopted": False,
    "blocked": False,
    "created": True,
    "not_applied": False,
    "reconciled": None,
    "rejected": False,
    "unknown": None,
}

PLAN_FIELDS: Tuple[str, ...] = (
    "access", "blocking_reasons", "caller", "collision", "contract_key", "cwd",
    "executable", "executable_path", "object_headers", "object_key", "plan_hash",
    "plan_id", "plan_token", "recovery_out", "remote_operations",
    "required_capabilities", "result_out", "retention", "source", "state_root",
    "target_contract", "target_contract_hash", "target_ref", "upload_mode",
)

RECOVERY_FIELDS: Tuple[str, ...] = (
    "allowed_actions", "object_key", "operation", "operation_id", "plan_hash",
    "plan_id", "recovery_id", "recovery_state", "result_out", "retry_safe",
    "root_recovery_id", "target_contract_hash",
)

RESULT_FIELDS: Tuple[str, ...] = (
    "allowed_actions", "authorization_required", "blocking_reasons", "operation",
    "operation_id", "plan_hash", "plan_id", "predecessor_operation_id",
    "predecessor_result_hash", "recovery_id", "recovery_state", "result_hash",
    "retry_safe", "root_recovery_id", "target_contract_hash",
)

ACK_FIELDS: Tuple[str, ...] = (
    "acknowledged", "caller", "plan_id", "predecessor_operation_id", "recovery_id",
    "result_hash", "root_recovery_id",
)

BODY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "s3-upload.plan": PLAN_FIELDS,
    "s3-upload.recovery-descriptor": RECOVERY_FIELDS,
    "s3-upload.result": RESULT_FIELDS,
    "s3-upload.ack": ACK_FIELDS,
}


def body_of(artifact: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(artifact, dict):
        raise DeliverySchemaError("artifact must be an object")
    return {key: value for key, value in artifact.items() if key not in ENVELOPE_KEYS}


def _exact_body(artifact_type: str, body: Dict[str, Any]) -> None:
    if artifact_type not in BODY_FIELDS:
        raise DeliverySchemaError("artifact type has no registered field set")
    if not isinstance(body, dict):
        raise DeliverySchemaError("artifact body must be an object")
    expected = set(BODY_FIELDS[artifact_type])
    unknown = sorted(set(body) - expected)
    missing = sorted(expected - set(body))
    if unknown:
        raise DeliverySchemaError(f"{artifact_type} body has unknown fields: {', '.join(unknown)}")
    if missing:
        raise DeliverySchemaError(f"{artifact_type} body is missing fields: {', '.join(missing)}")


def build_typed(artifact_type: str, body: Dict[str, Any]) -> Dict[str, Any]:
    _exact_body(artifact_type, body)
    return envelope(artifact_type, body)


def parse_typed(text: str, *, expected_type: str) -> Dict[str, Any]:
    if expected_type not in BODY_FIELDS:
        raise DeliverySchemaError("artifact type has no registered field set")
    item = parse_artifact(text, expected_type=expected_type)
    _exact_body(expected_type, body_of(item))
    return item


def artifact_digest(domain: str, value: Any) -> str:
    try:
        payload = canonicalize({"domain": domain, "value": value})
    except StrictJSONError as exc:
        raise DeliverySchemaError("digest input is not canonically serializable") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()
