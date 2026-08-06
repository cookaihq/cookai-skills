import pytest

import ask


def _models_ok(*a, **k):
    return [{"id": "gpt-5.5", "capabilities": ["text", "vision"]},
            {"id": "gemini-3.5-flash", "capabilities": ["text", "vision", "video", "audio", "file"]}], "k1"


def test_main_text_only_success(monkeypatch, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)
    monkeypatch.setattr(ask, "submit_llm", lambda body, keys, **k: ({"id": "task-llm-1"}, "k1"))
    monkeypatch.setattr(ask, "poll_task", lambda tid, key, **k: {
        "status": "completed", "results": [{"choices": [{"message": {"content": "hi there"}}]}]})
    code = ask.main(["--model", "gpt-5.5", "--prompt", "hello"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() == "hi there"
    assert "sk-abcd1234efgh" not in out.err


def test_main_no_key_returns_2(monkeypatch, tmp_path):
    monkeypatch.delenv("AIHUB_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = ask.main(["--model", "gpt-5.5", "--prompt", "hi"])
    assert code == 2


def test_main_no_prompt_no_media_returns_2(monkeypatch, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    code = ask.main(["--model", "gpt-5.5"])  # neither prompt nor media
    assert code == 2
    assert "至少" in capsys.readouterr().err


def test_main_capability_precheck_fails_returns_3_without_submit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)

    def _boom(*a, **k):
        raise AssertionError("submit_llm must NOT be called when precheck fails")

    monkeypatch.setattr(ask, "submit_llm", _boom)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    code = ask.main(["--model", "gpt-5.5", "--video", str(f)])  # gpt-5.5 lacks 'video'
    err = capsys.readouterr().err
    assert code == 3
    assert "video" in err
    assert "gemini-3.5-flash" in err


def test_main_local_media_uploaded_then_submitted(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)
    monkeypatch.setattr(ask, "upload_local_file", lambda path, keys, **k: "https://files/x.png")
    captured = {}

    def fake_submit(body, keys, **k):
        captured["body"] = body
        return {"id": "task-llm-2"}, "k1"

    monkeypatch.setattr(ask, "submit_llm", fake_submit)
    monkeypatch.setattr(ask, "poll_task", lambda tid, key, **k: {
        "status": "completed", "results": [{"choices": [{"message": {"content": "a cat"}}]}]})
    img = tmp_path / "x.png"
    img.write_bytes(b"PNG")
    code = ask.main(["--model", "gemini-3.5-flash", "--prompt", "what is this", "--image", str(img)])
    assert code == 0
    blocks = captured["body"]["messages"][-1]["content"]
    assert {"type": "image_url", "image_url": {"url": "https://files/x.png"}} in blocks


def test_main_failed_task_returns_1(monkeypatch, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)
    monkeypatch.setattr(ask, "submit_llm", lambda body, keys, **k: ({"id": "t"}, "k1"))
    monkeypatch.setattr(ask, "poll_task", lambda tid, key, **k: {
        "status": "failed", "error": {"code": "task_failed", "message": "boom"}})
    code = ask.main(["--model", "gpt-5.5", "--prompt", "hi"])
    assert code == 1
    assert "boom" in capsys.readouterr().err


def test_main_upload_failure_returns_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)

    def _raise(path, keys, **k):
        raise ask.UploadHelperError(413, "文件过大")

    monkeypatch.setattr(ask, "upload_local_file", _raise)
    img = tmp_path / "x.png"
    img.write_bytes(b"PNG")
    code = ask.main(["--model", "gemini-3.5-flash", "--prompt", "x", "--image", str(img)])
    assert code == 1
    assert "文件过大" in capsys.readouterr().err


def test_main_youtube_passthrough_normalized_no_upload(monkeypatch, capsys):
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")
    monkeypatch.setattr(ask, "fetch_models", _models_ok)

    def _no_upload(*a, **k):
        raise AssertionError("upload_local_file must NOT be called for a YouTube URL")

    monkeypatch.setattr(ask, "upload_local_file", _no_upload)
    captured = {}

    def fake_submit(body, keys, **k):
        captured["body"] = body
        return {"id": "t"}, "k1"

    monkeypatch.setattr(ask, "submit_llm", fake_submit)
    monkeypatch.setattr(ask, "poll_task", lambda tid, key, **k: {
        "status": "completed", "results": [{"choices": [{"message": {"content": "ok"}}]}]})
    code = ask.main(["--model", "gemini-3.5-flash", "--prompt", "概述",
                     "--video", "https://www.youtube.com/shorts/XY_z-12"])
    assert code == 0
    blocks = captured["body"]["messages"][-1]["content"]
    assert {"type": "video_url",
            "video_url": {"url": "https://www.youtube.com/watch?v=XY_z-12"}} in blocks


def test_main_precheck_urlerror_proceeds(monkeypatch, capsys):
    import urllib.error
    monkeypatch.setenv("AIHUB_API_KEY", "sk-abcd1234efgh")

    def _raise(*a, **k):
        raise urllib.error.URLError("net down")

    monkeypatch.setattr(ask, "fetch_models", _raise)
    monkeypatch.setattr(ask, "submit_llm", lambda body, keys, **k: ({"id": "t"}, "k1"))
    monkeypatch.setattr(ask, "poll_task", lambda tid, key, **k: {
        "status": "completed", "results": [{"choices": [{"message": {"content": "hi"}}]}]})
    code = ask.main(["--model", "gpt-5.5", "--prompt", "hi"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() == "hi"
    assert "预校验跳过" in out.err
