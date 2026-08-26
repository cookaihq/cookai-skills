from argparse import Namespace

import pytest

from video_to_markdown import cli, state


def make_valid_task(tmp_path):
    task_dir = tmp_path / "original-task"
    task_dir.mkdir()
    manifest, _ = state.initialize_task(
        task_dir,
        settings_snapshot={"asr_model": "paraformer-v2", "sources": {}},
    )
    manifest["validation"]["complete"] = True
    state.save_manifest(task_dir, manifest)
    (task_dir / "document.md").write_text("# Document\n", encoding="utf-8")
    state.atomic_write_json(
        task_dir / "review-report.json",
        {"document": "document.md"},
    )
    return task_dir


def test_rename_preflights_both_targets_before_changing_document(tmp_path, monkeypatch):
    task_dir = make_valid_task(tmp_path)
    (tmp_path / "occupied-task").mkdir()
    monkeypatch.setattr(
        cli.review,
        "validate_task",
        lambda task_dir, manifest: {"complete": True, "blockers": []},
    )

    with pytest.raises(cli.WorkflowError, match="task rename target already exists"):
        cli.command_rename(
            Namespace(
                task_dir=str(task_dir),
                document_name="renamed.md",
                task_name="occupied-task",
            )
        )

    assert (task_dir / "document.md").is_file()
    assert not (task_dir / "renamed.md").exists()
    assert state.read_json(task_dir / "manifest.json")["artifacts"]["document"] == "document.md"


def test_rename_changes_document_and_task_directory(tmp_path, monkeypatch):
    task_dir = make_valid_task(tmp_path)
    monkeypatch.setattr(
        cli.review,
        "validate_task",
        lambda task_dir, manifest: {"complete": True, "blockers": []},
    )

    assert (
        cli.command_rename(
            Namespace(
                task_dir=str(task_dir),
                document_name="notes.md",
                task_name="renamed-task",
            )
        )
        == 0
    )

    renamed = tmp_path / "renamed-task"
    assert not task_dir.exists()
    assert (renamed / "notes.md").read_text(encoding="utf-8") == "# Document\n"
    manifest = state.read_json(renamed / "manifest.json")
    assert manifest["task_dir_name"] == "renamed-task"
    assert manifest["artifacts"]["document"] == "notes.md"
    assert state.read_json(renamed / "review-report.json")["document"] == "notes.md"
