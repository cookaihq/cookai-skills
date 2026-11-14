"""Vision plans must reach the caller before recognition on every entry path."""

import json

import pytest

from video_to_markdown import cli, config, delivery, state, vision
from test_review import fixture_task
from test_workflow import video  # shared real-media fixture


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(config.VISION_KEY, raising=False)
    caps = tmp_path / "caps.json"
    state.atomic_write_json(caps, {"schema_version": 1, "host": "codex", "source": "synthetic session",
        "main_vision": True, "native_agent": True, "models": [
            {"id": "vision-example", "name": "Example", "vision": True, "available": True}]})
    subtitle = tmp_path / "speech.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic speech.\n", encoding="utf-8")
    return caps, subtitle


def call(capsys, *args):
    assert cli.main(list(map(str, args))) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("method", vision.METHODS)
def test_selected_route_reaches_start_output_without_credentials(tmp_path, video, session, capsys, monkeypatch, method):
    caps, subtitle = session
    route = {"method": method}
    if method != "main-agent":
        route["model"] = "vision-example"
    if method in {"api", "independent-agent"}:
        route.update(base_url="https://vision.example:8443/private-service-path", api_key="synthetic-private-key")
    if method == "independent-agent":
        which = vision.shutil.which
        monkeypatch.setattr(vision.shutil, "which", lambda name: "synthetic-codex" if name == "codex" else which(name))
    route_file = tmp_path / "private-route.json"
    state.atomic_write_json(route_file, route)
    started = call(capsys, "start", "--video", video, "--subtitle", subtitle, "--project-root", tmp_path,
                   "--vision-config", route_file, "--capabilities", caps, "--no-global-config")
    assert started["next_action"] == "inspect_batch"
    notice = started["vision_notice"]
    assert notice["method"] == method and notice["host"] == "codex"
    assert notice["model"] == route.get("model")
    # Catalog visibility and a configured ID do not verify execution identity.
    assert notice["model_verification"] == "unverified"
    assert notice["config_source"] == "task option"
    assert method in notice["message"] and "codex" in notice["message"]
    if method in {"api", "independent-agent"}:
        assert notice["service"] == "https://vision.example:8443"
        assert notice["service"] in notice["message"]
        assert notice["image_capability"] is None and notice["account_availability"] is None
    else:
        assert notice["image_capability"] is True
    if method == "independent-agent":
        assert notice["runtime"] == "codex" and notice["protocol"] == "responses"
    output = json.dumps(started)
    assert all(secret not in output for secret in ("synthetic-private-key", "private-service-path", "private-route.json", "credential_ref"))


def test_unverified_native_capabilities_remain_visible(tmp_path, video, session, capsys):
    caps, subtitle = session
    evidence = state.read_json(caps)
    evidence["models"][0].update(vision=None, available=None)
    state.atomic_write_json(caps, evidence)
    started = call(capsys, "start", "--video", video, "--subtitle", subtitle, "--project-root", tmp_path,
                   "--vision-method", "host-agent", "--vision-model", "vision-example",
                   "--accept-unverified-model", "--capabilities", caps, "--no-global-config")
    notice = started["vision_notice"]
    assert notice["image_capability"] is None and notice["account_availability"] is None
    assert "图片能力" in notice["message"] and "账号可用性" in notice["message"]


def test_unconfigured_task_announces_after_choice_and_retains_route_on_resume(tmp_path, video, session, capsys, monkeypatch):
    caps, subtitle = session
    pending = call(capsys, "start", "--video", video, "--subtitle", subtitle, "--project-root", tmp_path,
                   "--capabilities", caps, "--no-global-config")
    assert pending["next_action"] == "choose_vision_once" and len(pending["choices"]) == 4
    assert "vision_notice" not in pending
    task = pending["task_dir"]
    selected = call(capsys, "resume", "--task-dir", task, "--vision-method", "main-agent", "--capabilities", caps)
    assert selected["vision_notice"]["method"] == "main-agent"
    assert selected["vision_notice"]["model"] is None
    assert selected["vision_notice"]["model_verification"] == "unverified"
    # A broken new default must not be loaded to produce the saved notice.
    monkeypatch.setenv(config.VISION_KEY, "invalid new default")
    resumed = call(capsys, "resume", "--task-dir", task)
    assert resumed["vision_notice"] == selected["vision_notice"]
    assert resumed["next_action"] == "inspect_batch"


@pytest.mark.parametrize("source", ["project", "environment"])
def test_fixed_choice_is_announced_automatically_with_its_source(tmp_path, video, session, capsys, monkeypatch, source):
    caps, subtitle = session
    group = json.dumps({"ask_each_task": False, "routes": {"codex": {"method": "main-agent"}}})
    if source == "environment":
        monkeypatch.setenv(config.VISION_KEY, group)
        expected_source = "process environment"
    else:
        path = tmp_path / ".env.video-to-markdown"
        config.update_file(path, {config.VISION_KEY: group})
        expected_source = str(path)
    started = call(capsys, "start", "--video", video, "--subtitle", subtitle,
                   "--project-root", tmp_path, "--capabilities", caps, "--no-global-config")
    assert started["next_action"] == "inspect_batch"
    assert started["vision_notice"]["method"] == "main-agent"
    assert started["vision_notice"]["config_source"] == expected_source
    assert expected_source in started["vision_notice"]["message"]


def test_start_and_resume_announce_even_while_waiting_for_transcription(tmp_path, video, session, capsys, monkeypatch):
    caps, _ = session
    monkeypatch.setattr(cli.asr, "advance", lambda *a, **k: {"next_action": "wait_for_transcription", "receipt": "synthetic"})
    started = call(capsys, "start", "--video", video, "--project-root", tmp_path,
                   "--vision-method", "main-agent", "--capabilities", caps, "--no-global-config")
    assert started["next_action"] == "wait_for_transcription" and started["receipt"] == "synthetic"
    assert started["vision_notice"]["method"] == "main-agent"
    task = next((tmp_path / "data" / "video-to-markdown").iterdir())
    resumed = call(capsys, "resume", "--task-dir", task)
    assert resumed["next_action"] == "wait_for_transcription"
    assert resumed["vision_notice"] == started["vision_notice"]


def test_switch_announces_new_route_before_any_new_dispatch(tmp_path, video, session, capsys):
    caps, subtitle = session
    started = call(capsys, "start", "--video", video, "--subtitle", subtitle, "--project-root", tmp_path,
                   "--vision-method", "main-agent", "--capabilities", caps, "--no-global-config")
    switched = call(capsys, "vision-switch", "--task-dir", started["task_dir"], "--from-batch", "batch-0001",
                    "--reason", "user requested another model", "--vision-method", "host-agent",
                    "--vision-model", "vision-example", "--capabilities", caps)
    assert switched["status"] == "applied"
    assert switched["vision_notice"]["method"] == "host-agent"
    assert switched["vision_notice"]["model"] == "vision-example"
    assert "vision-example" in switched["vision_notice"]["message"]
    assert switched["vision_notice"]["model_verification"] == "unverified"
    resumed = call(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["vision_notice"] == switched["vision_notice"]


@pytest.mark.parametrize("draft", [False, True])
def test_completed_and_draft_resume_include_saved_plan(tmp_path, capsys, draft):
    task, manifest = fixture_task(tmp_path)
    manifest["settings"]["invocation_cwd"] = str(tmp_path)
    manifest["vision"].update(host="codex", source="task option")
    manifest["delivery"] = delivery.initialize("sample.mp4", "20261114000000-sample.mp4", None)
    if draft:
        visual = state.read_json(task / "visual-observations.json")
        visual["revisions"][0]["observations"][0].update(confidence="uncertain", needs_resample=True)
        state.atomic_write_json(task / "visual-observations.json", visual)
        manifest["vision"]["issues"]["title"] = {"rounds": 2, "start_ms": 0, "end_ms": 1000}
        (task / "document.md").write_text("# Opening\n\nDRAFT\n00:00:00.000 unreadable title\n", encoding="utf-8")
        report = state.read_json(task / "review-report.json")
        report["intervals"][0].update(classification="unresolved", reason="unreadable title", media=[], issue_ids=["title"])
        state.atomic_write_json(task / "review-report.json", report)
    state.save_manifest(task, manifest)
    final = delivery.finalize(task, title=None, keep_name=True, copy_directory=None, draft=draft)
    resumed = call(capsys, "resume", "--task-dir", final["task_dir"])
    assert resumed["next_action"] == ("draft_delivered" if draft else "complete")
    assert resumed["vision_notice"]["method"] == "main-agent"
    assert resumed["vision_notice"]["model"] is None


def test_explicit_skip_never_announces_a_vision_plan(tmp_path, video, session, capsys):
    _, subtitle = session
    # Even an unusable configured visual route is irrelevant to a skip task.
    config.update_file(tmp_path / ".env", {config.VISION_KEY: "invalid disabled configuration"})
    started = call(capsys, "start", "--video", video, "--subtitle", subtitle, "--project-root", tmp_path,
                   "--skip-visuals", "user only needs speech", "--no-global-config")
    assert started["next_action"] == "write_document" and "vision_notice" not in started
    resumed = call(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == "write_document" and "vision_notice" not in resumed
