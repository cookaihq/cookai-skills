import json
import os
import stat

import pytest

import handoff_io
from conftest import CALLER
from delivery_schema import body_of, parse_typed
from delivery_workflow import BOUNDARIES, publish
from plan_store import PlanStore, build_plan_body, new_plan_id
from s3 import Response


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(200)


def issue_plan(project, dry_run, snapshot, digest):
    store = PlanStore(str(project.state_root))
    body = build_plan_body(
        dry_run_plan=dry_run.plan, source_snapshot=dry_run.source.snapshot,
        target_contract=snapshot, target_contract_hash=digest, caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
        state_root=str(project.state_root), recovery_out=project.recovery_out,
        result_out=project.result_out, plan_id=new_plan_id(),
    )
    return store, store.issue(body, source_path=str(project.source),
                              soft_max_bytes=1048576, created_at=CREATED_AT)


class HardCrash(BaseException):
    pass


def operation_record_path(project, plan_id):
    return project.state_root / "plans" / plan_id / "operation.json"


def operation_record(project, plan_id):
    return json.loads(operation_record_path(project, plan_id).read_text(encoding="utf-8"))


def checkpoint_path(project, checkpoint_id):
    return project.state_root / "checkpoints" / (str(checkpoint_id) + ".json")


def run(project, resolved, store, token, transport, **overrides):
    kwargs = dict(
        resolved=resolved, store=store, token=token, transport=transport,
        project_root=str(project.root), config_home=str(project.home),
        caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
    )
    kwargs.update(overrides)
    return publish(**kwargs)


def durable_state(project, plan_id):
    checkpoints = project.state_root / "checkpoints"
    return {
        "checkpoints": sorted(
            item.name for item in
            (checkpoints.iterdir() if checkpoints.is_dir() else ())
            if item.suffix == ".json"
        ),
        "operation_record": os.path.exists(
            project.state_root / "plans" / plan_id / "operation.json"
        ),
        "recovery_written": os.path.exists(project.recovery_out),
        "result_written": os.path.exists(project.result_out),
    }


def test_boundary_vocabulary_is_locked():
    assert BOUNDARIES == (
        "plan_durable", "revalidated", "checkpoint_durable",
        "before_recovery_fsync", "after_recovery_fsync",
        "before_request", "after_request",
        "before_result_fsync", "after_result_fsync",
        "before_stdout", "before_ack", "before_cleanup",
    )


def test_boundaries_fire_in_the_contract_order(project, resolved, dry_run, snapshot, contract_digest):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    seen = []
    run(project, resolved, store, issued.token, Recorder(), on_boundary=seen.append)
    assert seen == [
        "revalidated", "checkpoint_durable", "before_recovery_fsync",
        "after_recovery_fsync", "before_request", "after_request",
        "before_result_fsync", "after_result_fsync", "before_stdout",
    ]
    positions = [BOUNDARIES.index(name) for name in seen]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_internal_checkpoint_exists_before_the_recovery_descriptor(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    observed = {}

    def observe(name):
        if name == "before_recovery_fsync":
            observed.update(durable_state(project, issued.plan_id))

    run(project, resolved, store, issued.token, Recorder(), on_boundary=observe)
    assert len(observed["checkpoints"]) == 1
    assert observed["operation_record"] is True
    assert observed["recovery_written"] is False


def test_internal_checkpoint_precedes_the_descriptor_at_the_commit_call(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    seen = []
    real = handoff_io.commit

    def spy(target, data):
        seen.append((target.path, durable_state(project, issued.plan_id)))
        return real(target, data)

    monkeypatch.setattr("delivery_workflow.commit", spy)
    run(project, resolved, store, issued.token, Recorder())
    assert [entry[0] for entry in seen] == [project.recovery_out, project.result_out]
    at_recovery = seen[0][1]
    assert len(at_recovery["checkpoints"]) == 1
    assert at_recovery["operation_record"] is True
    assert at_recovery["recovery_written"] is False
    assert at_recovery["result_written"] is False
    assert seen[1][1]["recovery_written"] is True


def test_recovery_descriptor_is_durable_before_the_first_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    observed = {}

    def observe(name):
        if name == "before_request":
            observed["recovery"] = open(project.recovery_out, "rb").read()

    raw = Recorder()

    def watched(method, url, headers, body):
        observed["at_send"] = durable_state(project, issued.plan_id)
        observed["bytes_at_send"] = (
            open(project.recovery_out, "rb").read()
            if os.path.exists(project.recovery_out) else None
        )
        return raw(method, url, headers, body)

    run(project, resolved, store, issued.token, watched, on_boundary=observe)
    descriptor = parse_typed(observed["recovery"].decode("utf-8"),
                             expected_type="s3-upload.recovery-descriptor")
    assert body_of(descriptor)["plan_id"] == issued.plan_id
    assert observed["at_send"]["recovery_written"] is True
    assert observed["at_send"]["operation_record"] is True
    assert len(observed["at_send"]["checkpoints"]) == 1
    assert observed["at_send"]["result_written"] is False
    assert observed["bytes_at_send"] == observed["recovery"]
    assert open(project.recovery_out, "rb").read() == observed["bytes_at_send"]
    assert len(raw.calls) == 1


def test_recovery_descriptor_contains_no_secret_or_private_checkpoint(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    raw = open(project.recovery_out, "r", encoding="utf-8").read()
    assert "project-secret-value" not in raw
    assert "PROJECTKEY1234" not in raw
    assert "images-key" not in raw
    assert "checkpoints" not in raw
    assert outcome.checkpoint_id not in raw
    assert issued.token not in raw
    assert set(json.loads(raw)) == {
        "artifact_type", "schema_version", "allowed_actions", "object_key", "operation",
        "operation_id", "plan_hash", "plan_id", "recovery_id", "recovery_state",
        "result_out", "retry_safe", "root_recovery_id", "target_contract_hash",
    }


def test_recovery_write_failure_sends_zero_requests(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()

    def explode(target, data):
        raise handoff_io.HandoffError("injected recovery write failure",
                                      reason=handoff_io.HANDOFF_WRITE_FAILED)

    monkeypatch.setattr("delivery_workflow.commit", explode)
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert body["retry_safe"] is True
    assert not os.path.exists(project.recovery_out)
    assert durable_state(project, issued.plan_id)["operation_record"] is False


def test_recovery_parent_drift_after_preflight_sends_zero_requests(
    project, resolved, dry_run, snapshot, contract_digest, tmp_path
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()

    def swap(name):
        if name == "checkpoint_durable":
            replacement = tmp_path / "swapped-out"
            replacement.mkdir()
            project.out.rename(tmp_path / "detached-out")
            replacement.rename(project.out)

    outcome = run(project, resolved, store, issued.token, raw, on_boundary=swap)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body_of(outcome.result)["blocking_reasons"] == ["handoff_write_failed"]
    assert body_of(outcome.result)["retry_safe"] is True
    assert not os.path.exists(project.recovery_out)
    assert durable_state(project, issued.plan_id)["operation_record"] is False


def test_recovery_out_is_immutable_and_a_second_publish_replays(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = Recorder()
    original_outcome = run(project, resolved, store, issued.token, first)
    original = open(project.recovery_out, "rb").read()
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert len(first.calls) == 1
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert open(project.recovery_out, "rb").read() == original
    assert body_of(outcome.result) == body_of(original_outcome.result)


def test_a_retry_after_an_in_process_descriptor_failure_uploads(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = Recorder()

    def explode(target, data):
        raise handoff_io.HandoffError("injected recovery write failure",
                                      reason=handoff_io.HANDOFF_WRITE_FAILED)

    monkeypatch.setattr("delivery_workflow.commit", explode)
    crashed = body_of(run(project, resolved, store, issued.token, first).result)
    assert first.calls == []
    assert crashed["recovery_state"] == "known_not_applied"
    assert crashed["retry_safe"] is True
    assert crashed["allowed_actions"] == ["inspect", "publish"]
    assert durable_state(project, issued.plan_id)["operation_record"] is False
    assert not os.path.exists(project.recovery_out)

    monkeypatch.undo()
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert len(second.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert body["allowed_actions"] == ["inspect", "ack"]
    assert body["retry_safe"] is False
    assert durable_state(project, issued.plan_id)["operation_record"] is True
    assert body_of(parse_typed(open(project.recovery_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.recovery-descriptor"))["plan_id"] == issued.plan_id
    assert body_of(parse_typed(open(project.result_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.result")) == body


def test_a_hard_crash_between_the_operation_record_and_the_descriptor_stays_conservative(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = Recorder()

    def die(name):
        if name == "before_recovery_fsync":
            raise HardCrash("the process is gone before the descriptor is durable")

    with pytest.raises(HardCrash):
        run(project, resolved, store, issued.token, first, on_boundary=die)
    assert first.calls == []
    assert durable_state(project, issued.plan_id)["operation_record"] is True
    assert not os.path.exists(project.recovery_out)

    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["retry_safe"] is False
    assert body_of(parse_typed(open(project.result_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.result")) == body


@pytest.mark.parametrize("damage", ["corrupt", "unreadable"])
def test_an_unreadable_operation_record_fails_closed(
    project, resolved, dry_run, snapshot, contract_digest, damage
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    path = operation_record_path(project, issued.plan_id)
    if damage == "corrupt":
        path.write_bytes(b"{not json at all")
        path.chmod(0o600)
    else:
        path.write_bytes(b"{}")
        path.chmod(0o644)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["blocking_reasons"] == []
    assert not os.path.exists(project.recovery_out)
    assert os.path.exists(path)


def test_a_descriptor_that_outlived_a_rolled_back_operation_record_replays(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    real = handoff_io.commit
    failed = []

    def create_then_fail(target, data):
        if target.path == project.recovery_out and not failed:
            real(target, data)
            failed.append(target.path)
            raise handoff_io.HandoffError("injected post-create fsync failure",
                                          reason=handoff_io.HANDOFF_WRITE_FAILED)
        return real(target, data)

    monkeypatch.setattr("delivery_workflow.commit", create_then_fail)
    first = Recorder()
    crashed = body_of(run(project, resolved, store, issued.token, first).result)
    assert first.calls == []
    assert crashed["recovery_state"] == "known_not_applied"
    descriptor = open(project.recovery_out, "rb").read()
    assert durable_state(project, issued.plan_id)["operation_record"] is False

    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert open(project.recovery_out, "rb").read() == descriptor
    assert durable_state(project, issued.plan_id)["operation_record"] is True
    assert body_of(parse_typed(open(project.result_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.result")) == body


@pytest.mark.parametrize("wedge", ["read_only_plan_directory", "detached_plan_directory"])
def test_a_rollback_that_itself_fails_answers_conservatively_twice(
    project, resolved, dry_run, snapshot, contract_digest, tmp_path, wedge
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    plan_dir = project.state_root / "plans" / issued.plan_id
    detached = tmp_path / "detached-plan"
    out_mode = stat.S_IMODE(os.stat(project.out).st_mode)
    plan_mode = stat.S_IMODE(os.stat(plan_dir).st_mode)

    def wound(name):
        if name == "checkpoint_durable":
            os.chmod(project.out, 0o500)
        elif name == "before_recovery_fsync":
            if wedge == "read_only_plan_directory":
                os.chmod(plan_dir, 0o500)
            else:
                plan_dir.rename(detached)

    first = Recorder()
    outcome = run(project, resolved, store, issued.token, first, on_boundary=wound)
    os.chmod(project.out, out_mode)
    if wedge == "read_only_plan_directory":
        os.chmod(plan_dir, plan_mode)
    else:
        detached.rename(plan_dir)
    body = body_of(outcome.result)
    assert first.calls == []
    assert outcome.transport_calls == 0
    assert not os.path.exists(project.recovery_out)
    assert durable_state(project, issued.plan_id)["operation_record"] is True
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    surviving = operation_record(project, issued.plan_id)
    assert checkpoint_path(project, surviving["checkpoint_id"]).exists()

    second = Recorder()
    replay = body_of(run(project, resolved, store, issued.token, second).result)
    assert second.calls == []
    assert replay["recovery_state"] == "in_flight_unknown"
    assert replay["retry_safe"] is False
    assert replay["allowed_actions"] == ["inspect", "reconcile"]
    assert operation_record(project, issued.plan_id) == surviving
    assert checkpoint_path(project, surviving["checkpoint_id"]).exists()


def test_repeated_rollbacks_do_not_accumulate_orphan_checkpoints(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    real = handoff_io.commit
    attempts = []

    def explode_until_the_fifth(target, data):
        if target.path == project.recovery_out and len(attempts) < 4:
            attempts.append(target.path)
            raise handoff_io.HandoffError("injected recovery write failure",
                                          reason=handoff_io.HANDOFF_WRITE_FAILED)
        return real(target, data)

    monkeypatch.setattr("delivery_workflow.commit", explode_until_the_fifth)
    for _ in range(4):
        outcome = run(project, resolved, store, issued.token, Recorder())
        body = body_of(outcome.result)
        assert body["recovery_state"] == "known_not_applied"
        assert body["retry_safe"] is True
        assert outcome.transport_calls == 0
        assert durable_state(project, issued.plan_id)["checkpoints"] == []
        assert durable_state(project, issued.plan_id)["operation_record"] is False
        assert outcome.checkpoint_id is None

    last = Recorder()
    outcome = run(project, resolved, store, issued.token, last)
    assert len(last.calls) == 1
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"
    state = durable_state(project, issued.plan_id)
    assert len(state["checkpoints"]) == 1
    assert state["operation_record"] is True
    assert state["checkpoints"] == [
        operation_record(project, issued.plan_id)["checkpoint_id"] + ".json"
    ]


def test_recovery_and_operation_identities_are_derived_from_the_plan(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    project.source.write_bytes(b"different!!")
    first = body_of(run(project, resolved, store, issued.token, Recorder()).result)
    second = body_of(run(project, resolved, store, issued.token, Recorder()).result)
    assert first["blocking_reasons"] == ["source_drift"]
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)
    assert durable_state(project, issued.plan_id)["operation_record"] is False
    assert first["operation_id"] == second["operation_id"]
    assert first["recovery_id"] == second["recovery_id"]
    assert first["operation_id"] != first["recovery_id"]
    assert len(first["operation_id"]) == 32


def test_the_descriptor_and_the_result_agree_on_the_operation_chain(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    run(project, resolved, store, issued.token, Recorder())
    descriptor = body_of(parse_typed(
        open(project.recovery_out, "r", encoding="utf-8").read(),
        expected_type="s3-upload.recovery-descriptor",
    ))
    result = body_of(parse_typed(
        open(project.result_out, "r", encoding="utf-8").read(),
        expected_type="s3-upload.result",
    ))
    assert descriptor["operation_id"] == result["operation_id"]
    assert descriptor["recovery_id"] == result["recovery_id"]
    assert descriptor["root_recovery_id"] == result["root_recovery_id"]
    assert descriptor["plan_id"] == result["plan_id"] == issued.plan_id
    assert descriptor["plan_hash"] == result["plan_hash"]
    assert descriptor["target_contract_hash"] == result["target_contract_hash"]


def test_already_handed_off_is_disjoint_from_the_handoff_error_hierarchy():
    import delivery_workflow
    import plan_store

    assert not issubclass(delivery_workflow.AlreadyHandedOff, handoff_io.HandoffError)
    assert not issubclass(delivery_workflow.AlreadyHandedOff, plan_store.PlanStoreError)
    assert not issubclass(delivery_workflow.AlreadyHandedOff, ValueError)
    assert issubclass(delivery_workflow.AlreadyHandedOff, delivery_workflow.WorkflowError)
