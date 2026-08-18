from __future__ import annotations

import json as _json
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple

DEFAULT_BOUNDARY = "----aihubmaxUploadBoundaryXyZ"

# api.aihubmax.com sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" User-Agent with HTTP 403 / "error code: 1010" (banned
# browser signature). Sending a browser-like UA clears that gate. Callers may
# override by passing their own User-Agent header.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

Resp = namedtuple("Resp", "status json text headers", defaults=(None,))


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
                 body: "bytes | None" = None, timeout: int = 60) -> Resp:
    """Perform an HTTP request. Returns Resp(status, json, text) for any HTTP
    status (including 4xx/5xx). Raises urllib.error.URLError only on network
    failure (caller treats that as fatal, not a key-fallback trigger)."""
    if not any(h.lower() == "user-agent" for h in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
            resp_headers = dict(r.headers.items())
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        resp_headers = dict(e.headers.items()) if e.headers else {}
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    return Resp(status, parsed, text, resp_headers)


# --------------------------------------------------------------------------- #
# 网络抖动处理（ADR 0006）：分类 → 重试 → 退避，写操作按幂等性区别对待。
# --------------------------------------------------------------------------- #

# 规则 3 默认参数：总尝试 3 次（首次 + 2 次重试），第 n 次重试前等 2^(n-1) 秒。
NET_MAX_ATTEMPTS = 3
# 规则 2：HTTP 5xx / 429 算瞬时。
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Retry-After 上限：上游给出离谱值时不至于把脚本挂死。
RETRY_AFTER_CAP_SECONDS = 60


class AmbiguousWrite(Exception):
    """写操作结果不明（请求已发出但没拿到应答）——ADR 0006 规则 4：禁止盲重试。"""

    def __init__(self, op: str, cause):
        super().__init__("%s：请求已发出但未收到应答（%s）" % (op, cause))
        self.op = op
        self.cause = cause


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def backoff_seconds(attempt: int) -> int:
    """attempt 为刚失败的那次尝试序号（1 起）；返回本次重试前的等待秒数。"""
    return 2 ** (attempt - 1)


def is_transient_network_error(exc) -> bool:
    """网络层异常是否算瞬时（ADR 0006 规则 2）。

    `http_request` 已把所有 HTTP 状态（含 4xx/5xx）转成 Resp 返回，所以能走到
    这里的只剩真正的网络故障：DNS 解析失败、连接被拒/重置、TLS 握手失败、
    读写超时。它们一律算瞬时。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    # 读响应体阶段的超时/中断不经 urllib 包装，直接是 OSError 家族
    # （socket.timeout 自 Python 3.3 起即 OSError 子类）。
    return isinstance(exc, (socket.timeout, OSError))


def is_connection_stage_failure(exc) -> bool:
    """请求是否**确定未被服务端处理**——只有这种失败才允许对计费写操作重试。

    urllib 的异常形态能精确区分的只有两类：`URLError.reason` 是
    `socket.gaierror`（DNS 解析失败，连 TCP 都没开始）或 `ConnectionRefusedError`
    （对端明确拒绝连接）。这两类下请求体不可能到达服务端，重试安全。

    **超时不在此列**：`urlopen(timeout=...)` 对建连超时和读应答超时抛的都是
    `socket.timeout`，异常对象本身不带「卡在哪个阶段」的信息，无法区分「还没连上」
    与「已发出、正在等应答」。按 ADR 0006「禁止臆断」的要求，这里不猜，一律按
    结果不明（ambiguous）处理。
    """
    reason = getattr(exc, "reason", None)
    if reason is None:
        # 不是 URLError：错误发生在读应答阶段，请求必然已经发出去了。
        return False
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def _retry_after_seconds(resp) -> "float | None":
    """解析 429 的 Retry-After（秒数形式）；缺失或非法返回 None（ADR 0006 规则 3）。"""
    headers = resp.headers or {}
    raw = None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            raw = v
            break
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None  # HTTP-date 形式不解析，退回指数退避
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def request_with_retry(method: str, url: str, headers: dict, body=None,
                       timeout: int = 60, *, idempotent: bool, op: str = "请求",
                       transport=None, sleep=None) -> Resp:
    """带瞬时错误分类 + 3 次尝试 + 指数退避的 HTTP 调用（ADR 0006 规则 2/3/4）。

    `idempotent=True`（幂等读：轮询、下载）——瞬时网络异常与 5xx/429 都重试。

    `idempotent=False`（计费写：创建任务、上传文件）——只重试两类**确定未被服务端
    处理**的失败：
      1. HTTP 429：服务端按限流**拒绝**了请求，任务没建、不扣费，重试安全（若带
         Retry-After 则遵循该值）；
      2. 连接阶段失败（DNS 解析失败 / 连接被拒），见 `is_connection_stage_failure`。
    其余网络失败（尤其是超时）结果不明，抛 `AmbiguousWrite`，由调用方提示用户去查
    任务状态——盲重试会重复创建任务、重复扣费。5xx 同样不重试：服务端已收到请求，
    是否已落库无法确认。
    """
    if transport is None:
        transport = http_request
    if sleep is None:
        sleep = time.sleep

    for attempt in range(1, NET_MAX_ATTEMPTS + 1):
        try:
            resp = transport(method, url, headers, body, timeout=timeout)
        except Exception as exc:
            if not is_transient_network_error(exc):
                raise
            if not idempotent and not is_connection_stage_failure(exc):
                raise AmbiguousWrite(op, exc)
            if attempt == NET_MAX_ATTEMPTS:
                raise
            delay = backoff_seconds(attempt)
            log("[retry] %s 第 %d/%d 次尝试失败（网络瞬时故障: %s），%ss 后重试"
                % (op, attempt, NET_MAX_ATTEMPTS, exc, delay))
            sleep(delay)
            continue

        retryable = resp.status in RETRYABLE_STATUSES if idempotent else resp.status == 429
        if not retryable or attempt == NET_MAX_ATTEMPTS:
            return resp
        delay = _retry_after_seconds(resp) if resp.status == 429 else None
        reason = "HTTP %s" % resp.status
        if delay is None:
            delay = backoff_seconds(attempt)
        else:
            reason += "，遵循 Retry-After"
        log("[retry] %s 第 %d/%d 次尝试失败（%s），%ss 后重试"
            % (op, attempt, NET_MAX_ATTEMPTS, reason, delay))
        sleep(delay)
    raise AssertionError("unreachable")  # 循环内每条分支都 return / raise / continue


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
