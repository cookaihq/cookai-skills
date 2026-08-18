from __future__ import annotations

import http.client
import json as _json
import math
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple

DEFAULT_BOUNDARY = "----aihubmaxUploadBoundaryXyZ"

# --- 超时分级（ADR 0006 规则 1）------------------------------------------
# 每次网络调用都必须带超时，且按操作类型给不同上限：状态/元数据查询走
# METADATA_TIMEOUT，大文件上传按体量放宽走 UPLOAD_TIMEOUT。调用方可显式覆盖。
METADATA_TIMEOUT = 60   # 模型清单、任务提交、任务轮询
UPLOAD_TIMEOUT = 300    # /v1/files/upload/stream 上传本地媒体

# --- 重试参数（ADR 0006 规则 3）------------------------------------------
MAX_ATTEMPTS = 3        # 首次 + 2 次重试
BACKOFF_BASE = 2        # 指数退避：第 1 次重试前等 1s，第 2 次等 2s

# 429 + 全部 5xx 视为瞬时；确定性 4xx（400/401/403/404/413/422）不重试。
RETRYABLE_STATUS_SET = frozenset([429])

# Retry-After 采信上限：上游给出离谱值（甚至超过整个轮询预算）时不至于把脚本挂死。
RETRY_AFTER_CAP_SECONDS = 60

# --- 写操作安全等级（ADR 0006 规则 4）------------------------------------
IDEMPOTENT = "idempotent"        # 幂等读（GET）或可安全重放的调用
BILLING_WRITE = "billing_write"  # 计费写：只有「确认未受理」的失败才允许重试

# --- 网络失败的阶段分类 ---------------------------------------------------
# 「URLError == 请求没发出去」是错的，不能用来判定重试安全性：CPython 的
# AbstractHTTPHandler.do_open 把 h.request(...) 整段包在同一个 try 里
# （`except OSError as err: raise URLError(err)`），而 h.request 会把请求体一路
# 写进 socket。因此上传大 body 写到一半连接被重置时，抛出的同样是 URLError，
# 此时服务端可能已经收下了完整请求并开始计费。
#
# 能**确定**请求没发出去的只有两类 reason：`socket.gaierror`（DNS 都没解析出来，
# TCP 还没开始）与 `ConnectionRefusedError`（对端明确拒绝建连）。这两类下请求体
# 不可能到达服务端，计费写重试才是安全的。
#
# 超时不在此列：`urlopen(timeout=...)` 对建连超时与读应答超时抛的都是
# `socket.timeout`，异常对象本身不带「卡在哪个阶段」的信息。按 ADR 0006
# 「禁止臆断」的要求这里不猜，一律按结果不明（ambiguous）处理。
# 判定口径与参考实现 pdf2md_docx/scripts/client.py 的 is_connection_stage_failure 一致。
SEND_FAILED = "send_failed"
RESPONSE_LOST = "response_lost"

# 「请求确定未发出」的 URLError.reason 类型白名单。
CONNECTION_STAGE_REASONS = (socket.gaierror, ConnectionRefusedError)

# URLError 是 OSError 子类，单列无扩展意义；HTTPException（BadStatusLine、
# IncompleteRead 等）不是 OSError，漏掉会让这类瞬时故障穿透重试层（同 memoji L2）。
NETWORK_ERRORS = (OSError, http.client.HTTPException)


class AmbiguousRequest(Exception):
    """计费写操作「已发出但结果未知」——不盲重试，交给上层如实报给用户。"""

    def __init__(self, op: str, cause: BaseException):
        super().__init__("%s：请求已发出但未收到响应（%s）" % (op, cause))
        self.op = op
        self.cause = cause


def classify_network_error(exc: BaseException) -> str:
    """把网络异常分成「请求确定未发出」与「结果不明」两类（依据见上面注释）。

    只有 `URLError.reason` 属于 CONNECTION_STAGE_REASONS 才判 SEND_FAILED；
    其余一切（含所有超时、连接重置、以及发送 body 途中失败的 URLError）都判
    RESPONSE_LOST，让计费写走 AmbiguousRequest 而不是被重发。
    """
    if isinstance(exc, urllib.error.HTTPError):
        # 拿到了完整 HTTP 响应，不属于网络失败；http_request 已把它转成 Resp，
        # 正常路径不会走到这里，兜底按「已发出」处理。
        return RESPONSE_LOST
    reason = getattr(exc, "reason", None)
    if reason is None:
        # 不是 URLError：错误发生在读应答阶段，请求必然已经发出去了。
        return RESPONSE_LOST
    if isinstance(reason, CONNECTION_STAGE_REASONS):
        return SEND_FAILED
    return RESPONSE_LOST


def is_transient_status(status: int) -> bool:
    """HTTP 层的瞬时错误：429 与全部 5xx（ADR 0006 规则 2）。"""
    return status in RETRYABLE_STATUS_SET or 500 <= status <= 599


def retry_after_seconds(resp) -> "float | None":
    """429 带 Retry-After 时按该值等待，而不是退避公式（ADR 0006 规则 3）。

    只认秒数形式；HTTP-date 形式与非法值一律回落到退避公式（返回 None）。
    采信值钳到 RETRY_AFTER_CAP_SECONDS：上游给出 86400 之类的值时照单全收会把
    脚本挂死一整天。`nan` / `inf` 能通过 float() 但不是可用的等待时长
    （`min(nan, 60)` 返回 nan，`sleep(nan)` 直接崩），先用 math.isfinite 挡掉。
    """
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, RETRY_AFTER_CAP_SECONDS)


def _default_log(msg: str) -> None:
    # 日志只写 URL/状态码/等待秒数，不含 Authorization 头（凭证掩码，ADR 0003）。
    print(msg, file=sys.stderr)


def request_with_retry(call, *, op: str, write_safety: str = IDEMPOTENT,
                       max_attempts: int = MAX_ATTEMPTS,
                       retryable_statuses=None, sleep=None, log=None):
    """执行 `call()`（返回 Resp）并按 ADR 0006 处理瞬时故障。

    - `write_safety=IDEMPOTENT`：瞬时状态码（默认 429/5xx）与任何网络异常都重试。
    - `write_safety=BILLING_WRITE`：只有「请求确定未发出」（DNS 解析失败 / 连接被
      拒，见 `classify_network_error`）与调用方显式列入 `retryable_statuses` 的
      状态码才重试；其余网络失败（含全部超时）结果不明，抛 `AmbiguousRequest`，
      绝不盲重试（避免重复扣费）。
    - 重试次数耗尽后：状态码类返回最后一次 Resp（由调用方按业务报错），异常类
      原样抛出最后一次异常。
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

# api.aihubmax.com sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" User-Agent with HTTP 403 / "error code: 1010" (banned
# browser signature) — observed on the file-upload routes. Sending a browser-like
# UA clears that gate. Callers may override by passing their own User-Agent header.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# headers 供 retry_after_seconds() 读 429 的 Retry-After；默认 None 让测试与旧
# 调用方仍可用三元组构造 Resp(status, json, text)。
Resp = namedtuple("Resp", "status json text headers")
Resp.__new__.__defaults__ = (None,)


def encode_multipart(fields: dict, file_field: str, filename: str,
                     file_bytes: bytes, boundary: str = DEFAULT_BOUNDARY) -> tuple[str, bytes]:
    """Build a multipart/form-data body. Returns (content_type, body_bytes)."""
    b = boundary.encode()
    crlf = b"\r\n"
    chunks = []
    for name, value in fields.items():
        chunks.append(b"--" + b + crlf)
        chunks.append(('Content-Disposition: form-data; name="%s"' % name).encode() + crlf)
        chunks.append(crlf)
        chunks.append(str(value).encode() + crlf)
    chunks.append(b"--" + b + crlf)
    chunks.append(
        ('Content-Disposition: form-data; name="%s"; filename="%s"' % (file_field, filename)).encode() + crlf
    )
    chunks.append(b"Content-Type: application/octet-stream" + crlf)
    chunks.append(crlf)
    chunks.append(file_bytes + crlf)
    chunks.append(b"--" + b + b"--" + crlf)
    return "multipart/form-data; boundary=" + boundary, b"".join(chunks)


def http_request(method: str, url: str, headers: dict,
                 body: "bytes | None" = None, timeout: int = METADATA_TIMEOUT) -> Resp:
    """Perform an HTTP request. Returns Resp(status, json, text, headers) for any
    HTTP status (including 4xx/5xx). Raises urllib.error.URLError when the request
    could not be sent, and socket.timeout / OSError when the response was lost —
    `classify_network_error` tells the two apart. Retry policy is NOT applied here;
    wrap the call in `request_with_retry` (ADR 0006)."""
    if not any(h.lower() == "user-agent" for h in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
            resp_headers = r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        resp_headers = e.headers
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    return Resp(status, parsed, text, resp_headers)


def call_with_key_fallback(keys: list, attempt) -> tuple[Resp, str]:
    """Try each key via attempt(key)->Resp. Advance to the next key ONLY on HTTP
    401 (auth error; 401 does not consume credits). Any other status (or success)
    stops immediately. Returns (Resp, used_key). Raises ValueError if no keys."""
    if not keys:
        raise ValueError("no API key available (AIHUB_API_KEY not found)")
    last = None
    for k in keys:
        last = attempt(k)
        if last.status != 401:
            return last, k
    return last, keys[-1]
