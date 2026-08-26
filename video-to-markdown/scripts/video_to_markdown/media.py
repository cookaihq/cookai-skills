from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from . import paths, state


USER_AGENT = "video-to-markdown/1.0"
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_SECONDS = 120


class MediaError(ValueError):
    pass


def _run(command: list[str], *, timeout: float, operation: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        name = command[0]
        hint = "brew install ffmpeg" if "ff" in Path(name).name else "install %s" % name
        raise MediaError("%s is unavailable. Run: %s" % (name, hint)) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("%s exceeded %.0f seconds" % (operation, timeout)) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[-2000:]
        raise MediaError("%s failed: %s" % (operation, detail.strip()))
    return result


def preflight(ffmpeg_bin: str, ffprobe_bin: str) -> dict:
    versions = {}
    for key, binary in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)):
        result = _run([binary, "-version"], timeout=30, operation=key + " preflight")
        line = result.stdout.decode("utf-8", "replace").splitlines()
        versions[key] = line[0] if line else "unknown"
    try:
        import PIL
    except Exception as exc:
        raise MediaError("Pillow import failed: %s" % exc) from exc
    versions["pillow"] = getattr(PIL, "__version__", "unknown")
    return versions


def probe_video(path: Path, ffprobe_bin: str) -> dict:
    result = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
        operation="ffprobe source video",
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned invalid JSON") from exc
    streams = document.get("streams") if isinstance(document, dict) else None
    if not isinstance(streams, list) or not any(
        isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
    ):
        raise MediaError("source has no video stream")
    duration_values = []
    format_data = document.get("format") if isinstance(document.get("format"), dict) else {}
    duration_values.append(format_data.get("duration"))
    duration_values.extend(
        item.get("duration") for item in streams if isinstance(item, dict)
    )
    duration = None
    for value in duration_values:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            duration = candidate if duration is None else max(duration, candidate)
    if duration is None:
        raise MediaError("ffprobe did not report a positive duration")
    return {
        "duration_ms": int(round(duration * 1000)),
        "format": format_data,
        "streams": streams,
    }


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _transient_download(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def download_video(
    url: str,
    destination: Path,
    *,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.%s.download" % (destination.name, uuid.uuid4().hex))
    last_error: Exception | None = None
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    content_type = response.headers.get_content_type()
                    if content_type.startswith("text/") or content_type in {
                        "application/json",
                        "text/html",
                    }:
                        raise MediaError(
                            "URL returned %s, not a directly downloadable video"
                            % content_type
                        )
                    with temporary.open("xb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                if temporary.stat().st_size == 0:
                    raise MediaError("downloaded video is empty")
                os.replace(temporary, destination)
                return
            except MediaError:
                raise
            except Exception as exc:
                last_error = exc
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                if not _transient_download(exc) or attempt == DOWNLOAD_ATTEMPTS:
                    break
                wait = 2 ** (attempt - 1)
                sleep(wait)
        raise MediaError("video download failed after 3 attempts: %s" % last_error)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def prepare_source(
    video: str,
    *,
    task_dir: Path,
    task_name: str,
    policy: paths.SourcePolicy,
) -> tuple[Path, dict]:
    filename = paths.source_filename(video)
    remote = paths.is_http_url(video)
    if policy.mode == "absolute":
        assert policy.directory is not None
        destination = policy.directory / task_name / filename
        managed = True
    elif policy.mode == "project" or remote:
        destination = task_dir / "source" / filename
        managed = True
    else:
        destination = Path(video).expanduser().resolve()
        managed = False
    if remote:
        download_video(video, destination)
    else:
        source = Path(video).expanduser().resolve()
        if not source.is_file():
            raise MediaError("local video does not exist: %s" % source)
        if managed:
            _copy_atomic(source, destination)
    digest, size = state.sha256_file(destination)
    return destination.resolve(), {
        "kind": "url" if remote else "local",
        "origin": video,
        "original_filename": filename,
        "managed": managed,
        "physical_path": str(destination.resolve()),
        "sha256": digest,
        "size_bytes": size,
    }


def verify_source(source_record: dict) -> Path:
    path = Path(source_record.get("physical_path", ""))
    if not path.is_file():
        raise MediaError("source video is missing: %s" % path)
    digest, size = state.sha256_file(path)
    if digest != source_record.get("sha256") or size != source_record.get("size_bytes"):
        raise MediaError("source video changed since the task was created")
    return path


def extract_audio(source: Path, destination: Path, ffmpeg_bin: str) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.%s.flac" % (destination.name, uuid.uuid4().hex))
    try:
        _run(
            [
                ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "flac",
                str(temporary),
            ],
            timeout=1800,
            operation="audio extraction",
        )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    digest, size = state.sha256_file(destination)
    return {"path": str(destination), "sha256": digest, "size_bytes": size}


def parse_timecode(value: str | int) -> int:
    if isinstance(value, int):
        if value < 0:
            raise MediaError("time must not be negative")
        return value
    text = str(value).strip()
    match = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]?\d)(?:[.,](\d{1,3}))?", text)
    if not match:
        raise MediaError("invalid timecode: %s" % value)
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    fraction = (match.group(4) or "").ljust(3, "0")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction or 0)


def format_timecode(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return "%02d:%02d:%02d.%03d" % (hours, minutes, seconds, millis)


def nearest_frame_pts(source: Path, target_ms: int, ffprobe_bin: str) -> int:
    target_seconds = max(0.0, target_ms / 1000.0)
    start_seconds = max(0.0, target_seconds - 1.0)
    result = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            "%.6f%%+3" % start_seconds,
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(source),
        ],
        timeout=120,
        operation="frame PTS lookup",
    )
    values = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            values.append(int(round(float(line.strip().split(",")[0]) * 1000)))
        except (ValueError, IndexError):
            continue
    if not values:
        raise MediaError("ffprobe returned no frame PTS near %s" % format_timecode(target_ms))
    after = [item for item in values if item >= target_ms]
    return min(after) if after else min(values, key=lambda item: abs(item - target_ms))


def extract_frame(
    source: Path,
    *,
    requested_ms: int,
    output_dir: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    crop: tuple[int, int, int, int] | None = None,
) -> dict:
    actual_ms = nearest_frame_pts(source, requested_ms, ffprobe_bin)
    suffix = ""
    filters = []
    if crop is not None:
        x, y, width, height = crop
        filters.append("crop=%d:%d:%d:%d" % (width, height, x, y))
        suffix = "-crop-%d-%d-%d-%d" % crop
    frame_id = "frame-%012d%s" % (actual_ms, suffix)
    destination = output_dir / (frame_id + ".jpg")
    if not destination.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "%.6f" % (actual_ms / 1000.0),
            "-i",
            str(source),
            "-frames:v",
            "1",
        ]
        if filters:
            command.extend(["-vf", ",".join(filters)])
        command.extend(["-q:v", "2", str(destination)])
        _run(command, timeout=180, operation="frame extraction")
    digest, size = state.sha256_file(destination)
    return {
        "frame_id": frame_id,
        "requested_ms": requested_ms,
        "pts_ms": actual_ms,
        "timestamp": format_timecode(actual_ms),
        "path": str(destination),
        "sha256": digest,
        "size_bytes": size,
        "crop": list(crop) if crop is not None else None,
    }


SCENE_PTS = re.compile(r"pts_time:([0-9.]+)")


def scene_candidates(
    source: Path,
    *,
    start_ms: int,
    end_ms: int,
    ffmpeg_bin: str,
    threshold: float = 0.30,
) -> list[int]:
    duration = max(0.001, (end_ms - start_ms) / 1000.0)
    command = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-ss",
        "%.6f" % (start_ms / 1000.0),
        "-t",
        "%.6f" % duration,
        "-i",
        str(source),
        "-vf",
        "select='gt(scene,%.4f)',showinfo" % threshold,
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(180, int(duration * 4)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError("scene detection failed: %s" % exc) from exc
    if result.returncode != 0:
        raise MediaError(
            "scene detection failed: %s"
            % result.stderr.decode("utf-8", "replace")[-2000:].strip()
        )
    candidates = []
    for match in SCENE_PTS.finditer(result.stderr.decode("utf-8", "replace")):
        value = start_ms + int(round(float(match.group(1)) * 1000))
        if start_ms <= value < end_ms:
            candidates.append(value)
    return sorted(set(candidates))
