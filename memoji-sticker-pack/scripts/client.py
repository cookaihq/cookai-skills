from __future__ import annotations

import http.client
import math
import os
import sys
import time
import urllib.error
import urllib.request
import json as _json
from collections import namedtuple

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）。本文件是被 import 的模块，
# 正常路径下解释器已由入口拉进 <skill>/.venv；这里的守卫只对「直接执行本文件」
# 生效，保证任何执行方式都落在同一个 venv 里。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()


DEFAULT_BOUNDARY = "----aihubmaxUploadBoundaryXyZ"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- 超时分级（ADR 0006 规则 1）------------------------------------------
METADATA_TIMEOUT = 60   # 元数据 / 状态查询
UPLOAD_TIMEOUT = 300    # 文件上传（照片、基准图）

# --- 重试参数（ADR 0006 规则 3）------------------------------------------
MAX_ATTEMPTS = 3        # 首次 + 2 次重试
BACKOFF_BASE = 2        # 指数退避：第 1 次重试前等 1s，第 2 次等 2s
MAX_RETRY_AFTER_SECONDS = 120

# --- 写操作安全等级（ADR 0006 规则 4）------------------------------------
IDEMPOTENT = "idempotent"        # 幂等读（GET）
BILLING_WRITE = "billing_write"  # 写操作：只有「确认未受理」的失败才允许重试

# --- 网络失败的阶段分类 ---------------------------------------------------
# urllib 的结构性事实（CPython Lib/urllib/request.py AbstractHTTPHandler.do_open）：
# 连接建立与请求发送阶段的 OSError 被包成 urllib.error.URLError 抛出；等待/读取
# 响应阶段的超时与连接中断则原样抛出 socket.timeout / TimeoutError /
# ConnectionResetError。因此：
#   URLError          → 请求没能发出去 → 写操作也可安全重试
#   其他 OSError 子类 → 请求已发出但没拿到响应 → 结果不明，禁止盲重试
SEND_FAILED = "send_failed"
RESPONSE_LOST = "response_lost"

# URLError 是 OSError 的子类，所以 (URLError, OSError) 等价于只写 OSError——真正
# 漏掉的是 http.client 家族：BadStatusLine / IncompleteRead / RemoteDisconnected
# 等不继承 OSError，属于连接被中途掐断的典型形态，不列进来就会以未捕获异常穿透
# 重试层直接崩掉。
NETWORK_ERRORS = (OSError, http.client.HTTPException)

# headers 供 retry_after_seconds() 读 429 的 Retry-After；默认 None 让测试与旧
# 调用方仍可用三元组构造 Resp(status, json, text)。
Resp = namedtuple("Resp", "status json text headers")
Resp.__new__.__defaults__ = (None,)


class AmbiguousRequest(Exception):
    """写操作「已发出但结果未知」——不盲重试，交给上层如实报给用户。"""

    def __init__(self, op: str, cause: BaseException):
        super().__init__("%s：请求已发出但未收到响应（%s）" % (op, cause))
        self.op = op
        self.cause = cause


def classify_network_error(exc: BaseException) -> str:
    """把网络异常分成「请求未发出」与「响应丢失」两类（依据见上面注释）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return RESPONSE_LOST
    if isinstance(exc, urllib.error.URLError):
        return SEND_FAILED
    return RESPONSE_LOST


def is_transient_status(status: int) -> bool:
    """HTTP 层的瞬时错误：429 与全部 5xx（ADR 0006 规则 2）。"""
    return status == 429 or 500 <= status <= 599


def retry_after_seconds(resp) -> "float | None":
    """429 带 Retry-After 时按该值等待（只认秒数形式，且有上限）。

    `float()` 接受 "nan" / "inf"，而 `min(nan, 120)` 返回 nan、`sleep(nan)` 直接
    抛异常把脚本打断，所以先用 math.isfinite 挡掉再钳制。
    """
    headers = getattr(resp, "headers", None)
    if not headers or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _default_log(msg: str) -> None:
    # 日志只写操作名/状态码/等待秒数，不含 Authorization 头（ADR 0003 掩码）。
    print(msg, file=sys.stderr)


def request_with_retry(call, *, op: str, write_safety: str = IDEMPOTENT,
                       max_attempts: int = MAX_ATTEMPTS,
                       retryable_statuses=None, sleep=None, log=None):
    """执行 `call()`（返回 Resp）并按 ADR 0006 处理瞬时故障。

    - `write_safety=IDEMPOTENT`：瞬时状态码（429/5xx）与任何网络异常都重试。
    - `write_safety=BILLING_WRITE`：只有「请求未发出」（URLError）与调用方显式
      列入 `retryable_statuses` 的状态码才重试；「请求已发出但响应丢失」抛
      `AmbiguousRequest`，绝不盲重试。
    """
    if sleep is None:
        sleep = time.sleep
    if log is None:
        log = _default_log
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = call()
        except NETWORK_ERRORS as exc:
            stage = classify_network_error(exc)
            if write_safety == BILLING_WRITE and stage == RESPONSE_LOST:
                raise AmbiguousRequest(op, exc)
            if attempt >= max_attempts:
                raise
            wait = BACKOFF_BASE ** (attempt - 1)
            log("[retry] %s 第 %d/%d 次失败（%s: %s），%s 秒后重试"
                % (op, attempt, max_attempts, stage, exc, wait))
            sleep(wait)
            continue
        if retryable_statuses is None:
            transient = is_transient_status(resp.status)
        else:
            transient = resp.status in retryable_statuses
        if not transient or attempt >= max_attempts:
            return resp
        wait = retry_after_seconds(resp)
        reason = "HTTP %s（Retry-After）" % resp.status
        if wait is None:
            wait = BACKOFF_BASE ** (attempt - 1)
            reason = "HTTP %s" % resp.status
        log("[retry] %s 第 %d/%d 次返回 %s，%s 秒后重试"
            % (op, attempt, max_attempts, reason, wait))
        sleep(wait)
    return resp


def encode_multipart(
    fields: dict,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    boundary: str = DEFAULT_BOUNDARY,
) -> tuple[str, bytes]:
    """Build a multipart/form-data body and return its content type and bytes."""
    boundary_bytes = boundary.encode()
    crlf = b"\r\n"
    chunks = []
    for name, value in fields.items():
        chunks.append(b"--" + boundary_bytes + crlf)
        chunks.append(
            ('Content-Disposition: form-data; name="%s"' % name).encode() + crlf
        )
        chunks.append(crlf)
        chunks.append(str(value).encode() + crlf)
    chunks.append(b"--" + boundary_bytes + crlf)
    chunks.append(
        (
            'Content-Disposition: form-data; name="%s"; filename="%s"'
            % (file_field, filename)
        ).encode()
        + crlf
    )
    chunks.append(b"Content-Type: application/octet-stream" + crlf)
    chunks.append(crlf)
    chunks.append(file_bytes + crlf)
    chunks.append(b"--" + boundary_bytes + b"--" + crlf)
    return "multipart/form-data; boundary=" + boundary, b"".join(chunks)


def http_request(
    method: str,
    url: str,
    headers: dict,
    body: "bytes | None" = None,
    timeout: int = METADATA_TIMEOUT,
) -> Resp:
    """Return a response for HTTP statuses; leave network failures to the caller.

    URLError 表示请求没能发出去，其他 OSError 表示响应丢失（见 classify_network_error）。
    重试策略不在这里，由 `request_with_retry` 包裹调用（ADR 0006）。
    """
    if not any(header.lower() == "user-agent" for header in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            response_headers = response.headers
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
        response_headers = error.headers
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    return Resp(status, parsed, text, response_headers)


def call_with_key_fallback(keys: list, attempt) -> tuple[Resp, str]:
    """Advance through API keys only when the provider returns HTTP 401."""
    if not keys:
        raise ValueError("no API key available (AIHUB_API_KEY not found)")
    last = None
    for key in keys:
        last = attempt(key)
        if last.status != 401:
            return last, key
    return last, keys[-1]
