"""Behavioral checks at the public CLI and its documented delivery files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from filelock import FileLock
import pytest

from conftest import SKILL, page_payload
from test_recovery import submit


def test_newer_installed_skill_reruns_all_pages_in_same_task_after_move(pdf, tmp_path, command):
    started = command("start", pdf, "--project", tmp_path, "--owner", "a", "--vision", "available")
    task = Path(started["task_dir"])
    submit(command, task, started["writer"], 1, tmp_path / "page.json")
    command("pause", "--task", task, "--writer", started["writer"], "--reason", "upgrade test")
    moved = tmp_path / "moved-task"
    shutil.copytree(task, moved)
    installation = tmp_path / "new-installation"
    shutil.copytree(SKILL, installation, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "tests"))
    source = installation / "SKILL.md"
    source.write_text(source.read_text(encoding="utf-8").replace('version: "1.0.0"', 'version: "1.0.1"').replace("v1.0.0｜", "v1.0.1｜"), encoding="utf-8")
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=sys.prefix)
    result = subprocess.run([sys.executable, str(installation / "scripts/workflow.py"), "resume", "--task", str(moved),
                             "--owner", "b", "--vision", "available"], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    resumed = json.loads(result.stdout)
    assert resumed["task_id"] == started["task_id"]
    assert resumed["pending_pages"] == [1, 2]
    assert resumed["run"]["skill_version"] == "1.0.1"
    assert (moved / started["run"]["snapshot"] / "SKILL.md").is_file()
    assert (moved / resumed["run"]["snapshot"] / "SKILL.md").is_file()
    assert command("status", "--task", task)["pending_pages"] == [2]


def test_process_lock_and_nonmatching_writer_do_not_change_task(pdf, tmp_path, command):
    started = command("start", pdf, "--project", tmp_path, "--owner", "a", "--vision", "available")
    task = Path(started["task_dir"])
    before = (task / "events.jsonl").read_bytes()
    with FileLock(str(task / ".write.lock")):
        result = command("render", "--task", task, "--writer", started["writer"], "--page", 1, expect=2)
        assert result["kind"] == "Timeout"
    command("render", "--task", task, "--writer", "different", "--page", 1, expect=2)
    assert (task / "events.jsonl").read_bytes() == before


def test_review_gap_never_replaces_complete_result(pdf, tmp_path, command):
    started = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task = Path(started["task_dir"])
    submit(command, task, started["writer"], 1, tmp_path / "page.json")
    command("finish", "--task", task, "--writer", started["writer"])
    resumed = command("resume", "--task", task, "--owner", "a", "--vision", "available", "--redo-pages", "1")
    submit(command, task, resumed["writer"], 1, tmp_path / "unclear.json", issue=True)
    result = command("finish", "--task", task, "--writer", resumed["writer"])
    assert result["status"] == "partial" and result["current_result"] == "results/v001"
    assert list((task / "evidence").glob("*.png"))
    assert list((task / "cache").rglob("*.png"))


@pytest.mark.parametrize("bad", ["![fake](missing.png)", "![escape](../../outside.png)",
                                  '<img src="https://example.com/secret.png">'])
def test_nonportable_resources_rejected_before_completion(pdf, tmp_path, command, bad):
    started = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task = Path(started["task_dir"])
    command("render", "--task", task, "--writer", started["writer"], "--page", 1)
    result = command("submit-page", "--task", task, "--writer", started["writer"], "--page", 1,
                     "--input", page_payload(tmp_path / "bad.json", bad), expect=2)
    assert result["status"] == "error"
    state = command("status", "--task", task)
    assert state["pending_pages"] == [1] and state["current_result"] is None


def test_absent_vision_and_invalid_pages_do_not_create_completed_task(pdf, tmp_path, command):
    command("start", pdf, "--project", tmp_path, "--owner", "a", "--vision", "unavailable", expect=2)
    command("start", pdf, "--project", tmp_path, "--owner", "a", "--vision", "available", "--pages", "0,3", expect=2)
    assert not list((tmp_path / "data/pdf-to-markdown").glob("*/task.json"))
