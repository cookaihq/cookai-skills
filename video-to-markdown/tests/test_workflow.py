"""User-visible workflow tests with real media and persistent task state."""

import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib
from threading import Thread

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / "scripts" / "workflow.py"


@contextmanager
def video_server(video, *, content_length=None, responses=()):
    """A loopback HTTP origin; the workflow uses its real download path."""
    payload = video.read_bytes()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_index = len(requests)
            requests.append((self.path, time.monotonic()))
            status, headers = responses[request_index] if request_index < len(responses) else (200, {})
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload) if content_length is None else content_length))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:%d/video.mp4" % server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def run_workflow(cwd, *arguments, expected=0, environment=None, error=None):
    env = os.environ.copy()
    if environment:
        env.update(environment)
    result = subprocess.run(
        [sys.executable, str(WORKFLOW), *map(str, arguments)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in result.stderr
    if error:
        assert error in result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.fixture
def video(tmp_path):
    ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for real-media workflow tests")
    target = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target


def test_subtitled_video_can_start_and_resume_with_real_state(tmp_path, video):
    subtitle = tmp_path / "speech.srt"
    subtitle.write_text(
        "1\n00:00:00,200 --> 00:00:01,500\nAn existing transcript.\n",
        encoding="utf-8",
    )
    started = run_workflow(tmp_path, "start", "--video", video, "--subtitle", subtitle)
    task_dir = Path(started["task_dir"])
    declared_version = tomllib.loads((WORKFLOW.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))["skill_version"] == declared_version
    original = (task_dir / "transcript.json").read_bytes()
    resumed = run_workflow(tmp_path, "resume", "--task-dir", task_dir)
    assert resumed["stage"] == "observation_in_progress"
    assert resumed["next_action"] == "inspect_batch"
    assert (task_dir / "transcript.json").read_bytes() == original
    assert json.loads(original)["segments"][0]["text"] == "An existing transcript."


def test_entrypoint_recovers_the_skill_interpreter_from_a_base_python(tmp_path):
    result = subprocess.run(
        [sys._base_executable, str(WORKFLOW), "--help"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "resume" in result.stdout


def test_entrypoint_repairs_missing_dependencies_in_its_own_environment(tmp_path):
    installation = tmp_path / "test-skill-install"
    installation.mkdir()
    shutil.copytree(WORKFLOW.parent, installation / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    for filename in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(WORKFLOW.parents[1] / filename, installation / filename)
    environment = os.environ.copy()
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("VIDEO_TO_MARKDOWN_BOOTSTRAP_REEXEC", None)
    subprocess.run(
        ["uv", "venv", "--python", sys._base_executable, str(installation / ".venv")],
        env=environment, check=True, capture_output=True, timeout=60,
    )
    interpreter = installation / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(interpreter), str(installation / "scripts" / "workflow.py"), "--help"]
    repaired = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert "rebuilding from uv.lock" in repaired.stderr
    assert "resume" in repaired.stdout
    repeated = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "rebuilding from uv.lock" not in repeated.stderr


def test_missing_private_state_has_an_actionable_error(tmp_path):
    run_workflow(tmp_path, "resume", "--task-dir", tmp_path / "missing-task", expected=1)


def test_missing_subtitle_remains_the_input_after_resume(tmp_path, video):
    subtitle = tmp_path / "later.srt"
    run_workflow(tmp_path, "start", "--video", video, "--subtitle", subtitle, expected=1)
    task_dir = next((tmp_path / "video-to-markdown-output").iterdir())
    invalid = run_workflow(tmp_path, "validate", "--task-dir", task_dir, expected=1)
    assert invalid["complete"] is False
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nRecovered subtitles.\n", encoding="utf-8")
    resumed = run_workflow(
        tmp_path, "resume", "--task-dir", task_dir,
        environment={"AIHUB_API_KEY": "", "X_API_KEY": ""},
    )
    assert resumed["stage"] == "observation_in_progress"
    result = json.loads((task_dir / "transcript.json").read_text(encoding="utf-8"))
    assert result["segments"][0]["text"] == "Recovered subtitles."


def test_resume_rejects_changed_audio_before_using_a_transcription_service(tmp_path, video):
    without_credentials = {"AIHUB_API_KEY": "", "X_API_KEY": ""}
    run_workflow(tmp_path, "start", "--video", video, environment=without_credentials)
    task_dir = next((tmp_path / "video-to-markdown-output").iterdir())
    audio = task_dir / "source" / "audio-for-asr.mp3"
    with audio.open("ab") as output:
        output.write(b"changed-after-preparation")
    run_workflow(
        tmp_path, "resume", "--task-dir", task_dir, expected=1,
        environment=without_credentials, error="prepared audio changed",
    )


def test_signed_video_url_is_kept_only_in_private_state(tmp_path, video):
    subtitle = tmp_path / "speech.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nDownloaded video.\n", encoding="utf-8")
    with video_server(video) as (url, requests):
        signed_url = url + "?token=synthetic-secret-5473#private-fragment"
        started = run_workflow(tmp_path, "start", "--video", signed_url, "--subtitle", subtitle)
    assert len(requests) == 1
    task_dir = Path(started["task_dir"])
    public = (task_dir / "manifest.json").read_text(encoding="utf-8")
    assert "synthetic-secret-5473" not in public
    assert "private-fragment" not in public
    private = json.loads((task_dir / ".state" / "private.json").read_text(encoding="utf-8"))
    assert private["source_origin"] == signed_url
    assert run_workflow(tmp_path, "resume", "--task-dir", task_dir)["stage"] == "observation_in_progress"


def test_download_rejects_truncated_http_body_after_bounded_retries(tmp_path, video):
    subtitle = tmp_path / "speech.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nDownloaded video.\n", encoding="utf-8")
    with video_server(video, content_length=video.stat().st_size + 64) as (url, requests):
        run_workflow(
            tmp_path, "start", "--video", url, "--subtitle", subtitle,
            expected=1, error="incomplete response body",
        )
    assert len(requests) == 3
    task_dir = next((tmp_path / "video-to-markdown-output").iterdir())
    assert not (task_dir / "source" / "video.mp4").exists()
    assert not (task_dir / "transcript.json").exists()


def test_download_observes_server_retry_after_before_retrying(tmp_path, video):
    subtitle = tmp_path / "speech.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nDownloaded video.\n", encoding="utf-8")
    with video_server(video, responses=[(429, {"Retry-After": "2"})]) as (url, requests):
        started = run_workflow(tmp_path, "start", "--video", url, "--subtitle", subtitle)
    assert started["stage"] == "observation_in_progress"
    assert len(requests) == 2
    assert requests[1][1] - requests[0][1] >= 2


@pytest.mark.skipif(os.name != "nt", reason="Windows private-state ACL contract")
def test_resume_rejects_a_private_directory_that_inherits_permissions(tmp_path, video):
    import win32security

    subtitle = tmp_path / "speech.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nPrivate state.\n", encoding="utf-8")
    started = run_workflow(tmp_path, "start", "--video", video, "--subtitle", subtitle)
    task_dir = Path(started["task_dir"])
    private_dir = task_dir / ".state"
    descriptor = win32security.GetFileSecurity(str(private_dir), win32security.DACL_SECURITY_INFORMATION)
    win32security.SetNamedSecurityInfo(
        str(private_dir), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        | win32security.UNPROTECTED_DACL_SECURITY_INFORMATION,
        None, None, descriptor.GetSecurityDescriptorDacl(), None,
    )
    run_workflow(tmp_path, "resume", "--task-dir", task_dir, expected=1)
