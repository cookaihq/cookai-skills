from __future__ import annotations

import os

from client import (BILLING_WRITE, UPLOAD_TIMEOUT, call_with_key_fallback,
                    encode_multipart, http_request, request_with_retry)

_HINTS = {
    400: "请求格式错误", 401: "鉴权失败", 403: "存储空间不足",
    413: "文件过大，请压缩或更换更小的文件",
    429: "请求频率超限（已按 Retry-After / 指数退避自动重试 3 次仍未通过）",
    500: "服务器内部错误（结果不明：文件可能已存入但没拿到 URL，未自动重试）",
}

# 上传是写操作、无幂等键（ADR 0006 规则 4）：
#   - 429 明确表示本次未受理 → 可安全重试，带 Retry-After 时按该值等待。
#   - 连接阶段失败（DNS 解析失败 / 连接被拒）→ 请求确定没发出去，可安全重试。
#   - 5xx 与「已发出但响应丢失」→ 无法确认服务端是否已存下这个文件，按结果不明
#     处理：5xx 直接报 UploadHelperError（消息里写明结果不明），响应丢失由
#     request_with_retry 抛 client.AmbiguousRequest，都不盲重试。上传 body 途中
#     断开也归此类——urllib 会把它包成 URLError，但文件可能已整份到达服务端。
UPLOAD_RETRYABLE_STATUSES = frozenset([429])


class UploadHelperError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def upload_local_file(path: str, keys: list, *, base_url: str = "https://api.aihubmax.com",
                      transport=None, sleep=None, log=None) -> str:
    """Upload a local file via the aihubmax stream endpoint; return the hosted URL.
    Raises UploadHelperError on HTTP failure, client.AmbiguousRequest when the request
    went out but no response came back; lets a retry-exhausted URLError propagate."""
    if transport is None:
        transport = http_request
    with open(path, "rb") as fh:
        file_bytes = fh.read()
    ctype, body = encode_multipart({"auto_cleanup": "true"}, "file", os.path.basename(path), file_bytes)
    url = base_url + "/v1/files/upload/stream"

    def attempt(key):
        headers = {"Content-Type": ctype, "Authorization": "Bearer " + key}
        return request_with_retry(
            lambda: transport("POST", url, headers, body, timeout=UPLOAD_TIMEOUT),
            op="upload_local_file(%s)" % os.path.basename(path),
            write_safety=BILLING_WRITE, retryable_statuses=UPLOAD_RETRYABLE_STATUSES,
            sleep=sleep, log=log)

    resp, _ = call_with_key_fallback(keys, attempt)
    if resp.status == 200 and isinstance(resp.json, dict) and resp.json.get("url"):
        return resp.json["url"]
    if resp.status == 200:
        raise UploadHelperError(200, "[HTTP 200] 上传响应缺少 url 字段: %s" % (resp.text or "")[:200])
    if 500 <= resp.status <= 599:
        hint = _HINTS.get(resp.status,
                          "服务器错误（结果不明：文件可能已存入但没拿到 URL，未自动重试）")
    else:
        hint = _HINTS.get(resp.status, "未预期的响应")
    server = ""
    if isinstance(resp.json, dict) and isinstance(resp.json.get("error"), dict):
        server = resp.json["error"].get("message", "")
    msg = "[HTTP %s] %s" % (resp.status, hint)
    if server:
        msg += " | 上游: " + server
    raise UploadHelperError(resp.status, msg)
