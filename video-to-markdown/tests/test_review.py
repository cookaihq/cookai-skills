from pathlib import Path

import pytest

from video_to_markdown import review, state


def fixture_task(tmp_path: Path) -> tuple[Path, dict]:
    task = tmp_path / "task"
    task.mkdir()
    manifest, _ = state.initialize_task(
        task,
        settings_snapshot={"asr_model": "paraformer-v2", "sources": {}},
    )
    source = task / "source" / "video.mp4"
    source.write_bytes(b"video")
    source_hash, source_size = state.sha256_file(source)
    frame = task / "assets" / "frames" / "frame-000000000000.jpg"
    frame.write_bytes(b"frame")
    frame_hash, frame_size = state.sha256_file(frame)
    batch = {
        "schema_version": 1,
        "batch_id": "batch-0001",
        "start_ms": 0,
        "end_ms": 1000,
        "first_pass": {
            "frames": [
                {
                    "frame_id": "frame-000000000000",
                    "pts_ms": 0,
                    "path": str(frame),
                    "sha256": frame_hash,
                    "size_bytes": frame_size,
                }
            ],
            "contact_sheets": [],
        },
        "resampling_rounds": [],
        "observation_revisions": 1,
    }
    state.atomic_write_json(task / ".state" / "batches" / "batch-0001.json", batch)
    manifest["source"] = {
        "physical_path": str(source),
        "sha256": source_hash,
        "size_bytes": source_size,
        "duration_ms": 1000,
    }
    manifest["batches"] = [
        {
            "batch_id": "batch-0001",
            "start_ms": 0,
            "end_ms": 1000,
            "status": "observed",
            "state_path": ".state/batches/batch-0001.json",
        }
    ]
    manifest["adaptive_review_performed"] = True
    state.save_manifest(task, manifest)
    state.atomic_write_json(
        task / "transcript.json",
        {
            "schema_version": 1,
            "segments": [{"segment_id": "segment-000001", "text": "spoken"}],
        },
    )
    observation = {
        "observation_id": "visual-1",
        "batch_id": "batch-0001",
        "revision": 1,
        "confidence": "certain",
        "needs_resample": False,
    }
    state.atomic_write_json(
        task / "visual-observations.json",
        {
            "schema_version": 1,
            "revisions": [{"observations": [observation]}],
            "latest": [observation],
        },
    )
    (task / "document.md").write_text(
        "# Opening\n\n![frame](assets/frames/frame-000000000000.jpg)\n",
        encoding="utf-8",
    )
    state.atomic_write_json(
        task / "review-report.json",
        {
            "schema_version": 1,
            "document": "document.md",
            "intervals": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "classification": "represented_by_screenshot",
                    "document_anchor": "#opening",
                    "media": ["assets/frames/frame-000000000000.jpg"],
                    "observation_ids": ["visual-1"],
                    "transcript_segment_ids": ["segment-000001"],
                    "reason": None,
                }
            ],
        },
    )
    return task, manifest


def test_complete_review_passes(tmp_path: Path):
    task, manifest = fixture_task(tmp_path)
    result = review.validate_task(task, manifest)
    assert result["complete"] is True
    assert result["blockers"] == []


def test_timeline_gap_blocks_completion(tmp_path: Path):
    task, manifest = fixture_task(tmp_path)
    report = state.read_json(task / "review-report.json")
    report["intervals"][0]["start_ms"] = 1
    state.atomic_write_json(task / "review-report.json", report)
    result = review.validate_task(task, manifest)
    assert result["complete"] is False
    assert any("gap or overlap" in item for item in result["blockers"])


def test_out_of_range_transcript_time_blocks_completion(tmp_path: Path):
    task, manifest = fixture_task(tmp_path)
    transcript_doc = state.read_json(task / "transcript.json")
    transcript_doc["segments"][0].update({"start_ms": 0, "end_ms": 1001})
    state.atomic_write_json(task / "transcript.json", transcript_doc)

    result = review.validate_task(task, manifest)

    assert result["complete"] is False
    assert any("invalid normalized times" in item for item in result["blockers"])


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [
        (0.0, 1000),
        (None, 1000),
        (900, 800),
    ],
)
def test_invalid_transcript_time_shape_blocks_completion(
    tmp_path: Path,
    start_ms: object,
    end_ms: object,
):
    task, manifest = fixture_task(tmp_path)
    transcript_doc = state.read_json(task / "transcript.json")
    transcript_doc["segments"][0].update({"start_ms": start_ms, "end_ms": end_ms})
    state.atomic_write_json(task / "transcript.json", transcript_doc)

    result = review.validate_task(task, manifest)

    assert result["complete"] is False
    assert any("invalid normalized times" in item for item in result["blockers"])
