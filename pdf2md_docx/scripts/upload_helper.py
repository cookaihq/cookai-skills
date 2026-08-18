from __future__ import annotations

import os

from client import call_with_key_fallback, encode_multipart, request_with_retry

_HINTS = {
    400: "请求格式错误", 401: "鉴权失败", 403: "存储空间不足",
    413: "文件过大，请压缩或更换更小的文件",
    429: "请求频率超限（已按 ADR 0006 自动重试 3 次仍被限流，请稍后再试）",
    500: "服务器内部错误",
}


class UploadHelperError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def upload_local_file(path: str, keys: list, *, base_url: str = "https://api.aihubmax.com",
                      transport=None, sleep=None) -> str:
    """Upload a local file via the aihubmax stream endpoint; return the hosted URL.
    Raises UploadHelperError on HTTP failure.

    上传是写操作（在上游存储里落一份文件），按 ADR 0006 规则 4 走
    `idempotent=False`：只重试 429（服务端限流拒绝，文件没落盘）与连接阶段失败；
    请求发出之后的超时抛 `client.AmbiguousWrite`，由调用方报给用户，不盲重试。
    """
    with open(path, "rb") as fh:
        file_bytes = fh.read()
    ctype, body = encode_multipart({"auto_cleanup": "true"}, "file", os.path.basename(path), file_bytes)
    url = base_url + "/v1/files/upload/stream"

    def attempt(key):
        headers = {"Content-Type": ctype, "Authorization": "Bearer " + key}
        return request_with_retry("POST", url, headers, body, idempotent=False,
                                  op="上传 PDF 到 aihubmax",
                                  transport=transport, sleep=sleep)

    resp, _ = call_with_key_fallback(keys, attempt)
    if resp.status == 200 and isinstance(resp.json, dict) and resp.json.get("url"):
        return resp.json["url"]
    if resp.status == 200:
        raise UploadHelperError(200, "[HTTP 200] 上传响应缺少 url 字段: %s" % (resp.text or "")[:200])
    hint = _HINTS.get(resp.status, "未预期的响应")
    server = ""
    if isinstance(resp.json, dict) and isinstance(resp.json.get("error"), dict):
        server = resp.json["error"].get("message", "")
    msg = "[HTTP %s] %s" % (resp.status, hint)
    if server:
        msg += " | 上游: " + server
    raise UploadHelperError(resp.status, msg)
