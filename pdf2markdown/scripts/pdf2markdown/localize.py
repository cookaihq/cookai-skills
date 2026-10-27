"""Localize remote or inline images before freezing direct Markdown."""

import base64
import binascii
import hashlib
import os
import time
import urllib.parse

from . import fsatomic, imageformat
from . import assets

#: 暂存树内的保留子目录名（顶层）。
LOCALIZED_DIR = "_localized"

#: §10.4 第 2 层：瞬时失败的有界退避重试（首次之外 3 次），与结果 ZIP 同节奏。
RETRY_BACKOFF_SECONDS = (2, 5, 10)

#: 单资源与累计上限对齐 §11.3 的资源上限（`core/assets.py`）。
MAX_COUNT = assets.ASSET_MAX_COUNT
MAX_TOTAL_BYTES = assets.ASSET_MAX_TOTAL_BYTES

#: 本地化结论（闭合五值）：
#:   "unchanged"                主稿只引用本地相对目标，一个字节都没动
#:   "localized"                已改写，markdown 为改写后字节
#:   "invalid"                  确定无效（scheme/嗅探/映射/名字冲突/超限）
#:   "environment_unavailable"  本机到不了目标 origin，零付费额度消耗
#:   "unavailable"              免费手段穷尽，本次转换结果确定不可得
UNCHANGED = "unchanged"
LOCALIZED = "localized"
INVALID = "invalid"
ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
UNAVAILABLE = "unavailable"

#: 非本地目标的判据，与 `assets.validate_local_resources` 的拒绝前缀一致。
NON_LOCAL_PREFIXES = ("http://", "https://", "data:", "blob:", "//")


class Outcome(dict):
    """{kind, summary, markdown?, records, members}——kind 闭合五值。"""

    def __init__(self, kind, summary="", markdown=None, records=(), members=()):
        super().__init__(
            kind=kind,
            summary=summary,
            markdown=markdown,
            records=list(records),
            members=list(members),
        )


def _sleep(services):
    return services.get("sleep") or time.sleep


def is_non_local(target):
    return bool(urllib.parse.urlsplit(target).scheme) or target.startswith("//")


def _origin_of(url):
    """https URL 的 origin（scheme://netloc）；解析不出时返回 None。

    痕迹文件里只记 origin，不记完整 URL（§16 日志策略）。
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def _fingerprint(target):
    return hashlib.sha256(target.encode("utf-8", "replace")).hexdigest()


def decode_data_uri(target, max_bytes):
    """`data:` URI → 字节。base64 与 percent 编码两种形态都支持。

    返回 (bytes, None) 或 (None, 失败摘要)。声明的 media type 一律不采信——
    扩展名只由字节嗅探派生（§12.2），这里解码出字节即可。
    """
    if not target.startswith("data:"):
        return None, "not a data uri"
    remainder = target[len("data:"):]
    comma = remainder.find(",")
    if comma < 0:
        return None, "data uri has no payload separator"
    meta = remainder[:comma]
    payload = remainder[comma + 1:]
    is_base64 = any(
        part.strip().lower() == "base64" for part in meta.split(";")[1:]
    )
    try:
        if is_base64:
            # validate=True：base64 段里混入非字母表字符说明这不是可信载荷。
            data = base64.b64decode(payload, validate=True)
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None, "data uri payload is not decodable"
    if len(data) > max_bytes:
        return None, "data uri payload exceeds byte cap"
    return data, None


def download_with_triage(services, url):
    """The injected downloader owns timeout, classification and a three-attempt budget."""
    from .network import DownloadError
    try:
        data = services["asset_fetch"](url, MAX_TOTAL_BYTES)
    except DownloadError as exc:
        kind = "invalid" if exc.category == "oversized" else "unavailable"
        return None, kind, exc.code, [exc.category]
    if not isinstance(data, bytes) or len(data) > MAX_TOTAL_BYTES:
        return None, "invalid", "invalid image response", ["invalid"]
    return data, "ok", "", ["ok"]


def _reserved_name_taken(members):
    """ZIP 成员是否已占用顶层 `_localized` 名字。"""
    for member in members:
        first = member["path"].split("/")[0]
        if first == LOCALIZED_DIR:
            return True
    return False


def localize_markdown(markdown_bytes, staging_dir, members, services):
    """主 Markdown 的外链图片本地化 → Outcome。

    markdown_bytes：ZIP 里选定的主 Markdown 原字节。
    staging_dir：解压暂存树根（目标相对路径以它为基准，与 §11.3 的
        `validate_local_resources(base_dir=staging)` 同一基准）。
    members：`zipsafe.adopt_result_zip` 返回的成员清单（用于保留名冲突检查）。
    services：需要 `pandoc_path`；有非本地目标时还需要 `asset_fetch`，
        可选 `asset_probe`、`sleep`。

    本地相对目标原样放行、一个字节都不动；混合引用因此天然覆盖。
    """
    pandoc = services["pandoc_path"]
    try:
        targets = assets.discover_image_targets(markdown_bytes, pandoc)
    except assets.AssetError as exc:
        return Outcome(INVALID, "image target discovery failed: %s" % exc)
    external = [t for t in targets if is_non_local(t)]
    if not external:
        return Outcome(UNCHANGED, markdown=markdown_bytes)
    if len(external) > MAX_COUNT:
        return Outcome(INVALID, "external image count exceeds cap")
    if _reserved_name_taken(members):
        return Outcome(
            INVALID,
            "result zip already occupies the reserved %s name" % LOCALIZED_DIR,
        )

    records = []
    replacements = {}
    written = {}  # sha256 -> {"path", "byte_count"}
    total = 0
    for target in external:
        record = {
            "classification": None,
            "result": None,
            "failure_category": None,
            "origin": _origin_of(target) if target.startswith("https://") else None,
            "target_sha256": _fingerprint(target),
        }
        records.append(record)
        if target.startswith("https://"):
            record["classification"] = "https"
            data, kind, summary, attempts = download_with_triage(services, target)
            record["attempts"] = attempts
            if kind != "ok":
                record["result"] = "failed"
                record["failure_category"] = kind
                if kind == "environment_unavailable":
                    return Outcome(ENVIRONMENT_UNAVAILABLE, summary, records=records)
                if kind == "invalid":
                    return Outcome(INVALID, summary, records=records)
                return Outcome(UNAVAILABLE, summary, records=records)
        elif target.startswith("data:"):
            record["classification"] = "data_uri"
            data, failure = decode_data_uri(target, MAX_TOTAL_BYTES)
            if data is None:
                record["result"] = "failed"
                record["failure_category"] = "invalid"
                return Outcome(INVALID, failure, records=records)
        else:
            # `http://`、`//`、`blob:` 及其它一切 scheme：确定无效。重新转换
            # 同一源只会让 Doc2X 再写一次同样的目标，付费 successor 是纯烧钱。
            record["classification"] = "scheme_not_allowed"
            record["result"] = "failed"
            record["failure_category"] = "invalid"
            return Outcome(
                INVALID,
                "image target scheme is not localizable",
                records=records,
            )

        media_type = imageformat.sniff_image_media_type(data)
        extension = imageformat.MEDIA_EXTENSIONS.get(media_type)
        if extension is None:
            record["result"] = "failed"
            record["failure_category"] = "invalid"
            return Outcome(
                INVALID,
                "localized image media type outside closed extension map",
                records=records,
            )
        sha = hashlib.sha256(data).hexdigest()
        relative = "%s/%s.%s" % (LOCALIZED_DIR, sha, extension)
        record["result"] = "localized"
        record["sha256"] = sha
        record["byte_count"] = len(data)
        record["media_type"] = media_type
        replacements[target] = relative
        if sha in written:
            continue  # 内容寻址天然去重：同一张图被引用多次只落一份字节
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            record["result"] = "failed"
            record["failure_category"] = "invalid"
            return Outcome(
                INVALID, "localized asset total bytes exceed cap", records=records
            )
        path = os.path.join(staging_dir, LOCALIZED_DIR, "%s.%s" % (sha, extension))
        try:
            fsatomic.ensure_private_dir(os.path.dirname(path), "localized asset dir")
            # 内容寻址落盘天然幂等：同名文件只可能是同一份字节（名字就是这份
            # 字节的 SHA-256）。校验后跳过重写，而不是让 write-once 拒绝——
            # 中断重入不该因为上一次写到一半就把整轮转换判死。
            if os.path.lexists(path) and fsatomic.read_file_sha256(
                path, "localized asset"
            ) == sha:
                written[sha] = {"path": relative, "byte_count": len(data)}
                continue
            fsatomic.atomic_write_bytes(path, data, "localized asset")
        except (fsatomic.UnsafePathError, OSError) as exc:
            record["result"] = "failed"
            record["failure_category"] = "invalid"
            # 摘要只带异常类型名：`UnsafePathError` 的消息里含本地绝对路径，
            # 而摘要会进 Base `转换错误`，那里禁止写入路径（§13.1）。
            return Outcome(
                INVALID,
                "localized asset write refused (%s)" % type(exc).__name__,
                records=records,
            )
        written[sha] = {"path": relative, "byte_count": len(data)}

    try:
        localized = assets.replace_image_targets(markdown_bytes, replacements, pandoc)
    except (UnicodeDecodeError, assets.AssetError) as exc:
        return Outcome(INVALID, "image rewrite refused: %s" % exc, records=records)
    new_members = [
        {"path": entry["path"], "byte_count": entry["byte_count"], "sha256": sha}
        for sha, entry in sorted(written.items())
    ]
    return Outcome(LOCALIZED, markdown=localized, records=records, members=new_members)
