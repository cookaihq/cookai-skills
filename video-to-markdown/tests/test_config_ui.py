import json
import subprocess
import sys
import http.cookiejar
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from skill_ui.contracts import UIError, public_config
from video_to_markdown import config
from video_to_markdown.ui_adapter import VideoAdapter


def adapter(tmp_path, **kwargs):
    return VideoAdapter(tmp_path, home_config_dir=tmp_path / "home", environ={}, **kwargs)


def edits(**values):
    return {k: {"op": "set", "value": v} for k, v in values.items()}


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


def test_changed_lower_priority_layer_invalidates_revision_and_preserves_draft(tmp_path):
    ui = adapter(tmp_path)
    revision = ui.config()["revision"]
    (tmp_path / ".env").write_text("FFMPEG_BIN=some-other-ffmpeg\n")
    with pytest.raises(UIError, match="其他进程"):
        ui.save(edits(method="main-agent", ask=False), revision, "project")
    assert not (tmp_path / ".env.video-to-markdown").exists()


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
    finally:
        command[3] = "stop"
        subprocess.run(command, check=True, capture_output=True, timeout=30)


def test_switching_to_main_agent_removes_stale_remote_model_and_connection(tmp_path):
    ui = adapter(tmp_path)
    ui.save(edits(method="api", ask=False, model="remote", base_url="https://synthetic.invalid/v1",
                  api_key="synthetic-only", protocol="chat-completions"), ui.config()["revision"], "project")
    ui.save(edits(method="main-agent"), ui.config()["revision"], "project")
    assert config.resolve({}, tmp_path).vision["routes"]["codex"] == {"method": "main-agent"}
