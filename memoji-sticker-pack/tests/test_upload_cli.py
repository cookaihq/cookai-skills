import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import upload  # noqa: E402
from client import Resp  # noqa: E402


def test_file_upload_cli_retries_401_and_prints_only_public_url(
    monkeypatch, tmp_path, capsys
):
    source = tmp_path / "portrait.png"
    source.write_bytes(b"PNGDATA")
    (tmp_path / ".env.local").write_text(
        "AIHUB_API_KEY=good-key-5678\n", encoding="utf-8"
    )
    monkeypatch.setenv("AIHUB_API_KEY", "bad-key-1234")
    monkeypatch.chdir(tmp_path)
    requests = []

    def fake_transport(method, url, headers, body=None, timeout=60):
        requests.append((method, url, headers, body))
        if headers["Authorization"] == "Bearer bad-key-1234":
            return Resp(401, None, "")
        return Resp(
            200,
            {"url": "https://files.example/portrait.png", "id": "f1", "size": 7},
            "",
        )

    monkeypatch.setattr(upload, "http_request", fake_transport)

    code = upload.main(
        ["--file", str(source), "--base-url", "https://api.example"]
    )

    output = capsys.readouterr()
    assert code == 0
    assert output.out.strip() == "https://files.example/portrait.png"
    assert "bad-key-1234" not in output.err
    assert "good-key-5678" not in output.err
    assert "good****5678" in output.err
    assert [request[2]["Authorization"] for request in requests] == [
        "Bearer bad-key-1234",
        "Bearer good-key-5678",
    ]
    assert requests[0][0:2] == (
        "POST",
        "https://api.example/v1/files/upload/stream",
    )
    assert b'filename="portrait.png"' in requests[0][3]
    assert b"PNGDATA" in requests[0][3]


@pytest.mark.parametrize(
    ("source_args", "endpoint", "payload_key", "payload_value"),
    [
        (
            ["--url", "https://source.example/portrait.png"],
            "/v1/files/upload/url",
            "url",
            "https://source.example/portrait.png",
        ),
        (
            ["--base64", "data:image/png;base64,UE5H"],
            "/v1/files/upload/base64",
            "file_data",
            "data:image/png;base64,UE5H",
        ),
    ],
)
def test_non_file_sources_use_matching_upload_endpoint(
    monkeypatch,
    tmp_path,
    capsys,
    source_args,
    endpoint,
    payload_key,
    payload_value,
):
    monkeypatch.setenv("AIHUB_API_KEY", "test-key-1234")
    monkeypatch.chdir(tmp_path)
    requests = []

    def fake_transport(method, url, headers, body=None, timeout=60):
        requests.append((method, url, headers, body))
        return Resp(200, {"url": "https://files.example/reference.png"}, "")

    monkeypatch.setattr(upload, "http_request", fake_transport)

    code = upload.main(source_args + ["--base-url", "https://api.example"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "https://files.example/reference.png"
    assert requests[0][1] == "https://api.example" + endpoint
    assert json.loads(requests[0][3])[payload_key] == payload_value


def test_upload_cli_reports_provider_error_without_exposing_key(
    monkeypatch, tmp_path, capsys
):
    source = tmp_path / "large.png"
    source.write_bytes(b"PNGDATA")
    monkeypatch.setenv("AIHUB_API_KEY", "secret-key-1234")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        upload,
        "http_request",
        lambda *args, **kwargs: Resp(
            413,
            {"error": {"message": "provider rejected the file"}},
            "",
        ),
    )

    code = upload.main(["--file", str(source)])

    output = capsys.readouterr()
    assert code == 1
    assert "文件过大" in output.err
    assert "secret-key-1234" not in output.err
    assert output.out == ""
