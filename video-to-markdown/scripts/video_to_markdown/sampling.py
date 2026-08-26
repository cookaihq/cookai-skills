from __future__ import annotations

import math
import os
import subprocess
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import media, state


BATCH_MS = 300_000
PERIODIC_MS = 30_000
MAX_FIRST_PASS_FRAMES = 40
CONTACT_COLUMNS = 4
CONTACT_ROWS = 3


class SamplingError(ValueError):
    pass


def split_batches(duration_ms: int) -> list[dict]:
    if duration_ms <= 0:
        raise SamplingError("video duration must be positive")
    batches = []
    for index, start_ms in enumerate(range(0, duration_ms, BATCH_MS), 1):
        batches.append(
            {
                "batch_id": "batch-%04d" % index,
                "start_ms": start_ms,
                "end_ms": min(duration_ms, start_ms + BATCH_MS),
                "status": "pending",
                "state_path": ".state/batches/batch-%04d.json" % index,
            }
        )
    return batches


def _dedupe_candidates(values: list[tuple[int, str]], *, minimum_gap_ms: int = 500) -> list[dict]:
    merged = []
    for timestamp, reason in sorted(values):
        if merged and timestamp - merged[-1]["requested_ms"] < minimum_gap_ms:
            if reason not in merged[-1]["reasons"]:
                merged[-1]["reasons"].append(reason)
            continue
        merged.append({"requested_ms": timestamp, "reasons": [reason]})
    if len(merged) <= MAX_FIRST_PASS_FRAMES:
        return merged
    selected = []
    for index in range(MAX_FIRST_PASS_FRAMES):
        source_index = round(index * (len(merged) - 1) / (MAX_FIRST_PASS_FRAMES - 1))
        selected.append(merged[source_index])
    by_time = {item["requested_ms"]: item for item in selected}
    return [by_time[key] for key in sorted(by_time)]


def first_pass_candidates(
    source: Path,
    batch: dict,
    *,
    ffmpeg_bin: str,
) -> list[dict]:
    start_ms = batch["start_ms"]
    end_ms = batch["end_ms"]
    values = [(start_ms, "batch_start")]
    cursor = start_ms + PERIODIC_MS
    while cursor < end_ms:
        values.append((cursor, "periodic_fallback"))
        cursor += PERIODIC_MS
    values.append((max(start_ms, end_ms - 1), "batch_end"))
    for timestamp in media.scene_candidates(
        source,
        start_ms=start_ms,
        end_ms=end_ms,
        ffmpeg_bin=ffmpeg_bin,
    ):
        values.append((timestamp, "scene_change"))
    return _dedupe_candidates(values)


def _contact_sheet_page(frames: list[dict], destination: Path) -> None:
    cell_width = 480
    image_height = 270
    label_height = 34
    cell_height = image_height + label_height
    canvas = Image.new(
        "RGB",
        (CONTACT_COLUMNS * cell_width, CONTACT_ROWS * cell_height),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    for index, frame in enumerate(frames):
        row, column = divmod(index, CONTACT_COLUMNS)
        x = column * cell_width
        y = row * cell_height
        with Image.open(frame["path"]) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
            paste_x = x + (cell_width - normalized.width) // 2
            paste_y = y + (image_height - normalized.height) // 2
            canvas.paste(normalized, (paste_x, paste_y))
        draw.rectangle((x, y + image_height, x + cell_width, y + cell_height), fill="black")
        label = "%s  %s" % (frame["timestamp"], frame["frame_id"])
        draw.text((x + 8, y + image_height + 9), label, fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    try:
        canvas.save(temporary, format="JPEG", quality=88)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create_contact_sheets(frames: list[dict], output_dir: Path, prefix: str) -> list[dict]:
    page_size = CONTACT_COLUMNS * CONTACT_ROWS
    sheets = []
    for page_index in range(0, len(frames), page_size):
        page = frames[page_index : page_index + page_size]
        destination = output_dir / ("%s-p%03d.jpg" % (prefix, page_index // page_size + 1))
        _contact_sheet_page(page, destination)
        digest, size = state.sha256_file(destination)
        sheets.append(
            {
                "path": str(destination),
                "sha256": digest,
                "size_bytes": size,
                "frame_ids": [item["frame_id"] for item in page],
            }
        )
    return sheets


def prepare_first_pass(
    *,
    task_dir: Path,
    manifest: dict,
    source: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> None:
    if not manifest.get("batches"):
        manifest["batches"] = split_batches(manifest["source"]["duration_ms"])
        state.save_manifest(task_dir, manifest)
    for batch in manifest["batches"]:
        state_path = task_dir / batch["state_path"]
        if state_path.exists() and batch.get("status") != "pending":
            continue
        candidates = first_pass_candidates(source, batch, ffmpeg_bin=ffmpeg_bin)
        frames = []
        for candidate in candidates:
            frame = media.extract_frame(
                source,
                requested_ms=candidate["requested_ms"],
                output_dir=task_dir / "assets" / "frames",
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
            )
            frame["candidate_reasons"] = candidate["reasons"]
            if not any(item["frame_id"] == frame["frame_id"] for item in frames):
                frames.append(frame)
        sheets = create_contact_sheets(
            frames,
            task_dir / "assets" / "frames",
            batch["batch_id"] + "-contact",
        )
        batch_state = {
            "schema_version": 1,
            "batch_id": batch["batch_id"],
            "start_ms": batch["start_ms"],
            "end_ms": batch["end_ms"],
            "first_pass": {"frames": frames, "contact_sheets": sheets},
            "resampling_rounds": [],
            "observation_revisions": 0,
        }
        state.atomic_write_json(state_path, batch_state)
        batch["status"] = "ready_for_observation"
        batch["frame_count"] = len(frames)
        batch["contact_sheet_count"] = len(sheets)
        state.save_manifest(task_dir, manifest)


def load_batch(task_dir: Path, manifest: dict, batch_id: str | None = None) -> tuple[dict, dict]:
    summaries = manifest.get("batches") or []
    summary = None
    if batch_id:
        summary = next((item for item in summaries if item.get("batch_id") == batch_id), None)
    else:
        summary = next(
            (
                item
                for item in summaries
                if item.get("status") in {"ready_for_observation", "needs_resample"}
            ),
            None,
        )
    if summary is None:
        raise SamplingError("no matching batch is ready for observation")
    return summary, state.read_json(task_dir / summary["state_path"])


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    try:
        x, y, width, height = (int(item) for item in value.split(":"))
    except (TypeError, ValueError) as exc:
        raise SamplingError("crop must be x:y:w:h") from exc
    if min(x, y) < 0 or width <= 0 or height <= 0:
        raise SamplingError("crop coordinates must be non-negative with positive size")
    return x, y, width, height


def resample(
    *,
    task_dir: Path,
    manifest: dict,
    source: Path,
    batch_id: str,
    start_ms: int,
    end_ms: int,
    reason: str,
    interval_ms: int | None,
    crop: tuple[int, int, int, int] | None,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> dict:
    summary, batch_state = load_batch(task_dir, manifest, batch_id)
    if not reason.strip():
        raise SamplingError("resampling reason is required")
    if start_ms < summary["start_ms"] or end_ms > summary["end_ms"] or start_ms >= end_ms:
        raise SamplingError("resampling interval must be inside the selected batch")
    interval = interval_ms or max(250, min(2000, (end_ms - start_ms) // 12 or 250))
    if interval < 100:
        raise SamplingError("--interval-ms must be at least 100")
    requested = list(range(start_ms, end_ms, interval))
    if len(requested) > 60:
        raise SamplingError("resampling would exceed 60 frames; increase --interval-ms")
    frames = []
    for timestamp in requested:
        frame = media.extract_frame(
            source,
            requested_ms=timestamp,
            output_dir=task_dir / "assets" / "frames",
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            crop=crop,
        )
        if not any(item["frame_id"] == frame["frame_id"] for item in frames):
            frames.append(frame)
    round_number = len(batch_state.get("resampling_rounds") or []) + 1
    sheets = create_contact_sheets(
        frames,
        task_dir / "assets" / "frames",
        "%s-resample-%03d" % (batch_id, round_number),
    )
    record = {
        "round": round_number,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "reason": reason.strip(),
        "interval_ms": interval,
        "crop": list(crop) if crop is not None else None,
        "frames": frames,
        "contact_sheets": sheets,
    }
    batch_state.setdefault("resampling_rounds", []).append(record)
    state.atomic_write_json(task_dir / summary["state_path"], batch_state)
    summary["status"] = "ready_for_observation"
    summary["resampling_rounds"] = round_number
    manifest["adaptive_review_performed"] = True
    state.save_manifest(task_dir, manifest)
    return record


def make_gif(
    *,
    task_dir: Path,
    manifest: dict,
    source: Path,
    start_ms: int,
    end_ms: int,
    purpose: str,
    ffmpeg_bin: str,
    ffprobe_bin: str,
) -> dict:
    duration_ms = manifest["source"]["duration_ms"]
    if not purpose.strip():
        raise SamplingError("GIF purpose is required")
    if start_ms < 0 or end_ms > duration_ms or start_ms >= end_ms:
        raise SamplingError("GIF interval is outside the source duration")
    if end_ms - start_ms > 20_000:
        raise SamplingError("GIF interval must not exceed 20 seconds")
    actual_start = media.nearest_frame_pts(source, start_ms, ffprobe_bin)
    actual_end = media.nearest_frame_pts(source, max(start_ms, end_ms - 1), ffprobe_bin)
    if actual_end <= actual_start:
        raise SamplingError("GIF interval contains fewer than two decoded frames")
    name = "motion-%012d-%012d.gif" % (actual_start, actual_end)
    destination = task_dir / "assets" / "motion" / name
    filter_graph = (
        "fps=8,scale='min(960,iw)':-2:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=sierra2_4a"
    )
    command = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "%.6f" % (actual_start / 1000.0),
        "-t",
        "%.6f" % ((actual_end - actual_start) / 1000.0),
        "-i",
        str(source),
        "-filter_complex",
        filter_graph,
        "-loop",
        "0",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SamplingError("GIF generation failed: %s" % exc) from exc
    if result.returncode != 0:
        raise SamplingError(
            "GIF generation failed: %s"
            % result.stderr.decode("utf-8", "replace")[-2000:].strip()
        )
    digest, size = state.sha256_file(destination)
    record = {
        "path": str(destination.relative_to(task_dir)),
        "start_ms": actual_start,
        "end_ms": actual_end,
        "purpose": purpose.strip(),
        "sha256": digest,
        "size_bytes": size,
    }
    manifest.setdefault("motion_assets", []).append(record)
    state.save_manifest(task_dir, manifest)
    return record
