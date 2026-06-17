import os
import transcribe as T


def test_task_dir_name_uses_label():
    # label sanitized & truncated to 10 code points (11-char input loses the last char)
    name = T.task_dir_name("20260618-101010", label="会议记录超过十个字也截断", model="scribe-v2")
    assert name == "20260618-101010-会议记录超过十个字也"


def test_task_dir_name_falls_back_to_model_when_label_empty():
    # empty label -> model name, sanitized & truncated to 10: "cohere-transcribe" -> "cohere-tra"
    assert T.task_dir_name("20260618-101010", "", "cohere-transcribe") == "20260618-101010-cohere-tra"


def test_subfolder_name_priority():
    assert T.subfolder_name(0, audio_label="meeting", url="http://x/a.mp3") == "meeting"
    assert T.subfolder_name(0, audio_label=None, url="http://x/Inter view.mp3") == "Inter_view"
    # empty/host-only URL -> no usable stem -> indexed fallback
    assert T.subfolder_name(2, audio_label=None, url="") == "audio-03"


def test_write_outputs_creates_files(tmp_path):
    from adapters import NormalizedResult, Word
    nr = NormalizedResult("hello world", "en", 0.9,
                          [Word("hello", 0.0, 0.5, None), Word(" world", 0.5, 1.0, None)],
                          [], False, True)
    raw = {"id": "task-x", "model": "scribe-v2", "status": "completed", "results": [{"text": "hello world"}]}
    T.write_outputs(str(tmp_path), "scribe-v2", [nr], raw, {"srt", "vtt", "md"})
    names = set(os.listdir(tmp_path))
    assert {"result.json", "transcript.txt", "transcript.md", "transcript.srt", "transcript.vtt"} <= names
    assert "hello world" in open(tmp_path / "transcript.txt", encoding="utf-8").read()
