import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from video_to_markdown import state


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_DIR / "scripts" / "workflow.py"


def run_workflow(cwd: Path, *arguments: str, expect: int = 0) -> dict:
    result = subprocess.run(
        [sys.executable, str(WORKFLOW), *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == expect, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def make_video(tmp_path: Path) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")
    if shutil.which("say") is None:
        pytest.skip("macOS say is required for the generated-speech fixture")
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
    speech = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(speech), "Opening title. The square moves. Second scene."],
        check=True,
        timeout=30,
    )
    video = tmp_path / "sample-video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "10",
            "-i",
            str(frames / "frame-%03d.png"),
            "-i",
            str(speech),
            "-af",
            "apad",
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


def test_offline_end_to_end_with_frames_resampling_gif_and_resume(tmp_path: Path):
    video = make_video(tmp_path)
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
    assert len(task_dir.name.split("-", 1)[0]) == 20

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
    (task_dir / "document.md").write_text(document, encoding="utf-8")
    state.atomic_write_json(
        task_dir / "review-report.json",
        {
            "schema_version": 1,
            "document": "document.md",
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
    assert resumed["stage"] == "complete"
    assert resumed["next_action"] == "ask_rename_questions"
