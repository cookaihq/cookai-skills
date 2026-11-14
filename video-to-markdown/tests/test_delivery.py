from pathlib import Path
from urllib.parse import unquote

import pytest

from video_to_markdown import delivery, review, state
from test_review import fixture_task


def prepared(tmp_path, *, copy=True):
    task, manifest = fixture_task(tmp_path)
    manifest["delivery"] = delivery.initialize("a8f93.mp4", "20261108132559-a8f93.mp4", tmp_path / "copies" if copy else None)
    state.save_manifest(task, manifest)
    return task


def finish(task, **options):
    return delivery.finalize(task, title=options.get("title"), keep_name=options.get("keep_name", False), copy_directory=options.get("copy_directory"))


def assert_portable(document):
    content = document.read_text(encoding="utf-8")
    for match in review.IMAGE_LINK.finditer(content):
        assert (document.parent / unquote(match.group("target"))).read_bytes() == b"frame"


def test_finalize_relocates_managed_source_and_frames_then_copies_portably(tmp_path):
    task = prepared(tmp_path)
    result = finish(task, title="主题 (示范)")
    renamed = Path(result["task_dir"])
    assert renamed.name == "20261108132559-a8f93.mp4-主题 (示范)"
    assert Path(result["document"]).name == "a8f93.mp4-主题 (示范).md"
    assert not task.exists()
    _, manifest, _ = state.load_task(renamed)
    assert review.validate_task(renamed, manifest)["complete"]
    assert delivery.copy_is_current(renamed, manifest)
    repeated = finish(renamed)
    assert repeated["copy_document"] == result["copy_document"]
    copied = Path(result["copy_document"])
    renamed.rename(tmp_path / "moved-away")
    assert_portable(copied)
    assert not list(copied.parent.rglob("private.json"))


def test_keep_name_and_url_without_copy(tmp_path):
    task = prepared(tmp_path, copy=False)
    result = finish(task, keep_name=True)
    assert Path(result["document"]).name == "a8f93.mp4.md"
    assert Path(result["task_dir"]).name == "20261108132559-a8f93.mp4"
    assert result["copy_document"] is None


def test_conflicting_copy_preserved_and_delivery_can_retry_elsewhere(tmp_path):
    task = prepared(tmp_path)
    occupied = tmp_path / "copies" / "a8f93.mp4.md"
    occupied.parent.mkdir()
    occupied.write_text("user notes")
    with pytest.raises(delivery.DeliveryError, match="different content"):
        finish(task, keep_name=True)
    assert occupied.read_text() == "user notes"
    assert list(occupied.parent.iterdir()) == [occupied]
    task = tmp_path / "20261108132559-a8f93.mp4"
    result = finish(task, copy_directory=str(tmp_path / "elsewhere"))
    assert Path(result["copy_document"]).parent == tmp_path / "elsewhere"


def test_rename_preflights_both_names_before_moving_document(tmp_path):
    task = prepared(tmp_path)
    (tmp_path / "20261108132559-a8f93.mp4-主题").mkdir()
    with pytest.raises(delivery.DeliveryError, match="task rename target already exists"):
        finish(task, title="主题")
    assert (task / "document.md").exists()
    assert state.read_json(task / "manifest.json")["artifacts"]["document"] == "document.md"


@pytest.mark.parametrize("after_directory_move", [False, True])
def test_interrupted_rename_is_recovered_before_validation(tmp_path, monkeypatch, after_directory_move):
    task = prepared(tmp_path)
    original_write = state.atomic_write_json
    original_rename = Path.rename
    if after_directory_move:
        def interrupted_write(path, value, **kwargs):
            if path.name == "batch-0001.json":
                raise OSError("simulated interruption")
            return original_write(path, value, **kwargs)
        monkeypatch.setattr(state, "atomic_write_json", interrupted_write)
    else:
        def interrupted_rename(path, target):
            if path == task:
                raise OSError("simulated interruption")
            return original_rename(path, target)
        monkeypatch.setattr(Path, "rename", interrupted_rename)
    with pytest.raises(OSError, match="simulated interruption"):
        finish(task, title="主题")
    monkeypatch.setattr(state, "atomic_write_json", original_write)
    monkeypatch.setattr(Path, "rename", original_rename)
    current = tmp_path / "20261108132559-a8f93.mp4-主题" if after_directory_move else task
    result = finish(current)
    assert result["stage"] == "complete"
    assert_portable(Path(result["copy_document"]))


def test_missing_copy_can_be_repaired_without_transcription(tmp_path):
    result = finish(prepared(tmp_path), keep_name=True)
    task, manifest, _ = state.load_task(result["task_dir"])
    Path(result["copy_document"]).unlink()
    assert not delivery.copy_is_current(task, manifest)
    assert finish(task)["stage"] == "complete"


def test_invalid_document_cannot_be_finalized(tmp_path):
    task = prepared(tmp_path)
    (task / "assets/frames/frame-000000000000.png").unlink()
    with pytest.raises(delivery.DeliveryError, match="document validation failed"):
        finish(task, keep_name=True)
    assert not (tmp_path / "copies").exists()
