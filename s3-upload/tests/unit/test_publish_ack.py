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
from strict_json import canonicalize, loads


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(200)


def published(project, resolved, transport=None):
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
        resolved=resolved, store=store, token=issued.token,
        transport=transport if transport is not None else Recorder(),
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
    # Byte-identity is promised for a same-caller replay: the receipt's caller
    # field records who obtained this issuance, and a replay is bound by the
    # token digest plus the recorded result hash, not by the original caller.
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
    # The forged result must recompute its own result_hash: a plain string
    # substitution breaks the self-consistent hash and is stopped by the hash
    # check one line earlier, so it never proves the plan_id binding exists.
    store, issued, outcome = published(project, resolved)
    body = dict(body_of(outcome.result))
    body["plan_id"] = "f" * 32
    body["result_hash"] = compute_result_hash(body)
    other = serialize_artifact(build_typed("s3-upload.result", body)).decode("utf-8")
    with pytest.raises(WorkflowError, match="belongs to another plan"):
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
    # The outcome half of this exit had no assertion anywhere in the suite:
    # changing it to "created" left all 1085 tests green (probe, full scope).
    # The result would then have claimed a write that this call demonstrably
    # did not perform -- it sent nothing at all, as the two assertions above
    # record -- so the reason and the outcome must both be pinned here.
    assert body_of(replay.result)["outcome"] == "blocked"
    assert body_of(replay.result)["object_written"] is False


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


def test_a_non_terminal_result_cannot_be_acknowledged(project, resolved):
    def timing_out(method, url, headers, body):
        raise TimeoutError("injected transport timeout")

    store, issued, outcome = published(project, resolved, transport=timing_out)
    assert body_of(outcome.result)["recovery_state"] == "in_flight_unknown"
    raw = open(project.result_out, "r", encoding="utf-8").read()
    with pytest.raises(WorkflowError, match="only a terminal result"):
        ack(project, store, issued.token, raw)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )
    assert not os.path.exists(project.ack_out)


def test_an_unsafe_checkpoint_store_does_not_take_back_a_durable_ack(project, resolved,
                                                                     monkeypatch):
    import artifacts as artifacts_module

    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    os.unlink(project.state_root / "checkpoints" / ".gitignore")
    real_write = artifacts_module.atomic_write

    def racing(path, data, **kwargs):
        # A concurrent creator wins the guard write between remove()'s probe
        # and its create-once attempt; the loser then re-reads the guard, and
        # an unsafe re-read surfaces a bare FileSecurityError (a ValueError,
        # not an OSError) out of CheckpointStore.remove.
        if path.endswith(os.path.join("checkpoints", ".gitignore")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("*\n!.gitignore\n")
            os.chmod(path, 0o644)
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(artifacts_module, "atomic_write", racing)
    result = ack(project, store, issued.token, raw)
    assert result.state == "acknowledged"
    assert os.path.exists(project.ack_out)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "tombstone.json")
    # The refused removal leaves the checkpoint behind; the ack stands anyway.
    assert os.path.exists(
        project.state_root / "checkpoints" / (outcome.checkpoint_id + ".json")
    )


def test_a_type_polluted_operation_record_is_refused_not_a_crash(project, resolved):
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    path = project.state_root / "plans" / issued.plan_id / "operation.json"
    value = loads(path.read_text(encoding="utf-8"))
    value["result_out"] = 42
    os.unlink(path)
    path.write_bytes(canonicalize(value))
    os.chmod(path, 0o600)
    with pytest.raises(WorkflowError, match="could not be read back"):
        ack(project, store, issued.token, raw)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "record.json")
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "spool")
    assert not os.path.exists(project.ack_out)


def test_a_publish_replay_racing_an_ack_reports_already_acknowledged(project, resolved):
    # Deterministic interleaving: publish's consume probes the tombstone
    # (None), a full acknowledgement lands in between, and the record read that
    # follows finds the file gone. The replay must answer with the durable
    # terminal state, not report the caller's own token as invalid.
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    real = store.load_tombstone
    injected = {"done": False}

    def interleave(plan_id):
        value = real(plan_id)
        if value is None and not injected["done"]:
            injected["done"] = True
            ack(project, PlanStore(str(project.state_root)), issued.token, raw)
        return value

    store.load_tombstone = interleave
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
    assert os.path.exists(project.ack_out)
    assert os.path.exists(project.state_root / "plans" / issued.plan_id / "tombstone.json")


def test_a_forged_token_crossing_the_ack_race_window_is_still_refused(project, resolved):
    # Same deterministic interleaving as the replay test above, but with a
    # forged token: consume probes the tombstone (None), a full
    # acknowledgement lands in between, and the record read finds the file
    # gone. The fallback's tombstone re-read then answers the token, and its
    # compare_digest is the only guard left between a forgery and the durable
    # settlement -- the forgery must come back token_invalid, never
    # already_acknowledged.
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    forged = issued.plan_id + ".not-the-secret"
    real = store.load_tombstone
    injected = {"done": False}

    def interleave(plan_id):
        value = real(plan_id)
        if value is None and not injected["done"]:
            injected["done"] = True
            ack(project, PlanStore(str(project.state_root)), issued.token, raw)
        return value

    store.load_tombstone = interleave
    second = Recorder()
    replay = publish(
        resolved=resolved, store=store, token=forged, transport=second,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    assert second.calls == []
    assert replay.transport_calls == 0
    assert body_of(replay.result)["blocking_reasons"] == ["token_invalid"]
    assert body_of(replay.result)["recovery_state"] == "blocked"


def test_a_replay_after_the_tombstone_is_bound_by_the_token_not_the_caller(project, resolved):
    # Ruled semantics: the token is the credential. Once the tombstone exists,
    # the plan record that named the original caller is gone, so a replay is
    # bound by the token digest and the recorded result hash alone -- no
    # caller (or executable/cwd) check is re-imposed, and the receipt's caller
    # field records who presented the token this time.
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    first = ack(project, store, issued.token, raw)
    assert first.state == "acknowledged"
    replayer_ack_out = str(project.out / "replayer-ack.json")
    replayed = ack(
        project, store, issued.token, raw,
        caller="vi-pdf2md",
        executable_path="/usr/local/bin/python3",
        cwd=str(project.home),
        ack_out=replayer_ack_out,
    )
    assert replayed.state == "already_acknowledged"
    assert body_of(replayed.ack)["caller"] == "vi-pdf2md"
    assert body_of(replayed.ack)["result_hash"] == body_of(first.ack)["result_hash"]
    assert os.path.exists(replayer_ack_out)


def test_two_acks_racing_at_the_tombstone_write_both_succeed(project, resolved):
    # Deterministic interleaving: the loser loads the record inside _finish,
    # the winner completes a whole acknowledgement, and the loser's create-once
    # tombstone write fails with FileExistsError. The durable receipt on both
    # sides is the same settlement, so both callers succeed.
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    real = store.load_record
    calls = {"n": 0}
    winner = {}

    def interleave(plan_id):
        value = real(plan_id)
        calls["n"] += 1
        if calls["n"] == 2:  # the read taken inside _finish, before the write
            winner["outcome"] = ack(
                project, PlanStore(str(project.state_root)), issued.token, raw
            )
        return value

    store.load_record = interleave
    loser = ack(project, store, issued.token, raw)
    assert winner["outcome"].state in {"acknowledged", "already_acknowledged"}
    assert loser.state in {"acknowledged", "already_acknowledged"}
    assert serialize_artifact(loser.ack) == serialize_artifact(winner["outcome"].ack)
    plan_dir = project.state_root / "plans" / issued.plan_id
    assert os.path.exists(plan_dir / "tombstone.json")
    assert not os.path.exists(plan_dir / "record.json")
    assert not os.path.exists(plan_dir / "spool")
    assert os.path.exists(project.ack_out)


def test_two_acks_racing_at_the_record_read_both_succeed(project, resolved):
    # Deterministic interleaving: the loser's _finish probes the tombstone
    # (None), the winner completes a whole acknowledgement, and the loser's
    # record read then finds the file gone. The re-read of the tombstone
    # recognises the identical settlement, so both callers succeed.
    store, issued, outcome = published(project, resolved)
    raw = open(project.result_out, "r", encoding="utf-8").read()
    real = store.load_tombstone
    calls = {"n": 0}
    winner = {}

    def interleave(plan_id):
        value = real(plan_id)
        calls["n"] += 1
        if calls["n"] == 2 and value is None:  # the probe inside _finish
            winner["outcome"] = ack(
                project, PlanStore(str(project.state_root)), issued.token, raw
            )
        return value

    store.load_tombstone = interleave
    loser = ack(project, store, issued.token, raw)
    assert winner["outcome"].state in {"acknowledged", "already_acknowledged"}
    assert loser.state in {"acknowledged", "already_acknowledged"}
    assert serialize_artifact(loser.ack) == serialize_artifact(winner["outcome"].ack)
    plan_dir = project.state_root / "plans" / issued.plan_id
    assert os.path.exists(plan_dir / "tombstone.json")
    assert not os.path.exists(plan_dir / "record.json")
    assert not os.path.exists(plan_dir / "spool")
    assert os.path.exists(project.ack_out)


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
