import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from PIL import Image

from video_to_markdown import cli, config, media, sampling, state, vision
from test_review import fixture_task


def result_for(payload, *, uncertain=False):
    return {"schema_version": 1, "checks": [], "observations": [{
        "observation_id": "fact-" + payload["dispatch_id"], "start_ms": payload["batch_start_ms"], "end_ms": payload["batch_end_ms"],
        "description": "A visible synthetic title", "evidence_frame_ids": [payload["frames"][0]["frame_id"]],
        "motion_dependent": False, "confidence": "uncertain" if uncertain else "certain", "needs_resample": uncertain,
        "resample_reason": "unreadable title" if uncertain else None}]}


def api_task(tmp_path):
    task, manifest = fixture_task(tmp_path)
    route = {"method": "api", "base_url": "https://synthetic.invalid/v1", "api_key": "synthetic-private-key",
             "model": "vision-example", "protocol": "chat-completions"}
    route_file = tmp_path / "private-route.json"
    state.atomic_write_json(route_file, route)
    manifest["vision"].update(route=vision.safe_route(route), source="task option", credential_ref=str(route_file), dispatches=[], model_calls=0, internal_model_calls_unknown=False)
    dispatch = vision.prepare_dispatch(task, manifest, "batch-0001")[0]
    return task, manifest, dispatch


def test_all_four_options_are_returned_even_with_unknown_host_capabilities():
    value = vision.options(vision.capabilities(host="workbuddy"))
    assert {c["method"] for c in value["choices"]} == set(vision.METHODS)
    assert value["models"] == []
    assert next(c for c in value["choices"] if c["method"] == "host-agent")["available"] is None


def test_default_asks_and_selection_survives_configuration_changes(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    manifest, _ = state.initialize_task(task, settings_snapshot={})
    resolved = config.resolve({}, tmp_path)
    caps = {"host": "codex", "source": "session tool", "main_vision": True, "models": []}
    value = vision.select(task, manifest, resolved, host="codex", caps=caps)
    assert value["next_action"] == "choose_vision_once" and len(value["choices"]) == 4
    vision.select(task, manifest, resolved, host="codex", caps=caps, override={"method": "main-agent"})
    config.update_file(tmp_path / ".env", {config.VISION_KEY: json.dumps({"ask_each_task": True, "routes": {}})})
    again = vision.select(task, manifest, config.resolve({}, tmp_path), host="codex", caps=caps)
    assert again["next_action"] == "inspect_batch"
    assert manifest["vision"]["route"] == {"method": "main-agent"}


def test_disabled_model_and_unknown_manual_routing_never_fall_back(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    manifest, _ = state.initialize_task(task, settings_snapshot={})
    caps = {"host": "workbuddy", "source": "current session", "native_agent": True,
            "models": [{"id": "disabled", "vision": True, "available": False}]}
    resolved = config.resolve({}, tmp_path)
    with pytest.raises(vision.VisionError, match="disabled"):
        vision.select(task, manifest, resolved, host="workbuddy", caps=caps, override={"method": "host-agent", "model": "disabled"})
    with pytest.raises(vision.VisionError, match="absent"):
        vision.select(task, manifest, resolved, host="workbuddy", caps=caps, override={"method": "host-agent", "model": "invented"})
    assert manifest["vision"]["route"] is None


def test_five_second_candidates_keep_all_61_plus_short_local_changes(monkeypatch):
    monkeypatch.setattr(media, "scene_candidates", lambda *a, **k: [23_400])
    monkeypatch.setattr(media, "local_change_candidates", lambda *a, **k: [{"requested_ms": 2100, "score": .06}])
    candidates = sampling.first_pass_candidates(Path("synthetic.mp4"), {"start_ms": 0, "end_ms": 300_000}, ffmpeg_bin="ffmpeg")
    assert len(candidates) == 63
    assert set(range(0, 300_000, 5000)) <= {c["requested_ms"] for c in candidates}
    assert 299_999 in {c["requested_ms"] for c in candidates}
    assert next(c for c in candidates if c["requested_ms"] == 2100)["local_change_signal"]["score"] == .06


def test_capacity_splits_preserve_every_original_and_saved_batch_identity(tmp_path):
    task, manifest = fixture_task(tmp_path)
    summary, batch = sampling.load_batch(task, manifest, "batch-0001")
    frames = []
    for n in range(61):
        path = task / "assets" / "frames" / ("extra-%d.png" % n)
        Image.new("RGB", (100, 60), (n, 10, 20)).save(path)
        digest, size = state.sha256_file(path)
        frames.append({"frame_id": "extra-%d" % n, "pts_ms": n * 10, "path": str(path), "sha256": digest, "size_bytes": size})
    batch["first_pass"]["frames"] = frames
    state.atomic_write_json(task / summary["state_path"], batch)
    manifest["vision"]["route"]["max_images"] = 7
    dispatches = vision.prepare_dispatch(task, manifest, "batch-0001")
    payloads = [state.read_json(task / d["input"]) for d in dispatches]
    assert len(payloads) == 9
    assert all(len(p["frames"]) <= 7 and p["agent_task_id"] == "batch-0001" for p in payloads)
    assert {f["frame_id"] for p in payloads for f in p["frames"]} == {f["frame_id"] for f in frames}
    assert vision.prepare_dispatch(task, manifest, "batch-0001") == dispatches


def test_api_uses_selected_model_and_preserves_received_result_on_model_mismatch(tmp_path):
    task, manifest, dispatch = api_task(tmp_path)
    calls = []
    def opener(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        payload = json.loads(body["messages"][0]["content"][0]["text"])
        return io.BytesIO(json.dumps({"model": "wrong-model", "choices": [{"message": {"content": json.dumps(result_for(payload))}}]}).encode())
    with pytest.raises(vision.VisionError, match="differs"):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=opener)
    assert calls[0]["model"] == "vision-example"
    assert "synthetic-private-key" not in json.dumps(manifest)
    assert "synthetic-private-key" not in json.dumps(calls)
    assert (task / (".state/vision/" + dispatch["dispatch_id"] + "-received.json")).exists()
    with pytest.raises(vision.VisionError, match="reconcile"):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=opener)
    assert len(calls) == 1


def test_timeout_does_not_replay_a_possibly_billed_request_after_resume(tmp_path):
    task, manifest, dispatch = api_task(tmp_path)
    calls = []
    def timed_out(*a, **k):
        calls.append(1)
        raise TimeoutError("synthetic timeout")
    with pytest.raises(vision.VisionError):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=timed_out)
    _, restored, _ = state.load_task(task)
    with pytest.raises(vision.VisionError, match="reconcile"):
        vision.execute(task, restored, dispatch["dispatch_id"], opener=timed_out)
    assert calls == [1]
    assert restored["vision"]["model_calls"] == 1


def test_retry_after_is_not_shortened_or_ignored_on_resume(tmp_path):
    task, manifest, dispatch = api_task(tmp_path)
    def limited(*a, **k):
        raise urllib.error.HTTPError("https://synthetic.invalid", 429, "limited", {"Retry-After": "120"}, io.BytesIO(b""))
    with pytest.raises(vision.VisionError, match="429"):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=limited)
    _, restored, _ = state.load_task(task)
    with pytest.raises(vision.VisionError, match="cooldown"):
        vision.execute(task, restored, dispatch["dispatch_id"], opener=limited)
    assert restored["vision"]["model_calls"] == 1


def test_result_recording_is_idempotent_and_models_are_not_silently_substituted(tmp_path):
    task, manifest, dispatch = api_task(tmp_path)
    _, payload = vision.dispatch_input(task, manifest, dispatch["dispatch_id"])
    vision.reserve_call(task, manifest, dispatch)
    value = result_for(payload)
    vision.record_result(task, manifest, dispatch["dispatch_id"], value, actual_model="vision-example", already_reserved=True)
    before = (task / "visual-observations.json").read_bytes()
    vision.record_result(task, manifest, dispatch["dispatch_id"], value, actual_model="vision-example", already_reserved=True)
    assert (task / "visual-observations.json").read_bytes() == before
    assert manifest["vision"]["model_calls"] == 1


@pytest.mark.parametrize("tamper_prior", [False, True])
def test_supplement_combines_inspected_evidence_but_rejects_unseen_or_changed_inputs(tmp_path, tamper_prior):
    task, manifest, first = api_task(tmp_path)
    _, original = vision.dispatch_input(task, manifest, first["dispatch_id"])
    vision.reserve_call(task, manifest, first)
    vision.record_result(task, manifest, first["dispatch_id"], result_for(original),
                         actual_model="vision-example", model_calls=1, already_reserved=True)
    summary, batch = sampling.load_batch(task, manifest, "batch-0001")
    frames = []
    for pts in (100, 200):
        path = task / "assets" / "frames" / ("supplement-%d.png" % pts)
        Image.new("RGB", (100, 60), (pts, 10, 20)).save(path)
        digest, size = state.sha256_file(path)
        frames.append({"frame_id": "supplement-%d" % pts, "pts_ms": pts, "path": str(path),
                       "sha256": digest, "size_bytes": size})
    batch["resampling_rounds"].append({"frames": frames, "contact_sheets": []})
    state.atomic_write_json(task / summary["state_path"], batch)
    second = vision.prepare_dispatch(task, manifest, "batch-0001", frames=frames[:1])[0]
    # Merely prepared images in the same batch remain ineligible as evidence.
    vision.prepare_dispatch(task, manifest, "batch-0001", frames=frames[1:])
    _, payload = vision.dispatch_input(task, manifest, second["dispatch_id"])
    vision.reserve_call(task, manifest, second)
    result = result_for(payload)
    result["observations"][0]["evidence_frame_ids"].append(frames[1]["frame_id"])
    with pytest.raises(vision.VisionError, match="outside"):
        vision.record_result(task, manifest, second["dispatch_id"], result,
                             actual_model="vision-example", model_calls=1, already_reserved=True)
    result["observations"][0]["evidence_frame_ids"] = [frames[0]["frame_id"], original["frames"][0]["frame_id"]]
    if tamper_prior:
        original["instruction"] += " changed"
        state.atomic_write_json(task / first["input"], original)
        with pytest.raises(vision.VisionError, match="input changed"):
            vision.record_result(task, manifest, second["dispatch_id"], result,
                                 actual_model="vision-example", model_calls=1, already_reserved=True)
    else:
        receipt = vision.record_result(task, manifest, second["dispatch_id"], result,
                                       actual_model="vision-example", model_calls=1, already_reserved=True)
        assert receipt["status"] == "complete"
        saved = state.read_json(task / "visual-observations.json")["latest"]
        assert next(o for o in saved if o["observation_id"] == result["observations"][0]["observation_id"])["evidence_frame_ids"] == result["observations"][0]["evidence_frame_ids"]


def test_two_resampling_rounds_are_persistent_per_issue(tmp_path):
    task, manifest, _ = api_task(tmp_path)
    for _ in range(2):
        vision.begin_issue(task, manifest, "blurry-title", start_ms=0, end_ms=1000, reason="title unreadable")
        _, manifest, _ = state.load_task(task)
    with pytest.raises(vision.VisionError, match="budget exhausted"):
        vision.begin_issue(task, manifest, "blurry-title", start_ms=0, end_ms=2000, reason="expand same issue")
    assert manifest["vision"]["issues"]["blurry-title"]["rounds"] == 2


@pytest.mark.parametrize("offset", [False, True])
def test_long_gop_fractional_fps_png_matches_exact_decoded_pts(tmp_path, offset):
    source = tmp_path / "long.mp4"
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=160x90:rate=30000/1001:duration=8", "-c:v", "libx264", "-g", "240"]
    if offset:
        command += ["-output_ts_offset", "10"]
    subprocess.run([*command, str(source)], check=True, capture_output=True, timeout=60)
    for target in (0, 1, 66, 6300, 7999):
        frame = media.extract_frame(source, requested_ms=target, output_dir=tmp_path / "frames", ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        assert frame["pts_verified"] and frame["path"].endswith(".png")
        assert abs(frame["pts_ms"] - target) <= 34
        assert frame["source_pts_ms"] == frame["pts_ms"] + (10_000 if offset else 0)
        with Image.open(frame["path"]) as image:
            assert image.size == (160, 90)


def test_independent_agent_gets_key_only_via_environment_and_unknown_request_count_is_honest(tmp_path, monkeypatch):
    task, manifest, dispatch = api_task(tmp_path)
    _, payload = vision.dispatch_input(task, manifest, dispatch["dispatch_id"])
    route = {**vision.connection(manifest), "method": "independent-agent", "runtime": "codex", "protocol": "responses"}
    monkeypatch.setattr(vision.shutil, "which", lambda _: "/synthetic/codex")
    def runner(command, **kwargs):
        assert "synthetic-private-key" not in json.dumps(command)
        assert kwargs["env"]["VIDEO_VISION_RUNTIME_KEY"] == "synthetic-private-key"
        assert "--sandbox" in command and command[command.index("--sandbox") + 1] == "read-only"
        path = Path(command[command.index("--output-last-message") + 1])
        state.atomic_write_json(path, result_for(payload))
        return subprocess.CompletedProcess(command, 0, b'{"type":"turn.completed","usage":{}}\n', b"")
    value, actual, usage = vision.run_codex(route, payload, task, runner=runner)
    assert actual is None and usage["model_calls"] is None
    assert value["schema_version"] == 1


def test_invalid_api_json_is_preserved_without_replaying_the_request(tmp_path):
    task, manifest, dispatch = api_task(tmp_path)
    raw = b'{"model":"vision-example","choices":[{"message":{"content":"not JSON"}}]}'
    calls = []
    def opener(*a, **k):
        calls.append(1)
        return io.BytesIO(raw)
    with pytest.raises(vision.VisionError):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=opener)
    assert dispatch["status"] == "response_invalid"
    assert (task / dispatch["raw_response"]).read_bytes() == raw
    with pytest.raises(vision.VisionError, match="reconcile"):
        vision.execute(task, manifest, dispatch["dispatch_id"], opener=opener)
    assert calls == [1]


def test_model_catalog_is_not_a_claim_of_image_or_account_permission():
    route = {"method": "api", "base_url": "https://synthetic.invalid/v1", "api_key": "synthetic"}
    catalog = vision.model_catalog(route, opener=lambda *a, **k: io.BytesIO(b'{"data":[{"id":"listed"}]}'))
    assert catalog["models"][0]["vision"] is None
    assert catalog["models"][0]["available"] is None


def test_switch_recovery_invalidates_only_affected_work_and_keeps_budgets(tmp_path, monkeypatch):
    task, manifest, dispatch = api_task(tmp_path)
    manifest["vision"]["issues"]["title"] = {"rounds": 2, "start_ms": 0, "end_ms": 1000}
    manifest["vision"]["model_calls"] = 7
    visual = state.read_json(task / "visual-observations.json")
    visual["revisions"][0]["batch_id"] = "batch-0001"
    visual["revisions"].append({"batch_id": "other-batch", "observations": [{"observation_id": "keep", "revision": 1}]})
    state.atomic_write_json(task / "visual-observations.json", visual)
    caps = {"host": "codex", "source": "test session", "main_vision": True, "models": []}
    real_finish = vision.finish_switch
    monkeypatch.setattr(vision, "finish_switch", lambda *a: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        vision.switch_route(task, manifest, config.resolve({}, tmp_path), host="codex", caps=caps,
                            route={"method": "main-agent"}, credential_ref=None, from_batch="batch-0001", reason="test route switch")
    _, restored, _ = state.load_task(task)
    real_finish(task, restored)
    assert restored["vision"]["route"]["method"] == "main-agent"
    assert restored["vision"]["dispatches"] == []
    assert restored["vision"]["switches"][-1]["previous_dispatches"][0]["dispatch_id"] == dispatch["dispatch_id"]
    assert restored["vision"]["model_calls"] == 7 and restored["vision"]["issues"]["title"]["rounds"] == 2
    assert [o["observation_id"] for o in state.read_json(task / "visual-observations.json")["latest"]] == ["keep"]


def test_resume_rejects_new_route_options_instead_of_ignoring_them(tmp_path, monkeypatch, capsys):
    task, manifest, _ = api_task(tmp_path)
    manifest["settings"]["invocation_cwd"] = str(tmp_path)
    state.save_manifest(task, manifest)
    monkeypatch.setattr(media, "preflight", lambda *a: {})
    monkeypatch.setattr(media, "verify_source", lambda *a: tmp_path / "synthetic.mp4")
    assert cli.main(["resume", "--task-dir", str(task), "--vision-method", "main-agent"]) == 1
    assert "vision-switch" in capsys.readouterr().err


def test_explicit_budget_changes_do_not_reset_existing_usage(tmp_path):
    task, manifest, _ = api_task(tmp_path)
    for _ in range(2):
        vision.begin_issue(task, manifest, "title", start_ms=0, end_ms=1000, reason="blur")
    vision.extend_issue(task, manifest, "title", rounds=1, reason="user explicitly requested one more attempt")
    vision.begin_issue(task, manifest, "title", start_ms=0, end_ms=2000, reason="expanded same issue")
    assert manifest["vision"]["issues"]["title"]["rounds"] == 3
    with pytest.raises(vision.VisionError, match="exhausted"):
        vision.begin_issue(task, manifest, "title", start_ms=0, end_ms=2000, reason="still unreadable")
    manifest["vision"]["model_calls"] = 5
    vision.set_call_limit(task, manifest, limit=8, reason="user set total cap")
    assert manifest["vision"]["model_calls"] == 5
    vision.set_call_limit(task, manifest, limit=None, reason="user explicitly removed total cap")
    assert manifest["vision"]["call_limit"] is None and len(manifest["vision"]["budget_changes"]) == 2


def test_saved_route_is_not_replaced_or_blocked_by_invalid_new_defaults(tmp_path):
    task, manifest, _ = api_task(tmp_path)
    manifest["settings"]["invocation_cwd"] = str(tmp_path)
    (tmp_path / ".env.video-to-markdown").write_text(config.VISION_KEY + "=invalid-new-default")
    assert cli._resolved_from_manifest(manifest, use_home=False).vision is None
    assert vision.connection(manifest)["model"] == "vision-example"
