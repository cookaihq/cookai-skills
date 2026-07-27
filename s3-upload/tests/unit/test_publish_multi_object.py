import json
import os

from conftest import CALLER
from delivery_schema import PLAN_FIELDS, RESULT_FIELDS, body_of
from delivery_workflow import acknowledge, publish
from plan_store import PlanStore, build_plan_body, new_plan_id
from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from s3 import Response
from target_contract import contract_hash, contract_snapshot


EXECUTABLE = "/usr/bin/python3"
CREATED_AT = "2026-07-26T00:00:00Z"
IMAGES = ("page-1.png", "page-2.png", "page-3.png")


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(200)


def plan_for(project, resolved, name, index):
    source = project.root / name
    source.write_bytes(b"image-" + str(index).encode("ascii") * 8)
    out = project.out / name
    out.mkdir()
    store = PlanStore(str(project.state_root))
    dry_run = build_upload_dry_run(
        resolved=resolved, file_path=str(source), explicit_key=None, content_type=None,
        cache_control=None, content_disposition=None, presign_expires=None,
        reference_out=None, project_root=str(project.root),
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
            state_root=str(project.state_root),
            recovery_out=str(out / "recovery.json"), result_out=str(out / "result.json"),
            plan_id=new_plan_id(),
        )
        issued = store.issue(body, source_path=str(source), soft_max_bytes=1048576,
                             created_at=CREATED_AT)
    finally:
        dry_run.close()
    return store, issued, out


def test_plan_and_result_have_no_cross_object_field():
    for field in PLAN_FIELDS + RESULT_FIELDS:
        assert "batch" not in field
        assert "sibling" not in field
        assert "transaction" not in field
    assert "object_key" in PLAN_FIELDS
    assert "object_keys" not in PLAN_FIELDS


def test_each_image_gets_an_independent_plan_identity(project, resolved):
    issued = [plan_for(project, resolved, name, index)
              for index, name in enumerate(IMAGES)]
    plan_ids = [item[1].plan_id for item in issued]
    tokens = [item[1].token for item in issued]
    assert len(set(plan_ids)) == 3
    assert len(set(tokens)) == 3
    for _, item, _ in issued:
        body = body_of(item.artifact)
        assert isinstance(body["object_key"], str)
        assert body["source"]["path"].endswith(".png")


def test_a_token_cannot_publish_another_objects_plan(project, resolved):
    first = plan_for(project, resolved, IMAGES[0], 0)
    second = plan_for(project, resolved, IMAGES[1], 1)
    raw = Recorder()
    outcome = publish(
        resolved=resolved, store=second[0], token=first[1].token, transport=raw,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    body = body_of(outcome.result)
    assert body["plan_id"] == first[1].plan_id
    assert body["plan_id"] != second[1].plan_id
    assert len(raw.calls) == 1
    assert os.path.exists(first[2] / "result.json")
    assert not os.path.exists(second[2] / "result.json")


def test_a_failed_sibling_does_not_roll_back_a_published_object(project, resolved):
    first = plan_for(project, resolved, IMAGES[0], 0)
    second = plan_for(project, resolved, IMAGES[1], 1)
    ok = Recorder()
    publish(
        resolved=resolved, store=first[0], token=first[1].token, transport=ok,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    frozen = open(first[2] / "result.json", "rb").read()
    (project.root / IMAGES[1]).write_bytes(b"rewritten-after-plan")
    blocked = Recorder()
    outcome = publish(
        resolved=resolved, store=second[0], token=second[1].token, transport=blocked,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )
    assert blocked.calls == []
    assert body_of(outcome.result)["blocking_reasons"] == ["source_drift"]
    assert open(first[2] / "result.json", "rb").read() == frozen
    assert len(ok.calls) == 1


def _run(project, resolved, item, transport):
    return publish(
        resolved=resolved, store=item[0], token=item[1].token, transport=transport,
        project_root=str(project.root), config_home=str(project.home), caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
    )


def _ack(project, item):
    return acknowledge(
        store=item[0], token=item[1].token, caller=CALLER,
        executable_path=EXECUTABLE, cwd=str(project.root),
        result_text=open(item[2] / "result.json", encoding="utf-8").read(),
        ack_out=str(item[2] / "ack.json"), project_root=str(project.root),
        config_home=str(project.home),
    )


def test_acknowledging_one_object_leaves_its_siblings_publishable(project, resolved):
    first = plan_for(project, resolved, IMAGES[0], 0)
    second = plan_for(project, resolved, IMAGES[1], 1)
    _run(project, resolved, first, Recorder())
    assert _ack(project, first).state == "acknowledged"
    late = Recorder()
    outcome = _run(project, resolved, second, late)
    assert len(late.calls) == 1
    assert body_of(outcome.result)["recovery_state"] == "terminal_unacknowledged"
    receipt = _ack(project, second)
    assert receipt.state == "acknowledged"
    assert body_of(receipt.ack)["plan_id"] == second[1].plan_id
    assert os.path.exists(first[2] / "ack.json")


def test_caller_aggregation_needs_three_separate_publishes(project, resolved):
    issued = [plan_for(project, resolved, name, index)
              for index, name in enumerate(IMAGES)]
    recorders = []
    for store, item, out in issued:
        recorder = Recorder()
        recorders.append(recorder)
        publish(
            resolved=resolved, store=store, token=item.token, transport=recorder,
            project_root=str(project.root), config_home=str(project.home),
            caller=CALLER, executable_path=EXECUTABLE, cwd=str(project.root),
        )
    assert [len(recorder.calls) for recorder in recorders] == [1, 1, 1]
    roots = set()
    for _, _, out in issued:
        item = json.loads(open(out / "result.json", encoding="utf-8").read())
        roots.add(body_of(item)["root_recovery_id"])
    assert len(roots) == 3
