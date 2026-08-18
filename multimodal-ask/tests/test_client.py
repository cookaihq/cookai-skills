"""client.py 重试分支的离线单测（不发任何真实请求）。

覆盖三处此前无测试保护的判定：
  1. 网络失败的阶段分类（只有 DNS 失败 / 连接被拒才算「请求未发出」）
  2. BILLING_WRITE 下「结果不明」必须抛 AmbiguousRequest 而不是重发
  3. Retry-After 的解析、上限钳制与 nan/inf 防护
"""

import socket
import urllib.error

import pytest

import client
from client import (BILLING_WRITE, IDEMPOTENT, RESPONSE_LOST,
                    RETRY_AFTER_CAP_SECONDS, SEND_FAILED, AmbiguousRequest,
                    Resp, classify_network_error, request_with_retry,
                    retry_after_seconds)


# --------------------------------------------------------------------------- #
# 1. 阶段分类
# --------------------------------------------------------------------------- #

def test_classify_dns_failure_is_send_failed():
    exc = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
    assert classify_network_error(exc) == SEND_FAILED


def test_classify_connection_refused_is_send_failed():
    exc = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    assert classify_network_error(exc) == SEND_FAILED


def test_classify_urlerror_wrapping_body_send_failure_is_response_lost():
    """回归 H1b：urllib 把「写请求体途中连接被重置」也包成 URLError。

    do_open 把 h.request(...)（含写 body）整段包在 `except OSError -> URLError`
    里，所以 URLError 不代表请求没发出去。按 URLError 就判 SEND_FAILED 会让计费
    写被重发、重复扣费。
    """
    exc = urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))
    assert classify_network_error(exc) == RESPONSE_LOST


def test_classify_timeout_is_response_lost():
    """超时区分不出「还没连上」与「已发出正在等应答」，一律按结果不明。"""
    assert classify_network_error(urllib.error.URLError(socket.timeout("timed out"))) == RESPONSE_LOST
    assert classify_network_error(socket.timeout("timed out")) == RESPONSE_LOST


def test_classify_bare_oserror_is_response_lost():
    """不是 URLError → 错误发生在读应答阶段 → 请求必然已发出。"""
    assert classify_network_error(ConnectionResetError(54, "reset")) == RESPONSE_LOST


def test_classify_httperror_is_response_lost():
    exc = urllib.error.HTTPError("https://api.x", 500, "boom", {}, None)
    assert classify_network_error(exc) == RESPONSE_LOST


# --------------------------------------------------------------------------- #
# 2. BILLING_WRITE 的 ambiguous 路由
# --------------------------------------------------------------------------- #

def _recorder():
    calls = {"n": 0, "waits": [], "logs": []}
    return calls


def test_billing_write_response_lost_raises_ambiguous_without_retry():
    calls = _recorder()

    def call():
        calls["n"] += 1
        raise urllib.error.URLError(ConnectionResetError(54, "reset"))

    with pytest.raises(AmbiguousRequest) as ei:
        request_with_retry(call, op="submit_llm", write_safety=BILLING_WRITE,
                           sleep=calls["waits"].append, log=calls["logs"].append)
    assert calls["n"] == 1, "结果不明时绝不能重发"
    assert ei.value.op == "submit_llm"
    assert calls["waits"] == []


def test_billing_write_timeout_raises_ambiguous_without_retry():
    calls = _recorder()

    def call():
        calls["n"] += 1
        raise socket.timeout("timed out")

    with pytest.raises(AmbiguousRequest):
        request_with_retry(call, op="submit_llm", write_safety=BILLING_WRITE,
                           sleep=calls["waits"].append, log=calls["logs"].append)
    assert calls["n"] == 1


def test_billing_write_dns_failure_is_retried_then_reraised():
    calls = _recorder()

    def call():
        calls["n"] += 1
        raise urllib.error.URLError(socket.gaierror(8, "nodename"))

    with pytest.raises(urllib.error.URLError):
        request_with_retry(call, op="submit_llm", write_safety=BILLING_WRITE,
                           sleep=calls["waits"].append, log=calls["logs"].append)
    assert calls["n"] == client.MAX_ATTEMPTS
    assert calls["waits"] == [1, 2], "指数退避 1s、2s"


def test_billing_write_429_is_retried_but_5xx_is_not():
    for status, expected_calls in ((429, 3), (500, 1), (503, 1)):
        calls = _recorder()

        def call():
            calls["n"] += 1
            return Resp(status, None, "", {})

        out = request_with_retry(call, op="submit_llm", write_safety=BILLING_WRITE,
                                 retryable_statuses=frozenset([429]),
                                 sleep=calls["waits"].append, log=calls["logs"].append)
        assert out.status == status
        assert calls["n"] == expected_calls, "HTTP %s 的尝试次数不符" % status


def test_idempotent_retries_any_network_error_then_reraises():
    calls = _recorder()

    def call():
        calls["n"] += 1
        raise socket.timeout("timed out")

    with pytest.raises(socket.timeout):
        request_with_retry(call, op="poll", write_safety=IDEMPOTENT,
                           sleep=calls["waits"].append, log=calls["logs"].append)
    assert calls["n"] == client.MAX_ATTEMPTS


def test_idempotent_retries_5xx_and_returns_last_response():
    calls = _recorder()

    def call():
        calls["n"] += 1
        return Resp(503, None, "", {})

    out = request_with_retry(call, op="poll", sleep=calls["waits"].append,
                             log=calls["logs"].append)
    assert out.status == 503
    assert calls["n"] == client.MAX_ATTEMPTS
    assert calls["waits"] == [1, 2]


def test_success_on_second_attempt_stops_retrying():
    calls = _recorder()

    def call():
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp(503, None, "", {})
        return Resp(200, {"id": "t1"}, "", {})

    out = request_with_retry(call, op="poll", sleep=calls["waits"].append,
                             log=calls["logs"].append)
    assert out.status == 200
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 3. Retry-After 解析 / 钳制 / nan 防护
# --------------------------------------------------------------------------- #

def test_retry_after_plain_seconds():
    assert retry_after_seconds(Resp(429, None, "", {"Retry-After": "5"})) == 5


def test_retry_after_is_capped():
    assert RETRY_AFTER_CAP_SECONDS == 60
    assert retry_after_seconds(Resp(429, None, "", {"Retry-After": "86400"})) == 60


def test_retry_after_nan_and_inf_fall_back_to_backoff():
    """`float("nan")` 不报错，但 min(nan, 60) 仍是 nan，sleep(nan) 会崩。"""
    for raw in ("nan", "NaN", "inf", "-inf", "Infinity"):
        assert retry_after_seconds(Resp(429, None, "", {"Retry-After": raw})) is None, raw


def test_retry_after_negative_and_http_date_fall_back():
    assert retry_after_seconds(Resp(429, None, "", {"Retry-After": "-3"})) is None
    assert retry_after_seconds(
        Resp(429, None, "", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None


def test_retry_after_missing_header_or_no_headers():
    assert retry_after_seconds(Resp(429, None, "", {})) is None
    assert retry_after_seconds(Resp(429, None, "", None)) is None


def test_retry_after_capped_value_is_actually_slept():
    """钳制必须真正作用到等待时长上，不能只在解析函数里算完就丢。"""
    calls = _recorder()

    def call():
        calls["n"] += 1
        return Resp(429, None, "", {"Retry-After": "86400"})

    request_with_retry(call, op="poll", sleep=calls["waits"].append,
                       log=calls["logs"].append)
    assert calls["waits"] == [60, 60]


def test_nan_retry_after_never_reaches_sleep():
    calls = _recorder()

    def call():
        calls["n"] += 1
        return Resp(429, None, "", {"Retry-After": "nan"})

    request_with_retry(call, op="poll", sleep=calls["waits"].append,
                       log=calls["logs"].append)
    assert calls["waits"] == [1, 2], "nan 必须回落到指数退避"
    assert all(w == w for w in calls["waits"]), "等待时长不得为 nan"
