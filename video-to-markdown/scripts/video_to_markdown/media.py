from __future__ import annotations

import json
import http.client
import math
from email.utils import parsedate_to_datetime
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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


class IncompleteDownload(ConnectionError):
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
        if value is None:
            continue
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
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead))


def _download_retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        raw = exc.headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(raw).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = float("nan")
            if math.isfinite(delay) and delay >= 0:
                return min(delay, 60.0)  # ADR 0006's cap for this Skill.
    return float(2 ** (attempt - 1))


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
                    raw_length = response.headers.get("Content-Length")
                    try:
                        expected_size = int(raw_length) if raw_length is not None else None
                    except ValueError as exc:
                        raise MediaError("download response has an invalid Content-Length") from exc
                    if expected_size is not None and expected_size < 0:
                        raise MediaError("download response has a negative Content-Length")
                    received_size = 0
                    with temporary.open("xb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            received_size += len(chunk)
                        if expected_size is not None and received_size != expected_size:
                            raise IncompleteDownload("incomplete response body")
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
                wait = _download_retry_delay(exc, attempt)
                print("video download attempt %d/%d failed (%s); retry in %.1fs"
                      % (attempt, DOWNLOAD_ATTEMPTS, type(exc).__name__, wait), file=sys.stderr)
                sleep(wait)
        reason = "incomplete response body" if isinstance(last_error, (IncompleteDownload, http.client.IncompleteRead)) else (
            "HTTP %d" % last_error.code if isinstance(last_error, urllib.error.HTTPError) else type(last_error).__name__
        )
        raise MediaError("video download failed after %d attempt(s): %s" % (attempt, reason))
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
        "origin": "%s://%s" % (urllib.parse.urlsplit(video).scheme, urllib.parse.urlsplit(video).hostname)
        if remote else video,
        "origin_is_private": remote,
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


def verify_audio(task_dir: Path, audio_record: dict) -> Path:
    relative = Path(audio_record.get("path", ""))
    path = (task_dir / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(task_dir.resolve()):
        raise MediaError("prepared audio path is outside the task directory")
    if not path.is_file():
        raise MediaError("prepared audio is missing; restore the original audio before resuming")
    digest, size = state.sha256_file(path)
    if digest != audio_record.get("sha256") or size != audio_record.get("size_bytes"):
        raise MediaError("prepared audio changed; restore the original audio before resuming")
    return path


def extract_audio(source: Path, destination: Path, ffmpeg_bin: str, *, ffprobe_bin: str = "ffprobe", source_record: dict | None = None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.%s.mp3" % (destination.name, uuid.uuid4().hex))
    streams = (source_record or {}).get("streams", [])
    audio_stream: dict = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    if source_record is not None and not audio_stream:
        raise MediaError("video has no audio track; provide a timestamped transcript")
    # Keep the first audio track and its offset on the source video timeline.
    video_start = float((source_record or {}).get("format", {}).get("start_time", 0) or 0)
    offset_ms = round((float(audio_stream.get("start_time", video_start) or 0) - video_start) * 1000)
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
                "24000",
                "-c:a",
                "libmp3lame",
                "-b:a", "64k",
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
    audio_probe = json.loads(_run([ffprobe_bin, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(destination)], timeout=120, operation="ffprobe extracted audio").stdout)
    audio_duration_ms = round(float(audio_probe["format"]["duration"]) * 1000)
    if len(audio_probe.get("streams", [])) != 1 or audio_probe["streams"][0].get("codec_type") != "audio" or audio_duration_ms <= 0:
        raise MediaError("extracted audio is not a complete decodable audio-only file")
    return {"path": str(destination), "sha256": digest, "size_bytes": size,
            "codec": "mp3", "sample_rate": 24000, "channels": 1, "bitrate": 64000,
            "track_index": audio_stream.get("index", 0), "time_offset_ms": offset_ms,
            "duration_ms": audio_duration_ms, "encoded_start_time": audio_probe["streams"][0].get("start_time"),
            "source_duration_ms": (source_record or {}).get("duration_ms")}


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
