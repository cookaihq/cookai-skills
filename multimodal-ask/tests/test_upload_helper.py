import pytest

import client
import upload_helper
from client import Resp


def test_upload_local_file_success(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"VIDEO")
    seen = {}

    def transport(method, url, headers, body=None, timeout=60):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        return Resp(200, {"url": "https://files/clip.mp4", "id": "f1", "size": 5}, "")

    out = upload_helper.upload_local_file(str(f), ["k1"], base_url="https://api.x", transport=transport)
    assert out == "https://files/clip.mp4"
    assert seen["url"] == "https://api.x/v1/files/upload/stream"
    assert seen["auth"] == "Bearer k1"


def test_upload_local_file_error_raises(tmp_path):
    f = tmp_path / "big.mp4"
    f.write_bytes(b"x")

    def transport(method, url, headers, body=None, timeout=60):
        return Resp(413, {"error": {"message": "文件过大"}}, "")

    with pytest.raises(upload_helper.UploadHelperError) as ei:
        upload_helper.upload_local_file(str(f), ["k1"], base_url="https://api.x", transport=transport)
    assert ei.value.status == 413
    assert "请压缩" in ei.value.message   # hint from _HINTS[413], not just the upstream echo
    assert "上游" in ei.value.message     # upstream message section present


def test_upload_local_file_200_without_url_raises(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")

    def transport(method, url, headers, body=None, timeout=60):
        return Resp(200, {"id": "f1"}, "")  # 200 but no url field

    with pytest.raises(upload_helper.UploadHelperError) as ei:
        upload_helper.upload_local_file(str(f), ["k1"], base_url="https://api.x", transport=transport)
    assert ei.value.status == 200
    assert "缺少 url" in ei.value.message


def test_upload_local_file_generic_urlerror_is_ambiguous_not_replayed(tmp_path):
    """回归 H1b：URLError 不等于「请求没发出去」。

    urllib 把「写请求体途中失败」也包成 URLError，此时文件可能已经整份到达服务端。
    上传是计费写操作，必须按结果不明抛 AmbiguousRequest 且只尝试一次，不能重发。
    """
    import urllib.error
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    calls = {"n": 0}

    def transport(method, url, headers, body=None, timeout=60):
        calls["n"] += 1
        raise urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))

    with pytest.raises(client.AmbiguousRequest):
        upload_helper.upload_local_file(str(f), ["k1"], base_url="https://api.x",
                                        transport=transport, sleep=lambda s: None,
                                        log=lambda m: None)
    assert calls["n"] == 1


def test_upload_local_file_dns_failure_is_retried_then_propagates(tmp_path):
    """DNS 解析失败是唯一能确定「请求没发出去」的一类，重试安全。"""
    import socket
    import urllib.error
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    calls = {"n": 0}

    def transport(method, url, headers, body=None, timeout=60):
        calls["n"] += 1
        raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))

    with pytest.raises(urllib.error.URLError):
        upload_helper.upload_local_file(str(f), ["k1"], base_url="https://api.x",
                                        transport=transport, sleep=lambda s: None,
                                        log=lambda m: None)
    assert calls["n"] == client.MAX_ATTEMPTS
