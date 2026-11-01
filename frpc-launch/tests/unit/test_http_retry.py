"""http_get 的网络抖动处理（ADR 0006 §2/§3）：分类、重试次数、退避、Retry-After。

全部离线：urlopen 与 sleep 都被替换，不发任何真实请求。
"""
import email.message
import socket
import urllib.error

import pytest

import frpc_launch
from frpc_launch import FrpcLaunchError


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


def http_error(code, retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://example.com/x", code, "boom", headers, None)


@pytest.fixture
def patched(monkeypatch):
    """把 urlopen 换成可编排的假实现，并把退避 sleep 记账后跳过。"""
    state = {"calls": 0, "slept": []}

    def install(outcomes):
        def fake_urlopen(req, timeout=None):
            state["calls"] += 1
            outcome = outcomes[state["calls"] - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome)

        monkeypatch.setattr(frpc_launch.urllib.request, "urlopen", fake_urlopen)

    monkeypatch.setattr(frpc_launch.time, "sleep", lambda s: state["slept"].append(s))
    state["install"] = install
    return state


def test_transient_urlerror_retries_then_succeeds(patched):
    patched["install"]([
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.URLError("[Errno 54] Connection reset by peer"),
        b"OK",
    ])

    assert frpc_launch.http_get("https://example.com/x") == b"OK"
    assert patched["calls"] == 3
    assert patched["slept"] == [1, 2]  # 指数退避 1s、2s


def test_http_5xx_is_transient(patched):
    patched["install"]([http_error(503), b"OK"])

    assert frpc_launch.http_get("https://example.com/x") == b"OK"
    assert patched["calls"] == 2
    assert patched["slept"] == [1]


def test_http_429_follows_retry_after(patched):
    patched["install"]([http_error(429, retry_after=7), b"OK"])

    assert frpc_launch.http_get("https://example.com/x") == b"OK"
    assert patched["slept"] == [7]


def test_http_429_excessive_retry_after_exits_without_retry(patched):
    patched["install"]([http_error(429, retry_after=9999), b"OK"])

    with pytest.raises(FrpcLaunchError, match="未提前重试"):
        frpc_launch.http_get("https://example.com/x")
    assert patched["calls"] == 1
    assert patched["slept"] == []


def test_deterministic_4xx_is_not_retried(patched):
    patched["install"]([http_error(404)])

    with pytest.raises(FrpcLaunchError) as excinfo:
        frpc_launch.http_get("https://example.com/x")

    assert patched["calls"] == 1
    assert patched["slept"] == []
    assert "确定性错误" in str(excinfo.value)


def test_attempts_are_bounded(patched):
    patched["install"]([urllib.error.URLError("boom")] * frpc_launch.HTTP_MAX_ATTEMPTS)

    with pytest.raises(FrpcLaunchError):
        frpc_launch.http_get("https://example.com/x")

    assert patched["calls"] == frpc_launch.HTTP_MAX_ATTEMPTS
    assert patched["slept"] == [1, 2]


def test_http_get_json_reuses_retrying_transport(patched):
    patched["install"]([http_error(500), b'{"ok": true}'])

    assert frpc_launch.http_get_json("https://example.com/x") == {"ok": True}
    assert patched["calls"] == 2


def test_retry_after_http_date_on_503(patched, monkeypatch):
    from email.utils import formatdate
    monkeypatch.setattr(frpc_launch.time, "time", lambda: 1000000)
    patched["install"]([http_error(503, formatdate(1000012, usegmt=True)), b"OK"])
    assert frpc_launch.http_get("https://example.com/x") == b"OK"
    assert patched["slept"] == [12]


def test_certificate_failure_never_retries(patched):
    import ssl
    patched["install"]([urllib.error.URLError(ssl.SSLCertVerificationError("bad certificate"))])
    with pytest.raises(FrpcLaunchError, match="证书"):
        frpc_launch.http_get("https://example.com/x")
    assert patched["calls"] == 1 and patched["slept"] == []


def test_download_read_loop_obeys_total_budget(monkeypatch):
    ticks = iter([0, 0, 0, 91])
    monkeypatch.setattr(frpc_launch.time, "monotonic", lambda: next(ticks))
    response = FakeResponse(b"chunk")
    response.read1 = lambda n: response.payload
    monkeypatch.setattr(frpc_launch.urllib.request, "urlopen", lambda *a, **kw: response)
    with pytest.raises(FrpcLaunchError, match="下载总预算"):
        frpc_launch.http_get("https://example.com/x")
