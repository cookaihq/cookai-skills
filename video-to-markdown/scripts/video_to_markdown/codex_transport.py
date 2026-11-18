"""One retry owner for the custom Responses provider used by the vision CLI."""
from __future__ import annotations

import hmac
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer

_RETRYABLE_READ_STATUS = {408, 429, 500, 502, 503, 504}
_RESPONSE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_MAX_EVENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Recovery:
    request_retries: int = 5
    stream_retries: int = 5
    backoff_base_ms: int = 1000
    backoff_max_ms: int = 16000
    responses_background: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cooldown(value):
    if not value:
        return 0.0
    try:
        if re.fullmatch(r"[0-9]{1,15}", value.strip()):
            return int(value)
        date = parsedate_to_datetime(value)
        return max(0.0, date.timestamp() - time.time()) if date.tzinfo else 0.0
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


def _rate_limited(error) -> bool:
    if error.code != 429:
        return False
    try:
        body = json.loads(error.read(65536))
        details = body["error"]
        return (details.get("code") or details.get("type")) in ("rate_limit_exceeded", "rate_limit_error")
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return False


class _Transport:
    def __init__(self, route, recovery, timeout):
        self.base_url = route["base_url"].rstrip("/")
        self.key = route["api_key"]
        self.settings = recovery
        self.deadline = time.monotonic() + timeout
        self.token = secrets.token_urlsafe(32)
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.cancel = threading.Event()
        self.request_retries = 0
        self.stream_reconnects = 0
        self.response_id = None
        self.request_id = None
        self.retry_not_before = None
        self.last_failure = None
        self.outcome_unknown = False
        self.recoveries = []

    def snapshot(self):
        return {"request_retries": self.request_retries, "stream_reconnects": self.stream_reconnects,
                "response_id": self.response_id, "request_id": self.request_id,
                "retry_not_before": self.retry_not_before, "last_failure": self.last_failure,
                "outcome_unknown": self.outcome_unknown,
                "recoveries": self.recoveries}

    def open(self, suffix, body, headers):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.cancel.is_set():
            raise TimeoutError()
        request = urllib.request.Request(self.base_url + suffix, data=body, headers={
            **headers, "Authorization": "Bearer " + self.key,
            "Accept": "text/event-stream", "Content-Type": "application/json",
        })
        return self.opener.open(request, timeout=min(300, remaining))

    def wait(self, recover_stream, retry_after):
        self.retry_not_before = time.time() + retry_after if retry_after else None
        used = self.stream_reconnects if recover_stream else self.request_retries
        limit = self.settings.stream_retries if recover_stream else self.settings.request_retries
        delay = min(self.settings.backoff_base_ms * 2**used, self.settings.backoff_max_ms) / 1000
        delay = max(delay, retry_after)
        if used >= limit or time.monotonic() + delay >= self.deadline or self.cancel.wait(delay):
            return False
        self.retry_not_before = None
        self.recoveries.append({"kind": "stream_reconnect" if recover_stream else "request_retry",
                                "number": used + 1, "wait_ms": round(delay * 1000), "reason": self.last_failure})
        if recover_stream:
            self.stream_reconnects += 1
        else:
            self.request_retries += 1
        return True

    def send(self, handler, suffix, body):
        if self.outcome_unknown:
            handler.send_error(409)
            return
        streaming = suffix == "/responses" and body.get("stream") is True
        if self.retry_not_before is not None and time.time() < self.retry_not_before:
            handler.send_error(429)
            return
        if streaming and self.settings.responses_background:
            body.update(background=True, store=True)
        request = json.dumps(body, ensure_ascii=False).encode()
        forwarded = ("User-Agent", "originator", "session_id", "OpenAI-Beta", "x-codex-turn-state",
                     "x-codex-turn-metadata", "X-Client-Request-Id")
        headers = {name: handler.headers[name] for name in forwarded if name in handler.headers}
        self.response_id, self.request_id, self.last_failure = None, None, None
        while True:
            try:
                upstream = self.open(suffix, request, headers)
                break
            except urllib.error.HTTPError as error:
                with error:
                    retry_after = _cooldown(error.headers.get("Retry-After"))
                    self.retry_not_before = time.time() + retry_after if retry_after else None
                    self.request_id = error.headers.get("x-request-id")
                    self.last_failure = "upstream_http_" + str(error.code)
                    if _rate_limited(error) and self.wait(False, retry_after):
                        continue
                    self.outcome_unknown = error.code >= 500
                    handler.send_error(error.code if 400 <= error.code <= 599 else 502)
                    return
            except OSError as error:
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                unsent = isinstance(reason, ConnectionRefusedError) or (
                    isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN)
                self.last_failure = "provider_connection_failed" if unsent else "request_outcome_unknown"
                if unsent and self.wait(False, 0):
                    continue
                self.outcome_unknown = not unsent
                handler.send_error(502)
                return

        self.outcome_unknown = True
        self.request_id = upstream.headers.get("x-request-id")
        self.last_failure = None
        handler.send_response(upstream.status)
        for name in ("Content-Type", "x-request-id", "x-codex-turn-state", "openai-model"):
            if name in upstream.headers:
                handler.send_header(name, upstream.headers[name])
        handler.end_headers()
        with upstream:
            if not streaming or "text/event-stream" not in upstream.headers.get("Content-Type", "").lower():
                while chunk := upstream.read1(65536):
                    handler.wfile.write(chunk)
                self.outcome_unknown = False
                return
            complete, cursor = self.copy_stream(upstream, handler, None)
        if complete:
            self.outcome_unknown = False
            return
        self.last_failure = "stream_disconnected"
        if self.response_id is None:
            return
        if cursor is None:
            self.last_failure = "stream_resume_cursor_missing"
            return
        retry_after = _cooldown(upstream.headers.get("Retry-After"))
        while self.wait(True, retry_after):
            query = {"stream": "true"}
            if cursor is not None:
                query["starting_after"] = cursor
            resume = "/responses/" + self.response_id + "?" + urllib.parse.urlencode(query)
            retry_after = 0
            try:
                with self.open(resume, None, headers) as resumed:
                    if "text/event-stream" not in resumed.headers.get("Content-Type", "").lower():
                        self.last_failure = "stream_resume_unsupported"
                        return
                    complete, cursor = self.copy_stream(resumed, handler, cursor)
                    retry_after = _cooldown(resumed.headers.get("Retry-After"))
                    if complete:
                        self.outcome_unknown = False
                        self.last_failure = None
                        return
            except urllib.error.HTTPError as error:
                with error:
                    retry_after = _cooldown(error.headers.get("Retry-After"))
                    self.retry_not_before = time.time() + retry_after if retry_after else None
                    self.last_failure = "stream_resume_http_" + str(error.code)
                    if error.code not in _RETRYABLE_READ_STATUS:
                        return
            except OSError as error:
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                if not isinstance(reason, (TimeoutError, ConnectionError, socket.gaierror)):
                    return

    def copy_stream(self, upstream, handler, cursor):
        lines = []
        size = 0
        while not self.cancel.is_set() and time.monotonic() < self.deadline:
            try:
                line = upstream.readline(_MAX_EVENT_BYTES + 1)
            except (OSError, urllib.error.URLError, EOFError, IncompleteRead):
                break
            if not line or size + len(line) > _MAX_EVENT_BYTES:
                break
            lines.append(line)
            size += len(line)
            if line not in (b"\n", b"\r\n"):
                continue
            data = b"\n".join(item[5:].lstrip().rstrip(b"\r\n") for item in lines if item.startswith(b"data:"))
            event = {}
            try:
                value = json.loads(data)
                if isinstance(value, dict):
                    event = value
            except ValueError:
                pass
            number = None if event.get("type") == "keepalive" else event.get("sequence_number")
            if type(number) is int and number >= 0 and cursor is not None and number <= cursor:
                lines, size = [], 0
                continue
            response = event.get("response")
            identifier = response.get("id") if isinstance(response, dict) else None
            if isinstance(identifier, str) and _RESPONSE_ID.fullmatch(identifier):
                if self.response_id not in (None, identifier):
                    break
                self.response_id = identifier
            handler.wfile.write(b"".join(lines))
            lines, size = [], 0
            if type(number) is int and number >= 0:
                cursor = number
            if data == b"[DONE]" or event.get("type") in ("response.completed", "response.failed", "response.incomplete", "error"):
                return True, cursor
        return False, cursor


@contextmanager
def serve(route, recovery, *, timeout=600):
    transport = _Transport(route, recovery, timeout)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + transport.token).encode()):
                self.send_error(401)
                return
            if self.path not in ("/responses", "/responses/compact"):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, OSError):
                self.send_error(400)
                return
            try:
                self.connection.settimeout(min(timeout, 300))
                transport.send(self, self.path, body)
            except (OSError, EOFError, IncompleteRead):
                self.close_connection = True

    class Server(HTTPServer):
        def handle_error(self, _request, _address):
            transport.last_failure = "local_transport_error"

    server = Server(("127.0.0.1", 0), Handler)
    # The handler can wait on a disconnected client or upstream. Closing the
    # listening socket and setting cancellation bounds cleanup without a join
    # on an upstream socket owned by the request thread.
    stop = threading.Event()
    server.timeout = 0.1

    def listen():
        while not stop.is_set():
            server.handle_request()

    worker = threading.Thread(target=listen, name="video-vision-transport", daemon=True)
    try:
        worker.start()
        yield "http://127.0.0.1:" + str(server.server_port), transport.token, transport
    finally:
        transport.cancel.set()
        stop.set()
        worker.join(timeout=0.2)
        server.server_close()
