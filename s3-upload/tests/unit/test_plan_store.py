import hashlib
import os
import stat

import pytest

import plan_store
from conftest import CALLER, SOURCE_BYTES
from delivery_schema import (
    DeliverySchemaError, body_of, build_typed, parse_typed, serialize_artifact,
)
from strict_json import canonicalize
from plan_store import (
    ConsumedPlan,
    IssuedPlan,
    PlanStore,
    PlanStoreError,
    build_plan_body,
    new_plan_id,
    plan_hash,
    token_digest,
)


CREATED_AT = "2026-07-26T00:00:00Z"


def make_body(project, dry_run, snapshot, contract_digest, **overrides):
    body = build_plan_body(
        dry_run_plan=dry_run.plan,
        source_snapshot=dry_run.source.snapshot,
        target_contract=snapshot,
        target_contract_hash=contract_digest,
        caller=CALLER,
        executable_path="/usr/bin/python3",
        cwd=str(project.root),
        state_root=str(project.state_root),
        recovery_out=project.recovery_out,
        result_out=project.result_out,
        plan_id=new_plan_id(),
    )
    body.update(overrides)
    return body


def issue(project, dry_run, snapshot, contract_digest, **overrides):
    store = PlanStore(str(project.state_root))
    body = make_body(project, dry_run, snapshot, contract_digest, **overrides)
    return store, store.issue(
        body,
        source_path=str(project.source),
        soft_max_bytes=1048576,
        created_at=CREATED_AT,
    )


def write_tampered_record(project, plan_id, record, *, object_key):
    body = dict(body_of(record["plan"]))
    body["object_key"] = object_key
    tampered = dict(record, plan=build_typed("s3-upload.plan", body))
    path = project.state_root / "plans" / plan_id / "record.json"
    path.write_bytes(canonicalize(tampered))
    path.chmod(0o600)


def test_new_plan_id_is_unguessable_hex():
    values = {new_plan_id() for _ in range(64)}
    assert len(values) == 64
    for value in values:
        assert len(value) == 32
        assert all(character in "0123456789abcdef" for character in value)


def test_plan_body_is_a_valid_typed_artifact(project, dry_run, snapshot, contract_digest):
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    assert issued.artifact["artifact_type"] == "s3-upload.plan"
    raw = serialize_artifact(issued.artifact).decode("utf-8")
    assert parse_typed(raw, expected_type="s3-upload.plan") == issued.artifact


def test_issue_returns_a_token_for_an_executable_plan(project, dry_run, snapshot, contract_digest):
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    assert issued.artifact["executable"] is True
    assert isinstance(issued.token, str)
    assert issued.token.startswith(issued.plan_id + ".")
    assert len(issued.token) > len(issued.plan_id) + 32


def test_spool_holds_the_frozen_source_bytes(project, dry_run, snapshot, contract_digest):
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    spool = project.state_root / "plans" / issued.plan_id / "spool"
    assert spool.read_bytes() == SOURCE_BYTES
    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    assert spool.stat().st_nlink == 1


def test_plan_directory_is_owned_and_0700(project, dry_run, snapshot, contract_digest):
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    directory = project.state_root / "plans" / issued.plan_id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert directory.stat().st_uid == os.geteuid()
    assert stat.S_IMODE((project.state_root / "plans").stat().st_mode) == 0o700


def test_record_never_stores_the_token_itself(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    raw = (project.state_root / "plans" / issued.plan_id / "record.json").read_text()
    assert issued.token not in raw
    assert issued.token.split(".", 1)[1] not in raw
    assert token_digest(issued.plan_id, issued.token) in raw


def test_record_plan_copy_carries_no_token(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    record = store.load_record(issued.plan_id)
    assert body_of(record["plan"])["plan_token"] is None
    assert issued.artifact["plan_token"] == issued.token


def test_plan_hash_ignores_token_and_hash_fields(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    stored = body_of(store.load_record(issued.plan_id)["plan"])
    handed = body_of(issued.artifact)
    assert stored["plan_hash"] == handed["plan_hash"]
    assert plan_hash(stored) == plan_hash(handed)


def test_plan_hash_changes_when_any_bound_fact_changes(project, dry_run, snapshot, contract_digest):
    body = make_body(project, dry_run, snapshot, contract_digest)
    assert plan_hash(body) != plan_hash(dict(body, object_key="images/other.png"))
    assert plan_hash(body) != plan_hash(dict(body, caller="vi-pdf2md"))
    assert plan_hash(body) != plan_hash(dict(body, state_root="/elsewhere"))


def test_build_plan_body_deep_copies_the_target_contract_snapshot(
    project, dry_run, snapshot, contract_digest
):
    body = make_body(project, dry_run, snapshot, contract_digest)
    original_bucket = snapshot["bucket"]
    snapshot["bucket"] = "attacker-bucket"
    assert body["target_contract"]["bucket"] == original_bucket


def test_spool_rejects_a_source_larger_than_the_soft_limit(project, dry_run, snapshot, contract_digest):
    store = PlanStore(str(project.state_root))
    body = make_body(project, dry_run, snapshot, contract_digest)
    with pytest.raises(PlanStoreError):
        store.issue(body, source_path=str(project.source), soft_max_bytes=4,
                    created_at=CREATED_AT)
    assert not (project.state_root / "plans" / body["plan_id"] / "spool").exists()


def test_spool_rejects_a_source_that_does_not_match_the_planned_identity(
    project, dry_run, snapshot, contract_digest
):
    store = PlanStore(str(project.state_root))
    body = make_body(project, dry_run, snapshot, contract_digest)
    body["source"] = dict(body["source"], sha256="0" * 64)
    with pytest.raises(PlanStoreError):
        store.issue(body, source_path=str(project.source), soft_max_bytes=1048576,
                    created_at=CREATED_AT)


def test_load_record_rejects_a_record_whose_plan_hash_no_longer_matches_its_body(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    record = store.load_record(issued.plan_id)
    write_tampered_record(project, issued.plan_id, record, object_key="images/other.png")
    with pytest.raises(PlanStoreError):
        store.load_record(issued.plan_id)


def test_reissuing_the_same_plan_id_raises_plan_store_error(
    project, dry_run, snapshot, contract_digest
):
    store = PlanStore(str(project.state_root))
    body = make_body(project, dry_run, snapshot, contract_digest)
    store.issue(body, source_path=str(project.source), soft_max_bytes=1048576,
               created_at=CREATED_AT)
    with pytest.raises(PlanStoreError):
        store.issue(body, source_path=str(project.source), soft_max_bytes=1048576,
                    created_at=CREATED_AT)


def test_consume_accepts_the_issued_token(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    consumed = store.consume(
        issued.token, caller=CALLER, executable_path="/usr/bin/python3",
        cwd=str(project.root), state_root=str(project.state_root),
    )
    assert isinstance(consumed, ConsumedPlan)
    assert consumed.state == "active"
    assert consumed.plan_id == issued.plan_id


@pytest.mark.parametrize("overrides", [
    {"caller": "vi-pdf2md"},
    {"executable_path": "/opt/other/python3"},
    {"cwd": "/tmp"},
])
def test_consume_rejects_replay_across_identity(project, dry_run, snapshot, contract_digest, overrides):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    kwargs = dict(
        caller=CALLER, executable_path="/usr/bin/python3",
        cwd=str(project.root), state_root=str(project.state_root),
    )
    kwargs.update(overrides)
    with pytest.raises(PlanStoreError):
        store.consume(issued.token, **kwargs)


def test_consume_rejects_when_the_recorded_state_root_disagrees_with_the_store(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(
        project, dry_run, snapshot, contract_digest, state_root="/elsewhere/state-root",
    )
    with pytest.raises(PlanStoreError):
        store.consume(
            issued.token, caller=CALLER, executable_path="/usr/bin/python3",
            cwd=str(project.root), state_root=str(project.state_root),
        )


def test_consume_rejects_replay_against_another_state_root(project, dry_run, snapshot, contract_digest, tmp_path):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    other = tmp_path / "other-root"
    other.mkdir()
    with pytest.raises(PlanStoreError):
        store.consume(
            issued.token, caller=CALLER, executable_path="/usr/bin/python3",
            cwd=str(project.root), state_root=str(other),
        )


def test_consume_rejects_a_forged_secret(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    forged = issued.plan_id + ".forged-secret-value"
    with pytest.raises(PlanStoreError):
        store.consume(
            forged, caller=CALLER, executable_path="/usr/bin/python3",
            cwd=str(project.root), state_root=str(project.state_root),
        )


def test_consume_rejects_a_token_for_an_absent_record(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    (project.state_root / "plans" / issued.plan_id / "record.json").unlink()
    with pytest.raises(PlanStoreError):
        store.consume(
            issued.token, caller=CALLER, executable_path="/usr/bin/python3",
            cwd=str(project.root), state_root=str(project.state_root),
        )


def test_consume_takes_its_facts_only_from_the_on_disk_record_not_a_caller_supplied_artifact(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    record = store.load_record(issued.plan_id)
    write_tampered_record(project, issued.plan_id, record, object_key="images/attacker.png")
    with pytest.raises(PlanStoreError):
        store.consume(
            issued.token, caller=CALLER, executable_path="/usr/bin/python3",
            cwd=str(project.root), state_root=str(project.state_root),
        )


def test_spool_bytes_verifies_size_and_digest(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    source = body_of(issued.artifact)["source"]
    assert store.spool_bytes(
        issued.plan_id, expected_size=source["size"], expected_sha256=source["sha256"],
    ) == SOURCE_BYTES
    with pytest.raises(PlanStoreError):
        store.spool_bytes(issued.plan_id, expected_size=source["size"], expected_sha256="0" * 64)


def test_spool_bytes_rejects_a_tampered_spool(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    source = body_of(issued.artifact)["source"]
    spool = project.state_root / "plans" / issued.plan_id / "spool"
    spool.write_bytes(b"tampered!!!")
    with pytest.raises(PlanStoreError):
        store.spool_bytes(
            issued.plan_id, expected_size=source["size"], expected_sha256=source["sha256"],
        )


def make_operation_record(**overrides):
    value = {
        "checkpoint_id": "checkpoint-1",
        "operation_id": "operation-1",
        "recovery_id": "recovery-1",
        "result_out": "/tmp/result.json",
        "root_recovery_id": "recovery-1",
    }
    value.update(overrides)
    return value


def test_operation_record_is_absent_until_written(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    assert store.operation_record(issued.plan_id) is None


def test_write_operation_record_then_read_it_back(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    value = make_operation_record()
    store.write_operation_record(issued.plan_id, value)
    assert store.operation_record(issued.plan_id) == value


def test_write_operation_record_is_idempotent_for_the_same_value(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    value = make_operation_record()
    store.write_operation_record(issued.plan_id, value)
    store.write_operation_record(issued.plan_id, value)
    assert store.operation_record(issued.plan_id) == value


def test_write_operation_record_rejects_a_conflicting_rewrite(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    store.write_operation_record(issued.plan_id, make_operation_record())
    with pytest.raises(PlanStoreError):
        store.write_operation_record(issued.plan_id, make_operation_record(operation_id="operation-2"))


def test_acknowledge_then_consume_returns_the_tombstone(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    result_hash = "sha256:" + "0" * 64
    tombstone = store.acknowledge(issued.plan_id, result_hash=result_hash)
    assert tombstone["acknowledged"] is True
    assert tombstone["plan_id"] == issued.plan_id
    assert tombstone["result_hash"] == result_hash
    consumed = store.consume(
        issued.token, caller=CALLER, executable_path="/usr/bin/python3",
        cwd=str(project.root), state_root=str(project.state_root),
    )
    assert consumed.state == "acknowledged"
    assert consumed.record is None
    assert consumed.tombstone == tombstone
    directory = project.state_root / "plans" / issued.plan_id
    assert not (directory / "spool").exists()
    assert not (directory / "record.json").exists()


def test_repeated_invalidate_returns_the_existing_tombstone(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    first = store.invalidate(issued.plan_id)
    second = store.invalidate(issued.plan_id)
    assert first == second
    assert first["acknowledged"] is False
    assert first["result_hash"] is None


def test_invalidate_after_acknowledge_does_not_overwrite_the_tombstone(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    result_hash = "sha256:" + "1" * 64
    acknowledged = store.acknowledge(issued.plan_id, result_hash=result_hash)
    invalidated = store.invalidate(issued.plan_id)
    assert invalidated == acknowledged
    assert invalidated["acknowledged"] is True
    assert invalidated["result_hash"] == result_hash


def test_prepare_wraps_a_guard_write_failure_as_plan_store_error(
    project, dry_run, snapshot, contract_digest, monkeypatch
):
    store = PlanStore(str(project.state_root))
    body = make_body(project, dry_run, snapshot, contract_digest)

    def failing_atomic_write(path, data, *, mode=0o600, replace=True):
        raise OSError("disk full")

    monkeypatch.setattr(plan_store, "atomic_write", failing_atomic_write)
    with pytest.raises(PlanStoreError):
        store.issue(body, source_path=str(project.source), soft_max_bytes=1048576,
                    created_at=CREATED_AT)


def test_plans_directory_carries_a_git_ignore_guard(project, dry_run, snapshot, contract_digest):
    issue(project, dry_run, snapshot, contract_digest)
    guard = project.state_root / "plans" / ".gitignore"
    assert guard.read_bytes() == b"*\n!.gitignore\n"
    assert stat.S_IMODE(guard.stat().st_mode) == 0o600


def test_finish_wraps_a_directory_open_failure_as_plan_store_error(
    project, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue(project, dry_run, snapshot, contract_digest)

    def failing_open_directory(path):
        raise OSError("boom")

    monkeypatch.setattr(plan_store, "open_directory", failing_open_directory)
    with pytest.raises(PlanStoreError):
        store.acknowledge(issued.plan_id, result_hash="sha256:" + "0" * 64)


def test_lock_validates_the_name_before_touching_the_filesystem(tmp_path):
    state_root = tmp_path / "state-root"
    store = PlanStore(str(state_root))
    with pytest.raises(PlanStoreError):
        with store.lock("../escape"):
            pass
    assert not state_root.exists()


def test_lock_is_exclusive(project, dry_run, snapshot, contract_digest):
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    name = "plan-" + issued.plan_id
    with store.lock(name):
        with pytest.raises(PlanStoreError):
            with PlanStore(str(project.state_root)).lock(name):
                pass


def test_token_is_generated_only_after_the_spool_is_durably_fsynced(
    project, dry_run, snapshot, contract_digest, monkeypatch
):
    events = []
    real_spool = plan_store.PlanStore._spool

    def spool_spy(self, *args, **kwargs):
        result = real_spool(self, *args, **kwargs)
        events.append("spool_fsynced")
        return result

    real_token_urlsafe = plan_store.secrets.token_urlsafe

    def token_urlsafe_spy(count):
        events.append("token_generated")
        return real_token_urlsafe(count)

    monkeypatch.setattr(plan_store.PlanStore, "_spool", spool_spy)
    monkeypatch.setattr(plan_store.secrets, "token_urlsafe", token_urlsafe_spy)
    store, issued = issue(project, dry_run, snapshot, contract_digest)
    assert events == ["spool_fsynced", "token_generated"]
    assert issued.token is not None


def test_spool_and_record_are_fsynced_before_the_token_reaches_the_caller(
    project, dry_run, snapshot, contract_digest, monkeypatch
):
    events = []
    real_fsync = plan_store.os.fsync
    real_token = plan_store.secrets.token_urlsafe

    def fsync_spy(fd):
        info = os.fstat(fd)
        events.append(("dir" if stat.S_ISDIR(info.st_mode) else "file", info.st_ino))
        return real_fsync(fd)

    def token_spy(count):
        events.append(("token", None))
        return real_token(count)

    monkeypatch.setattr(plan_store.os, "fsync", fsync_spy)
    monkeypatch.setattr(plan_store.secrets, "token_urlsafe", token_spy)
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    monkeypatch.undo()

    directory = project.state_root / "plans" / issued.plan_id
    spool_ino = (directory / "spool").stat().st_ino
    record_ino = (directory / "record.json").stat().st_ino
    dir_ino = directory.stat().st_ino
    token_at = events.index(("token", None))

    assert ("file", spool_ino) in events[:token_at]
    assert ("dir", dir_ino) in events[:token_at]
    assert ("file", record_ino) in events[token_at:]


def test_spool_digest_is_computed_from_bytes_read_during_the_copy(
    project, dry_run, snapshot, contract_digest, monkeypatch
):
    reads = []
    real_read = plan_store.os.read

    def read_spy(fd, count):
        chunk = real_read(fd, count)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(plan_store.os, "read", read_spy)
    _, issued = issue(project, dry_run, snapshot, contract_digest)
    assert reads == [len(SOURCE_BYTES), 0]
    assert body_of(issued.artifact)["source"]["sha256"] == hashlib.sha256(SOURCE_BYTES).hexdigest()
