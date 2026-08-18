#!/usr/bin/env python3
"""Create, poll, and safely download image-2 tasks."""

from __future__ import annotations

import os
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。放在
# `__main__` 守卫里，好让 tests 的 exec_module 导入本模块时不触发 execv。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Optional, Sequence, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_BASE_URL = "https://api.aihubmax.com"
# Canonical key variable name; `X_API_KEY` is the historical name still accepted
# as a fallback in every source.
KEY_NAME = "AIHUB_API_KEY"
LEGACY_KEY_NAME = "X_API_KEY"
KEY_NAMES = (KEY_NAME, LEGACY_KEY_NAME)
CREATE_ENDPOINT = "/v1/images/generations"
QUERY_ENDPOINT_PREFIX = "/v1/tasks"
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT_SECONDS = 60
SAFE_INTEGER_MAX = 2**53 - 1

# --- 网络抖动处理（ADR 0006）------------------------------------------------
# 总尝试 3 次（首次 + 2 次重试），指数退避 1s、2s；429 带 Retry-After 时按该值等。
MAX_TRANSPORT_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 1
MAX_RETRY_AFTER_SECONDS = 120  # 上游给出的超长 Retry-After 不照单全收，避免挂死

# 轮询墙钟预算（ADR 0006 规则 5）。`--max-attempts` 只数轮次、不管每轮实际耗时：
# 每轮内层最多 3 次尝试、退避 1s+2s，加上 60s 的单次 API 超时，90 轮理论上能拖到
# 数小时。因此在次数制之外再压一条真墙钟上限 = max_attempts × poll_interval
# （默认 90 × 8s = 720s），随两个参数一起缩放；内层重试不得突破它。
DEFAULT_POLL_WALL_CLOCK_BUDGET_SECONDS = 720  # = 默认 90 次 × 8s

# 网络失败的阶段分类。urllib 的结构性事实（CPython
# Lib/urllib/request.py AbstractHTTPHandler.do_open）：连接建立与请求发送阶段的
# OSError 被包成 urllib.error.URLError；等待/读取响应阶段的超时与连接中断原样抛出
# socket.timeout / TimeoutError / ConnectionResetError。据此：
#   SEND_FAILED   请求没能发出去 → 服务端不可能受理 → 计费的 create 也可安全重试
#   RESPONSE_LOST 请求已发出但没拿到响应 → 结果不明 → create 禁止盲重试（重复扣费）
#   DETERMINISTIC 与网络抖动无关的确定性失败（响应超本地上限、代理隧道被禁）→ 不重试
TRANSPORT_SEND_FAILED = "send_failed"
TRANSPORT_RESPONSE_LOST = "response_lost"
TRANSPORT_DETERMINISTIC = "deterministic"

ALLOWED_MODELS = {"gpt-image-2", "gpt-image-2-limit"}
ALLOWED_QUALITY = {"low", "medium", "high"}
ALLOWED_OUTPUT_FORMAT = {"png", "jpeg", "webp"}
ALLOWED_BACKGROUND = {"auto", "opaque"}
FULL_PRESETS = {
    "1024x768",
    "768x1024",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "1920x1080",
    "1080x1920",
    "2560x1440",
    "1440x2560",
    "3840x2160",
    "2160x3840",
}
LIMIT_PRESETS = {"1024x1024", "1024x1536", "1536x1024"}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$", re.ASCII)
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$", re.ASCII)
TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
QUOTED_STRING = r'"(?:[\x20-\x21\x23-\x5B\x5D-\x7E]|\\[\x20-\x7E])*"'
MEDIA_TYPE_RE = re.compile(
    rf"^{TOKEN}/{TOKEN}(?: *; *{TOKEN} *=[ ]*(?:{TOKEN}|{QUOTED_STRING}))* *$",
    re.ASCII,
)
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
REDIRECT_STATUSES = frozenset(range(300, 400))

ERROR_MESSAGES = {
    "invalid_arguments": "Invalid command arguments.",
    "configuration_error": "Image API configuration is unavailable.",
    "invalid_task_id": "Image task identifier is invalid.",
    # 结果不明（ambiguous）语义：请求可能已经到达上游、任务可能已创建并计费，也
    # 可能根本没发出去。本地无法区分，因此绝不自动重发（ADR 0006 规则 4）；
    # stderr 的 [net] 日志会写清本次到底是「请求未发出」还是「响应丢失」。
    "create_transport_error": (
        "Image task creation request failed; the task may or may not have been created upstream. "
        "Check the console before retrying."
    ),
    "create_http_error": "Image task creation was rejected.",
    "create_response_invalid": "Image task creation response was invalid.",
    "query_transport_error": "Image task query request failed.",
    "query_http_error": "Image task query was rejected.",
    "query_response_invalid": "Image task query response was invalid.",
    "upstream_failed": "Image generation failed.",
    "poll_timeout": "Image generation did not finish before the polling limit.",
    "internal_error": "Image generation command failed.",
}


class ArgumentProblem(ValueError):
    pass


class ConfigurationProblem(ValueError):
    pass


class ResponseProblem(ValueError):
    pass


class TransportFailure(OSError):
    """Transport-level failure carrying the stage it happened in (see the
    TRANSPORT_* constants). `stage` decides whether a retry is safe."""

    def __init__(self, message: str, stage: str = TRANSPORT_RESPONSE_LOST) -> None:
        super().__init__(message)
        self.stage = stage


class DownloadFailure(OSError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class OneHopResponse:
    status: int
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    pinned_address: str
    server_hostname: str
    port: int
    request_target: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class KeyCandidate:
    value: str
    source: str
    legacy: bool = False


@dataclass(frozen=True)
class UpstreamOutput:
    index: int
    url: str
    content_type: str


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class StandardApiTransport:
    """One-request API transport with redirects and ambient proxies disabled."""

    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    @staticmethod
    def _read_limited(response: Any) -> bytes:
        body = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(body) > MAX_API_RESPONSE_BYTES:
            # 确定性拒收，不是抖动：重试同样会超限。
            raise TransportFailure(
                "API response exceeds local safety limit", TRANSPORT_DETERMINISTIC
            )
        return body

    def request(
        self,
        method: str,
        url: str,
        headers: Sequence[tuple[str, str]],
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return HttpResponse(
                    int(response.status),
                    tuple((str(key), str(value)) for key, value in response.headers.items()),
                    self._read_limited(response),
                )
        except HTTPError as response:
            try:
                response_body = self._read_limited(response)
            finally:
                response.close()
            return HttpResponse(
                int(response.code),
                tuple((str(key), str(value)) for key, value in response.headers.items()),
                response_body,
            )
        except TransportFailure:
            # _read_limited 已经定好 stage（确定性），原样上抛。
            raise
        except URLError as exc:
            # 连接建立 / 请求发送阶段失败：请求没能发出去。
            raise TransportFailure(
                "API request transport failed", TRANSPORT_SEND_FAILED
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            # 等待 / 读取响应阶段失败：请求已经发出去了，服务端可能已受理。
            raise TransportFailure(
                "API request transport failed", TRANSPORT_RESPONSE_LOST
            ) from exc


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_address: str, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = pinned_address

    def connect(self) -> None:
        if self._tunnel_host is not None:
            raise TransportFailure("proxy tunnels are disabled", TRANSPORT_DETERMINISTIC)
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class StandardDownloadTransport:
    """Fetch exactly one pinned HTTPS hop into the supplied temporary file."""

    def __init__(
        self,
        timeout: int = DOWNLOAD_TIMEOUT_SECONDS,
        connection_factory: Optional[Callable[[str, int, str, int], Any]] = None,
    ) -> None:
        self.timeout = timeout
        self._connection_factory = connection_factory or _PinnedHTTPSConnection

    def fetch(self, request: DownloadRequest, sink: BinaryIO) -> OneHopResponse:
        connection = self._connection_factory(
            request.server_hostname,
            request.port,
            request.pinned_address,
            self.timeout,
        )
        try:
            connection.request("GET", request.request_target, headers=dict(request.headers))
            response = connection.getresponse()
            try:
                result = OneHopResponse(
                    int(response.status),
                    tuple((str(key), str(value)) for key, value in response.getheaders()),
                )
                if 200 <= response.status < 300:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        sink.write(chunk)
                    if response.length not in (None, 0):
                        # 响应体被截断：属于连接层抖动，幂等 GET 可安全重试。
                        raise TransportFailure(
                            "download body ended before Content-Length",
                            TRANSPORT_RESPONSE_LOST,
                        )
                return result
            finally:
                response.close()
        except TransportFailure:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportFailure(
                "download transport failed", TRANSPORT_RESPONSE_LOST
            ) from exc
        finally:
            connection.close()


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentProblem(message)


class Logger:
    def __init__(self, json_mode: bool, stdout: TextIO, stderr: TextIO) -> None:
        self._progress = stderr if json_mode else stdout
        self._stderr = stderr
        self._secrets: tuple[str, ...] = ()

    def set_secrets(self, secrets: Iterable[str]) -> None:
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )

    def _redact(self, message: str) -> str:
        for secret in self._secrets:
            message = message.replace(secret, "[redacted]")
        return message

    def info(self, message: str) -> None:
        print(self._redact(message), file=self._progress)

    def error(self, message: str) -> None:
        print(self._redact(message), file=self._stderr)


def build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="create_task.sh",
        add_help=False,
        allow_abbrev=False,
        description="Create an image-2 task, poll it, and optionally save its outputs.",
    )
    parser.add_argument("--prompt", default="", help="prompt text (required)")
    parser.add_argument(
        "--model",
        default="gpt-image-2",
        help="gpt-image-2 (default) or gpt-image-2-limit",
    )
    parser.add_argument(
        "--resolution",
        default="1024x1024",
        help="preset resolution or WIDTHxHEIGHT",
    )
    parser.add_argument("--num-outputs", default="1", help="number of generated images")
    parser.add_argument(
        "--image-url",
        action="append",
        default=[],
        help="reference image URL; repeatable",
    )
    parser.add_argument("--quality", default="", help="low, medium, or high (full model)")
    parser.add_argument("--output-format", default="", help="png, jpeg, or webp (full model)")
    parser.add_argument("--background", default="", help="auto or opaque (full model)")
    parser.add_argument("--mask-url", default="", help="mask image URL (full model)")
    parser.add_argument("--poll-interval", default="8", help="seconds between polls")
    parser.add_argument("--max-attempts", default="90", help="maximum poll attempts")
    parser.add_argument("--base-url", default=None, help="override the image API base URL")
    parser.add_argument(
        "--use-local-key",
        action="store_true",
        help="also read ~/.config/image-2/.env",
    )
    parser.add_argument("--output-dir", default="", help="directory for saved images")
    parser.add_argument("--filename", default="", help="saved filename stem")
    parser.add_argument("--label", default="", help="label used in the default filename")
    parser.add_argument("--no-save", action="store_true", help="return URLs without downloading")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="emit one structured terminal result to stdout",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="show_help", help="show help")
    return parser


def _positive_integer(value: str, option: str) -> int:
    if not isinstance(value, str) or POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise ArgumentProblem(f"{option} must be a positive integer")
    number = int(value)
    if number > SAFE_INTEGER_MAX:
        raise ArgumentProblem(f"{option} exceeds the safe integer range")
    return number


def _validate_resolution(resolution: str, model: str) -> None:
    if model == "gpt-image-2-limit":
        if resolution not in LIMIT_PRESETS:
            raise ArgumentProblem("--resolution is not supported by gpt-image-2-limit")
        return
    if resolution in FULL_PRESETS:
        return
    match = re.fullmatch(r"([0-9]+)x([0-9]+)", resolution, re.ASCII)
    if match is None:
        raise ArgumentProblem("--resolution must be a preset or WIDTHxHEIGHT")
    width, height = (int(match.group(1)), int(match.group(2)))
    if width % 16 or height % 16:
        raise ArgumentProblem("custom resolution sides must be multiples of 16")
    if not (256 <= width <= 3840 and 256 <= height <= 3840):
        raise ArgumentProblem("custom resolution sides must be in 256..3840")
    if not (655360 <= width * height <= 8294400):
        raise ArgumentProblem("custom resolution total pixels are out of range")
    if max(width, height) > min(width, height) * 3:
        raise ArgumentProblem("custom resolution aspect ratio exceeds 3:1")


def _normalize_base_url(value: str) -> str:
    if not value or _contains_control(value) or any(character.isspace() for character in value):
        raise ArgumentProblem("--base-url is invalid")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ArgumentProblem("--base-url is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ArgumentProblem("--base-url is invalid")
    return value.rstrip("/")


def validate_arguments(namespace: argparse.Namespace) -> argparse.Namespace:
    if not namespace.prompt or not namespace.prompt.strip():
        raise ArgumentProblem("--prompt is required")
    if namespace.model not in ALLOWED_MODELS:
        raise ArgumentProblem("--model is invalid")
    _validate_resolution(namespace.resolution, namespace.model)
    namespace.num_outputs = _positive_integer(namespace.num_outputs, "--num-outputs")
    if namespace.model == "gpt-image-2-limit" and namespace.num_outputs != 1:
        raise ArgumentProblem("gpt-image-2-limit requires --num-outputs 1")
    if namespace.model == "gpt-image-2" and not 1 <= namespace.num_outputs <= 10:
        raise ArgumentProblem("gpt-image-2 requires --num-outputs in 1..10")
    if namespace.quality and namespace.quality not in ALLOWED_QUALITY:
        raise ArgumentProblem("--quality is invalid")
    if namespace.output_format and namespace.output_format not in ALLOWED_OUTPUT_FORMAT:
        raise ArgumentProblem("--output-format is invalid")
    if namespace.background and namespace.background not in ALLOWED_BACKGROUND:
        raise ArgumentProblem("--background is invalid")
    if namespace.model == "gpt-image-2-limit" and any(
        (namespace.quality, namespace.output_format, namespace.background, namespace.mask_url)
    ):
        raise ArgumentProblem("gpt-image-2-limit does not support advanced output options")
    if namespace.mask_url and namespace.background:
        raise ArgumentProblem("--mask-url and --background are mutually exclusive")
    if namespace.mask_url and not namespace.image_url:
        raise ArgumentProblem("--mask-url requires --image-url")
    namespace.poll_interval = _positive_integer(namespace.poll_interval, "--poll-interval")
    namespace.max_attempts = _positive_integer(namespace.max_attempts, "--max-attempts")
    namespace.base_url = _normalize_base_url(namespace.base_url)
    if namespace.filename:
        if "/" in namespace.filename or _contains_control(namespace.filename):
            raise ArgumentProblem("--filename must be a control-free basename")
        stem, suffix = os.path.splitext(namespace.filename)
        if suffix and stem:
            namespace.filename = stem
        if namespace.filename in {"", ".", ".."}:
            raise ArgumentProblem("--filename must name a file")
    return namespace


def _contains_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResponseProblem("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ResponseProblem(f"non-finite JSON number: {value}")


def parse_response_object(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ResponseProblem) as exc:
        raise ResponseProblem("response is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ResponseProblem("response must be a JSON object")
    return value


def _parse_dotenv_key(path: Path) -> tuple[str, str]:
    """Return (value, var_name) for the first key name present in the file,
    canonical name before the legacy one; ("", "") when neither is set."""
    if not path.exists():
        return "", ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigurationProblem(f"cannot read {path}") from exc
    selected: dict[str, str] = {}
    for line in lines:
        if re.match(r"^[ \t]*#", line) or not line.strip():
            continue
        match = re.match(r"^[ \t]*(AIHUB_API_KEY|X_API_KEY)[ \t]*=[ \t]*(.*)$", line)
        if match is None:
            continue
        value = match.group(2).rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        selected[match.group(1)] = value
    for name in KEY_NAMES:
        if selected.get(name):
            return selected[name], name
    return "", ""


def collect_keys(
    environ: Mapping[str, str],
    cwd: Path,
    home: Path,
    use_local_key: bool,
) -> list[KeyCandidate]:
    candidates: list[KeyCandidate] = []
    seen: set[str] = set()

    def add(value: str, source: str, var_name: str) -> None:
        if value and value not in seen:
            seen.add(value)
            legacy = var_name == LEGACY_KEY_NAME
            label = f"{source} ({LEGACY_KEY_NAME}, deprecated)" if legacy else source
            candidates.append(KeyCandidate(value, label, legacy))

    def add_from_file(path: Path, source: str) -> None:
        value, var_name = _parse_dotenv_key(path)
        add(value, source, var_name)

    for name in KEY_NAMES:
        env_value = environ.get(name, "")
        if env_value:
            add(env_value, f"env {name}" if name == KEY_NAME else "env", name)
            break
    add_from_file(cwd / ".env.local", f"{cwd}/.env.local")
    add_from_file(cwd / ".env", f"{cwd}/.env")
    if use_local_key:
        add_from_file(home / ".config" / "image-2" / ".env", "~/.config/image-2/.env")
    return candidates


def mask_key(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def build_payload(namespace: argparse.Namespace) -> bytes:
    resolution: Any = namespace.resolution
    if namespace.model == "gpt-image-2" and namespace.resolution not in FULL_PRESETS:
        width, height = namespace.resolution.split("x", 1)
        resolution = {"width": int(width), "height": int(height)}
    payload: dict[str, Any] = {
        "model": namespace.model,
        "prompt": namespace.prompt,
        "num_outputs": namespace.num_outputs,
        "resolution": resolution,
    }
    if namespace.quality:
        payload["quality"] = namespace.quality
    if namespace.output_format:
        payload["output_format"] = namespace.output_format
    if namespace.background:
        payload["background"] = namespace.background
    if namespace.mask_url:
        payload["mask_url"] = namespace.mask_url
    if namespace.image_url:
        payload["image_urls"] = namespace.image_url
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def validate_task_id(value: Any, active_keys: Sequence[str]) -> str:
    if not isinstance(value, str) or TASK_ID_RE.fullmatch(value) is None:
        raise ResponseProblem("invalid task id")
    if any(key in value for key in active_keys):
        raise ResponseProblem("reflected task id")
    return value


def _strict_percent_decode(value: str) -> str:
    index = 0
    while index < len(value):
        if value[index] == "%":
            if index + 2 >= len(value) or any(
                character not in HEX_DIGITS for character in value[index + 1 : index + 3]
            ):
                raise ResponseProblem("malformed percent escape")
            index += 3
        else:
            index += 1
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResponseProblem("percent-decoded URL is not UTF-8") from exc


def validate_upstream_url(value: Any, active_keys: Sequence[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ResponseProblem("upstream URL must be a string")
    if len(value.encode("utf-8")) > 8192:
        raise ResponseProblem("upstream URL is too long")
    if _contains_control(value) or any(character.isspace() for character in value):
        raise ResponseProblem("upstream URL contains whitespace or controls")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ResponseProblem("upstream URL authority is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in value
    ):
        raise ResponseProblem("upstream URL must be absolute HTTPS without userinfo or fragment")
    decoded = _strict_percent_decode(value)
    if any(key in value or key in decoded for key in active_keys):
        raise ResponseProblem("upstream URL reflected an active API key")
    return value


def validate_content_type(value: Any, active_keys: Sequence[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ResponseProblem("content type must be a string")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise ResponseProblem("content type must be visible ASCII")
    if MEDIA_TYPE_RE.fullmatch(value) is None:
        raise ResponseProblem("content type is not a valid media type")
    if any(key in value for key in active_keys):
        raise ResponseProblem("content type reflected an active API key")
    return value


def parse_completed_outputs(data: dict[str, Any], active_keys: Sequence[str]) -> Optional[list[UpstreamOutput]]:
    results = data.get("results")
    if results is None or results == []:
        return None
    if not isinstance(results, list):
        raise ResponseProblem("completed results must be an array")
    parsed: list[UpstreamOutput] = []
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            raise ResponseProblem("result must be an object")
        parsed.append(
            UpstreamOutput(
                index,
                validate_upstream_url(result.get("url"), active_keys),
                validate_content_type(result.get("content_type"), active_keys),
            )
        )
    return parsed


def sanitize_label(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", value)
    value = re.sub(r"\s+", "_", value, flags=re.UNICODE).strip("_")
    return value[:10]


def _normalized_absolute_path(value: str, cwd: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.normpath(str(path.absolute())))


def output_stem(namespace: argparse.Namespace, timestamp: str) -> str:
    if namespace.filename:
        return namespace.filename
    label = sanitize_label(namespace.label if namespace.label else namespace.prompt)
    return f"{timestamp}-{label}" if label else timestamp


def extension_for(output: UpstreamOutput) -> str:
    media_type = output.content_type.split(";", 1)[0].strip().lower()
    if media_type == "image/png":
        return "png"
    if media_type in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if media_type == "image/webp":
        return "webp"
    suffix = Path(urlsplit(output.url).path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpg", "jpeg", "webp"}:
        return "jpg" if suffix == "jpeg" else suffix
    return "png"


def unique_target_path(directory: Path, stem: str, extension: str) -> Path:
    candidate = directory / f"{stem}.{extension}"
    index = 2
    while os.path.lexists(candidate):
        candidate = directory / f"{stem}-{index}.{extension}"
        index += 1
    return candidate


def default_resolver(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadFailure("DNS resolution failed") from exc
    addresses: list[str] = []
    for record in records:
        address = str(record[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _canonical_url_identity(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port or 443
    authority = hostname if port == 443 else f"{hostname}:{port}"
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))


def _download_request(
    url: str,
    resolver: Callable[[str, int], Sequence[str]],
) -> DownloadRequest:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise DownloadFailure("download host is missing")
    try:
        server_hostname = hostname.encode("idna").decode("ascii")
        port = parsed.port or 443
        addresses = list(resolver(server_hostname, port))
    except (UnicodeError, ValueError, OSError) as exc:
        raise DownloadFailure("download address resolution failed") from exc
    if not addresses:
        raise DownloadFailure("download address resolution returned no addresses")
    normalized: list[str] = []
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DownloadFailure("download address is invalid") from exc
        if not parsed_address.is_global:
            raise DownloadFailure("download address is not globally routable")
        canonical = str(parsed_address)
        if canonical not in normalized:
            normalized.append(canonical)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&;%:@!$'()*+,-._~/?")
    request_target = path + (f"?{query}" if query else "")
    return DownloadRequest(
        url=url,
        pinned_address=normalized[0],
        server_hostname=server_hostname,
        port=port,
        request_target=request_target,
        headers=(("Accept", "*/*"), ("User-Agent", "image-2/2")),
    )


def _single_header(headers: Sequence[tuple[str, str]], name: str) -> str:
    values = [value for key, value in headers if key.lower() == name.lower()]
    if len(values) != 1 or not values[0]:
        raise DownloadFailure(f"redirect requires exactly one {name} header")
    return values[0]


def publish_no_replace(temporary_path: Path, final_path: Path) -> None:
    os.link(temporary_path, final_path)
    try:
        temporary_path.unlink()
    except OSError:
        # The complete final inode is already published; a stale temp is safer
        # than misreporting the valid final path as a failed partial download.
        pass


def _transport_stage(exc: BaseException) -> str:
    """Stage of a transport failure; anything unlabeled is treated as the unsafe
    case (response lost), so an unknown error never gets a write replayed."""
    return getattr(exc, "stage", TRANSPORT_RESPONSE_LOST)


def _is_transient_status(status: int) -> bool:
    """ADR 0006 规则 2：429 与全部 5xx 算瞬时；确定性 4xx 不算。"""
    return status == 429 or 500 <= status <= 599


def _backoff_seconds(attempt: int) -> float:
    """指数退避：第 1 次重试前等 1s，第 2 次等 2s。"""
    return float(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def _retry_after_seconds(headers: Sequence[tuple[str, str]]) -> Optional[float]:
    """429 的 Retry-After（只认秒数形式；HTTP-date 与非法值回落到退避公式）。

    `float()` 接受 "nan" / "inf"，而 `min(nan, 120)` 返回 nan、`sleep(nan)` 直接
    抛异常把脚本打断，所以先用 math.isfinite 挡掉再钳制。
    """
    for key, value in headers:
        if key.lower() != "retry-after":
            continue
        try:
            seconds = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return None


def _api_request_with_retry(
    api_transport: Any,
    method: str,
    url: str,
    headers: Sequence[tuple[str, str]],
    body: Optional[bytes],
    *,
    operation: str,
    retry_response_lost: bool,
    sleeper: Callable[[float], None],
    logger: Logger,
    attempts: int = MAX_TRANSPORT_ATTEMPTS,
) -> HttpResponse:
    """One API call with ADR 0006 jitter handling.

    `retry_response_lost=False` is the billing-write setting used by task creation.
    Anything that leaves the outcome unknown is reported instead of replayed:

    - transport failures after the request went out (`TRANSPORT_RESPONSE_LOST`);
    - HTTP 5xx. The server received the request; whether it created (and billed)
      a task before failing to answer cannot be told apart from a clean rejection,
      so a 5xx is NOT retried here — same verdict as a lost response.

    Only HTTP 429 stays retryable for a billing write: it is the one status where
    the server states it declined the request, so no task exists and nothing was
    billed. Send-stage failures (the request never left the client) are retried in
    both settings; idempotent reads additionally retry every transient status.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = api_transport.request(method, url, headers, body)
        except Exception as exc:  # noqa: BLE001 - stage decides what is safe
            stage = _transport_stage(exc)
            if stage == TRANSPORT_RESPONSE_LOST and not retry_response_lost:
                logger.error(
                    f"[net] {operation}: the request went out but no response came back; "
                    "the result is unknown and was NOT retried"
                )
                raise
            if stage == TRANSPORT_DETERMINISTIC or attempt >= attempts:
                raise
            wait = _backoff_seconds(attempt)
            logger.info(
                f"[net] {operation} attempt {attempt}/{attempts} failed ({stage}); "
                f"retrying in {wait}s"
            )
            sleeper(wait)
            continue
        if not isinstance(response, HttpResponse):
            raise TransportFailure(
                "API transport returned an invalid response", TRANSPORT_DETERMINISTIC
            )
        if retry_response_lost:
            status_retryable = _is_transient_status(response.status)
        else:
            # 计费写：只有 429 能确认「服务端拒绝、未受理、未扣费」。5xx 与响应
            # 丢失同口径——服务端已收到请求，是否已建任务无法确认，不重试。
            status_retryable = response.status == 429
            if 500 <= response.status <= 599:
                logger.error(
                    f"[net] {operation}: the server answered HTTP {response.status}; "
                    "whether it accepted the request is unknown and it was NOT retried"
                )
        if attempt >= attempts or not status_retryable:
            return response
        wait = _retry_after_seconds(response.headers)
        source = "Retry-After"
        if wait is None:
            wait = _backoff_seconds(attempt)
            source = "backoff"
        logger.info(
            f"[net] {operation} attempt {attempt}/{attempts} returned HTTP {response.status}; "
            f"retrying in {wait}s ({source})"
        )
        sleeper(wait)
    raise TransportFailure("API request retries exhausted", TRANSPORT_RESPONSE_LOST)


def _fetch_hop_with_retry(
    transport: Any,
    request: DownloadRequest,
    sink: BinaryIO,
    *,
    operation: str,
    sleeper: Callable[[float], None],
    logger: Logger,
    attempts: int = MAX_TRANSPORT_ATTEMPTS,
) -> Any:
    """Fetch one download hop, retrying transient failures (ADR 0006).

    Downloads are idempotent GETs, so both transport failures and transient
    statuses are safe to replay; the sink is rewound before every attempt so a
    partial body from a failed attempt cannot be published.
    """
    for attempt in range(1, attempts + 1):
        sink.seek(0)
        sink.truncate(0)
        try:
            response = transport.fetch(request, sink)
        except Exception as exc:  # noqa: BLE001 - stage decides what is safe
            stage = _transport_stage(exc)
            if stage == TRANSPORT_DETERMINISTIC or attempt >= attempts:
                raise
            wait = _backoff_seconds(attempt)
            logger.info(
                f"[net] {operation} attempt {attempt}/{attempts} failed ({stage}); "
                f"retrying in {wait}s"
            )
            sleeper(wait)
            continue
        if not isinstance(response, OneHopResponse):
            return response
        if attempt >= attempts or not _is_transient_status(response.status):
            return response
        wait = _retry_after_seconds(response.headers)
        source = "Retry-After"
        if wait is None:
            wait = _backoff_seconds(attempt)
            source = "backoff"
        logger.info(
            f"[net] {operation} attempt {attempt}/{attempts} returned HTTP {response.status}; "
            f"retrying in {wait}s ({source})"
        )
        sleeper(wait)
    raise DownloadFailure("download retries exhausted")


def download_one(
    output: UpstreamOutput,
    target: Path,
    active_keys: Sequence[str],
    transport: Any,
    resolver: Callable[[str, int], Sequence[str]],
    publisher: Callable[[Path, Path], None],
    sleeper: Callable[[float], None],
    logger: Logger,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".image-2-download-",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as sink:
            current_url = output.url
            seen = {_canonical_url_identity(current_url)}
            followed = 0
            while True:
                validate_upstream_url(current_url, active_keys)
                request = _download_request(current_url, resolver)
                # 单跳内自带瞬时重试（sink 每次尝试前被清空，见 _fetch_hop_with_retry）
                response = _fetch_hop_with_retry(
                    transport,
                    request,
                    sink,
                    operation=f"download result {output.index}",
                    sleeper=sleeper,
                    logger=logger,
                )
                if not isinstance(response, OneHopResponse):
                    raise DownloadFailure("download transport returned an invalid response")
                if 200 <= response.status < 300:
                    sink.flush()
                    os.fsync(sink.fileno())
                    break
                if response.status in REDIRECT_STATUSES:
                    if followed >= MAX_REDIRECTS:
                        raise DownloadFailure("redirect limit exceeded")
                    location = _single_header(response.headers, "Location")
                    next_url = urljoin(current_url, location)
                    validate_upstream_url(next_url, active_keys)
                    identity = _canonical_url_identity(next_url)
                    if identity in seen:
                        raise DownloadFailure("redirect loop detected")
                    seen.add(identity)
                    current_url = next_url
                    followed += 1
                    continue
                raise DownloadFailure("download response was not successful")
        publisher(temporary_path, target)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def download_outputs(
    outputs: Sequence[UpstreamOutput],
    output_dir: Path,
    stem: str,
    active_keys: Sequence[str],
    transport: Any,
    resolver: Callable[[str, int], Sequence[str]],
    publisher: Callable[[Path, Path], None],
    sleeper: Callable[[float], None],
    logger: Logger,
) -> list[dict[str, Any]]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.error("[save] output directory could not be created")
        return [
            {
                "index": output.index,
                "local_path": None,
                "upstream_url": output.url,
                "content_type": output.content_type,
                "status": "download_failed",
            }
            for output in outputs
        ]

    results: list[dict[str, Any]] = []
    multiple = len(outputs) > 1
    for output in outputs:
        file_stem = f"{stem}-{output.index:02d}" if multiple else stem
        target = unique_target_path(output_dir, file_stem, extension_for(output))
        try:
            download_one(
                output, target, active_keys, transport, resolver, publisher, sleeper, logger
            )
        except Exception:
            logger.error(f"[save] result {output.index} download failed")
            local_path: Optional[str] = None
            status = "download_failed"
        else:
            local_path = str(target)
            status = "saved"
            logger.info(f"[save] result {output.index} saved to {target}")
        results.append(
            {
                "index": output.index,
                "local_path": local_path,
                "upstream_url": output.url,
                "content_type": output.content_type,
                "status": status,
            }
        )
    return results


def _failure(code: str, task_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "failed",
        "outputs": [],
        "error": {"code": code, "message": ERROR_MESSAGES[code]},
    }


def _timeout(task_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "timed_out",
        "outputs": [],
        "error": {"code": "poll_timeout", "message": ERROR_MESSAGES["poll_timeout"]},
    }


def _success(task_id: str, outputs: list[dict[str, Any]]) -> dict[str, Any]:
    status = "ok" if all(output["status"] != "download_failed" for output in outputs) else "partial_success"
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": status,
        "outputs": outputs,
        "error": None,
    }


def _exit_code(document: dict[str, Any]) -> int:
    if document["status"] == "ok":
        return 0
    if document["status"] == "partial_success":
        return 1
    if document["status"] == "timed_out":
        return 3
    if document["error"]["code"] == "upstream_failed":
        return 2
    return 1


def _emit_json(document: dict[str, Any], stdout: TextIO) -> None:
    stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    stdout.write("\n")
    stdout.flush()


def _emit_legacy(document: dict[str, Any], stdout: TextIO, stderr: TextIO) -> None:
    status = document["status"]
    if status in {"ok", "partial_success"}:
        for output in document["outputs"]:
            print(f"Result {output['index']}: {output['upstream_url']}", file=stdout)
        saved = [output for output in document["outputs"] if output["status"] == "saved"]
        failed = [output for output in document["outputs"] if output["status"] == "download_failed"]
        if failed:
            print(
                f"[save] Completed with failures: saved={len(saved)} failed={len(failed)}",
                file=stderr,
            )
            for output in saved:
                print(f"  saved: {output['local_path']}", file=stdout)
        elif saved:
            print("[save] Saved file(s):", file=stdout)
            for output in saved:
                print(f"  {output['local_path']}", file=stdout)
        return
    error = document["error"]
    print(f"Error [{error['code']}]: {error['message']}", file=stderr)
    if document["task_id"] is not None:
        print(f"Task ID: {document['task_id']}", file=stderr)


def _log_summary(
    namespace: argparse.Namespace,
    keys: Sequence[KeyCandidate],
    output_dir: Path,
    logger: Logger,
) -> None:
    logger.info("Request summary:")
    logger.info(f"- create endpoint: {namespace.base_url}{CREATE_ENDPOINT}")
    logger.info(f"- query endpoint: {namespace.base_url}{QUERY_ENDPOINT_PREFIX}/{{id}}")
    logger.info(f"- model: {namespace.model}")
    logger.info(f"- resolution: {namespace.resolution}")
    logger.info(f"- num_outputs: {namespace.num_outputs}")
    logger.info("- key chain (high to low):")
    for index, key in enumerate(keys, start=1):
        logger.info(f"    {index}. {key.source} ({mask_key(key.value)})")
    if keys and keys[0].legacy:
        logger.info(
            f"⚠️ {LEGACY_KEY_NAME} 已废弃，请改用 {KEY_NAME}"
            f"（本次仍按 {LEGACY_KEY_NAME} 读取，来源：{keys[0].source}）"
        )
    logger.info(f"- poll interval: {namespace.poll_interval}s")
    logger.info(f"- max attempts: {namespace.max_attempts}")
    logger.info("- save: disabled (--no-save)" if namespace.no_save else f"- save directory: {output_dir}")


def run_task(
    namespace: argparse.Namespace,
    keys: Sequence[KeyCandidate],
    active_keys: Sequence[str],
    api_transport: Any,
    download_transport: Any,
    resolver: Callable[[str, int], Sequence[str]],
    sleeper: Callable[[float], None],
    publisher: Callable[[Path, Path], None],
    output_dir: Path,
    timestamp: str,
    logger: Logger,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    payload = build_payload(namespace)
    create_url = f"{namespace.base_url}{CREATE_ENDPOINT}"
    selected_key: Optional[KeyCandidate] = None
    create_response: Optional[HttpResponse] = None
    for key in keys:
        logger.info(f"[auth] Trying key from: {key.source} ({mask_key(key.value)})")
        try:
            # 计费写操作（ADR 0006 规则 4）：只重试「确认未受理」的失败——请求没
            # 发出去（send_failed），或服务端明确以 429/5xx 应答（没有任务返回）。
            # 请求发出后响应丢失时不重试，按结果不明报出，避免重复扣费。
            response = _api_request_with_retry(
                api_transport,
                "POST",
                create_url,
                (("Content-Type", "application/json"), ("Authorization", f"Bearer {key.value}")),
                payload,
                operation="create task",
                retry_response_lost=False,
                sleeper=sleeper,
                logger=logger,
            )
        except Exception:
            return _failure("create_transport_error")
        logger.info(f"[auth] HTTP {response.status}")
        if response.status == 401:
            continue
        selected_key = key
        create_response = response
        break
    if selected_key is None or create_response is None:
        return _failure("create_http_error")
    if 500 <= create_response.status <= 599:
        # 5xx 不能报「已拒绝」：服务端收到了请求，任务可能已创建并计费，只是应答
        # 失败。与「响应丢失」同一终态码，让用户先去控制台确认再决定是否重发。
        return _failure("create_transport_error")
    if not 200 <= create_response.status < 300:
        return _failure("create_http_error")
    try:
        create_data = parse_response_object(create_response.body)
    except ResponseProblem:
        return _failure("create_response_invalid")
    task_value = create_data.get("id")
    if not isinstance(task_value, str):
        return _failure("create_response_invalid")
    try:
        task_id = validate_task_id(task_value, active_keys)
    except ResponseProblem:
        return _failure("invalid_task_id")

    logger.info(f"[auth] Using key from: {selected_key.source}")
    logger.info(f"Task ID: {task_id}")
    query_url = f"{namespace.base_url}{QUERY_ENDPOINT_PREFIX}/{task_id}?sync_upstream=true"
    # 次数制预算之外再压一条真墙钟上限（见 DEFAULT_POLL_WALL_CLOCK_BUDGET_SECONDS）。
    poll_budget_seconds = namespace.max_attempts * namespace.poll_interval
    poll_deadline = monotonic() + poll_budget_seconds
    for attempt in range(1, namespace.max_attempts + 1):
        if monotonic() >= poll_deadline:
            logger.info(
                f"[poll] wall-clock budget of {poll_budget_seconds}s is spent after "
                f"{attempt - 1} attempts; giving up on polling"
            )
            break
        try:
            # 幂等 GET：单次瞬时失败（429/5xx/网络异常）先消耗内层重试（3 次 +
            # 指数退避 + 日志），**不再让一次抖动击穿整个轮询预算**（ADR 0006
            # 规则 5）。内层重试穷尽才落到 query_transport_error 终态。
            response = _api_request_with_retry(
                api_transport,
                "GET",
                query_url,
                (("Authorization", f"Bearer {selected_key.value}"),),
                None,
                operation=f"poll task attempt {attempt}",
                retry_response_lost=True,
                sleeper=sleeper,
                logger=logger,
            )
        except Exception:
            return _failure("query_transport_error", task_id)
        if not 200 <= response.status < 300:
            return _failure("query_http_error", task_id)
        try:
            query_data = parse_response_object(response.body)
            status = query_data.get("status")
            if not isinstance(status, str) or status.lower() not in {
                "pending",
                "processing",
                "completed",
                "failed",
            }:
                raise ResponseProblem("query status is invalid")
            status = status.lower()
        except ResponseProblem:
            return _failure("query_response_invalid", task_id)
        result_count = len(query_data.get("results") or []) if isinstance(query_data.get("results"), list) else 0
        logger.info(
            f"[Attempt {attempt}/{namespace.max_attempts}] status={status} results={result_count}"
        )
        if status == "failed":
            return _failure("upstream_failed", task_id)
        if status == "completed":
            try:
                upstream_outputs = parse_completed_outputs(query_data, active_keys)
            except ResponseProblem:
                return _failure("query_response_invalid", task_id)
            if upstream_outputs is not None:
                if namespace.no_save:
                    outputs = [
                        {
                            "index": output.index,
                            "local_path": None,
                            "upstream_url": output.url,
                            "content_type": output.content_type,
                            "status": "not_saved",
                        }
                        for output in upstream_outputs
                    ]
                else:
                    outputs = download_outputs(
                        upstream_outputs,
                        output_dir,
                        output_stem(namespace, timestamp),
                        active_keys,
                        download_transport,
                        resolver,
                        publisher,
                        sleeper,
                        logger,
                    )
                return _success(task_id, outputs)
        if attempt < namespace.max_attempts:
            sleeper(namespace.poll_interval)
    return _timeout(task_id)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    api_transport: Any = None,
    download_transport: Any = None,
    resolver: Optional[Callable[[str, int], Sequence[str]]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    publisher: Callable[[Path, Path], None] = publish_no_replace,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    environ: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    environment: Mapping[str, str] = os.environ if environ is None else environ
    project_cwd = Path.cwd() if cwd is None else Path(cwd)
    project_cwd = Path(os.path.normpath(str(project_cwd.absolute())))
    user_home = Path.home() if home is None else Path(home)
    json_hint = "--json" in arguments
    parser = build_parser()
    try:
        namespace = parser.parse_args(arguments)
        if namespace.show_help:
            parser.print_help(file=output)
            return 0
        if namespace.base_url is None:
            namespace.base_url = environment.get("AIHUBMAX_BASE_URL", DEFAULT_BASE_URL)
        namespace = validate_arguments(namespace)
    except ArgumentProblem as exc:
        document = _failure("invalid_arguments")
        if json_hint:
            _emit_json(document, output)
        else:
            print(f"Error: {exc}", file=errors)
        return 1

    logger = Logger(namespace.json_mode, output, errors)
    try:
        keys = collect_keys(environment, project_cwd, user_home, namespace.use_local_key)
        if not keys:
            raise ConfigurationProblem("no API key is available")
        logger.set_secrets(key.value for key in keys)
        output_setting = (
            namespace.output_dir
            or environment.get("IMAGE_2_OUTPUT_DIR", "")
            or str(project_cwd)
        )
        output_dir = _normalized_absolute_path(output_setting, project_cwd)
        timestamp = clock().strftime("%Y%m%d-%H%M%S")
        _log_summary(namespace, keys, output_dir, logger)
        document = run_task(
            namespace,
            keys,
            [key.value for key in keys],
            StandardApiTransport() if api_transport is None else api_transport,
            StandardDownloadTransport() if download_transport is None else download_transport,
            default_resolver if resolver is None else resolver,
            sleeper,
            publisher,
            output_dir,
            timestamp,
            logger,
            monotonic,
        )
    except ConfigurationProblem:
        document = _failure("configuration_error")
    except Exception:
        document = _failure("internal_error")

    if namespace.json_mode:
        _emit_json(document, output)
    else:
        _emit_legacy(document, output, errors)
    return _exit_code(document)


if __name__ == "__main__":
    raise SystemExit(main())
