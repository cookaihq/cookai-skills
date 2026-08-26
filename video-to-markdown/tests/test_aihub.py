import socket
from pathlib import Path

import pytest

from video_to_markdown import aihub, state


def response(status, document=None, headers=None):
    return aihub.Response(status, document, "", headers or {})


def test_create_body_is_model_specific():
    assert aihub.create_body("paraformer-v2", "https://x/audio.flac") == {
        "model": "paraformer-v2",
        "file_urls": ["https://x/audio.flac"],
    }
    assert aihub.create_body("scribe-v2", "https://x/audio.flac") == {
        "model": "scribe-v2",
        "audio_url": "https://x/audio.flac",
    }


@pytest.mark.parametrize(
    ("model", "language"),
    [
        ("paraformer-v2", "ar"),
        ("paraformer-8k-v2", "en"),
        ("scribe-v2", "zh-CN"),
        ("cohere-transcribe", "hi"),
    ],
)
def test_create_body_rejects_unsupported_language_before_task_creation(model, language):
    with pytest.raises(aihub.AIHubError):
        aihub.create_body(model, "https://x/audio.flac", language)


def test_create_body_normalizes_supported_language_codes():
    assert aihub.create_body("paraformer-v2", "https://x/audio.flac", " EN ") == {
        "model": "paraformer-v2",
        "file_urls": ["https://x/audio.flac"],
        "language_hints": ["en"],
    }
    assert aihub.create_body("cohere-transcribe", "https://x/audio.flac", "ZH") == {
        "model": "cohere-transcribe",
        "audio_url": "https://x/audio.flac",
        "language": "zh",
    }


def test_write_retries_connection_refused_but_not_timeout():
    calls = []

    def refused_then_ok():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionRefusedError("refused")
        return response(200, {"id": "ok"})

    assert aihub.run_write(refused_then_ok, operation="write", sleep=lambda _: None).status == 200
    assert len(calls) == 2

    timeout_calls = []

    def timeout():
        timeout_calls.append(1)
        raise socket.timeout("timed out")

    with pytest.raises(aihub.AmbiguousWrite):
        aihub.run_write(timeout, operation="write", sleep=lambda _: None)
    assert len(timeout_calls) == 1


def test_write_retries_documented_rate_limit():
    calls = []

    def limited():
        calls.append(1)
        if len(calls) == 1:
            return response(429, {}, {"Retry-After": "0"})
        return response(200, {"id": "ok"})

    assert aihub.run_write(limited, operation="write", sleep=lambda _: None).status == 200
    assert len(calls) == 2


def test_poll_task_stops_at_wall_clock_budget():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def pending(method, url, headers, body, timeout):
        return response(200, {"id": "task-1", "status": "processing"})

    with pytest.raises(aihub.AIHubError, match="exceeded 5 seconds"):
        aihub.poll_task(
            "task-1",
            "secret",
            transport=pending,
            sleep=sleep,
            clock=clock,
            budget_seconds=5,
            interval_seconds=3,
        )
    assert sleeps == [3, 2]
    assert now[0] == 5


def test_resume_reuses_upload_and_task_id(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    manifest, private = state.initialize_task(
        task_dir,
        settings_snapshot={"asr_model": "scribe-v2", "sources": {}},
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_hash, source_size = state.sha256_file(source)
    manifest["source"] = {
        "physical_path": str(source),
        "sha256": source_hash,
        "size_bytes": source_size,
        "duration_ms": 1000,
    }
    state.save_manifest(task_dir, manifest)
    audio = task_dir / "source" / "audio-for-asr.flac"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    counts = {"upload": 0, "create": 0, "poll": 0}

    def upload_transport(url, headers, body, timeout):
        counts["upload"] += 1
        return response(200, {"id": "file-1", "url": "https://cdn/audio.flac"})

    def json_transport(method, url, headers, body, timeout):
        if method == "POST":
            counts["create"] += 1
            return response(200, {"id": "task-1", "status": "pending"})
        counts["poll"] += 1
        return response(
            200,
            {"id": "task-1", "status": "completed", "results": [{"text": "done"}]},
        )

    aihub.run_asr(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        audio_path=audio,
        api_key="secret",
        language=None,
        upload_transport=upload_transport,
        json_transport=json_transport,
        sleep=lambda _: None,
    )
    aihub.run_asr(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        audio_path=audio,
        api_key="secret",
        language=None,
        upload_transport=upload_transport,
        json_transport=json_transport,
        sleep=lambda _: None,
    )
    assert counts == {"upload": 1, "create": 1, "poll": 1}
    persisted_manifest = state.read_json(task_dir / "manifest.json")
    persisted_private = state.read_json(task_dir / ".state" / "private.json")
    assert persisted_manifest["asr"]["task_id"] == "task-1"
    assert "temporary_url" not in persisted_manifest["asr"]
    assert persisted_private["upload"]["temporary_url"] == "https://cdn/audio.flac"
    assert persisted_private["asr_terminal"]["response"]["status"] == "completed"


def test_paraformer_fetches_and_persists_transcription_before_normalizing(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    manifest, private = state.initialize_task(
        task_dir,
        settings_snapshot={"asr_model": "paraformer-v2", "sources": {}},
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_hash, source_size = state.sha256_file(source)
    manifest["source"] = {
        "physical_path": str(source),
        "sha256": source_hash,
        "size_bytes": source_size,
        "duration_ms": 6000,
    }
    audio = task_dir / "source" / "audio-for-asr.flac"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    def upload_transport(url, headers, body, timeout):
        return response(200, {"id": "file-1", "url": "https://cdn/audio.flac"})

    def json_transport(method, url, headers, body, timeout):
        if method == "POST":
            return response(200, {"id": "task-1", "status": "pending"})
        return response(
            200,
            {
                "id": "task-1",
                "status": "completed",
                "results": [
                    {
                        "file_url": "https://cdn/audio.flac",
                        "output": {
                            "transcription_url": "https://cdn/transcript.json"
                        },
                    }
                ],
            },
        )

    fetches = []

    def transcription_transport(url, timeout):
        fetches.append(url)
        return response(
            200,
            {
                "file_url": "https://cdn/audio.flac",
                "transcripts": [
                    {
                        "text": "Hello world.",
                        "sentences": [
                            {
                                "begin_time": 125,
                                "end_time": 987,
                                "text": "Hello world.",
                            }
                        ],
                    }
                ],
            },
        )

    normalized = aihub.run_asr(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        audio_path=audio,
        api_key="secret",
        language="en",
        upload_transport=upload_transport,
        json_transport=json_transport,
        transcription_transport=transcription_transport,
        sleep=lambda _: None,
    )

    assert fetches == ["https://cdn/transcript.json"]
    assert normalized["segments"][0]["raw_start"] == 125
    assert normalized["segments"][0]["start_ms"] == 125
    assert normalized["segments"][0]["timestamp_precision_ms"] == 1
    assert "https://cdn" not in (task_dir / "transcript.json").read_text(encoding="utf-8")
    persisted_private = state.read_json(task_dir / ".state" / "private.json")
    assert persisted_private["asr_transcription"]["state"] == "ready"
    assert (
        persisted_private["asr_transcription"]["response"]["transcripts"][0]["text"]
        == "Hello world."
    )
