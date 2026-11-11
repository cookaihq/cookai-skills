import json
from pathlib import Path
import shutil
import sys

from conftest import SKILL, page_payload


def submit(command, task, writer, page, path, text="Bandwidth: 100 kHz", issue=False):
    command("render", "--task", task, "--writer", writer, "--page", page)
    return command("submit-page", "--task", task, "--writer", writer, "--page", page,
                   "--input", page_payload(path, text, issue=issue))


def test_partial_resume_correction_unchanged_rerun_and_rollback(pdf, tmp_path, command):
    start = command("start", pdf, "--project", tmp_path, "--owner", "host-a", "--vision", "available")
    task, writer = Path(start["task_dir"]), start["writer"]
    submit(command, task, writer, 1, tmp_path / "one.json")
    command("fail-page", "--task", task, "--writer", writer, "--page", 2, "--reason", "Interrupted view")
    partial = command("finish", "--task", task, "--writer", writer)
    assert partial["status"] == "partial" and partial["current_result"] is None
    assert list((task / "cache").rglob("*.png"))
    resumed = command("resume", "--task", task, "--owner", "host-b", "--vision", "available")
    assert resumed["pending_pages"] == [2]
    first_path = resumed["pages"]["1"]["path"]
    submit(command, task, resumed["writer"], 2, tmp_path / "two.json", "Second page")
    complete = command("finish", "--task", task, "--writer", resumed["writer"])
    assert complete["current_result"] == "results/v002"
    resumed = command("resume", "--task", task, "--owner", "host-b", "--vision", "available")
    assert resumed["pending_pages"] == []
    assert resumed["pages"]["1"]["path"] == first_path
    unchanged = command("finish", "--task", task, "--writer", resumed["writer"])
    assert unchanged["versions"] == ["results/v001", "results/v002"]
    revision = command("resume", "--task", task, "--owner", "host-b", "--vision", "available", "--redo-pages", "1")
    submit(command, task, revision["writer"], 1, tmp_path / "three.json", "Corrected parameter")
    corrected = command("finish", "--task", task, "--writer", revision["writer"])
    assert corrected["current_result"] == "results/v003"
    rolled = command("adopt", "--task", task, "--result", "v002", "--owner", "host-b")
    assert rolled["current_result"] == "results/v002" and len(rolled["versions"]) == 3
    events = [json.loads(line) for line in (task / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"page-failed", "result-unchanged", "result-adopted"} <= {e["action"] for e in events}
    assert events[-1]["seq"] == rolled["seq"]


def test_writer_conflicts_corrupt_pages_and_interrupted_event_tail_recover(pdf, tmp_path, command):
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task, writer = Path(start["task_dir"]), start["writer"]
    denied = command("resume", "--task", task, "--owner", "b", "--vision", "available", expect=2)
    assert "owned" in denied["error"]
    entry = submit(command, task, writer, 1, tmp_path / "page.json")
    command("pause", "--task", task, "--writer", writer, "--reason", "interrupted")
    (task / entry["path"] / "raw.md").unlink()
    with (task / "events.jsonl").open("ab") as stream:
        stream.write(b'{"seq":999,"partial":')
    resumed = command("resume", "--task", task, "--owner", "b", "--vision", "available")
    assert resumed["pending_pages"] == [1]
    assert list((task / "evidence").glob("interrupted-event-*.bin"))
    assert (task / "task.json").is_file()
    (task / "source/document.pdf").write_bytes(pdf.read_bytes() + b"\n% changed\n")
    damaged = command("render", "--task", task, "--writer", resumed["writer"], "--page", 1, expect=2)
    assert "source" in damaged["error"].lower()
    events = [json.loads(line) for line in (task / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["action"] == "attempt-failed"


def test_disk_full_pauses_before_new_outputs_and_preserves_current_result(pdf, tmp_path, command, monkeypatch, capsys):
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task = Path(start["task_dir"])
    submit(command, task, start["writer"], 1, tmp_path / "p.json")
    command("finish", "--task", task, "--writer", start["writer"])
    resumed = command("resume", "--task", task, "--owner", "a", "--vision", "available", "--redo-pages", "1")
    sys.path.insert(0, str(SKILL / "scripts"))
    from workflow import main
    usage = shutil.disk_usage(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.total, 0))
        code = main(["render", "--task", str(task), "--writer", resumed["writer"], "--page", "1"])
        assert code == 2
        assert "space" in json.loads(capsys.readouterr().out)["error"].lower()
    status = command("status", "--task", task)
    assert status["status"] == "paused-space" and status["current_result"] == "results/v001"
    assert not list((task / "cache" / resumed["run"]["id"]).glob("*.png"))


def test_failed_resume_can_be_retried_with_new_cleaning_selection(pdf, tmp_path, command):
    profile = tmp_path / "profile.md"
    profile.write_text("Keep all technical values; remove only page footers.", encoding="utf-8")
    started = command("start", pdf, "--project", tmp_path, "--owner", "a", "--vision", "available",
                      "--cleaning", profile)
    task = Path(started["task_dir"])
    command("pause", "--task", task, "--writer", started["writer"], "--reason", "Restart host")
    (task / started["run"]["cleaning"]["path"]).unlink()
    command("resume", "--task", task, "--owner", "a", "--vision", "available", expect=2)
    assert command("status", "--task", task)["run"]["id"] == "r001"
    resumed = command("resume", "--task", task, "--owner", "a", "--vision", "available", "--no-cleaning")
    assert resumed["run"]["id"] == "r002"
    assert resumed["run"]["cleaning"]["enabled"] is False
