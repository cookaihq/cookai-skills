"""Bounded public HTTPS downloads and the fixed AIHub task transport.

Source, ZIP and image downloads share the original public-source connection
checks: resolve every address, reject non-public endpoints, pin the connected
TLS peer, and send no API credential to a download URL.
"""
import hashlib
import http.client
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import pdf_source
from . import doc2x, jcs

ORIGIN = "https://api.aihubmax.com"
MAX_RESPONSE_BYTES = 1024 * 1024
DOWNLOAD_SECONDS = 120


class DownloadError(ValueError):
    def __init__(self, code, category="rejected", retry_after=None):
        super().__init__(code)
        self.code = code
        self.category = category
        self.retry_after = retry_after


def retry_after(value):
    try:
        number = float(value)
        return min(max(number, 0), 60) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _download_once(url, destination, limit, *, transport=None, pdf=False):
    client = transport or pdf_source.ProductionHttpsTransport()
    current = pdf_source.parse_https_url(url)
    seen = set()
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for _ in range(pdf_source.MAX_REDIRECTS + 1):
        if current.request_url in seen:
            raise DownloadError("redirect_loop")
        seen.add(current.request_url)
        session = response = None
        try:
            _endpoints, _peer, session, response = pdf_source._open_response(client, current)
            if response.status in pdf_source.REDIRECT_STATUSES:
                locations = pdf_source._header_values(response.headers, "Location")
                if len(locations) != 1:
                    raise DownloadError("invalid_redirect")
                current = pdf_source.parse_https_url(urljoin(current.request_url, locations[0]))
                continue
            if response.status != 200:
                transient = response.status == 429 or 500 <= response.status < 600
                values = pdf_source._header_values(response.headers, "Retry-After")
                raise DownloadError("download_http_%s" % response.status,
                                    "transient" if transient else "rejected",
                                    retry_after(values[0]) if values else None)
            pdf_source._validate_content_encoding(response.headers)
            if pdf:
                pdf_source._content_type(response.headers)
            declared = pdf_source._declared_length(response.headers)
            if declared is not None and declared > limit:
                raise DownloadError("download_size_limit", "oversized")
            count = 0
            digest = hashlib.sha256()
            with open(destination, "xb") as out:
                os.chmod(destination, 0o600)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError("download_timeout", "transient")
                    chunk = response.read(65536, timeout=min(30, remaining))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > limit:
                        raise DownloadError("download_size_limit", "oversized")
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
            if declared is not None and count != declared:
                raise DownloadError("download_incomplete", "transient")
            if pdf:
                with open(destination, "rb") as source:
                    signature = source.read(5)
                if signature != b"%PDF-":
                    raise DownloadError("invalid_pdf")
                pdf_source.validate_pdf_identity(Path(destination))
            return {"sha256": digest.hexdigest(), "byte_count": count}
        finally:
            pdf_source._close_quietly(response)
            pdf_source._close_quietly(session)
    raise DownloadError("redirect_limit")


def download(url, destination, limit, *, transport=None, pdf=False, attempts=3, sleep=time.sleep):
    """Commit a complete download; failed attempts never leave a partial target."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise DownloadError("download_destination_exists")
    with tempfile.TemporaryDirectory(prefix=".download-", dir=destination.parent) as tmp:
        partial = Path(tmp) / "body"
        for attempt in range(attempts):
            try:
                result = _download_once(url, partial, limit, transport=transport, pdf=pdf)
                os.replace(partial, destination)
                return result
            except pdf_source.PdfSourceError as exc:
                category = "transient" if exc.code in pdf_source.TRANSIENT_SOURCE_ERROR_CODES else "rejected"
                failure = DownloadError(exc.code, category)
            except DownloadError as exc:
                failure = exc
            except (OSError, http.client.HTTPException) as exc:
                # Network read failures are retriable; disk failures are not.
                network = isinstance(exc, (TimeoutError, ConnectionError, http.client.HTTPException))
                failure = DownloadError("download_" + type(exc).__name__, "transient" if network else "rejected")
            partial.unlink(missing_ok=True)
            if failure.category != "transient" or attempt + 1 >= attempts:
                raise failure
            delay = failure.retry_after if failure.retry_after is not None else 2 ** attempt
            sys.stderr.write("[pdf2markdown] download retry %s/%s in %ss (%s)\n" %
                             (attempt + 2, attempts, delay, failure.code))
            sleep(delay)


def asset_fetch(url, limit, *, directory):
    with tempfile.TemporaryDirectory(prefix=".asset-", dir=directory) as tmp:
        dest = Path(tmp) / "image"
        download(url, dest, limit)
        return dest.read_bytes()


def task_transport(api_key):
    def request(spec):
        parts = urlsplit(spec["url"])
        if parts.scheme + "://" + parts.netloc != ORIGIN:
            raise doc2x.Doc2XReason("configuration_invalid", "unrecognized task endpoint")
        method = spec["method"]
        path = parts.path + ("?" + parts.query if parts.query else "")
        if (method == "POST" and path != doc2x.CREATE_PATH) or (
            method == "GET" and not parts.path.startswith("/v1/tasks/")
        ) or method not in ("GET", "POST"):
            raise doc2x.Doc2XReason("configuration_invalid", "unrecognized task operation")
        timeout = min(60 if method == "POST" else 30, spec.get("timeout", 60))
        connection = http.client.HTTPSConnection(parts.hostname, timeout=max(0.01, timeout))
        try:
            body = jcs.canonicalize(spec["body"]) if spec["body"] is not None else None
            connection.request(method, path, body=body, headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json", "Accept": "application/json",
            })
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise doc2x.Doc2XReason("submission_unknown" if method == "POST" else "conversion_result_invalid",
                                        "task response exceeds byte limit")
            if method == "GET" and response.status == 429:
                exc = doc2x.Doc2XPollTransient("http_429", "task query rate limited")
                exc.retry_after = retry_after(response.getheader("Retry-After"))
                raise exc
            return response.status, raw
        except (OSError, http.client.HTTPException) as exc:
            if method == "GET":
                raise doc2x.Doc2XPollTransient("transport", type(exc).__name__) from None
            raise doc2x.Doc2XReason("submission_unknown", "create transport interrupted") from None
        finally:
            connection.close()
    return request
