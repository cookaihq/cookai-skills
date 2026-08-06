from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from collections import namedtuple


DEFAULT_BOUNDARY = "----aihubmaxUploadBoundaryXyZ"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

Resp = namedtuple("Resp", "status json text")


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
    timeout: int = 60,
) -> Resp:
    """Return a response for HTTP statuses; leave network failures to the caller."""
    if not any(header.lower() == "user-agent" for header in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    return Resp(status, parsed, text)


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
