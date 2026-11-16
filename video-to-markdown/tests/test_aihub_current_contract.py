"""Public Video recovery against the AIhub Studio 6.5 / Audio 6.4 contract."""
import json
from pathlib import Path

import pytest

from video_to_markdown import external, state, transcript
from test_providers import invoke, isolated_configuration, mock_dependency
from test_workflow import video


@pytest.fixture
def current_plugin(tmp_path):
    root = tmp_path / "current-plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "skills/aihub-audio").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"aihub-studio","version":"6.5.1"}')
    (root / "skills/aihub-audio/SKILL.md").write_text('---\nname: aihub-audio\n---\n')
    for name in ("aihub.ps1", "aihub.mjs"):
        (root / "scripts" / name).touch()
    return root


def saved_run(argv):
    output = Path(argv[argv.index("--output-dir") + 1])
    record = output / "20261117123456-audio-run-a1b2c3d4/run.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"schema_version":1,"kind":"aihub-run","skill":"aihub-audio"}')
    return record


def test_current_local_input_merged_artifacts_and_schema4_resume(tmp_path, video, capsys, monkeypatch, current_plugin):
    calls = []
    record = None

    def execute(argv, **kwargs):
        nonlocal record
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        value = {"status": "ok"}
        if command == "plan":
            request = state.read_json(Path(argv[argv.index("--request-file") + 1]))
            audio = Path(request["inputs"]["audios"][0])
            assert audio.is_absolute() and audio.is_file()
            assert request["operation"] == "audio-transcribe" and "model" not in request
        elif command == "run":
            record = saved_run(argv)
            value = {"status": "delivered", "run_record": str(record), "result_check": {"status": "pending"}}
        elif command == "continue":
            assert Path(argv[argv.index("--record") + 1]) == record
            output = record.parent / "files"
            output.mkdir(exist_ok=True)
            raw = {"schema_version": 1, "time_unit": "seconds", "speaker_alignment": "fragment-local",
                   "text": "First.\nSecond.", "segments": [
                       {"text": "First.", "start": .2, "end": .5, "speaker": "source-1/part-1/channel-0/speaker-0"},
                       {"text": "Second.", "start": .6, "end": 1., "words": [{"text": "Second.", "start": .6, "end": 1.}]}],
                   "duplicates": [], "warnings": ["Speaker IDs are fragment-local."]}
            structured = output / "transcript.json"
            structured.write_text(json.dumps(raw), encoding="utf-8")
            text = output / "transcript.txt"
            text.write_text(raw["text"], encoding="utf-8")
            value = {"status": "delivered", "run_record": str(record), "model": "paraformer-v2",
                     "files": [{"path": str(p), "bytes": p.stat().st_size} for p in (structured, text)],
                     "result_check": {"status": "disabled"}}
        elif command != "doctor":
            raise AssertionError(f"unexpected command: {command}")
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--skip-visuals", "synthetic adapter fixture",
                     "--aihub-plugin-dir", current_plugin, "--request", "完整转写")
    assert started["next_action"] == "review_aihub_audio"
    task = Path(started["task_dir"])
    # Represents an existing 4.2.2 schema-4 consumer, not a product migration.
    manifest = state.read_json(task / "manifest.json")
    manifest["skill_version"] = "4.2.2"
    state.save_manifest(task, manifest)
    task_id = manifest["task_id"]
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["stage"] == "writing_document"
    normalized = state.read_json(task / "transcript.json")
    assert [s["text"] for s in normalized["segments"]] == ["First.", "Second."]
    assert normalized["segments"][0]["speaker"].endswith("speaker-0")
    assert normalized["segments"][1]["words"][0]["start_ms"] == 600
    assert len(normalized["source"]["raw_artifacts"]) == 2
    assert state.read_json(task / "manifest.json")["task_id"] == task_id
    assert state.read_json(task / "manifest.json")["skill_version"] == "4.2.2"
    assert calls == ["doctor", "plan", "run", "continue"]


def test_current_lost_run_response_recovers_timestamped_record(tmp_path, video, capsys, monkeypatch, current_plugin):
    calls = []
    record = None

    def execute(argv, **kwargs):
        nonlocal record
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        if command == "run":
            record = saved_run(argv)
            return external.Result(-1, timed_out=True)
        if command == "continue":
            assert Path(argv[argv.index("--record") + 1]) == record
            return external.Result(0, json.dumps({"status": "waiting", "run_record": str(record)}))
        assert command in {"doctor", "plan"}
        return external.Result(0, '{"status":"ok"}')

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", current_plugin, "--request", "完整转写")
    assert started["next_action"] == "submission_unknown"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == "continue_aihub_audio"
    assert calls == ["doctor", "plan", "run", "continue"]


@pytest.mark.parametrize("status,action", [
    ("preprocessing_failed", "repair_aihub_dependency"), ("preflight_failed", "repair_aihub_dependency"),
    ("transcription_failed", "repair_aihub_dependency"), ("failed", "repair_aihub_dependency"),
    ("upload_unknown", "submission_unknown"), ("merge_failed", "repair_transcript_timing"),
    ("persistence_failed", "continue_aihub_audio"),
    ("recovery_failed", "continue_aihub_audio"), ("confirmation_required", "confirm_aihub_audio"),
])
def test_current_saved_plan_never_switches_provider_or_replays_run(tmp_path, video, capsys, monkeypatch, current_plugin, status, action):
    calls = []
    record = None

    def execute(argv, **kwargs):
        nonlocal record
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        assert "--confirm" not in argv  # A dependency token is not user consent.
        if command in {"run", "continue"}:
            if command == "run":
                record = saved_run(argv)
            return external.Result(2, json.dumps({"status": status, "run_record": str(record), "task_id": "existing-child",
                "confirmation": {"token": "not-consent", "model": "paraformer-v2", "params": {}},
                "result": {"fragments": [{"task_id": "existing-child", "status": "delivered"}]}}))
        assert command in {"doctor", "plan"}
        return external.Result(0, '{"status":"ok"}')

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", current_plugin, "--request", "完整转写")
    assert started["provider"] == "aihub-audio" and started["next_action"] == action
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["provider"] == "aihub-audio" and resumed["next_action"] == action
    assert calls == ["doctor", "plan", "run", "continue"]


def test_current_dependency_does_not_repeat_a_legacy_unknown_upload(tmp_path, video, capsys, monkeypatch, current_plugin):
    package = current_plugin / "package.json"
    package.write_text('{"name":"aihub-studio","version":"1.3.0"}')
    calls = []

    def execute(argv, **kwargs):
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        if command == "upload":
            return external.Result(-1, timed_out=True)
        assert command == "doctor"
        return external.Result(0, '{"status":"ok"}')

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", current_plugin, "--request", "完整转写")
    assert started["next_action"] == "submission_unknown"
    package.write_text('{"name":"aihub-studio","version":"6.5.1"}')
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["provider"] == "aihub-audio" and resumed["next_action"] == "submission_unknown"
    assert calls.count("upload") == 1 and "run" not in calls


@pytest.mark.parametrize("segment", [
    {"text": "Full text, incomplete sentence list", "text_alignment": "unverified", "start": 0, "end": 1},
    {"text": "No actual timestamps"},
])
def test_merged_timing_is_never_invented(segment):
    raw = {"schema_version": 1, "time_unit": "seconds", "text": segment["text"], "segments": [segment]}
    with pytest.raises(transcript.TranscriptError):
        value = transcript.normalize_aihub_files([raw], model="paraformer-v2", language=None)
        transcript.apply_timeline(value, {"audio_for_asr": {"time_offset_ms": 0}, "source": {"duration_ms": 2000}})
