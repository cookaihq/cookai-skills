from __future__ import annotations

from typing import Any, Dict, Optional

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
