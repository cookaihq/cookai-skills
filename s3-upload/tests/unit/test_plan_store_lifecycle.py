import json

import pytest

from conftest import CALLER, write_target
from delivery_schema import body_of
from plan_store import PlanStore, PlanStoreError, build_plan_body, new_plan_id
from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from resolver import resolve_target
from target_contract import contract_hash, contract_snapshot


CREATED_AT = "2026-07-26T00:00:00Z"


def blocked_inputs(project):
    # An asserted custom contract carries no conditional-write evidence, so
    # collision=reject still blocks there. (The aws-s3 preset used to be the
    # blocked example until the minimal caller contract enabled
    # ConditionalPutObject for the reviewed presets.)
    write_target(
        project,
        collision="reject",
        provider="custom",
        endpoint="https://storage.internal.example",
        addressing="path",
    )
    resolved = resolve_target(
        cwd=str(project.root), config_home=str(project.home), environ={},
        cli_target=None, cli_caller=CALLER, use_local_key=False,
    )
    dry_run = build_upload_dry_run(
        resolved=resolved, file_path=str(project.source), explicit_key=None,
        content_type=None, cache_control=None, content_disposition=None,
        presign_expires=None, reference_out=None, project_root=str(project.root),
        config_home=str(project.home), allow_insecure_http=False,
    )
    key = derive_contract_key(resolved.target)
    snapshot = contract_snapshot(
        target_ref=resolved.ref, config_scope=resolved.ref.scope,
        project_root=str(project.root), target=resolved.target,
        contract_key=key, registry=registry_for_target(resolved.target, key),
    )
    return dry_run, snapshot, contract_hash(snapshot)


def body_for(project, dry_run, snapshot, digest):
    return build_plan_body(
        dry_run_plan=dry_run.plan,
        source_snapshot=dry_run.source.snapshot,
        target_contract=snapshot,
        target_contract_hash=digest,
        caller=CALLER,
        executable_path="/usr/bin/python3",
        cwd=str(project.root),
        state_root=str(project.state_root),
        recovery_out=project.recovery_out,
        result_out=project.result_out,
        plan_id=new_plan_id(),
    )


def test_the_blocked_fixture_is_really_blocked(project):
    dry_run, _, _ = blocked_inputs(project)
    try:
        assert dry_run.plan["executable"] is False
        assert dry_run.plan["blocking_reasons"] == ["collision_capability_missing"]
    finally:
        dry_run.close()


def test_blocked_plan_creates_no_spool_and_no_token(project):
    dry_run, snapshot, digest = blocked_inputs(project)
    try:
        store = PlanStore(str(project.state_root))
        before = sorted(str(path) for path in project.state_root.rglob("*"))
        body = body_for(project, dry_run, snapshot, digest)
        issued = store.issue(body, source_path=str(project.source),
                             soft_max_bytes=1048576, created_at=CREATED_AT)
    finally:
        dry_run.close()
    after = sorted(str(path) for path in project.state_root.rglob("*"))
    assert issued.token is None
    assert issued.artifact["executable"] is False
    assert issued.artifact["plan_token"] is None
    assert issued.artifact["blocking_reasons"] == ["collision_capability_missing"]
    assert not (project.state_root / "plans" / issued.plan_id).exists()
    assert after == before


def test_blocked_plan_leaves_no_usable_local_credential(project):
    dry_run, snapshot, digest = blocked_inputs(project)
    try:
        store = PlanStore(str(project.state_root))
        body = body_for(project, dry_run, snapshot, digest)
        issued = store.issue(body, source_path=str(project.source),
                             soft_max_bytes=1048576, created_at=CREATED_AT)
    finally:
        dry_run.close()
    directory = project.state_root / "plans" / issued.plan_id
    assert not (directory / "record.json").exists()
    assert not (directory / "spool").exists()


def test_blocked_plan_is_still_a_complete_machine_artifact(project):
    dry_run, snapshot, digest = blocked_inputs(project)
    try:
        store = PlanStore(str(project.state_root))
        issued = store.issue(body_for(project, dry_run, snapshot, digest),
                             source_path=str(project.source), soft_max_bytes=1048576,
                             created_at=CREATED_AT)
    finally:
        dry_run.close()
    body = body_of(issued.artifact)
    assert body["target_contract_hash"] == digest
    assert body["required_capabilities"]
    assert body["object_key"].startswith("images/")


def issue_executable(project, dry_run, snapshot, digest):
    store = PlanStore(str(project.state_root))
    issued = store.issue(body_for(project, dry_run, snapshot, digest),
                         source_path=str(project.source), soft_max_bytes=1048576,
                         created_at=CREATED_AT)
    return store, issued


def test_acknowledge_removes_spool_and_record_but_keeps_a_tombstone(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    directory = project.state_root / "plans" / issued.plan_id
    tombstone = store.acknowledge(issued.plan_id, result_hash="sha256:" + "2" * 64)
    assert not (directory / "spool").exists()
    assert not (directory / "record.json").exists()
    assert (directory / "tombstone.json").exists()
    assert tombstone == {
        "acknowledged": True,
        "plan_id": issued.plan_id,
        "result_hash": "sha256:" + "2" * 64,
        "token_digest": tombstone["token_digest"],
    }


def test_tombstone_carries_only_the_minimum_fields(project, dry_run, snapshot, contract_digest):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    store.acknowledge(issued.plan_id, result_hash="sha256:" + "2" * 64)
    raw = (project.state_root / "plans" / issued.plan_id / "tombstone.json").read_text()
    assert set(json.loads(raw)) == {"acknowledged", "plan_id", "result_hash", "token_digest"}
    assert issued.token not in raw
    assert "project-secret-value" not in raw


def test_replaying_the_token_after_cleanup_sees_only_the_tombstone(
    project, dry_run, snapshot, contract_digest
):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    store.acknowledge(issued.plan_id, result_hash="sha256:" + "2" * 64)
    consumed = store.consume(issued.token, caller=CALLER,
                             executable_path="/usr/bin/python3", cwd=str(project.root),
                             state_root=str(project.state_root))
    assert consumed.state == "acknowledged"
    assert consumed.record is None
    assert consumed.tombstone["acknowledged"] is True
    assert not (project.state_root / "plans" / issued.plan_id / "spool").exists()


def test_acknowledge_is_idempotent(project, dry_run, snapshot, contract_digest):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    first = store.acknowledge(issued.plan_id, result_hash="sha256:" + "2" * 64)
    second = store.acknowledge(issued.plan_id, result_hash="sha256:" + "3" * 64)
    assert first == second
    assert second["result_hash"] == "sha256:" + "2" * 64


def test_a_foreign_token_cannot_read_a_tombstone(project, dry_run, snapshot, contract_digest):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    store.acknowledge(issued.plan_id, result_hash="sha256:" + "2" * 64)
    with pytest.raises(PlanStoreError):
        store.consume(issued.plan_id + ".not-the-secret", caller=CALLER,
                      executable_path="/usr/bin/python3", cwd=str(project.root),
                      state_root=str(project.state_root))


def test_invalidate_records_an_unacknowledged_tombstone(project, dry_run, snapshot, contract_digest):
    store, issued = issue_executable(project, dry_run, snapshot, contract_digest)
    tombstone = store.invalidate(issued.plan_id)
    assert tombstone["acknowledged"] is False
    assert tombstone["result_hash"] is None
    assert not (project.state_root / "plans" / issued.plan_id / "spool").exists()
    consumed = store.consume(issued.token, caller=CALLER,
                             executable_path="/usr/bin/python3", cwd=str(project.root),
                             state_root=str(project.state_root))
    assert consumed.state == "acknowledged"
    assert consumed.record is None
    assert consumed.tombstone["acknowledged"] is False
