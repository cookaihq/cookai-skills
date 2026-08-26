from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_to_markdown import media, paths


def test_task_name_uses_microseconds_and_safe_stem():
    now = datetime(2026, 8, 25, 17, 53, 3, 123456, tzinfo=timezone.utc)
    assert (
        paths.task_dir_name("/tmp/My Video.mp4", now)
        == "20260825175303123456-My Video"
    )


def test_output_base_and_explicit_parent_have_distinct_semantics(tmp_path: Path):
    project = tmp_path / "project"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    configured = paths.resolve_output_parent(
        cwd=cwd,
        project_root=project,
        configured_base="configured",
        explicit_parent=None,
    )
    assert configured == cwd / "configured" / "video-to-markdown-output"
    explicit = paths.resolve_output_parent(
        cwd=cwd,
        project_root=project,
        configured_base="ignored",
        explicit_parent="complete-parent",
    )
    assert explicit == cwd / "complete-parent"


def test_source_policy_accepts_only_unset_project_or_absolute(tmp_path: Path):
    assert paths.parse_source_policy(None).mode == "external_local"
    assert paths.parse_source_policy("project").mode == "project"
    absolute = paths.parse_source_policy(str(tmp_path))
    assert absolute.mode == "absolute"
    with pytest.raises(paths.PathError):
        paths.parse_source_policy("relative/path")


def test_project_source_policy_copies_into_task_directory(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    prepared, record = media.prepare_source(
        str(source),
        task_dir=task_dir,
        task_name=task_dir.name,
        policy=paths.parse_source_policy("project"),
    )

    assert prepared == (task_dir / "source" / "source.mp4").resolve()
    assert prepared.read_bytes() == b"video"
    assert record["managed"] is True


def test_absolute_source_policy_copies_into_task_named_directory(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    configured = tmp_path / "saved-sources"

    prepared, record = media.prepare_source(
        str(source),
        task_dir=task_dir,
        task_name=task_dir.name,
        policy=paths.parse_source_policy(str(configured)),
    )

    assert prepared == (configured / task_dir.name / "source.mp4").resolve()
    assert prepared.read_bytes() == b"video"
    assert record["managed"] is True


def test_safe_leaf_rejects_traversal_and_wrong_document_suffix():
    with pytest.raises(paths.PathError):
        paths.safe_leaf("../document.md", require_markdown=True)
    with pytest.raises(paths.PathError):
        paths.safe_leaf("document.txt", require_markdown=True)
