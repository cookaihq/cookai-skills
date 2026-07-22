from __future__ import annotations

import hashlib
import hmac
import re
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
