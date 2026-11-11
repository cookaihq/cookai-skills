import json
import os
from pathlib import Path

from test_recovery import submit

KEY = "PDF_TO_MARKDOWN_CLEANING_PROFILE"
PROFILE = "# 用途\n技术资料\n# 处理对象\n页眉\n# 匹配条件\n仅删除 Vendor header\n# 保留例外\n保留安全提示\n# 版本\n1\n"


def test_default_profile_temporary_override_and_reclean_keep_raw_and_snapshots(pdf, tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env.local").write_text("UNRELATED=keep\n", encoding="utf-8")
    profile = tmp_path / "profile.md"
    profile.write_text(PROFILE, encoding="utf-8")
    saved = command("profile-save", "--project", project, "--name", "knowledge", "--input", profile, "--set-default")
    assert saved["effective"]["source"] == ".env.local"
    assert "UNRELATED=keep\n" in (project / ".env.local").read_text(encoding="utf-8")
    subdir = project / "chapter"
    subdir.mkdir()
    start = command("start", pdf, "--project", project, "--pages", "1", "--owner", "host", "--vision", "available", cwd=subdir)
    task, writer = Path(start["task_dir"]), start["writer"]
    submit(command, task, writer, 1, tmp_path / "page.json", "Vendor header\n\nBandwidth: 100 kHz")
    draft = command("finish", "--task", task, "--writer", writer)
    assert draft["status"] == "needs-cleaning"
    raw = (task / draft["input_dir"] / "raw.md").read_text(encoding="utf-8")
    cleaned = {"markdown": raw.replace("Vendor header\n\n", ""), "review": "Only the matched vendor header was removed.",
               "changes": [{"page": 1, "block": "instrument", "action": "remove", "reason": "Selected profile matches Vendor header"}]}
    inp = tmp_path / "clean.json"
    inp.write_text(json.dumps(cleaned), encoding="utf-8")
    command("submit-cleaning", "--task", task, "--writer", writer, "--input", inp)
    final = command("finish", "--task", task, "--writer", writer)
    folder = task / final["current_result"]
    assert "Vendor header" in (folder / "raw.md").read_text(encoding="utf-8")
    assert "Vendor header" not in (folder / "document.md").read_text(encoding="utf-8")
    (project / "config/pdf-to-markdown/cleaning/knowledge.md").unlink()
    assert PROFILE == (task / start["run"]["cleaning"]["path"]).read_text(encoding="utf-8")
    unchanged_default = (project / ".env.local").read_bytes()
    resumed = command("resume", "--task", task, "--owner", "host", "--vision", "available", "--no-cleaning")
    assert resumed["pending_pages"] == []
    uncleaned = command("finish", "--task", task, "--writer", resumed["writer"])
    assert uncleaned["current_result"] == "results/v002"
    assert unchanged_default == (project / ".env.local").read_bytes()


def test_configuration_precedence_and_shadowed_default_are_reported(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".env").write_text(f"{KEY}=parent-ignored.md\n", encoding="utf-8")
    assert command("config", "--project", project)["value"] is None
    (project / ".env").write_text(f"{KEY}=base.md\n", encoding="utf-8")
    (project / ".env.local").write_text(f"{KEY}='local.md'\n", encoding="utf-8")
    (project / ".env.pdf-to-markdown-vision").write_text(f"{KEY}=first.md\n{KEY}=special.md\n", encoding="utf-8")
    assert command("config", "--project", project)["value"] == "special.md"
    env = dict(os.environ, **{KEY: "process.md"})
    assert command("config", "--project", project, env=env)["source"] == "process"
    (project / ".env.pdf-to-markdown-vision").write_text(f"{KEY}=\n", encoding="utf-8")
    assert command("config", "--project", project)["value"] == "local.md"
    profile = tmp_path / "new.md"
    profile.write_text(PROFILE, encoding="utf-8")
    saved = command("profile-save", "--project", project, "--name", "new", "--input", profile, "--set-default", env=env)
    assert saved["effective"]["value"] == "process.md" and saved["effective"]["source"] == "process"


def test_missing_profile_delivers_only_partial_raw_and_keeps_selection(pdf, tmp_path, command):
    (tmp_path / ".env.local").write_text(f"{KEY}=missing.md\n", encoding="utf-8")
    started = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task = Path(started["task_dir"])
    assert started["run"]["cleaning"]["error"]
    submit(command, task, started["writer"], 1, tmp_path / "p.json")
    result = command("finish", "--task", task, "--writer", started["writer"])
    assert result["status"] == "partial" and result["current_result"] is None
    assert (task / "results/v001/raw.md").is_file()
    assert not (task / "results/v001/cleaned.md").exists()
