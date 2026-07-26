from __future__ import annotations

import hashlib
import http.client
import os
import re
import stat
from dataclasses import dataclass
from urllib.parse import urlsplit

import strict_json


UPLOAD_URL = "https://api.aihubmax.com/v1/files/upload/stream"
UPLOAD_HOST = "api.aihubmax.com"
UPLOAD_PATH = "/v1/files/upload/stream"
STREAM_CHUNK_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 60


class UploadError(ValueError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


@dataclass(frozen=True)
class UploadResult:
    state: str
    http_status: int | None
    reason_code: str | None
    url: str | None
    url_sha256: str | None


class MultipartBody:
    def __init__(self, descriptor: int, *, size_bytes: int, source_sha256: str):
        boundary = f"pdf2markdown-{source_sha256}"
        self.content_type = f"multipart/form-data; boundary={boundary}"
        marker = boundary.encode("ascii")
        self._prefix = (
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="auto_cleanup"'
            + b"\r\n\r\nfalse\r\n--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="file"; filename="source.pdf"'
            + b"\r\nContent-Type: application/pdf\r\n\r\n"
        )
        self._suffix = b"\r\n--" + marker + b"--\r\n"
        self._descriptor = descriptor
        self._size_bytes = size_bytes
        self._source_sha256 = source_sha256
        self._iterated = False
        self.completed = False
        self.content_length = len(self._prefix) + size_bytes + len(self._suffix)

    def __iter__(self):
        if self._iterated:
            raise UploadError("multipart body cannot be replayed")
        self._iterated = True
        yield self._prefix
        remaining = self._size_bytes
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(self._descriptor, min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                raise UploadError("source PDF ended before its recorded size")
            remaining -= len(chunk)
            digest.update(chunk)
            yield chunk
        if os.read(self._descriptor, 1):
            raise UploadError("source PDF exceeded its recorded size")
        if digest.hexdigest() != self._source_sha256:
            raise UploadError("streamed source bytes do not match the work bundle identity")
        yield self._suffix
        self.completed = True


def http_request(method: str, url: str, headers: dict, body) -> Response:
    if method != "POST" or url != UPLOAD_URL:
        raise UploadError("AIHub upload transport target is invalid")
    connection = http.client.HTTPSConnection(
        UPLOAD_HOST, 443, timeout=UPLOAD_TIMEOUT_SECONDS
    )
    try:
        connection.request(method, UPLOAD_PATH, body=body, headers=dict(headers))
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise UploadError("AIHub upload response exceeded its size limit")
        return Response(response.status, response_body)
    finally:
        connection.close()


def _response_body(response) -> bytes:
    body = getattr(response, "body", b"")
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise UploadError("AIHub upload response body is invalid")
    return body


def valid_https_url(value) -> bool:
    # This gate must stay byte-for-byte aligned with doc2x.valid_https_url
    # (doc2x.py:243-263): this is the write-side gate for a source URL
    # before it is persisted into private.json, doc2x's is the read-side
    # gate the same field is checked against again on create/poll/refresh.
    # A code-point bound here would admit a URL doc2x's byte bound then
    # rejects, staging a bundle with no way to recover it.
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > 16384:
            return False
        parsed = urlsplit(value)
        _port = parsed.port
    except (UnicodeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not any(character.isspace() or ord(character) < 0x20 for character in value)
    )


def _classify(response, body: MultipartBody) -> UploadResult:
    status = getattr(response, "status", None)
    if type(status) is not int or not 100 <= status <= 599 or not body.completed:
        return UploadResult(
            "source_upload_unknown", None, "invalid_transport_result", None, None
        )
    response_body = _response_body(response)
    if status == 200:
        try:
            document = strict_json.loads(response_body)
        except strict_json.StrictJsonError:
            document = None
        url = document.get("url") if isinstance(document, dict) else None
        if valid_https_url(url):
            return UploadResult(
                "source_upload_ready",
                status,
                None,
                url,
                f"sha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()}",
            )
        return UploadResult(
            "source_upload_unknown", status, "invalid_success_response", None, None
        )
    if status == 403:
        return UploadResult(
            "source_upload_rejected",
            status,
            "storage_capacity_exhausted",
            None,
            None,
        )
    return UploadResult(
        "source_upload_unknown", status, "unverified_upload_result", None, None
    )


def upload_open_source(
    *,
    source_fd: int,
    source_sha256: str,
    source_size: int,
    api_key: str,
    transport=None,
) -> UploadResult:
    opened = os.fstat(source_fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size != source_size
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        raise UploadError("source PDF descriptor is unsafe")
    digest = hashlib.sha256()
    size = 0
    while size < source_size:
        chunk = os.read(source_fd, min(STREAM_CHUNK_BYTES, source_size - size))
        if not chunk:
            raise UploadError("source PDF ended before its recorded size")
        digest.update(chunk)
        size += len(chunk)
    if os.read(source_fd, 1) or digest.hexdigest() != source_sha256:
        raise UploadError("source PDF does not match its recorded identity")
    os.lseek(source_fd, 0, os.SEEK_SET)
    body = MultipartBody(
        source_fd, size_bytes=source_size, source_sha256=source_sha256
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": body.content_type,
        "Content-Length": str(body.content_length),
        "Accept": "application/json",
        "Connection": "close",
        "User-Agent": "pdf2markdown/1",
    }
    request = http_request if transport is None else transport
    try:
        response = request("POST", UPLOAD_URL, headers, body)
        result = _classify(response, body)
    except Exception:
        result = UploadResult(
            "source_upload_unknown", None, "network_result_unknown", None, None
        )
    final = os.fstat(source_fd)
    if (
        (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    ):
        raise UploadError("source PDF changed during upload")
    return result
