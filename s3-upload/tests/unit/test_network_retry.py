"""ADR 0006 网络抖动处理的单元验收（s3-upload）。

只覆盖**读语义**调用的重试；写操作的 ambiguous 语义不变，这里也用一条断言把
「写路径不会因为本次整改开始重试」钉住。全部离线，不发任何真实请求。
"""
import http.client
import socket
from urllib.error import URLError

import multipart
import pytest
import s3


class _Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, method, url, headers, body):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def test_read_retry_recovers_from_a_transient_transport_failure():
    ok = s3.Response(200, b"", {})
    transport = _Recorder([s3.TransportError("timed out"), ok])
    waits = []

    result = s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is ok
    assert transport.calls == 2
    assert waits == [1]  # 指数退避 2^(n-1)


def test_read_retry_gives_up_after_the_default_attempt_budget():
    transport = _Recorder([socket.timeout("timed out")])
    waits = []

    result = s3.read_request_with_retry(
        transport, "GET", "https://x/k", {}, b"", sleep=waits.append
    )

    # 穷尽后返回 None —— 各调用点既有的「这次观测没答案」约定，落回 ambiguous。
    assert result is None
    assert transport.calls == s3.NET_MAX_ATTEMPTS
    assert waits == [1, 2]


def test_read_retry_does_not_retry_a_non_transport_exception():
    # 注入 transport 抛出的断言/编程错误不是网络故障，重试只会掩盖缺陷。
    transport = _Recorder([AssertionError("network access is not expected")])
    waits = []

    result = s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is None
    assert transport.calls == 1
    assert waits == []


def test_read_retry_logs_without_leaking_the_presigned_url():
    lines = []
    transport = _Recorder([s3.TransportError("timed out"), s3.Response(200, b"", {})])

    s3.read_request_with_retry(
        transport,
        "HEAD",
        "https://x/k?X-Amz-Signature=deadbeefsecret",
        {},
        b"",
        sleep=lambda _s: None,
        log=lines.append,
    )

    assert lines and "retry 1/3" in lines[0]
    assert all("deadbeefsecret" not in line for line in lines)


def test_default_retry_log_goes_to_stderr_only(capsys):
    # stdout 是 URL / 结构化 JSON 的输出通道，重试日志绝不能混进去。
    transport = _Recorder([s3.TransportError("timed out"), s3.Response(200, b"", {})])

    s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=lambda _s: None
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "retry 1/3" in captured.err


def test_read_retry_recovers_from_a_transient_5xx_status():
    """5xx 从不抛异常——http_request 把它转成 Response 返回，必须按状态码判瞬时。"""
    ok = s3.Response(200, b"", {})
    transport = _Recorder([s3.Response(503, b"slow down", {}), ok])
    waits = []

    result = s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is ok
    assert transport.calls == 2
    assert waits == [1]


def test_read_retry_returns_the_last_response_when_the_5xx_persists():
    # 穷尽后返回最后一个应答（而不是 None）：调用方按状态码走既有判定分支。
    transport = _Recorder([s3.Response(500, b"", {})])
    waits = []

    result = s3.read_request_with_retry(
        transport, "GET", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is not None and result.status == 500
    assert transport.calls == s3.NET_MAX_ATTEMPTS
    assert waits == [1, 2]


def test_read_retry_honours_retry_after_on_429():
    ok = s3.Response(200, b"", {})
    throttled = s3.Response(429, b"", {"Retry-After": "7"})
    transport = _Recorder([throttled, ok])
    waits = []

    result = s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is ok
    assert waits == [7]  # 遵循 Retry-After，而不是退避公式的 1s


def test_read_retry_clamps_an_oversized_retry_after():
    # 服务端可以给出任意大的值；无上限会把一次 CLI 调用挂到用户以为卡死。
    transport = _Recorder(
        [s3.Response(429, b"", {"retry-after": "3600"}), s3.Response(200, b"", {})]
    )
    waits = []

    s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert waits == [s3.NET_RETRY_AFTER_CAP_SECONDS]


def test_retry_after_falls_back_to_backoff_when_unreadable():
    # HTTP-date 形式与垃圾值都读不成秒数：退回指数退避，不臆断。
    assert s3.retry_after_seconds(s3.Response(429, b"", {})) is None
    assert (
        s3.retry_after_seconds(
            s3.Response(429, b"", {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        )
        is None
    )
    assert s3.retry_after_seconds(s3.Response(429, b"", {"Retry-After": "-5"})) == 0


def test_read_retry_does_not_retry_a_deterministic_4xx():
    transport = _Recorder([s3.Response(403, b"", {})])
    waits = []

    result = s3.read_request_with_retry(
        transport, "HEAD", "https://x/k", {}, b"", sleep=waits.append
    )

    assert result is not None and result.status == 403
    assert transport.calls == 1
    assert waits == []


def test_transient_read_status_classification():
    assert s3.is_transient_read_status(429) is True
    assert s3.is_transient_read_status(500) is True
    assert s3.is_transient_read_status(503) is True
    assert s3.is_transient_read_status(599) is True
    assert s3.is_transient_read_status(200) is False
    assert s3.is_transient_read_status(403) is False
    assert s3.is_transient_read_status(404) is False


def test_transient_transport_classification_covers_the_real_failure_families():
    assert s3.is_transient_transport_error(s3.TransportError("x")) is True
    assert s3.is_transient_transport_error(URLError("x")) is True
    assert s3.is_transient_transport_error(socket.timeout("x")) is True
    assert s3.is_transient_transport_error(ConnectionResetError("x")) is True
    assert s3.is_transient_transport_error(socket.gaierror("x")) is True
    assert s3.is_transient_transport_error(http.client.RemoteDisconnected("x")) is True
    assert s3.is_transient_transport_error(ValueError("x")) is False
    assert s3.is_transient_transport_error(AssertionError("x")) is False


def test_multipart_write_calls_keep_the_ambiguous_contract(monkeypatch):
    """写操作不重试：一次传输异常 = 一次物理请求 + None（→ *_unknown/ambiguous）。

    这是 ADR 0006 规则 4 的先例语义，本次整改明确不动它。
    """
    calls = {"n": 0}

    def transport(method, url, headers, body):
        calls["n"] += 1
        raise s3.TransportError("timed out")

    class _Signed:
        method = "PUT"
        url = "https://x/k"
        headers = {}
        body = b""

    monkeypatch.setattr(
        multipart, "_validated_request_moment", lambda resolved, now: now
    )
    monkeypatch.setattr(multipart, "connection_for", lambda resolved: None)

    result = multipart._request(
        resolved=object(),
        method="PUT",
        key="k",
        now=None,
        transport=transport,
        request_builder=lambda *a, **k: _Signed(),
    )

    assert result is None
    assert calls["n"] == 1


def test_multipart_read_only_calls_retry(monkeypatch):
    calls = {"n": 0}
    ok = s3.Response(200, b"", {})

    def transport(method, url, headers, body):
        calls["n"] += 1
        if calls["n"] < 3:
            raise s3.TransportError("timed out")
        return ok

    class _Signed:
        method = "HEAD"
        url = "https://x/k"
        headers = {}
        body = b""

    monkeypatch.setattr(
        multipart, "_validated_request_moment", lambda resolved, now: now
    )
    monkeypatch.setattr(multipart, "connection_for", lambda resolved: None)

    waits = []
    result = multipart._request(
        resolved=object(),
        method="HEAD",
        key="k",
        now=None,
        transport=transport,
        request_builder=lambda *a, **k: _Signed(),
        read_only=True,
        # 退避钩子从 _request 自己的签名注入，不再 patch s3 模块上的 time.sleep 全局。
        sleep=waits.append,
    )

    assert result is ok
    assert calls["n"] == s3.NET_MAX_ATTEMPTS
    assert waits == [1, 2]


def test_multipart_write_status_5xx_is_not_retried(monkeypatch):
    """读路径按状态码重试，写路径不受影响：一个 503 = 一次物理请求，直接返回。"""
    calls = {"n": 0}
    throttled = s3.Response(503, b"", {})

    def transport(method, url, headers, body):
        calls["n"] += 1
        return throttled

    class _Signed:
        method = "PUT"
        url = "https://x/k"
        headers = {}
        body = b""

    monkeypatch.setattr(
        multipart, "_validated_request_moment", lambda resolved, now: now
    )
    monkeypatch.setattr(multipart, "connection_for", lambda resolved: None)

    result = multipart._request(
        resolved=object(),
        method="PUT",
        key="k",
        now=None,
        transport=transport,
        request_builder=lambda *a, **k: _Signed(),
    )

    assert result is throttled
    assert calls["n"] == 1
