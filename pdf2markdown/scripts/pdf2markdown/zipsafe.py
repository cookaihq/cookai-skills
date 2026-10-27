"""结果 ZIP 安全采用（基线 §10.4）。

硬上限：压缩字节 536,870,912；成员数 65,600；单成员展开 268,435,456；
累计展开 805,306,368；路径 UTF-8 长度 1,024；目录深度 16；单成员与全局
展开/压缩比 ≤200:1。逐成员流式写入内容寻址暂存目录，不使用
extract/extractall；任一失败统一 `conversion_result_invalid` 且不留部分树。
"""

import hashlib
import os
import re
import shutil
import unicodedata
import zipfile

from . import fsatomic

ZIP_MAX_COMPRESSED_BYTES = 536870912
ZIP_MAX_MEMBERS = 65600
MEMBER_MAX_BYTES = 268435456
TOTAL_MAX_BYTES = 805306368
PATH_MAX_UTF8_BYTES = 1024
PATH_MAX_DEPTH = 16
MAX_EXPANSION_RATIO = 200

ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


class ZipAdoptionError(ValueError):
    """统一映射 conversion-core/conversion_result_invalid。"""


def _reject(message):
    raise ZipAdoptionError(message)


def _check_member_path(name, seen_normalized):
    if not isinstance(name, str) or not name:
        _reject("empty or non-text member path")
    name = name[:-1] if name.endswith("/") else name
    raw = name.encode("utf-8", "strict") if isinstance(name, str) else name
    if len(raw) > PATH_MAX_UTF8_BYTES:
        _reject("member path exceeds utf-8 length cap")
    if isinstance(name, bytes):
        _reject("member path is not valid utf-8")
    if "\\" in name:
        _reject("backslash in member path")
    if name.startswith("/") or name.startswith("~"):
        _reject("absolute member path")
    parts = name.split("/")
    if len(parts) > PATH_MAX_DEPTH:
        _reject("member path exceeds depth cap")
    for part in parts:
        if part in ("", ".", ".."):
            _reject("path traversal or empty segment in member path")
        if (any(ord(c) < 32 or c in '<>:"|?*' for c in part) or part.endswith((".", " "))
                or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)):
            _reject("member path is not portable to Windows")
    normalized = unicodedata.normalize("NFC", name).casefold()
    if normalized in seen_normalized:
        _reject("unicode/case normalization conflict between member paths")
    seen_normalized.add(normalized)
    return name


def adopt_result_zip(zip_path, staging_dir):
    """安全解压到受控内容寻址暂存目录。

    返回 {members: [{path, byte_count, sha256}], total_bytes}；任一失败抛
    ZipAdoptionError，且暂存目录被整体移除（零部分树）。
    """
    st = fsatomic.check_safe_regular_file(zip_path, "result zip")
    if st.st_size > ZIP_MAX_COMPRESSED_BYTES:
        _reject("zip exceeds compressed byte cap")
    fsatomic.ensure_private_dir(staging_dir, "zip staging dir")
    try:
        return _extract(zip_path, staging_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _extract(zip_path, staging_dir):
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        _reject("not a valid zip archive")
    with archive:
        infos = archive.infolist()
        if len(infos) > ZIP_MAX_MEMBERS:
            _reject("zip exceeds member count cap")
        declared_total = 0
        seen_normalized = set()
        members = []
        for info in infos:
            if info.flag_bits & 0x1:
                _reject("encrypted member")
            if info.compress_type not in ALLOWED_COMPRESSION:
                _reject("unknown compression algorithm")
            # 外部属性中的 symlink/设备位（Unix 高 16 位 st_mode）
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = mode & 0o170000
            if file_type in (0o120000, 0o020000, 0o060000, 0o010000, 0o140000):
                _reject("special-file member (symlink/device/fifo/socket)")
            declared = info.file_size
            compressed = info.compress_size
            if declared < 0 or compressed < 0:
                _reject("negative size (integer overflow)")
            if declared > MEMBER_MAX_BYTES:
                _reject("member declared size exceeds cap")
            declared_total += declared
            if declared_total > TOTAL_MAX_BYTES:
                _reject("declared total expansion exceeds cap")
            if compressed == 0 and declared > 0:
                _reject("zero compressed bytes with non-zero declared expansion")
            if compressed > 0 and declared > compressed * MAX_EXPANSION_RATIO:
                _reject("member expansion ratio exceeds 200:1")
            _check_member_path(info.filename, seen_normalized)
        # A file cannot also be a directory prefix, including case/Unicode aliases.
        file_paths = {unicodedata.normalize("NFC", i.filename).casefold()
                      for i in infos if not i.is_dir()}
        for info in infos:
            parts = unicodedata.normalize("NFC", info.filename.rstrip("/")).casefold().split("/")
            if any("/".join(parts[:index]) in file_paths for index in range(1, len(parts))):
                _reject("file/directory prefix conflict")
        total_compressed = sum(i.compress_size for i in infos)
        if total_compressed > 0 and declared_total > total_compressed * MAX_EXPANSION_RATIO:
            _reject("global expansion ratio exceeds 200:1")

        actual_total = 0
        for info in infos:
            if not info.filename or info.filename.endswith("/"):
                continue
            target = os.path.join(staging_dir, *info.filename.split("/"))
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, mode=0o700)
            digest = hashlib.sha256()
            written = 0
            fd = fsatomic.open_regular(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, "zip member")
            try:
                with archive.open(info) as src:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MEMBER_MAX_BYTES:
                            _reject("member actual bytes exceed cap")
                        if written > info.file_size:
                            _reject("member actual bytes exceed declared size")
                        actual_total += len(chunk)
                        if actual_total > TOTAL_MAX_BYTES:
                            _reject("actual total expansion exceeds cap")
                        digest.update(chunk)
                        fsatomic.write_all(fd, chunk)
                os.fsync(fd)
            finally:
                os.close(fd)
            if written != info.file_size:
                _reject("member header/actual byte mismatch")
            members.append(
                {
                    "path": info.filename,
                    "byte_count": written,
                    "sha256": digest.hexdigest(),
                }
            )
        return {"members": members, "total_bytes": actual_total}


def select_primary_markdown(members):
    """主 Markdown 必须唯一匹配；布局不唯一统一 conversion_result_invalid。

    候选：扩展名 .md 的成员；恰好一个时采用。多个时，若恰好一个位于根目录
    仍视为布局不唯一（保守：不猜测），返回 detail code。
    """
    candidates = [m for m in members if m["path"].lower().endswith(".md")]
    if len(candidates) == 1:
        return candidates[0]
    raise ZipAdoptionError(
        "primary markdown not uniquely matched (unexpected_result_layout)"
    )
