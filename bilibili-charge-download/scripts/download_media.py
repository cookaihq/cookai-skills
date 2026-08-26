#!/usr/bin/env python3
"""Download and verify authorized Bilibili DASH media from a protected manifest."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import email.utils
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
USER_AGENT = "Mozilla/5.0"
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
CDN_SUFFIXES = ("bilibili.com", "bilivideo.com")
BILIBILI_AKAMAI_RE = re.compile(
    r"^upos-[a-z0-9-]+-mirrorakam\.akamaized\.net$"
)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
COPY_BUFFER_SIZE = 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 60
DEFAULT_NETWORK_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 60.0
EXTERNAL_TOOL_TIMEOUT_SECONDS = 30
EXTERNAL_TOOL_INSTALL_COMMAND = "brew install ffmpeg"


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaSpec:
    label: str
    urls: Tuple[str, ...]
    stream_id: int
    codecs: str
    bandwidth: int
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class Manifest:
    path: Path
    source_url: str
    bvid: str
    title: str
    duration_ms: int
    video: MediaSpec
    audio: MediaSpec


@dataclass(frozen=True)
class ByteRange:
    index: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def log(message: str) -> None:
    print(message, flush=True)


def transient_error_reason(exc: BaseException) -> Optional[str]:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "http_429"
        if 500 <= exc.code <= 599:
            return f"http_{exc.code}"
        return None
    if isinstance(exc, urllib.error.URLError):
        return "url_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, ConnectionError):
        return "connection_error"
    if isinstance(exc, http.client.IncompleteRead):
        return "incomplete_response"
    return None


def retry_delay_seconds(exc: BaseException, retry_number: int) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        raw_value = exc.headers.get("Retry-After") if exc.headers else None
        if raw_value is not None:
            try:
                retry_after = float(raw_value)
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(raw_value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                    retry_after = max(0.0, retry_at.timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    retry_after = math.nan
            if math.isfinite(retry_after) and retry_after >= 0:
                return min(retry_after, MAX_RETRY_AFTER_SECONDS)
    return float(min(2 ** (retry_number - 1), int(MAX_RETRY_AFTER_SECONDS)))


def wait_before_retry(
    label: str,
    stage: str,
    next_attempt: int,
    total_attempts: int,
    exc: BaseException,
) -> None:
    reason = transient_error_reason(exc)
    if reason is None:
        raise ValueError("wait_before_retry requires a transient error")
    delay = retry_delay_seconds(exc, next_attempt - 1)
    log(
        f"{label}_network_retry stage={stage} attempt={next_attempt}/{total_attempts} "
        f"wait_seconds={delay:g} reason={reason}"
    )
    time.sleep(delay)


def require_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DownloadError(f"manifest field {field} must be an integer >= {minimum}")
    return value


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DownloadError(f"manifest field {field} must be a non-empty string")
    return value.strip()


def validate_url(url: str, allow_http_localhost: bool) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password:
        raise DownloadError("media URLs must not contain user information")
    if allow_http_localhost and parsed.scheme == "http" and host in LOCAL_HOSTS:
        return url
    if parsed.scheme != "https":
        raise DownloadError("media URLs must use HTTPS")
    is_bilibili_host = any(
        host == suffix or host.endswith(f".{suffix}") for suffix in CDN_SUFFIXES
    )
    if not is_bilibili_host and not BILIBILI_AKAMAI_RE.fullmatch(host):
        raise DownloadError("media URL host is outside the allowed Bilibili domains")
    return url


def parse_media(value: Any, label: str, allow_http_localhost: bool) -> MediaSpec:
    if not isinstance(value, dict):
        raise DownloadError(f"manifest field {label} must be an object")
    primary = validate_url(
        require_string(value.get("url"), f"{label}.url"), allow_http_localhost
    )
    backups_value = value.get("backup_urls", [])
    if not isinstance(backups_value, list):
        raise DownloadError(f"manifest field {label}.backup_urls must be an array")
    urls: List[str] = [primary]
    for index, backup in enumerate(backups_value):
        validated = validate_url(
            require_string(backup, f"{label}.backup_urls[{index}]"),
            allow_http_localhost,
        )
        if validated not in urls:
            urls.append(validated)
    width = value.get("width")
    height = value.get("height")
    return MediaSpec(
        label=label,
        urls=tuple(urls),
        stream_id=require_int(value.get("id"), f"{label}.id"),
        codecs=require_string(value.get("codecs"), f"{label}.codecs"),
        bandwidth=require_int(value.get("bandwidth"), f"{label}.bandwidth", 1),
        width=require_int(width, f"{label}.width", 1) if width is not None else None,
        height=require_int(height, f"{label}.height", 1) if height is not None else None,
    )


def load_manifest(path: Path, allow_http_localhost: bool) -> Manifest:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise DownloadError("manifest file is not readable") from exc
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise DownloadError("manifest permissions must be 0600 or stricter")
    if file_stat.st_size > 1024 * 1024:
        raise DownloadError("manifest exceeds the 1 MiB size limit")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise DownloadError("manifest root must be an object")
    version = require_int(data.get("schema_version"), "schema_version", 1)
    if version != SCHEMA_VERSION:
        raise DownloadError(f"unsupported manifest schema_version: {version}")
    source_url = require_string(data.get("source_url"), "source_url")
    source = urllib.parse.urlsplit(source_url)
    if source.scheme != "https" or source.hostname not in {
        "bilibili.com",
        "www.bilibili.com",
    }:
        raise DownloadError("source_url must be an HTTPS bilibili.com URL")
    bvid = require_string(data.get("bvid"), "bvid")
    if not BVID_RE.fullmatch(bvid):
        raise DownloadError("manifest bvid is invalid")
    return Manifest(
        path=path,
        source_url=source_url,
        bvid=bvid,
        title=require_string(data.get("title"), "title"),
        duration_ms=require_int(data.get("duration_ms"), "duration_ms", 1),
        video=parse_media(data.get("video"), "video", allow_http_localhost),
        audio=parse_media(data.get("audio"), "audio", allow_http_localhost),
    )


def parse_content_range(value: Optional[str]) -> Tuple[int, int, int]:
    match = CONTENT_RANGE_RE.fullmatch(value or "")
    if not match:
        raise DownloadError("CDN response has an invalid Content-Range header")
    values = tuple(int(item) for item in match.groups())
    return values[0], values[1], values[2]


def request_headers(source_url: str, start: int, end: int) -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Range": f"bytes={start}-{end}",
        "Referer": source_url,
        "User-Agent": USER_AGENT,
    }


def open_range(url: str, source_url: str, start: int, end: int):
    request = urllib.request.Request(
        url, headers=request_headers(source_url, start, end), method="GET"
    )
    return urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS)


def probe_candidate(
    spec: MediaSpec,
    url: str,
    source_url: str,
    attempts: int,
) -> Tuple[Optional[int], str]:
    for attempt in range(1, attempts + 1):
        try:
            with open_range(url, source_url, 0, 0) as response:
                if response.status != 206:
                    return None, f"HTTP {response.status} did not honor byte ranges"
                start, end, candidate_total = parse_content_range(
                    response.headers.get("Content-Range")
                )
                if (start, end) != (0, 0) or candidate_total <= 0:
                    return None, "CDN probe returned the wrong byte range"
                return candidate_total, ""
        except urllib.error.HTTPError as exc:
            reason = transient_error_reason(exc)
            if reason is None:
                return None, f"HTTP {exc.code} during CDN probe"
            if attempt == attempts:
                return None, f"{reason} exhausted {attempts} probe attempts"
            wait_before_retry(spec.label, "probe", attempt + 1, attempts, exc)
        except (
            urllib.error.URLError,
            TimeoutError,
            ssl.SSLError,
            ConnectionError,
            http.client.IncompleteRead,
        ) as exc:
            if attempt == attempts:
                reason = transient_error_reason(exc) or "network_error"
                return None, f"{reason} exhausted {attempts} probe attempts"
            wait_before_retry(spec.label, "probe", attempt + 1, attempts, exc)
        except DownloadError as exc:
            return None, str(exc)
    return None, "no candidate responded"


def probe_candidates(
    spec: MediaSpec,
    source_url: str,
    attempts: int = DEFAULT_NETWORK_ATTEMPTS,
) -> Tuple[int, Tuple[str, ...]]:
    total: Optional[int] = None
    usable: List[str] = []
    last_reason = "no candidate responded"
    attempts = max(1, attempts)
    for url in spec.urls:
        candidate_total, reason = probe_candidate(spec, url, source_url, attempts)
        if candidate_total is None:
            last_reason = reason
            continue
        if total is None:
            total = candidate_total
        if candidate_total == total:
            usable.append(url)
        else:
            last_reason = "backup CDN reported a different resource size"
    if total is None or not usable:
        raise DownloadError(f"{spec.label} range probe failed: {last_reason}")
    return total, tuple(usable)


def split_ranges(total: int, count: int) -> List[ByteRange]:
    actual_count = max(1, min(count, total))
    chunk = (total + actual_count - 1) // actual_count
    ranges: List[ByteRange] = []
    for index in range(actual_count):
        start = index * chunk
        if start >= total:
            break
        ranges.append(ByteRange(index, start, min(total - 1, start + chunk - 1)))
    return ranges


def download_part(
    byte_range: ByteRange,
    part_path: Path,
    urls: Sequence[str],
    source_url: str,
    total: int,
    retries: int,
) -> None:
    if part_path.exists() and part_path.stat().st_size > byte_range.size:
        raise DownloadError(f"range part {byte_range.index} is larger than expected")
    partial_path = part_path.with_suffix(".partial")
    if part_path.exists() and part_path.stat().st_size == byte_range.size:
        if partial_path.exists():
            partial_path.unlink()
        return
    if part_path.exists():
        part_path.unlink()
    if partial_path.exists():
        partial_path.unlink()
    attempts = max(1, retries)
    last_reason = "no candidate responded"
    for url in urls:
        for attempt in range(1, attempts + 1):
            if partial_path.exists():
                partial_path.unlink()
            try:
                with open_range(
                    url, source_url, byte_range.start, byte_range.end
                ) as response:
                    if response.status != 206:
                        last_reason = (
                            f"HTTP {response.status} did not honor byte ranges"
                        )
                        break
                    start, end, response_total = parse_content_range(
                        response.headers.get("Content-Range")
                    )
                    if (
                        start != byte_range.start
                        or end != byte_range.end
                        or response_total != total
                    ):
                        last_reason = "CDN returned the wrong byte range"
                        break
                    with partial_path.open("wb") as output:
                        while True:
                            block = response.read(COPY_BUFFER_SIZE)
                            if not block:
                                break
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
            except urllib.error.HTTPError as exc:
                if partial_path.exists():
                    partial_path.unlink()
                reason = transient_error_reason(exc)
                if reason is None:
                    last_reason = f"HTTP {exc.code} during range download"
                    break
                last_reason = f"{reason} exhausted {attempts} download attempts"
                if attempt == attempts:
                    break
                wait_before_retry(
                    f"range_{byte_range.index}",
                    "download",
                    attempt + 1,
                    attempts,
                    exc,
                )
                continue
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                ConnectionError,
                http.client.IncompleteRead,
            ) as exc:
                if partial_path.exists():
                    partial_path.unlink()
                reason = transient_error_reason(exc) or "network_error"
                last_reason = f"{reason} exhausted {attempts} download attempts"
                if attempt == attempts:
                    break
                wait_before_retry(
                    f"range_{byte_range.index}",
                    "download",
                    attempt + 1,
                    attempts,
                    exc,
                )
                continue
            except OSError as exc:
                if partial_path.exists():
                    partial_path.unlink()
                raise DownloadError(
                    f"cannot write range part {byte_range.index}: {exc}"
                ) from exc
            except DownloadError as exc:
                if partial_path.exists():
                    partial_path.unlink()
                last_reason = str(exc)
                break

            current_size = partial_path.stat().st_size if partial_path.exists() else 0
            if current_size == byte_range.size:
                os.replace(partial_path, part_path)
                return
            if current_size > byte_range.size:
                raise DownloadError(
                    f"range part {byte_range.index} exceeded its byte range"
                )
            if partial_path.exists():
                partial_path.unlink()
            last_reason = "response stopped before the expected byte count"
            if attempt == attempts:
                break
            incomplete = http.client.IncompleteRead(b"", byte_range.size)
            wait_before_retry(
                f"range_{byte_range.index}",
                "download",
                attempt + 1,
                attempts,
                incomplete,
            )
    raise DownloadError(
        f"range part {byte_range.index} failed: {last_reason}"
    )


def progress_reporter(
    label: str, part_paths: Sequence[Path], total: int, stop: threading.Event
) -> None:
    while not stop.wait(15):
        downloaded = 0
        for path in part_paths:
            if path.exists():
                downloaded += path.stat().st_size
                continue
            partial_path = path.with_suffix(".partial")
            if partial_path.exists():
                downloaded += partial_path.stat().st_size
        log(f"{label}_progress bytes={downloaded} total={total}")


def assemble_parts(
    part_paths: Sequence[Path], assembled_path: Path, expected_total: int
) -> None:
    if assembled_path.exists() and assembled_path.stat().st_size == expected_total:
        return
    temporary = assembled_path.with_suffix(assembled_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with temporary.open("wb") as output:
        for part_path in part_paths:
            with part_path.open("rb") as source:
                shutil.copyfileobj(source, output, COPY_BUFFER_SIZE)
        output.flush()
        os.fsync(output.fileno())
    if temporary.stat().st_size != expected_total:
        raise DownloadError("assembled media size does not match the CDN resource size")
    os.replace(temporary, assembled_path)


def download_resource(
    spec: MediaSpec,
    source_url: str,
    work_dir: Path,
    connections: int,
    retries: int,
) -> Path:
    total, urls = probe_candidates(spec, source_url, retries)
    ranges = split_ranges(total, connections)
    resource_dir = work_dir / f"{spec.label}-{total}-{len(ranges)}"
    resource_dir.mkdir(parents=True, exist_ok=True)
    part_paths = [resource_dir / f"part-{item.index:03d}" for item in ranges]
    log(
        f"{spec.label}_download_start bytes={total} parts={len(ranges)} candidates={len(urls)}"
    )
    stop = threading.Event()
    reporter = threading.Thread(
        target=progress_reporter,
        args=(spec.label, part_paths, total, stop),
        daemon=True,
    )
    reporter.start()
    errors: List[BaseException] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [
                executor.submit(
                    download_part,
                    item,
                    part_path,
                    urls,
                    source_url,
                    total,
                    retries,
                )
                for item, part_path in zip(ranges, part_paths)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
    finally:
        stop.set()
        reporter.join()
    if errors:
        first = errors[0]
        if isinstance(first, DownloadError):
            raise first
        raise DownloadError(f"{spec.label} download failed")
    actual_total = sum(path.stat().st_size for path in part_paths)
    if actual_total != total:
        raise DownloadError(
            f"{spec.label} parts total {actual_total} bytes; expected {total}"
        )
    assembled = work_dir / f"{spec.label}.m4s"
    assemble_parts(part_paths, assembled, total)
    log(f"{spec.label}_download_complete bytes={total}")
    return assembled


def preflight_external_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run(
                [tool, "-version"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=EXTERNAL_TOOL_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise DownloadError(
                f"{tool} is unavailable or failed its startup check. "
                f"Install or repair it with: {EXTERNAL_TOOL_INSTALL_COMMAND}"
            ) from exc
        if result.returncode != 0:
            raise DownloadError(
                f"{tool} -version exited with status {result.returncode}. "
                f"Install or repair it with: {EXTERNAL_TOOL_INSTALL_COMMAND}"
            )


def run_command(
    command: Sequence[str], stage: str, strict_stderr: bool = False
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if strict_stderr and result.stderr.strip():
            detail = result.stderr.strip()[-2000:]
            raise DownloadError(f"{stage} reported media errors: {detail}")
        return result
    except FileNotFoundError as exc:
        raise DownloadError(f"{command[0]} is required for {stage}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-2000:]
        raise DownloadError(f"{stage} failed: {detail or 'no diagnostic output'}") from exc


def probe_file(path: Path) -> Dict[str, Any]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        "ffprobe validation",
        strict_stderr=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError("ffprobe returned invalid JSON") from exc


def media_duration(probe: Dict[str, Any]) -> float:
    try:
        return float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DownloadError("final MP4 has no numeric duration") from exc


def packet_stats(path: Path) -> Dict[int, Tuple[int, int]]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,size",
            "-of",
            "csv=p=0",
            str(path),
        ],
        "packet validation",
        strict_stderr=True,
    )
    stats: Dict[int, Tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        values = line.split(",")
        if len(values) != 2:
            raise DownloadError("ffprobe returned an invalid packet row")
        try:
            stream_index, packet_size = (int(item) for item in values)
        except ValueError as exc:
            raise DownloadError("ffprobe returned non-numeric packet data") from exc
        count, total_bytes = stats.get(stream_index, (0, 0))
        stats[stream_index] = (count + 1, total_bytes + packet_size)
    if not stats:
        raise DownloadError("media contains no readable packets")
    return stats


def verify_sample_frames(path: Path, duration: float) -> None:
    seeks = sorted(
        set((0.0, duration * 0.25, duration * 0.5, duration * 0.75, max(0.0, duration - 10)))
    )
    for seek in seeks:
        result = run_command(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{seek:.3f}",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-f",
                "framemd5",
                "-",
            ],
            f"frame decode at {seek:.3f}s",
            strict_stderr=True,
        )
        frame_rows = [
            line for line in result.stdout.splitlines() if line and not line.startswith("#")
        ]
        if not frame_rows:
            raise DownloadError(f"frame decode at {seek:.3f}s produced no frame")


def verify_final(
    path: Path,
    manifest: Manifest,
    expected_packets: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Dict[str, Any]:
    probe = probe_file(path)
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise DownloadError("final MP4 has no stream list")
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise DownloadError("final MP4 must contain exactly one video and one audio stream")
    video = videos[0]
    if manifest.video.width and video.get("width") != manifest.video.width:
        raise DownloadError("final MP4 width differs from the selected video stream")
    if manifest.video.height and video.get("height") != manifest.video.height:
        raise DownloadError("final MP4 height differs from the selected video stream")
    duration = media_duration(probe)
    expected_duration = manifest.duration_ms / 1000
    if abs(duration - expected_duration) > 2:
        raise DownloadError(
            f"final MP4 duration {duration:.3f}s differs from expected {expected_duration:.3f}s"
        )
    final_packets = packet_stats(path)
    video_index = int(video["index"])
    audio_index = int(audios[0]["index"])
    if expected_packets is not None:
        if final_packets.get(video_index) != expected_packets["video"]:
            raise DownloadError(
                "final MP4 video packet count or payload bytes differ from the downloaded stream"
            )
        if final_packets.get(audio_index) != expected_packets["audio"]:
            raise DownloadError(
                "final MP4 audio packet count or payload bytes differ from the downloaded stream"
            )
    run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        "full MP4 demux scan",
        strict_stderr=True,
    )
    verify_sample_frames(path, duration)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(COPY_BUFFER_SIZE)
            if not block:
                break
            digest.update(block)
    return {
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "duration_seconds": duration,
        "sha256": digest.hexdigest(),
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
        },
        "audio": {"codec": audios[0].get("codec_name")},
    }


def mux(video_path: Path, audio_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{final_path.stem}.", suffix=".tmp.mp4", dir=str(final_path.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        run_command(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            "MP4 mux",
        )
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise DownloadError("MP4 mux produced an empty file")
        os.replace(temporary, final_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def wipe_manifest(path: Path) -> None:
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        log("warning: unable to remove the temporary media manifest")


def output_path(output_dir: Path, manifest: Manifest) -> Path:
    height = manifest.video.height
    suffix = f"-{height}p" if height else ""
    return output_dir / manifest.bvid / f"{manifest.bvid}{suffix}.mp4"


def execute(args: argparse.Namespace) -> Dict[str, Any]:
    manifest = load_manifest(args.manifest, args.allow_http_localhost)
    final_path = output_path(args.output_dir, manifest)
    if final_path.exists():
        result = verify_final(final_path, manifest)
        result.update({"status": "existing_verified", "bvid": manifest.bvid})
        return result
    work_dir = final_path.parent / f".{manifest.bvid}.download"
    work_dir.mkdir(parents=True, exist_ok=True)
    video_path = download_resource(
        manifest.video,
        manifest.source_url,
        work_dir,
        args.connections,
        args.retries,
    )
    audio_path = download_resource(
        manifest.audio,
        manifest.source_url,
        work_dir,
        args.connections,
        args.retries,
    )
    video_packets = packet_stats(video_path)
    audio_packets = packet_stats(audio_path)
    if len(video_packets) != 1 or len(audio_packets) != 1:
        raise DownloadError("downloaded DASH resources must each contain one stream")
    expected_packets = {
        "video": next(iter(video_packets.values())),
        "audio": next(iter(audio_packets.values())),
    }
    mux(video_path, audio_path, final_path)
    try:
        result = verify_final(final_path, manifest, expected_packets)
    except BaseException:
        failed_path = final_path.with_suffix(".failed.mp4")
        os.replace(final_path, failed_path)
        raise
    shutil.rmtree(work_dir)
    result.update({"status": "downloaded", "bvid": manifest.bvid})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and verify authorized Bilibili DASH media"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("downloads"))
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_NETWORK_ATTEMPTS,
        help="total attempts per CDN candidate, including the first request",
    )
    parser.add_argument("--consume-manifest", action="store_true")
    parser.add_argument("--allow-http-localhost", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.connections <= 32:
        print("ERROR: --connections must be between 1 and 32", file=sys.stderr)
        return 2
    if not 1 <= args.retries <= 10:
        print("ERROR: --retries must be between 1 and 10", file=sys.stderr)
        return 2
    try:
        preflight_external_tools()
        result = execute(args)
        log(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except DownloadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except BaseException as exc:
        print(f"ERROR: unexpected {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if args.consume_manifest:
            wipe_manifest(args.manifest)


if __name__ == "__main__":
    from _runtime_bootstrap import ensure

    ensure()
    raise SystemExit(main())
