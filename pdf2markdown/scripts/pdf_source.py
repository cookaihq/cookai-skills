from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import shutil
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from email import policy
from email.parser import HeaderParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0

# ADR 0006 规则 3 默认参数：一次逻辑下载总尝试 3 次，第 n 次重试前等 2^(n-1) 秒。
NET_MAX_ATTEMPTS = 3

# 规则 2 的分类结果：下面这些 PdfSourceError 码是**瞬时网络故障**，同一个 URL
# 稍后重试有希望成功。其余码（source_authentication_required、source_http_error、
# invalid_pdf、source_size_limit_exceeded、source_peer_mismatch、
# source_redirect_* 等）是「来源本身不合格」的确定性错误，重试必然同样失败，
# 立即报错退出。
TRANSIENT_SOURCE_ERROR_CODES = frozenset(
    {
        "source_dns_failed",
        "source_connect_timeout",
        "source_connect_failed",
        "source_read_timeout",
        "source_read_failed",
    }
)
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_DISK_BYTES = 256 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class PdfSourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedHttpsUrl:
    request_url: str
    request_target: str
    host: str
    port: int
    redacted: str
    original_name: str


@dataclass(frozen=True)
class ResolvedEndpoint:
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple
    canonical_ip: str


@dataclass(frozen=True)
class DownloadedPdf:
    sha256: str
    size_bytes: int
    original_name: str
    origin: dict


class _ProductionResponse:
    def __init__(self, response, sock):
        self._response = response
        self._socket = sock
        self.status = response.status
        self.headers = response.headers

    def read(self, size: int, *, timeout: float) -> bytes:
        self._socket.settimeout(timeout)
        return self._response.read(size)

    def close(self) -> None:
        self._response.close()


class _ProductionSession:
    def __init__(self, *, host: str, port: int, sock, peer_ip: str):
        self._host = host
        self._port = port
        self._socket = sock
        self._connection = None
        self.peer_ip = peer_ip

    def get(
        self,
        request_target: str,
        *,
        headers,
        read_timeout: float,
        redirects: bool,
        retries: int,
    ):
        if redirects or retries != 0 or not request_target.startswith("/"):
            raise PdfSourceError(
                "invalid_transport_contract", "The HTTPS transport contract is invalid."
            )
        if "#" in request_target or "://" in request_target:
            raise PdfSourceError(
                "invalid_transport_contract", "The HTTPS transport contract is invalid."
            )
        self._socket.settimeout(read_timeout)
        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=read_timeout
        )
        connection.sock = self._socket
        self._connection = connection
        try:
            connection.request("GET", request_target, headers=dict(headers))
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            raise PdfSourceError(
                "source_read_failed", "The HTTPS source response could not be read."
            ) from exc
        return _ProductionResponse(response, self._socket)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
        else:
            self._socket.close()


class ProductionHttpsTransport:
    def __init__(self, *, ssl_context=None):
        self._ssl_context = (
            ssl.create_default_context() if ssl_context is None else ssl_context
        )

    def resolve(self, host: str, port: int):
        try:
            records = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise PdfSourceError(
                "source_dns_failed", "The HTTPS source hostname could not be resolved."
            ) from exc
        return [
            ResolvedEndpoint(
                family=family,
                socktype=socktype,
                protocol=protocol,
                sockaddr=tuple(sockaddr),
                canonical_ip=_normalize_ip(sockaddr[0]),
            )
            for family, socktype, protocol, _canonical, sockaddr in records
        ]

    def connect_https(
        self,
        host: str,
        port: int,
        *,
        endpoint: ResolvedEndpoint,
        server_hostname: str,
        connect_timeout: float,
        proxies: bool,
    ):
        if proxies or server_hostname != host:
            raise PdfSourceError(
                "invalid_transport_contract", "The HTTPS transport contract is invalid."
        )
        _validate_endpoint(endpoint, port=port)
        raw_socket = tls_socket = None
        deadline = time.monotonic() + connect_timeout
        try:
            raw_socket = socket.socket(
                endpoint.family, endpoint.socktype, endpoint.protocol
            )
            raw_socket.settimeout(max(0.001, deadline - time.monotonic()))
            raw_socket.connect(endpoint.sockaddr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            raw_socket.settimeout(remaining)
            tls_socket = self._ssl_context.wrap_socket(
                raw_socket, server_hostname=server_hostname
            )
            raw_socket = None
            peer_ip = _normalize_ip(tls_socket.getpeername()[0])
            return _ProductionSession(
                host=host,
                port=port,
                sock=tls_socket,
                peer_ip=peer_ip,
            )
        except (socket.timeout, TimeoutError) as exc:
            if tls_socket is not None:
                tls_socket.close()
            if raw_socket is not None:
                raw_socket.close()
            raise PdfSourceError(
                "source_connect_timeout", "The HTTPS source connection timed out."
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            if tls_socket is not None:
                tls_socket.close()
            if raw_socket is not None:
                raw_socket.close()
            raise PdfSourceError(
                "source_connect_failed",
                "The HTTPS source connection could not be established.",
            ) from exc


def is_url_input(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "://" in value
    return parsed.scheme.lower() in {"http", "https", "ftp", "file"} or bool(
        parsed.scheme and "://" in value
    )


def _bounded_name(path: str) -> str:
    name = unquote(path.rsplit("/", 1)[-1]) or "document.pdf"
    name = name.replace("/", "-").replace("\\", "-")
    name = "".join(
        character
        for character in name
        if ord(character) >= 32 and ord(character) != 127
    )
    return name[:240] or "document.pdf"


def _normalized_host(hostname: str) -> str:
    if not hostname or "%" in hostname or hostname.endswith("."):
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if (
            len(hostname) > 253
            or not labels
            or any(HOST_LABEL_RE.fullmatch(label) is None for label in labels)
            or (len(labels) == 1 and labels[0].isdigit())
        ):
            raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
        return hostname.lower()
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    return str(ip)


def parse_https_url(value: str) -> ParsedHttpsUrl:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError as exc:
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.") from exc
    if parsed.scheme.lower() != "https":
        raise PdfSourceError(
            "unsafe_source_scheme", "Public URL sources must use HTTPS."
        )
    if username is not None or password is not None:
        raise PdfSourceError(
            "source_authentication_not_supported",
            "Authenticated source URLs are not supported.",
        )
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    if hostname is None:
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    host = _normalized_host(hostname)
    effective_port = 443 if port is None else port
    if effective_port < 1 or effective_port > 65535:
        raise PdfSourceError("invalid_source_url", "The source URL is invalid.")
    host_text = f"[{host}]" if ":" in host else host
    netloc = host_text if effective_port == 443 else f"{host_text}:{effective_port}"
    path = parsed.path or "/"
    request_target = urlunsplit(("", "", path, parsed.query, ""))
    request_url = urlunsplit(("https", netloc, path, parsed.query, ""))
    redacted = urlunsplit(("https", netloc, path, "", ""))
    return ParsedHttpsUrl(
        request_url=request_url,
        request_target=request_target,
        host=host,
        port=effective_port,
        redacted=redacted,
        original_name=_bounded_name(path),
    )


def _normalize_ip(value: str) -> str:
    if not isinstance(value, str) or not value or "%" in value:
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        ) from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    return str(ip)


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_unspecified
        and not ip.is_multicast
    )


def _validate_endpoint(endpoint, *, port: int) -> ResolvedEndpoint:
    if not isinstance(endpoint, ResolvedEndpoint):
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    if (
        endpoint.family not in {socket.AF_INET, socket.AF_INET6}
        or endpoint.socktype != socket.SOCK_STREAM
        or endpoint.protocol not in {0, socket.IPPROTO_TCP}
        or not isinstance(endpoint.sockaddr, tuple)
    ):
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    expected_size = 2 if endpoint.family == socket.AF_INET else 4
    if len(endpoint.sockaddr) != expected_size:
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    if type(endpoint.sockaddr[1]) is not int or endpoint.sockaddr[1] != port:
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    if endpoint.family == socket.AF_INET6 and (
        endpoint.sockaddr[2] != 0 or endpoint.sockaddr[3] != 0
    ):
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    normalized = _normalize_ip(endpoint.sockaddr[0])
    if (
        normalized != endpoint.canonical_ip
        or (endpoint.family == socket.AF_INET and ":" in normalized)
        or (endpoint.family == socket.AF_INET6 and ":" not in normalized)
    ):
        raise PdfSourceError(
            "source_dns_invalid", "The HTTPS source resolved to an invalid address."
        )
    return endpoint


def _verified_public_endpoints(values, *, port: int) -> list[ResolvedEndpoint]:
    if not isinstance(values, (list, tuple)) or not values:
        raise PdfSourceError(
            "source_dns_failed", "The HTTPS source hostname did not resolve."
        )
    endpoints = [_validate_endpoint(value, port=port) for value in values]
    if not all(_is_public_ip(endpoint.canonical_ip) for endpoint in endpoints):
        raise PdfSourceError(
            "unsafe_source_address",
            "The HTTPS source resolved to a non-public address.",
        )
    deduplicated = []
    identities = set()
    for endpoint in endpoints:
        identity = (
            endpoint.family,
            endpoint.socktype,
            endpoint.protocol,
            endpoint.sockaddr,
            endpoint.canonical_ip,
        )
        if identity not in identities:
            identities.add(identity)
            deduplicated.append(endpoint)
    return deduplicated


def _header_values(headers, name: str) -> list[str]:
    if hasattr(headers, "get_all"):
        return [
            value
            for value in headers.get_all(name, [])
            if isinstance(value, str)
        ]
    if isinstance(headers, (list, tuple)):
        values = []
        for item in headers:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
                and item[0].lower() == name.lower()
                and isinstance(item[1], str)
            ):
                values.append(item[1])
        return values
    if not hasattr(headers, "items"):
        return []
    return [
        value
        for key, value in headers.items()
        if isinstance(key, str)
        and key.lower() == name.lower()
        and isinstance(value, str)
    ]


def _content_type(headers) -> str:
    values = _header_values(headers, "Content-Type")
    if len(values) != 1 or any(
        (ord(character) < 32 and character != "\t") or ord(character) == 127
        for character in values[0]
    ):
        raise PdfSourceError(
            "invalid_pdf_content_type",
            "The HTTPS response does not declare a PDF content type.",
        )
    message = HeaderParser(policy=policy.default).parsestr(
        f"Content-Type: {values[0]}\n\n"
    )
    header = message["Content-Type"]
    media_type = header.content_type.lower()
    if header.defects or media_type != "application/pdf":
        raise PdfSourceError(
            "invalid_pdf_content_type",
            "The HTTPS response does not declare a PDF content type.",
        )
    return media_type


def _declared_length(headers) -> int | None:
    values = _header_values(headers, "Content-Length")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0].strip()) is None:
        raise PdfSourceError(
            "invalid_source_response", "The HTTPS source response is invalid."
        )
    return int(values[0].strip())


def _validate_content_encoding(headers) -> None:
    values = _header_values(headers, "Content-Encoding")
    if len(values) > 1 or (values and values[0].strip().lower() != "identity"):
        raise PdfSourceError(
            "unsupported_content_encoding",
            "The HTTPS source uses an unsupported content encoding.",
        )


def validate_pdf_identity(path: Path) -> None:
    # ADR 0007 §1.5：依赖预检的捕获面必须宽于 ImportError。真实 import 会执行包的
    # 顶层代码，半残环境（传递依赖缺失、二进制架构不符、动态库缺失）里那里抛出的
    # 可能是 OSError / RuntimeError 等任意异常；只接 ImportError 会让它们穿过这层
    # 映射、以未分类异常炸出去。统一映射为 pdf_parser_unavailable 并带上底层原文。
    try:
        import fitz
    except Exception as exc:  # noqa: BLE001 - 见上方注释
        raise PdfSourceError(
            "pdf_parser_unavailable",
            "PyMuPDF is required to validate the source PDF (%s: %s)."
            % (type(exc).__name__, exc),
        ) from exc
    previous_errors = bool(fitz.TOOLS.mupdf_display_errors())
    previous_warnings = bool(fitz.TOOLS.mupdf_display_warnings())
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    fitz.TOOLS.reset_mupdf_warnings()
    try:
        document = fitz.open(str(path))
        try:
            if not document.is_pdf:
                raise PdfSourceError(
                    "invalid_pdf", "The source does not contain a parseable PDF."
                )
            document.page_count
        finally:
            document.close()
    except PdfSourceError:
        raise
    except Exception as exc:
        raise PdfSourceError(
            "invalid_pdf", "The source does not contain a parseable PDF."
        ) from exc
    finally:
        fitz.TOOLS.reset_mupdf_warnings()
        fitz.TOOLS.mupdf_display_errors(previous_errors)
        fitz.TOOLS.mupdf_display_warnings(previous_warnings)


def _close_quietly(value) -> None:
    try:
        value.close()
    except Exception:
        pass


def _open_response(client, parsed: ParsedHttpsUrl):
    try:
        endpoints = _verified_public_endpoints(
            client.resolve(parsed.host, parsed.port), port=parsed.port
        )
    except PdfSourceError:
        raise
    except Exception as exc:
        raise PdfSourceError(
            "source_dns_failed", "The HTTPS source hostname could not be resolved."
        ) from exc
    endpoint = endpoints[0]
    try:
        session = client.connect_https(
            parsed.host,
            parsed.port,
            endpoint=endpoint,
            server_hostname=parsed.host,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            proxies=False,
        )
    except PdfSourceError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise PdfSourceError(
            "source_connect_timeout", "The HTTPS source connection timed out."
        ) from exc
    except Exception as exc:
        raise PdfSourceError(
            "source_connect_failed", "The HTTPS source connection could not be established."
        ) from exc
    response = None
    try:
        if not all(
            hasattr(session, attribute) for attribute in ("peer_ip", "get", "close")
        ):
            raise PdfSourceError(
                "invalid_transport_contract", "The HTTPS transport contract is invalid."
            )
        peer_ip = _normalize_ip(session.peer_ip)
        resolved_ips = [item.canonical_ip for item in endpoints]
        if (
            not _is_public_ip(peer_ip)
            or peer_ip not in resolved_ips
            or peer_ip != endpoint.canonical_ip
        ):
            raise PdfSourceError(
                "source_peer_mismatch",
                "The connected peer does not match the verified public addresses.",
            )
        response = session.get(
            parsed.request_target,
            headers={
                "Accept": "application/pdf",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "pdf2markdown/1",
            },
            read_timeout=READ_TIMEOUT_SECONDS,
            redirects=False,
            retries=0,
        )
        if not all(
            hasattr(response, attribute)
            for attribute in ("status", "headers", "read", "close")
        ):
            raise PdfSourceError(
                "invalid_source_response", "The HTTPS source response is invalid."
            )
        return endpoints, peer_ip, session, response
    except PdfSourceError:
        if response is not None:
            _close_quietly(response)
        _close_quietly(session)
        raise
    except (TimeoutError, socket.timeout) as exc:
        _close_quietly(session)
        raise PdfSourceError(
            "source_read_timeout", "The HTTPS source response timed out."
        ) from exc
    except Exception as exc:
        _close_quietly(session)
        raise PdfSourceError(
            "source_read_failed", "The HTTPS source response could not be read."
        ) from exc


def _stream_response(response, destination: Path, *, headers) -> tuple[str, int]:
    declared_length = _declared_length(headers)
    if declared_length is not None and (
        declared_length > MAX_SOURCE_BYTES
        or declared_length > MAX_SOURCE_DISK_BYTES
    ):
        raise PdfSourceError(
            "source_size_limit_exceeded",
            "The HTTPS source exceeds the download size limit.",
        )
    descriptor = None
    completed = False
    try:
        try:
            descriptor = os.open(
                str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise PdfSourceError(
                "source_disk_write_failed",
                "The HTTPS source could not be written to the work bundle.",
            ) from exc
        digest = hashlib.sha256()
        size = 0
        header = bytearray()
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfSourceError(
                    "source_read_timeout", "The HTTPS source download timed out."
                )
            try:
                chunk = response.read(STREAM_CHUNK_BYTES, timeout=remaining)
            except (TimeoutError, socket.timeout) as exc:
                raise PdfSourceError(
                    "source_read_timeout", "The HTTPS source download timed out."
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise PdfSourceError(
                    "source_read_failed", "The HTTPS source download failed."
                ) from exc
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise PdfSourceError(
                    "invalid_source_response", "The HTTPS source response is invalid."
                )
            new_size = size + len(chunk)
            if new_size > MAX_SOURCE_BYTES or new_size > MAX_SOURCE_DISK_BYTES:
                raise PdfSourceError(
                    "source_size_limit_exceeded",
                    "The HTTPS source exceeds the download size limit.",
                )
            try:
                free_bytes = shutil.disk_usage(destination.parent).free
            except OSError as exc:
                raise PdfSourceError(
                    "source_disk_write_failed",
                    "The HTTPS source disk capacity could not be checked.",
                ) from exc
            if free_bytes < len(chunk):
                raise PdfSourceError(
                    "source_disk_limit_exceeded",
                    "There is not enough bounded disk capacity for the HTTPS source.",
                )
            if len(header) < 5:
                header.extend(chunk[: 5 - len(header)])
                if len(header) == 5 and bytes(header) != b"%PDF-":
                    raise PdfSourceError(
                        "invalid_pdf", "The HTTPS source does not contain PDF bytes."
                    )
            try:
                offset = 0
                while offset < len(chunk):
                    written = os.write(descriptor, chunk[offset:])
                    if written <= 0:
                        raise OSError("short disk write")
                    offset += written
            except OSError as exc:
                raise PdfSourceError(
                    "source_disk_write_failed",
                    "The HTTPS source could not be written to the work bundle.",
                ) from exc
            digest.update(chunk)
            size = new_size
        if declared_length is not None and declared_length != size:
            raise PdfSourceError(
                "invalid_source_response", "The HTTPS source response is incomplete."
            )
        if bytes(header) != b"%PDF-":
            raise PdfSourceError(
                "invalid_pdf", "The HTTPS source does not contain PDF bytes."
            )
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise PdfSourceError(
                "source_disk_write_failed",
                "The HTTPS source could not be written to the work bundle.",
            ) from exc
        completed = True
        return digest.hexdigest(), size
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass


def download_https_pdf(
    source_url: str,
    destination: Path,
    *,
    transport=None,
    sleep=None,
) -> DownloadedPdf:
    """下载公开 HTTPS PDF 到 `destination`。

    这是幂等 GET（同一 URL 反复取同一份内容，不产生远端副作用），所以按 ADR 0006
    规则 2/3 对**瞬时网络故障**重试：总尝试 3 次、退避 1s/2s、每次向 stderr 打一行
    重试日志。瞬时的判定见 `TRANSIENT_SOURCE_ERROR_CODES`——DNS 解析失败、连接
    超时/失败、读超时/读失败；「来源本身不合格」（需要鉴权、非 200、非 PDF、超限、
    对端不匹配、重定向异常）是确定性错误，立即抛出不重试。

    重试前会清掉上一轮可能残留的半截文件：`_stream_response` 以 `O_EXCL` 创建
    目标文件，残留会让下一次尝试直接以 `source_disk_write_failed` 失败。
    """
    wait = time.sleep if sleep is None else sleep
    for attempt in range(1, NET_MAX_ATTEMPTS + 1):
        try:
            return _download_https_pdf_once(source_url, destination, transport=transport)
        except PdfSourceError as exc:
            if (
                exc.code not in TRANSIENT_SOURCE_ERROR_CODES
                or attempt == NET_MAX_ATTEMPTS
            ):
                raise
            _discard_partial_download(destination)
            delay = 2 ** (attempt - 1)
            # 日志走 stderr（stdout 是 workflow 的结构化 JSON）；只打错误码，
            # 不打 URL——query 按敏感数据处理，见 README「安全边界」。
            sys.stderr.write(
                "[pdf2markdown] source download retry %d/%d after %s; waiting %ds\n"
                % (attempt, NET_MAX_ATTEMPTS, exc.code, delay)
            )
            wait(delay)
    raise AssertionError("unreachable")


def _discard_partial_download(destination: Path) -> None:
    # `FileNotFoundError` 是 `OSError` 的子类，单列它是死代码——`OSError` 已经把
    # 「文件不在」「权限不足」「目录只读」全部收下，重试前清残留失败不该终止重试。
    try:
        destination.unlink()
    except OSError:
        pass


def _download_https_pdf_once(
    source_url: str,
    destination: Path,
    *,
    transport=None,
) -> DownloadedPdf:
    initial = parse_https_url(source_url)
    current = initial
    client = ProductionHttpsTransport() if transport is None else transport
    visited = set()
    hops = []
    redirect_count = 0
    while True:
        if current.request_url in visited:
            raise PdfSourceError(
                "source_redirect_loop", "The HTTPS source redirect chain contains a loop."
            )
        visited.add(current.request_url)
        endpoints, peer_ip, session, response = _open_response(client, current)
        try:
            status = response.status
            if type(status) is not int:
                raise PdfSourceError(
                    "invalid_source_response", "The HTTPS source response is invalid."
                )
            hop = {
                "url": current.redacted,
                "status_code": status,
                "resolved_addresses": list(
                    dict.fromkeys(endpoint.canonical_ip for endpoint in endpoints)
                ),
                "peer_ip": peer_ip,
            }
            if status in REDIRECT_STATUSES:
                locations = _header_values(response.headers, "Location")
                if len(locations) != 1 or not locations[0]:
                    raise PdfSourceError(
                        "invalid_source_redirect",
                        "The HTTPS source redirect is missing one valid location.",
                    )
                if redirect_count >= MAX_REDIRECTS:
                    raise PdfSourceError(
                        "source_redirect_limit_exceeded",
                        "The HTTPS source exceeds the redirect limit.",
                    )
                next_value = urljoin(current.request_url, locations[0])
                next_url = parse_https_url(next_value)
                hops.append(hop)
                redirect_count += 1
                current = next_url
                continue
            if status in {401, 403}:
                raise PdfSourceError(
                    "source_authentication_required",
                    "The HTTPS source requires authentication or denies public access.",
                )
            if status != 200:
                raise PdfSourceError(
                    "source_http_error", "The HTTPS source did not return a PDF."
                )
            content_type = _content_type(response.headers)
            _validate_content_encoding(response.headers)
            source_hash, source_size = _stream_response(
                response, destination, headers=response.headers
            )
            hops.append(hop)
        finally:
            _close_quietly(response)
            _close_quietly(session)
        try:
            validate_pdf_identity(destination)
        except Exception:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise
        return DownloadedPdf(
            sha256=source_hash,
            size_bytes=source_size,
            original_name=current.original_name,
            origin={
                "kind": "https",
                "initial_url": initial.redacted,
                "final_url": current.redacted,
                "input_url_sha256": hashlib.sha256(
                    source_url.encode("utf-8")
                ).hexdigest(),
                "download": {
                    "content_type": content_type,
                    "redirect_count": redirect_count,
                    "hops": hops,
                },
            },
        )


def _valid_redacted_url(value) -> bool:
    try:
        parsed = parse_https_url(value)
    except (PdfSourceError, TypeError):
        return False
    return parsed.request_url == parsed.redacted == value


def valid_origin(origin) -> bool:
    if not isinstance(origin, dict):
        return False
    if origin.get("kind") == "local":
        return (
            set(origin) == {"kind", "path"}
            and isinstance(origin.get("path"), str)
            and bool(origin["path"])
        )
    if set(origin) != {
        "kind",
        "initial_url",
        "final_url",
        "input_url_sha256",
        "download",
    } or origin.get("kind") != "https":
        return False
    if not _valid_redacted_url(origin.get("initial_url")) or not _valid_redacted_url(
        origin.get("final_url")
    ):
        return False
    input_hash = origin.get("input_url_sha256")
    download = origin.get("download")
    if (
        not isinstance(input_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", input_hash) is None
        or not isinstance(download, dict)
        or set(download) != {"content_type", "redirect_count", "hops"}
        or download.get("content_type") != "application/pdf"
        or type(download.get("redirect_count")) is not int
        or download["redirect_count"] < 0
        or download["redirect_count"] > MAX_REDIRECTS
        or not isinstance(download.get("hops"), list)
        or len(download["hops"]) != download["redirect_count"] + 1
    ):
        return False
    for index, hop in enumerate(download["hops"]):
        if not isinstance(hop, dict) or set(hop) != {
            "url",
            "status_code",
            "resolved_addresses",
            "peer_ip",
        }:
            return False
        addresses = hop.get("resolved_addresses")
        try:
            normalized_addresses = [_normalize_ip(value) for value in addresses]
            peer = _normalize_ip(hop.get("peer_ip"))
        except (PdfSourceError, TypeError):
            return False
        expected_status = 200 if index == len(download["hops"]) - 1 else None
        if (
            not _valid_redacted_url(hop.get("url"))
            or not isinstance(addresses, list)
            or not addresses
            or len(set(normalized_addresses)) != len(normalized_addresses)
            or not all(_is_public_ip(value) for value in normalized_addresses)
            or peer not in normalized_addresses
            or not _is_public_ip(peer)
            or type(hop.get("status_code")) is not int
            or (expected_status == 200 and hop["status_code"] != 200)
            or (expected_status is None and hop["status_code"] not in REDIRECT_STATUSES)
            or (index == 0 and hop["url"] != origin["initial_url"])
            or (index == len(download["hops"]) - 1 and hop["url"] != origin["final_url"])
        ):
            return False
    return True
