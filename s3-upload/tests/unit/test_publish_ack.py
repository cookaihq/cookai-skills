import inspect
import os
import socket

import pytest

from conftest import CALLER
from delivery_records import result_hash as compute_result_hash
from delivery_schema import body_of, build_typed, parse_typed, serialize_artifact
from delivery_workflow import WorkflowError, acknowledge, publish
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


def published(project, resolved):
    store = PlanStore(str(project.state_root))
    from planning import build_upload_dry_run
    from target_contract import contract_hash, contract_snapshot
    from planning import derive_contract_key, registry_for_target

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
        issued = store.issue(body, source_path=str(project.source),
                             soft_max_bytes=1048576, created_at=CREATED_AT)
    finally:
        dry_run.close()
    outcome = publish(
        resolved=resolved, store=store, token=issued.token, transport=Recorder(),
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    return store, issued, outcome


def ack(project, store, token, result_text, **overrides):
    kwargs = dict(
        store=store, token=token, caller=CALLER, executable_path=EXECUTABLE,
        cwd=str(project.root), result_text=result_text, ack_out=project.ack_out,
        project_root=str(project.root), config_home=str(project.home),
    )
    kwargs.update(overrides)
    return acknowledge(**kwargs)


def test_acknowledge_has_no_transport_parameter():
    assert "transport" not in inspect.signature(acknowledge).parameters


def test_acknowledge_makes_no_network_call(project, resolved, monkeypatch):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()

    def explode(*args, **kwargs):
        raise AssertionError("ack must not perform network I/O")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    result = ack(project, store, issued.token, raw)
    assert result.state == "acknowledged"


def test_acknowledge_writes_a_receipt_and_cleans_internal_state(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    result = ack(project, store, issued.token, raw)
    receipt = parse_typed(open(project.ack_out, encoding="utf-8").read(),
                          expected_type="s3-upload.ack")
    body = body_of(receipt)
    assert body["acknowledged"] is True
    assert body["plan_id"] == issued.plan_id
    assert body["result_hash"] == body_of(outcome.result)["result_hash"]
    assert body["predecessor_operation_id"] == body_of(outcome.result)["operation_id"]
    assert body["root_recovery_id"] == body_of(outcome.result)["root_recovery_id"]
    assert result.ack == receipt
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert not os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "tombstone.json")


def test_result_and_recovery_artifacts_survive_cleanup(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "rb").read()
    recovery = open(project.recovery_out, "rb").read()
    ack(project, store, issued.token, raw.decode("utf-8"))
    assert open(project.result_out, "rb").read() == raw
    assert open(project.recovery_out, "rb").read() == recovery


def test_acknowledge_is_idempotent_and_byte_identical(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    first = ack(project, store, issued.token, raw)
    receipt = open(project.ack_out, "rb").read()
    second = ack(project, store, issued.token, raw)
    assert second.state == "already_acknowledged"
    assert serialize_artifact(second.ack) == serialize_artifact(first.ack)
    assert open(project.ack_out, "rb").read() == receipt


def test_a_result_with_a_forged_hash_does_not_clean_anything(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    forged = raw.replace(body_of(outcome.result)["result_hash"], "sha256:" + "9" * 64)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, forged)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    assert not os.path.exists(project.ack_out)


def test_a_result_from_another_plan_does_not_clean_anything(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    other = raw.replace(issued.plan_id, "f" * 32)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, other)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert not os.path.exists(project.ack_out)


def test_a_result_that_does_not_match_the_durable_result_out_is_rejected(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    os.chmod(project.result_out, 0o600)
    with open(project.result_out, "w", encoding="utf-8") as handle:
        handle.write(raw.replace('"publish"', '"reconcile"'))
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")


def test_a_foreign_caller_cannot_acknowledge(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw, caller="vi-pdf2md")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")


def test_publish_after_acknowledge_reports_the_tombstone_and_sends_nothing(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    ack(project, store, issued.token, raw)
    second = Recorder()
    replay = publish(
        resolved=resolved, store=store, token=issued.token, transport=second,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    assert second.calls == []
    assert replay.transport_calls == 0
    assert body_of(replay.result)["blocking_reasons"] == ["already_acknowledged"]
    assert body_of(replay.result)["recovery_state"] == "terminal_acknowledged"


def test_a_tampered_result_with_the_recorded_hash_cannot_rewrite_a_lost_receipt(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    ack(project, store, issued.token, raw)
    os.unlink(project.ack_out)
    tampered = raw.replace(body_of(outcome.result)["recovery_id"], "e" * 32)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, tampered)
    assert not os.path.exists(project.ack_out)
    result = ack(project, store, issued.token, raw)
    assert result.state == "already_acknowledged"
    assert os.path.exists(project.ack_out)


def test_a_recomposed_result_cannot_replace_a_lost_receipt(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    ack(project, store, issued.token, raw)
    os.unlink(project.ack_out)
    body = dict(body_of(outcome.result))
    body["predecessor_operation_id"] = "a" * 32
    body["result_hash"] = compute_result_hash(body)
    forged = serialize_artifact(build_typed("s3-upload.result", body)).decode("utf-8")
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, forged)
    assert not os.path.exists(project.ack_out)


def test_an_unreadable_result_out_stops_the_ack(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    os.chmod(project.result_out, 0o644)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert not os.path.exists(project.ack_out)
    os.chmod(project.result_out, 0o600)
    result = ack(project, store, issued.token, raw)
    assert result.state == "acknowledged"


def test_a_corrupt_operation_record_stops_the_ack(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    os.chmod(project.state_root / "plans" / issued.plan_id / "operation.json", 0o644)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert not os.path.exists(project.ack_out)


def test_an_unsafe_ack_destination_cleans_nothing(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw,
            ack_out=str(project.state_root / "ack.json"))
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    assert not os.path.exists(project.state_root / "ack.json")


def test_a_failed_cleanup_after_the_receipt_is_retryable(project, resolved, monkeypatch):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    real_unlink = os.unlink

    def deny_spool(name, *args, **kwargs):
        if name == "spool" and "dir_fd" in kwargs:
            raise OSError("injected cleanup failure")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny_spool)
    with pytest.raises(WorkflowError):
        ack(project, store, issued.token, raw)
    monkeypatch.undo()
    assert os.path.exists(project.ack_out)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "tombstone.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    receipt = open(project.ack_out, "rb").read()
    second = ack(project, store, issued.token, raw)
    assert second.state == "already_acknowledged"
    assert open(project.ack_out, "rb").read() == receipt
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert not os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert not os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
