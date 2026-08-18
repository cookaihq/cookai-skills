from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import stat
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urljoin

import pdf_source


MAX_ARCHIVE_BYTES = pdf_source.MAX_SOURCE_BYTES
MAX_TOTAL_COMPRESSED_BYTES = MAX_ARCHIVE_BYTES
MAX_TOTAL_UNCOMPRESSED_BYTES = pdf_source.MAX_SOURCE_BYTES
MAX_MEMBER_BYTES = MAX_TOTAL_UNCOMPRESSED_BYTES
MAX_STAGING_DISK_BYTES = MAX_ARCHIVE_BYTES + MAX_TOTAL_UNCOMPRESSED_BYTES
MAX_MEMBERS = zipfile.ZIP_FILECOUNT_LIMIT
MAX_MEMBER_PATH_BYTES = 1024
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_DEPTH = 128
MAX_TOTAL_PATH_COMPONENTS = zipfile.ZIP_FILECOUNT_LIMIT
MAX_REDIRECTS = pdf_source.MAX_REDIRECTS
CONNECT_TIMEOUT_SECONDS = pdf_source.CONNECT_TIMEOUT_SECONDS
READ_TIMEOUT_SECONDS = pdf_source.READ_TIMEOUT_SECONDS

# ADR 0006 规则 3 默认参数：一次逻辑下载总尝试 3 次，第 n 次重试前等 2^(n-1) 秒。
NET_MAX_ATTEMPTS = pdf_source.NET_MAX_ATTEMPTS

# 规则 2 的分类结果：这些码是**瞬时网络故障**，同一个结果 URL 稍后重试有希望
# 成功。其余码（result_url_unavailable 的 401/403/404、unsafe_result_*、
# result_peer_mismatch、archive_size_limit_exceeded、result_disk_write_failed、
# invalid_result_* 等）是确定性错误或本地故障，重试必然同样失败。
TRANSIENT_RESULT_ERROR_CODES = frozenset(
    {
        "result_dns_failed",
        "result_connect_timeout",
        "result_connect_failed",
        "result_read_timeout",
        "result_download_failed",
        "result_download_transient",
    }
)
STREAM_CHUNK_BYTES = pdf_source.STREAM_CHUNK_BYTES
SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


class ResultArchiveError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PreparedArchive:
    archive_sha256: str
    archive_size_bytes: int
    tree_sha256: str
    member_count: int
    total_compressed_bytes: int
    total_uncompressed_bytes: int
    main_markdown_path: str | None
    main_markdown_sha256: str | None
    reason_code: str | None


def limits_record() -> dict:
    return {
        "max_archive_bytes": MAX_ARCHIVE_BYTES,
        "max_total_compressed_bytes": MAX_TOTAL_COMPRESSED_BYTES,
        "max_total_uncompressed_bytes": MAX_TOTAL_UNCOMPRESSED_BYTES,
        "max_member_bytes": MAX_MEMBER_BYTES,
        "max_staging_disk_bytes": MAX_STAGING_DISK_BYTES,
        "max_members": MAX_MEMBERS,
        "max_member_path_bytes": MAX_MEMBER_PATH_BYTES,
        "max_path_component_bytes": MAX_PATH_COMPONENT_BYTES,
        "max_path_depth": MAX_PATH_DEPTH,
        "max_total_path_components": MAX_TOTAL_PATH_COMPONENTS,
        "supported_compression": [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    }


def _fsync_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ResultArchiveError(
            "result_disk_write_failed",
            "The prepared result could not be made durable.",
        ) from exc


def _raise_pdf_error(exc: pdf_source.PdfSourceError) -> None:
    mapping = {
        "unsafe_source_scheme": "unsafe_result_url",
        "invalid_source_url": "unsafe_result_url",
        "source_authentication_not_supported": "unsafe_result_url",
        "unsafe_source_address": "unsafe_result_address",
        "source_peer_mismatch": "result_peer_mismatch",
        "source_dns_failed": "result_dns_failed",
        "source_dns_invalid": "result_dns_invalid",
        "source_connect_timeout": "result_connect_timeout",
        "source_connect_failed": "result_connect_failed",
        "source_read_timeout": "result_read_timeout",
        "source_read_failed": "result_download_failed",
    }
    raise ResultArchiveError(
        mapping.get(exc.code, "result_download_failed"),
        "The Doc2X result URL could not be downloaded safely.",
    ) from exc


def _open_response(client, parsed):
    try:
        endpoints = pdf_source._verified_public_endpoints(
            client.resolve(parsed.host, parsed.port), port=parsed.port
        )
        endpoint = endpoints[0]
        session = client.connect_https(
            parsed.host,
            parsed.port,
            endpoint=endpoint,
            server_hostname=parsed.host,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            proxies=False,
        )
    except pdf_source.PdfSourceError as exc:
        _raise_pdf_error(exc)
    except (TimeoutError, socket.timeout) as exc:
        raise ResultArchiveError(
            "result_connect_timeout", "The Doc2X result connection timed out."
        ) from exc
    except Exception as exc:
        raise ResultArchiveError(
            "result_connect_failed", "The Doc2X result connection failed."
        ) from exc
    response = None
    try:
        if not all(
            hasattr(session, attribute) for attribute in ("peer_ip", "get", "close")
        ):
            raise ResultArchiveError(
                "invalid_transport_contract", "The result transport is invalid."
            )
        try:
            peer_ip = pdf_source._normalize_ip(session.peer_ip)
        except pdf_source.PdfSourceError as exc:
            _raise_pdf_error(exc)
        resolved_ips = [item.canonical_ip for item in endpoints]
        if (
            not pdf_source._is_public_ip(peer_ip)
            or peer_ip not in resolved_ips
            or peer_ip != endpoint.canonical_ip
        ):
            raise ResultArchiveError(
                "result_peer_mismatch",
                "The result peer does not match the verified public address.",
            )
        response = session.get(
            parsed.request_target,
            headers={
                "Accept": "application/zip, application/octet-stream",
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
            raise ResultArchiveError(
                "invalid_result_response", "The result response is invalid."
            )
        return endpoints, peer_ip, session, response
    except pdf_source.PdfSourceError as exc:
        if response is not None:
            pdf_source._close_quietly(response)
        pdf_source._close_quietly(session)
        _raise_pdf_error(exc)
    except Exception:
        if response is not None:
            pdf_source._close_quietly(response)
        pdf_source._close_quietly(session)
        raise


def _declared_length(headers) -> int | None:
    values = pdf_source._header_values(headers, "Content-Length")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0].strip()) is None:
        raise ResultArchiveError(
            "invalid_result_response", "The result response length is invalid."
        )
    return int(values[0].strip())


def _validate_encoding(headers) -> None:
    values = pdf_source._header_values(headers, "Content-Encoding")
    if len(values) > 1 or (values and values[0].strip().lower() != "identity"):
        raise ResultArchiveError(
            "unsupported_result_encoding",
            "The result response uses an unsupported content encoding.",
        )


def _stream_archive(
    response, destination_name: str, *, destination_fd: int
) -> tuple[str, int]:
    declared = _declared_length(response.headers)
    if declared is not None and declared > MAX_ARCHIVE_BYTES:
        raise ResultArchiveError(
            "archive_size_limit_exceeded", "The result ZIP exceeds its size limit."
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(
            destination_name, flags, 0o600, dir_fd=destination_fd
        )
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        size = 0
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResultArchiveError(
                    "result_read_timeout", "The result download timed out."
                )
            try:
                chunk = response.read(STREAM_CHUNK_BYTES, timeout=remaining)
            except (TimeoutError, socket.timeout) as exc:
                raise ResultArchiveError(
                    "result_read_timeout", "The result download timed out."
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise ResultArchiveError(
                    "result_download_failed", "The result download failed."
                ) from exc
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ResultArchiveError(
                    "invalid_result_response", "The result response body is invalid."
                )
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ResultArchiveError(
                    "archive_size_limit_exceeded",
                    "The result ZIP exceeds its size limit.",
                )
            try:
                filesystem = os.fstatvfs(descriptor)
                if filesystem.f_bavail * filesystem.f_frsize < len(chunk):
                    raise ResultArchiveError(
                        "staging_disk_limit_exceeded",
                        "The result ZIP exceeds available bounded disk capacity.",
                    )
            except OSError as exc:
                raise ResultArchiveError(
                    "result_disk_write_failed", "Result disk capacity is unavailable."
                ) from exc
            offset = 0
            while offset < len(chunk):
                try:
                    written = os.write(descriptor, chunk[offset:])
                except OSError as exc:
                    raise ResultArchiveError(
                        "result_disk_write_failed", "The result ZIP could not be saved."
                    ) from exc
                if written <= 0:
                    raise ResultArchiveError(
                        "result_disk_write_failed", "The result ZIP could not be saved."
                    )
                offset += written
            digest.update(chunk)
        if declared is not None and declared != size:
            raise ResultArchiveError(
                "invalid_result_response", "The result response is incomplete."
            )
        _fsync_descriptor(descriptor)
        return digest.hexdigest(), size
    finally:
        if descriptor is not None:
            os.close(descriptor)


def download_archive(
    url: str,
    destination_name: str,
    *,
    destination_fd: int,
    transport=None,
) -> tuple[str, int]:
    try:
        initial = pdf_source.parse_https_url(url)
    except pdf_source.PdfSourceError as exc:
        _raise_pdf_error(exc)
    current = initial
    client = pdf_source.ProductionHttpsTransport() if transport is None else transport
    visited = set()
    redirects = 0
    while True:
        if current.request_url in visited:
            raise ResultArchiveError(
                "result_redirect_loop", "The result redirect chain contains a loop."
            )
        visited.add(current.request_url)
        _endpoints, _peer, session, response = _open_response(client, current)
        try:
            status = response.status
            if type(status) is not int:
                raise ResultArchiveError(
                    "invalid_result_response", "The result response status is invalid."
                )
            if status in pdf_source.REDIRECT_STATUSES:
                locations = pdf_source._header_values(response.headers, "Location")
                if len(locations) != 1 or not locations[0]:
                    raise ResultArchiveError(
                        "invalid_result_redirect", "The result redirect is invalid."
                    )
                if redirects >= MAX_REDIRECTS:
                    raise ResultArchiveError(
                        "result_redirect_limit_exceeded",
                        "The result redirect limit was exceeded.",
                    )
                try:
                    current = pdf_source.parse_https_url(
                        urljoin(current.request_url, locations[0])
                    )
                except pdf_source.PdfSourceError as exc:
                    _raise_pdf_error(exc)
                redirects += 1
                continue
            if status in {401, 403, 404}:
                raise ResultArchiveError(
                    "result_url_unavailable", "The result URL is no longer available."
                )
            if status != 200:
                raise ResultArchiveError(
                    "result_download_transient", "The result URL did not return a ZIP."
                )
            _validate_encoding(response.headers)
            downloaded = _stream_archive(
                response, destination_name, destination_fd=destination_fd
            )
            _fsync_descriptor(destination_fd)
            return downloaded
        finally:
            pdf_source._close_quietly(response)
            pdf_source._close_quietly(session)


def _member_path(info: zipfile.ZipInfo) -> tuple[str, bool]:
    name = info.filename
    original_name = info.orig_filename
    if (
        not isinstance(name, str)
        or not isinstance(original_name, str)
        or original_name != name
        or not name
        or "\x00" in name
        or "\x00" in original_name
        or "\\" in name
        or name.startswith("/")
        or DRIVE_PREFIX.match(name)
    ):
        raise ResultArchiveError(
            "unsafe_archive_path", "The result ZIP contains an unsafe member path."
        )
    is_directory = info.is_dir()
    path_name = name[:-1] if is_directory and name.endswith("/") else name
    if (
        not path_name
        or path_name.startswith("/")
        or path_name.endswith("/")
        or "//" in path_name
    ):
        raise ResultArchiveError(
            "unsafe_archive_path", "The result ZIP contains an unsafe member path."
        )
    parts = path_name.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ResultArchiveError(
            "unsafe_archive_path", "The result ZIP contains an unsafe member path."
        )
    try:
        path_size = len(path_name.encode("utf-8"))
        component_sizes = [len(part.encode("utf-8")) for part in parts]
    except UnicodeEncodeError as exc:
        raise ResultArchiveError(
            "unsafe_archive_path", "The result ZIP contains an unsafe member path."
        ) from exc
    if len(parts) > MAX_PATH_DEPTH:
        raise ResultArchiveError(
            "archive_path_depth_limit_exceeded",
            "A result ZIP member exceeds the path-depth limit.",
        )
    if path_size > MAX_MEMBER_PATH_BYTES:
        raise ResultArchiveError(
            "archive_member_path_limit_exceeded",
            "A result ZIP member path exceeds its byte limit.",
        )
    if any(size > MAX_PATH_COMPONENT_BYTES for size in component_sizes):
        raise ResultArchiveError(
            "archive_path_component_limit_exceeded",
            "A result ZIP path component exceeds its byte limit.",
        )
    return path_name, is_directory


def _member_kind(info: zipfile.ZipInfo, *, is_directory: bool) -> str:
    if info.flag_bits & 0x1:
        raise ResultArchiveError(
            "encrypted_archive_unsupported", "Encrypted ZIP members are not supported."
        )
    if info.compress_type not in SUPPORTED_COMPRESSION:
        raise ResultArchiveError(
            "unsupported_archive_compression",
            "The result ZIP uses unsupported compression.",
        )
    if is_directory and (info.file_size != 0 or info.compress_size != 0):
        raise ResultArchiveError(
            "unsupported_archive_member_type",
            "The result ZIP contains a directory with payload data.",
        )
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if is_directory and file_type not in {0, stat.S_IFDIR}:
        raise ResultArchiveError(
            "unsupported_archive_member_type",
            "The result ZIP contains a special member.",
        )
    if not is_directory and file_type not in {0, stat.S_IFREG}:
        raise ResultArchiveError(
            "unsupported_archive_member_type",
            "The result ZIP contains a special member.",
        )
    return "directory" if is_directory else "file"


def _validated_members(archive: zipfile.ZipFile):
    try:
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ResultArchiveError(
            "invalid_result_archive", "The result ZIP is invalid."
        ) from exc
    if len(members) > MAX_MEMBERS:
        raise ResultArchiveError(
            "archive_member_limit_exceeded", "The result ZIP has too many members."
        )
    records = []
    namespace = {
        "kind": "directory",
        "explicit": True,
        "source_component": None,
        "children": {},
    }
    total_compressed = 0
    total_uncompressed = 0
    total_path_components = 0
    for info in members:
        path, is_directory = _member_path(info)
        kind = _member_kind(info, is_directory=is_directory)
        normalized_parts = [
            unicodedata.normalize(
                "NFC", unicodedata.normalize("NFC", part).casefold()
            )
            for part in path.split("/")
        ]
        canonical_sizes = [len(part.encode("utf-8")) for part in normalized_parts]
        canonical_path_size = sum(canonical_sizes) + len(normalized_parts) - 1
        if canonical_path_size > MAX_MEMBER_PATH_BYTES:
            raise ResultArchiveError(
                "archive_member_path_limit_exceeded",
                "A canonical result ZIP member path exceeds its byte limit.",
            )
        if any(size > MAX_PATH_COMPONENT_BYTES for size in canonical_sizes):
            raise ResultArchiveError(
                "archive_path_component_limit_exceeded",
                "A canonical result ZIP path component exceeds its byte limit.",
            )
        total_path_components += len(normalized_parts)
        if total_path_components > MAX_TOTAL_PATH_COMPONENTS:
            raise ResultArchiveError(
                "archive_path_component_budget_exceeded",
                "The result ZIP exceeds its total path-component budget.",
            )
        node = namespace
        for index, (source_part, part) in enumerate(
            zip(path.split("/"), normalized_parts)
        ):
            final = index == len(normalized_parts) - 1
            child = node["children"].get(part)
            if child is None:
                child = {
                    "kind": kind if final else "directory",
                    "explicit": final,
                    "source_component": source_part,
                    "children": {},
                }
                node["children"][part] = child
            elif child["source_component"] != source_part:
                raise ResultArchiveError(
                    "archive_path_conflict",
                    "The result ZIP contains canonical path aliases.",
                )
            elif not final and child["kind"] == "file":
                raise ResultArchiveError(
                    "archive_path_conflict", "The result ZIP contains path conflicts."
                )
            elif final:
                if child["explicit"] or child["kind"] != kind:
                    raise ResultArchiveError(
                        "archive_path_conflict",
                        "The result ZIP contains conflicting paths.",
                    )
                child["explicit"] = True
            node = child
        total_compressed += info.compress_size
        total_uncompressed += info.file_size
        if info.file_size > MAX_MEMBER_BYTES:
            raise ResultArchiveError(
                "archive_member_size_limit_exceeded",
                "A result ZIP member exceeds its size limit.",
            )
        if total_compressed > MAX_TOTAL_COMPRESSED_BYTES:
            raise ResultArchiveError(
                "archive_compressed_limit_exceeded",
                "The result ZIP exceeds its compressed-size limit.",
            )
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ResultArchiveError(
                "archive_uncompressed_limit_exceeded",
                "The result ZIP exceeds its uncompressed-size limit.",
            )
        records.append((path, kind, info))
    return records, total_compressed, total_uncompressed


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ResultArchiveError(
            "unsafe_extraction_target", "A result extraction directory is unsafe."
        ) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(descriptor)
        raise ResultArchiveError(
            "unsafe_extraction_target", "A result extraction directory is unsafe."
        )
    return descriptor


def _parent_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            child = _open_directory(current, part, create=True)
            _fsync_descriptor(current)
            os.close(current)
            current = child
        return current
    except Exception:
        os.close(current)
        raise


def _write_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    parent_fd: int,
    name: str,
) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        raise ResultArchiveError(
            "unsafe_extraction_target", "A result extraction target is unsafe."
        ) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        os.fchmod(descriptor, 0o600)
        try:
            source = archive.open(info, "r")
        except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
            raise ResultArchiveError(
                "invalid_result_archive", "A result ZIP member could not be read."
            ) from exc
        with source:
            while True:
                try:
                    chunk = source.read(STREAM_CHUNK_BYTES)
                except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                    raise ResultArchiveError(
                        "invalid_result_archive", "A result ZIP member is corrupt."
                    ) from exc
                if not chunk:
                    break
                size += len(chunk)
                if size > info.file_size or size > MAX_MEMBER_BYTES:
                    raise ResultArchiveError(
                        "archive_member_size_limit_exceeded",
                        "A result ZIP member exceeds its declared size.",
                    )
                try:
                    filesystem = os.fstatvfs(descriptor)
                    free_bytes = filesystem.f_bavail * filesystem.f_frsize
                except OSError as exc:
                    raise ResultArchiveError(
                        "result_disk_write_failed",
                        "Extraction disk capacity could not be checked.",
                    ) from exc
                if free_bytes < len(chunk):
                    raise ResultArchiveError(
                        "staging_disk_limit_exceeded",
                        "There is not enough bounded capacity for extraction.",
                    )
                offset = 0
                while offset < len(chunk):
                    try:
                        written = os.write(descriptor, chunk[offset:])
                    except OSError as exc:
                        raise ResultArchiveError(
                            "result_disk_write_failed",
                            "A result ZIP member could not be saved.",
                        ) from exc
                    if written <= 0:
                        raise ResultArchiveError(
                            "result_disk_write_failed",
                            "A result ZIP member could not be saved.",
                        )
                    offset += written
                digest.update(chunk)
            if source.read(1) != b"":
                raise ResultArchiveError(
                    "invalid_result_archive", "A result ZIP member has trailing data."
                )
        if size != info.file_size:
            raise ResultArchiveError(
                "invalid_result_archive", "A result ZIP member is incomplete."
            )
        _fsync_descriptor(descriptor)
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def canonical_tree_hash(records: list[dict]) -> str:
    encoded = json.dumps(
        sorted(records, key=lambda item: item["path"]),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _open_verified_archive(name: str, *, parent_fd: int):
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ResultArchiveError(
            "invalid_result_archive", "The downloaded result ZIP is missing."
        ) from exc
    stream = archive = None
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > MAX_ARCHIVE_BYTES
        ):
            raise ResultArchiveError(
                "invalid_result_archive", "The downloaded result ZIP is unsafe."
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if size > opened.st_size or size > MAX_ARCHIVE_BYTES:
                raise ResultArchiveError(
                    "invalid_result_archive", "The downloaded result ZIP changed."
                )
        if size != opened.st_size:
            raise ResultArchiveError(
                "invalid_result_archive", "The downloaded result ZIP is incomplete."
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(descriptor), "rb")
        archive = zipfile.ZipFile(stream, "r")
        return descriptor, opened, digest.hexdigest(), stream, archive
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if archive is not None:
            archive.close()
        if stream is not None:
            stream.close()
        os.close(descriptor)
        if isinstance(exc, ResultArchiveError):
            raise
        raise ResultArchiveError(
            "invalid_result_archive", "The downloaded result is not a valid ZIP."
        ) from exc
    except Exception:
        if archive is not None:
            archive.close()
        if stream is not None:
            stream.close()
        os.close(descriptor)
        raise


def _assert_archive_unchanged(
    name: str, *, parent_fd: int, descriptor: int, opened
) -> None:
    try:
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ResultArchiveError(
            "integrity_violation", "The result ZIP changed during extraction."
        ) from exc
    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(final, field) != getattr(opened, field) for field in identity) or (
        final.st_dev,
        final.st_ino,
    ) != (current.st_dev, current.st_ino):
        raise ResultArchiveError(
            "integrity_violation", "The result ZIP changed during extraction."
        )


def extract_and_verify(
    attempt_fd: int,
    *,
    request_filename: str,
    archive_name: str = "result.zip",
    raw_name: str = "raw",
) -> PreparedArchive:
    descriptor, archive_info, archive_digest, archive_stream, archive = (
        _open_verified_archive(archive_name, parent_fd=attempt_fd)
    )
    try:
        members, total_compressed, total_uncompressed = _validated_members(archive)
        if archive_info.st_size + total_uncompressed > MAX_STAGING_DISK_BYTES:
            raise ResultArchiveError(
                "staging_disk_limit_exceeded",
                "The result exceeds the staging disk limit.",
        )
        try:
            os.mkdir(raw_name, 0o700, dir_fd=attempt_fd)
        except OSError as exc:
            raise ResultArchiveError(
                "unsafe_extraction_target", "The extraction root could not be created."
            ) from exc
        _fsync_descriptor(attempt_fd)
        root_fd = _open_directory(attempt_fd, raw_name, create=False)
        directory_paths = set()
        for member_path, member_kind, _member_info in members:
            parts = member_path.split("/")
            directory_paths.update(
                "/".join(parts[:index]) for index in range(1, len(parts))
            )
            if member_kind == "directory":
                directory_paths.add(member_path)
        tree_records = [
            {"path": path, "type": "directory"}
            for path in sorted(directory_paths)
        ]
        try:
            for path, kind, info in sorted(members, key=lambda item: item[0]):
                parts = tuple(path.split("/"))
                parent_fd = _parent_directory(root_fd, parts[:-1])
                try:
                    if kind == "directory":
                        directory_fd = _open_directory(
                            parent_fd, parts[-1], create=True
                        )
                        _fsync_descriptor(directory_fd)
                        os.close(directory_fd)
                    else:
                        digest, size = _write_member(
                            archive, info, parent_fd=parent_fd, name=parts[-1]
                        )
                        tree_records.append(
                            {
                                "path": path,
                                "type": "file",
                                "size_bytes": size,
                                "sha256": digest,
                            }
                        )
                finally:
                    _fsync_descriptor(parent_fd)
                    os.close(parent_fd)
            _fsync_descriptor(root_fd)
        finally:
            os.close(root_fd)
        _fsync_descriptor(attempt_fd)
    finally:
        archive.close()
        archive_stream.close()
        try:
            _assert_archive_unchanged(
                archive_name,
                parent_fd=attempt_fd,
                descriptor=descriptor,
                opened=archive_info,
            )
        finally:
            os.close(descriptor)
    markdown_records = [
        item
        for item in tree_records
        if item["type"] == "file" and item["path"].endswith(".md")
    ]
    exact = [
        item
        for item in markdown_records
        if PurePosixPath(item["path"]).name == f"{request_filename}.md"
    ]
    selected = exact[0] if len(exact) == 1 else (
        markdown_records[0] if not exact and len(markdown_records) == 1 else None
    )
    return PreparedArchive(
        archive_sha256=archive_digest,
        archive_size_bytes=archive_info.st_size,
        tree_sha256=canonical_tree_hash(tree_records),
        member_count=len(members),
        total_compressed_bytes=total_compressed,
        total_uncompressed_bytes=total_uncompressed,
        main_markdown_path=None if selected is None else selected["path"],
        main_markdown_sha256=None if selected is None else selected["sha256"],
        reason_code=None if selected is not None else "unexpected_result_layout",
    )


def _discard_partial_archive(attempt_fd: int, name: str) -> None:
    """清掉上一轮留下的半截 result.zip。

    `_stream_archive` 以 `O_EXCL` 创建目标文件且失败时不删除，残留会让下一次
    尝试直接以 `result_disk_write_failed` 失败——重试前必须先删。
    """
    try:
        os.unlink(name, dir_fd=attempt_fd)
    except OSError:
        pass


def download_and_prepare(
    url: str,
    attempt_fd: int,
    *,
    request_filename: str,
    transport=None,
    sleep=None,
) -> PreparedArchive:
    """下载并校验 Doc2X 结果 ZIP。

    下载是幂等 GET（同一结果 URL 反复取同一份内容，不产生远端副作用、不重复
    计费），所以按 ADR 0006 规则 2/3 对瞬时网络故障重试：总尝试 3 次、退避
    1s/2s、每次向 stderr 打一行重试日志；重试全部失败后才把错误交给调用方落成
    `recoverable_error`，由用户拿新 generation 重跑同一条命令。
    确定性错误（结果 URL 已失效、地址不安全、超出体积上限）立即抛出不重试。
    """
    wait = time.sleep if sleep is None else sleep
    downloaded_hash = downloaded_size = None
    for attempt in range(1, NET_MAX_ATTEMPTS + 1):
        try:
            downloaded_hash, downloaded_size = download_archive(
                url,
                "result.zip",
                destination_fd=attempt_fd,
                transport=transport,
            )
            break
        except ResultArchiveError as exc:
            if (
                exc.code not in TRANSIENT_RESULT_ERROR_CODES
                or attempt == NET_MAX_ATTEMPTS
            ):
                raise
            _discard_partial_archive(attempt_fd, "result.zip")
            delay = 2 ** (attempt - 1)
            # 日志走 stderr（stdout 是 workflow 的结构化 JSON）；只打错误码，
            # 结果 URL 是带签名的短期凭证，不进日志。
            sys.stderr.write(
                "[pdf2markdown] result download retry %d/%d after %s; waiting %ds\n"
                % (attempt, NET_MAX_ATTEMPTS, exc.code, delay)
            )
            wait(delay)
    prepared = extract_and_verify(
        attempt_fd, request_filename=request_filename
    )
    if (
        prepared.archive_sha256 != downloaded_hash
        or prepared.archive_size_bytes != downloaded_size
    ):
        raise ResultArchiveError(
            "integrity_violation", "The result ZIP changed after download."
        )
    return prepared
