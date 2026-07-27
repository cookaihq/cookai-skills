import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CALLER
from delivery_schema import body_of, parse_typed
from delivery_workflow import acknowledge, publish
from plan_store import PlanStore, build_plan_body, new_plan_id
from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from s3 import Response
from target_contract import contract_hash, contract_snapshot


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"
CHILD = Path(__file__).parent / "crash_child.py"
SCRIPTS = str(Path(__file__).parents[2] / "scripts")

DURABILITY_BOUNDARIES = (
    "before_recovery_fsync",
    "after_recovery_fsync",
    "before_result_fsync",
    "after_result_fsync",
)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(200)


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


def run_child(project, issued, boundary, phase, counter):
    payload = {
        "scripts": SCRIPTS,
        "state_root": str(project.state_root),
        "cwd": str(project.root),
        "config_home": str(project.home),
        "caller": CALLER,
        "executable_path": EXECUTABLE,
        "token": issued.token,
        "boundary": boundary,
        "phase": phase,
        "counter": str(counter),
        "result_out": project.result_out,
        "ack_out": project.ack_out,
    }
    return subprocess.run(
        [sys.executable, str(CHILD), json.dumps(payload)],
        capture_output=True, text=True, timeout=120,
    )


def on_disk_requests(counter):
    return 0 if not os.path.exists(counter) else os.path.getsize(counter)


@pytest.mark.parametrize("boundary", DURABILITY_BOUNDARIES)
def test_forced_termination_is_really_forced(project, resolved, boundary):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    completed = run_child(project, issued, boundary, "publish", counter)
    assert completed.returncode == -signal.SIGKILL, completed.stderr


def test_kill_before_recovery_fsync_leaves_no_descriptor_and_no_request(
    project, resolved
):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "before_recovery_fsync", "publish", counter)
    assert on_disk_requests(counter) == 0
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)


def test_kill_after_recovery_fsync_leaves_a_durable_descriptor(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "after_recovery_fsync", "publish", counter)
    assert on_disk_requests(counter) == 0
    descriptor = parse_typed(open(project.recovery_out, encoding="utf-8").read(),
                             expected_type="s3-upload.recovery-descriptor")
    body = body_of(descriptor)
    assert body["plan_id"] == issued.plan_id
    assert body["recovery_state"] == "in_flight_unknown"
    assert body["allowed_actions"] == ["inspect", "reconcile"]


def test_new_process_after_recovery_fsync_kill_sends_nothing(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "after_recovery_fsync", "publish", counter)
    recorder = Recorder()
    outcome = publish(
        resolved=resolved, store=PlanStore(str(project.state_root)), token=issued.token,
        transport=recorder, project_root=str(project.root),
        config_home=str(project.home), caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root),
    )
    assert recorder.calls == []
    assert outcome.transport_calls == 0
    assert on_disk_requests(counter) == 0
    assert body_of(outcome.result)["recovery_state"] == "in_flight_unknown"


def test_kill_before_result_fsync_records_exactly_one_on_disk_request(
    project, resolved
):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "before_result_fsync", "publish", counter)
    assert on_disk_requests(counter) == 1
    assert os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)


def test_new_process_after_result_fsync_kill_never_repeats_the_put(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "after_result_fsync", "publish", counter)
    assert on_disk_requests(counter) == 1
    durable = open(project.result_out, "rb").read()
    recorder = Recorder()
    outcome = publish(
        resolved=resolved, store=PlanStore(str(project.state_root)), token=issued.token,
        transport=recorder, project_root=str(project.root),
        config_home=str(project.home), caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root),
    )
    assert recorder.calls == []
    assert on_disk_requests(counter) == 1
    assert open(project.result_out, "rb").read() == durable
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"


def test_kill_inside_the_transport_window_records_the_request_first(
    project, resolved
):
    """Cover the operations.py transport-call window no BOUNDARIES hook reaches:
    the request has left the process but the in-flight checkpoint was never
    advanced. The child's transport fsyncs the counter and then SIGKILLs
    itself before returning the response, so the on-disk order is pinned:
    descriptor first, then the request, and no result ever."""
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    completed = run_child(project, issued, "in_transport", "publish", counter)
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    assert on_disk_requests(counter) == 1
    descriptor = parse_typed(open(project.recovery_out, encoding="utf-8").read(),
                             expected_type="s3-upload.recovery-descriptor")
    assert body_of(descriptor)["recovery_state"] == "in_flight_unknown"
    assert not os.path.exists(project.result_out)


def test_new_process_after_a_transport_window_kill_sends_nothing(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "in_transport", "publish", counter)
    assert on_disk_requests(counter) == 1
    recorder = Recorder()
    outcome = publish(
        resolved=resolved, store=PlanStore(str(project.state_root)), token=issued.token,
        transport=recorder, project_root=str(project.root),
        config_home=str(project.home), caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root),
    )
    assert recorder.calls == []
    assert outcome.transport_calls == 0
    assert on_disk_requests(counter) == 1
    assert body_of(outcome.result)["recovery_state"] == "in_flight_unknown"


def test_kill_before_ack_keeps_every_recoverable_artifact(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "after_result_fsync", "publish", counter)
    completed = run_child(project, issued, "before_ack", "ack", counter)
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    assert on_disk_requests(counter) == 1
    assert not os.path.exists(project.ack_out)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")


def test_kill_before_cleanup_keeps_the_receipt_and_allows_an_idempotent_retry(
    project, resolved
):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    run_child(project, issued, "after_result_fsync", "publish", counter)
    completed = run_child(project, issued, "before_cleanup", "ack", counter)
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    receipt = open(project.ack_out, "rb").read()
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    outcome = acknowledge(
        store=PlanStore(str(project.state_root)), token=issued.token, caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
        result_text=open(project.result_out, encoding="utf-8").read(),
        ack_out=project.ack_out, project_root=str(project.root),
        config_home=str(project.home),
    )
    assert outcome.state == "acknowledged"
    assert open(project.ack_out, "rb").read() == receipt
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert on_disk_requests(counter) == 1


def test_the_counter_is_written_by_the_child_not_the_parent(project, resolved):
    store, issued = issue_plan(project, resolved)
    counter = project.root / "requests.count"
    assert on_disk_requests(counter) == 0
    run_child(project, issued, "after_result_fsync", "publish", counter)
    assert on_disk_requests(counter) == 1
    assert open(counter, "rb").read() == b"1"
