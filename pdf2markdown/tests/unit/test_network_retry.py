"""ADR 0006 网络抖动处理的单元验收。

三个改动点各自直接调用、显式传 `sleep=` 记录退避秒数，不经 workflow、不发任何
真实网络请求：
  - pdf_source.download_https_pdf   来源 PDF 下载（幂等 GET）
  - result_archive.download_and_prepare  Doc2X 结果 ZIP 下载（幂等 GET）
  - doc2x.poll_task                 单次逻辑轮询内部的物理重试
"""
import http.client
import socket
from pathlib import Path

import doc2x
import pdf_source
import pytest
import result_archive


# --------------------------------------------------------------------------- #
# pdf_source.download_https_pdf
# --------------------------------------------------------------------------- #


class _CountingFailure:
    """A transport whose resolve() always fails with the given exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def resolve(self, *_args, **_kwargs):
        self.calls += 1
        raise self.exc


def test_source_download_retries_a_transient_dns_failure_then_gives_up(tmp_path):
    transport = _CountingFailure(OSError("temporary dns outage"))
    waits = []

    with pytest.raises(pdf_source.PdfSourceError) as caught:
        pdf_source.download_https_pdf(
            "https://docs.example/report.pdf",
            tmp_path / "source.pdf",
            transport=transport,
            sleep=waits.append,
        )

    assert caught.value.code == "source_dns_failed"
    assert transport.calls == pdf_source.NET_MAX_ATTEMPTS
    assert waits == [1, 2]  # 指数退避 2^(n-1)


def test_source_download_does_not_retry_a_deterministic_source_error(tmp_path):
    class Unresolvable:
        def __init__(self):
            self.calls = 0

        def resolve(self, *_args, **_kwargs):
            self.calls += 1
            raise pdf_source.PdfSourceError(
                "unsafe_source_address", "The address is not public."
            )

    transport = Unresolvable()
    waits = []

    with pytest.raises(pdf_source.PdfSourceError) as caught:
        pdf_source.download_https_pdf(
            "https://docs.example/report.pdf",
            tmp_path / "source.pdf",
            transport=transport,
            sleep=waits.append,
        )

    assert caught.value.code == "unsafe_source_address"
    assert transport.calls == 1
    assert waits == []


def test_transient_source_codes_stay_disjoint_from_deterministic_ones():
    # 确定性错误一旦被误列进瞬时集合，「来源不合格」就会被重试并报成网络错误。
    deterministic = {
        "unsafe_source_scheme",
        "invalid_source_url",
        "unsafe_source_address",
        "source_peer_mismatch",
        "source_authentication_required",
        "source_http_error",
        "source_size_limit_exceeded",
        "source_redirect_loop",
        "source_redirect_limit_exceeded",
        "invalid_pdf",
        "source_disk_write_failed",
    }
    assert not (deterministic & pdf_source.TRANSIENT_SOURCE_ERROR_CODES)


# --------------------------------------------------------------------------- #
# result_archive.download_and_prepare
# --------------------------------------------------------------------------- #


def test_result_download_retries_a_transient_failure_and_clears_the_partial(
    tmp_path, monkeypatch
):
    import os

    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt_fd = os.open(str(attempt_dir), os.O_RDONLY)
    calls = {"n": 0}

    def fake_download_archive(url, name, *, destination_fd, transport=None):
        calls["n"] += 1
        # 模拟「写了半截然后断线」：O_EXCL 建文件后失败且不删除。
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=destination_fd)
        os.write(descriptor, b"partial")
        os.close(descriptor)
        raise result_archive.ResultArchiveError(
            "result_read_timeout", "The result download timed out."
        )

    monkeypatch.setattr(result_archive, "download_archive", fake_download_archive)
    waits = []
    try:
        with pytest.raises(result_archive.ResultArchiveError) as caught:
            result_archive.download_and_prepare(
                "https://results.example/result.zip",
                attempt_fd,
                request_filename="doc.pdf",
                sleep=waits.append,
            )
        assert caught.value.code == "result_read_timeout"
        assert calls["n"] == result_archive.NET_MAX_ATTEMPTS
        assert waits == [1, 2]
        # 每一轮重试前半截文件都被清掉了，否则第 2 次 O_EXCL 就会失败并把错误码
        # 变成 result_disk_write_failed（那才是这条断言真正在守的东西）。
        assert caught.value.code != "result_disk_write_failed"
    finally:
        os.close(attempt_fd)


def test_result_download_does_not_retry_an_expired_result_url(tmp_path, monkeypatch):
    import os

    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    attempt_fd = os.open(str(attempt_dir), os.O_RDONLY)
    calls = {"n": 0}

    def fake_download_archive(url, name, *, destination_fd, transport=None):
        calls["n"] += 1
        raise result_archive.ResultArchiveError(
            "result_url_unavailable", "The result URL is no longer available."
        )

    monkeypatch.setattr(result_archive, "download_archive", fake_download_archive)
    waits = []
    try:
        with pytest.raises(result_archive.ResultArchiveError) as caught:
            result_archive.download_and_prepare(
                "https://results.example/result.zip",
                attempt_fd,
                request_filename="doc.pdf",
                sleep=waits.append,
            )
        assert caught.value.code == "result_url_unavailable"
        assert calls["n"] == 1
        assert waits == []
    finally:
        os.close(attempt_fd)


# --------------------------------------------------------------------------- #
# doc2x.poll_task
# --------------------------------------------------------------------------- #


class _PollTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, method, url, headers, body):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class _PollResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body


def test_poll_retries_a_transient_network_failure_before_spending_the_budget():
    transport = _PollTransport(
        [
            socket.timeout("read timed out"),
            http.client.RemoteDisconnected("closed"),
            _PollResponse(200, b'{"status":"processing"}'),
        ]
    )
    waits = []

    result = doc2x.poll_task(
        task_id="task-1", api_key="k", transport=transport, sleep=waits.append
    )

    assert result.state == "processing"
    assert transport.calls == doc2x.NET_MAX_ATTEMPTS
    assert waits == [1, 2]


def test_poll_reports_transient_only_after_the_retries_are_exhausted():
    transport = _PollTransport([socket.timeout("read timed out")])
    waits = []

    result = doc2x.poll_task(
        task_id="task-1", api_key="k", transport=transport, sleep=waits.append
    )

    assert result.state == "poll_transient"
    assert transport.calls == doc2x.NET_MAX_ATTEMPTS
    assert waits == [1, 2]


def test_poll_does_not_retry_a_non_network_transport_error():
    # 注入 transport 抛出的断言/编程错误不是网络故障，重试只会掩盖缺陷。
    transport = _PollTransport([AssertionError("network access is not expected")])
    waits = []

    result = doc2x.poll_task(
        task_id="task-1", api_key="k", transport=transport, sleep=waits.append
    )

    assert result.state == "poll_transient"
    assert transport.calls == 1
    assert waits == []


def test_poll_does_not_retry_an_http_level_transient_response():
    # HTTP 5xx 交由外层轮询循环按其轮询间隔重试，物理层不再叠一轮。
    transport = _PollTransport([_PollResponse(503, b"")])
    waits = []

    result = doc2x.poll_task(
        task_id="task-1", api_key="k", transport=transport, sleep=waits.append
    )

    assert result.state == "poll_transient"
    assert transport.calls == 1
    assert waits == []


def test_transient_network_classification_covers_the_real_failure_families():
    assert doc2x.is_transient_network_error(socket.timeout("t")) is True
    assert doc2x.is_transient_network_error(ConnectionResetError("reset")) is True
    assert doc2x.is_transient_network_error(socket.gaierror("dns")) is True
    assert doc2x.is_transient_network_error(
        http.client.IncompleteRead(b"", 1)
    ) is True
    assert doc2x.is_transient_network_error(doc2x.Doc2xError("too big")) is False
    assert doc2x.is_transient_network_error(AssertionError("stub")) is False
