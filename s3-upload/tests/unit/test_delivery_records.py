import hashlib

import pytest

from action_registry import ACTIONS, AUTHORIZED_ACTIONS, RECOVERY_STATES, allowed_actions, retry_safe
from delivery_records import (
    RecordError,
    authorization_required,
    build_ack,
    build_recovery_descriptor,
    build_result,
    result_hash,
)
from delivery_schema import BLOCKING_REASONS, DeliverySchemaError, body_of, serialize_artifact
from strict_json import canonicalize


HASH = "sha256:" + "4" * 64


def recovery(**overrides):
    kwargs = dict(
        recovery_id="r" * 32,
        root_recovery_id="r" * 32,
        operation="publish",
        operation_id="o" * 32,
        plan_id="p" * 32,
        plan_hash=HASH,
        target_contract_hash=HASH,
        object_key="images/a.png",
        result_out="/projects/demo/out/result.json",
        state="not_started",
        capabilities_ok=True,
    )
    kwargs.update(overrides)
    return build_recovery_descriptor(**kwargs)


def result(**overrides):
    kwargs = dict(
        operation="publish",
        operation_id="o" * 32,
        plan_id="p" * 32,
        plan_hash=HASH,
        target_contract_hash=HASH,
        recovery_id="r" * 32,
        root_recovery_id="r" * 32,
        state="not_started",
        capabilities_ok=True,
    )
    kwargs.update(overrides)
    return build_result(**kwargs)


def ack(**overrides):
    kwargs = dict(
        caller="pdf2markdown",
        plan_id="p" * 32,
        recovery_id="r" * 32,
        root_recovery_id="r" * 32,
        predecessor_operation_id="o" * 32,
        result_hash_value=HASH,
    )
    kwargs.update(overrides)
    return build_ack(**kwargs)


def test_recovery_descriptor_is_a_typed_artifact():
    item = recovery()
    assert item["artifact_type"] == "s3-upload.recovery-descriptor"
    assert item["schema_version"] == 1


def test_result_is_a_typed_artifact():
    item = result()
    assert item["artifact_type"] == "s3-upload.result"
    assert item["schema_version"] == 1


def test_ack_is_a_typed_artifact():
    item = ack()
    assert item["artifact_type"] == "s3-upload.ack"
    assert item["schema_version"] == 1


def test_domain_constants_are_pinned_literals():
    import delivery_records

    assert delivery_records.RECOVERY_DOMAIN == "s3-upload/recovery-descriptor/v1"
    assert delivery_records.RESULT_DOMAIN == "s3-upload/result/v1"
    assert delivery_records.ACK_DOMAIN == "s3-upload/ack/v1"


@pytest.mark.parametrize("state", RECOVERY_STATES)
@pytest.mark.parametrize("capabilities_ok", [True, False])
def test_recovery_state_fields_come_from_the_registry(state, capabilities_ok):
    item = body_of(recovery(state=state, capabilities_ok=capabilities_ok))
    assert item["allowed_actions"] == list(
        allowed_actions(state, capabilities_ok=capabilities_ok)
    )
    assert item["retry_safe"] is retry_safe(state)


def test_recovery_descriptor_rejects_an_unregistered_state():
    with pytest.raises(RecordError):
        recovery(state="mystery")


def test_recovery_descriptor_rejects_an_unregistered_operation():
    with pytest.raises(RecordError):
        recovery(operation="verify_public")


def test_recovery_descriptor_carries_no_private_checkpoint_channel():
    body = body_of(recovery())
    assert "checkpoint_id" not in body
    assert "credential" not in body
    assert "authorization" not in body
    raw = serialize_artifact(recovery()).decode("utf-8")
    assert "project-secret-value" not in raw
    assert ".s3-upload/checkpoints" not in raw


@pytest.mark.parametrize("state", RECOVERY_STATES)
def test_result_state_fields_are_consistent_with_the_registry(state):
    body = body_of(result(state=state))
    assert body["recovery_state"] == state
    assert body["allowed_actions"] == list(allowed_actions(state, capabilities_ok=True))
    assert body["retry_safe"] is retry_safe(state)


@pytest.mark.parametrize("state", RECOVERY_STATES)
def test_authorization_required_is_a_subset_of_allowed_authorized_actions(state):
    required = authorization_required(state, capabilities_ok=True)
    assert set(required) <= AUTHORIZED_ACTIONS
    assert set(required) <= set(allowed_actions(state, capabilities_ok=True))
    assert list(required) == sorted(required, key=ACTIONS.index)


def test_authorization_required_is_empty_without_capabilities():
    for state in RECOVERY_STATES:
        assert authorization_required(state, capabilities_ok=False) == ()


def test_result_hash_is_self_consistent():
    item = result()
    body = body_of(item)
    assert body["result_hash"] == result_hash(body)
    assert body["result_hash"].startswith("sha256:")


def test_result_hash_is_computed_from_canonical_bytes_not_repr():
    body = body_of(result())
    material = {key: value for key, value in body.items() if key != "result_hash"}
    expected = "sha256:" + hashlib.sha256(
        canonicalize({"domain": "s3-upload/result/v1", "value": material})
    ).hexdigest()
    assert body["result_hash"] == expected
    assert body["result_hash"] != "sha256:" + hashlib.sha256(repr(material).encode("utf-8")).hexdigest()


def test_result_hash_changes_with_any_bound_fact():
    first = body_of(result())["result_hash"]
    assert first != body_of(result(operation_id="z" * 32))["result_hash"]
    assert first != body_of(result(state="blocked"))["result_hash"]
    assert first != body_of(result(plan_hash="sha256:" + "5" * 64))["result_hash"]


def test_result_rejects_an_unregistered_blocking_reason():
    with pytest.raises(RecordError):
        result(blocking_reasons=["please_retry_later"])


@pytest.mark.parametrize("reason", BLOCKING_REASONS)
def test_result_accepts_every_registered_blocking_reason(reason):
    assert body_of(result(state="blocked", blocking_reasons=[reason]))["blocking_reasons"] == [reason]


def test_result_chain_fields_are_carried_verbatim():
    body = body_of(result(
        operation="reconcile",
        predecessor_operation_id="q" * 32,
        predecessor_result_hash="sha256:" + "6" * 64,
        state="in_flight_unknown",
    ))
    assert body["predecessor_operation_id"] == "q" * 32
    assert body["predecessor_result_hash"] == "sha256:" + "6" * 64
    assert body["root_recovery_id"] == "r" * 32


def test_blocked_result_offers_no_mutation_action():
    body = body_of(result(state="blocked", blocking_reasons=["unclassified_outcome"]))
    assert body["allowed_actions"] == ["inspect"]
    assert body["retry_safe"] is False
    assert body["authorization_required"] == []


def test_ack_is_deterministic_and_carries_the_predecessor_chain():
    assert serialize_artifact(ack()) == serialize_artifact(ack())
    assert body_of(ack())["acknowledged"] is True
    assert body_of(ack())["result_hash"] == HASH
    assert body_of(ack())["predecessor_operation_id"] == "o" * 32
    assert body_of(ack())["root_recovery_id"] == "r" * 32


def test_builders_reject_bodies_with_extra_fields():
    with pytest.raises(TypeError):
        build_ack(caller="pdf2markdown", plan_id="p" * 32, recovery_id="r" * 32,
                  root_recovery_id="r" * 32, predecessor_operation_id="o" * 32,
                  result_hash_value=HASH, extra=1)
