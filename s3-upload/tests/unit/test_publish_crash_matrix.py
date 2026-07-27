import ast
import inspect
import os

import pytest

from conftest import CALLER
from delivery_schema import body_of, parse_typed
from delivery_workflow import BOUNDARIES, acknowledge, publish
from plan_store import PlanStore, build_plan_body, new_plan_id
from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from s3 import Response
from target_contract import contract_hash, contract_snapshot


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"

PUBLISH_BOUNDARIES = (
    "revalidated",
    "checkpoint_durable",
    "before_recovery_fsync",
    "after_recovery_fsync",
    "before_request",
    "after_request",
    "before_result_fsync",
    "after_result_fsync",
    "before_stdout",
)

# boundary -> (requests sent by the crashed run, requests sent by the recovery
# run, recovery_state the recovery run reports, whether a recovery descriptor
# is durable after recovery). A crash at before_recovery_fsync leaves the
# operation record on disk without a descriptor, so its replay converges
# conservatively (ruled, pinned by
# test_a_hard_crash_between_the_operation_record_and_the_descriptor_stays_conservative)
# instead of publishing again.
CRASH_MATRIX = {
    "revalidated": (0, 1, "terminal_unacknowledged", True),
    "checkpoint_durable": (0, 1, "terminal_unacknowledged", True),
    "before_recovery_fsync": (0, 0, "in_flight_unknown", False),
    "after_recovery_fsync": (0, 0, "in_flight_unknown", True),
    "before_request": (0, 0, "in_flight_unknown", True),
    "after_request": (1, 0, "in_flight_unknown", True),
    "before_result_fsync": (1, 0, "in_flight_unknown", True),
    "after_result_fsync": (1, 0, "terminal_unacknowledged", True),
    "before_stdout": (1, 0, "terminal_unacknowledged", True),
}


class Crash(BaseException):
    pass


class Recorder:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(self.status)


def crash_at(boundary):
    def hook(name):
        if name == boundary:
            raise Crash(name)
    return hook


def issue_plan(project, resolved):
    store = PlanStore(str(project.state_root))
    dry_run = build_upload_dry_run(
        resolved=resolved, file_path=str(project.source), explicit_key=None,
        content_type=None, cache_control=None, content_disposition=None,
        presign_expires=None, reference_out=None, project_root=str(project.root),
        config_home=str(project.home), allow_insecure_http=False,
    )
    try:
        key = derive_contract_key(resolved.target)
        snapshot = contract_snapshot(
            target_ref=resolved.ref, config_scope=resolved.ref.scope,
            project_root=str(project.root), target=resolved.target, contract_key=key,
            registry=registry_for_target(resolved.target, key),
        )
        body = build_plan_body(
            dry_run_plan=dry_run.plan, source_snapshot=dry_run.source.snapshot,
            target_contract=snapshot, target_contract_hash=contract_hash(snapshot),
            caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
            state_root=str(project.state_root), recovery_out=project.recovery_out,
            result_out=project.result_out, plan_id=new_plan_id(),
        )
        return store, store.issue(body, source_path=str(project.source),
                                  soft_max_bytes=1048576, created_at=CREATED_AT)
    finally:
        dry_run.close()


def run(project, resolved, store, token, transport, **overrides):
    kwargs = dict(
        resolved=resolved, store=store, token=token, transport=transport,
        project_root=str(project.root), config_home=str(project.home),
        caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
    )
    kwargs.update(overrides)
    return publish(**kwargs)


def durable_result_body(project):
    return body_of(parse_typed(
        open(project.result_out, encoding="utf-8").read(),
        expected_type="s3-upload.result",
    ))


def test_every_publish_boundary_is_registered():
    assert PUBLISH_BOUNDARIES == BOUNDARIES[1:10]
    assert set(CRASH_MATRIX) == set(PUBLISH_BOUNDARIES)


def test_the_gate_is_armed_exactly_once_only_after_the_descriptor_commit():
    """Pin the arm() call site structurally.

    Moving gate.arm() ahead of the descriptor commit is behaviourally
    invisible: the transport is only invoked after checkpoint_notice returns
    successfully, and relocating arm() does not change the notice's control
    flow, so the full suite stays green under that mutation (run and
    confirmed: 839 passed with arm() moved before the commit). The at-most-once
    argument rests on "descriptor not on disk => never armed => zero
    requests", which is only true while arm() stays the single call site,
    inside checkpoint_notice, after the commit call and after the idempotent
    replay exit. This test reads the source, because no behavioural probe can.
    """
    import delivery_workflow

    tree = ast.parse(inspect.getsource(delivery_workflow))
    arms = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "arm"
    ]
    assert len(arms) == 1
    arm = arms[0]
    assert isinstance(arm.func.value, ast.Name)
    assert arm.func.value.id == "gate"
    notice = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "checkpoint_notice"
    )
    assert notice.lineno <= arm.lineno <= notice.end_lineno
    commit_call = next(
        node for node in ast.walk(notice)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "commit"
    )
    idempotent_if = next(
        node for node in ast.walk(notice)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and any(isinstance(item, ast.Constant) and item.value == "idempotent"
                for item in node.test.comparators)
    )
    assert any(isinstance(item, ast.Raise) for item in idempotent_if.body)
    before_request = next(
        node for node in ast.walk(notice)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hook"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "before_request"
    )
    assert commit_call.lineno < idempotent_if.lineno < arm.lineno
    assert arm.lineno < before_request.lineno


def test_b1_crash_after_plan_durability_leaves_no_remote_effect(project, resolved):
    store, issued = issue_plan(project, resolved)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)
    assert not (project.state_root / "checkpoints").exists()
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    assert len(raw.calls) == 1
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"


@pytest.mark.parametrize("boundary", PUBLISH_BOUNDARIES)
def test_total_remote_mutations_stay_at_most_one_across_crash_and_recovery(
    project, resolved, boundary
):
    first_expected, second_expected, state, recovery_written = CRASH_MATRIX[boundary]
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at(boundary))
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert len(first.calls) == first_expected
    assert len(second.calls) == second_expected
    assert len(first.calls) + len(second.calls) <= 1
    assert outcome.transport_calls == len(second.calls)
    body = body_of(outcome.result)
    assert body["operation"] == "publish"
    assert body["recovery_state"] == state
    assert durable_result_body(project) == body
    assert os.path.exists(project.recovery_out) is recovery_written


@pytest.mark.parametrize("boundary", ["revalidated", "checkpoint_durable",
                                      "before_recovery_fsync"])
def test_crash_before_the_recovery_handoff_never_sends_a_request(
    project, resolved, boundary
):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at(boundary))
    assert first.calls == []
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)


@pytest.mark.parametrize("boundary", ["revalidated", "checkpoint_durable"])
def test_recovery_after_a_pre_handoff_crash_can_still_publish_once(
    project, resolved, boundary
):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at(boundary))
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert first.calls == []
    assert len(second.calls) == 1
    body = body_of(outcome.result)
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert durable_result_body(project) == body


def test_a_crash_between_the_record_and_the_descriptor_replays_conservatively(
    project, resolved
):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at("before_recovery_fsync"))
    assert first.calls == []
    record = project.state_root / "plans" / issued.plan_id / "operation.json"
    assert os.path.exists(record)
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    body = body_of(outcome.result)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["retry_safe"] is False
    assert durable_result_body(project) == body
    assert not os.path.exists(project.recovery_out)


@pytest.mark.parametrize("boundary", ["after_recovery_fsync", "before_request"])
def test_crash_between_handoff_and_request_leaves_a_readable_descriptor(
    project, resolved, boundary
):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at(boundary))
    assert first.calls == []
    descriptor = parse_typed(open(project.recovery_out, encoding="utf-8").read(),
                             expected_type="s3-upload.recovery-descriptor")
    body = body_of(descriptor)
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert body["retry_safe"] is False
    assert body["plan_id"] == issued.plan_id


@pytest.mark.parametrize("boundary", ["before_recovery_fsync",
                                      "after_recovery_fsync", "before_request",
                                      "after_request", "before_result_fsync",
                                      "after_result_fsync", "before_stdout"])
def test_replay_after_a_post_handoff_crash_sends_nothing(project, resolved, boundary):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at(boundary))
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert second.calls == []
    assert outcome.transport_calls == 0
    assert body_of(outcome.result)["recovery_state"] in {
        "in_flight_unknown", "terminal_unacknowledged",
    }


def test_crash_after_result_fsync_replays_the_same_terminal_result(project, resolved):
    store, issued = issue_plan(project, resolved)
    first = Recorder()
    with pytest.raises(Crash):
        run(project, resolved, store, issued.token, first,
            on_boundary=crash_at("before_stdout"))
    durable = open(project.result_out, "rb").read()
    second = Recorder()
    outcome = run(project, resolved, store, issued.token, second)
    assert len(first.calls) == 1
    assert second.calls == []
    assert open(project.result_out, "rb").read() == durable
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"
    assert body_of(outcome.result)["allowed_actions"] == ["inspect", "ack"]


def test_ambiguous_crash_leaves_only_read_only_reconcile(project, resolved):
    store, issued = issue_plan(project, resolved)
    outcome = run(project, resolved, store, issued.token, Recorder(status=503))
    body = body_of(outcome.result)
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]
    assert "publish" not in body["allowed_actions"]
    assert "resume" not in body["allowed_actions"]
    assert "abort" not in body["allowed_actions"]
    assert "ack" not in body["allowed_actions"]
    assert body["retry_safe"] is False
    assert durable_result_body(project) == body


def test_crash_before_ack_leaves_everything_recoverable(project, resolved):
    store, issued = issue_plan(project, resolved)
    run(project, resolved, store, issued.token, Recorder())
    raw = open(project.result_out, encoding="utf-8").read()
    with pytest.raises(Crash):
        acknowledge(
            store=store, token=issued.token, caller=CALLER,
            executable_path=EXECUTABLE, cwd=str(project.root), result_text=raw,
            ack_out=project.ack_out, project_root=str(project.root),
            config_home=str(project.home), on_boundary=crash_at("before_ack"),
        )
    assert not os.path.exists(project.ack_out)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    outcome = acknowledge(
        store=store, token=issued.token, caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root), result_text=raw, ack_out=project.ack_out,
        project_root=str(project.root), config_home=str(project.home),
    )
    assert outcome.state == "acknowledged"


def test_crash_before_cleanup_leaves_the_receipt_and_allows_a_retry(project, resolved):
    store, issued = issue_plan(project, resolved)
    run(project, resolved, store, issued.token, Recorder())
    raw = open(project.result_out, encoding="utf-8").read()
    with pytest.raises(Crash):
        acknowledge(
            store=store, token=issued.token, caller=CALLER,
            executable_path=EXECUTABLE, cwd=str(project.root), result_text=raw,
            ack_out=project.ack_out, project_root=str(project.root),
            config_home=str(project.home), on_boundary=crash_at("before_cleanup"),
        )
    receipt = open(project.ack_out, "rb").read()
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    outcome = acknowledge(
        store=store, token=issued.token, caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root), result_text=raw, ack_out=project.ack_out,
        project_root=str(project.root), config_home=str(project.home),
    )
    assert outcome.state == "acknowledged"
    assert open(project.ack_out, "rb").read() == receipt
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")


def test_no_crash_path_ever_produces_a_second_remote_mutation(project, resolved):
    store, issued = issue_plan(project, resolved)
    totals = []
    for boundary in PUBLISH_BOUNDARIES:
        recorder = Recorder()
        try:
            run(project, resolved, store, issued.token, recorder,
                on_boundary=crash_at(boundary))
        except Crash:
            pass
        totals.append(len(recorder.calls))
    assert sum(totals) <= 1
