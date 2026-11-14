"""Real FFmpeg screening on deterministic slides, UI changes and motion."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from video_to_markdown import clarity, media, sampling, state
from test_review import fixture_task


def encode_images(tmp_path: Path, images: list[Image.Image]) -> Path:
    for index, image in enumerate(images):
        image.save(tmp_path / ("input-%03d.png" % index))
    video = tmp_path / "screen.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-framerate", "10", "-i",
                    str(tmp_path / "input-%03d.png"), "-c:v", "libx264", "-crf", "10", "-g", "120", str(video)],
                   check=True, capture_output=True, timeout=60)
    return video


@pytest.mark.parametrize("kind", ["slide", "popup", "motion", "static"])
def test_scene_and_tile_signals_on_representative_samples(tmp_path, kind):
    images = []
    for index in range(40):
        image = Image.new("RGB", (320, 192), "white")
        draw = ImageDraw.Draw(image)
        draw.text((8, 8), "Screening sample", fill="black")
        if kind == "slide" and index >= 12:
            draw.rectangle((0, 0, 319, 191), fill="#303030")
            draw.text((20, 20), "Second page", fill="white")
        elif kind == "popup" and 12 <= index < 23:
            draw.rectangle((90, 55, 125, 80), fill="black")
            draw.text((92, 60), "OK", fill="white")
        elif kind == "motion":
            draw.rectangle((index * 5, 40, index * 5 + 48, 95), fill="black")
        images.append(image)
    video = encode_images(tmp_path, images)
    local = media.local_change_candidates(video, start_ms=0, end_ms=4000, ffmpeg_bin="ffmpeg")
    scenes = media.scene_candidates(video, start_ms=0, end_ms=4000, ffmpeg_bin="ffmpeg")
    if kind == "static":
        assert not local and not scenes
    else:
        assert local, kind
    if kind == "slide":
        assert any(1100 <= pts <= 1300 for pts in scenes)
    if kind == "popup":
        assert not scenes  # Local change survives a low whole-frame scene score.
        detected = next(c for c in local if 1200 <= c["requested_ms"] < 2300)
        original = media.extract_frame(video, requested_ms=detected["requested_ms"], output_dir=tmp_path / "frames",
                                       ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        with Image.open(original["path"]) as frame:
            assert frame.convert("L").getpixel((95, 57)) < 40


def test_clarity_search_scores_real_neighbors_across_batch_boundary(tmp_path):
    images = []
    sharp = Image.new("RGB", (320, 192), "white")
    draw = ImageDraw.Draw(sharp)
    for x in range(10, 300, 16):
        draw.rectangle((x, 20, x + 5, 170), fill="black")
    for index in range(30):
        images.append(sharp.filter(ImageFilter.GaussianBlur(3)) if 8 <= index <= 12 else sharp)
    video = encode_images(tmp_path, images)
    (tmp_path / "fixture").mkdir()
    task, manifest = fixture_task(tmp_path / "fixture")
    manifest["source"] = {**media.probe_video(video, "ffprobe"), "physical_path": str(video),
                          "sha256": state.sha256_file(video)[0], "size_bytes": video.stat().st_size}
    summary, batch = sampling.load_batch(task, manifest, "batch-0001")
    summary.update(start_ms=1000, end_ms=2000)
    for old in batch["first_pass"]["frames"]:
        Path(old["path"]).unlink()
    reference = media.extract_frame(video, requested_ms=1000, output_dir=task / "assets/frames", ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    batch["first_pass"]["frames"] = [reference]
    state.atomic_write_json(task / summary["state_path"], batch)
    record = clarity.search(task, manifest, batch_id="batch-0001", frame_id=reference["frame_id"], issue_id="blur",
                            ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", original_checked=True, reason="blurred source")
    assert min(f["pts_ms"] for f in record["frames"]) < summary["start_ms"]
    assert all(f["pts_verified"] for f in record["frames"])
    ranking = sorted(record["frames"], key=lambda f: f["rank_score"])
    assert ranking[0]["blur_score"] < next(f for f in record["frames"] if f["frame_id"] == reference["frame_id"])["blur_score"]
    assert record["status"] == "awaiting_model_confirmation"  # FFmpeg alone never declares semantic resolution.
