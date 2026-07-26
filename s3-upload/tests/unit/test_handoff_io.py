import os
import stat

import pytest

import delivery_schema
import handoff_io
import safe_io
from handoff_io import (
    HANDOFF_UNSAFE,
    HANDOFF_WRITE_FAILED,
    MAX_ARTIFACT_BYTES,
    HandoffError,
    HandoffTarget,
    commit,
    preflight,
)


PAYLOAD = b'{"artifact_type":"s3-upload.result"}'


def preflight_for(project, path, *, source_identity=None):
    return preflight(
        path,
        project_root=str(project.root),
        config_home=str(project.home),
        state_root=str(project.state_root),
        source_identity=source_identity,
    )


def test_preflight_accepts_a_fresh_caller_owned_destination(project):
    target = preflight_for(project, project.result_out)
    assert isinstance(target, HandoffTarget)
    assert target.existing_sha256 is None
    assert target.path == os.path.abspath(project.result_out)


def _production_shape_protected_target(project, relative):
    target_path = project.root / relative
    if relative in {".s3-upload/config.json", ".s3-upload/targets/images.json"}:
        target_path.chmod(0o600)
    elif relative in {".s3-upload/checkpoints/x.json", ".s3-upload/plans/x/record.json"}:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.parent.chmod(0o700)
    return target_path


@pytest.mark.parametrize("relative", [
    ".env",
    ".env.local",
    ".s3-upload/config.json",
    ".s3-upload/targets/images.json",
    ".s3-upload/checkpoints/x.json",
    ".s3-upload/plans/x/record.json",
    ".s3-upload",
])
def test_preflight_rejects_protected_namespaces(project, relative):
    target_path = _production_shape_protected_target(project, relative)
    with pytest.raises(HandoffError, match="protected namespace") as excinfo:
        preflight_for(project, str(target_path))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_destination_inside_the_config_home(project):
    with pytest.raises(HandoffError, match="protected namespace") as excinfo:
        preflight_for(project, str(project.home / "result.json"))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_destination_inside_a_state_root_outside_the_dot_dir(project, tmp_path):
    external_state_root = tmp_path / "external-state"
    external_state_root.mkdir()
    with pytest.raises(HandoffError, match="protected namespace") as excinfo:
        preflight(
            str(external_state_root / "result.json"),
            project_root=str(project.root),
            config_home=str(project.home),
            state_root=str(external_state_root),
            source_identity=None,
        )
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_symlinked_parent(project):
    link = project.root / "linked-out"
    link.symlink_to(project.out)
    with pytest.raises(HandoffError, match="parent directory is unsafe") as excinfo:
        preflight_for(project, str(link / "result.json"))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_group_writable_parent(project):
    project.out.chmod(0o775)
    try:
        with pytest.raises(HandoffError, match="owned and not group/world-writable") as excinfo:
            preflight_for(project, project.result_out)
        assert excinfo.value.reason == HANDOFF_UNSAFE
    finally:
        project.out.chmod(0o755)


def test_preflight_rejects_a_symlinked_destination(project):
    other = project.out / "elsewhere.json"
    other.write_bytes(PAYLOAD)
    other.chmod(0o600)
    link = project.out / "result.json"
    link.symlink_to(other)
    with pytest.raises(HandoffError, match="destination is unsafe") as excinfo:
        preflight_for(project, str(link))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_hardlink_alias(project):
    other = project.out / "elsewhere.json"
    other.write_bytes(PAYLOAD)
    other.chmod(0o600)
    os.link(str(other), project.result_out)
    with pytest.raises(HandoffError, match="hardlink alias") as excinfo:
        preflight_for(project, project.result_out)
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_world_readable_existing_destination(project):
    existing = project.out / "result.json"
    existing.write_bytes(PAYLOAD)
    existing.chmod(0o644)
    with pytest.raises(HandoffError, match="unsafe ownership or mode") as excinfo:
        preflight_for(project, project.result_out)
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_destination_that_aliases_the_source(project):
    project.source.chmod(0o600)
    info = project.source.stat()
    with pytest.raises(HandoffError, match="aliases the upload source") as excinfo:
        preflight_for(project, str(project.source), source_identity=(info.st_dev, info.st_ino))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_a_non_regular_file_destination(project):
    directory_shaped = project.out / "subdir"
    directory_shaped.mkdir()
    directory_shaped.chmod(0o700)
    with pytest.raises(HandoffError, match="not a regular file") as excinfo:
        preflight_for(project, str(directory_shaped))
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_preflight_rejects_an_oversized_existing_destination(project):
    existing = project.out / "result.json"
    existing.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    existing.chmod(0o600)
    with pytest.raises(HandoffError, match="too large") as excinfo:
        preflight_for(project, project.result_out)
    assert excinfo.value.reason == HANDOFF_UNSAFE


def test_commit_creates_the_artifact_with_0600_and_a_single_link(project):
    target = preflight_for(project, project.result_out)
    assert commit(target, PAYLOAD) == "created"
    written = project.out / "result.json"
    assert written.read_bytes() == PAYLOAD
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert written.stat().st_nlink == 1


def test_commit_replays_byte_identical_payload_idempotently(project):
    target = preflight_for(project, project.result_out)
    assert commit(target, PAYLOAD) == "created"
    assert commit(target, PAYLOAD) == "idempotent"
    assert (project.out / "result.json").read_bytes() == PAYLOAD


def test_commit_refuses_to_overwrite_with_other_content(project):
    target = preflight_for(project, project.result_out)
    commit(target, PAYLOAD)
    with pytest.raises(HandoffError, match="immutable") as excinfo:
        commit(target, b'{"artifact_type":"s3-upload.ack"}')
    assert (project.out / "result.json").read_bytes() == PAYLOAD
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_rejects_a_destination_created_before_commit_starts_as_immutable(project):
    target = preflight_for(project, project.result_out)
    intruder = project.out / "result.json"
    intruder.write_bytes(b"intruder")
    intruder.chmod(0o600)
    with pytest.raises(HandoffError, match="immutable") as excinfo:
        commit(target, PAYLOAD)
    assert intruder.read_bytes() == b"intruder"
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_rejects_a_destination_created_between_the_check_and_the_write(project, monkeypatch):
    real_atomic_write = safe_io.atomic_write
    intruder_payload = b"intruder-bytes"

    def racing_atomic_write(path, data, *, mode=0o600, replace=True, dir_fd=None):
        intruder_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(intruder_fd, intruder_payload)
        finally:
            os.close(intruder_fd)
        return real_atomic_write(path, data, mode=mode, replace=replace, dir_fd=dir_fd)

    monkeypatch.setattr(handoff_io, "atomic_write", racing_atomic_write)
    target = preflight_for(project, project.result_out)
    with pytest.raises(HandoffError, match="created concurrently") as excinfo:
        commit(target, PAYLOAD)
    assert (project.out / "result.json").read_bytes() == intruder_payload
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_rejects_a_parent_directory_that_became_unsafe_before_commit(project):
    target = preflight_for(project, project.result_out)
    moved = project.out.parent / "moved-out"
    project.out.rename(moved)
    link = project.out
    link.symlink_to(moved)
    try:
        with pytest.raises(HandoffError, match="parent directory is unsafe") as excinfo:
            commit(target, PAYLOAD)
        assert excinfo.value.reason == HANDOFF_WRITE_FAILED
    finally:
        link.unlink()
        moved.rename(project.out)


def test_commit_rejects_when_the_underlying_write_fails_durably(project, monkeypatch):
    def failing_atomic_write(path, data, *, mode=0o600, replace=True, dir_fd=None):
        raise OSError("simulated durable write failure")

    monkeypatch.setattr(handoff_io, "atomic_write", failing_atomic_write)
    target = preflight_for(project, project.result_out)
    with pytest.raises(HandoffError, match="could not be written durably") as excinfo:
        commit(target, PAYLOAD)
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED
    assert not (project.out / "result.json").exists()


def test_commit_write_stays_inside_the_preflight_verified_parent_when_swapped(project, monkeypatch, tmp_path):
    real_atomic_write = safe_io.atomic_write
    swapped = {"done": False}

    def swapping_atomic_write(path, data, *, mode=0o600, replace=True, dir_fd=None):
        if not swapped["done"]:
            swapped["done"] = True
            replacement = tmp_path / "swapped-in-out"
            replacement.mkdir()
            moved = tmp_path / "moved-out"
            project.out.rename(moved)
            replacement.rename(project.out)
        return real_atomic_write(path, data, mode=mode, replace=replace, dir_fd=dir_fd)

    monkeypatch.setattr(handoff_io, "atomic_write", swapping_atomic_write)
    target = preflight_for(project, project.result_out)
    assert commit(target, PAYLOAD) == "created"
    assert not (project.out / "result.json").exists()
    assert (tmp_path / "moved-out" / "result.json").read_bytes() == PAYLOAD


def test_commit_rejects_a_destination_that_vanished_after_preflight(project):
    existing = project.out / "result.json"
    existing.write_bytes(PAYLOAD)
    existing.chmod(0o600)
    target = preflight_for(project, project.result_out)
    assert target.existing_sha256 is not None
    existing.unlink()
    with pytest.raises(HandoffError, match="destination changed after preflight") as excinfo:
        commit(target, PAYLOAD)
    assert not existing.exists()
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_detects_parent_drift_after_preflight(project, tmp_path):
    target = preflight_for(project, project.result_out)
    replacement = tmp_path / "replacement-out"
    replacement.mkdir()
    project.out.rename(tmp_path / "moved-out")
    replacement.rename(project.out)
    with pytest.raises(HandoffError, match="parent changed after preflight") as excinfo:
        commit(target, PAYLOAD)
    assert not (project.out / "result.json").exists()
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_detects_a_parent_mode_change_with_dev_and_ino_unchanged(project):
    target = preflight_for(project, project.result_out)
    project.out.chmod(0o700)
    try:
        with pytest.raises(HandoffError, match="parent changed after preflight") as excinfo:
            commit(target, PAYLOAD)
        assert not (project.out / "result.json").exists()
        assert excinfo.value.reason == HANDOFF_WRITE_FAILED
    finally:
        project.out.chmod(0o755)


def test_commit_rejects_non_bytes_payload(project):
    target = preflight_for(project, project.result_out)
    with pytest.raises(HandoffError, match="payload must be bytes") as excinfo:
        commit(target, '{"artifact_type":"s3-upload.result"}')
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_commit_rejects_a_payload_over_the_size_limit_before_writing_any_bytes(project):
    assert MAX_ARTIFACT_BYTES == 262144
    target = preflight_for(project, project.result_out)
    oversize = b"x" * (262144 + 1)
    with pytest.raises(HandoffError, match="payload is too large") as excinfo:
        commit(target, oversize)
    assert not (project.out / "result.json").exists()
    assert excinfo.value.reason == HANDOFF_WRITE_FAILED


def test_handoff_error_reasons_are_exactly_the_two_declared_codes(project):
    def trigger_preflight_protected():
        with pytest.raises(HandoffError) as excinfo:
            preflight_for(project, str(project.root / ".env"))
        return excinfo.value.reason

    def trigger_preflight_existing_unsafe():
        other = project.out / "elsewhere.json"
        other.write_bytes(PAYLOAD)
        other.chmod(0o600)
        aliased = str(project.out / "hardlink-check.json")
        os.link(str(other), aliased)
        with pytest.raises(HandoffError) as excinfo:
            preflight_for(project, aliased)
        return excinfo.value.reason

    def trigger_commit_immutable():
        target = preflight_for(project, str(project.out / "immutable-check.json"))
        commit(target, PAYLOAD)
        with pytest.raises(HandoffError) as excinfo:
            commit(target, b"other")
        return excinfo.value.reason

    def trigger_commit_bad_payload():
        target = preflight_for(project, str(project.out / "bad-payload-check.json"))
        with pytest.raises(HandoffError) as excinfo:
            commit(target, "not-bytes")
        return excinfo.value.reason

    reasons = set()
    for scenario in (
        trigger_preflight_protected,
        trigger_preflight_existing_unsafe,
        trigger_commit_immutable,
        trigger_commit_bad_payload,
    ):
        reasons.add(scenario())

    assert reasons == {HANDOFF_UNSAFE, HANDOFF_WRITE_FAILED}


def test_handoff_reason_codes_are_declared_delivery_blocking_reasons():
    assert HANDOFF_UNSAFE == "handoff_unsafe"
    assert HANDOFF_WRITE_FAILED == "handoff_write_failed"
    assert {HANDOFF_UNSAFE, HANDOFF_WRITE_FAILED} <= set(delivery_schema.BLOCKING_REASONS)
