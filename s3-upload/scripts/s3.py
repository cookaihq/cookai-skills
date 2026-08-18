from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Dict, Optional, Sequence, Tuple

from config import Connection, mask_access_key


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class SignedRequest:
    method: str
    url: str
    headers: Dict[str, str]
    body: bytes
    canonical_request: str


@dataclass(frozen=True)
class IdentifierResult:
    classification: str
    value: Optional[str]


class TransportError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 网络抖动处理（ADR 0006）——只覆盖**读语义**调用。
#
# 为什么写操作不在这里重试（ADR 0006 规则 4 + 规则 6 偏离记录）：
# 本 skill 的写调用（PutObject / DeleteObject / multipart 的
# CreateMultipartUpload、UploadPart、CompleteMultipartUpload、AbortMultipartUpload）
# 在传输异常时一律置 `*_unknown` 检查点并返回 `ambiguous`，交由调用方走 reconcile
# 判定实际落地情况——这正是 ADR 0006 规则 4 引用本 skill 为先例的那套语义，不动。
#
# 「连接建立阶段失败（请求未发出）」为什么没有从 ambiguous 里拆出来允许重试：
# 拆分需要在异常里能读出「失败发生在连接建立阶段还是请求已发出之后」，而这条链路
# 读不出来——
#   1. 生产 transport `http_request` 把 URLError / OSError 一律压平成
#      `TransportError(str(reason))`，异常类型信息只剩一个字符串（这是 s3.py 既有
#      的刻意设计，见 open_body_stream 的 docstring）；`__cause__` 里虽还留着原始
#      异常，但——
#   2. transport 是**可注入**的（live_adapter、delivery_workflow、全部测试矩阵都
#      注入自己的实现），注入方直接 `raise TransportError(...)` 时根本没有
#      `__cause__`，分类在最需要它的非 urllib 路径上恰好失效；
#   3. 即使拿到原始异常，urllib 也只有 `socket.gaierror`（DNS）与
#      `ConnectionRefusedError` 是确定「请求未发出」的；**连接超时与读应答超时抛的
#      都是 `socket.timeout`，异常对象不带阶段信息**，而超时恰恰是这里最常见的失败
#      形态。
# 三条合起来：能精确拆出的只是一个很小且罕见的子集，代价是把「重复上传 / 重复删除
# 不可发生」这条安全契约的判定依据从「异常一律 ambiguous」改成「依赖异常携带的可选
# 信息」。按 ADR 0006「禁止臆断，以代码实际能力为准」，此处**保持现状**，本注释即
# 规则 6 要求的偏离说明。
#
# 读路径的**两条**瞬时判据（ADR 0006 规则 2 把 HTTP 5xx 与 429 也列为瞬时）：
#   a. 抛出来的传输异常 —— `is_transient_transport_error` 分类；
#   b. **正常返回的 Response 上的状态码** —— `is_transient_read_status` 分类。
# 判据 b 不能省：`http_request` 把包括 5xx / 429 在内的所有 HTTP 状态都转成
# `Response` 正常返回（只有真正的网络故障才抛异常），所以 5xx / 429 永远进不了
# except 分支。只按判据 a 重试等于对「服务端限流 / 短暂不可用」这两类最典型的瞬时
# 失败完全不生效。判据 b 只作用于**读语义**调用，写路径不受影响，仍是「一次物理
# 请求 + ambiguous」。
# --------------------------------------------------------------------------- #

# ADR 0006 规则 3 默认参数：单次逻辑调用总尝试 3 次，第 n 次重试前等 2^(n-1) 秒。
NET_MAX_ATTEMPTS = 3

# ADR 0006 规则 3：429 带 `Retry-After` 时遵循该值而非退避公式。上限钳到 60 秒——
# 服务端可以给出任意大的值，无上限会把一次 CLI 调用挂死到用户无法判断是否卡死。
NET_RETRY_AFTER_CAP_SECONDS = 60


def is_transient_transport_error(exc) -> bool:
    """传输异常是否算瞬时（ADR 0006 规则 2）。

    `http_request` 已把所有 HTTP 状态（含 4xx/5xx）转成 `Response` 返回，能作为
    异常抛到调用层的只剩真正的网络故障：`TransportError`（生产 transport 对
    URLError / OSError 的压平）、`URLError`、`http.client.HTTPException`
    （RemoteDisconnected / IncompleteRead 等）、`OSError`（连接超时、connection
    reset、DNS 解析失败、TLS 握手失败）。

    其余异常（注入 transport 抛出的断言、`ValueError` 等编程错误）不算瞬时，
    不重试——重试只会同样失败，还会掩盖真实缺陷。
    """
    from http.client import HTTPException

    return isinstance(exc, (TransportError, URLError, HTTPException, OSError))


def is_transient_read_status(status: int) -> bool:
    """HTTP 状态码是否算瞬时（ADR 0006 规则 2）。

    5xx（服务端短暂不可用）与 429（限流）算瞬时；其余状态——尤其是 403 / 404 /
    400 这类确定性 4xx——是确定性结果，立即返回给调用方按语义判定，重试只会同样
    失败。仅用于**读语义**调用。
    """
    return status == 429 or 500 <= status <= 599


def retry_after_seconds(response: Response) -> Optional[int]:
    """读 `Retry-After` 响应头，返回钳到 [0, 60] 的秒数；没有 / 读不懂时返回 None。

    只认 RFC 9110 的 delta-seconds 形式（S3 兼容服务端限流一律用这种）。HTTP-date
    形式需要引入「现在几点」这个额外依赖才能换算，而本函数的调用方是纯离线可测的
    重试循环——读不懂就返回 None，退回指数退避公式，不臆断。
    """
    headers = response.headers or {}
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() != "retry-after":
            continue
        try:
            seconds = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return max(0, min(seconds, NET_RETRY_AFTER_CAP_SECONDS))
    return None


def _default_retry_log(line: str) -> None:
    """重试日志走 stderr：stdout 是 URL / 结构化 JSON 结果，不能被污染。"""
    import sys

    print(line, file=sys.stderr, flush=True)


def read_request_with_retry(
    transport,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: bytes,
    *,
    sleep=None,
    log=_default_retry_log,
):
    """执行一次**读语义**请求（HEAD / GET / List），瞬时失败按 ADR 0006 重试。

    只允许用于读：读没有远端副作用，重试天然安全（规则 4 对写操作的幂等前提在读上
    自动成立）。

    两类瞬时失败都会重试：抛出的传输异常（`is_transient_transport_error`），以及
    正常返回但状态码为 5xx / 429 的应答（`is_transient_read_status`）——后者必须
    单独判，因为 `http_request` 把所有 HTTP 状态都转成 `Response` 返回，5xx / 429
    根本不会走到 except。429 带 `Retry-After` 时按该值等待（钳到 60 秒），否则按
    指数退避。

    返回值：拿到确定性应答（含重试穷尽后的最后一个 5xx / 429）返回该 `Response`，
    让调用方按状态码走既有判定；传输异常穷尽 / 非瞬时异常返回 `None`——`None` 正是
    各调用点既有的「这次观测没拿到答案」约定，因此重试是纯增量，不改变任何既有的
    ambiguous / unknown 判定。
    """
    wait = time.sleep if sleep is None else sleep
    for attempt in range(1, NET_MAX_ATTEMPTS + 1):
        try:
            response = transport(method, url, headers, body)
        except Exception as exc:  # noqa: BLE001 - 分类交给 is_transient_transport_error
            if not is_transient_transport_error(exc) or attempt == NET_MAX_ATTEMPTS:
                return None
            delay = 2 ** (attempt - 1)
            reason = type(exc).__name__
        else:
            if (
                not is_transient_read_status(response.status)
                or attempt == NET_MAX_ATTEMPTS
            ):
                return response
            after = retry_after_seconds(response)
            delay = 2 ** (attempt - 1) if after is None else after
            reason = "HTTP %d" % response.status
        if log is not None:
            # 日志只打方法与失败原因：URL 含预签名 query，绝不入日志。
            log(
                "[s3-upload] %s read retry %d/%d after %s; waiting %ds"
                % (method, attempt, NET_MAX_ATTEMPTS, reason, delay)
            )
        wait(delay)
    return None


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _strict_percent_decode(value: str) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            decoded.append(ord(character))
            index += 1
            continue
        if index + 2 >= len(value) or not re.fullmatch(
            r"[0-9A-Fa-f]{2}", value[index + 1:index + 3]
        ):
            raise ValueError("invalid percent escape")
        decoded.append(int(value[index + 1:index + 3], 16))
        index += 3
    return bytes(decoded)


def parse_provider_identifier(
    value: object,
    *,
    active_credentials: Sequence[str],
) -> IdentifierResult:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return IdentifierResult("identifier_rejected", None)
    if any(not 0x21 <= ord(character) <= 0x7E for character in value):
        return IdentifierResult("identifier_rejected", None)
    try:
        decoded = _strict_percent_decode(value)
        credential_bytes = tuple(
            credential.encode("ascii")
            for credential in active_credentials
            if credential
        )
    except (UnicodeEncodeError, ValueError):
        return IdentifierResult("identifier_rejected", None)
    raw = value.encode("ascii")
    if any(
        credential in raw or credential in decoded
        for credential in credential_bytes
    ):
        return IdentifierResult("identifier_rejected", None)
    return IdentifierResult("accepted", value)


def encode_key(key: str) -> str:
    return quote(key, safe="/-_.~", encoding="utf-8", errors="strict")


def object_url(conn: Connection, key: str) -> str:
    encoded = encode_key(key)
    p = urlsplit(conn.endpoint)
    if conn.addressing == "bucket-bound":
        return conn.endpoint.rstrip("/") + "/" + encoded
    if conn.addressing == "virtual":
        host = f"{conn.bucket}.{p.hostname}"
        if p.port:
            host += f":{p.port}"
        return urlunsplit((p.scheme, host, "/" + encoded, "", ""))
    return (
        conn.endpoint.rstrip("/")
        + "/"
        + quote(conn.bucket, safe="-_.~")
        + "/"
        + encoded
    )


def public_url(base: str, key: str) -> str:
    return base.rstrip("/") + "/" + encode_key(key)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()

def _signing_key(secret: str, date: str, region: str) -> bytes:
    return _sign(
        _sign(_sign(_sign(("AWS4" + secret).encode(), date), region), "s3"),
        "aws4_request",
    )


def _now(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def build_signed_request(
    conn: Connection,
    *,
    method: str,
    key: str,
    query: Sequence[Tuple[str, str]] = (),
    body: bytes = b"",
    headers: Sequence[Tuple[str, str]] = (),
    payload_hash: Optional[str] = None,
    payload_header: str = "x-amz-content-sha256",
    now: Optional[datetime] = None,
) -> SignedRequest:
    if not isinstance(method, str) or not re.fullmatch(r"[A-Z]+", method):
        raise ValueError("invalid method")
    moment = _now(now)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = moment.strftime("%Y%m%d")
    base_url = object_url(conn, key)
    parts = urlsplit(base_url)
    encoded_query = sorted(
        (quote(name, safe="-_.~"), quote(value, safe="-_.~"))
        for name, value in query
    )
    canonical_query = "&".join(f"{name}={value}" for name, value in encoded_query)
    url = base_url + ("?" + canonical_query if canonical_query else "")
    if payload_hash is None:
        payload_hash = hashlib.sha256(body).hexdigest()
    elif not (
        re.fullmatch(r"[0-9a-f]{64}", payload_hash)
        or payload_hash in {
            "UNSIGNED-PAYLOAD",
            "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
            "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
        }
    ):
        raise ValueError("invalid payload hash")
    if payload_header not in {
        "x-amz-content-sha256",
        "x-oss-content-sha256",
    }:
        raise ValueError("invalid payload hash header")
    signed: Dict[str, str] = {}
    reserved_headers = {
        "authorization",
        "host",
        "x-amz-content-sha256",
        "x-amz-date",
        "x-amz-security-token",
        "x-oss-content-sha256",
    }
    for raw_name, raw_value in headers:
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise ValueError("invalid header")
        name = raw_name.lower()
        if name in reserved_headers:
            raise ValueError(f"reserved header: {name}")
        if name in signed:
            raise ValueError(f"duplicate header: {name}")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name):
            raise ValueError("invalid header name")
        if any(
            (ord(character) < 0x20 and character != "\t")
            or ord(character) > 0x7E
            for character in raw_value
        ):
            raise ValueError(f"invalid header value: {name}")
        signed[name] = re.sub(r"[ \t]+", " ", raw_value.strip(" \t"))
    signed.update(
        {
            "host": parts.netloc,
            payload_header: payload_hash,
            "x-amz-date": amz_date,
        }
    )
    if conn.session_token:
        signed["x-amz-security-token"] = conn.session_token
    names = sorted(signed)
    canonical_headers = "".join(
        f"{name}:{signed[name].strip()}\n" for name in names
    )
    signed_headers = ";".join(names)
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{date}/{conn.region}/s3/aws4_request"
    string = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(conn.secret_access_key, date, conn.region),
        string.encode(),
        hashlib.sha256,
    ).hexdigest()
    signed["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={conn.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return SignedRequest(
        method=method,
        url=url,
        headers=signed,
        body=body,
        canonical_request=canonical,
    )

def build_put_request(
    conn: Connection,
    key: str,
    body: bytes,
    content_type: str,
    now: Optional[datetime] = None,
) -> Tuple[str, Dict[str, str], bytes]:
    request = build_signed_request(
        conn,
        method="PUT",
        key=key,
        body=body,
        headers=(("content-length", str(len(body))), ("content-type", content_type)),
        now=now,
    )
    return request.url, request.headers, request.body


def presign_get(
    conn: Connection,
    key: str,
    expires: int,
    now: Optional[datetime] = None,
) -> str:
    moment = _now(now)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = moment.strftime("%Y%m%d")
    url = object_url(conn, key)
    parts = urlsplit(url)
    scope = f"{date}/{conn.region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{conn.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if conn.session_token:
        params["X-Amz-Security-Token"] = conn.session_token
    query = "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
        for name, value in sorted(params.items())
    )
    canonical = "\n".join(
        [
            "GET",
            parts.path or "/",
            query,
            f"host:{parts.netloc}\n",
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(conn.secret_access_key, date, conn.region),
        string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return url + "?" + query + "&X-Amz-Signature=" + signature


def http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: bytes,
    timeout: int = 30,
) -> Response:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(_NoRedirectHandler()).open(
            request, timeout=timeout
        ) as response:
            return Response(response.status, response.read(8192), dict(response.headers))
    except HTTPError as exc:
        return Response(exc.code, exc.read(8192), dict(exc.headers or {}))
    except (URLError, OSError) as exc:
        raise TransportError(str(exc.reason if isinstance(exc, URLError) else exc)) from exc


def open_body_stream(method: str, url: str, headers: Dict[str, str],
                     timeout: int = 30):
    """Open a response for streaming, without reading it into memory.

    http_request() is deliberately capped at 8 KiB of body: it exists to
    classify a response, not to read one. A full-body verification cannot use
    it and must not raise that cap, so it gets its own entry point sharing the
    one redirect policy: _NoRedirectHandler refuses every 3xx, which surfaces
    as an HTTPError the caller has to classify.

    Unlike http_request this converts nothing. http_request turns an HTTPError
    into a Response so callers can branch on a status code, and a URLError
    into a TransportError; here the caller is a verifier that must treat a
    3xx, a 4xx and a dead socket as the same answer -- no full body was read
    -- so flattening them into a status here would only make it re-derive the
    distinction it does not want.
    """
    request = Request(url, data=None, headers=headers, method=method)
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def put_object(
    conn: Connection,
    key: str,
    body: bytes,
    content_type: str,
    *,
    transport=http_request,
    now: Optional[datetime] = None,
) -> Response:
    url, headers, payload = build_put_request(conn, key, body, content_type, now)
    response = transport("PUT", url, headers, payload)
    if not 200 <= response.status < 300:
        # Redact before truncating so a credential crossing the output boundary
        # cannot leave a visible prefix in stderr.
        text = response.body.decode("utf-8", "replace")
        replacements = sorted(
            [
                (conn.secret_access_key, "****"),
                (conn.session_token, "****"),
                (conn.access_key_id, mask_access_key(conn.access_key_id)),
            ],
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for credential, replacement in replacements:
            if credential:
                text = text.replace(credential, replacement)
                # The transport intentionally bounds response bodies. If that
                # bound cuts a reflected credential, remove its trailing prefix.
                for length in range(len(credential) - 1, 0, -1):
                    if text.endswith(credential[:length]):
                        text = text[:-length] + "****"
                        break
        text = text[:2000]
        raise TransportError(f"HTTP {response.status}: {text}")
    return response
