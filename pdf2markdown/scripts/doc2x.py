from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from urllib.parse import quote
from urllib.parse import urlsplit

import strict_json


CREATE_URL = "https://api.aihubmax.com/v1/run/generations"
CREATE_HOST = "api.aihubmax.com"
CREATE_PATH = "/v1/run/generations"
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60
TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


class Doc2xError(ValueError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


@dataclass(frozen=True)
class CreateResult:
    state: str
    http_status: int | None
    reason_code: str | None
    task_id: str | None


@dataclass(frozen=True)
class PollResult:
    state: str
    http_status: int | None
    reason_code: str | None
    upstream_status: str | None
    results: object
    url: str | None = None


def canonical_request_bytes(request: dict) -> bytes:
    return json.dumps(
        request, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def http_request(method: str, url: str, headers: dict, body: bytes) -> Response:
    if method != "POST" or url != CREATE_URL:
        raise Doc2xError("Doc2X create transport target is invalid")
    connection = http.client.HTTPSConnection(
        CREATE_HOST, 443, timeout=REQUEST_TIMEOUT_SECONDS
    )
    try:
        connection.request(method, CREATE_PATH, body=body, headers=dict(headers))
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise Doc2xError("Doc2X create response exceeded its size limit")
        return Response(response.status, response_body)
    finally:
        connection.close()


def _response_body(response) -> bytes:
    body = getattr(response, "body", b"")
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise Doc2xError("Doc2X create response body is invalid")
    return body


def _classify(response) -> CreateResult:
    status = getattr(response, "status", None)
    if type(status) is not int or not 100 <= status <= 599:
        return CreateResult(
            "submission_unknown", None, "invalid_transport_result", None
        )
    try:
        document = strict_json.loads(_response_body(response))
    except (Doc2xError, strict_json.StrictJsonError):
        document = None
    task_id = document.get("id") if isinstance(document, dict) else None
    if (
        status == 200
        and isinstance(task_id, str)
        and TASK_ID_PATTERN.fullmatch(task_id) is not None
    ):
        return CreateResult("submitted", status, None, task_id)
    return CreateResult("submission_unknown", status, "no_task_id", None)


def create_task(*, request: dict, api_key: str, transport=None) -> CreateResult:
    body = canonical_request_bytes(request)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Accept": "application/json",
        "Connection": "close",
        "User-Agent": "pdf2markdown/1",
    }
    send = http_request if transport is None else transport
    try:
        return _classify(send("POST", CREATE_URL, headers, body))
    except Exception:
        return CreateResult(
            "submission_unknown", None, "network_result_unknown", None
        )


def _poll_url(task_id: str) -> str:
    return (
        "https://api.aihubmax.com/v1/tasks/"
        + quote(task_id, safe="")
        + "?sync_upstream=true"
    )


def http_poll_request(method: str, url: str, headers: dict, body: bytes) -> Response:
    if method != "GET" or not url.startswith(
        "https://api.aihubmax.com/v1/tasks/"
    ) or not url.endswith("?sync_upstream=true"):
        raise Doc2xError("Doc2X poll transport target is invalid")
    path = url.removeprefix("https://api.aihubmax.com")
    connection = http.client.HTTPSConnection(
        CREATE_HOST, 443, timeout=REQUEST_TIMEOUT_SECONDS
    )
    try:
        connection.request(method, path, body=body, headers=dict(headers))
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise Doc2xError("Doc2X poll response exceeded its size limit")
        return Response(response.status, response_body)
    finally:
        connection.close()


def _classify_poll(response) -> PollResult:
    status = getattr(response, "status", None)
    if type(status) is not int or not 100 <= status <= 599:
        return PollResult(
            "poll_transient", None, "poll_transient", None, None
        )
    if status == 401:
        return PollResult(
            "poll_unauthorized", status, "poll_unauthorized", None, None
        )
    if status == 404:
        return PollResult(
            "task_unavailable", status, "task_unavailable", None, None
        )
    if status != 200:
        return PollResult("poll_transient", status, "poll_transient", None, None)
    try:
        document = strict_json.loads(_response_body(response))
    except (Doc2xError, strict_json.StrictJsonError):
        return PollResult("poll_transient", status, "poll_transient", None, None)
    upstream_status = document.get("status") if isinstance(document, dict) else None
    if upstream_status in {"pending", "processing"}:
        return PollResult(
            upstream_status, status, None, upstream_status, document.get("results")
        )
    if upstream_status == "completed":
        results = document.get("results")
        if results is None:
            results = []
        if not isinstance(results, list):
            return PollResult(
                "unsafe_result_url",
                status,
                "unsafe_result_url",
                upstream_status,
                None,
            )
        urls = []
        for item in results:
            if not isinstance(item, dict):
                return PollResult(
                    "unsafe_result_url",
                    status,
                    "unsafe_result_url",
                    upstream_status,
                    None,
                )
            value = item.get("url")
            if not valid_https_url(value):
                return PollResult(
                    "unsafe_result_url",
                    status,
                    "unsafe_result_url",
                    upstream_status,
                    None,
                )
            if value not in urls:
                urls.append(value)
        if not urls:
            return PollResult(
                "result_pending", status, None, upstream_status, [], None
            )
        if len(urls) > 1:
            return PollResult(
                "unexpected_result_count",
                status,
                "unexpected_result_count",
                upstream_status,
                None,
            )
        return PollResult(
            "result_ready", status, None, upstream_status, None, urls[0]
        )
    if upstream_status == "failed":
        return PollResult("failed", status, "task_failed", upstream_status, None)
    return PollResult(
        "poll_transient", status, "poll_transient", None, None
    )


def poll_task(*, task_id: str, api_key: str, transport=None) -> PollResult:
    url = _poll_url(task_id)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Connection": "close",
        "User-Agent": "pdf2markdown/1",
    }
    send = http_poll_request if transport is None else transport
    try:
        return _classify_poll(send("GET", url, headers, b""))
    except Exception:
        return PollResult(
            "poll_transient", None, "poll_transient", None, None
        )


def valid_https_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 16384:
        return False
    try:
        value.encode("utf-8")
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
