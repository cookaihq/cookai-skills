import json
import os

import pytest

from conftest import CALLER, SOURCE_BYTES, write_target
from delivery_schema import body_of
from delivery_workflow import TransportGate, TransportSealed, publish
from plan_store import PlanStore, build_plan_body, new_plan_id
from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from resolver import resolve_target
from s3 import Response
from target_contract import contract_hash, contract_snapshot


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"


class Recorder:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or Response(200)

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.response


def issue_plan(project, dry_run, snapshot, digest, **overrides):
    store = PlanStore(str(project.state_root))
    body = build_plan_body(
        dry_run_plan=dry_run.plan,
        source_snapshot=dry_run.source.snapshot,
        target_contract=snapshot,
        target_contract_hash=digest,
        caller=CALLER,
        executable_path=EXECUTABLE,
        cwd=str(project.root),
        state_root=str(project.state_root),
        recovery_out=project.recovery_out,
        result_out=project.result_out,
        plan_id=new_plan_id(),
    )
    body.update(overrides)
    return store, store.issue(body, source_path=str(project.source),
                              soft_max_bytes=1048576, created_at=CREATED_AT)


def run(project, resolved, store, token, transport, **overrides):
    kwargs = dict(
        resolved=resolved, store=store, token=token, transport=transport,
        project_root=str(project.root), config_home=str(project.home),
        caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
    )
    kwargs.update(overrides)
    return publish(**kwargs)


def test_sealed_gate_refuses_before_arming():
    raw = Recorder()
    gate = TransportGate(raw)
    assert gate.armed is False
    with pytest.raises(TransportSealed):
        gate("PUT", "https://example.invalid/a", {}, b"")
    assert raw.calls == []
    assert gate.calls == 0
    gate.arm()
    gate("PUT", "https://example.invalid/a", {}, b"")
    assert len(raw.calls) == 1
    assert gate.calls == 1


def test_publish_succeeds_and_sends_exactly_one_request(project, resolved, dry_run, snapshot, contract_digest):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    body = body_of(outcome.result)
    assert len(raw.calls) == 1
    assert outcome.transport_calls == 1
    assert body["recovery_state"] == "terminal_unacknowledged"
    assert body["blocking_reasons"] == []
    assert body["operation"] == "publish"


def test_publish_sends_only_spool_bytes(project, resolved, dry_run, snapshot, contract_digest, monkeypatch):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    monkeypatch.setattr(
        "source_file.VerifiedSource.single_put_bytes",
        lambda self: b"BYTES-READ-FROM-THE-LIVE-SOURCE-FILE",
    )
    outcome = run(project, resolved, store, issued.token, raw)
    assert raw.calls[0][3] == SOURCE_BYTES
    assert raw.calls[0][3] != b"BYTES-READ-FROM-THE-LIVE-SOURCE-FILE"
    assert outcome.transport_calls == 1


def test_publish_sends_frozen_bytes_when_the_source_is_rewritten_after_revalidation(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()

    def rewrite(name):
        if name == "revalidated":
            project.source.write_bytes(b"OVERWRITTEN")

    outcome = run(project, resolved, store, issued.token, raw, on_boundary=rewrite)
    assert raw.calls[0][3] == SOURCE_BYTES
    assert project.source.read_bytes() == b"OVERWRITTEN"
    assert outcome.transport_calls == 1


@pytest.mark.parametrize("overrides,reason", [
    ({"caller": "vi-pdf2md"}, "token_invalid"),
    ({"executable_path": "/opt/other/python3"}, "token_invalid"),
])
def test_publish_rejects_identity_drift_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest, overrides, reason
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw, **overrides)
    assert raw.calls == []
    assert outcome.transport_calls == 0
    assert body_of(outcome.result)["blocking_reasons"] == [reason]
    assert not os.path.exists(project.recovery_out)
    assert not os.path.exists(project.result_out)


def test_publish_rejects_target_contract_drift_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    write_target(project, prefix="other-images/")
    drifted = resolve_target(
        cwd=str(project.root), config_home=str(project.home), environ={},
        cli_target=None, cli_caller=CALLER, use_local_key=False,
    )
    raw = Recorder()
    outcome = run(project, drifted, store, issued.token, raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["target_contract_drift"]
    assert not os.path.exists(project.recovery_out)


def test_publish_rejects_public_base_drift_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    write_target(project, access={
        "mode": "public",
        "public_base_url": "https://cdn.example.com/",
        "presign_expires_seconds": None,
    })
    drifted = resolve_target(
        cwd=str(project.root), config_home=str(project.home), environ={},
        cli_target=None, cli_caller=CALLER, use_local_key=False,
    )
    raw = Recorder()
    outcome = run(project, drifted, store, issued.token, raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["target_contract_drift"]


def test_publish_rejects_source_drift_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    project.source.write_bytes(b"different!!")
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["source_drift"]
    assert not os.path.exists(project.recovery_out)


def test_publish_rejects_a_removed_source_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    project.source.unlink()
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["source_drift"]


def test_publish_rejects_unsafe_handoff_paths_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(
        project, dry_run, snapshot, contract_digest,
        result_out=str(project.root / ".env.local"),
    )
    raw = Recorder()
    outcome = run(project, resolved, store, issued.token, raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["handoff_unsafe"]


def test_publish_rejects_an_unknown_token_before_any_request(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, _ = issue_plan(project, dry_run, snapshot, contract_digest)
    raw = Recorder()
    outcome = run(project, resolved, store, new_plan_id() + ".fabricated", raw)
    assert raw.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["token_invalid"]
    assert body_of(outcome.result)["recovery_state"] == "blocked"
    assert body_of(outcome.result)["allowed_actions"] == ["inspect"]


def test_publish_holds_the_project_target_and_plan_locks(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    held = []

    def observe(name):
        if name == "revalidated":
            for lock_name in ("project", "plan-" + issued.plan_id):
                try:
                    with PlanStore(str(project.state_root)).lock(lock_name):
                        held.append(("free", lock_name))
                except Exception:
                    held.append(("held", lock_name))

    run(project, resolved, store, issued.token, Recorder(), on_boundary=observe)
    assert held == [("held", "project"), ("held", "plan-" + issued.plan_id)]


def test_publish_never_finalizes_the_internal_checkpoint(
    project, resolved, dry_run, snapshot, contract_digest
):
    store, issued = issue_plan(project, dry_run, snapshot, contract_digest)
    outcome = run(project, resolved, store, issued.token, Recorder())
    checkpoints = sorted(
        item.name for item in (project.state_root / "checkpoints").iterdir()
        if item.suffix == ".json"
    )
    assert checkpoints == [outcome.checkpoint_id + ".json"]
