from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import state, transcript


BASE_URL = "https://api.aihubmax.com"
UPLOAD_PATH = "/v1/files/upload/stream"
CREATE_PATH = "/v1/audios/generations"
POLL_PATH = "/v1/tasks/{task_id}?sync_upstream=true"
METADATA_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 10
POLL_BUDGET_SECONDS = 7200
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPTION_BYTES = 64 * 1024 * 1024
RETRY_AFTER_CAP_SECONDS = 60


class AIHubError(ValueError):
    pass


class AmbiguousWrite(AIHubError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    document: object
    text: str
    headers: object


def _parse_response(status: int, raw: bytes, headers: object) -> Response:
    text = raw.decode("utf-8", "replace")
    try:
        document = json.loads(text) if text else None
    except json.JSONDecodeError:
        document = None
    return Response(status, document, text, headers)


def json_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int = METADATA_TIMEOUT_SECONDS,
) -> Response:
    request_headers = {"User-Agent": "video-to-markdown/1.0", **headers}
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as incoming:
            raw = incoming.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AIHubError("AIHub response exceeded 4 MiB")
            return _parse_response(incoming.status, raw, incoming.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AIHubError("AIHub error response exceeded 4 MiB") from exc
        return _parse_response(exc.code, raw, exc.headers)


def transcription_request(
    url: str,
    timeout: int = METADATA_TIMEOUT_SECONDS,
) -> Response:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AIHubError("AIHub transcription URL must be HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "video-to-markdown/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as incoming:
            raw = incoming.read(MAX_TRANSCRIPTION_BYTES + 1)
            if len(raw) > MAX_TRANSCRIPTION_BYTES:
                raise AIHubError("AIHub transcription response exceeded 64 MiB")
            return _parse_response(incoming.status, raw, incoming.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AIHubError("AIHub transcription error response exceeded 4 MiB") from exc
        return _parse_response(exc.code, raw, exc.headers)


class MultipartBody:
    def __init__(self, path: Path, boundary: str):
        self.path = path
        self.boundary = boundary
        marker = boundary.encode("ascii")
        self.prefix = (
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="auto_cleanup"\r\n\r\ntrue\r\n--'
            + marker
            + b'\r\nContent-Disposition: form-data; name="file"; filename="audio-for-asr.flac"'
            + b"\r\nContent-Type: audio/flac\r\n\r\n"
        )
        self.suffix = b"\r\n--" + marker + b"--\r\n"
        self.content_length = len(self.prefix) + path.stat().st_size + len(self.suffix)

    def __iter__(self) -> Iterable[bytes]:
        yield self.prefix
        with self.path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        yield self.suffix


def upload_request(
    url: str,
    headers: dict[str, str],
    body: MultipartBody,
    timeout: int = UPLOAD_TIMEOUT_SECONDS,
) -> Response:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AIHubError("upload URL must be HTTPS")
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    try:
        request_headers = {
            "User-Agent": "video-to-markdown/1.0",
            "Content-Length": str(body.content_length),
            **headers,
        }
        connection.request("POST", parsed.path, body=body, headers=request_headers)
        incoming = connection.getresponse()
        raw = incoming.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AIHubError("AIHub upload response exceeded 4 MiB")
        return _parse_response(incoming.status, raw, incoming.headers)
    finally:
        connection.close()


def _safe_connection_failure(exc: Exception) -> bool:
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def _retry_after(response: Response) -> float | None:
    raw = response.headers.get("Retry-After") if hasattr(response.headers, "get") else None
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, RETRY_AFTER_CAP_SECONDS)


def _server_message(response: Response) -> str:
    if isinstance(response.document, dict):
        error = response.document.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return response.text[:500]


def run_write(
    call: Callable[[], Response],
    *,
    operation: str,
    sleep: Callable[[float], None] = time.sleep,
) -> Response:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = call()
        except Exception as exc:
            if not _safe_connection_failure(exc):
                raise AmbiguousWrite(
                    "%s may have been accepted but no response was received: %s"
                    % (operation, exc)
                ) from exc
            if attempt == MAX_ATTEMPTS:
                raise AIHubError("%s could not connect after 3 attempts: %s" % (operation, exc))
            sleep(2 ** (attempt - 1))
            continue
        if response.status == 429 and attempt < MAX_ATTEMPTS:
            wait = _retry_after(response)
            sleep(wait if wait is not None else 2 ** (attempt - 1))
            continue
        if 500 <= response.status <= 599:
            raise AmbiguousWrite(
                "%s returned HTTP %s; the write result is unknown" % (operation, response.status)
            )
        return response
    raise AIHubError("%s exhausted retry attempts" % operation)


def run_read(
    call: Callable[[], Response],
    *,
    operation: str,
    deadline: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Response:
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if clock() >= deadline:
            raise AIHubError("%s exceeded its wall-clock budget" % operation)
        try:
            response = call()
        except Exception as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            wait = 2 ** (attempt - 1)
            if clock() + wait >= deadline:
                break
            sleep(wait)
            continue
        if response.status == 429 or 500 <= response.status <= 599:
            if attempt == MAX_ATTEMPTS:
                return response
            wait = _retry_after(response)
            wait = wait if wait is not None else 2 ** (attempt - 1)
            if clock() + wait >= deadline:
                return response
            sleep(wait)
            continue
        return response
    raise AIHubError("%s failed after 3 attempts: %s" % (operation, last_error))


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + api_key, "Accept": "application/json"}


def upload_audio(
    audio_path: Path,
    api_key: str,
    *,
    transport: Callable = upload_request,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    digest, _ = state.sha256_file(audio_path)

    def call() -> Response:
        boundary = "video-to-markdown-%s" % digest
        body = MultipartBody(audio_path, boundary)
        headers = {
            **_auth_headers(api_key),
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        }
        return transport(BASE_URL + UPLOAD_PATH, headers, body, UPLOAD_TIMEOUT_SECONDS)

    response = run_write(call, operation="AIHub audio upload", sleep=sleep)
    if response.status != 200:
        raise AIHubError(
            "AIHub audio upload returned HTTP %s: %s"
            % (response.status, _server_message(response))
        )
    if not isinstance(response.document, dict):
        raise AmbiguousWrite("AIHub audio upload returned an invalid success response")
    file_id = response.document.get("id")
    url = response.document.get("url")
    if not isinstance(file_id, str) or not file_id or not isinstance(url, str) or not url:
        raise AmbiguousWrite("AIHub audio upload success response lacks id or url")
    return response.document


def create_body(model: str, audio_url: str, language: str | None = None) -> dict:
    language = language.strip().lower() if language else None
    body: dict = {"model": model}
    if model == "paraformer-v2":
        body["file_urls"] = [audio_url]
        if language:
            allowed = {"zh", "en", "ja", "yue", "ko", "de", "fr", "ru"}
            if language not in allowed:
                raise AIHubError(
                    "paraformer-v2 language must be one of: %s"
                    % ", ".join(sorted(allowed))
                )
            body["language_hints"] = [language]
    elif model == "paraformer-8k-v2":
        if language:
            raise AIHubError("paraformer-8k-v2 does not accept a language hint")
        body["file_urls"] = [audio_url]
    elif model == "scribe-v2":
        body["audio_url"] = audio_url
        if language:
            if re.fullmatch(r"[a-z]{2,3}", language) is None:
                raise AIHubError(
                    "scribe-v2 language must be a 2- or 3-letter ISO language code"
                )
            body["language_code"] = language
    elif model == "cohere-transcribe":
        body["audio_url"] = audio_url
        if language:
            allowed = {
                "ar",
                "de",
                "en",
                "es",
                "el",
                "fr",
                "it",
                "ja",
                "ko",
                "nl",
                "pl",
                "pt",
                "vi",
                "zh",
            }
            if language not in allowed:
                raise AIHubError(
                    "cohere-transcribe language must be one of: %s"
                    % ", ".join(sorted(allowed))
                )
            body["language"] = language
    else:
        raise AIHubError("unsupported ASR model: %s" % model)
    return body


def create_task(
    body: dict,
    api_key: str,
    *,
    transport: Callable = json_request,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def call() -> Response:
        return transport(
            "POST",
            BASE_URL + CREATE_PATH,
            {**_auth_headers(api_key), "Content-Type": "application/json"},
            payload,
            METADATA_TIMEOUT_SECONDS,
        )

    response = run_write(call, operation="AIHub ASR task creation", sleep=sleep)
    if response.status != 200:
        raise AIHubError(
            "AIHub ASR task creation returned HTTP %s: %s"
            % (response.status, _server_message(response))
        )
    if not isinstance(response.document, dict) or not response.document.get("id"):
        raise AmbiguousWrite("AIHub ASR task creation success response lacks a task id")
    return response.document


def poll_task(
    task_id: str,
    api_key: str,
    *,
    transport: Callable = json_request,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: int = POLL_BUDGET_SECONDS,
    interval_seconds: int = POLL_INTERVAL_SECONDS,
) -> dict:
    deadline = clock() + budget_seconds
    url = BASE_URL + POLL_PATH.format(task_id=urllib.parse.quote(task_id, safe=""))
    while clock() < deadline:
        response = run_read(
            lambda: transport(
                "GET",
                url,
                _auth_headers(api_key),
                None,
                METADATA_TIMEOUT_SECONDS,
            ),
            operation="AIHub task polling",
            deadline=deadline,
            sleep=sleep,
            clock=clock,
        )
        if response.status != 200:
            raise AIHubError(
                "AIHub task polling returned HTTP %s: %s"
                % (response.status, _server_message(response))
            )
        if not isinstance(response.document, dict):
            raise AIHubError("AIHub task polling returned invalid JSON")
        status_value = response.document.get("status")
        if status_value == "completed":
            return response.document
        if status_value == "failed":
            raise AIHubError(
                "AIHub ASR task failed: %s"
                % json.dumps(response.document.get("error"), ensure_ascii=False)
            )
        if status_value not in {"pending", "processing"}:
            raise AIHubError("AIHub task returned unknown status: %s" % status_value)
        wait = min(interval_seconds, max(0.0, deadline - clock()))
        if wait > 0:
            sleep(wait)
    raise AIHubError("AIHub task polling exceeded %s seconds" % budget_seconds)


def _transcription_url(value: object) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("transcription_url")
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in value.values():
            found = _transcription_url(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _transcription_url(nested)
            if found:
                return found
    return None


def fetch_transcription(
    url: str,
    *,
    transport: Callable = transcription_request,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: int = 300,
) -> dict:
    deadline = clock() + budget_seconds
    response = run_read(
        lambda: transport(url, METADATA_TIMEOUT_SECONDS),
        operation="AIHub transcription download",
        deadline=deadline,
        sleep=sleep,
        clock=clock,
    )
    if response.status != 200:
        raise AIHubError(
            "AIHub transcription download returned HTTP %s: %s"
            % (response.status, _server_message(response))
        )
    if not isinstance(response.document, dict):
        raise AIHubError("AIHub transcription download returned invalid JSON")
    return response.document


def run_asr(
    *,
    task_dir: Path,
    manifest: dict,
    private: dict,
    audio_path: Path,
    api_key: str | None,
    language: str | None,
    upload_transport: Callable = upload_request,
    json_transport: Callable = json_request,
    transcription_transport: Callable = transcription_request,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    if api_key is None:
        raise AIHubError(
            "AIHUB_API_KEY is required when no subtitle is supplied "
            "(process env, $PWD/.env.video-to-markdown, $PWD/.env.local, $PWD/.env, or --use-local-key)"
        )
    model = manifest["asr"]["model"]
    if model in {"paraformer-v2", "paraformer-8k-v2"}:
        if manifest["source"]["duration_ms"] > 12 * 60 * 60 * 1000:
            raise AIHubError(
                "%s accepts at most 12 hours per file; this Skill does not split paid ASR tasks"
                % model
            )
        if audio_path.stat().st_size > 2 * 1024 * 1024 * 1024:
            raise AIHubError("%s accepts at most 2GB per file" % model)
    upload_state = private["upload"].get("state")
    if upload_state == "ambiguous":
        raise AmbiguousWrite("previous AIHub upload result is ambiguous; not uploading again")
    audio_sha256, _ = state.sha256_file(audio_path)
    temporary_url = private["upload"].get("temporary_url")
    if not temporary_url:
        private["upload"] = {
            "state": "intent",
            "intent_audio_sha256": audio_sha256,
            "temporary_url": None,
            "response": None,
        }
        state.save_private(task_dir, private)
        try:
            upload_response = upload_audio(
                audio_path, api_key, transport=upload_transport, sleep=sleep
            )
        except AmbiguousWrite:
            private["upload"]["state"] = "ambiguous"
            manifest["asr"]["state"] = "ambiguous_upload"
            state.save_private(task_dir, private)
            state.save_manifest(task_dir, manifest)
            raise
        private["upload"] = {
            "state": "ready",
            "intent_audio_sha256": audio_sha256,
            "temporary_url": upload_response["url"],
            "response": upload_response,
        }
        manifest["asr"].update(
            {
                "state": "uploaded",
                "file_id": upload_response["id"],
                "uploaded_at": state.utc_now(),
                "url_expiry_notice": "Uploaded source URL expires after 72 hours.",
            }
        )
        state.save_private(task_dir, private)
        state.save_manifest(task_dir, manifest)
        temporary_url = upload_response["url"]

    create_state = private["asr_create"].get("state")
    if create_state == "ambiguous":
        raise AmbiguousWrite("previous AIHub ASR create result is ambiguous; not creating again")
    task_id = manifest["asr"].get("task_id")
    if not task_id:
        body = create_body(model, temporary_url, language)
        fingerprint = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        private["asr_create"] = {
            "state": "intent",
            "intent_fingerprint": fingerprint,
            "response": None,
        }
        state.save_private(task_dir, private)
        try:
            create_response = create_task(
                body, api_key, transport=json_transport, sleep=sleep
            )
        except AmbiguousWrite:
            private["asr_create"]["state"] = "ambiguous"
            manifest["asr"]["state"] = "ambiguous_asr_create"
            state.save_private(task_dir, private)
            state.save_manifest(task_dir, manifest)
            raise
        task_id = str(create_response["id"])
        private["asr_create"] = {
            "state": "ready",
            "intent_fingerprint": fingerprint,
            "response": create_response,
        }
        manifest["asr"].update({"state": "processing", "task_id": task_id})
        state.save_private(task_dir, private)
        state.save_manifest(task_dir, manifest)

    terminal_record = private.get("asr_terminal") or {}
    terminal = terminal_record.get("response")
    if not isinstance(terminal, dict) or terminal_record.get("task_id") != str(task_id):
        terminal = poll_task(
            str(task_id),
            api_key,
            transport=json_transport,
            sleep=sleep,
            clock=clock,
        )
        private["asr_terminal"] = {
            "state": "ready",
            "task_id": str(task_id),
            "response": terminal,
        }
        state.save_private(task_dir, private)

    transcription_response = None
    if model in {"paraformer-v2", "paraformer-8k-v2"}:
        transcription_url = _transcription_url(terminal)
        if not transcription_url:
            manifest["asr"]["state"] = "completed_unusable_result"
            state.save_manifest(task_dir, manifest)
            raise AIHubError("completed Paraformer task did not return a transcription URL")
        url_fingerprint = hashlib.sha256(transcription_url.encode("utf-8")).hexdigest()
        transcription_record = private.get("asr_transcription") or {}
        transcription_response = transcription_record.get("response")
        if (
            not isinstance(transcription_response, dict)
            or transcription_record.get("url_fingerprint") != url_fingerprint
        ):
            private["asr_transcription"] = {
                "state": "intent",
                "url_fingerprint": url_fingerprint,
                "response": None,
            }
            state.save_private(task_dir, private)
            transcription_response = fetch_transcription(
                transcription_url,
                transport=transcription_transport,
                sleep=sleep,
                clock=clock,
            )
            private["asr_transcription"] = {
                "state": "ready",
                "url_fingerprint": url_fingerprint,
                "response": transcription_response,
            }
            state.save_private(task_dir, private)

    try:
        normalized = transcript.normalize_aihub(
            terminal,
            model=model,
            language=language,
            transcription_response=transcription_response,
            duration_ms=manifest.get("source", {}).get("duration_ms"),
        )
    except transcript.TranscriptError:
        manifest["asr"]["state"] = "completed_unusable_result"
        state.save_manifest(task_dir, manifest)
        raise
    transcript.write_transcript(task_dir, normalized)
    manifest["asr"]["state"] = "completed"
    manifest["asr"]["completed_at"] = state.utc_now()
    state.save_manifest(task_dir, manifest)
    return normalized
