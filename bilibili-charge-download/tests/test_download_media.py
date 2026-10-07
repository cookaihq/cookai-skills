#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import datetime
import email.utils
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.error
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit


SKILL_DIR = Path(__file__).resolve().parent.parent
DOWNLOADER = SKILL_DIR / "scripts" / "download_media.py"
RANGE_RE = re.compile(r"^bytes=(\d+)-(\d+)$")
TEST_BVID = "BV1TEST00000"
sys.path.insert(0, str(DOWNLOADER.parent))
import download_media  # noqa: E402


class RangeHandler(BaseHTTPRequestHandler):
    root: Path
    interrupted = False
    state_lock = threading.Lock()
    request_counts: dict[str, int] = defaultdict(int)
    scripted_responses: dict[str, list[tuple[int, str | None]]] = {}

    def do_GET(self) -> None:
        relative = urlsplit(self.path).path.lstrip("/")
        with self.state_lock:
            type(self).request_counts[relative] += 1
            scripted = type(self).scripted_responses.get(relative, [])
            response = scripted.pop(0) if scripted else None
        if response is not None:
            status, retry_after = response
            self.send_response(status)
            if retry_after is not None:
                self.send_header("Retry-After", retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target = (self.root / relative).resolve()
        if self.root not in target.parents or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        match = RANGE_RE.fullmatch(self.headers.get("Range", ""))
        if not match:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)
            return
        start, end = (int(item) for item in match.groups())
        if start < 0 or end < start or end >= len(data):
            self.send_error(416)
            return
        body = data[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        should_interrupt = False
        if relative == "media/video.mp4" and len(body) > 4096:
            with self.state_lock:
                if not self.interrupted:
                    type(self).interrupted = True
                    should_interrupt = True
        if should_interrupt:
            midpoint = len(body) // 2
            self.wfile.write(body[:midpoint])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class LocalRangeServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="bili-skill-test-")).resolve()
        self.media_dir = self.temp_dir / "media"
        self.media_dir.mkdir()
        self.payload_path = self.media_dir / "payload.bin"
        self.payload = bytes(range(256)) * 32
        self.payload_path.write_bytes(self.payload)
        RangeHandler.root = self.temp_dir
        RangeHandler.interrupted = False
        RangeHandler.request_counts = defaultdict(int)
        RangeHandler.scripted_responses = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        shutil.rmtree(self.temp_dir)


class NetworkRetryTest(LocalRangeServerTestCase):
    def media_spec(self, *relative_urls: str) -> download_media.MediaSpec:
        return download_media.MediaSpec(
            label="video",
            urls=tuple(f"{self.base_url}/{item}?signed=do-not-log" for item in relative_urls),
            stream_id=80,
            codecs="avc1.640032",
            bandwidth=100000,
            width=320,
            height=180,
        )

    def test_probe_retries_429_with_capped_retry_after_without_leaking_url(self) -> None:
        RangeHandler.scripted_responses["media/payload.bin"] = [(429, "999")]
        output = io.StringIO()
        with mock.patch.object(download_media.time, "sleep") as sleep:
            with contextlib.redirect_stdout(output):
                total, urls = download_media.probe_candidates(
                    self.media_spec("media/payload.bin"),
                    "https://www.bilibili.com/video/BV1TEST00000/",
                )

        self.assertEqual(total, len(self.payload))
        self.assertEqual(len(urls), 1)
        self.assertEqual(RangeHandler.request_counts["media/payload.bin"], 2)
        sleep.assert_called_once_with(60.0)
        retry_log = output.getvalue()
        self.assertIn("attempt=2/3", retry_log)
        self.assertIn("reason=http_429", retry_log)
        self.assertNotIn(self.base_url, retry_log)
        self.assertNotIn("do-not-log", retry_log)

    def test_probe_retries_5xx_with_exponential_backoff(self) -> None:
        RangeHandler.scripted_responses["media/payload.bin"] = [
            (500, None),
            (503, None),
        ]
        with mock.patch.object(download_media.time, "sleep") as sleep:
            total, urls = download_media.probe_candidates(
                self.media_spec("media/payload.bin"),
                "https://www.bilibili.com/video/BV1TEST00000/",
            )

        self.assertEqual(total, len(self.payload))
        self.assertEqual(len(urls), 1)
        self.assertEqual(RangeHandler.request_counts["media/payload.bin"], 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(2.0)])

    def test_non_finite_retry_after_uses_exponential_backoff(self) -> None:
        RangeHandler.scripted_responses["media/payload.bin"] = [(429, "nan")]
        with mock.patch.object(download_media.time, "sleep") as sleep:
            download_media.probe_candidates(
                self.media_spec("media/payload.bin"),
                "https://www.bilibili.com/video/BV1TEST00000/",
            )

        sleep.assert_called_once_with(1.0)

    def test_http_date_retry_after_is_followed_and_capped(self) -> None:
        now = datetime.datetime(2026, 8, 25, tzinfo=datetime.timezone.utc)
        retry_at = email.utils.format_datetime(now + datetime.timedelta(seconds=90))
        error = urllib.error.HTTPError(
            self.base_url,
            429,
            "rate limited",
            {"Retry-After": retry_at},
            None,
        )

        with mock.patch.object(download_media.time, "time", return_value=now.timestamp()):
            delay = download_media.retry_delay_seconds(error, 1)

        self.assertEqual(delay, 60.0)

    def test_probe_retries_url_error(self) -> None:
        real_open_range = download_media.open_range
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError("temporary failure")
            return real_open_range(*args, **kwargs)

        with mock.patch.object(download_media, "open_range", side_effect=fail_once):
            with mock.patch.object(download_media.time, "sleep") as sleep:
                total, urls = download_media.probe_candidates(
                    self.media_spec("media/payload.bin"),
                    "https://www.bilibili.com/video/BV1TEST00000/",
                )

        self.assertEqual(total, len(self.payload))
        self.assertEqual(len(urls), 1)
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(1.0)

    def test_deterministic_404_is_not_retried_without_backup(self) -> None:
        RangeHandler.scripted_responses["media/payload.bin"] = [(404, None)] * 4
        part_path = self.temp_dir / "part-000"
        with mock.patch.object(download_media.time, "sleep") as sleep:
            with self.assertRaises(download_media.DownloadError):
                download_media.download_part(
                    download_media.ByteRange(0, 0, len(self.payload) - 1),
                    part_path,
                    self.media_spec("media/payload.bin").urls,
                    "https://www.bilibili.com/video/BV1TEST00000/",
                    len(self.payload),
                    3,
                )

        self.assertEqual(RangeHandler.request_counts["media/payload.bin"], 1)
        sleep.assert_not_called()
        self.assertFalse(part_path.exists())

    def test_deterministic_404_falls_back_without_waiting(self) -> None:
        RangeHandler.scripted_responses["media/primary.bin"] = [(404, None)]
        (self.media_dir / "primary.bin").write_bytes(self.payload)
        part_path = self.temp_dir / "part-000"
        spec = self.media_spec("media/primary.bin", "media/payload.bin")
        with mock.patch.object(download_media.time, "sleep") as sleep:
            download_media.download_part(
                download_media.ByteRange(0, 0, len(self.payload) - 1),
                part_path,
                spec.urls,
                "https://www.bilibili.com/video/BV1TEST00000/",
                len(self.payload),
                3,
            )

        self.assertEqual(RangeHandler.request_counts["media/primary.bin"], 1)
        self.assertEqual(RangeHandler.request_counts["media/payload.bin"], 1)
        sleep.assert_not_called()
        self.assertEqual(part_path.read_bytes(), self.payload)


class DownloadMediaE2ETest(LocalRangeServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.video_path = self.media_dir / "video.mp4"
        self.audio_path = self.media_dir / "audio.m4a"
        self._create_media()

    def _create_media(self) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=24",
                "-t",
                "2",
                "-an",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-movflags",
                "+frag_keyframe+empty_moov",
                str(self.video_path),
            ],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000",
                "-t",
                "2",
                "-vn",
                "-c:a",
                "aac",
                "-movflags",
                "+frag_keyframe+empty_moov",
                str(self.audio_path),
            ],
            check=True,
            timeout=30,
        )

    def test_parallel_resume_backup_mux_verify_and_manifest_cleanup(self) -> None:
        base = self.base_url
        manifest_path = self.temp_dir / "manifest.json"
        manifest = {
            "schema_version": 1,
            "source_url": f"https://www.bilibili.com/video/{TEST_BVID}/",
            "bvid": TEST_BVID,
            "title": "range test",
            "duration_ms": 2000,
            "video": {
                "url": f"{base}/missing/video.mp4",
                "backup_urls": [f"{base}/media/video.mp4"],
                "id": 80,
                "codecs": "mp4v.20.9",
                "width": 320,
                "height": 180,
                "bandwidth": 100000,
            },
            "audio": {
                "url": f"{base}/media/audio.m4a",
                "backup_urls": [],
                "id": 30280,
                "codecs": "mp4a.40.2",
                "bandwidth": 64000,
            },
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        output_dir = self.temp_dir / "output"
        result = subprocess.run(
            [
                sys.executable,
                str(DOWNLOADER),
                "--manifest",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                "--connections",
                "4",
                "--consume-manifest",
                "--allow-http-localhost",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(manifest_path.exists())
        self.assertTrue(RangeHandler.interrupted, "test server did not interrupt a range")
        combined_output = result.stdout + result.stderr
        self.assertNotIn(base, combined_output)
        final_path = output_dir / TEST_BVID / f"{TEST_BVID}-180p.mp4"
        self.assertTrue(final_path.is_file())
        self.assertFalse((final_path.parent / f".{TEST_BVID}.download").exists())
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height",
                "-of",
                "json",
                str(final_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        streams = json.loads(probe.stdout)["streams"]
        self.assertEqual([item["codec_type"] for item in streams], ["video", "audio"])
        self.assertEqual(streams[0]["width"], 320)
        self.assertEqual(streams[0]["height"], 180)

        corrupt_output = self.temp_dir / "corrupt-output"
        corrupt_final = corrupt_output / TEST_BVID / f"{TEST_BVID}-180p.mp4"
        corrupt_final.parent.mkdir(parents=True)
        shutil.copy2(final_path, corrupt_final)
        with corrupt_final.open("r+b") as handle:
            midpoint = corrupt_final.stat().st_size // 2
            handle.seek(midpoint)
            handle.write(b"\0" * (corrupt_final.stat().st_size - midpoint))
        corrupt_manifest = self.temp_dir / "corrupt-manifest.json"
        corrupt_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(corrupt_manifest, 0o600)
        corrupt_result = subprocess.run(
            [
                sys.executable,
                str(DOWNLOADER),
                "--manifest",
                str(corrupt_manifest),
                "--output-dir",
                str(corrupt_output),
                "--consume-manifest",
                "--allow-http-localhost",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(corrupt_result.returncode, 0)
        self.assertFalse(corrupt_manifest.exists())

    def test_bilibili_akamai_hostname_is_narrowly_allowed(self) -> None:
        accepted = "https://upos-hz-mirrorakam.akamaized.net/media/video.m4s"
        self.assertEqual(download_media.validate_url(accepted, False), accepted)
        with self.assertRaises(download_media.DownloadError):
            download_media.validate_url(
                "https://unrelated-service.akamaized.net/media/video.m4s", False
            )


class RuntimePreflightTest(unittest.TestCase):
    def test_preflight_executes_ffmpeg_and_ffprobe_with_timeout(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="version", stderr="")
        with mock.patch.object(
            download_media.subprocess, "run", return_value=completed
        ) as run:
            download_media.preflight_external_tools()

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["ffmpeg", "-version"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                ),
                mock.call(
                    ["ffprobe", "-version"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                ),
            ],
        )

    def test_preflight_failure_includes_executable_install_command(self) -> None:
        with mock.patch.object(
            download_media.subprocess, "run", side_effect=FileNotFoundError
        ):
            with self.assertRaisesRegex(
                download_media.DownloadError, r"brew install ffmpeg"
            ):
                download_media.preflight_external_tools()

    def test_preflight_nonzero_exit_includes_executable_install_command(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="broken")
        with mock.patch.object(
            download_media.subprocess, "run", return_value=failed
        ):
            with self.assertRaisesRegex(
                download_media.DownloadError, r"brew install ffmpeg"
            ):
                download_media.preflight_external_tools()


class RuntimeBootstrapBoundaryTest(unittest.TestCase):
    def test_import_does_not_call_runtime_bootstrap(self) -> None:
        code = f"""
import sys
import types
sys.path.insert(0, {str(DOWNLOADER.parent)!r})
bootstrap = types.ModuleType('_runtime_bootstrap')
def ensure():
    raise RuntimeError('bootstrap ran during import')
bootstrap.ensure = ensure
sys.modules['_runtime_bootstrap'] = bootstrap
import download_media
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SkillVersionTest(unittest.TestCase):
    def test_pyproject_and_skill_frontmatter_versions_match(self) -> None:
        project_version = tomllib.loads(
            (SKILL_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = skill_text.split("---", 2)[1]
        version_match = re.search(r"(?m)^metadata:\s*\n  version:\s*\"([^\"\s]+)\"\s*$", frontmatter)
        description_match = re.search(
            r"(?m)^description:\s*(?:>-\s*\n\s*)?(v[0-9]+\.[0-9]+\.[0-9]+｜)",
            frontmatter,
        )

        self.assertIsNotNone(version_match)
        self.assertIsNotNone(description_match)
        self.assertEqual(version_match.group(1), project_version)
        self.assertEqual(description_match.group(1), f"v{project_version}｜")


if __name__ == "__main__":
    unittest.main(verbosity=2)
