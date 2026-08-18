"""ADR 0006 网络抖动处理与本地错误分流的单元验收（preview-share）。

全部离线：`ftplib` 全部换成假会话，不建立任何真实连接、不发任何请求。
"""
import ftplib
import socket

import pytest
import upload


class _FakeFtp:
    """只实现 upload_file / remote_size 走到的那几个 ftplib.FTP 方法。"""

    def __init__(self, *, stor_script=(), sizes=()):
        self.stor_script = list(stor_script)
        self.sizes = list(sizes)
        self.stor_calls = []
        self.size_calls = 0
        self.closed = False

    def storbinary(self, command, fh):
        # 记录每次尝试**从哪个偏移开始读**以及实际读到的内容：重传若不 seek(0)，
        # 传上去的就只是中断位置之后的残段。
        start = fh.tell()
        payload = fh.read()
        self.stor_calls.append((command, start, payload))
        index = min(len(self.stor_calls) - 1, len(self.stor_script) - 1)
        outcome = self.stor_script[index] if self.stor_script else None
        if isinstance(outcome, Exception):
            raise outcome

    def voidcmd(self, command):
        return "200 OK"

    def size(self, remote):
        self.size_calls += 1
        index = min(self.size_calls - 1, len(self.sizes) - 1)
        outcome = self.sizes[index] if self.sizes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True

    def quit(self):
        self.closed = True


def _session(ftp, monkeypatch):
    session = upload.FtpSession.__new__(upload.FtpSession)
    session.parts = None
    session.timeout = 1
    session.ftp = ftp
    session.size_supported = True
    monkeypatch.setattr(upload.time, "sleep", lambda _s: None)
    return session


# --- 分类 ---------------------------------------------------------------------


def test_transient_classification_excludes_local_filesystem_errors():
    # 瞬时：协议层临时否定 + 三类 socket 故障
    assert upload.is_transient_ftp_error(ftplib.error_temp("421 service busy")) is True
    assert upload.is_transient_ftp_error(socket.timeout("timed out")) is True
    assert upload.is_transient_ftp_error(ConnectionResetError("reset")) is True
    assert upload.is_transient_ftp_error(BrokenPipeError("broken pipe")) is True
    assert upload.is_transient_ftp_error(socket.gaierror("no such host")) is True

    # 确定性：FTP 永久否定
    assert upload.is_transient_ftp_error(ftplib.error_perm("550 denied")) is False
    # 确定性：本地文件系统错误。曾经因为写成裸 OSError 被误判成瞬时，
    # 于是本地文件的问题被重试三轮后报成 FTP 故障。
    assert upload.is_transient_ftp_error(FileNotFoundError("missing")) is False
    assert upload.is_transient_ftp_error(PermissionError("denied")) is False
    assert upload.is_transient_ftp_error(IsADirectoryError("is a dir")) is False
    # 语义不明的协议异常同样不重试
    assert upload.is_transient_ftp_error(ftplib.error_proto("garbage")) is False


# --- 本地文件错误：立刻失败，且说清是本地问题 ---------------------------------


def test_a_missing_local_file_fails_immediately_as_a_local_error(tmp_path, monkeypatch):
    ftp = _FakeFtp()
    session = _session(ftp, monkeypatch)

    with pytest.raises(upload.LocalSourceError) as excinfo:
        session.upload_file(str(tmp_path / "gone.html"), "/r/gone.html", 10)

    assert "gone.html" in str(excinfo.value)
    assert ftp.stor_calls == []  # 一个 STOR 都没发，更没有重试三轮
    assert ftp.size_calls == 0  # 也没有去问服务器状态


def test_an_unreadable_local_file_fails_immediately_as_a_local_error(
    tmp_path, monkeypatch
):
    blocked = tmp_path / "blocked.html"
    blocked.write_bytes(b"x" * 8)
    blocked.chmod(0o000)
    ftp = _FakeFtp()
    session = _session(ftp, monkeypatch)

    try:
        with pytest.raises(upload.LocalSourceError):
            session.upload_file(str(blocked), "/r/blocked.html", 8)
    finally:
        blocked.chmod(0o600)

    assert ftp.stor_calls == []


# --- 瞬时故障：重传，且必须从文件头开始 ---------------------------------------


def test_a_transient_failure_retransmits_the_whole_file_from_offset_zero(
    tmp_path, monkeypatch
):
    payload = b"0123456789" * 4
    local = tmp_path / "page.html"
    local.write_bytes(payload)
    ftp = _FakeFtp(
        stor_script=[ConnectionResetError("reset"), None],
        sizes=[7],  # 远端只有半截，说明上一轮没写完 -> 重传
    )
    session = _session(ftp, monkeypatch)

    outcome = session.upload_file(str(local), "/r/page.html", len(payload))

    assert outcome == "uploaded"
    assert len(ftp.stor_calls) == 2
    # 两次尝试都从偏移 0 开始，且送出的是完整内容——文件句柄提到循环外之后，
    # 少了 seek(0) 就会在第二次只传剩下的残段。
    assert [call[1] for call in ftp.stor_calls] == [0, 0]
    assert [call[2] for call in ftp.stor_calls] == [payload, payload]


def test_a_transient_failure_skips_retransmission_when_the_remote_is_complete(
    tmp_path, monkeypatch
):
    payload = b"abcd" * 5
    local = tmp_path / "done.html"
    local.write_bytes(payload)
    ftp = _FakeFtp(
        stor_script=[socket.timeout("timed out"), None],
        sizes=[len(payload)],  # 远端已是完整文件：上一轮其实写完了
    )
    session = _session(ftp, monkeypatch)

    outcome = session.upload_file(str(local), "/r/done.html", len(payload))

    assert outcome == "already-complete"
    assert len(ftp.stor_calls) == 1  # 不重传


# --- SIZE 不可用：ambiguous，不盲重试 -----------------------------------------


def test_an_unprobeable_remote_becomes_ambiguous_instead_of_a_blind_retry(
    tmp_path, monkeypatch
):
    payload = b"z" * 16
    local = tmp_path / "risky.html"
    local.write_bytes(payload)
    ftp = _FakeFtp(
        stor_script=[ConnectionResetError("reset"), None],
        # 502 = 服务器没实现 SIZE：先查后写的前提不成立
        sizes=[ftplib.error_perm("502 not implemented")],
    )
    session = _session(ftp, monkeypatch)

    with pytest.raises(upload.AmbiguousUpload) as excinfo:
        session.upload_file(str(local), "/r/risky.html", len(payload))

    assert "不盲重试" in str(excinfo.value)
    assert len(ftp.stor_calls) == 1


def test_a_deterministic_ftp_error_is_not_retried(tmp_path, monkeypatch):
    local = tmp_path / "denied.html"
    local.write_bytes(b"q" * 4)
    ftp = _FakeFtp(stor_script=[ftplib.error_perm("550 permission denied")])
    session = _session(ftp, monkeypatch)

    with pytest.raises(ftplib.error_perm):
        session.upload_file(str(local), "/r/denied.html", 4)

    assert len(ftp.stor_calls) == 1
    assert ftp.size_calls == 0


def test_a_persistent_transient_failure_gives_up_after_the_attempt_budget(
    tmp_path, monkeypatch
):
    payload = b"y" * 12
    local = tmp_path / "flaky.html"
    local.write_bytes(payload)
    ftp = _FakeFtp(
        stor_script=[ConnectionResetError("reset")],
        sizes=[3],  # 远端始终不完整，每轮都重传
    )
    session = _session(ftp, monkeypatch)

    with pytest.raises(ConnectionResetError):
        session.upload_file(str(local), "/r/flaky.html", len(payload))

    assert len(ftp.stor_calls) == upload.NET_MAX_ATTEMPTS
    assert [call[1] for call in ftp.stor_calls] == [0] * upload.NET_MAX_ATTEMPTS
