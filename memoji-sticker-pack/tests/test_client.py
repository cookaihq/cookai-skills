"""client.py 重试分支的离线单测（不发任何真实请求）。"""

import http.client
import socket
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import client  # noqa: E402
from client import (BILLING_WRITE, MAX_RETRY_AFTER_SECONDS,  # noqa: E402
                    AmbiguousRequest, Resp, request_with_retry,
                    retry_after_seconds)


# --------------------------------------------------------------------------- #
# L1：Retry-After 的 nan / inf 防护与上限钳制
# --------------------------------------------------------------------------- #

def test_retry_after_plain_and_capped():
    assert retry_after_seconds(Resp(429, None, "", {"Retry-After": "5"})) == 5
    assert retry_after_seconds(
        Resp(429, None, "", {"Retry-After": "99999"})) == MAX_RETRY_AFTER_SECONDS


def test_retry_after_nan_and_inf_fall_back_to_backoff():
    """`min(nan, 120)` 仍是 nan，`sleep(nan)` 会崩——必须先被 isfinite 挡掉。"""
    for raw in ("nan", "NaN", "inf", "-inf", "Infinity"):
        assert retry_after_seconds(Resp(429, None, "", {"Retry-After": raw})) is None, raw


def test_retry_after_negative_and_http_date_fall_back():
    assert retry_after_seconds(Resp(429, None, "", {"Retry-After": "-3"})) is None
    assert retry_after_seconds(
        Resp(429, None, "", {"Retry-After": "Mon, 21 Oct 2026 07:28:00 GMT"})) is None


def test_nan_retry_after_never_reaches_sleep():
    waits = []

    def call():
        return Resp(429, None, "", {"Retry-After": "nan"})

    request_with_retry(call, op="poll", sleep=waits.append, log=lambda m: None)
    assert waits == [1, 2], "nan 必须回落到指数退避"


def test_capped_retry_after_is_actually_slept():
    waits = []

    def call():
        return Resp(429, None, "", {"Retry-After": "99999"})

    request_with_retry(call, op="poll", sleep=waits.append, log=lambda m: None)
    assert waits == [MAX_RETRY_AFTER_SECONDS, MAX_RETRY_AFTER_SECONDS]


# --------------------------------------------------------------------------- #
# L2：http.client 家族必须被重试层接住
# --------------------------------------------------------------------------- #

def test_network_errors_covers_http_client_exceptions():
    """`(URLError, OSError)` 等价于只有 OSError——http.client 家族会漏网。"""
    assert issubclass(http.client.HTTPException, client.NETWORK_ERRORS) or \
        http.client.HTTPException in client.NETWORK_ERRORS
    assert not issubclass(http.client.BadStatusLine, OSError), \
        "前提校验：BadStatusLine 确实不是 OSError 子类"


@pytest.mark.parametrize("exc_factory", [
    lambda: http.client.BadStatusLine("garbage"),
    lambda: http.client.IncompleteRead(b"half"),
    lambda: http.client.ResponseNotReady(),
])
def test_http_client_exceptions_are_retried_not_leaked(exc_factory):
    calls = {"n": 0}
    waits = []

    def call():
        calls["n"] += 1
        raise exc_factory()

    with pytest.raises(http.client.HTTPException):
        request_with_retry(call, op="poll", sleep=waits.append, log=lambda m: None)
    assert calls["n"] == client.MAX_ATTEMPTS, "应被重试层接住并重试，而不是直接穿透"
    assert waits == [1, 2]


def test_http_client_exception_on_billing_write_is_ambiguous():
    """连接被中途掐断 → 结果不明 → 抛 AmbiguousRequest，绝不重发。"""
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise http.client.IncompleteRead(b"half")

    with pytest.raises(AmbiguousRequest):
        request_with_retry(call, op="upload", write_safety=BILLING_WRITE,
                           sleep=lambda s: None, log=lambda m: None)
    assert calls["n"] == 1


def test_socket_timeout_still_retried_for_idempotent_reads():
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        raise socket.timeout("timed out")

    with pytest.raises(socket.timeout):
        request_with_retry(call, op="poll", sleep=lambda s: None, log=lambda m: None)
    assert calls["n"] == client.MAX_ATTEMPTS
