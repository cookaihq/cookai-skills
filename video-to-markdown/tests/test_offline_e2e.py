import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
from PIL import Image, ImageDraw

from video_to_markdown import review, state


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_DIR / "scripts" / "workflow.py"


def run_workflow(cwd: Path, *arguments: str, expect: int = 0) -> dict:
    result = subprocess.run(
        [sys.executable, str(WORKFLOW), *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert result.returncode == expect, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def make_video(tmp_path: Path) -> Path:
    ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required")
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(60):
        first_scene = index < 30
        image = Image.new("RGB", (640, 360), "white" if first_scene else "#202020")
        draw = ImageDraw.Draw(image)
        draw.text(
            (30, 30),
            "Opening title" if first_scene else "Second scene",
            fill="black" if first_scene else "white",
        )
        x = 40 + (index % 30) * 15
        draw.rectangle(
            (x, 170, x + 70, 240),
            fill="#1261a0" if first_scene else "#38a169",
        )
        image.save(frames / ("frame-%03d.png" % index))
    video = tmp_path / "sample-video.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "10",
            "-i",
            str(frames / "frame-%03d.png"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        timeout=120,
    )
    return video


@pytest.mark.parametrize("opaque", [False, True])
def test_offline_end_to_end_with_frames_resampling_gif_and_resume(tmp_path: Path, monkeypatch, opaque):
    video = make_video(tmp_path)
    if opaque:
        video = video.rename(tmp_path / "a8f93.mp4")
        monkeypatch.setenv("VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR", "project")
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\nOpening title. The square moves.\n\n"
        "2\n00:00:03,000 --> 00:00:06,000\nSecond scene.\n",
        encoding="utf-8",
    )
    started = run_workflow(
        tmp_path,
        "start",
        "--video",
        str(video),
        "--subtitle",
        str(subtitle),
        "--output-parent",
        str(tmp_path / "output"),
    )
    task_dir = Path(started["task_dir"])
    assert task_dir.parent == tmp_path / "output"
    assert len(task_dir.name.split("-", 1)[0]) == 14
    assert task_dir.name.endswith("-" + video.name)
    assert Path(started["document"]).name == video.name + ".md"
    assert not (video.parent / (video.name + ".md")).exists()

    manifest = state.read_json(task_dir / "manifest.json")
    duration_ms = manifest["source"]["duration_ms"]
    batch_summary = manifest["batches"][0]
    batch_state = state.read_json(task_dir / batch_summary["state_path"])
    first_frame = batch_state["first_pass"]["frames"][0]
    assert 0 <= first_frame["pts_ms"] <= duration_ms
    for sheet in batch_state["first_pass"]["contact_sheets"]:
        with Image.open(sheet["path"]) as image:
            assert image.width > 0 and image.height > 0

    initial_observation = tmp_path / "observations-1.json"
    initial_observation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "observations": [
                    {
                        "observation_id": "moving-square",
                        "start_ms": 0,
                        "end_ms": duration_ms,
                        "description": "The title changes at three seconds and a square moves horizontally.",
                        "evidence_frame_ids": [first_frame["frame_id"]],
                        "motion_dependent": True,
                        "confidence": "certain",
                        "needs_resample": False,
                        "resample_reason": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_workflow(
        tmp_path,
        "record-observations",
        "--task-dir",
        str(task_dir),
        "--batch-id",
        "batch-0001",
        "--input",
        str(initial_observation),
    )

    resampled = run_workflow(
        tmp_path,
        "resample",
        "--task-dir",
        str(task_dir),
        "--batch-id",
        "batch-0001",
        "--start",
        "00:00:01.000",
        "--end",
        "00:00:03.000",
        "--interval-ms",
        "250",
        "--reason",
        "Confirm the movement between the sparse first-pass frames.",
    )
    assert len(resampled["frames"]) >= 4
    second_frame = resampled["frames"][1]
    revised = json.loads(initial_observation.read_text())
    revised["observations"][0]["evidence_frame_ids"] = [
        first_frame["frame_id"],
        second_frame["frame_id"],
    ]
    revised_observation = tmp_path / "observations-2.json"
    revised_observation.write_text(json.dumps(revised), encoding="utf-8")
    recorded = run_workflow(
        tmp_path,
        "record-observations",
        "--task-dir",
        str(task_dir),
        "--batch-id",
        "batch-0001",
        "--input",
        str(revised_observation),
    )
    assert recorded["observations"][0]["revision"] == 2

    run_workflow(
        tmp_path, "adaptive-review", "--task-dir", str(task_dir),
        "--reason", "The additional frames explain the moving square and title change.",
    )

    motion = run_workflow(
        tmp_path,
        "make-gif",
        "--task-dir",
        str(task_dir),
        "--start",
        "00:00:03.000",
        "--end",
        "00:00:05.500",
        "--purpose",
        "Show the square moving in the second scene.",
    )
    gif_path = task_dir / motion["path"]
    with Image.open(gif_path) as gif:
        assert getattr(gif, "n_frames", 1) > 1

    screenshot_relative = Path(first_frame["path"]).relative_to(task_dir).as_posix()
    document = (
        "# Opening title\n\n"
        "The opening title and initial square are visible at 00:00:00.000.\n\n"
        "![Opening title at 00:00:00.000](%s)\n\n"
        "# Second scene\n\n"
        "At 00:00:03.000 the title changes and the square continues moving.\n\n"
        "![Moving square from 00:00:03.000](%s)\n"
        % (screenshot_relative, motion["path"])
    )
    Path(started["document"]).write_text(document, encoding="utf-8")
    state.atomic_write_json(
        task_dir / "review-report.json",
        {
            "schema_version": 1,
            "document": Path(started["document"]).name,
            "intervals": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "classification": "represented_by_screenshot",
                    "document_anchor": "#opening-title",
                    "media": [screenshot_relative],
                    "observation_ids": ["moving-square"],
                    "transcript_segment_ids": ["segment-000001"],
                    "reason": None,
                },
                {
                    "start_ms": 3000,
                    "end_ms": duration_ms,
                    "classification": "represented_by_gif",
                    "document_anchor": "#second-scene",
                    "media": [motion["path"]],
                    "observation_ids": ["moving-square"],
                    "transcript_segment_ids": ["segment-000002"],
                    "reason": None,
                },
            ],
        },
    )
    validated = run_workflow(tmp_path, "validate", "--task-dir", str(task_dir))
    assert validated["complete"] is True

    resumed = run_workflow(tmp_path, "resume", "--task-dir", str(task_dir))
    assert resumed["stage"] == "validated"
    assert resumed["next_action"] == "finalize"

    finalized = run_workflow(tmp_path, "finalize", "--task-dir", str(task_dir),
                             *(["--title", "方块运动演示"] if opaque else ["--keep-name"]))
    task_dir = Path(finalized["task_dir"])
    expected = video.name + ("-方块运动演示" if opaque else "")
    assert task_dir.name.endswith("-" + expected)
    assert Path(finalized["document"]).name == expected + ".md"
    copied = Path(finalized["copy_document"])
    assert copied == video.parent / (expected + ".md")
    for match in review.IMAGE_LINK.finditer(copied.read_text(encoding="utf-8")):
        with Image.open(copied.parent / unquote(match.group("target"))) as media_image:
            assert media_image.width > 0
    assert run_workflow(tmp_path, "resume", "--task-dir", str(task_dir))["next_action"] == "complete"
    assert run_workflow(tmp_path, "finalize", "--task-dir", str(task_dir))["copy_document"] == str(copied)

    Path(finalized["document"]).unlink()
    resumed = run_workflow(tmp_path, "resume", "--task-dir", str(task_dir), expect=1)
    assert resumed["stage"] == "blocked_validation"
    assert resumed["next_action"] == "repair_artifacts"
    assert "document Markdown is missing or unsafe" in resumed["validation"]["blockers"]
    assert not Path(finalized["document"]).exists()
