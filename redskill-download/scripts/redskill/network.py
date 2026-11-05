import email.utils
import hashlib
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .errors import DownloadError
from .inputs import safe_component

ENDPOINT = "https://edith.xiaohongshu.com/api/sns/v1/creator/red_skill/get_skill_bundle"
MAX_ZIP = 100 * 1024 * 1024
ATTEMPTS = 3
REQUEST_TIMEOUT = 30
TOTAL_BUDGET = 180
MAX_INLINE_WAIT = 30


def validate_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        allowed = host == "edith.xiaohongshu.com" or host.endswith(".xhscdn.com")
        if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise DownloadError("invalid_download_url", "Expected an official HTTPS RedSkill/CDN URL") from exc


def secure_cdn_url(url):
    """The official manifest sometimes returns HTTP; use the same CDN path/query over TLS."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme == "http" and (parsed.hostname or "").endswith(".xhscdn.com")
                and not parsed.username and not parsed.password and parsed.port in (None, 80)
                and not parsed.fragment):
            url = urllib.parse.urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
    except (ValueError, TypeError) as exc:
        raise DownloadError("invalid_download_url", "Invalid official CDN URL") from exc
    validate_url(url)
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def retry_delay(value, now=None):
    now = time.time() if now is None else now
    if not value:
        return None
    try:
        if str(value).strip().isdigit():
            return float(value)
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


class Client:
    def __init__(self, *, opener=None, budget=TOTAL_BUDGET, clock=time.monotonic, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + budget

    def _read(self, url, *, limit, destination=None):
        validate_url(url)
        for attempt in range(ATTEMPTS):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise DownloadError("network_budget_exhausted", "Download network budget exhausted")
            retry = None
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "redskill-download/1.0.0", "Accept": "*/*"})
                with self.opener.open(request, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
                    validate_url(response.geturl())
                    size_header = response.headers.get("Content-Length", "")
                    if size_header.isdigit() and int(size_header) > limit:
                        raise DownloadError("download_too_large", "Response exceeds the download size limit", max_bytes=limit)
                    parts, size = [], 0
                    stream = destination.open("wb") if destination else None
                    digest = hashlib.sha256()
                    try:
                        while True:
                            remaining = self.deadline - self.clock()
                            if remaining <= 0:
                                raise DownloadError("network_budget_exhausted", "Download network budget exhausted")
                            # Bound each socket read by the remaining overall budget where urllib exposes it.
                            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                            if sock is not None:
                                sock.settimeout(min(REQUEST_TIMEOUT, remaining))
                            chunk = response.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > limit:
                                raise DownloadError("download_too_large", "Response exceeds the download size limit", max_bytes=limit)
                            digest.update(chunk)
                            if stream:
                                stream.write(chunk)
                            else:
                                parts.append(chunk)
                    finally:
                        if stream:
                            stream.close()
                    if size_header.isdigit() and size != int(size_header):
                        raise OSError("incomplete response")
                    return (size, digest.hexdigest()) if destination else b"".join(parts)
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry = retry_delay(exc.headers.get("Retry-After"))
                exc.close()
                if status not in (408, 429, 500, 502, 503, 504):
                    code = "access_denied" if status in (401, 403) else "not_found" if status == 404 else "http_error"
                    raise DownloadError(code, "RedSkill request failed", http_status=status) from None
                reason = "HTTP " + str(status)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                cause = getattr(exc, "reason", exc)
                if isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)):
                    raise DownloadError("tls_certificate_error", "TLS certificate validation failed") from None
                if isinstance(cause, PermissionError):
                    raise DownloadError("permission_denied", "Network or output access was denied; check host permissions and the output directory") from None
                if isinstance(cause, ssl.SSLError):
                    raise DownloadError("tls_error", "TLS connection failed") from None
                # Both endpoints are read-only GETs; retrying cannot submit a purchase or installation.
                reason = type(cause).__name__
            delay = retry if retry is not None else float(2 ** attempt)
            remaining = self.deadline - self.clock()
            if delay > remaining or delay > MAX_INLINE_WAIT:
                raise DownloadError("retry_later", "Server cooldown exceeds this run's wait budget",
                                    retry_at=datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat())
            if attempt == ATTEMPTS - 1:
                details = {"retry_at": datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()} if retry is not None else {}
                raise DownloadError("network_failed", "Read request failed after bounded retries", **details)
            print(json.dumps({"event": "network_retry", "attempt": attempt + 2,
                              "wait_seconds": delay, "reason": reason}), file=sys.stderr)
            self.sleep(delay)

    def manifest(self, identifier):
        url = ENDPOINT + "?" + urllib.parse.urlencode({"identifier": identifier})
        try:
            response = json.loads(self._read(url, limit=1024 * 1024).decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise DownloadError("invalid_manifest", "RedSkill returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("code") != 0 or response.get("success") is not True:
            raise DownloadError("service_rejected", "RedSkill did not return a published bundle",
                                service_code=response.get("code") if isinstance(response, dict) else None)
        data = response.get("data")
        if not isinstance(data, dict) or data.get("identifier") != identifier:
            raise DownloadError("invalid_manifest", "Manifest identifier does not match the request")
        safe_component(data.get("version"), "version")
        import re
        if not isinstance(data.get("sha256"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", data["sha256"]):
            raise DownloadError("invalid_manifest", "Manifest has no valid SHA256")
        zip_url = secure_cdn_url(data.get("zip_url"))
        return {"identifier": identifier, "version": data["version"], "sha256": data["sha256"].lower(),
                "zip_url": zip_url}

    def download(self, url, destination):
        return self._read(url, limit=MAX_ZIP, destination=destination)
