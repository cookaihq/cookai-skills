import io
import urllib.error

import pytest

import client


def test_encode_multipart_structure():
    ctype, body = client.encode_multipart(
        fields={"auto_cleanup": "true"},
        file_field="file",
        filename="clip.mp4",
        file_bytes=b"\x00\x01BINARY",
        boundary="BOUND123",
    )
    assert ctype == "multipart/form-data; boundary=BOUND123"
    assert b'name="auto_cleanup"' in body
    assert b"true" in body
    assert b'name="file"; filename="clip.mp4"' in body
    assert b"\x00\x01BINARY" in body
    assert body.endswith(b"--BOUND123--\r\n")
    assert body.startswith(b"--BOUND123\r\n")
    assert b'name="auto_cleanup"\r\n\r\ntrue\r\n' in body


class _FakeHTTPResponse:
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_request_parses_json_on_200(monkeypatch):
    def fake_urlopen(req, timeout=60):
        return _FakeHTTPResponse(200, b'{"url": "https://x/y", "id": "f1", "size": 5}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    resp = client.http_request("POST", "https://api.aihubmax.com/v1/files/upload/url",
                               {"Content-Type": "application/json"}, body=b"{}")
    assert resp.status == 200
    assert resp.json["url"] == "https://x/y"


def test_http_request_returns_status_on_httperror(monkeypatch):
    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            url="u", code=413, msg="too large", hdrs=None,
            fp=io.BytesIO(b'{"error": {"message": "\\u6587\\u4ef6\\u8fc7\\u5927", "type": "file_too_large_error"}}'),
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    resp = client.http_request("POST", "https://api.aihubmax.com/v1/files/upload/stream", {}, body=b"x")
    assert resp.status == 413
    assert resp.json["error"]["type"] == "file_too_large_error"


def test_http_request_adds_browser_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=60):
        seen.update({key.lower(): value for key, value in req.header_items()})
        return _FakeHTTPResponse(200, b'{"url": "https://x/y"}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client.http_request("POST", "https://api.aihubmax.com/v1/files/upload/url", {}, body=b"{}")
    assert seen["user-agent"] == client.DEFAULT_USER_AGENT


def test_http_request_preserves_caller_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=60):
        seen.update({key.lower(): value for key, value in req.header_items()})
        return _FakeHTTPResponse(200, b'{"url": "https://x/y"}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client.http_request(
        "POST",
        "https://api.aihubmax.com/v1/files/upload/url",
        {"user-agent": "custom-agent"},
        body=b"{}",
    )
    assert seen["user-agent"] == "custom-agent"


def _resp(status):
    return client.Resp(status, None, "")


def test_fallback_advances_only_on_401():
    tried = []

    def attempt(key):
        tried.append(key)
        return _resp(401) if key == "bad" else _resp(200)

    resp, used = client.call_with_key_fallback(["bad", "good"], attempt)
    assert resp.status == 200
    assert used == "good"
    assert tried == ["bad", "good"]


def test_fallback_stops_on_non_401():
    tried = []

    def attempt(key):
        tried.append(key)
        return _resp(413)

    resp, used = client.call_with_key_fallback(["k1", "k2"], attempt)
    assert resp.status == 413
    assert used == "k1"
    assert tried == ["k1"]  # did NOT try k2


def test_fallback_all_401_returns_last():
    resp, used = client.call_with_key_fallback(["a", "b"], lambda k: _resp(401))
    assert resp.status == 401
    assert used == "b"


def test_fallback_empty_keys_raises():
    with pytest.raises(ValueError):
        client.call_with_key_fallback([], lambda k: _resp(200))
