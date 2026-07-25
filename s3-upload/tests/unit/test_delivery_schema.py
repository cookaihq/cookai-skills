import pytest

from delivery_schema import (
    ARTIFACT_TYPES,
    DeliverySchemaError,
    envelope,
    parse_artifact,
    serialize_artifact,
)


def test_artifact_types_are_closed():
    assert ARTIFACT_TYPES == frozenset({
        "s3-upload.probe",
        "s3-upload.plan",
        "s3-upload.object-reference",
        "s3-upload.result",
        "s3-upload.recovery-descriptor",
        "s3-upload.ack",
    })


def test_envelope_adds_type_and_integer_version():
    item = envelope("s3-upload.probe", {"readiness": "ready"})
    assert item["artifact_type"] == "s3-upload.probe"
    assert item["schema_version"] == 1
    assert not isinstance(item["schema_version"], bool)
    assert item["readiness"] == "ready"


def test_envelope_rejects_body_overriding_envelope_fields():
    with pytest.raises(DeliverySchemaError):
        envelope("s3-upload.probe", {"artifact_type": "s3-upload.plan"})
    with pytest.raises(DeliverySchemaError):
        envelope("s3-upload.probe", {"schema_version": 2})


def test_envelope_rejects_unregistered_type():
    with pytest.raises(DeliverySchemaError):
        envelope("s3-upload.maintainer-evidence", {})


def test_parse_round_trips_canonical_bytes():
    item = envelope("s3-upload.result", {"outcome": "created"})
    raw = serialize_artifact(item)
    assert parse_artifact(raw.decode("utf-8")) == item
    assert serialize_artifact(parse_artifact(raw.decode("utf-8"))) == raw


def test_parse_rejects_unregistered_pair():
    with pytest.raises(DeliverySchemaError):
        parse_artifact('{"artifact_type":"s3-upload.probe","schema_version":2}')
    with pytest.raises(DeliverySchemaError):
        parse_artifact('{"artifact_type":"s3-upload.unknown","schema_version":1}')


def test_parse_rejects_v1_artifact_without_type():
    legacy = '{"schema_version":1,"target_ref":"project:images","location":{}}'
    with pytest.raises(DeliverySchemaError) as excinfo:
        parse_artifact(legacy)
    assert "incompatible" in str(excinfo.value)


def test_parse_rejects_boolean_version():
    with pytest.raises(DeliverySchemaError):
        parse_artifact('{"artifact_type":"s3-upload.probe","schema_version":true}')


def test_parse_enforces_expected_type():
    raw = serialize_artifact(envelope("s3-upload.probe", {})).decode("utf-8")
    with pytest.raises(DeliverySchemaError):
        parse_artifact(raw, expected_type="s3-upload.plan")


def test_parse_error_does_not_echo_untrusted_content():
    raw = '{"artifact_type":"s3-upload.unknown","schema_version":1,"leak":"AKIAsecretvalue"}'
    with pytest.raises(DeliverySchemaError) as excinfo:
        parse_artifact(raw)
    assert "AKIAsecretvalue" not in str(excinfo.value)


def test_parse_rejects_floats_via_strict_json():
    with pytest.raises(DeliverySchemaError):
        parse_artifact('{"artifact_type":"s3-upload.probe","schema_version":1,"x":1.5}')


def test_serialize_rejects_non_object():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact([])


def test_serialize_rejects_unregistered_type():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact({"artifact_type": "s3-upload.unknown", "schema_version": 1})


def test_serialize_rejects_missing_or_unregistered_version():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact({"artifact_type": "s3-upload.probe"})
    with pytest.raises(DeliverySchemaError):
        serialize_artifact({"artifact_type": "s3-upload.probe", "schema_version": 99})


def test_serialize_rejects_floats_via_strict_json():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact(envelope("s3-upload.probe", {"x": 1.5}))


def test_serialize_rejects_boolean_version():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact({"artifact_type": "s3-upload.probe", "schema_version": True})


def test_serialize_rejects_unhashable_type():
    with pytest.raises(DeliverySchemaError):
        serialize_artifact({"artifact_type": ["x"], "schema_version": 1})
