from pathlib import Path

import pytest

from delivery_schema import DeliverySchemaError, parse_artifact, serialize_artifact


GOLDENS = Path(__file__).parents[1] / "goldens" / "delivery"


def read(name):
    return (GOLDENS / name).read_text(encoding="utf-8").strip()


def test_valid_probe_golden_round_trips_byte_identical():
    raw = read("probe-valid.json")
    artifact = parse_artifact(raw, expected_type="s3-upload.probe")
    assert serialize_artifact(artifact).decode("utf-8") == raw


def test_invalid_version_golden_is_rejected():
    with pytest.raises(DeliverySchemaError):
        parse_artifact(read("probe-invalid-version.json"))


def test_legacy_v1_object_reference_golden_is_incompatible():
    with pytest.raises(DeliverySchemaError) as excinfo:
        parse_artifact(read("legacy-object-reference-v1.json"))
    assert "incompatible" in str(excinfo.value)
