import errno
import json
import os

import pytest

import delivery_workflow
import handoff_io
from artifacts import ArtifactError, CheckpointStore
from conftest import CALLER
from delivery_records import result_hash
from delivery_schema import body_of, parse_typed, serialize_artifact
from delivery_workflow import TransportSealed, publish
from operations import OperationError
from plan_store import PlanStore, build_plan_body, new_plan_id
from s3 import Response


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"


class Recorder:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(self.status)


def issue_plan(project, dry_run, snapshot, digest, **overrides):
    store = PlanStore(str(project.state_root))
    body = build_plan_body(
        dry_run_plan=dry_run.plan, source_snapshot=dry_run.source.snapshot,
        target_contract=snapshot, target_contract_hash=digest, caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
        state_root=str(project.state_root), recovery_out=project.recovery_out,
        result_out=project.result_out, plan_id=new_plan_id(),
    )
    body.update(overrides)
    return store, store.issue(body, source_path=str(project.source),
                              soft_max_bytes=1048576, created_at=CREATED_AT)


class HardCrash(BaseException):
    pass


def operation_record(project, plan_id):
    path = project.state_root / "plans" / plan_id / "operation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def checkpoints_on_disk(project):
    directory = project.state_root / "checkpoints"
    return sorted(
        item.name[: -len(".json")]
        for item in (directory.iterdir() if directory.is_dir() else ())
        if item.suffix == ".json"
    )


def durable(path, expected_type):
    return body_of(parse_typed(open(path, "r", encoding="utf-8").read(),
                               expected_type=expected_type))


def replace_durable_result(path, **changes):
    item = parse_typed(path.read_text(encoding="utf-8"),
                       expected_type="s3-upload.result")
    item.update(changes)
    if "result_hash" not in changes:
        item["result_hash"] = result_hash(body_of(item))
    path.write_bytes(serialize_artifact(item))
    path.chmod(0o600)
    return item


def run(project, resolved, store, token, transport, **overrides):
    kwargs = dict(
        resolved=resolved, store=store, token=token, transport=transport,
        project_root=str(project.root), config_home=str(project.home),
        caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
    )
    kwargs.update(overrides)
    return publish(**kwargs)


def test_result_is_durable_before_the_returned_value_is_observable(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    observed = {}

    def observe(name):
        if name == "before_result_fsync":
            observed["before"] = os.path.exists(project.result_out)
        if name == "before_stdout":
            observed["at_stdout"] = open(project.result_out, "rb").read()

    outcome = run(project, resolved, store, issued.token, Recorder(), on_boundary=observe)
    assert observed["before"] is False
    assert observed["at_stdout"] == serialize_artifact(outcome.result)


def test_durable_result_parses_as_a_closed_v2_result(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    raw = open(project.result_out, "r", encoding="utf-8").read()
    item = parse_typed(raw, expected_type="s3-upload.result")
    body = body_of(item)
    assert body["result_hash"] == result_hash(body)
    assert body["plan_id"] == issued.plan_id
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert body["allowed_actions"] == ["inspect", "ack"]
    assert body == body_of(outcome.result)


def test_result_file_is_0600_with_a_single_link(project, resolved, dry_run, snapshot,
                                                contract_digest):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    run(project, resolved, store, issued.token, Recorder())
    info = os.stat(project.result_out)
    assert info.st_nlink == 1
    assert info.st_mode & 0o777 == 0o600


def test_result_carries_no_secret(project, resolved, dry_run, snapshot, contract_digest):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    raw = open(project.result_out, "r", encoding="utf-8").read()
    for secret in ("project-secret-value", "PROJECTKEY1234", "images-key", issued.token):
        assert secret not in raw
    assert outcome.checkpoint_id not in raw


def test_ambiguous_outcome_is_still_written_before_output(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder(status=503))
    body = body_of(outcome.result)
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["retry_safe"] is False
    assert open(project.result_out, "rb").read() == serialize_artifact(outcome.result)


def test_result_write_failure_keeps_the_checkpoint_and_never_repeats_the_put(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    real = handoff_io.commit
    state = {"count": 0}

    def flaky(target, data):
        state["count"] += 1
        if state["count"] == 2:
            raise handoff_io.HandoffError("injected result write failure",
                                          reason=handoff_io.HANDOFF_WRITE_FAILED)
        return real(target, data)

    monkeypatch.setattr("delivery_workflow.commit", flaky)
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert "handoff_write_failed" in body["blocking_reasons"]
    assert not os.path.exists(project.result_out)
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    assert os.path.exists(project.recovery_out)


def test_result_out_is_replayed_byte_for_byte_and_never_overwritten(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    original = open(project.result_out, "rb").read()
    second = Recorder()
    replay = run(project, resolved, store, issued.token, second)
    assert second.calls == []
    assert replay.transport_calls == 0
    assert open(project.result_out, "rb").read() == original
    assert serialize_artifact(replay.result) == serialize_artifact(first.result)


@pytest.mark.parametrize(
    "damage, value",
    [
        ("result_hash", "sha256:" + "0" * 64),
        ("plan_hash", "sha256:" + "1" * 64),
        ("target_contract_hash", "sha256:" + "2" * 64),
        ("operation", "inspect"),
        ("operation_id", "3" * 32),
        ("recovery_id", "4" * 32),
    ],
)
def test_a_result_with_a_wrong_hash_or_bound_identity_is_not_replayed(
    project, resolved, dry_run, snapshot, contract_digest, damage, value
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    tampered = replace_durable_result(project.out / "result.json", **{damage: value})

    second = Recorder()
    replay = run(project, resolved, store, issued.token, second)
    body = body_of(replay.result)
    assert second.calls == []
    assert replay.transport_calls == 0
    assert body != body_of(tampered)
    assert body["operation_id"] == body_of(first.result)["operation_id"]
    assert body["recovery_id"] == body_of(first.result)["recovery_id"]
    assert body["result_hash"] == result_hash(body)
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert open(project.result_out, "rb").read() == serialize_artifact(tampered)


def test_root_recovery_id_defaults_to_the_first_recovery_id(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    body = body_of(outcome.result)
    assert body["root_recovery_id"] == body["recovery_id"]
    assert body["predecessor_operation_id"] is None
    assert body["predecessor_result_hash"] is None


def test_chain_fields_are_threaded_into_a_fresh_publish(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    chained = run(
        project, resolved, store, issued.token, Recorder(),
        root_recovery_id="r" * 32,
        predecessor_operation_id="q" * 32,
        predecessor_result_hash="sha256:" + "8" * 64,
    )
    body = body_of(chained.result)
    assert body["root_recovery_id"] == "r" * 32
    assert body["predecessor_operation_id"] == "q" * 32
    assert body["predecessor_result_hash"] == "sha256:" + "8" * 64
    assert body["recovery_id"] != body["root_recovery_id"]
    assert open(project.result_out, "rb").read() == serialize_artifact(chained.result)


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_definitive_no_write_lands_a_terminal_result(
    project, resolved, dry_run, snapshot, contract_digest, status
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder(status=status)
    seen = []
    outcome = run(project, resolved, store, issued.token, raw, on_boundary=seen.append)
    body = body_of(outcome.result)
    assert seen == [
        "revalidated", "checkpoint_durable", "before_recovery_fsync",
        "after_recovery_fsync", "before_request", "after_request",
        "before_result_fsync", "after_result_fsync", "before_stdout",
    ]
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "known_not_applied"
    assert body["allowed_actions"] == ["inspect"]
    assert body["authorization_required"] == []
    assert open(project.result_out, "rb").read() == serialize_artifact(outcome.result)
    assert durable(project.result_out, "s3-upload.result") == body
    record = operation_record(project, issued.plan_id)
    assert record is not None
    assert checkpoints_on_disk(project) == [record["checkpoint_id"]]
    checkpoint = json.loads(
        (project.state_root / "checkpoints" / (record["checkpoint_id"] + ".json"))
        .read_text(encoding="utf-8")
    )
    assert checkpoint["state"] == "not_started"
    assert outcome.checkpoint_id == record["checkpoint_id"]


def test_the_recovery_descriptor_defers_to_the_durable_result(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    run(project, resolved, store, issued.token, Recorder(status=403))
    descriptor = durable(project.recovery_out, "s3-upload.recovery-descriptor")
    result = durable(project.result_out, "s3-upload.result")
    assert descriptor["recovery_state"] == "in_flight_unknown"
    assert result["recovery_state"] == "known_not_applied"
    assert descriptor["result_out"] == project.result_out
    assert descriptor["operation_id"] == result["operation_id"]
    assert descriptor["recovery_id"] == result["recovery_id"]
    assert descriptor["plan_id"] == result["plan_id"] == issued.plan_id


def test_a_definitive_no_write_is_replayed_and_never_repeated(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder(status=403))
    original = open(project.result_out, "rb").read()
    second = Recorder()
    replay = run(project, resolved, store, issued.token, second)
    assert second.calls == []
    assert replay.transport_calls == 0
    assert open(project.result_out, "rb").read() == original
    assert serialize_artifact(replay.result) == serialize_artifact(first.result)
    assert body_of(replay.result)["recovery_state"] == "known_not_applied"


def test_an_internal_checkpoint_failure_never_reaches_the_caller_as_an_exception(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()

    def explode(self, checkpoint):
        raise ArtifactError("injected checkpoint create failure")

    monkeypatch.setattr(CheckpointStore, "create", explode)
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert body["plan_id"] == issued.plan_id
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)
    assert operation_record(project, issued.plan_id) is None

    monkeypatch.undo()
    retried = Recorder()
    replay = run(project, resolved, store, issued.token, retried)
    assert len(retried.calls) == 1
    assert body_of(replay.result)["recovery_state"] == "terminal_unacknowledged"
    assert durable(project.result_out, "s3-upload.result") == body_of(replay.result)


def test_an_operation_error_before_transport_does_not_cross_after_request(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    seen = []

    def fail_before_transport(**kwargs):
        raise OperationError("injected pre-request operation failure")

    monkeypatch.setattr("delivery_workflow.execute_single_put", fail_before_transport)
    raw = Recorder()
    outcome = run(
        project, resolved, store, issued.token, raw, on_boundary=seen.append
    )
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert "after_request" not in seen
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert not os.path.exists(project.result_out)


def test_an_internal_failure_after_the_request_is_reported_as_in_flight(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    real = CheckpointStore.replace
    seen = {"count": 0}

    def flaky(self, checkpoint):
        seen["count"] += 1
        if seen["count"] == 2:
            raise ArtifactError("injected checkpoint replace failure")
        return real(self, checkpoint)

    monkeypatch.setattr(CheckpointStore, "replace", flaky)
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert open(project.result_out, "rb").read() == serialize_artifact(outcome.result)
    assert os.path.exists(project.recovery_out)
    assert operation_record(project, issued.plan_id) is not None
    # The reported checkpoint is the one the checkpoint directory still holds.
    # publish reports the id it is carrying unless a stat proves the directory
    # let go of it, so this pins the two against each other rather than merely
    # observing that the process handed back the handle it was carrying.
    assert outcome.checkpoint_id is not None
    assert checkpoints_on_disk(project) == [outcome.checkpoint_id]


def test_a_sealed_violation_is_never_swallowed_by_a_structured_exit(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def rogue(**kwargs):
        kwargs["transport"]("PUT", "https://example.invalid/object", {}, b"")
        raise AssertionError("the sealed gate must refuse the request")

    monkeypatch.setattr("delivery_workflow.execute_single_put", rogue)
    with pytest.raises(TransportSealed) as caught:
        run(project, resolved, store, issued.token, Recorder())
    assert isinstance(caught.value.__context__, TransportSealed)
    assert not os.path.exists(project.result_out)


def test_the_reported_recovery_descriptor_is_the_one_on_disk(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    assert outcome.recovery is not None
    assert serialize_artifact(outcome.recovery) == open(project.recovery_out, "rb").read()


def test_no_recovery_descriptor_is_reported_when_none_is_durable(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def die(name):
        if name == "before_recovery_fsync":
            raise HardCrash("the process is gone before the descriptor is durable")

    with pytest.raises(HardCrash):
        run(project, resolved, store, issued.token, Recorder(), on_boundary=die)
    replay = run(project, resolved, store, issued.token, Recorder())
    assert body_of(replay.result)["recovery_state"] == "in_flight_unknown"
    assert not os.path.exists(project.recovery_out)
    assert replay.recovery is None


def test_already_handed_off_without_a_result_uses_result_boundaries(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def die(name):
        if name == "before_recovery_fsync":
            raise HardCrash("injected crash before recovery durability")

    with pytest.raises(HardCrash):
        run(project, resolved, store, issued.token, Recorder(), on_boundary=die)
    assert not os.path.exists(project.result_out)

    seen = []
    outcome = run(
        project, resolved, store, issued.token, Recorder(), on_boundary=seen.append
    )
    assert seen == [
        "revalidated", "checkpoint_durable", "before_result_fsync",
        "after_result_fsync", "before_stdout",
    ]
    assert open(project.result_out, "rb").read() == serialize_artifact(outcome.result)


def test_already_handed_off_result_write_failure_is_reported(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def die(name):
        if name == "before_recovery_fsync":
            raise HardCrash("injected crash before recovery durability")

    with pytest.raises(HardCrash):
        run(project, resolved, store, issued.token, Recorder(), on_boundary=die)
    record = operation_record(project, issued.plan_id)

    def fail_result_write(target, data):
        raise handoff_io.HandoffError(
            "injected result write failure", reason=handoff_io.HANDOFF_WRITE_FAILED
        )

    monkeypatch.setattr("delivery_workflow.commit", fail_result_write)
    seen = []
    outcome = run(
        project, resolved, store, issued.token, Recorder(), on_boundary=seen.append
    )
    assert seen == ["revalidated", "checkpoint_durable", "before_result_fsync"]
    assert body_of(outcome.result)["blocking_reasons"] == ["handoff_write_failed"]
    assert not os.path.exists(project.result_out)
    assert record["checkpoint_id"] in checkpoints_on_disk(project)


def test_a_descriptor_that_outlived_a_rollback_is_reported_and_keeps_its_checkpoint(
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
    crashed = run(project, resolved, store, issued.token, Recorder())
    assert body_of(crashed.result)["recovery_state"] == "known_not_applied"
    assert crashed.recovery is not None
    assert serialize_artifact(crashed.recovery) == open(project.recovery_out, "rb").read()

    replay = run(project, resolved, store, issued.token, Recorder())
    assert body_of(replay.result)["recovery_state"] == "in_flight_unknown"
    record = operation_record(project, issued.plan_id)
    assert record is not None
    assert record["checkpoint_id"] in checkpoints_on_disk(project)
    assert replay.checkpoint_id == record["checkpoint_id"]


def fail_the_first_checkpoint_replace(monkeypatch):
    """Wedge the window between CheckpointStore.create and checkpoint_notice.

    execute_single_put creates the checkpoint, moves it to put_in_flight with
    a replace, and only then calls checkpoint_notice. Failing that first
    replace lands the publish in the generic exception exit with a checkpoint
    already on disk and nothing else durable.
    """
    real = CheckpointStore.replace
    seen = {"count": 0}

    def flaky(self, checkpoint):
        seen["count"] += 1
        if seen["count"] == 1:
            raise ArtifactError("injected checkpoint replace failure")
        return real(self, checkpoint)

    monkeypatch.setattr(CheckpointStore, "replace", flaky)
    return seen


def test_a_later_exception_never_contradicts_the_durable_result(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    original = open(project.result_out, "rb").read()
    assert body_of(first.result)["recovery_state"] == "terminal_unacknowledged"

    fail_the_first_checkpoint_replace(monkeypatch)
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert body == body_of(first.result)
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert body["allowed_actions"] == ["inspect", "ack"]
    assert body["retry_safe"] is False
    assert open(project.result_out, "rb").read() == original


def test_a_later_exception_before_the_notice_still_defers_to_the_durable_result(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder(status=403))
    original = open(project.result_out, "rb").read()
    assert body_of(first.result)["recovery_state"] == "known_not_applied"

    def explode(target, data):
        raise handoff_io.HandoffError("injected handoff failure",
                                      reason=handoff_io.HANDOFF_WRITE_FAILED)

    monkeypatch.setattr("delivery_workflow.commit", explode)
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert second.calls == []
    assert body_of(outcome.result) == body_of(first.result)
    assert open(project.result_out, "rb").read() == original


@pytest.mark.parametrize("damage", ["foreign_plan", "corrupt"])
def test_a_foreign_or_corrupt_result_out_is_never_this_plans_answer(
    project, resolved, dry_run, snapshot, contract_digest, damage
):
    if damage == "corrupt":
        payload = b"{not a result artifact at all"
    else:
        other_store, other = issue_plan(
            project, dry_run, snapshot, contract_digest,
            recovery_out=str(project.out / "other-recovery.json"),
            result_out=str(project.out / "other-result.json"),
        )
        run(project, resolved, other_store, other.token, Recorder())
        payload = open(project.out / "other-result.json", "rb").read()
        assert durable(project.out / "other-result.json",
                       "s3-upload.result")["plan_id"] == other.plan_id
    with open(project.result_out, "wb") as handle:
        handle.write(payload)
    os.chmod(project.result_out, 0o600)

    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert body["plan_id"] == issued.plan_id
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert "handoff_write_failed" in body["blocking_reasons"]
    assert open(project.result_out, "rb").read() == payload


def test_a_handoff_error_after_the_request_is_never_reported_as_retry_safe(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    real = delivery_workflow.execute_single_put

    def send_then_fail(**kwargs):
        real(**kwargs)
        raise handoff_io.HandoffError("injected post-request handoff failure",
                                      reason=handoff_io.HANDOFF_WRITE_FAILED)

    monkeypatch.setattr("delivery_workflow.execute_single_put", send_then_fail)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert durable(project.result_out, "s3-upload.result") == body


def test_a_handoff_error_with_no_request_still_invites_a_retry(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def explode(target, data):
        raise handoff_io.HandoffError("injected recovery write failure",
                                      reason=handoff_io.HANDOFF_WRITE_FAILED)

    monkeypatch.setattr("delivery_workflow.commit", explode)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert not os.path.exists(project.result_out)
    assert operation_record(project, issued.plan_id) is None


def test_a_definitive_no_write_keeps_its_checkpoint_consistent_with_the_operation_record(
    project, resolved, dry_run, snapshot, contract_digest
):
    """The 403 exit leaves the record and the checkpoint agreeing.

    execute_single_put retains the checkpoint on a definitive no-write
    response, moved back to "not_started", instead of removing it while
    operation.json keeps pointing at it. The record must stay behind either
    way -- discarding it would let a replay lose the AlreadyHandedOff trigger
    and send the Put a second time -- and now the checkpoint it names is
    really on disk.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder(status=403))
    record = operation_record(project, issued.plan_id)
    assert body_of(outcome.result)["recovery_state"] == "known_not_applied"
    assert record is not None
    assert checkpoints_on_disk(project) == [record["checkpoint_id"]]
    checkpoint = json.loads(
        (project.state_root / "checkpoints" / (record["checkpoint_id"] + ".json"))
        .read_text(encoding="utf-8")
    )
    assert checkpoint["state"] == "not_started"
    assert outcome.checkpoint_id == record["checkpoint_id"]


def test_a_definitive_no_write_survives_a_failed_checkpoint_retention_write(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """A local retention write must not erase what the remote already said.

    The remote's definitive refusal to write is established fact by the time
    the checkpoint is moved back to "not_started"; a replace that fails there
    (checkpoint directory drifted, ENOSPC) is a local bookkeeping failure. It
    must not displace DefinitiveNoWrite into the generic exception exit, which
    would report in_flight_unknown / retry_safe=False and leave reconcile
    probing an object the server already refused.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    real = CheckpointStore.replace

    def fail_retention_write(self, checkpoint):
        if checkpoint["state"] == "not_started":
            raise ArtifactError("injected retention write failure")
        return real(self, checkpoint)

    monkeypatch.setattr(CheckpointStore, "replace", fail_retention_write)
    raw = Recorder(status=403)
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert open(project.result_out, "rb").read() == serialize_artifact(outcome.result)
    record = operation_record(project, issued.plan_id)
    assert record is not None
    assert checkpoints_on_disk(project) == [record["checkpoint_id"]]
    checkpoint = json.loads(
        (project.state_root / "checkpoints" / (record["checkpoint_id"] + ".json"))
        .read_text(encoding="utf-8")
    )
    assert checkpoint["state"] == "put_in_flight"
    assert outcome.checkpoint_id == record["checkpoint_id"]


def test_a_failure_before_the_notice_cleans_the_checkpoint_and_remains_retryable(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """A failure in the create-to-notice window rolls back and stays retryable.

    checkpoint_created hands the id to the publish as soon as the checkpoint
    is durable, so a failure before checkpoint_notice can prove nothing left
    the process: no request was sent and no operation record exists. The exit
    drops the checkpoint it created and answers known_not_applied with
    retry_safe=True. Do NOT close a failure here by relabelling the exit
    in_flight_unknown, which would turn a safely retryable failure into a
    dead end.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    fail_the_first_checkpoint_replace(monkeypatch)
    real_remove = CheckpointStore.remove
    removed = []

    def record_remove(self, checkpoint_id):
        removed.append(checkpoint_id)
        return real_remove(self, checkpoint_id)

    monkeypatch.setattr(CheckpointStore, "remove", record_remove)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert outcome.checkpoint_id is None
    assert len(removed) == 1
    assert checkpoints_on_disk(project) == []
    assert operation_record(project, issued.plan_id) is None
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)

    monkeypatch.undo()
    retried = Recorder()
    replay = run(project, resolved, store, issued.token, retried)
    assert len(retried.calls) == 1
    assert body_of(replay.result)["recovery_state"] == "terminal_unacknowledged"


def test_checkpoint_survival_lookup_keeps_an_id_rejected_by_store_validation(project):
    invalid = "not-a-checkpoint-id"
    assert delivery_workflow._surviving_checkpoint(str(project.root), invalid) == invalid


def fail_fstat_on(path, monkeypatch, *, limit=1):
    """Fail os.fstat for one specific file, whichever descriptor reaches it.

    handoff_io converts only the os.open of a handoff destination into a
    HandoffError. The fstat that follows it -- the one that decides whether
    the thing just opened is still a regular, single-linked, 0600 file -- is a
    bare OSError, and this is how a read-back is made to fail after the file
    has been opened.
    """
    info = os.stat(path)
    identity = (info.st_dev, info.st_ino)
    real = os.fstat
    injected = []

    def failing(fd):
        current = real(fd)
        if len(injected) < limit and (current.st_dev, current.st_ino) == identity:
            injected.append(fd)
            raise OSError(errno.EIO, "injected fstat failure")
        return current

    monkeypatch.setattr(os, "fstat", failing)
    return injected


def test_a_failed_read_back_never_escapes_over_an_answer_already_written(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """A read-back that fails must not take the publish with it.

    The descriptor read at the end of emit runs after the object may already
    be on the remote and after result_out has been written. An OSError from it
    -- an fstat or a read failing on the open descriptor, neither of which
    handoff_io converts -- would escape publish as a traceback, so the caller
    would be handed an exception instead of the in_flight_unknown that is
    sitting on disk, and would have no way to learn a request had been sent.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder(status=503)
    state = {}

    def wound(name):
        if name == "after_recovery_fsync" and "injected" not in state:
            state["injected"] = fail_fstat_on(project.recovery_out, monkeypatch)

    outcome = run(project, resolved, store, issued.token, raw, on_boundary=wound)
    body = body_of(outcome.result)
    assert state["injected"]
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert os.path.exists(project.result_out)
    assert durable(project.result_out, "s3-upload.result") == body
    # Reported as absent, never reconstructed: the descriptor is on disk and
    # this process is holding bytes that would parse, but a read that failed
    # has established nothing about what the destination now holds.
    assert outcome.recovery is None
    assert durable(project.recovery_out, "s3-upload.recovery-descriptor")[
        "plan_id"
    ] == issued.plan_id


def test_an_unreadable_operation_record_keeps_the_checkpoint_it_could_not_clear(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    path = project.state_root / "plans" / issued.plan_id / "operation.json"
    path.write_bytes(b"{not json at all")
    path.chmod(0o600)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert body["recovery_state"] == "in_flight_unknown"
    assert outcome.checkpoint_id is not None
    assert checkpoints_on_disk(project) == [outcome.checkpoint_id]
