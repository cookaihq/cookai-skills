from pathlib import Path

import pytest

from action_registry import ACTIONS
from delivery_schema import (
    ACK_FIELDS,
    ARTIFACT_TYPES,
    BLOCKING_REASONS,
    BODY_FIELDS,
    DeliverySchemaError,
    OPERATIONS,
    PLAN_FIELDS,
    RECOVERY_FIELDS,
    RESULT_FIELDS,
    artifact_digest,
    body_of,
    build_typed,
    parse_typed,
    serialize_artifact,
)


GOLDENS = Path(__file__).parents[1] / "goldens" / "delivery"


def plan_body(**overrides):
    body = {
        "access": {"mode": "private", "url_kind": "presigned", "presign_expires_seconds": 3600,
                   "presign_effective_seconds": 3600, "public_base_url": None},
        "blocking_reasons": [],
        "caller": "pdf2markdown",
        "collision": {"policy": "replace", "max_attempts": 1},
        "contract_key": {"schema_version": 1},
        "cwd": "/projects/demo",
        "executable": True,
        "executable_path": "/usr/bin/python3",
        "object_headers": {"content_type": "image/png", "cache_control": None,
                           "content_disposition": None},
        "object_key": "images/a.png",
        "plan_hash": "sha256:" + "0" * 64,
        "plan_id": "0" * 32,
        "plan_token": None,
        "recovery_out": "/projects/demo/out/recovery.json",
        "remote_operations": ["PutObject"],
        "required_capabilities": [],
        "result_out": "/projects/demo/out/result.json",
        "retention": {"mode": "retain", "days": None, "enforcement": "external-unverified"},
        "source": {"path": "/projects/demo/a.png", "size": 11, "mtime_ns": "1",
                   "device": "2", "inode": "3",
                   "sha256": "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"},
        "state_root": "/projects/demo/.s3-upload",
        "target_contract": {"contract_version": 1},
        "target_contract_hash": "sha256:" + "1" * 64,
        "target_ref": "project:images",
        "upload_mode": "single-put",
    }
    body.update(overrides)
    return body


def test_operations_vocabulary_is_locked():
    assert OPERATIONS == ("publish", "reconcile", "resume", "abort", "ack", "inspect")


def test_verify_public_is_not_a_caller_operation():
    assert "verify_public" in ACTIONS
    assert "verify_public" not in OPERATIONS


def test_blocking_reasons_vocabulary_is_locked():
    assert BLOCKING_REASONS == (
        "already_acknowledged",
        "caller_drift",
        "capability_missing",
        "cwd_drift",
        "executable_drift",
        "handoff_unsafe",
        "handoff_write_failed",
        "plan_not_executable",
        "source_drift",
        "state_root_drift",
        "target_contract_drift",
        "token_invalid",
        "unclassified_outcome",
    )


def test_body_fields_cover_exactly_four_artifact_types():
    assert set(BODY_FIELDS) == {
        "s3-upload.plan",
        "s3-upload.recovery-descriptor",
        "s3-upload.result",
        "s3-upload.ack",
    }
    assert set(BODY_FIELDS) | {"s3-upload.probe", "s3-upload.object-reference"} == ARTIFACT_TYPES
    assert BODY_FIELDS["s3-upload.plan"] == PLAN_FIELDS
    assert BODY_FIELDS["s3-upload.recovery-descriptor"] == RECOVERY_FIELDS
    assert BODY_FIELDS["s3-upload.result"] == RESULT_FIELDS
    assert BODY_FIELDS["s3-upload.ack"] == ACK_FIELDS


def test_every_registered_field_set_is_sorted_and_unique():
    for artifact_type, fields in BODY_FIELDS.items():
        assert len(set(fields)) == len(fields), artifact_type
        assert list(fields) == sorted(fields), artifact_type
        assert "artifact_type" not in fields
        assert "schema_version" not in fields


def test_plan_fields_are_locked():
    assert PLAN_FIELDS == (
        "access", "blocking_reasons", "caller", "collision", "contract_key", "cwd",
        "executable", "executable_path", "object_headers", "object_key", "plan_hash",
        "plan_id", "plan_token", "recovery_out", "remote_operations",
        "required_capabilities", "result_out", "retention", "source", "state_root",
        "target_contract", "target_contract_hash", "target_ref", "upload_mode",
    )


def test_recovery_fields_are_locked():
    assert RECOVERY_FIELDS == (
        "allowed_actions", "object_key", "operation", "operation_id", "plan_hash",
        "plan_id", "recovery_id", "recovery_state", "result_out", "retry_safe",
        "root_recovery_id", "target_contract_hash",
    )


def test_result_fields_are_locked():
    assert RESULT_FIELDS == (
        "allowed_actions", "authorization_required", "blocking_reasons", "operation",
        "operation_id", "plan_hash", "plan_id", "predecessor_operation_id",
        "predecessor_result_hash", "recovery_id", "recovery_state", "result_hash",
        "retry_safe", "root_recovery_id", "target_contract_hash",
    )


def test_ack_fields_are_locked():
    assert ACK_FIELDS == (
        "acknowledged", "caller", "plan_id", "predecessor_operation_id", "recovery_id",
        "result_hash", "root_recovery_id",
    )


def test_build_typed_wraps_body_in_the_envelope():
    item = build_typed("s3-upload.plan", plan_body())
    assert item["artifact_type"] == "s3-upload.plan"
    assert item["schema_version"] == 1
    assert body_of(item) == plan_body()


def test_build_typed_rejects_missing_field():
    body = plan_body()
    del body["object_key"]
    with pytest.raises(DeliverySchemaError):
        build_typed("s3-upload.plan", body)


def test_build_typed_rejects_unknown_field():
    with pytest.raises(DeliverySchemaError):
        build_typed("s3-upload.plan", plan_body(sibling_plan_ids=["x"]))


def test_build_typed_rejects_type_without_registered_field_set():
    with pytest.raises(DeliverySchemaError):
        build_typed("s3-upload.probe", {})


def test_parse_typed_round_trips_canonical_bytes():
    raw = serialize_artifact(build_typed("s3-upload.plan", plan_body())).decode("utf-8")
    item = parse_typed(raw, expected_type="s3-upload.plan")
    assert serialize_artifact(item).decode("utf-8") == raw


def test_parse_typed_rejects_extra_body_field():
    raw = serialize_artifact(
        build_typed("s3-upload.plan", plan_body())
    ).decode("utf-8").replace('"caller":', '"caller_extra":"x","caller":', 1)
    with pytest.raises(DeliverySchemaError):
        parse_typed(raw, expected_type="s3-upload.plan")


def test_parse_typed_rejects_cross_type_artifact():
    raw = serialize_artifact(build_typed("s3-upload.plan", plan_body())).decode("utf-8")
    with pytest.raises(DeliverySchemaError):
        parse_typed(raw, expected_type="s3-upload.result")


def test_artifact_digest_is_domain_separated():
    first = artifact_digest("s3-upload/plan/v1", {"a": 1})
    second = artifact_digest("s3-upload/result/v1", {"a": 1})
    assert first != second
    assert first.startswith("sha256:")
    assert len(first) == 71


def test_legacy_v1_plan_golden_is_incompatible():
    raw = (GOLDENS / "legacy-plan-v1.json").read_text(encoding="utf-8").strip()
    with pytest.raises(DeliverySchemaError) as excinfo:
        parse_typed(raw, expected_type="s3-upload.plan")
    assert "incompatible" in str(excinfo.value)


def test_legacy_v1_result_golden_is_incompatible():
    raw = (GOLDENS / "legacy-result-v1.json").read_text(encoding="utf-8").strip()
    with pytest.raises(DeliverySchemaError) as excinfo:
        parse_typed(raw, expected_type="s3-upload.result")
    assert "incompatible" in str(excinfo.value)


def test_legacy_goldens_are_not_silently_upgraded():
    for name in ("legacy-plan-v1.json", "legacy-result-v1.json"):
        raw = (GOLDENS / name).read_text(encoding="utf-8").strip()
        assert "artifact_type" not in raw
        for artifact_type in BODY_FIELDS:
            with pytest.raises(DeliverySchemaError):
                parse_typed(raw, expected_type=artifact_type)
