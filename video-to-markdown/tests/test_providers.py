"""Transcription acceptance through start/resume and the real task files."""
import json
from pathlib import Path

import pytest

from video_to_markdown import cli
from test_workflow import video  # Real ffmpeg fixture, shared with entrypoint tests.


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    import os
    for name in list(os.environ):
        if name.startswith(("VIDEO_TO_MARKDOWN_", "AIHUB_")) or name == "X_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def invoke(capsys, *args, code=0):
    result = cli.main(list(map(str, args)))
    output = capsys.readouterr()
    assert result == code, output.out + output.err
    assert not output.err, output.err
    return json.loads(output.out)


def mock_dependency(monkeypatch, execute):
    from video_to_markdown import external
    def with_version(argv, **kwargs):
        if argv[0] == "lark-cli" and argv[3:] == ["--version"]:
            return external.Result(0, "lark-cli version 1.0.98")
        return execute(argv, **kwargs)
    monkeypatch.setattr(external, "execute", with_version)


def test_default_task_requests_aihub_audio_and_keeps_choice_on_resume(tmp_path, video, capsys):
    started = invoke(capsys, "start", "--video", video)
    assert started["provider"] == "aihub-audio"
    assert started["next_action"] == "load_aihub_audio"
    task = Path(started["task_dir"])
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n", encoding="utf-8")
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["provider"] == "aihub-audio"
    assert resumed["next_action"] == "load_aihub_audio"
    assert not (task / "transcript.json").exists()


def test_missing_aihub_switches_once_and_feishu_login_pause_survives_resume(tmp_path, video, capsys):
    configuration = tmp_path / ".env.local"
    configuration.write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=aihub-audio\n", encoding="utf-8")
    started = invoke(capsys, "start", "--video", video, "--aihub-unavailable")
    assert started["provider"] == "feishu-minutes"
    assert started["next_action"] == "select_feishu_profile"
    assert started["reason"] == "aihub_dependency_unavailable"
    task = Path(started["task_dir"])
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["provider"] == "feishu-minutes"
    assert resumed["next_action"] == "select_feishu_profile"
    assert configuration.read_text(encoding="utf-8") == "VIDEO_TO_MARKDOWN_ASR_PROVIDER=aihub-audio\n"


def test_explicit_provider_overrides_default_without_changing_it(tmp_path, video, capsys):
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n", encoding="utf-8")
    started = invoke(capsys, "start", "--video", video, "--provider", "aihub-audio", "--only-provider", "--aihub-unavailable")
    assert started["provider"] == "aihub-audio"
    assert started["next_action"] == "repair_aihub_dependency"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["provider"] == "aihub-audio"


def test_feishu_upload_wait_resume_exports_all_timed_segments(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    (tmp_path / ".env.local").write_text(
        "VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n"
        "VIDEO_TO_MARKDOWN_FEISHU_PROFILE=work\n"
        "VIDEO_TO_MARKDOWN_FEISHU_APP_ID=cli_testapp\n"
        "VIDEO_TO_MARKDOWN_FEISHU_USER_OPEN_ID=ou_testuser\n", encoding="utf-8")
    calls = []

    def execute(argv, *, cwd, timeout):
        calls.append(argv)
        assert argv[:3] == ["lark-cli", "--profile", "work"]
        assert Path(cwd) == tmp_path
        assert 0 < timeout <= 60
        command = argv[3:5]
        if command == ["profile", "list"]:
            value = [{"name": "work", "appId": "cli_testapp", "brand": "feishu"}]
        elif command == ["auth", "status"]:
            value = {"appId": "cli_testapp", "brand": "feishu", "identities": {"user": {
                "available": True, "status": "ready", "tokenStatus": "valid", "openId": "ou_testuser"}}}
        elif command == ["drive", "+upload"]:
            audio = Path(argv[argv.index("--file") + 1])
            assert audio.suffix == ".mp3" and audio.stat().st_size > 0
            value = {"ok": True, "identity": "user", "data": {"file_token": "file_test"}}
        elif command == ["minutes", "+upload"]:
            value = {"ok": True, "identity": "user", "data": {"minute_token": "minute_test", "minute_url": "https://test.feishu.cn/minutes/minute_test"}}
        elif command == ["minutes", "+detail"]:
            processing = sum(c[3:5] == command for c in calls) == 1
            item = {"minute_token": "minute_test", **({"status": "processing", "retryable": True} if processing else {"title": "test", "note_id": "note_test"})}
            value = {"ok": not processing, "identity": "user", "data": {"minutes": [item]}}
            return external.Result(1 if processing else 0, json.dumps(value), "")
        elif command == ["api", "GET"]:
            assert json.loads(argv[argv.index("--params") + 1]) == {"need_timestamp": True, "file_format": "srt"}
            target = Path(argv[argv.index("--output") + 1])
            target.write_text("1\n00:00:00,000 --> 00:00:00,500\n第一句\n\n2\n00:00:00,600 --> 00:00:01,000\nSecond speaker\n", encoding="utf-8")
            value = {"saved_path": str(target), "size_bytes": target.stat().st_size, "content_type": "text/plain"}
        else:
            raise AssertionError(argv)
        return external.Result(0, json.dumps(value), "")

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "wait_feishu"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == "inspect_batch"
    doc = json.loads((Path(started["task_dir"]) / "transcript.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in doc["segments"]] == ["第一句", "Second speaker"]
    assert doc["source"]["timing_granularity"] == "segment"
    assert sum(c[3:5] == ["drive", "+upload"] for c in calls) == 1
    assert sum(c[3:5] == ["minutes", "+upload"] for c in calls) == 1


def test_aihub_uses_dependency_run_and_complete_files_then_waits_for_review(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    plugin = tmp_path / "installed-aihub"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "skills/aihub-audio").mkdir(parents=True)
    (plugin / "package.json").write_text('{"name":"aihub-studio","version":"1.3.0"}', encoding="utf-8")
    (plugin / "skills/aihub-audio/SKILL.md").write_text('---\nname: aihub-audio\n---\n', encoding="utf-8")
    for entry in ("aihub.ps1", "aihub.mjs"):
        (plugin / "scripts" / entry).touch()
    calls = []
    record = None

    def execute(argv, *, cwd, timeout):
        nonlocal record
        assert argv[argv.index("--skill") + 1] == "aihub-audio"
        assert "--no-global-config" in argv
        assert Path(cwd) == tmp_path
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        value = {"schema_version": 1, "status": "ok"}
        if command == "upload":
            value.update(id="file-aihub", filename="audio.mp3", size=123, url="https://upload.test/audio?sig=PRIVATE")
        elif command == "plan":
            request = json.loads(Path(argv[argv.index("--request-file") + 1]).read_text(encoding="utf-8"))
            assert request["operation"] == "audio-transcribe" and "model" not in request
            assert request["inputs"]["audios"] == ["https://upload.test/audio?sig=PRIVATE"]
        elif command == "run":
            record = Path(argv[argv.index("--output-dir") + 1]) / "aihub-run-test/run.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"schema_version":1,"kind":"aihub-run","skill":"aihub-audio"}', encoding="utf-8")
            value = {"status": "waiting", "run_record": str(record), "task_id": "task-test"}
        elif command == "continue":
            assert argv[argv.index("--record") + 1] == str(record)
            files = []
            for index, raw in enumerate((
                {"transcripts": [{"sentences": [{"begin_time": 0, "end_time": 500, "text": "第一句"}]}]},
                [{"segments": [{"start": 0.6, "end": 1.0, "text": "Second speaker"}]}],
            )):
                path = record.parent / (str(index) + ".json")
                path.write_text(json.dumps(raw), encoding="utf-8")
                files.append({"path": str(path), "bytes": path.stat().st_size, "url": "https://result.test?sig=PRIVATE", "mime_type": "application/json"})
            checked = calls.count("continue") > 1
            value = {"status": "delivered", "run_record": str(record), "task_id": "task-test", "model": "paraformer-v2", "files": files,
                     "text": "first-only-must-not-use", "result_check": {"status": "matched" if checked else "pending", "user_notice": "Result check can be disabled."}}
        else:
            assert command == "doctor", argv
        return external.Result(0, json.dumps(value), "")

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", plugin, "--no-global-config", "--request", "请完整转写视频并整理为带截图的文档")
    assert started["next_action"] == "continue_aihub_audio"
    task = Path(started["task_dir"])
    waiting = invoke(capsys, "resume", "--task-dir", task)
    assert waiting["next_action"] == "review_aihub_audio"
    assert not (task / "transcript.json").exists()
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["next_action"] == "inspect_batch"
    doc = json.loads((task / "transcript.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in doc["segments"]] == ["第一句", "Second speaker"]
    assert calls.count("upload") == calls.count("run") == 1
    for filename in ("manifest.json", "transcript.json", "document.md"):
        assert "PRIVATE" not in (task / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize("choice,expected,changed", [
    ("aihub-once", "aihub-audio", False),
    ("aihub-default", "aihub-audio", True),
    ("pause", "feishu-minutes", False),
])
def test_feishu_quota_choice_changes_only_authorized_scope(tmp_path, video, capsys, monkeypatch, choice, expected, changed):
    from video_to_markdown import external
    configuration = tmp_path / ".env.local"
    original = ("# keep me\nVIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n"
                "VIDEO_TO_MARKDOWN_FEISHU_PROFILE=work\nVIDEO_TO_MARKDOWN_FEISHU_APP_ID=app\n"
                "VIDEO_TO_MARKDOWN_FEISHU_USER_OPEN_ID=user\nUNRELATED=unchanged\n")
    configuration.write_text(original, encoding="utf-8")
    calls = []

    def execute(argv, **kwargs):
        command = argv[3:5]
        calls.append(command)
        if command == ["profile", "list"]:
            value = [{"name": "work", "appId": "app", "brand": "feishu"}]
        elif command == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}
        else:
            assert command == ["drive", "+upload"]
            return external.Result(1, "", json.dumps({"ok": False, "error": {"type": "api", "subtype": "quota_exceeded", "code": 2091008}}))
        return external.Result(0, json.dumps(value), "")

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "feishu_quota_choice"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--choice", choice)
    assert resumed["provider"] == expected
    assert resumed["next_action"] == ("paused" if choice == "pause" else "load_aihub_audio")
    assert configuration.read_text(encoding="utf-8") == (original.replace("ASR_PROVIDER=feishu-minutes", "ASR_PROVIDER=aihub-audio") if changed else original)
    if changed:
        assert resumed["default_save"]["effective"] is True
    assert calls.count(["drive", "+upload"]) == 1
    if choice == "aihub-once":
        stopped = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--aihub-unavailable")
        assert stopped["next_action"] == "both_providers_failed"
        assert stopped["provider"] == "aihub-audio"


def test_feishu_login_and_identity_confirmation_survive_turns_without_writes(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    available = False
    user_id = "user-original"
    calls = []

    def execute(argv, **kwargs):
        assert argv[:3] == ["lark-cli", "--profile", "chosen"]
        calls.append(argv[3:5])
        if argv[3:5] == ["profile", "list"]:
            value = [{"name": "chosen", "appId": "app", "brand": "feishu"}]
        elif argv[3:5] == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {
                "available": available, "tokenStatus": "valid" if available else "expired", "openId": user_id, "userName": "Synthetic User"}}}
        else:
            raise AssertionError("no remote write before a matching confirmed identity")
        return external.Result(0, json.dumps(value), "")

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--provider", "feishu-minutes", "--feishu-profile", "chosen")
    assert started["next_action"] == "login_feishu"
    task = started["task_dir"]
    paused = invoke(capsys, "resume", "--task-dir", task, "--choice", "pause")
    assert paused["next_action"] == "paused"
    available = True
    preview = invoke(capsys, "resume", "--task-dir", task, "--choice", "continue")
    assert preview["next_action"] == "confirm_feishu_identity"
    assert preview["identity"]["user_open_id"] == "user-original"
    user_id = "user-changed"
    drift = invoke(capsys, "resume", "--task-dir", task, "--confirm-feishu-identity", preview["identity_token"])
    assert drift["next_action"] == "confirm_feishu_identity"
    assert drift["identity_token"] != preview["identity_token"]
    assert not (tmp_path / ".env.local").exists()


def test_concurrent_resume_cannot_enter_a_pending_upload(tmp_path, video, capsys, monkeypatch):
    import subprocess
    import sys
    from video_to_markdown import external
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)
    writes = []

    def execute(argv, **kwargs):
        if argv[3:5] == ["profile", "list"]:
            value = [{"name": "work", "appId": "app", "brand": "feishu"}]
        elif argv[3:5] == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}
        else:
            assert argv[3:5] == ["drive", "+upload"]
            writes.append(argv)
            task = Path(argv[argv.index("--file") + 1]).parent.parent
            entry = Path(cli.__file__).parents[1] / "workflow.py"
            child = subprocess.run([sys.executable, str(entry), "resume", "--task-dir", str(task)], capture_output=True, text=True, timeout=20)
            assert child.returncode == 1 and "already running" in child.stderr, child.stdout + child.stderr
            return external.Result(-1, timed_out=True)
        return external.Result(0, json.dumps(value), "")

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "submission_unknown"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == "submission_unknown"
    assert len(writes) == 1


@pytest.mark.parametrize("selected", ["old", "new"])
def test_identity_drift_reports_difference_and_can_rebind_before_submission(tmp_path, video, capsys, monkeypatch, selected):
    from video_to_markdown import external
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "old", "FEISHU_APP_ID": "expected-app", "FEISHU_USER_OPEN_ID": "expected-user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)

    def execute(argv, **kwargs):
        profile = argv[2]
        if argv[3:5] == ["profile", "list"]:
            value = [{"name": profile, "appId": "actual-app", "brand": "feishu"}]
        else:
            assert argv[3:5] == ["auth", "status"]
            value = {"appId": "actual-app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "actual-user"}}}
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "repair_feishu_identity"
    assert started["expected_identity"]["app_id"] == "expected-app"
    assert started["actual_identity"]["app_id"] == "actual-app"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--feishu-profile", selected)
    assert resumed["next_action"] == "confirm_feishu_identity"
    assert resumed["identity"]["profile"] == selected


def test_resume_global_switches_are_kept_for_following_turns(tmp_path, video, capsys, monkeypatch):
    import os
    if os.name != "nt":
        pytest.skip("native Windows home-directory resolution")
    personal = tmp_path / "personal"
    configuration = personal / ".config/video-to-markdown/.env"
    configuration.parent.mkdir(parents=True)
    configuration.write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(personal))
    started = invoke(capsys, "start", "--video", video)
    task = Path(started["task_dir"])
    invoke(capsys, "resume", "--task-dir", task, "--use-local-key")
    assert json.loads((task / "manifest.json").read_text(encoding="utf-8"))["settings"]["use_home"] is True
    invoke(capsys, "resume", "--task-dir", task, "--no-global-config")
    configuration.write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=invalid-do-not-read\n", encoding="utf-8")
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["provider"] == "aihub-audio"


def test_export_failure_preserves_minute_and_only_retries_query_and_download(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)
    calls = []

    def execute(argv, **kwargs):
        command = argv[3:5]
        calls.append(command)
        if command == ["profile", "list"]:
            value = [{"name": "work", "appId": "app", "brand": "feishu"}]
        elif command == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}
        elif command == ["drive", "+upload"]:
            value = {"ok": True, "data": {"file_token": "file"}}
        elif command == ["minutes", "+upload"]:
            value = {"ok": True, "data": {"minute_token": "minute"}}
        elif command == ["minutes", "+detail"]:
            value = {"ok": True, "data": {"minutes": [{"minute_token": "minute", "title": "title", "note_id": "note"}]}}
        else:
            assert command == ["api", "GET"]
            if calls.count(command) == 1:
                return external.Result(3, "", '{"error":{"type":"authorization"}}')
            target = Path(argv[argv.index("--output") + 1])
            target.write_text("1\n00:00:00,000 --> 00:00:01,000\nAll text\n", encoding="utf-8")
            value = {"saved_path": str(target), "size_bytes": target.stat().st_size}
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "retry_feishu_export"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == "inspect_batch"
    assert calls.count(["drive", "+upload"]) == calls.count(["minutes", "+upload"]) == 1


def test_nonzero_audio_start_is_preserved_in_exported_video_timestamps(tmp_path, video, capsys, monkeypatch):
    import os
    import subprocess
    from video_to_markdown import external
    delayed = tmp_path / "delayed.mkv"
    subprocess.run([os.environ["FFMPEG_BIN"], "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=160x90:d=3",
                    "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "pcm_s16le", str(delayed)], check=True, timeout=30)
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)

    def execute(argv, **kwargs):
        command = argv[3:5]
        if command == ["profile", "list"]:
            value = [{"name": "work", "appId": "app", "brand": "feishu"}]
        elif command == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}
        elif command == ["drive", "+upload"]:
            value = {"ok": True, "data": {"file_token": "file"}}
        elif command == ["minutes", "+upload"]:
            value = {"ok": True, "data": {"minute_token": "minute"}}
        elif command == ["minutes", "+detail"]:
            value = {"ok": True, "data": {"minutes": [{"minute_token": "minute", "title": "title", "note_id": "note"}]}}
        else:
            target = Path(argv[argv.index("--output") + 1])
            target.write_text("1\n00:00:00,100 --> 00:00:00,900\nSpeech after silence\n", encoding="utf-8")
            value = {"saved_path": str(target), "size_bytes": target.stat().st_size}
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", delayed)
    task = Path(started["task_dir"])
    doc = json.loads((task / "transcript.json").read_text(encoding="utf-8"))
    assert (doc["segments"][0]["start_ms"], doc["segments"][0]["end_ms"]) == (1100, 1900)
    manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
    audio = manifest["audio_for_asr"]
    assert audio["track_index"] == 1 and audio["time_offset_ms"] == 1000
    assert 1000 <= audio["duration_ms"] <= 1100


def test_process_interrupted_during_upload_never_repeats_or_switches(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    started = invoke(capsys, "start", "--video", video, "--provider", "feishu-minutes")
    task = Path(started["task_dir"])
    assert "aihub-once" in started["choices"]
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)
    writes = []

    def execute(argv, **kwargs):
        if argv[3:5] == ["profile", "list"]:
            return external.Result(0, json.dumps([{"name": "work", "appId": "app", "brand": "feishu"}]))
        if argv[3:5] == ["auth", "status"]:
            return external.Result(0, json.dumps({"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}))
        writes.append(argv)
        raise KeyboardInterrupt()

    mock_dependency(monkeypatch, execute)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["resume", "--task-dir", str(task)])
    # A local failure on the next turn must not hide the unresolved remote write.
    def missing_cli(argv, **kwargs):
        return external.Result(-1, missing=True)
    monkeypatch.setattr(external, "execute", missing_cli)
    resumed = invoke(capsys, "resume", "--task-dir", task, "--choice", "aihub-once")
    assert resumed["next_action"] == "submission_unknown"
    paused = invoke(capsys, "resume", "--task-dir", task, "--choice", "pause")
    assert paused["next_action"] == "submission_unknown"
    assert paused["paused"] is True
    still_paused = invoke(capsys, "resume", "--task-dir", task)
    assert still_paused["next_action"] == "submission_unknown"
    assert still_paused["paused"] is True
    assert len(writes) == 1


def test_auth_repair_explicit_retry_reuses_upload_without_creating_twice(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    for key, value in {"ASR_PROVIDER": "feishu-minutes", "FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)
    writes = []

    def execute(argv, **kwargs):
        command = argv[3:5]
        if command == ["profile", "list"]:
            value = [{"name": "work", "appId": "app", "brand": "feishu"}]
        elif command == ["auth", "status"]:
            value = {"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}
        elif command == ["drive", "+upload"]:
            writes.append(command)
            value = {"ok": True, "data": {"file_token": "saved-file"}}
        elif command == ["minutes", "+upload"]:
            writes.append(command)
            assert argv[argv.index("--file-token") + 1] == "saved-file"
            if writes.count(command) == 1:
                return external.Result(3, "", json.dumps({"ok": False, "error": {"type": "authorization", "subtype": "permission_denied"}}))
            value = {"ok": True, "data": {"minute_token": "saved-minute"}}
        else:
            assert command == ["minutes", "+detail"]
            value = {"ok": False, "data": {"minutes": [{"minute_token": "saved-minute", "status": "processing"}]}}
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video)
    assert started["next_action"] == "repair_feishu_authorization"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--retry-provider")
    assert resumed["next_action"] == "wait_feishu"
    assert writes.count(["drive", "+upload"]) == 1
    assert writes.count(["minutes", "+upload"]) == 2


@pytest.fixture
def aihub_plugin(tmp_path):
    root = tmp_path / "plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "skills/aihub-audio").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"aihub-studio","version":"1.3.0"}')
    (root / "skills/aihub-audio/SKILL.md").write_text('---\nname: aihub-audio\n---\n')
    for name in ("aihub.ps1", "aihub.mjs"):
        (root / "scripts" / name).touch()
    return root


@pytest.mark.parametrize("outcome,action", [("waiting", "continue_aihub_audio"), ("query_failed", "continue_aihub_audio"),
    ("download_failed", "continue_aihub_audio"), ("persistence_failed", "recover_aihub_known_task"),
    ("confirmation_required", "confirm_aihub_audio"), ("submission_unknown", "submission_unknown"),
    ("not_submitted", "select_feishu_profile")])
def test_aihub_classifies_task_outcomes_without_new_submission(tmp_path, video, capsys, monkeypatch, aihub_plugin, outcome, action):
    from video_to_markdown import external
    commands = []
    record = None

    def execute(argv, **kwargs):
        nonlocal record
        command = argv[argv.index("--skill") - 1]
        commands.append(command)
        value = {"status": "ok"}
        if command == "upload":
            value.update(id="file", url="https://test.invalid/audio")
        elif command in {"run", "continue"}:
            if command == "run":
                record = Path(argv[argv.index("--output-dir") + 1]) / "aihub-run-state/run.json"
                record.parent.mkdir(parents=True)
                record.write_text('{"schema_version":1,"kind":"aihub-run","skill":"aihub-audio"}')
            value = {"status": outcome, "run_record": str(record)}
            if outcome == "persistence_failed":
                value["task_id"] = "known-task"
        return external.Result(0, json.dumps(value))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", aihub_plugin, "--request", "完整转写")
    assert started["next_action"] == action
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"])
    assert resumed["next_action"] == action
    assert commands.count("run") == commands.count("upload") == 1


def test_user_can_switch_after_cancelling_feishu_login(tmp_path, video, capsys):
    started = invoke(capsys, "start", "--video", video, "--provider", "feishu-minutes")
    task = started["task_dir"]
    invoke(capsys, "resume", "--task-dir", task, "--choice", "pause")
    switched = invoke(capsys, "resume", "--task-dir", task, "--choice", "aihub-once")
    assert switched["provider"] == "aihub-audio"
    assert switched["next_action"] == "load_aihub_audio"
    stopped = invoke(capsys, "resume", "--task-dir", task, "--aihub-unavailable")
    assert stopped["provider"] == "aihub-audio"
    assert stopped["next_action"] == "repair_aihub_dependency"


@pytest.mark.parametrize("blocked_source,reason", [
    ("environment", "process_environment_override"),
    ("symlink", "save_or_readback_failed"),
    ("global-disabled", "global_reading_not_enabled"),
])
def test_default_save_failure_does_not_undo_task_only_switch(tmp_path, video, capsys, monkeypatch, blocked_source, reason):
    from video_to_markdown import external
    original = "# preserve\nVIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n"
    source = tmp_path / "existing.env"
    source.write_text(original, encoding="utf-8")
    if blocked_source == "environment":
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_ASR_PROVIDER", "feishu-minutes")
    elif blocked_source == "symlink":
        (tmp_path / ".env.local").symlink_to(source)
    for key, value in {"FEISHU_PROFILE": "work", "FEISHU_APP_ID": "app", "FEISHU_USER_OPEN_ID": "user"}.items():
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_" + key, value)

    def execute(argv, **kwargs):
        if argv[3:5] == ["profile", "list"]:
            return external.Result(0, json.dumps([{"name": "work", "appId": "app", "brand": "feishu"}]))
        if argv[3:5] == ["auth", "status"]:
            return external.Result(0, json.dumps({"appId": "app", "brand": "feishu", "identities": {"user": {"available": True, "tokenStatus": "valid", "openId": "user"}}}))
        assert argv[3:5] == ["drive", "+upload"]
        return external.Result(1, "", json.dumps({"ok": False, "error": {"type": "api", "subtype": "quota_exceeded", "code": 2091008}}))

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--provider", "feishu-minutes")
    assert started["next_action"] == "feishu_quota_choice"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--choice", "aihub-default")
    assert resumed["provider"] == "aihub-audio"
    assert resumed["next_action"] == "load_aihub_audio"
    assert resumed["default_save"]["effective"] is False
    assert resumed["default_save"]["reason"] == reason
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env").exists()


def test_legacy_task_is_preserved_and_never_resubmitted(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    started = invoke(capsys, "start", "--video", video)
    task = Path(started["task_dir"])
    manifest_path = task / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["skill_version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = {str(path.relative_to(task)): path.read_bytes() for path in task.rglob("*") if path.is_file()}
    def never_execute(*args, **kwargs):
        raise AssertionError("legacy task must not contact a dependency")
    monkeypatch.setattr(external, "execute", never_execute)
    assert cli.main(["resume", "--task-dir", str(task)]) == 1
    output = capsys.readouterr()
    assert "old tasks are preserved but cannot resume in v2" in output.err
    assert {str(path.relative_to(task)): path.read_bytes() for path in task.rglob("*") if path.is_file()} == before


def test_lark_version_is_checked_before_any_upload(tmp_path, video, capsys, monkeypatch):
    from video_to_markdown import external
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        return external.Result(0, "lark-cli version 1.0.1")
    monkeypatch.setattr(external, "execute", execute)
    started = invoke(capsys, "start", "--video", video, "--provider", "feishu-minutes", "--feishu-profile", "work")
    assert started["next_action"] == "update_lark_cli"
    assert calls == [["lark-cli", "--profile", "work", "--version"]]


def test_aihub_persistence_receipt_is_replayed_before_continue(tmp_path, video, capsys, monkeypatch, aihub_plugin):
    from video_to_markdown import external
    calls = []
    private_path = None
    saved_private = None
    def execute(argv, **kwargs):
        nonlocal private_path, saved_private
        command = argv[argv.index("--skill") - 1]
        calls.append(command)
        value = {"status": "ok"}
        if command == "upload":
            value.update(id="file", url="https://test.invalid/audio")
        elif command == "run":
            output = Path(argv[argv.index("--output-dir") + 1])
            record = output / "aihub-run-known/run.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"schema_version":1,"kind":"aihub-run","skill":"aihub-audio"}')
            value = {"status": "persistence_failed", "run_record": str(record), "task_id": "original-id"}
            # Actual filesystem fault: receipt can be saved, but projection cannot.
            private_path = output.parent / "private.json"
            saved_private = output.parent / "private.before"
            private_path.rename(saved_private)
            private_path.mkdir()
        elif command == "continue":
            raise AssertionError("must consume the known-ID receipt before a stale continue")
        return external.Result(0, json.dumps(value))
    mock_dependency(monkeypatch, execute)
    assert cli.main(["start", "--video", str(video), "--aihub-plugin-dir", str(aihub_plugin), "--request", "完整转写"]) == 1
    capsys.readouterr()
    private_path.rmdir()
    saved_private.rename(private_path)
    task = private_path.parent.parent
    resumed = invoke(capsys, "resume", "--task-dir", task)
    assert resumed["next_action"] == "recover_aihub_known_task"
    assert resumed["task_id"] == "original-id"
    assert calls.count("run") == 1 and "continue" not in calls


def test_unknown_aihub_upload_cannot_be_changed_to_dependency_missing(tmp_path, video, capsys, monkeypatch, aihub_plugin):
    from video_to_markdown import external
    writes = []

    def execute(argv, **kwargs):
        command = argv[argv.index("--skill") - 1]
        if command == "upload":
            writes.append(command)
            return external.Result(-1, timed_out=True)
        assert command == "doctor"
        return external.Result(0, '{"status":"ok"}')

    mock_dependency(monkeypatch, execute)
    started = invoke(capsys, "start", "--video", video, "--aihub-plugin-dir", aihub_plugin, "--request", "完整转写")
    assert started["next_action"] == "submission_unknown"
    resumed = invoke(capsys, "resume", "--task-dir", started["task_dir"], "--aihub-unavailable")
    assert resumed["provider"] == "aihub-audio"
    assert len(writes) == 1
