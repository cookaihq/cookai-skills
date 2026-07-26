import os
import stat

import pytest

from handoff_io import MAX_ARTIFACT_BYTES, HandoffError, HandoffTarget, commit, preflight


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
    with pytest.raises(HandoffError):
        preflight_for(project, str(project.root / relative))


def test_preflight_rejects_a_destination_inside_the_config_home(project):
    with pytest.raises(HandoffError):
        preflight_for(project, str(project.home / "result.json"))


def test_preflight_rejects_a_symlinked_parent(project):
    link = project.root / "linked-out"
    link.symlink_to(project.out)
    with pytest.raises(HandoffError):
        preflight_for(project, str(link / "result.json"))


def test_preflight_rejects_a_group_writable_parent(project):
    project.out.chmod(0o775)
    try:
        with pytest.raises(HandoffError):
            preflight_for(project, project.result_out)
    finally:
        project.out.chmod(0o755)


def test_preflight_rejects_a_symlinked_destination(project):
    other = project.out / "elsewhere.json"
    other.write_bytes(PAYLOAD)
    other.chmod(0o600)
    link = project.out / "result.json"
    link.symlink_to(other)
    with pytest.raises(HandoffError, match="destination is unsafe"):
        preflight_for(project, str(link))


def test_preflight_rejects_a_hardlink_alias(project):
    other = project.out / "elsewhere.json"
    other.write_bytes(PAYLOAD)
    other.chmod(0o600)
    os.link(str(other), project.result_out)
    with pytest.raises(HandoffError, match="hardlink alias"):
        preflight_for(project, project.result_out)


def test_preflight_rejects_a_world_readable_existing_destination(project):
    existing = project.out / "result.json"
    existing.write_bytes(PAYLOAD)
    existing.chmod(0o644)
    with pytest.raises(HandoffError, match="unsafe ownership or mode"):
        preflight_for(project, project.result_out)


def test_preflight_rejects_a_destination_that_aliases_the_source(project):
    project.source.chmod(0o600)
    info = project.source.stat()
    with pytest.raises(HandoffError, match="aliases the upload source"):
        preflight_for(project, str(project.source), source_identity=(info.st_dev, info.st_ino))


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
    with pytest.raises(HandoffError, match="immutable"):
        commit(target, b'{"artifact_type":"s3-upload.ack"}')
    assert (project.out / "result.json").read_bytes() == PAYLOAD


def test_commit_detects_a_destination_created_after_preflight(project):
    target = preflight_for(project, project.result_out)
    intruder = project.out / "result.json"
    intruder.write_bytes(b"intruder")
    intruder.chmod(0o600)
    with pytest.raises(HandoffError):
        commit(target, PAYLOAD)
    assert intruder.read_bytes() == b"intruder"


def test_commit_detects_parent_drift_after_preflight(project, tmp_path):
    target = preflight_for(project, project.result_out)
    replacement = tmp_path / "replacement-out"
    replacement.mkdir()
    project.out.rename(tmp_path / "moved-out")
    replacement.rename(project.out)
    with pytest.raises(HandoffError, match="parent changed after preflight"):
        commit(target, PAYLOAD)
    assert not (project.out / "result.json").exists()


def test_commit_rejects_non_bytes_payload(project):
    target = preflight_for(project, project.result_out)
    with pytest.raises(HandoffError):
        commit(target, '{"artifact_type":"s3-upload.result"}')


def test_commit_rejects_a_payload_over_the_size_limit_before_writing_any_bytes(project):
    assert MAX_ARTIFACT_BYTES == 262144
    target = preflight_for(project, project.result_out)
    oversize = b"x" * (262144 + 1)
    with pytest.raises(HandoffError):
        commit(target, oversize)
    assert not (project.out / "result.json").exists()
