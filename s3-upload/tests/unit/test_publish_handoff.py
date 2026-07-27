import errno
import json
import os
import signal
import stat
from contextlib import contextmanager

import pytest

import handoff_io
from conftest import CALLER
from delivery_schema import body_of, parse_typed, serialize_artifact
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


class Wedged(BaseException):
    """Raised out of a blocked syscall by the deadline below.

    A BaseException so publish's own ``except Exception`` cannot swallow the
    timeout and turn a wedged run into a structured result.
    """


@contextmanager
def deadline(seconds):
    """Bound a call that must not be able to block forever.

    A publish that opens a handoff destination without O_NONBLOCK blocks in
    os.open until a writer shows up on the FIFO, which never happens: no
    assertion fails, the test never returns, and the whole suite stops. SIGALRM
    interrupts the syscall and the handler raises, so the wedge surfaces as a
    named failure in this test instead of a job that has to be killed.
    """
    def expire(signum, frame):
        raise Wedged("the call did not return within %.1fs" % seconds)

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


FOREIGN_RECORD = {
    "checkpoint_id": "f" * 32,
    "operation_id": "e" * 32,
    "recovery_id": "d" * 32,
    "result_out": "/nowhere/foreign-result.json",
    "root_recovery_id": "c" * 32,
}


def fail_the_plan_directory_fsync(project, plan_id, monkeypatch, *, limit=1,
                                  on_failure=None):
    plan_dir = project.state_root / "plans" / plan_id
    record_path = operation_record_path(project, plan_id)
    info = os.stat(plan_dir)
    plan_identity = (info.st_dev, info.st_ino)
    real_fsync = os.fsync
    injected = []

    def failing_fsync(fd):
        current = os.fstat(fd)
        if (
            len(injected) < limit
            and stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == plan_identity
            and record_path.exists()
        ):
            injected.append(fd)
            if on_failure is not None:
                on_failure(record_path)
            raise OSError(errno.EIO, "injected plan directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    return injected


def test_an_operation_record_that_survived_a_failed_write_is_rolled_back(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    record_path = operation_record_path(project, issued.plan_id)
    injected = fail_the_plan_directory_fsync(project, issued.plan_id, monkeypatch)
    first = Recorder()
    outcome = run(project, resolved, store, issued.token, first)
    body = body_of(outcome.result)
    assert injected
    assert first.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["retry_safe"] is True
    assert body["allowed_actions"] == ["inspect", "publish"]
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert not record_path.exists()
    assert outcome.checkpoint_id is None
    state = durable_state(project, issued.plan_id)
    assert state["operation_record"] is False
    assert state["checkpoints"] == []
    assert not os.path.exists(project.recovery_out)

    second = Recorder()
    replay = run(project, resolved, store, issued.token, second)
    replayed = body_of(replay.result)
    assert len(second.calls) == 1
    assert replay.transport_calls == 1
    assert replayed["recovery_state"] == "terminal_unacknowledged"
    assert replayed["allowed_actions"] == ["inspect", "ack"]
    assert record_path.exists()
    assert durable_state(project, issued.plan_id)["operation_record"] is True
    assert body_of(parse_typed(open(project.result_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.result")) == replayed


def test_an_operation_record_that_cannot_be_read_back_stays_conservative(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    record_path = operation_record_path(project, issued.plan_id)
    injected = fail_the_plan_directory_fsync(
        project, issued.plan_id, monkeypatch,
        on_failure=lambda path: path.chmod(0o644),
    )
    first = Recorder()
    outcome = run(project, resolved, store, issued.token, first)
    body = body_of(outcome.result)
    assert injected
    assert record_path.exists()
    surviving = json.loads(record_path.read_text(encoding="utf-8"))
    assert checkpoint_path(project, surviving["checkpoint_id"]).exists()
    assert first.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["blocking_reasons"] == []
    assert outcome.checkpoint_id == surviving["checkpoint_id"]
    assert not os.path.exists(project.recovery_out)
    assert body_of(parse_typed(open(project.result_out, "r", encoding="utf-8").read(),
                               expected_type="s3-upload.result")) == body

    second = Recorder()
    replay = body_of(run(project, resolved, store, issued.token, second).result)
    assert second.calls == []
    assert replay["recovery_state"] == "in_flight_unknown"
    assert replay["retry_safe"] is False
    assert replay["allowed_actions"] == ["inspect", "reconcile"]
    assert record_path.exists()
    assert json.loads(record_path.read_text(encoding="utf-8")) == surviving


def test_a_foreign_operation_record_is_never_rolled_back(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    record_path = operation_record_path(project, issued.plan_id)

    def substitute(path):
        path.write_text(json.dumps(FOREIGN_RECORD, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)

    injected = fail_the_plan_directory_fsync(project, issued.plan_id, monkeypatch,
                                             on_failure=substitute)
    first = Recorder()
    outcome = run(project, resolved, store, issued.token, first)
    body = body_of(outcome.result)
    assert injected
    assert first.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert json.loads(record_path.read_text(encoding="utf-8")) == FOREIGN_RECORD
    assert outcome.checkpoint_id is not None
    assert outcome.checkpoint_id != FOREIGN_RECORD["checkpoint_id"]
    assert checkpoint_path(project, outcome.checkpoint_id).exists()
    assert not os.path.exists(project.recovery_out)

    second = Recorder()
    replay = body_of(run(project, resolved, store, issued.token, second).result)
    assert second.calls == []
    assert replay["recovery_state"] == "in_flight_unknown"
    assert replay["retry_safe"] is False
    assert json.loads(record_path.read_text(encoding="utf-8")) == FOREIGN_RECORD


def test_repeated_operation_record_failures_do_not_accumulate_orphan_checkpoints(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    injected = fail_the_plan_directory_fsync(project, issued.plan_id, monkeypatch,
                                             limit=4)
    for _ in range(4):
        outcome = run(project, resolved, store, issued.token, Recorder())
        body = body_of(outcome.result)
        assert body["recovery_state"] == "known_not_applied"
        assert body["retry_safe"] is True
        assert outcome.transport_calls == 0
        assert outcome.checkpoint_id is None
        state = durable_state(project, issued.plan_id)
        assert state["checkpoints"] == []
        assert state["operation_record"] is False
    assert len(injected) == 4

    last = Recorder()
    outcome = run(project, resolved, store, issued.token, last)
    assert len(last.calls) == 1
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"
    state = durable_state(project, issued.plan_id)
    assert state["operation_record"] is True
    assert state["checkpoints"] == [
        operation_record(project, issued.plan_id)["checkpoint_id"] + ".json"
    ]


def test_a_replayed_handoff_reports_a_checkpoint_it_could_not_drop(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    checkpoints = project.state_root / "checkpoints"

    def die(name):
        if name == "before_recovery_fsync":
            raise HardCrash("the process is gone before the descriptor is durable")

    with pytest.raises(HardCrash):
        run(project, resolved, store, issued.token, Recorder(), on_boundary=die)
    dropped = run(project, resolved, store, issued.token, Recorder())
    assert dropped.checkpoint_id is None
    assert os.path.exists(project.result_out)

    def wound(name):
        if name == "checkpoint_durable":
            os.chmod(checkpoints, 0o500)

    outcome = run(project, resolved, store, issued.token, Recorder(), on_boundary=wound)
    os.chmod(checkpoints, 0o700)
    body = body_of(outcome.result)
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["retry_safe"] is False
    assert outcome.checkpoint_id is not None
    assert checkpoint_path(project, outcome.checkpoint_id).exists()
    assert outcome.checkpoint_id != operation_record(project, issued.plan_id)["checkpoint_id"]


def test_a_failed_checkpoint_drop_reports_the_checkpoint_it_left_behind(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    checkpoints = project.state_root / "checkpoints"
    out_mode = stat.S_IMODE(os.stat(project.out).st_mode)

    def wound(name):
        if name == "checkpoint_durable":
            os.chmod(project.out, 0o500)
            os.chmod(checkpoints, 0o500)

    reported = []
    for _ in range(2):
        recorder = Recorder()
        outcome = run(project, resolved, store, issued.token, recorder,
                      on_boundary=wound)
        os.chmod(project.out, out_mode)
        os.chmod(checkpoints, 0o700)
        body = body_of(outcome.result)
        assert recorder.calls == []
        assert outcome.transport_calls == 0
        assert body["recovery_state"] == "known_not_applied"
        assert body["blocking_reasons"] == ["handoff_write_failed"]
        assert durable_state(project, issued.plan_id)["operation_record"] is False
        assert not os.path.exists(project.recovery_out)
        reported.append(outcome.checkpoint_id)

    stranded = sorted(
        item.name[: -len(".json")] for item in checkpoints.iterdir()
        if item.suffix == ".json"
    )
    assert len(stranded) == 2
    assert sorted(reported) == stranded
    assert reported[0] != reported[1]


def test_an_unsafe_checkpoint_store_does_not_take_the_rollback_exit_down(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    # The rollback of an unarmed checkpoint runs inside publish's own except
    # handler, so an exception escaping CheckpointStore.remove there would
    # replace the structured known_not_applied answer with a traceback. The
    # bare-FileSecurityError shape is the lost-creation-race one: remove()'s
    # _prepare finds the guard missing, loses the create-once write to a
    # competitor, and the unsafe re-read surfaces FileSecurityError (a
    # ValueError, not an OSError) out of remove().
    import artifacts as artifacts_module

    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    real_write = artifacts_module.atomic_write

    def racing(path, data, **kwargs):
        if path.endswith(os.path.join("checkpoints", ".gitignore")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("*\n!.gitignore\n")
            os.chmod(path, 0o644)
        return real_write(path, data, **kwargs)

    def sabotage(name):
        if name == "checkpoint_durable":
            os.unlink(project.state_root / "checkpoints" / ".gitignore")
            monkeypatch.setattr(artifacts_module, "atomic_write", racing)
            raise RuntimeError("injected crash before the durable handoff")

    outcome = run(project, resolved, store, issued.token, raw, on_boundary=sabotage)
    body = body_of(outcome.result)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["blocking_reasons"] == []
    assert durable_state(project, issued.plan_id)["operation_record"] is False
    assert not os.path.exists(project.recovery_out)
    # The refused removal leaves the checkpoint behind, reported at the exit.
    assert outcome.checkpoint_id is not None
    assert checkpoint_path(project, outcome.checkpoint_id).exists()


def test_a_checkpoint_the_directory_will_not_show_is_still_reported(
    project, resolved, dry_run, snapshot, contract_digest
):
    """Only proven absence may drop a checkpoint id.

    A checkpoint directory that has drifted off 0700 is one CheckpointStore
    refuses to touch, so the checkpoint this call created is stranded there
    and nothing will collect it. The publish reports the id it is carrying
    unless a stat proves the directory let go of it, and here the stat is
    refused rather than answered: a mode with no traversal bit fails every
    lookup inside with EACCES. Answering None on that would tell the caller a
    stranded checkpoint is gone.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    checkpoints = project.state_root / "checkpoints"
    out_mode = stat.S_IMODE(os.stat(project.out).st_mode)

    def wound(name):
        if name == "checkpoint_durable":
            os.chmod(project.out, 0o500)
            os.chmod(checkpoints, 0o600)

    recorder = Recorder()
    try:
        outcome = run(project, resolved, store, issued.token, recorder,
                      on_boundary=wound)
        # Pin the mechanism the assertions below rest on: while the directory
        # is in this state, "is the checkpoint still there" cannot be answered.
        with pytest.raises(PermissionError):
            os.lstat(checkpoints / "probe.json")
    finally:
        os.chmod(project.out, out_mode)
        os.chmod(checkpoints, 0o700)

    body = body_of(outcome.result)
    assert recorder.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "known_not_applied"
    assert body["blocking_reasons"] == ["handoff_write_failed"]
    assert durable_state(project, issued.plan_id)["operation_record"] is False
    assert not os.path.exists(project.recovery_out)
    assert outcome.checkpoint_id is not None
    assert checkpoint_path(project, outcome.checkpoint_id).exists()
    assert durable_state(project, issued.plan_id)["checkpoints"] == [
        outcome.checkpoint_id + ".json"
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


@pytest.mark.parametrize("swap", ["none", "symlink", "mode", "hardlink", "fifo"])
def test_a_descriptor_destination_that_turned_unsafe_reads_back_as_absent(
    project, resolved, dry_run, snapshot, contract_digest, swap
):
    """The descriptor read must obey the same rules as the write.

    Reading recovery_out back with a plain open() would follow a symlink,
    accept a world-writable file or a hardlink alias, and block forever on a
    FIFO. The read goes through the preflighted HandoffTarget instead, so a
    destination swapped out after preflight reports no descriptor at all.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    assert first.recovery is not None
    descriptor = open(project.recovery_out, "rb").read()

    def wound(name):
        if name != "checkpoint_durable":
            return
        if swap == "symlink":
            os.rename(project.recovery_out, project.out / "hidden.json")
            os.symlink("hidden.json", project.recovery_out)
        elif swap == "mode":
            os.chmod(project.recovery_out, 0o666)
        elif swap == "hardlink":
            os.link(project.recovery_out, project.out / "alias.json")
        elif swap == "fifo":
            os.unlink(project.recovery_out)
            os.mkfifo(project.recovery_out, 0o600)

    second = Recorder()
    try:
        with deadline(5.0):
            outcome = run(project, resolved, store, issued.token, second,
                          on_boundary=wound)
    except Wedged as exc:
        pytest.fail("publish blocked on the %s destination: %s" % (swap, exc))
    assert second.calls == []
    assert body_of(outcome.result) == body_of(first.result)
    if swap == "none":
        assert outcome.recovery is not None
        assert serialize_artifact(outcome.recovery) == descriptor
    else:
        assert outcome.recovery is None
    if swap == "symlink":
        assert os.path.islink(project.recovery_out)
        assert open(project.out / "hidden.json", "rb").read() == descriptor


@pytest.mark.parametrize(
    "damage", ["source_drift", "recovery_loosened", "recovery_symlinked"]
)
def test_an_exit_after_the_plan_is_bound_still_answers_with_the_durable_result(
    project, resolved, dry_run, snapshot, contract_digest, damage
):
    """A durable result outranks every exit taken once the plan is bound.

    These exits all run before the recovery preflight -- two of them are the
    preflight refusing recovery_out -- and each of them used to answer
    "blocked" with allowed_actions=["inspect"], which is the one shape that
    can never be acknowledged: the plan, the token and the spool stay behind
    forever while a terminal_unacknowledged result sits on result_out. So
    result_out is preflighted as soon as the plan is, and every exit from
    there on reports what is durable rather than what this attempt hit.

    The descriptor is a different matter: nothing preflighted recovery_out on
    these paths, so it is not read at all and no descriptor is reported.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    assert first.recovery is not None
    original = open(project.result_out, "rb").read()
    if damage == "source_drift":
        project.source.write_bytes(b"different!!")
    elif damage == "recovery_loosened":
        os.chmod(project.recovery_out, 0o666)
    else:
        os.rename(project.recovery_out, project.out / "hidden-recovery.json")
        os.symlink("hidden-recovery.json", project.recovery_out)

    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body == body_of(first.result)
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert body["allowed_actions"] == ["inspect", "ack"]
    assert body["blocking_reasons"] == []
    assert outcome.recovery is None
    assert open(project.result_out, "rb").read() == original


def test_an_exit_whose_result_destination_is_unsafe_reports_no_answer(
    project, resolved, dry_run, snapshot, contract_digest
):
    """The one exit that may still answer "blocked" is the unreadable one.

    Loosening result_out is not the defect the test above closes: the durable
    answer cannot be read through a destination that just failed preflight, so
    there is nothing to report and blocked is the honest reply. Do NOT close
    this by reading result_out anyway.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    first = run(project, resolved, store, issued.token, Recorder())
    assert first.recovery is not None
    os.chmod(project.result_out, 0o666)

    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body["blocking_reasons"] == ["handoff_unsafe"]
    assert body["recovery_state"] == "blocked"
    assert outcome.recovery is None
    assert os.path.exists(project.recovery_out)


def test_a_result_destination_that_cannot_be_read_still_reaches_its_exit(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """Binding result_out early must not become a new way to raise.

    The bind puts a filesystem read in front of exits that previously touched
    nothing, so a read that fails there could stop those exits from being
    reached at all: a source_drift that used to be answered would come back as
    a traceback. preflight converts only the os.open of the destination; a
    read failing after it comes out as a bare OSError, so the bind absorbs
    that too, reports no durable result, and lets the exit below answer.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    run(project, resolved, store, issued.token, Recorder())
    assert os.path.exists(project.result_out)
    project.source.write_bytes(b"different!!")

    info = os.stat(project.result_out)
    identity = (info.st_dev, info.st_ino)
    real_fstat, real_read = os.fstat, os.read
    injected = []

    def failing_read(fd, count):
        current = real_fstat(fd)
        if (current.st_dev, current.st_ino) == identity:
            injected.append(fd)
            raise OSError(errno.EIO, "injected result_out read failure")
        return real_read(fd, count)

    monkeypatch.setattr(os, "read", failing_read)
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert injected
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body["blocking_reasons"] == ["source_drift"]
    assert body["recovery_state"] == "blocked"
    assert outcome.recovery is None


def test_a_late_preflight_oserror_is_reported_as_handoff_unsafe(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """The gate preflight must absorb a bare OSError, not just HandoffError.

    preflight converts only some failures into HandoffError; an fstat or
    directory open failing with EIO comes out as a bare OSError. The late
    preflight is the exit that judges the handoff destinations, so an I/O
    failure there is the same verdict -- handoff_unsafe -- not a traceback
    that takes the whole publish with it.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def eio(path, **kwargs):
        raise OSError(errno.EIO, "injected preflight I/O failure")

    monkeypatch.setattr("delivery_workflow.preflight", eio)
    recorder = Recorder()
    outcome = run(project, resolved, store, issued.token, recorder)
    body = body_of(outcome.result)
    assert recorder.calls == []
    assert outcome.transport_calls == 0
    assert body["blocking_reasons"] == ["handoff_unsafe"]
    assert body["recovery_state"] == "blocked"
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)


def test_a_checkpoint_deleted_out_from_under_a_success_is_reported_absent(
    project, resolved, dry_run, snapshot, contract_digest
):
    """The success exit answers from the directory, not from its own handle.

    A checkpoint collected out from under the publish -- here deleted at the
    after_request boundary -- is proven absent, so the success exit must
    report checkpoint_id=None rather than replay the in-process handle it is
    still carrying. Reporting the raw handle would point the caller at a
    checkpoint that no longer exists.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    checkpoints = project.state_root / "checkpoints"

    def wound(name):
        if name == "after_request":
            for item in checkpoints.iterdir():
                if item.suffix == ".json":
                    item.unlink()

    outcome = run(project, resolved, store, issued.token, Recorder(),
                  on_boundary=wound)
    body = body_of(outcome.result)
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert durable_state(project, issued.plan_id)["checkpoints"] == []
    assert outcome.checkpoint_id is None


def test_an_identity_extraction_failure_is_answered_as_source_drift(
    project, resolved, dry_run, snapshot, contract_digest, monkeypatch
):
    """A malformed source identity must not escape as a traceback.

    _source_identity reads plan fields that carry an integrity hash, so a
    plan whose identity no longer parses has no reachable trigger today; the
    try around the call is defence in depth. If it ever fires, the publish
    answers source_drift -- the identity could not be tied to the plan --
    instead of handing the caller an exception.
    """
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)

    def explode(plan):
        raise ValueError("injected malformed source identity")

    monkeypatch.setattr("delivery_workflow._source_identity", explode)
    recorder = Recorder()
    outcome = run(project, resolved, store, issued.token, recorder)
    body = body_of(outcome.result)
    assert recorder.calls == []
    assert outcome.transport_calls == 0
    assert body["blocking_reasons"] == ["source_drift"]
    assert body["recovery_state"] == "blocked"


def test_already_handed_off_is_disjoint_from_the_handoff_error_hierarchy():
    import delivery_workflow
    import plan_store

    assert not issubclass(delivery_workflow.AlreadyHandedOff, handoff_io.HandoffError)
    assert not issubclass(delivery_workflow.AlreadyHandedOff, plan_store.PlanStoreError)
    assert not issubclass(delivery_workflow.AlreadyHandedOff, ValueError)
    assert issubclass(delivery_workflow.AlreadyHandedOff, delivery_workflow.WorkflowError)


def test_handoff_safe_read_helper_is_private():
    assert not hasattr(handoff_io, "read_artifact")
    assert callable(handoff_io._read_artifact)
