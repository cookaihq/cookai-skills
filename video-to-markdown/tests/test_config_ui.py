import json
import subprocess
import sys
import uuid
import time
import http.cookiejar
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from skill_ui.contracts import UIError, public_config
from video_to_markdown import codex_launch, config
from video_to_markdown.ui_adapter import VideoAdapter


def adapter(tmp_path, **kwargs):
    return VideoAdapter(tmp_path, home_config_dir=tmp_path / "home", environ={}, **kwargs)


def edits(**values):
    return {k: {"op": "set", "value": v} for k, v in values.items()}


def test_independent_capability_uses_the_pages_project_and_global_scope(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    user_config = project / "home"
    user_config.mkdir()
    (project / ".env.video-to-markdown").write_text("VIDEO_TO_MARKDOWN_STREAM_MAX_RETRIES=0\n")
    (user_config / ".env").write_text("VIDEO_TO_MARKDOWN_REQUEST_MAX_RETRIES=2\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIDEO_TO_MARKDOWN_REQUEST_MAX_RETRIES", "9")

    def resolve(cwd, **kwargs):
        assert cwd == project
        assert kwargs["environ"] == {}
        return codex_launch.Launch(("synthetic-codex",), "test")

    monkeypatch.setattr(codex_launch, "resolve", resolve)
    monkeypatch.setattr(codex_launch, "probe", lambda launch: None)

    def independent(use_home):
        snapshot = adapter(project, use_home=use_home).config()
        caps = next(f for f in snapshot["fields"] if f["id"] == "capabilities")["effective"]["value"]
        return next(choice for choice in caps["choices"] if choice["method"] == "independent-agent")

    project_only = independent(False)
    assert project_only["recovery"]["stream_retries"] == 0
    assert project_only["recovery"]["request_retries"] == 5
    with_global = independent(True)
    assert with_global["recovery"]["stream_retries"] == 0
    assert with_global["recovery"]["request_retries"] == 2
    assert with_global["recovery_sources"]["request_retries"] == str(user_config / ".env")


def test_preview_and_save_reuse_capability_evidence_until_runtime_inputs_change(tmp_path, monkeypatch):
    probes = []
    monkeypatch.setattr(codex_launch, "resolve", lambda *args, **kwargs: codex_launch.Launch(("synthetic-codex",), "test"))
    monkeypatch.setattr(codex_launch, "probe", lambda launch: probes.append(launch))
    ui = adapter(tmp_path)
    revision = ui.config()["revision"]
    ui.preview(edits(method="main-agent", ask=False), revision, "project")
    ui.save(edits(method="main-agent", ask=False), revision, "project")
    assert len(probes) == 1
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_REQUEST_MAX_RETRIES=3\n")
    current = ui.config()
    choice = next(f for f in current["fields"] if f["id"] == "capabilities")["effective"]["value"]["choices"][2]
    assert choice["recovery"]["request_retries"] == 3
    assert len(probes) == 2
    real_clock = time.monotonic
    monkeypatch.setattr("video_to_markdown.ui_adapter.time.monotonic", lambda: real_clock() + 31)
    ui.config()
    assert len(probes) == 3


def test_preview_uses_real_loader_without_writes_and_save_reads_back(tmp_path):
    ui = adapter(tmp_path)
    before = ui.config()
    changes = edits(method="api", ask=False, model="remote-vision", base_url="https://synthetic.invalid/v1", api_key="synthetic-only-key", protocol="chat-completions")
    preview = ui.preview(changes, before["revision"], "project")
    assert not (tmp_path / ".env.video-to-markdown").exists()
    assert not (tmp_path / ".gitignore").exists()
    assert next(f for f in preview["fields"] if f["id"] == "method")["effective"]["value"] == "api"
    assert "synthetic-only-key" not in json.dumps(public_config(preview))
    saved = ui.save(changes, before["revision"], "project")
    loaded = config.resolve({}, tmp_path)
    assert loaded.vision["routes"]["codex"]["api_key"] == "synthetic-only-key"
    assert next(f for f in saved["fields"] if f["id"] == "method")["effective"]["source"] == "project"


def test_unrelated_concurrent_field_is_preserved(tmp_path):
    ui = adapter(tmp_path)
    revision = ui.config()["revision"]
    (tmp_path / ".env").write_text("FFMPEG_BIN=some-other-ffmpeg\n")
    ui.save(edits(method="main-agent", ask=False), revision, "project")
    loaded = config.resolve({}, tmp_path)
    assert loaded.ffmpeg_bin == 'some-other-ffmpeg'
    assert loaded.vision['routes']['codex']['method'] == 'main-agent'


def test_project_connection_never_borrows_global_key_and_disabled_global_not_read(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(config.VISION_KEY + "=" + json.dumps({"ask_each_task": False, "routes": {"any": {"method": "api", "model": "global", "base_url": "https://synthetic.invalid/v1", "api_key": "global-key"}}}))
    disabled = adapter(tmp_path)
    assert next(f for f in disabled.config()["fields"] if f["id"] == "api_key")["effective"]["value"] is None
    ui = adapter(tmp_path, use_home=True)
    changes = edits(method="api", ask=False, model="project", base_url="https://synthetic.invalid/v1")
    with pytest.raises(UIError, match="连接不完整"):
        ui.save(changes, ui.config()["revision"], "project")
    assert not (tmp_path / ".env.video-to-markdown").exists()


def test_secret_mask_is_never_a_key_and_unrelated_values_are_preserved(tmp_path):
    path = tmp_path / ".env.video-to-markdown"
    path.write_text("# existing unrelated settings\nUNRELATED=value\n")
    ui = adapter(tmp_path)
    with pytest.raises(UIError, match="遮盖值"):
        ui.save(edits(api_key="syn••••••key"), ui.config()["revision"], "project")
    ui.save(edits(method="main-agent", ask=False), ui.config()["revision"], "project")
    assert "UNRELATED=value" in path.read_text()


def test_save_refuses_tracked_secret_target_and_prepares_exact_ignore_for_untracked(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    path = tmp_path / ".env.video-to-markdown"
    path.write_text("# tracked test configuration\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env.video-to-markdown"], capture_output=True, check=True)
    ui = adapter(tmp_path)
    with pytest.raises(UIError, match="Git 跟踪"):
        ui.save(edits(method="main-agent", ask=False), ui.config()["revision"], "project")
    assert path.read_text() == "# tracked test configuration\n"
    # A different, untracked loader layer gets narrow ignore protection at save time.
    ui.save(edits(method="main-agent", ask=False), ui.config()["revision"], "local")
    result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", ".env.local"], capture_output=True)
    assert result.returncode == 0


def test_feishu_identity_is_same_layer_and_inherit_removes_only_vision_group(tmp_path):
    ui = adapter(tmp_path)
    with pytest.raises(UIError, match="同一文件"):
        ui.save(edits(**{"feishu-profile": "work"}), ui.config()["revision"], "project")
    ui.save(edits(method="main-agent", ask=False, output="D:/synthetic/output"), ui.config()["revision"], "project")
    ui.save({"inherit": {"op": "set", "value": True}}, ui.config()["revision"], "project")
    loaded = config.resolve({}, tmp_path)
    assert loaded.vision is None and loaded.output_base == "D:/synthetic/output"


def test_background_http_service_saves_without_owner_privilege_and_checks_csrf(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workflow = Path(__file__).resolve().parents[1] / "scripts" / "workflow.py"
    command = [sys.executable, str(workflow), "config-ui", "start", "--project", str(project), "--runtime-dir", str(tmp_path / "run")]
    started = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
    record = json.loads(started.stdout)
    origin, token = record["url"].split("/#")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, data=None, csrf=""):
        raw = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(origin + path, data=raw, headers={"Origin": origin, "Content-Type": "application/json", "X-CSRF-Token": csrf})
        with opener.open(req, timeout=10) as response:
            return json.load(response)
    try:
        request("/api/session", {"token": token})
        snapshot = request("/api/state")
        change = {"target": "project", "revision": snapshot["config"]["revision"], "changes": edits(method="main-agent", ask=False)}
        with pytest.raises(urllib.error.HTTPError) as rejection:
            request("/api/save", change)
        assert rejection.value.code == 403
        assert not (project / ".env.video-to-markdown").exists()
        saved = request("/api/save", change, snapshot["csrf"])
        assert saved["receipt"]["target"] == "project"
        assert config.resolve({}, project).vision["routes"]["codex"]["method"] == "main-agent"
        original = saved['config']['revision']
        partial = request('/api/save', {'target': 'project', 'revision': original,
            'changes': edits(asr='invalid', output=str(project / 'output'), request_id=str(uuid.uuid4()))}, snapshot['csrf'])
        assert partial['config']['task']['field_results']['asr']['status'] == 'failed'
        assert partial['config']['task']['field_results']['output']['status'] == 'saved'
        assert config.resolve({}, project).output_base == str(project / 'output')
    finally:
        command[3] = "stop"
        subprocess.run(command, check=True, capture_output=True, timeout=30)


def test_switching_to_main_agent_removes_stale_remote_model_and_connection(tmp_path):
    ui = adapter(tmp_path)
    ui.save(edits(method="api", ask=False, model="remote", base_url="https://synthetic.invalid/v1",
                  api_key="synthetic-only", protocol="chat-completions"), ui.config()["revision"], "project")
    ui.save(edits(method="main-agent"), ui.config()["revision"], "project")
    assert config.resolve({}, tmp_path).vision["routes"]["codex"] == {"method": "main-agent"}


def test_key_placeholder_cancellation_and_explicit_delete_follow_the_loader(tmp_path):
    ui = adapter(tmp_path)
    saved = edits(method="api", ask=True, model="remote", base_url="https://synthetic.invalid/v1",
                  api_key="synthetic-only", protocol="chat-completions")
    ui.save(saved, ui.config()["revision"], "project")
    ui.save(edits(model="new-model"), ui.config()["revision"], "project")
    assert config.resolve({}, tmp_path).vision["routes"]["codex"]["api_key"] == "synthetic-only"
    with pytest.raises(UIError, match="空 Key"):
        ui.save(edits(api_key=""), ui.config()["revision"], "project")
    ui.save({"api_key": {"op": "unset"}}, ui.config()["revision"], "project")
    assert "api_key" not in config.resolve({}, tmp_path).vision["routes"]["codex"]
    with pytest.raises(UIError, match="连接不完整"):
        ui.save(edits(ask=False), ui.config()["revision"], "project")


def test_fixed_without_method_rejected_instead_of_silently_picking_main_agent(tmp_path):
    ui = adapter(tmp_path)
    ui.save(edits(ask=True), ui.config()["revision"], "project")
    before = (tmp_path / ".env.video-to-markdown").read_bytes()
    with pytest.raises(UIError, match="先选择识别方式"):
        ui.save(edits(ask=False), ui.config()["revision"], "project")
    assert (tmp_path / ".env.video-to-markdown").read_bytes() == before


def test_restore_inheritance_previews_real_fallback_and_rejects_mixed_vision_drafts(tmp_path):
    ui = adapter(tmp_path, use_home=True)
    ui.save(edits(method="main-agent", ask=False), ui.config()["revision"], "global")
    ui.save(edits(ask=True), ui.config()["revision"], "project")
    restore = {"inherit": {"op": "set", "value": True}}
    projected = ui.preview(restore, ui.config()["revision"], "project")
    assert next(f for f in projected["fields"] if f["id"] == "ask")["effective"] == {
        "source": "global", "status": "configured", "value": False}
    with pytest.raises(UIError, match="不能同时提交"):
        ui.preview({**restore, **edits(model="parked")}, ui.config()["revision"], "project")
    ui.save({**restore, **edits(output="synthetic-output")}, ui.config()["revision"], "project")
    resolved = config.resolve({}, tmp_path, use_home=True, home_config_dir=tmp_path / "home")
    assert resolved.vision["routes"]["codex"]["method"] == "main-agent"
    assert resolved.output_base == "synthetic-output"


def test_any_route_edits_preserve_the_complete_same_file_connection(tmp_path):
    path = tmp_path / ".env.video-to-markdown"
    path.write_text(config.VISION_KEY + "=" + json.dumps({"ask_each_task": False, "routes": {
        "any": {"method": "api", "model": "original", "base_url": "https://synthetic.invalid/v1", "api_key": "same-file-key"}}}))
    ui = adapter(tmp_path)
    ui.save(edits(model="new-model"), ui.config()["revision"], "project")
    route = config.resolve({}, tmp_path).vision["routes"]["codex"]
    assert route["model"] == "new-model" and route["api_key"] == "same-file-key"
    assert route["base_url"] == "https://synthetic.invalid/v1"


def test_session_receipt_recovers_a_save_without_exposing_or_persisting_request_fields(tmp_path):
    ui = adapter(tmp_path)
    revision, request_id = ui.config()["revision"], str(uuid.uuid4())
    ui.save({**edits(method="main-agent", ask=False), "request_id": {"op": "set", "value": request_id}}, revision, "project")
    public = public_config(ui.config())
    receipt = next(f for f in public["fields"] if f["id"] == "save-receipt")["effective"]["value"]
    assert receipt == {"request_id": request_id, "target": "project", "before_revision": revision,
                       "after_revision": public["revision"],
                       "field_results": {'method': {'status': 'saved'}, 'ask': {'status': 'saved'}}}
    text = (tmp_path / ".env.video-to-markdown").read_text()
    assert "request_id" not in text and request_id not in text


@pytest.mark.parametrize("value", ["not-a-uuid", None, 1])
def test_bad_request_identifier_cannot_change_configuration(tmp_path, value):
    ui = adapter(tmp_path)
    with pytest.raises(UIError, match="请求标识"):
        ui.save({**edits(ask=True), "request_id": {"op": "set", "value": value}}, ui.config()["revision"], "project")
    assert not (tmp_path / ".env.video-to-markdown").exists()
