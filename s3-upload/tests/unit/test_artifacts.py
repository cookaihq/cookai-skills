from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from artifacts import (
    ArtifactError,
    CheckpointStore,
    IdentifierRejected,
    build_object_reference,
    lifecycle_policy_id,
    parse_checkpoint,
    parse_object_reference,
    preflight_reference_output,
    serialize_object_reference,
    validate_provider_identifier,
    write_reference_output,
)
from v2_schema import parse_target


def target_dict():
    return {
        "schema_version": 1,
        "credential": "project:key-main",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "temporary-builds/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 3600,
        },
        "retention": {"mode": "retain", "days": None},
        "collision": "replace",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": None,
            "part_size_bytes": None,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }


def object_reference(version_id=None):
    target = parse_target(target_dict(), expected_scope="project")
    return build_object_reference(
        target_ref="project:temporary-builds",
        target=target,
        key="temporary-builds/cover.png",
        version_id=version_id,
    )


def checkpoint(reference=None, **changes):
    value = {
        "schema_version": 1,
        "checkpoint_id": "123456789abc4def8123456789abcdef",
        "kind": "put",
        "state": "prepared",
        "operation_id": "abcdef0123454abc9def0123456789ab",
        "created_at": "2026-07-22T12:00:00Z",
        "updated_at": "2026-07-22T12:00:00Z",
        "target_ref": "project:temporary-builds",
        "target_fingerprint": object_reference()["target_fingerprint"],
        "object_reference_draft": reference or object_reference(),
        "upload_plan": {
            "content_type": "image/png",
            "cache_control": None,
            "content_disposition": None,
            "presign_expires_seconds": 3600,
        },
        "collision": {
            "policy": "replace",
            "base_key": "temporary-builds/cover.png",
            "attempt": 1,
            "max_attempts": 1,
        },
        "source": {
            "path": "/tmp/cover.png",
            "size": 5,
            "mtime_ns": "1",
            "device": "2",
            "inode": "3",
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        },
        "reference_out": None,
        "multipart": None,
        "delete_scope": None,
    }
    value.update(changes)
    return value


def multipart_checkpoint(*, state, upload_id=None, return_state=None,
                         in_flight_part=None, acknowledged_parts=None, source_size=5):
    value = checkpoint(kind="multipart", state=state)
    value["source"] = dict(value["source"], size=source_size)
    value["multipart"] = {
        "upload_id": upload_id,
        "part_size_bytes": 5242880,
        "part_max_attempts": 3,
        "return_state": return_state,
        "in_flight_part": in_flight_part,
        "acknowledged_parts": acknowledged_parts or [],
    }
    return value


def test_object_reference_is_closed_and_canonical():
    reference = object_reference("version-1")
    assert list(reference) == [
        "schema_version", "target_ref", "target_fingerprint", "location", "access", "retention"
    ]
    assert reference["location"]["version_id"] == "version-1"
    encoded = serialize_object_reference(reference)
    assert not encoded.endswith(b"\n")
    assert parse_object_reference(encoded.decode("utf-8")) == reference
    changed = dict(reference)
    changed["unknown"] = True
    with pytest.raises(ArtifactError, match="unknown fields"):
        parse_object_reference(json.dumps(changed))


def test_lifecycle_policy_id_is_stable_and_location_specific():
    target = parse_target(target_dict(), expected_scope="project")
    first = lifecycle_policy_id("project:temporary-builds", target)
    second = lifecycle_policy_id("project:temporary-builds", target)
    assert first == second
    assert first.startswith("s3-upload-v2-") and len(first) == len("s3-upload-v2-") + 64


@pytest.mark.parametrize(
    "value",
    ["", "bad\nvalue", "x" * 4097, "prefix-SECRET-VALUE-suffix", "prefix-SECRET%2DVALUE-suffix"],
)
def test_provider_identifier_rejects_invalid_or_reflected_values(value):
    with pytest.raises(IdentifierRejected) as error:
        validate_provider_identifier(value, ("ACCESS-KEY", "SECRET-VALUE", "SESSION-TOKEN"))
    if value:
        assert value not in str(error.value)


def test_checkpoint_schema_rejects_variant_and_part_inconsistency():
    assert parse_checkpoint(checkpoint())["state"] == "prepared"
    invalid = checkpoint(multipart={
        "upload_id": "upload-1",
        "part_size_bytes": 5242880,
        "part_max_attempts": 3,
        "return_state": None,
        "in_flight_part": None,
        "acknowledged_parts": [],
    })
    with pytest.raises(ArtifactError, match="multipart must be null"):
        parse_checkpoint(invalid)

    multipart = checkpoint(
        kind="multipart",
        state="uploading",
        multipart={
            "upload_id": "upload-1",
            "part_size_bytes": 5242880,
            "part_max_attempts": 3,
            "return_state": None,
            "in_flight_part": {
                "part_number": 1,
                "size": 5,
                "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                "attempt": 1,
            },
            "acknowledged_parts": [{
                "part_number": 1,
                "size": 5,
                "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                "etag": "etag-1",
            }],
        },
    )
    with pytest.raises(ArtifactError, match="in-flight part duplicates"):
        parse_checkpoint(multipart)


@pytest.mark.parametrize(
    "state, changes",
    [
        (state, {"upload_id": "upload-1"})
        for state in ("prepared", "initiating", "initiation_unknown", "not_started")
    ] + [
        (state, {"acknowledged_parts": [{
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "etag": "etag-1",
        }]})
        for state in ("prepared", "initiating", "initiation_unknown", "not_started")
    ] + [
        (state, {"in_flight_part": {
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "attempt": 1,
        }})
        for state in ("prepared", "initiating", "initiation_unknown", "not_started")
    ],
)
def test_pre_session_multipart_states_reject_session_identifiers_and_parts(state, changes):
    value = multipart_checkpoint(state=state, **changes)

    with pytest.raises(ArtifactError, match="before initiation"):
        parse_checkpoint(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"acknowledged_parts": [{
            "part_number": 1,
            "size": 4,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "etag": "etag-1",
        }]},
        {"in_flight_part": {
            "part_number": 1,
            "size": 4,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "attempt": 1,
        }},
    ],
)
def test_multipart_parts_must_match_their_deterministic_source_range(changes):
    value = multipart_checkpoint(state="uploading", upload_id="upload-1", **changes)

    with pytest.raises(ArtifactError, match="source range"):
        parse_checkpoint(value)


@pytest.mark.parametrize(
    "state",
    ("completing", "completion_unknown", "collision_detected", "complete"),
)
def test_multipart_completion_states_require_the_full_source_to_be_acknowledged(state):
    value = multipart_checkpoint(state=state, upload_id="upload-1")

    with pytest.raises(ArtifactError, match="fully acknowledged"):
        parse_checkpoint(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"acknowledged_parts": [{
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "etag": "etag-1",
        }]},
        {"in_flight_part": {
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "attempt": 1,
        }},
    ],
)
def test_initiated_multipart_has_not_started_any_part(changes):
    value = multipart_checkpoint(state="initiated", upload_id="upload-1", **changes)

    with pytest.raises(ArtifactError, match="initiated"):
        parse_checkpoint(value)


@pytest.mark.parametrize("state", ("aborting", "abort_unknown"))
@pytest.mark.parametrize(
    "return_state, acknowledged_parts",
    [
        ("initiated", [{
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "etag": "etag-1",
        }]),
        ("collision_detected", []),
    ],
)
def test_abort_states_preserve_parts_consistent_with_their_return_state(
    state, return_state, acknowledged_parts
):
    value = multipart_checkpoint(
        state=state,
        upload_id="upload-1",
        return_state=return_state,
        acknowledged_parts=acknowledged_parts,
    )

    with pytest.raises(ArtifactError, match="return_state"):
        parse_checkpoint(value)


def test_aborted_multipart_rejects_an_in_flight_part():
    value = multipart_checkpoint(
        state="aborted",
        upload_id="upload-1",
        in_flight_part={
            "part_number": 1,
            "size": 5,
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "attempt": 1,
        },
    )

    with pytest.raises(ArtifactError, match="aborted"):
        parse_checkpoint(value)


def test_checkpoint_store_creates_guard_and_round_trips(tmp_path):
    store = CheckpointStore(str(tmp_path))
    value = checkpoint()
    store.create(value)
    guard = tmp_path / ".s3-upload" / "checkpoints" / ".gitignore"
    path = tmp_path / ".s3-upload" / "checkpoints" / (value["checkpoint_id"] + ".json")
    assert guard.read_bytes() == b"*\n!.gitignore\n"
    assert (guard.stat().st_mode & 0o777) == 0o600
    assert (path.stat().st_mode & 0o777) == 0o600
    assert store.load(value["checkpoint_id"]) == value
    changed = dict(value, state="put_in_flight")
    store.replace(changed)
    assert store.load(value["checkpoint_id"])["state"] == "put_in_flight"


def test_checkpoint_store_rejects_unsafe_guard(tmp_path):
    directory = tmp_path / ".s3-upload" / "checkpoints"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    guard = directory / ".gitignore"
    guard.write_text("*\n", encoding="utf-8")
    guard.chmod(0o600)
    with pytest.raises(ArtifactError, match="guard"):
        CheckpointStore(str(tmp_path)).create(checkpoint())


def test_reference_output_rejects_protected_and_source_aliases(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    source_info = source.stat()
    env_local = tmp_path / ".env.local"
    env_local.write_text("secret", encoding="utf-8")
    env_local.chmod(0o600)

    with pytest.raises(ArtifactError, match="protected"):
        preflight_reference_output(
            str(env_local), project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            source_identity=(source_info.st_dev, source_info.st_ino),
        )

    alias = tmp_path / "alias.json"
    os.link(source, alias)
    with pytest.raises(ArtifactError, match="alias|link"):
        preflight_reference_output(
            str(alias), project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            source_identity=(source_info.st_dev, source_info.st_ino),
        )


def test_reference_output_atomic_write_and_idempotent_replay(tmp_path):
    output = tmp_path / "references" / "object.json"
    output.parent.mkdir()
    snapshot = preflight_reference_output(
        str(output), project_root=str(tmp_path), config_home=str(tmp_path / "home"),
        source_identity=None,
    )
    reference = object_reference()
    write_reference_output(snapshot, reference)
    assert output.read_bytes() == serialize_object_reference(reference)
    assert (output.stat().st_mode & 0o777) == 0o600
    write_reference_output(snapshot, reference)


def test_reference_output_detects_cas_change(tmp_path):
    output = tmp_path / "object.json"
    snapshot = preflight_reference_output(
        str(output), project_root=str(tmp_path), config_home=str(tmp_path / "home"),
        source_identity=None,
    )
    output.write_text("unrelated", encoding="utf-8")
    output.chmod(0o600)
    with pytest.raises(ArtifactError, match="changed"):
        write_reference_output(snapshot, object_reference())
    assert output.read_text(encoding="utf-8") == "unrelated"


def test_reference_output_parent_swap_cannot_redirect_the_write(tmp_path, monkeypatch):
    parent = tmp_path / "references"
    parent.mkdir()
    output = parent / "object.json"
    snapshot = preflight_reference_output(
        str(output), project_root=str(tmp_path), config_home=str(tmp_path / "home"),
        source_identity=None,
    )
    parent_info = parent.stat()
    displaced_parent = tmp_path / "validated-references"
    real_close = os.close
    swapped = False

    def close_and_swap_parent(descriptor):
        nonlocal swapped
        try:
            info = os.fstat(descriptor)
        except OSError:
            info = None
        real_close(descriptor)
        if (
            not swapped
            and info is not None
            and (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino)
        ):
            parent.rename(displaced_parent)
            parent.mkdir()
            swapped = True

    monkeypatch.setattr(os, "close", close_and_swap_parent)

    reference = object_reference()
    write_reference_output(snapshot, reference)

    assert swapped is True
    assert not output.exists()
    assert (displaced_parent / output.name).read_bytes() == serialize_object_reference(reference)
