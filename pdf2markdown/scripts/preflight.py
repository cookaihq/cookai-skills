from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import stat
import struct
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import bundle
import markdown_structure


SCHEMA_VERSION = 1
DEFAULT_RENDER_DPI = 300
MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 600
MAX_PAGE_COUNT = 2_000
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PAGE_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 250_000_000
MAX_INVENTORY_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
PREFLIGHT_RECORD_ASCII_EXPANSION_FACTOR = 6
PREFLIGHT_RESULT_METADATA_BYTES_PER_PAGE = 256
PREFLIGHT_RESULT_FIXED_OVERHEAD_BYTES = 64 * 1024
MAX_PREFLIGHT_RESULT_BYTES = (
    MAX_RECORD_BYTES * PREFLIGHT_RECORD_ASCII_EXPANSION_FACTOR
    + MAX_PAGE_COUNT * PREFLIGHT_RESULT_METADATA_BYTES_PER_PAGE
    + PREFLIGHT_RESULT_FIXED_OVERHEAD_BYTES
)
PNG_CONTAINER_OVERHEAD_BYTES = 1024 * 1024
PAGE_CLASSIFICATIONS = frozenset({"content", "blank", "risk", "unreadable"})
RISK_CODES = frozenset(
    {
        "scanned_content",
        "handwritten_content",
        "low_resolution",
        "blurred_content",
        "abnormal_rotation",
        "small_text",
        "complex_multicolumn",
        "cross_page_table",
        "layout_form",
        "mixed_text_images",
        "partial_blank",
        "structure_extraction_incomplete",
        "unreadable_content",
        "other",
    }
)
RESOURCE_LIMITS = {
    "max_source_bytes": MAX_SOURCE_BYTES,
    "max_page_count": MAX_PAGE_COUNT,
    "max_page_pixels": MAX_PAGE_PIXELS,
    "max_total_pixels": MAX_TOTAL_PIXELS,
    "max_inventory_bytes": MAX_INVENTORY_BYTES,
    "max_preflight_result_bytes": MAX_PREFLIGHT_RESULT_BYTES,
    "preflight_record_ascii_expansion_factor": (
        PREFLIGHT_RECORD_ASCII_EXPANSION_FACTOR
    ),
    "preflight_result_metadata_bytes_per_page": (
        PREFLIGHT_RESULT_METADATA_BYTES_PER_PAGE
    ),
    "preflight_result_fixed_overhead_bytes": PREFLIGHT_RESULT_FIXED_OVERHEAD_BYTES,
    "png_container_overhead_bytes": PNG_CONTAINER_OVERHEAD_BYTES,
}

class PreflightError(ValueError):
    def __init__(self, code: str, message: str, *, context=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = {} if context is None else context


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def object_hash(value: dict) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_file_bytes(name: str, *, dir_fd: int, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise PreflightError("integrity_violation", "A preflight read limit is invalid.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PreflightError("integrity_violation", "A preflight input is missing or unsafe.") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > max_bytes
        ):
            raise PreflightError(
                "integrity_violation",
                "A preflight input is not a bounded private regular file.",
            )
        chunks = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, max_bytes + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise PreflightError(
                    "integrity_violation", "A preflight input exceeds its read limit."
                )
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise PreflightError(
                "integrity_violation", "A preflight input changed while it was read."
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_private_file(name: str, data: bytes, *, dir_fd: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise PreflightError(
            "integrity_violation", "A preflight staging file already exists or is unsafe."
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(dir_fd)


def load_record_input(path, *, cwd) -> dict:
    candidate = path if path.is_absolute() else cwd / path
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(candidate, follow_symlinks=False)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise PreflightError("invalid_preflight_record", "The preflight record cannot be read.") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_RECORD_BYTES
        ):
            raise PreflightError(
                "invalid_preflight_record", "The preflight record must be a bounded regular file."
            )
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_RECORD_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RECORD_BYTES:
                raise PreflightError(
                    "invalid_preflight_record", "The preflight record exceeds its size limit."
                )
        final = os.fstat(descriptor)
        current = os.stat(candidate, follow_symlinks=False)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise PreflightError(
                "invalid_preflight_record", "The preflight record changed while it was read."
            )
        try:
            return bundle.decode_json_object(b"".join(chunks))
        except bundle.BundleStateError as exc:
            raise PreflightError(
                "invalid_preflight_record", "The preflight record is not strict JSON."
            ) from exc
    finally:
        os.close(descriptor)


def _promote_private_file(
    temporary_name: str,
    final_name: str,
    *,
    dir_fd: int,
    expected_sha256: str,
    max_bytes: int,
) -> None:
    try:
        os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PreflightError("integrity_violation", "A preflight output is unsafe.") from exc
    else:
        raise PreflightError(
            "integrity_violation", "A preflight output would overwrite an existing artifact."
        )
    data = _private_file_bytes(
        temporary_name, dir_fd=dir_fd, max_bytes=max_bytes
    )
    if bytes_hash(data) != expected_sha256:
        raise PreflightError(
            "integrity_violation", "A preflight staging artifact does not match its journal."
        )
    os.rename(
        temporary_name,
        final_name,
        src_dir_fd=dir_fd,
        dst_dir_fd=dir_fd,
    )
    os.fsync(dir_fd)


def _artifact_exists(name: str, *, dir_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PreflightError("integrity_violation", "A pending artifact is unsafe.") from exc


def _recover_private_artifact(
    temporary_name: str,
    final_name: str,
    *,
    dir_fd: int,
    expected_sha256: str,
    max_bytes: int,
) -> None:
    temporary_exists = _artifact_exists(temporary_name, dir_fd=dir_fd)
    final_exists = _artifact_exists(final_name, dir_fd=dir_fd)
    if temporary_exists and final_exists:
        raise PreflightError(
            "integrity_violation", "A pending artifact exists under both temporary and final names."
        )
    if not temporary_exists and not final_exists:
        raise PreflightError(
            "integrity_violation", "A pending artifact is missing from both journaled names."
        )
    if temporary_exists:
        _promote_private_file(
            temporary_name,
            final_name,
            dir_fd=dir_fd,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
        return
    if (
        bytes_hash(
            _private_file_bytes(final_name, dir_fd=dir_fd, max_bytes=max_bytes)
        )
        != expected_sha256
    ):
        raise PreflightError(
            "integrity_violation", "A promoted artifact does not match its prepared hash."
        )


def _module_version(module, *attributes: str) -> str:
    for attribute in attributes:
        value = getattr(module, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _pymupdf_api_available(fitz) -> bool:
    tools = getattr(fitz, "TOOLS", None)
    module_api = all(
        callable(value)
        for value in (
            getattr(fitz, "open", None),
            getattr(fitz, "Matrix", None),
            getattr(tools, "mupdf_display_errors", None),
            getattr(tools, "mupdf_display_warnings", None),
            getattr(tools, "reset_mupdf_warnings", None),
            getattr(tools, "mupdf_warnings", None),
        )
    ) and all(
        getattr(fitz, name, None) is not None
        for name in ("csRGB", "PDF_TX_FIELD_IS_PASSWORD")
    )
    document_type = getattr(fitz, "Document", None)
    page_type = getattr(fitz, "Page", None)
    pixmap_type = getattr(fitz, "Pixmap", None)
    object_api = all(
        callable(getattr(owner, name, None))
        for owner, names in (
            (document_type, ("load_page",)),
            (
                page_type,
                (
                    "get_pixmap",
                    "get_text",
                    "get_links",
                    "widgets",
                    "annots",
                    "get_images",
                    "get_image_info",
                    "get_drawings",
                ),
            ),
            (pixmap_type, ("tobytes",)),
        )
        for name in names
    )
    return module_api and object_api


def _beautifulsoup_api_available(bs4) -> bool:
    factory = getattr(bs4, "BeautifulSoup", None)
    if not callable(factory):
        return False
    try:
        document = factory("<p>preflight</p>", "html.parser")
        paragraph = document.find("p")
        return paragraph is not None and paragraph.get_text() == "preflight"
    except Exception:
        return False


def _page_image_byte_limit(pixels: int) -> int:
    return pixels * 4 + PNG_CONTAINER_OVERHEAD_BYTES


def _silence_mupdf(fitz) -> tuple[bool, bool]:
    previous = (
        bool(fitz.TOOLS.mupdf_display_errors()),
        bool(fitz.TOOLS.mupdf_display_warnings()),
    )
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    fitz.TOOLS.reset_mupdf_warnings()
    return previous


def _restore_mupdf(fitz, previous: tuple[bool, bool]) -> None:
    fitz.TOOLS.reset_mupdf_warnings()
    fitz.TOOLS.mupdf_display_errors(previous[0])
    fitz.TOOLS.mupdf_display_warnings(previous[1])


def _take_mupdf_warnings(fitz) -> list[str]:
    warnings = fitz.TOOLS.mupdf_warnings()
    return [line.strip() for line in warnings.splitlines() if line.strip()]


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if (
        len(data) < 24
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
    ):
        raise PreflightError("integrity_violation", "A page reference is not a PNG image.")
    return struct.unpack(">II", data[16:24])


def check_dependencies(*, environ: dict[str, str], visual_capability: str) -> dict:
    dependencies = []
    missing = []

    try:
        fitz = importlib.import_module("fitz")
    except (ImportError, ModuleNotFoundError):
        fitz = None
    fitz_version = (
        None if fitz is None else _module_version(fitz, "VersionBind", "__version__")
    )
    fitz_available = fitz is not None and _pymupdf_api_available(fitz)
    if not fitz_available:
        missing.append("pymupdf")
        dependencies.append(
            {
                "name": "pymupdf",
                "available": False,
                "version": fitz_version,
                "reason": "not_installed" if fitz is None else "incompatible_api",
                "purpose": "PDF structure extraction and page rendering",
            }
        )
    else:
        dependencies.append(
            {
                "name": "pymupdf",
                "available": True,
                "version": fitz_version,
                "reason": None,
                "purpose": "PDF structure extraction and page rendering",
            }
        )

    pandoc_path = shutil.which("pandoc", path=environ.get("PATH", os.defpath))
    pandoc_version = None
    pandoc_identity = None
    pandoc_available = False
    pandoc_reason = "not_installed" if pandoc_path is None else "incompatible_api"
    if pandoc_path is not None:
        try:
            inspection = markdown_structure.inspect_pandoc(
                pandoc_path, environ=environ
            )
        except markdown_structure.MarkdownStructureError:
            pandoc_available = False
        else:
            pandoc_path = inspection["executable"]
            pandoc_version = inspection["version"]
            pandoc_identity = inspection["executable_identity"]
            pandoc_available = True
            pandoc_reason = None
    if not pandoc_available:
        missing.append("pandoc")
    dependencies.append(
        {
            "name": "pandoc",
            "available": pandoc_available,
            "version": pandoc_version,
            "executable": pandoc_path,
            "executable_identity": pandoc_identity,
            "reason": pandoc_reason,
            "purpose": "GFM parsing and normalization",
        }
    )

    try:
        bs4 = importlib.import_module("bs4")
    except (ImportError, ModuleNotFoundError):
        bs4 = None
    bs4_available = bs4 is not None and _beautifulsoup_api_available(bs4)
    if not bs4_available:
        missing.append("beautifulsoup4")
    dependencies.append(
        {
            "name": "beautifulsoup4",
            "available": bs4_available,
            "version": None if bs4 is None else _module_version(bs4, "__version__"),
            "reason": (
                None
                if bs4_available
                else "not_installed"
                if bs4 is None
                else "incompatible_api"
            ),
            "purpose": "Structured HTML parsing for the versioned allowlist",
        }
    )

    visual_available = visual_capability == "available"
    if not visual_available:
        missing.append("host_visual")
    dependencies.append(
        {
            "name": "host_visual",
            "available": visual_available,
            "version": None,
            "reason": None if visual_available else "not_declared_available",
            "purpose": "Agent inspection of every page reference image",
        }
    )
    return {
        "dependencies": dependencies,
        "missing": missing,
        "fitz": fitz if fitz_available else None,
    }


def _rect(value) -> list[float]:
    coordinates = (
        (value.x0, value.y0, value.x1, value.y1)
        if all(hasattr(value, name) for name in ("x0", "y0", "x1", "y1"))
        else value[:4]
    )
    return [round(float(item), 4) for item in coordinates]


def _link_kind(fitz, link: dict) -> str:
    kinds = {
        getattr(fitz, "LINK_URI", object()): "external_uri",
        getattr(fitz, "LINK_GOTO", object()): "internal_jump",
        getattr(fitz, "LINK_GOTOR", object()): "remote_document",
        getattr(fitz, "LINK_LAUNCH", object()): "launch_action",
        getattr(fitz, "LINK_NAMED", object()): "named_action",
    }
    return kinds.get(link.get("kind"), "unknown_action")


def _links(fitz, page) -> list[dict]:
    values = []
    for link in page.get_links():
        value = {
            "kind": _link_kind(fitz, link),
            "from": _rect(link["from"]),
        }
        if value["kind"] == "external_uri" and isinstance(link.get("uri"), str):
            full_uri = link["uri"]
            try:
                parsed = urlsplit(full_uri)
                hostname = parsed.hostname or ""
                rendered_host = f"[{hostname}]" if ":" in hostname else hostname
                rendered_port = f":{parsed.port}" if parsed.port is not None else ""
                redaction_required = bool(
                    parsed.query
                    or parsed.fragment
                    or parsed.username is not None
                    or parsed.password is not None
                )
                value["uri"] = (
                    urlunsplit(
                        (
                            parsed.scheme,
                            rendered_host + rendered_port,
                            parsed.path,
                            "",
                            "",
                        )
                    )
                    if redaction_required
                    else full_uri
                )
                if redaction_required:
                    value["full_uri_sha256"] = hashlib.sha256(
                        full_uri.encode("utf-8")
                    ).hexdigest()
                    value["query_redacted"] = bool(parsed.query)
                    value["fragment_redacted"] = bool(parsed.fragment)
            except (TypeError, ValueError):
                value["uri"] = None
                value["full_uri_sha256"] = hashlib.sha256(
                    full_uri.encode("utf-8")
                ).hexdigest()
                value["unsafe_action_present"] = True
        elif value["kind"] == "internal_jump":
            value["target_page"] = link.get("page") + 1 if type(link.get("page")) is int else None
            target = link.get("to")
            value["target_point"] = (
                [round(float(target.x), 4), round(float(target.y), 4)]
                if target is not None
                else None
            )
        else:
            value["unsafe_action_present"] = value["kind"] in {
                "remote_document",
                "launch_action",
                "named_action",
                "unknown_action",
            }
        values.append(value)
    return values


def _forms(fitz, page) -> list[dict]:
    widgets = page.widgets()
    if widgets is None:
        return []
    values = []
    for widget in widgets:
        password = bool(widget.field_flags & fitz.PDF_TX_FIELD_IS_PASSWORD)
        value = {
            "field_name": widget.field_name,
            "field_type": widget.field_type_string,
            "field_value": None if password else widget.field_value,
            "rect": _rect(widget.rect),
        }
        if password:
            value["value_present"] = widget.field_value not in {None, ""}
            value["value_redacted"] = True
        values.append(value)
    return values


def _annotations(page) -> list[dict]:
    annotations = page.annots()
    if annotations is None:
        return []
    values = []
    for annotation in annotations:
        info = annotation.info if isinstance(annotation.info, dict) else {}
        annotation_type = annotation.type
        values.append(
            {
                "type": annotation_type[1] if isinstance(annotation_type, tuple) else str(annotation_type),
                "rect": _rect(annotation.rect),
                "content": info.get("content") if isinstance(info.get("content"), str) else None,
                "title": info.get("title") if isinstance(info.get("title"), str) else None,
                "subject": info.get("subject") if isinstance(info.get("subject"), str) else None,
            }
        )
    return values


def _text_blocks(page, *, sensitive_rects: list[list[float]]) -> list[dict]:
    values = []
    for block in page.get_text("blocks", sort=True):
        bbox = [round(float(item), 4) for item in block[:4]]
        sensitive = any(
            bbox[0] < rect[2]
            and bbox[2] > rect[0]
            and bbox[1] < rect[3]
            and bbox[3] > rect[1]
            for rect in sensitive_rects
        )
        value = {
            "bbox": bbox,
            "text": None if sensitive else block[4],
            "block_number": block[5],
            "block_type": block[6],
        }
        if sensitive:
            value["text_redacted"] = True
            value["redaction_reason"] = "password_widget"
        values.append(value)
    return values


def _image_descriptors(page) -> list[dict]:
    values = []
    for image in page.get_image_info(hashes=True, xrefs=True):
        digest = image.get("digest")
        values.append(
            {
                "xref": image.get("xref"),
                "width": image.get("width"),
                "height": image.get("height"),
                "bits_per_component": image.get("bpc"),
                "colorspace": image.get("colorspace"),
                "bbox": _rect(image["bbox"]) if image.get("bbox") is not None else None,
                "digest": digest.hex() if isinstance(digest, bytes) else None,
            }
        )
    return values


def _load_page(document, index: int):
    try:
        return document.load_page(index)
    except Exception as exc:
        raise PreflightError(
            "render_failed",
            f"Page {index + 1} could not be loaded for complete rendering.",
            context={"pages": [index + 1]},
        ) from exc


def _page_plan(document, *, dpi: int) -> list[dict]:
    fitz = importlib.import_module("fitz")
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    if document.page_count > MAX_PAGE_COUNT:
        raise PreflightError(
            "page_limit_exceeded",
            "The source PDF exceeds the page limit.",
            context={"observed_pages": document.page_count, "limit_pages": MAX_PAGE_COUNT},
        )
    width = max(4, len(str(document.page_count)))
    pages = []
    total_pixels = 0
    for index in range(document.page_count):
        page = _load_page(document, index)
        dimensions = (
            float(page.rect.x0),
            float(page.rect.y0),
            float(page.rect.x1),
            float(page.rect.y1),
            float(page.rect.width),
            float(page.rect.height),
        )
        if (
            not all(math.isfinite(value) for value in dimensions)
            or page.rect.width <= 0
            or page.rect.height <= 0
        ):
            raise PreflightError(
                "invalid_page_geometry",
                "A source page has invalid geometry.",
                context={"pages": [index + 1]},
            )
        pixel_rect = (page.rect * matrix).irect
        pixel_width = pixel_rect.width
        pixel_height = pixel_rect.height
        if pixel_width <= 0 or pixel_height <= 0:
            raise PreflightError(
                "invalid_page_geometry",
                "A source page has invalid rendered dimensions.",
                context={"pages": [index + 1]},
            )
        pixels = pixel_width * pixel_height
        total_pixels += pixels
        pages.append(
            {
                "page_number": index + 1,
                "width_points": round(float(page.rect.width), 4),
                "height_points": round(float(page.rect.height), 4),
                "rotation": page.rotation,
                "pixel_width": pixel_width,
                "pixel_height": pixel_height,
                "pixels": pixels,
                "final_name": f"page-{index + 1:0{width}d}.png",
            }
        )
    oversized = [page for page in pages if page["pixels"] > MAX_PAGE_PIXELS]
    if oversized:
        raise PreflightError(
            "page_pixel_limit_exceeded",
            "A source page exceeds the pixel limit.",
            context={
                "pages": [page["page_number"] for page in oversized],
                "observed_pixels": max(page["pixels"] for page in oversized),
                "limit_pixels": MAX_PAGE_PIXELS,
            },
        )
    if total_pixels > MAX_TOTAL_PIXELS:
        raise PreflightError(
            "total_pixel_limit_exceeded",
            "The source PDF exceeds the total pixel limit.",
            context={
                "pages": [page["page_number"] for page in pages],
                "observed_total_pixels": total_pixels,
                "limit_total_pixels": MAX_TOTAL_PIXELS,
            },
        )
    return pages


def _validate_page_plan(plan: list[dict], *, page_count: int) -> None:
    expected_numbers = list(range(1, page_count + 1))
    observed_numbers = [
        page.get("page_number") if isinstance(page, dict) else None for page in plan
    ]
    if len(plan) != page_count:
        missing = [number for number in expected_numbers if number not in observed_numbers]
        raise PreflightError(
            "page_reference_count_mismatch",
            "The page reference plan does not cover every source page.",
            context={
                "pages": missing,
                "expected_pages": page_count,
                "observed_pages": len(plan),
            },
        )
    width = max(4, len(str(page_count)))
    mismatched_pages = [
        expected
        for expected, observed, page in zip(expected_numbers, observed_numbers, plan)
        if observed != expected
        or page.get("final_name") != f"page-{expected:0{width}d}.png"
    ]
    if mismatched_pages:
        raise PreflightError(
            "page_reference_numbering_mismatch",
            "The page reference plan is not continuously numbered.",
            context={
                "pages": mismatched_pages,
                "observed_page_numbers": observed_numbers,
            },
        )


def _inventory_size_after_page(
    current_size: int, page_count: int, page: dict
) -> int:
    page_size = len(_json_bytes(page)) - 1
    return current_size + page_size + (1 if page_count else 0)


def _ensure_empty_baseline_targets(*, source_fd: int, pages_fd: int) -> None:
    try:
        os.stat("source-inventory.json", dir_fd=source_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise PreflightError(
            "integrity_violation", "An uncommitted source inventory already exists."
        )
    unexpected = [name for name in os.listdir(pages_fd) if not name.startswith(".")]
    if unexpected:
        raise PreflightError(
            "integrity_violation", "Uncommitted page reference images already exist."
        )


def build_baseline(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    dependencies: list[dict],
    fitz,
    render_dpi: int,
    at: str,
    pending_intent: dict | None = None,
) -> dict:
    if not MIN_RENDER_DPI <= render_dpi <= MAX_RENDER_DPI:
        raise PreflightError("invalid_render_dpi", "Render DPI is outside the supported range.")
    _ensure_empty_baseline_targets(
        source_fd=descriptors["source"], pages_fd=descriptors["pages"]
    )
    if manifest["source"]["size_bytes"] > MAX_SOURCE_BYTES:
        raise PreflightError(
            "source_size_limit_exceeded",
            "The saved source PDF exceeds the preflight snapshot limit.",
            context={
                "observed_bytes": manifest["source"]["size_bytes"],
                "limit_bytes": MAX_SOURCE_BYTES,
                "pages": [],
            },
        )
    source_bytes = _private_file_bytes(
        "source.pdf",
        dir_fd=descriptors["source"],
        max_bytes=MAX_SOURCE_BYTES,
    )
    if bytes_hash(source_bytes) != manifest["source"]["sha256"]:
        raise PreflightError("integrity_violation", "The source PDF hash changed before preflight.")
    mupdf_display = _silence_mupdf(fitz)
    try:
        document = fitz.open(stream=source_bytes, filetype="pdf")
        open_warnings = _take_mupdf_warnings(fitz)
    except Exception as exc:
        _restore_mupdf(fitz, mupdf_display)
        raise PreflightError("unreadable_input", "The saved source PDF cannot be fully opened.") from exc
    try:
        if document.is_repaired:
            raise PreflightError(
                "damaged_pdf",
                "The source PDF required structural repair before preflight.",
                context={
                    "mupdf_repaired": True,
                    "mupdf_warnings": open_warnings,
                },
            )
        if document.needs_pass:
            raise PreflightError(
                "password_required", "The source PDF requires a password for complete preflight."
            )
        if document.page_count == 0:
            raise PreflightError("zero_pages", "The source PDF has no pages.")
        plan = _page_plan(document, dpi=render_dpi)
        _validate_page_plan(plan, page_count=document.page_count)
        total_pixels = sum(page["pixels"] for page in plan)
        free_bytes = os.fstatvfs(descriptors["pages"]).f_bavail * os.fstatvfs(
            descriptors["pages"]
        ).f_frsize
        if free_bytes < total_pixels * 3:
            raise PreflightError(
                "render_disk_limit_exceeded", "There is not enough disk capacity for preflight."
            )

        if pending_intent is None:
            operation_id = f"preflight-baseline-{uuid.uuid4().hex}"
            action_id = f"preflight-{uuid.uuid4().hex}"
            for page in plan:
                page["temporary_name"] = f".{operation_id}-{page['final_name']}.part"
            inventory_temporary_name = f".{operation_id}-source-inventory.json.part"
            intent = {
                "schema_version": SCHEMA_VERSION,
                "event": "preflight_baseline_intent",
                "operation_id": operation_id,
                "expected_generation": manifest["generation"],
                "new_generation": manifest["generation"] + 1,
                "at": at,
                "source_sha256": manifest["source"]["sha256"],
                "source_size_bytes": manifest["source"]["size_bytes"],
                "render_dpi": render_dpi,
                "resource_limits": RESOURCE_LIMITS,
                "dependencies": dependencies,
                "inventory_temporary_name": inventory_temporary_name,
                "pages": plan,
                "action_id": action_id,
                "previous_manifest": manifest,
                "previous_manifest_hash": object_hash(manifest),
                "previous_private_hash": object_hash(private_state),
            }
            bundle.append_history(intent, state_fd=descriptors["state"])
        else:
            intent = pending_intent
            operation_id = intent["operation_id"]
            action_id = intent["action_id"]
            inventory_temporary_name = intent["inventory_temporary_name"]
            if (
                not _valid_baseline_intent(intent)
                or intent.get("previous_manifest") != manifest
                or intent.get("previous_private_hash") != object_hash(private_state)
                or intent.get("render_dpi") != render_dpi
                or intent.get("dependencies") != dependencies
                or intent.get("resource_limits") != RESOURCE_LIMITS
            ):
                raise PreflightError(
                    "integrity_violation", "A pending baseline intent cannot be reproduced."
                )
            recorded_plan = intent.get("pages")
            if not isinstance(recorded_plan, list) or len(recorded_plan) != len(plan):
                raise PreflightError(
                    "integrity_violation", "A pending baseline page plan changed."
                )
            for actual, recorded in zip(plan, recorded_plan):
                comparison = dict(recorded)
                temporary_name = comparison.pop("temporary_name", None)
                if actual != comparison or not isinstance(temporary_name, str):
                    raise PreflightError(
                        "integrity_violation", "A pending baseline page plan changed."
                    )
            plan = recorded_plan
            for name, directory_fd, max_bytes in [
                (
                    inventory_temporary_name,
                    descriptors["source"],
                    MAX_INVENTORY_BYTES,
                ),
                *[
                    (
                        page["temporary_name"],
                        descriptors["pages"],
                        _page_image_byte_limit(page["pixels"]),
                    )
                    for page in plan
                ],
            ]:
                if _artifact_exists(name, dir_fd=directory_fd):
                    _private_file_bytes(
                        name, dir_fd=directory_fd, max_bytes=max_bytes
                    )
                    os.unlink(name, dir_fd=directory_fd)
                    os.fsync(directory_fd)

        inventory = {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": manifest["source"]["sha256"],
            "page_count": document.page_count,
            "document_security": {
                "encryption_metadata_present": bool(
                    isinstance(document.metadata, dict)
                    and document.metadata.get("encryption")
                ),
                "password_required": bool(document.needs_pass),
            },
            "render": {"dpi": render_dpi, "format": "png", "lossless": True},
            "resource_limits": RESOURCE_LIMITS,
            "dependencies": dependencies,
            "pages": [],
        }
        inventory_size = len(_json_bytes(inventory))
        if inventory_size > MAX_INVENTORY_BYTES:
            raise PreflightError(
                "inventory_limit_exceeded",
                "The source structure inventory exceeds its size limit.",
                context={
                    "pages": [],
                    "observed_bytes": inventory_size,
                    "limit_bytes": MAX_INVENTORY_BYTES,
                },
            )
        inventory_pages = []
        page_references = []
        for page_plan in plan:
            fitz.TOOLS.reset_mupdf_warnings()
            page = _load_page(document, page_plan["page_number"] - 1)
            extraction_issues = []
            try:
                forms = _forms(fitz, page)
            except Exception:
                forms = []
                extraction_issues.append("form_extraction_failed")
            try:
                text_blocks = _text_blocks(
                    page,
                    sensitive_rects=[
                        form["rect"]
                        for form in forms
                        if form.get("value_redacted") is True
                    ],
                )
            except Exception:
                text_blocks = []
                extraction_issues.append("text_extraction_failed")
            try:
                links = _links(fitz, page)
            except Exception:
                links = []
                extraction_issues.append("link_extraction_failed")
            try:
                annotations = _annotations(page)
            except Exception:
                annotations = []
                extraction_issues.append("annotation_extraction_failed")
            try:
                images = _image_descriptors(page)
            except Exception:
                images = []
                extraction_issues.append("image_inventory_failed")
            try:
                drawing_count = len(page.get_drawings())
            except Exception:
                drawing_count = None
                extraction_issues.append("drawing_inventory_failed")
            inventory_page = {
                "page_number": page_plan["page_number"],
                "width_points": page_plan["width_points"],
                "height_points": page_plan["height_points"],
                "rotation": page_plan["rotation"],
                "pixel_width": page_plan["pixel_width"],
                "pixel_height": page_plan["pixel_height"],
                "pixels": page_plan["pixels"],
                "page_reference": f"02-pages/{page_plan['final_name']}",
                "image_sha256": "0" * 64,
                "text_blocks": text_blocks,
                "links": links,
                "forms": forms,
                "annotations": annotations,
                "images": images,
                "drawing_count": drawing_count,
                "extraction_issues": extraction_issues,
                "mupdf_warnings": [],
            }
            projected_size = _inventory_size_after_page(
                inventory_size, len(inventory_pages), inventory_page
            )
            if projected_size > MAX_INVENTORY_BYTES:
                raise PreflightError(
                    "inventory_limit_exceeded",
                    "The source structure inventory exceeds its size limit.",
                    context={
                        "pages": [page_plan["page_number"]],
                        "observed_bytes": projected_size,
                        "limit_bytes": MAX_INVENTORY_BYTES,
                    },
                )
            try:
                pixmap = page.get_pixmap(
                    dpi=render_dpi,
                    colorspace=fitz.csRGB,
                    alpha=False,
                    annots=True,
                )
                png = pixmap.tobytes("png")
            except Exception as exc:
                raise PreflightError(
                    "render_failed",
                    f"Page {page_plan['page_number']} could not be completely rendered.",
                    context={"pages": [page_plan["page_number"]]},
                ) from exc
            if (pixmap.width, pixmap.height) != (
                page_plan["pixel_width"],
                page_plan["pixel_height"],
            ):
                raise PreflightError(
                    "render_dimension_mismatch",
                    "A rendered page does not match its planned dimensions.",
                    context={"pages": [page_plan["page_number"]]},
                )
            page_warnings = _take_mupdf_warnings(fitz)
            if any(
                marker in warning.lower()
                for warning in page_warnings
                for marker in (
                    "page may not be correct",
                    "syntax error",
                    "format error",
                    "cannot parse",
                )
            ):
                raise PreflightError(
                    "render_failed",
                    f"Page {page_plan['page_number']} produced MuPDF completeness warnings.",
                    context={
                        "pages": [page_plan["page_number"]],
                        "mupdf_warnings": page_warnings,
                    },
                )
            if page_warnings:
                extraction_issues.append("mupdf_warning")
            image_sha256 = bytes_hash(png)
            _write_private_file(
                page_plan["temporary_name"], png, dir_fd=descriptors["pages"]
            )
            page_reference = {
                "page_number": page_plan["page_number"],
                "path": f"02-pages/{page_plan['final_name']}",
                "pixel_width": pixmap.width,
                "pixel_height": pixmap.height,
                "pixels": pixmap.width * pixmap.height,
                "image_sha256": image_sha256,
            }
            page_references.append(page_reference)
            inventory_page.update(
                {
                    "pixel_width": pixmap.width,
                    "pixel_height": pixmap.height,
                    "pixels": pixmap.width * pixmap.height,
                    "page_reference": page_reference["path"],
                    "image_sha256": image_sha256,
                    "mupdf_warnings": page_warnings,
                }
            )
            projected_size = _inventory_size_after_page(
                inventory_size, len(inventory_pages), inventory_page
            )
            if projected_size > MAX_INVENTORY_BYTES:
                raise PreflightError(
                    "inventory_limit_exceeded",
                    "The source structure inventory exceeds its size limit.",
                    context={
                        "pages": [page_plan["page_number"]],
                        "observed_bytes": projected_size,
                        "limit_bytes": MAX_INVENTORY_BYTES,
                    },
                )
            inventory_pages.append(inventory_page)
            inventory_size = projected_size

        missing_pages = [
            page["page_number"]
            for page in plan
            if not _artifact_exists(
                page["temporary_name"], dir_fd=descriptors["pages"]
            )
        ]
        if len(page_references) != document.page_count or missing_pages:
            raise PreflightError(
                "page_reference_count_mismatch",
                "The rendered page references do not cover every source page.",
                context={
                    "pages": missing_pages,
                    "expected_pages": document.page_count,
                    "observed_pages": document.page_count - len(missing_pages),
                },
            )
        for page_plan, page_reference in zip(plan, page_references):
            staged_png = _private_file_bytes(
                page_plan["temporary_name"],
                dir_fd=descriptors["pages"],
                max_bytes=_page_image_byte_limit(page_plan["pixels"]),
            )
            if (
                bytes_hash(staged_png) != page_reference["image_sha256"]
                or _png_dimensions(staged_png)
                != (page_plan["pixel_width"], page_plan["pixel_height"])
            ):
                raise PreflightError(
                    "render_failed",
                    "A staged page reference is incomplete.",
                    context={"pages": [page_plan["page_number"]]},
                )

        inventory["pages"] = inventory_pages
        inventory_bytes = _json_bytes(inventory)
        if len(inventory_bytes) != inventory_size or len(inventory_bytes) > (
            MAX_INVENTORY_BYTES
        ):
            raise PreflightError(
                "inventory_limit_exceeded",
                "The source structure inventory exceeds its size limit.",
                context={
                    "pages": [page["page_number"] for page in plan],
                    "observed_bytes": len(inventory_bytes),
                    "limit_bytes": MAX_INVENTORY_BYTES,
                },
            )
        _write_private_file(
            inventory_temporary_name, inventory_bytes, dir_fd=descriptors["source"]
        )
        inventory_sha256 = bytes_hash(inventory_bytes)
        evidence = {
            "source_sha256": manifest["source"]["sha256"],
            "render_dpi": render_dpi,
            "dependencies": dependencies,
            "inventory_sha256": inventory_sha256,
            "pages": page_references,
        }
        evidence_hash = object_hash(evidence)
        updated_manifest = dict(manifest)
        updated_manifest["generation"] = manifest["generation"] + 1
        updated_manifest["conversion_state"] = "preflight_pending"
        updated_manifest["artifacts"] = {
            "source_pdf": "01-source/source.pdf",
            "source_inventory": "01-source/source-inventory.json",
            "page_references": [item["path"] for item in page_references],
        }
        updated_manifest["preflight"] = {
            "render_dpi": render_dpi,
            "page_count": document.page_count,
            "dependencies": dependencies,
            "inventory_sha256": inventory_sha256,
            "page_references": page_references,
            "evidence_hash": evidence_hash,
            "pending_action": {
                "kind": "record_preflight",
                "action_id": action_id,
                "generation": updated_manifest["generation"],
                "evidence_hash": evidence_hash,
            },
            "result": None,
            "decision": None,
        }
        updated_private = dict(private_state)
        updated_private["generation"] = updated_manifest["generation"]
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_baseline_prepared",
            "operation_id": operation_id,
            "expected_generation": manifest["generation"],
            "new_generation": updated_manifest["generation"],
            "at": at,
            "inventory_sha256": inventory_sha256,
            "page_references": page_references,
            "pages_tree_hash": object_hash({"pages": page_references}),
            "total_pixels": total_pixels,
            "evidence_hash": evidence_hash,
            "action_id": action_id,
            "desired_manifest": updated_manifest,
            "desired_manifest_hash": object_hash(updated_manifest),
            "desired_private_hash": object_hash(updated_private),
        }
        bundle.append_history(prepared, state_fd=descriptors["state"])
        _promote_private_file(
            inventory_temporary_name,
            "source-inventory.json",
            dir_fd=descriptors["source"],
            expected_sha256=inventory_sha256,
            max_bytes=MAX_INVENTORY_BYTES,
        )
        for page_plan, page_reference in zip(plan, page_references):
            _promote_private_file(
                page_plan["temporary_name"],
                page_plan["final_name"],
                dir_fd=descriptors["pages"],
                expected_sha256=page_reference["image_sha256"],
                max_bytes=_page_image_byte_limit(page_plan["pixels"]),
            )
        bundle.atomic_write_json(
            "private.json", updated_private, dir_fd=descriptors["state"]
        )
        bundle.atomic_write_json(
            "manifest.json", updated_manifest, dir_fd=descriptors["root"]
        )
        bundle.append_history(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "preflight_baseline_committed",
                "operation_id": operation_id,
                "previous_generation": manifest["generation"],
                "generation": updated_manifest["generation"],
                "at": at,
                "manifest_hash": object_hash(updated_manifest),
                "private_hash": object_hash(updated_private),
            },
            state_fd=descriptors["state"],
        )
        return updated_manifest
    finally:
        document.close()
        _restore_mupdf(fitz, mupdf_display)


def commit_dependency_missing(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    dependencies: list[dict],
    missing: list[str],
    render_dpi: int,
    at: str,
) -> dict:
    operation_id = f"preflight-dependency-{uuid.uuid4().hex}"
    new_generation = manifest["generation"] + 1
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_dependency_intent",
        "operation_id": operation_id,
        "expected_generation": manifest["generation"],
        "new_generation": new_generation,
        "at": at,
        "source_sha256": manifest["source"]["sha256"],
        "render_dpi": render_dpi,
        "dependencies": dependencies,
        "missing": missing,
        "resume_state": "preparing",
        "previous_manifest": manifest,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    updated_manifest = dict(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = "recoverable_error"
    updated_manifest["preflight"] = {
        "status": "dependency_missing",
        "reason_code": "dependency_missing",
        "resume_state": "preparing",
        "render_dpi": render_dpi,
        "dependencies": dependencies,
        "missing": missing,
        "pending_action": None,
        "result": None,
        "decision": None,
    }
    updated_private = dict(private_state)
    updated_private["generation"] = new_generation
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_dependency_prepared",
        "operation_id": operation_id,
        "expected_generation": manifest["generation"],
        "new_generation": new_generation,
        "at": at,
        "desired_manifest": updated_manifest,
        "desired_manifest_hash": object_hash(updated_manifest),
        "desired_private_hash": object_hash(updated_private),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_dependency_committed",
            "operation_id": operation_id,
            "previous_generation": manifest["generation"],
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def _normalized_preflight_payload(payload: dict, *, inventory: dict) -> dict:
    if set(payload) != {"schema_version", "summary", "pages"}:
        raise PreflightError(
            "invalid_preflight_record", "The preflight record uses an unknown schema."
        )
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise PreflightError(
            "invalid_preflight_record", "The preflight record schema version is unsupported."
        )
    if payload.get("summary") not in {"pass", "warning", "blocked"}:
        raise PreflightError(
            "invalid_preflight_record", "The preflight summary is invalid."
        )
    pages = payload.get("pages")
    if not isinstance(pages, list) or len(pages) != inventory.get("page_count"):
        raise PreflightError(
            "invalid_preflight_record", "The preflight record must cover every source page."
        )
    normalized_pages = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or set(page) != {
            "page_number",
            "classification",
            "risk_codes",
            "evidence",
        }:
            raise PreflightError(
                "invalid_preflight_record", "A page preflight conclusion uses an unknown schema."
            )
        classification = page.get("classification")
        risk_codes = page.get("risk_codes")
        evidence = page.get("evidence")
        if (
            type(page.get("page_number")) is not int
            or page["page_number"] != index
            or classification not in PAGE_CLASSIFICATIONS
            or not isinstance(risk_codes, list)
            or len(risk_codes) != len(set(risk_codes))
            or not all(isinstance(code, str) and code in RISK_CODES for code in risk_codes)
            or not isinstance(evidence, list)
            or not evidence
            or not all(
                isinstance(item, str) and item.strip() and len(item) <= 2_000
                for item in evidence
            )
        ):
            raise PreflightError(
                "invalid_preflight_record", "A page preflight conclusion is incomplete or invalid."
            )
        if classification == "content" and risk_codes:
            raise PreflightError(
                "invalid_preflight_record", "A content conclusion cannot include risk codes."
            )
        if classification == "risk" and not risk_codes:
            raise PreflightError(
                "invalid_preflight_record", "A risk conclusion must include a stable risk code."
            )
        inventory_page = inventory["pages"][index - 1]
        extraction_issues = inventory_page.get("extraction_issues")
        if extraction_issues and "structure_extraction_incomplete" not in risk_codes:
            raise PreflightError(
                "invalid_preflight_record",
                "Incomplete structure extraction must be represented as a page risk.",
            )
        normalized_pages.append(
            {
                "page_number": index,
                "classification": classification,
                "risk_codes": sorted(risk_codes),
                "evidence": [item.strip() for item in evidence],
                "page_reference": inventory_page.get("page_reference"),
                "image_sha256": inventory_page.get("image_sha256"),
                "pixel_width": inventory_page.get("pixel_width"),
                "pixel_height": inventory_page.get("pixel_height"),
            }
        )
    classifications = [page["classification"] for page in normalized_pages]
    if "unreadable" in classifications or all(value == "blank" for value in classifications):
        derived = "blocked"
    elif any(value in {"blank", "risk"} for value in classifications):
        derived = "warning"
    else:
        derived = "pass"
    if payload["summary"] != derived:
        raise PreflightError(
            "invalid_preflight_record", "The preflight summary does not match the page conclusions."
        )
    return {"schema_version": 1, "summary": derived, "pages": normalized_pages}


def recovered_request_matches(
    *,
    descriptors: dict,
    intent: dict,
    record_command: str,
    action_id: str,
    evidence_hash: str,
    payload: dict | None = None,
    decision: str | None = None,
    basis: str | None = None,
) -> bool:
    if intent.get("action_id") != action_id or intent.get("evidence_hash") != evidence_hash:
        return False
    if record_command == "preflight":
        if intent.get("event") != "preflight_record_intent" or not isinstance(payload, dict):
            return False
        try:
            inventory = bundle.decode_json_object(
                _private_file_bytes(
                    "source-inventory.json",
                    dir_fd=descriptors["source"],
                    max_bytes=MAX_INVENTORY_BYTES,
                )
            )
            normalized = _normalized_preflight_payload(payload, inventory=inventory)
        except (bundle.BundleStateError, PreflightError):
            return False
        recorded = intent.get("payload")
        return (
            isinstance(recorded, dict)
            and recorded.get("result") == normalized["summary"]
            and recorded.get("pages") == normalized["pages"]
        )
    return (
        record_command == "decision"
        and intent.get("event") == "preflight_decision_intent"
        and intent.get("decision") == decision
        and isinstance(basis, str)
        and intent.get("basis") == basis.strip()
    )


def commit_preflight_record(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    payload: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    at: str,
) -> dict:
    pending_action = manifest.get("preflight", {}).get("pending_action")
    if manifest.get("conversion_state") != "preflight_pending" or not isinstance(
        pending_action, dict
    ):
        code = (
            "action_already_consumed"
            if manifest.get("preflight", {}).get("result") is not None
            else "preflight_action_mismatch"
        )
        raise PreflightError(
            code, "The preflight action is no longer pending."
        )
    if (
        manifest.get("generation") != expected_generation
        or pending_action.get("kind") != "record_preflight"
        or pending_action.get("generation") != expected_generation
        or pending_action.get("action_id") != action_id
    ):
        raise PreflightError(
            "preflight_action_mismatch",
            "The preflight action is stale or has a different identity.",
        )
    if pending_action.get("evidence_hash") != evidence_hash:
        raise PreflightError(
            "evidence_hash_mismatch",
            "The preflight action is bound to different baseline evidence.",
        )
    validate_baseline_artifacts(descriptors=descriptors, manifest=manifest)
    inventory_bytes = _private_file_bytes(
        "source-inventory.json",
        dir_fd=descriptors["source"],
        max_bytes=MAX_INVENTORY_BYTES,
    )
    try:
        inventory = bundle.decode_json_object(inventory_bytes)
    except bundle.BundleStateError as exc:
        raise PreflightError("integrity_violation", "The source inventory is invalid.") from exc
    normalized = _normalized_preflight_payload(payload, inventory=inventory)
    record = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": manifest["source"]["sha256"],
        "baseline_generation": expected_generation,
        "baseline_evidence_hash": evidence_hash,
        "recorded_at": at,
        "result": normalized["summary"],
        "pages": normalized["pages"],
    }
    record_bytes = _json_bytes(record)
    if len(record_bytes) > MAX_PREFLIGHT_RESULT_BYTES:
        raise PreflightError(
            "preflight_result_limit_exceeded",
            "The derived preflight result exceeds its size limit.",
            context={
                "observed_bytes": len(record_bytes),
                "limit_bytes": MAX_PREFLIGHT_RESULT_BYTES,
            },
        )
    operation_id = f"preflight-record-{uuid.uuid4().hex}"
    temporary_name = f".{operation_id}-preflight.json.part"
    record_sha256 = bytes_hash(record_bytes)
    new_generation = expected_generation + 1
    interaction_mode = manifest["settings_snapshot"]["interaction_mode"]
    next_action_id = (
        f"preflight-decision-{uuid.uuid4().hex}"
        if normalized["summary"] == "warning" and interaction_mode == "confirm"
        else None
    )
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_record_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "source_sha256": manifest["source"]["sha256"],
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "payload_temporary_name": temporary_name,
        "payload_sha256": record_sha256,
        "payload": record,
        "next_action_id": next_action_id,
        "previous_manifest": manifest,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    _write_private_file(temporary_name, record_bytes, dir_fd=descriptors["review"])
    updated_manifest = dict(manifest)
    updated_manifest["generation"] = new_generation
    if normalized["summary"] == "pass":
        updated_manifest["conversion_state"] = "ready_to_submit"
    elif normalized["summary"] == "warning" and interaction_mode == "confirm":
        updated_manifest["conversion_state"] = "preflight_warning"
    elif normalized["summary"] == "warning":
        updated_manifest["conversion_state"] = "ready_to_submit"
    else:
        updated_manifest["conversion_state"] = "preflight_blocked"
    updated_manifest["artifacts"] = {
        **manifest["artifacts"],
        "preflight": "04-review/preflight.json",
    }
    updated_preflight = dict(manifest["preflight"])
    updated_preflight["pending_action"] = (
        None
        if next_action_id is None
        else {
            "kind": "record_preflight_decision",
            "action_id": next_action_id,
            "generation": new_generation,
            "evidence_hash": f"sha256:{record_sha256}",
        }
    )
    updated_preflight["result"] = {
        "status": normalized["summary"],
        "path": "04-review/preflight.json",
        "sha256": record_sha256,
    }
    if normalized["summary"] == "pass":
        updated_preflight["reason_code"] = None
        updated_preflight["decision"] = {
            "status": "not_required",
            "source": "preflight_pass",
        }
    elif normalized["summary"] == "warning" and interaction_mode == "confirm":
        updated_preflight["reason_code"] = "risk_detected"
        updated_preflight["decision"] = {"status": "pending", "source": None}
    elif normalized["summary"] == "warning":
        updated_preflight["reason_code"] = None
        updated_preflight["decision"] = {
            "status": "accepted",
            "source": "interaction_mode_auto",
            "at": at,
            "evidence_hash": f"sha256:{record_sha256}",
        }
    else:
        updated_preflight["reason_code"] = "unreadable_input"
        updated_preflight["decision"] = {
            "status": "not_applicable",
            "source": "preflight_blocked",
        }
    updated_manifest["preflight"] = updated_preflight
    updated_private = dict(private_state)
    updated_private["generation"] = new_generation
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_record_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "preflight_sha256": record_sha256,
        "preflight_path": "04-review/preflight.json",
        "desired_manifest": updated_manifest,
        "desired_manifest_hash": object_hash(updated_manifest),
        "desired_private_hash": object_hash(updated_private),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    _promote_private_file(
        temporary_name,
        "preflight.json",
        dir_fd=descriptors["review"],
        expected_sha256=record_sha256,
        max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
    )
    bundle.atomic_write_json("private.json", updated_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", updated_manifest, dir_fd=descriptors["root"])
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_record_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def commit_preflight_decision(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    decision: str,
    basis: str,
    at: str,
) -> dict:
    pending_action = manifest.get("preflight", {}).get("pending_action")
    if manifest.get("conversion_state") != "preflight_warning" or not isinstance(
        pending_action, dict
    ):
        raise PreflightError(
            "action_already_consumed", "The preflight decision is no longer pending."
        )
    if (
        manifest.get("generation") != expected_generation
        or pending_action.get("kind") != "record_preflight_decision"
        or pending_action.get("generation") != expected_generation
        or pending_action.get("action_id") != action_id
    ):
        raise PreflightError(
            "preflight_action_mismatch",
            "The preflight decision is stale or has a different identity.",
        )
    if pending_action.get("evidence_hash") != evidence_hash:
        raise PreflightError(
            "evidence_hash_mismatch",
            "The preflight decision is bound to different evidence.",
        )
    if decision not in {"accept", "decline"} or not isinstance(basis, str) or not basis.strip() or len(basis) > 2_000:
        raise PreflightError(
            "invalid_preflight_decision", "The preflight decision and basis are invalid."
        )
    validate_baseline_artifacts(descriptors=descriptors, manifest=manifest)
    operation_id = f"preflight-decision-{uuid.uuid4().hex}"
    new_generation = expected_generation + 1
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_decision_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "source_sha256": manifest["source"]["sha256"],
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "decision": decision,
        "basis": basis.strip(),
        "previous_manifest": manifest,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    updated_manifest = dict(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = (
        "ready_to_submit" if decision == "accept" else "terminal_error"
    )
    updated_preflight = dict(manifest["preflight"])
    updated_preflight["pending_action"] = None
    updated_preflight["reason_code"] = (
        None if decision == "accept" else "preflight_declined"
    )
    updated_preflight["decision"] = {
        "status": "accepted" if decision == "accept" else "declined",
        "source": "user_confirmation",
        "basis": basis.strip(),
        "at": at,
        "evidence_hash": evidence_hash,
    }
    updated_manifest["preflight"] = updated_preflight
    updated_private = dict(private_state)
    updated_private["generation"] = new_generation
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_decision_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "desired_manifest": updated_manifest,
        "desired_manifest_hash": object_hash(updated_manifest),
        "desired_private_hash": object_hash(updated_private),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    bundle.atomic_write_json("private.json", updated_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", updated_manifest, dir_fd=descriptors["root"])
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_decision_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def blocker_for_error(error: PreflightError) -> dict | None:
    blockers = {
        "zero_pages": {
            "code": "zero_pages",
            "pages": [],
            "evidence": "The PDF parser reported zero source pages.",
        },
        "password_required": {
            "code": "password_required",
            "pages": [],
            "evidence": "The PDF requires a password before all pages can be read and rendered.",
        },
        "unreadable_input": {
            "code": "damaged_pdf",
            "pages": [],
            "evidence": "The saved PDF could not be fully opened for preflight.",
        },
        "damaged_pdf": {
            "code": "damaged_pdf",
            "pages": [],
            "evidence": "MuPDF reported that the saved PDF required structural repair.",
        },
        "page_limit_exceeded": {
            "code": "page_limit_exceeded",
            "pages": [],
            "evidence": f"The PDF exceeds the {MAX_PAGE_COUNT} page hard limit.",
        },
        "source_size_limit_exceeded": {
            "code": "source_size_limit_exceeded",
            "pages": [],
            "evidence": f"The source PDF exceeds the {MAX_SOURCE_BYTES} byte preflight limit.",
        },
        "page_pixel_limit_exceeded": {
            "code": "page_pixel_limit_exceeded",
            "pages": [],
            "evidence": f"At least one page exceeds the {MAX_PAGE_PIXELS} pixel hard limit.",
        },
        "total_pixel_limit_exceeded": {
            "code": "total_pixel_limit_exceeded",
            "pages": [],
            "evidence": f"The PDF exceeds the {MAX_TOTAL_PIXELS} total pixel hard limit.",
        },
        "render_disk_limit_exceeded": {
            "code": "render_disk_limit_exceeded",
            "pages": [],
            "evidence": "Available disk capacity is below the complete-render budget.",
        },
        "invalid_page_geometry": {
            "code": "invalid_page_geometry",
            "pages": [],
            "evidence": "At least one PDF page has invalid physical or rendered geometry.",
        },
        "render_failed": {
            "code": "render_failed",
            "pages": [],
            "evidence": "At least one complete page reference image could not be rendered.",
        },
        "render_dimension_mismatch": {
            "code": "render_dimension_mismatch",
            "pages": [],
            "evidence": "At least one rendered page did not match its planned full-page dimensions.",
        },
        "page_reference_count_mismatch": {
            "code": "page_reference_count_mismatch",
            "pages": [],
            "evidence": "The generated page references did not cover every source page.",
        },
        "page_reference_numbering_mismatch": {
            "code": "page_reference_numbering_mismatch",
            "pages": [],
            "evidence": "The generated page references were not continuously numbered.",
        },
        "inventory_limit_exceeded": {
            "code": "inventory_limit_exceeded",
            "pages": [],
            "evidence": "The complete source structure inventory exceeded its hard byte limit.",
        },
    }
    blocker = blockers.get(error.code)
    if blocker is None:
        return None
    return {**blocker, **error.context}


def abort_pending_baseline(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    blocker: dict,
    at: str,
    reason_code: str = "deterministic_block",
) -> None:
    history = bundle.read_history(state_fd=descriptors["state"])
    intent = history[-1] if history else None
    if (
        not isinstance(intent, dict)
        or intent.get("event") != "preflight_baseline_intent"
        or not _valid_baseline_intent(intent)
        or intent.get("previous_manifest") != manifest
        or intent.get("previous_manifest_hash") != object_hash(manifest)
        or intent.get("previous_private_hash") != object_hash(private_state)
        or not _valid_committed_prefix(history[:-1], manifest, private_state)
    ):
        raise PreflightError(
            "integrity_violation", "The failed baseline intent cannot be aborted safely."
        )
    if _artifact_exists("source-inventory.json", dir_fd=descriptors["source"]) or any(
        _artifact_exists(page["final_name"], dir_fd=descriptors["pages"])
        for page in intent["pages"]
    ):
        raise PreflightError(
            "integrity_violation", "A failed baseline promoted artifacts before prepared."
        )
    for name, directory_fd, max_bytes in [
        (
            intent["inventory_temporary_name"],
            descriptors["source"],
            MAX_INVENTORY_BYTES,
        ),
        *[
            (
                page["temporary_name"],
                descriptors["pages"],
                _page_image_byte_limit(page["pixels"]),
            )
            for page in intent["pages"]
        ],
    ]:
        if _artifact_exists(name, dir_fd=directory_fd):
            _private_file_bytes(
                name, dir_fd=directory_fd, max_bytes=max_bytes
            )
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_baseline_aborted",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "at": at,
            "reason_code": reason_code,
            "blocker": blocker,
        },
        state_fd=descriptors["state"],
    )


def commit_deterministic_block(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    dependencies: list[dict],
    render_dpi: int,
    blocker: dict,
    at: str,
) -> dict:
    operation_id = f"preflight-block-{uuid.uuid4().hex}"
    temporary_name = f".{operation_id}-preflight.json.part"
    record = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": manifest["source"]["sha256"],
        "baseline_generation": manifest["generation"],
        "baseline_evidence_hash": object_hash(
            {
                "source_sha256": manifest["source"]["sha256"],
                "render_dpi": render_dpi,
                "dependencies": dependencies,
                "deterministic_blockers": [blocker],
            }
        ),
        "recorded_at": at,
        "result": "blocked",
        "pages": [],
        "deterministic_blockers": [blocker],
    }
    record_bytes = _json_bytes(record)
    record_sha256 = bytes_hash(record_bytes)
    new_generation = manifest["generation"] + 1
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_block_intent",
        "operation_id": operation_id,
        "expected_generation": manifest["generation"],
        "new_generation": new_generation,
        "at": at,
        "source_sha256": manifest["source"]["sha256"],
        "render_dpi": render_dpi,
        "dependencies": dependencies,
        "blocker": blocker,
        "payload_temporary_name": temporary_name,
        "payload_sha256": record_sha256,
        "payload": record,
        "previous_manifest": manifest,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    _write_private_file(temporary_name, record_bytes, dir_fd=descriptors["review"])
    updated_manifest = dict(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = "preflight_blocked"
    updated_manifest["artifacts"] = {
        **manifest["artifacts"],
        "preflight": "04-review/preflight.json",
    }
    updated_manifest["preflight"] = {
        "status": "deterministic_blocked",
        "reason_code": "unreadable_input",
        "resume_state": None,
        "render_dpi": render_dpi,
        "dependencies": dependencies,
        "missing": [],
        "pending_action": None,
        "result": {
            "status": "blocked",
            "path": "04-review/preflight.json",
            "sha256": record_sha256,
        },
        "decision": {
            "status": "not_applicable",
            "source": "deterministic_block",
        },
        "deterministic_blockers": [blocker],
    }
    updated_private = dict(private_state)
    updated_private["generation"] = new_generation
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "preflight_block_prepared",
        "operation_id": operation_id,
        "expected_generation": manifest["generation"],
        "new_generation": new_generation,
        "at": at,
        "preflight_sha256": record_sha256,
        "preflight_path": "04-review/preflight.json",
        "desired_manifest": updated_manifest,
        "desired_manifest_hash": object_hash(updated_manifest),
        "desired_private_hash": object_hash(updated_private),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    _promote_private_file(
        temporary_name,
        "preflight.json",
        dir_fd=descriptors["review"],
        expected_sha256=record_sha256,
        max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
    )
    bundle.atomic_write_json("private.json", updated_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", updated_manifest, dir_fd=descriptors["root"])
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_block_committed",
            "operation_id": operation_id,
            "previous_generation": manifest["generation"],
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def _timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_baseline_page_plan(intent: dict) -> bool:
    operation_id = intent.get("operation_id")
    pages = intent.get("pages")
    if not isinstance(operation_id, str) or not isinstance(pages, list) or not pages:
        return False
    width = max(4, len(str(len(pages))))
    for page_number, page in enumerate(pages, start=1):
        final_name = f"page-{page_number:0{width}d}.png"
        if (
            not isinstance(page, dict)
            or set(page)
            != {
                "page_number",
                "width_points",
                "height_points",
                "rotation",
                "pixel_width",
                "pixel_height",
                "pixels",
                "final_name",
                "temporary_name",
            }
            or type(page.get("page_number")) is not int
            or page["page_number"] != page_number
            or not isinstance(page.get("width_points"), (int, float))
            or not isinstance(page.get("height_points"), (int, float))
            or page["width_points"] <= 0
            or page["height_points"] <= 0
            or type(page.get("rotation")) is not int
            or page["rotation"] not in {0, 90, 180, 270}
            or type(page.get("pixel_width")) is not int
            or type(page.get("pixel_height")) is not int
            or page["pixel_width"] <= 0
            or page["pixel_height"] <= 0
            or type(page.get("pixels")) is not int
            or page["pixels"] != page["pixel_width"] * page["pixel_height"]
            or page.get("final_name") != final_name
            or page.get("temporary_name")
            != f".{operation_id}-{final_name}.part"
        ):
            return False
    return True


def _valid_baseline_intent(intent: dict) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "source_size_bytes",
            "render_dpi",
            "resource_limits",
            "dependencies",
            "inventory_temporary_name",
            "pages",
            "action_id",
            "previous_manifest",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent.get("schema_version") == SCHEMA_VERSION
        and intent.get("event") == "preflight_baseline_intent"
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent["expected_generation"] + 1
        and _timestamp(intent.get("at"))
        and isinstance(intent.get("operation_id"), str)
        and re.fullmatch(r"preflight-baseline-[0-9a-f]{32}", intent["operation_id"])
        is not None
        and isinstance(intent.get("action_id"), str)
        and re.fullmatch(r"preflight-[0-9a-f]{32}", intent["action_id"]) is not None
        and isinstance(intent.get("source_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", intent["source_sha256"]) is not None
        and type(intent.get("source_size_bytes")) is int
        and intent["source_size_bytes"] >= 0
        and type(intent.get("render_dpi")) is int
        and MIN_RENDER_DPI <= intent["render_dpi"] <= MAX_RENDER_DPI
        and intent.get("resource_limits") == RESOURCE_LIMITS
        and isinstance(intent.get("dependencies"), list)
        and intent.get("inventory_temporary_name")
        == f".{intent['operation_id']}-source-inventory.json.part"
        and _valid_baseline_page_plan(intent)
    )


def _valid_dependency_intent(intent: dict) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "render_dpi",
            "dependencies",
            "missing",
            "resume_state",
            "previous_manifest",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent["schema_version"] == SCHEMA_VERSION
        and intent.get("event") == "preflight_dependency_intent"
        and isinstance(intent.get("operation_id"), str)
        and re.fullmatch(r"preflight-dependency-[0-9a-f]{32}", intent["operation_id"])
        is not None
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent["expected_generation"] + 1
        and _timestamp(intent.get("at"))
        and intent.get("resume_state") == "preparing"
        and isinstance(intent.get("dependencies"), list)
        and isinstance(intent.get("missing"), list)
        and bool(intent["missing"])
    )


def _valid_record_intent(intent: dict) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "action_id",
            "evidence_hash",
            "payload_temporary_name",
            "payload_sha256",
            "payload",
            "next_action_id",
            "previous_manifest",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent["schema_version"] == SCHEMA_VERSION
        and intent.get("event") == "preflight_record_intent"
        and isinstance(intent.get("operation_id"), str)
        and re.fullmatch(r"preflight-record-[0-9a-f]{32}", intent["operation_id"])
        is not None
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent["expected_generation"] + 1
        and _timestamp(intent.get("at"))
        and isinstance(intent.get("action_id"), str)
        and re.fullmatch(r"preflight-[0-9a-f]{32}", intent["action_id"]) is not None
        and isinstance(intent.get("evidence_hash"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", intent["evidence_hash"]) is not None
        and isinstance(intent.get("payload_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", intent["payload_sha256"]) is not None
        and isinstance(intent.get("payload"), dict)
        and bytes_hash(_json_bytes(intent["payload"])) == intent["payload_sha256"]
        and intent.get("payload_temporary_name")
        == f".{intent['operation_id']}-preflight.json.part"
        and (
            intent.get("next_action_id") is None
            or (
                isinstance(intent["next_action_id"], str)
                and re.fullmatch(
                    r"preflight-decision-[0-9a-f]{32}", intent["next_action_id"]
                )
                is not None
            )
        )
    )


def _valid_decision_intent(intent: dict) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "action_id",
            "evidence_hash",
            "decision",
            "basis",
            "previous_manifest",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent["schema_version"] == SCHEMA_VERSION
        and intent.get("event") == "preflight_decision_intent"
        and isinstance(intent.get("operation_id"), str)
        and re.fullmatch(r"preflight-decision-[0-9a-f]{32}", intent["operation_id"])
        is not None
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent["expected_generation"] + 1
        and _timestamp(intent.get("at"))
        and isinstance(intent.get("action_id"), str)
        and re.fullmatch(r"preflight-decision-[0-9a-f]{32}", intent["action_id"])
        is not None
        and isinstance(intent.get("evidence_hash"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", intent["evidence_hash"]) is not None
        and intent.get("decision") in {"accept", "decline"}
        and isinstance(intent.get("basis"), str)
        and bool(intent["basis"])
    )


def _valid_block_intent(intent: dict) -> bool:
    return (
        set(intent)
        == {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "render_dpi",
            "dependencies",
            "blocker",
            "payload_temporary_name",
            "payload_sha256",
            "payload",
            "previous_manifest",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        and type(intent.get("schema_version")) is int
        and intent["schema_version"] == SCHEMA_VERSION
        and intent.get("event") == "preflight_block_intent"
        and isinstance(intent.get("operation_id"), str)
        and re.fullmatch(r"preflight-block-[0-9a-f]{32}", intent["operation_id"])
        is not None
        and type(intent.get("expected_generation")) is int
        and intent.get("new_generation") == intent["expected_generation"] + 1
        and _timestamp(intent.get("at"))
        and isinstance(intent.get("dependencies"), list)
        and isinstance(intent.get("blocker"), dict)
        and isinstance(intent.get("payload_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", intent["payload_sha256"]) is not None
        and isinstance(intent.get("payload"), dict)
        and bytes_hash(_json_bytes(intent["payload"])) == intent["payload_sha256"]
        and intent.get("payload_temporary_name")
        == f".{intent['operation_id']}-preflight.json.part"
    )


def _valid_prepared_semantics(intent: dict, prepared: dict) -> bool:
    previous = intent.get("previous_manifest")
    desired = prepared.get("desired_manifest")
    if not isinstance(previous, dict) or not isinstance(desired, dict):
        return False
    if desired.get("generation") != intent.get("new_generation"):
        return False
    for field in (
        "schema_version",
        "source",
        "settings_snapshot",
        "conversion_attempts",
        "final_markdown",
        "publication_state",
    ):
        if desired.get(field) != previous.get(field):
            return False

    event = intent.get("event")
    if event == "preflight_baseline_intent":
        references = prepared.get("page_references")
        if not isinstance(references, list):
            return False
        evidence = {
            "source_sha256": intent.get("source_sha256"),
            "render_dpi": intent.get("render_dpi"),
            "dependencies": intent.get("dependencies"),
            "inventory_sha256": prepared.get("inventory_sha256"),
            "pages": references,
        }
        evidence_hash = object_hash(evidence)
        expected_preflight = {
            "render_dpi": intent.get("render_dpi"),
            "page_count": len(intent.get("pages", [])),
            "dependencies": intent.get("dependencies"),
            "inventory_sha256": prepared.get("inventory_sha256"),
            "page_references": references,
            "evidence_hash": evidence_hash,
            "pending_action": {
                "kind": "record_preflight",
                "action_id": intent.get("action_id"),
                "generation": intent.get("new_generation"),
                "evidence_hash": evidence_hash,
            },
            "result": None,
            "decision": None,
        }
        expected_artifacts = {
            "source_pdf": "01-source/source.pdf",
            "source_inventory": "01-source/source-inventory.json",
            "page_references": [item.get("path") for item in references],
        }
        return (
            desired.get("conversion_state") == "preflight_pending"
            and desired.get("preflight") == expected_preflight
            and desired.get("artifacts") == expected_artifacts
            and prepared.get("evidence_hash") == evidence_hash
            and prepared.get("action_id") == intent.get("action_id")
            and prepared.get("pages_tree_hash")
            == object_hash({"pages": references})
            and prepared.get("total_pixels")
            == sum(page.get("pixels", -1) for page in intent.get("pages", []))
        )

    if event == "preflight_dependency_intent":
        expected_preflight = {
            "status": "dependency_missing",
            "reason_code": "dependency_missing",
            "resume_state": "preparing",
            "render_dpi": intent.get("render_dpi"),
            "dependencies": intent.get("dependencies"),
            "missing": intent.get("missing"),
            "pending_action": None,
            "result": None,
            "decision": None,
        }
        return (
            desired.get("conversion_state") == "recoverable_error"
            and desired.get("preflight") == expected_preflight
            and desired.get("artifacts") == previous.get("artifacts")
        )

    if event == "preflight_record_intent":
        previous_preflight = previous.get("preflight")
        pending = (
            previous_preflight.get("pending_action")
            if isinstance(previous_preflight, dict)
            else None
        )
        payload = intent.get("payload")
        if (
            not isinstance(pending, dict)
            or pending.get("kind") != "record_preflight"
            or pending.get("generation") != intent.get("expected_generation")
            or pending.get("action_id") != intent.get("action_id")
            or pending.get("evidence_hash") != intent.get("evidence_hash")
            or not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("source_sha256") != intent.get("source_sha256")
            or payload.get("baseline_generation") != intent.get("expected_generation")
            or payload.get("baseline_evidence_hash") != intent.get("evidence_hash")
            or payload.get("recorded_at") != intent.get("at")
            or payload.get("result") not in {"pass", "warning", "blocked"}
            or not isinstance(payload.get("pages"), list)
            or prepared.get("preflight_sha256") != intent.get("payload_sha256")
            or prepared.get("preflight_path") != "04-review/preflight.json"
        ):
            return False
        summary = payload["result"]
        interaction_mode = previous["settings_snapshot"]["interaction_mode"]
        expected_state = (
            "ready_to_submit"
            if summary == "pass"
            or (summary == "warning" and interaction_mode == "auto")
            else "preflight_warning"
            if summary == "warning"
            else "preflight_blocked"
        )
        expected_pending = (
            {
                "kind": "record_preflight_decision",
                "action_id": intent.get("next_action_id"),
                "generation": intent.get("new_generation"),
                "evidence_hash": f"sha256:{intent['payload_sha256']}",
            }
            if summary == "warning" and interaction_mode == "confirm"
            else None
        )
        if (expected_pending is None) != (intent.get("next_action_id") is None):
            return False
        expected_preflight = dict(previous_preflight)
        expected_preflight["pending_action"] = expected_pending
        expected_preflight["result"] = {
            "status": summary,
            "path": "04-review/preflight.json",
            "sha256": intent["payload_sha256"],
        }
        if summary == "pass":
            expected_preflight["reason_code"] = None
            expected_preflight["decision"] = {
                "status": "not_required",
                "source": "preflight_pass",
            }
        elif interaction_mode == "confirm" and summary == "warning":
            expected_preflight["reason_code"] = "risk_detected"
            expected_preflight["decision"] = {"status": "pending", "source": None}
        elif summary == "warning":
            expected_preflight["reason_code"] = None
            expected_preflight["decision"] = {
                "status": "accepted",
                "source": "interaction_mode_auto",
                "at": intent.get("at"),
                "evidence_hash": f"sha256:{intent['payload_sha256']}",
            }
        else:
            expected_preflight["reason_code"] = "unreadable_input"
            expected_preflight["decision"] = {
                "status": "not_applicable",
                "source": "preflight_blocked",
            }
        return (
            desired.get("conversion_state") == expected_state
            and desired.get("preflight") == expected_preflight
            and desired.get("artifacts")
            == {**previous["artifacts"], "preflight": "04-review/preflight.json"}
        )

    if event == "preflight_decision_intent":
        previous_preflight = previous.get("preflight")
        pending = (
            previous_preflight.get("pending_action")
            if isinstance(previous_preflight, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or pending.get("kind") != "record_preflight_decision"
            or pending.get("generation") != intent.get("expected_generation")
            or pending.get("action_id") != intent.get("action_id")
            or pending.get("evidence_hash") != intent.get("evidence_hash")
        ):
            return False
        expected_preflight = dict(previous_preflight)
        expected_preflight["pending_action"] = None
        expected_preflight["reason_code"] = (
            None if intent.get("decision") == "accept" else "preflight_declined"
        )
        expected_preflight["decision"] = {
            "status": "accepted"
            if intent.get("decision") == "accept"
            else "declined",
            "source": "user_confirmation",
            "basis": intent.get("basis"),
            "at": intent.get("at"),
            "evidence_hash": intent.get("evidence_hash"),
        }
        return (
            desired.get("conversion_state")
            == (
                "ready_to_submit"
                if intent.get("decision") == "accept"
                else "terminal_error"
            )
            and desired.get("preflight") == expected_preflight
            and desired.get("artifacts") == previous.get("artifacts")
        )

    if event == "preflight_block_intent":
        payload = intent.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("result") != "blocked"
            or payload.get("source_sha256") != intent.get("source_sha256")
            or payload.get("deterministic_blockers") != [intent.get("blocker")]
            or prepared.get("preflight_sha256") != intent.get("payload_sha256")
            or prepared.get("preflight_path") != "04-review/preflight.json"
        ):
            return False
        expected_preflight = {
            "status": "deterministic_blocked",
            "reason_code": "unreadable_input",
            "resume_state": None,
            "render_dpi": intent.get("render_dpi"),
            "dependencies": intent.get("dependencies"),
            "missing": [],
            "pending_action": None,
            "result": {
                "status": "blocked",
                "path": "04-review/preflight.json",
                "sha256": intent.get("payload_sha256"),
            },
            "decision": {
                "status": "not_applicable",
                "source": "deterministic_block",
            },
            "deterministic_blockers": [intent.get("blocker")],
        }
        return (
            desired.get("conversion_state") == "preflight_blocked"
            and desired.get("preflight") == expected_preflight
            and desired.get("artifacts")
            == {**previous["artifacts"], "preflight": "04-review/preflight.json"}
        )
    return False


def _valid_prepared(intent: dict, prepared: dict) -> bool:
    event_prefix = intent["event"].removesuffix("_intent")
    common = {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "desired_manifest",
            "desired_manifest_hash",
            "desired_private_hash",
    }
    baseline_extra = {
            "inventory_sha256",
            "page_references",
            "pages_tree_hash",
            "total_pixels",
            "evidence_hash",
            "action_id",
    }
    record_extra = {"preflight_sha256", "preflight_path"}
    expected_fields = common | (
        baseline_extra
        if event_prefix == "preflight_baseline"
        else record_extra
        if event_prefix in {"preflight_record", "preflight_block"}
        else set()
    )
    desired_manifest = prepared.get("desired_manifest")
    return (
        set(prepared) == expected_fields
        and type(prepared.get("schema_version")) is int
        and prepared.get("schema_version") == SCHEMA_VERSION
        and prepared.get("event") == event_prefix + "_prepared"
        and prepared.get("operation_id") == intent.get("operation_id")
        and prepared.get("expected_generation") == intent.get("expected_generation")
        and prepared.get("new_generation") == intent.get("new_generation")
        and _timestamp(prepared.get("at"))
        and (
            event_prefix != "preflight_baseline"
            or prepared.get("action_id") == intent.get("action_id")
        )
        and isinstance(desired_manifest, dict)
        and object_hash(desired_manifest) == prepared.get("desired_manifest_hash")
        and _valid_prepared_semantics(intent, prepared)
    )


def _valid_committed(intent: dict, committed: dict, desired_manifest: dict, desired_private: dict) -> bool:
    event_prefix = intent["event"].removesuffix("_intent")
    return (
        set(committed)
        == {
            "schema_version",
            "event",
            "operation_id",
            "previous_generation",
            "generation",
            "at",
            "manifest_hash",
            "private_hash",
        }
        and type(committed.get("schema_version")) is int
        and committed.get("schema_version") == SCHEMA_VERSION
        and committed.get("event") == event_prefix + "_committed"
        and committed.get("operation_id") == intent.get("operation_id")
        and committed.get("previous_generation") == intent.get("expected_generation")
        and committed.get("generation") == intent.get("new_generation")
        and _timestamp(committed.get("at"))
        and committed.get("manifest_hash") == object_hash(desired_manifest)
        and committed.get("private_hash") == object_hash(desired_private)
    )


def _valid_intent(intent: dict) -> bool:
    validators = {
        "preflight_dependency_intent": _valid_dependency_intent,
        "preflight_baseline_intent": _valid_baseline_intent,
        "preflight_record_intent": _valid_record_intent,
        "preflight_decision_intent": _valid_decision_intent,
        "preflight_block_intent": _valid_block_intent,
    }
    validator = validators.get(intent.get("event"))
    return validator(intent) if validator is not None else False


def _valid_committed_prefix(
    history: list[dict], previous_manifest: dict, previous_private: dict
) -> bool:
    if any(
        isinstance(event, dict) and str(event.get("event", "")).startswith("preflight_")
        for event in history
    ):
        return valid_preflight_history(history, previous_manifest, previous_private)
    source = previous_manifest.get("source")
    return isinstance(source, dict) and bundle.valid_settings_history(
        history, previous_manifest, source.get("sha256")
    )


def _prepare_complete_baseline_intent(
    *,
    descriptors: dict,
    intent: dict,
    previous_manifest: dict,
    previous_private: dict,
    at: str,
) -> None:
    inventory_bytes = _private_file_bytes(
        intent["inventory_temporary_name"],
        dir_fd=descriptors["source"],
        max_bytes=MAX_INVENTORY_BYTES,
    )
    try:
        inventory = bundle.decode_json_object(inventory_bytes)
    except bundle.BundleStateError as exc:
        raise PreflightError(
            "integrity_violation", "A completed temporary source inventory is invalid."
        ) from exc
    plans = intent.get("pages")
    inventory_pages = inventory.get("pages") if isinstance(inventory, dict) else None
    if (
        not isinstance(plans, list)
        or not isinstance(inventory_pages, list)
        or inventory.get("source_sha256") != intent.get("source_sha256")
        or inventory.get("page_count") != len(plans)
        or inventory.get("render")
        != {"dpi": intent.get("render_dpi"), "format": "png", "lossless": True}
        or inventory.get("resource_limits") != intent.get("resource_limits")
        or inventory.get("dependencies") != intent.get("dependencies")
        or len(inventory_pages) != len(plans)
    ):
        raise PreflightError(
            "integrity_violation", "A completed temporary baseline does not match its intent."
        )
    page_references = []
    for plan, inventory_page in zip(plans, inventory_pages):
        if not isinstance(plan, dict) or not isinstance(inventory_page, dict):
            raise PreflightError(
                "integrity_violation", "A temporary baseline page entry is invalid."
            )
        expected_path = f"02-pages/{plan.get('final_name')}"
        image_sha256 = inventory_page.get("image_sha256")
        if (
            inventory_page.get("page_number") != plan.get("page_number")
            or inventory_page.get("pixel_width") != plan.get("pixel_width")
            or inventory_page.get("pixel_height") != plan.get("pixel_height")
            or inventory_page.get("pixels") != plan.get("pixels")
            or inventory_page.get("page_reference") != expected_path
            or not isinstance(image_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", image_sha256) is None
        ):
            raise PreflightError(
                "integrity_violation", "A temporary baseline page identity is invalid."
            )
        png = _private_file_bytes(
            plan["temporary_name"],
            dir_fd=descriptors["pages"],
            max_bytes=_page_image_byte_limit(plan["pixels"]),
        )
        if bytes_hash(png) != image_sha256 or _png_dimensions(png) != (
            plan["pixel_width"],
            plan["pixel_height"],
        ):
            raise PreflightError(
                "integrity_violation", "A temporary page reference does not match its inventory."
            )
        page_references.append(
            {
                "page_number": plan["page_number"],
                "path": expected_path,
                "pixel_width": plan["pixel_width"],
                "pixel_height": plan["pixel_height"],
                "pixels": plan["pixels"],
                "image_sha256": image_sha256,
            }
        )
    inventory_sha256 = bytes_hash(inventory_bytes)
    evidence_hash = object_hash(
        {
            "source_sha256": intent["source_sha256"],
            "render_dpi": intent["render_dpi"],
            "dependencies": intent["dependencies"],
            "inventory_sha256": inventory_sha256,
            "pages": page_references,
        }
    )
    desired_manifest = dict(previous_manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = "preflight_pending"
    desired_manifest["artifacts"] = {
        "source_pdf": "01-source/source.pdf",
        "source_inventory": "01-source/source-inventory.json",
        "page_references": [item["path"] for item in page_references],
    }
    desired_manifest["preflight"] = {
        "render_dpi": intent["render_dpi"],
        "page_count": len(plans),
        "dependencies": intent["dependencies"],
        "inventory_sha256": inventory_sha256,
        "page_references": page_references,
        "evidence_hash": evidence_hash,
        "pending_action": {
            "kind": "record_preflight",
            "action_id": intent["action_id"],
            "generation": intent["new_generation"],
            "evidence_hash": evidence_hash,
        },
        "result": None,
        "decision": None,
    }
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_baseline_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": at,
            "inventory_sha256": inventory_sha256,
            "page_references": page_references,
            "pages_tree_hash": object_hash({"pages": page_references}),
            "total_pixels": sum(item["pixels"] for item in page_references),
            "evidence_hash": evidence_hash,
            "action_id": intent["action_id"],
            "desired_manifest": desired_manifest,
            "desired_manifest_hash": object_hash(desired_manifest),
            "desired_private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )


def _prepare_record_intent(
    *,
    descriptors: dict,
    intent: dict,
    previous_manifest: dict,
    previous_private: dict,
    at: str,
) -> None:
    record = intent.get("payload")
    pending_action = previous_manifest.get("preflight", {}).get("pending_action")
    if (
        not isinstance(record, dict)
        or previous_manifest.get("conversion_state") != "preflight_pending"
        or not isinstance(pending_action, dict)
        or pending_action.get("action_id") != intent.get("action_id")
        or pending_action.get("evidence_hash") != intent.get("evidence_hash")
        or record.get("source_sha256") != intent.get("source_sha256")
        or record.get("baseline_generation") != intent.get("expected_generation")
        or record.get("baseline_evidence_hash") != intent.get("evidence_hash")
        or record.get("recorded_at") != intent.get("at")
    ):
        raise PreflightError(
            "integrity_violation", "A pending preflight record does not match its action."
        )
    inventory_bytes = _private_file_bytes(
        "source-inventory.json",
        dir_fd=descriptors["source"],
        max_bytes=MAX_INVENTORY_BYTES,
    )
    try:
        inventory = bundle.decode_json_object(inventory_bytes)
    except bundle.BundleStateError as exc:
        raise PreflightError("integrity_violation", "The source inventory is invalid.") from exc
    normalized = _normalized_preflight_payload(
        {
            "schema_version": record.get("schema_version"),
            "summary": record.get("result"),
            "pages": [
                {
                    key: page.get(key)
                    for key in (
                        "page_number",
                        "classification",
                        "risk_codes",
                        "evidence",
                    )
                }
                for page in record.get("pages", [])
                if isinstance(page, dict)
            ],
        },
        inventory=inventory,
    )
    if normalized["summary"] != record.get("result") or normalized["pages"] != record.get("pages"):
        raise PreflightError(
            "integrity_violation", "A pending preflight record payload is not normalized."
        )
    record_bytes = _json_bytes(record)
    record_sha256 = bytes_hash(record_bytes)
    if record_sha256 != intent.get("payload_sha256"):
        raise PreflightError(
            "integrity_violation", "A pending preflight record payload hash changed."
        )
    temporary_name = intent["payload_temporary_name"]
    if _artifact_exists("preflight.json", dir_fd=descriptors["review"]):
        raise PreflightError(
            "integrity_violation", "A preflight record was promoted before prepared."
        )
    if _artifact_exists(temporary_name, dir_fd=descriptors["review"]):
        if (
            bytes_hash(
                _private_file_bytes(
                    temporary_name,
                    dir_fd=descriptors["review"],
                    max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
                )
            )
            != record_sha256
        ):
            raise PreflightError(
                "integrity_violation", "A pending preflight record temp hash changed."
            )
    else:
        _write_private_file(temporary_name, record_bytes, dir_fd=descriptors["review"])

    summary = normalized["summary"]
    interaction_mode = previous_manifest["settings_snapshot"]["interaction_mode"]
    next_action_id = intent.get("next_action_id")
    if (summary == "warning" and interaction_mode == "confirm") != (
        next_action_id is not None
    ):
        raise PreflightError(
            "integrity_violation", "A pending warning decision action is inconsistent."
        )
    desired_manifest = dict(previous_manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = (
        "ready_to_submit"
        if summary == "pass" or (summary == "warning" and interaction_mode == "auto")
        else "preflight_warning"
        if summary == "warning"
        else "preflight_blocked"
    )
    desired_manifest["artifacts"] = {
        **previous_manifest["artifacts"],
        "preflight": "04-review/preflight.json",
    }
    desired_preflight = dict(previous_manifest["preflight"])
    desired_preflight["pending_action"] = (
        None
        if next_action_id is None
        else {
            "kind": "record_preflight_decision",
            "action_id": next_action_id,
            "generation": intent["new_generation"],
            "evidence_hash": f"sha256:{record_sha256}",
        }
    )
    desired_preflight["result"] = {
        "status": summary,
        "path": "04-review/preflight.json",
        "sha256": record_sha256,
    }
    if summary == "pass":
        desired_preflight["reason_code"] = None
        desired_preflight["decision"] = {
            "status": "not_required",
            "source": "preflight_pass",
        }
    elif summary == "warning" and interaction_mode == "confirm":
        desired_preflight["reason_code"] = "risk_detected"
        desired_preflight["decision"] = {"status": "pending", "source": None}
    elif summary == "warning":
        desired_preflight["reason_code"] = None
        desired_preflight["decision"] = {
            "status": "accepted",
            "source": "interaction_mode_auto",
            "at": intent["at"],
            "evidence_hash": f"sha256:{record_sha256}",
        }
    else:
        desired_preflight["reason_code"] = "unreadable_input"
        desired_preflight["decision"] = {
            "status": "not_applicable",
            "source": "preflight_blocked",
        }
    desired_manifest["preflight"] = desired_preflight
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_record_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": at,
            "preflight_sha256": record_sha256,
            "preflight_path": "04-review/preflight.json",
            "desired_manifest": desired_manifest,
            "desired_manifest_hash": object_hash(desired_manifest),
            "desired_private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )


def _prepare_dependency_intent(
    *, descriptors: dict, intent: dict, previous_manifest: dict, previous_private: dict, at: str
) -> None:
    if (
        previous_manifest.get("conversion_state") not in {"preparing", "recoverable_error"}
        or not isinstance(intent.get("dependencies"), list)
        or not isinstance(intent.get("missing"), list)
        or not intent["missing"]
    ):
        raise PreflightError(
            "integrity_violation", "A pending dependency result is inconsistent."
        )
    desired_manifest = dict(previous_manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = "recoverable_error"
    desired_manifest["preflight"] = {
        "status": "dependency_missing",
        "reason_code": "dependency_missing",
        "resume_state": "preparing",
        "render_dpi": intent["render_dpi"],
        "dependencies": intent["dependencies"],
        "missing": intent["missing"],
        "pending_action": None,
        "result": None,
        "decision": None,
    }
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_dependency_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": at,
            "desired_manifest": desired_manifest,
            "desired_manifest_hash": object_hash(desired_manifest),
            "desired_private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )


def _prepare_decision_intent(
    *, descriptors: dict, intent: dict, previous_manifest: dict, previous_private: dict, at: str
) -> None:
    pending = previous_manifest.get("preflight", {}).get("pending_action")
    if (
        previous_manifest.get("conversion_state") != "preflight_warning"
        or not isinstance(pending, dict)
        or pending.get("action_id") != intent.get("action_id")
        or pending.get("evidence_hash") != intent.get("evidence_hash")
    ):
        raise PreflightError(
            "integrity_violation", "A pending preflight decision is inconsistent."
        )
    decision = intent["decision"]
    desired_manifest = dict(previous_manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = (
        "ready_to_submit" if decision == "accept" else "terminal_error"
    )
    desired_preflight = dict(previous_manifest["preflight"])
    desired_preflight["pending_action"] = None
    desired_preflight["reason_code"] = (
        None if decision == "accept" else "preflight_declined"
    )
    desired_preflight["decision"] = {
        "status": "accepted" if decision == "accept" else "declined",
        "source": "user_confirmation",
        "basis": intent["basis"],
        "at": intent["at"],
        "evidence_hash": intent["evidence_hash"],
    }
    desired_manifest["preflight"] = desired_preflight
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_decision_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": at,
            "desired_manifest": desired_manifest,
            "desired_manifest_hash": object_hash(desired_manifest),
            "desired_private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )


def _prepare_block_intent(
    *, descriptors: dict, intent: dict, previous_manifest: dict, previous_private: dict, at: str
) -> None:
    record = intent.get("payload")
    if (
        not isinstance(record, dict)
        or record.get("result") != "blocked"
        or record.get("deterministic_blockers") != [intent.get("blocker")]
        or record.get("source_sha256") != intent.get("source_sha256")
    ):
        raise PreflightError(
            "integrity_violation", "A pending deterministic block payload is inconsistent."
        )
    record_bytes = _json_bytes(record)
    record_sha256 = bytes_hash(record_bytes)
    if record_sha256 != intent.get("payload_sha256"):
        raise PreflightError(
            "integrity_violation", "A pending deterministic block payload hash changed."
        )
    temporary_name = intent["payload_temporary_name"]
    if _artifact_exists("preflight.json", dir_fd=descriptors["review"]):
        raise PreflightError(
            "integrity_violation", "Deterministic block evidence was promoted before prepared."
        )
    if _artifact_exists(temporary_name, dir_fd=descriptors["review"]):
        if (
            bytes_hash(
                _private_file_bytes(
                    temporary_name,
                    dir_fd=descriptors["review"],
                    max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
                )
            )
            != record_sha256
        ):
            raise PreflightError(
                "integrity_violation", "Pending deterministic block evidence changed."
            )
    else:
        _write_private_file(temporary_name, record_bytes, dir_fd=descriptors["review"])
    desired_manifest = dict(previous_manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = "preflight_blocked"
    desired_manifest["artifacts"] = {
        **previous_manifest["artifacts"],
        "preflight": "04-review/preflight.json",
    }
    desired_manifest["preflight"] = {
        "status": "deterministic_blocked",
        "reason_code": "unreadable_input",
        "resume_state": None,
        "render_dpi": intent["render_dpi"],
        "dependencies": intent["dependencies"],
        "missing": [],
        "pending_action": None,
        "result": {
            "status": "blocked",
            "path": "04-review/preflight.json",
            "sha256": record_sha256,
        },
        "decision": {
            "status": "not_applicable",
            "source": "deterministic_block",
        },
        "deterministic_blockers": [intent["blocker"]],
    }
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "preflight_block_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": at,
            "preflight_sha256": record_sha256,
            "preflight_path": "04-review/preflight.json",
            "desired_manifest": desired_manifest,
            "desired_manifest_hash": object_hash(desired_manifest),
            "desired_private_hash": object_hash(desired_private),
        },
        state_fd=descriptors["state"],
    )


def recover_pending_operation(*, descriptors: dict, at: str) -> dict | None:
    history = bundle.read_history(state_fd=descriptors["state"])
    final_event = history[-1].get("event")
    if final_event == "preflight_baseline_aborted":
        if len(history) < 2:
            raise PreflightError(
                "integrity_violation", "An aborted baseline has no matching intent."
            )
        intent = history[-2]
        aborted = history[-1]
        reduced = reduce_preflight_history(history)
        current_manifest = bundle.read_json(
            "manifest.json", dir_fd=descriptors["root"]
        )
        current_private = bundle.read_json(
            "private.json", dir_fd=descriptors["state"]
        )
        if (
            not isinstance(intent, dict)
            or intent.get("event") != "preflight_baseline_intent"
            or not _valid_baseline_intent(intent)
            or reduced != (current_manifest, current_private)
            or aborted.get("operation_id") != intent.get("operation_id")
        ):
            raise PreflightError(
                "integrity_violation", "An aborted baseline cannot be recovered safely."
            )
        blocker = aborted.get("blocker")
        if not isinstance(blocker, dict):
            raise PreflightError(
                "integrity_violation", "An aborted baseline lost its result evidence."
            )
        if aborted.get("reason_code") == "dependency_missing":
            dependencies = blocker.get("dependencies")
            missing = blocker.get("missing")
            if not isinstance(dependencies, list) or not isinstance(missing, list):
                raise PreflightError(
                    "integrity_violation",
                    "An aborted dependency gate lost its capability evidence.",
                )
            updated_manifest = commit_dependency_missing(
                descriptors=descriptors,
                manifest=current_manifest,
                private_state=current_private,
                dependencies=dependencies,
                missing=missing,
                render_dpi=intent["render_dpi"],
                at=at,
            )
        else:
            updated_manifest = commit_deterministic_block(
                descriptors=descriptors,
                manifest=current_manifest,
                private_state=current_private,
                dependencies=intent["dependencies"],
                render_dpi=intent["render_dpi"],
                blocker=blocker,
                at=at,
            )
        return {
            "expected_generation": intent["expected_generation"],
            "generation": updated_manifest["generation"],
            "manifest": updated_manifest,
            "operation_id": intent["operation_id"],
            "event": intent["event"],
            "intent": intent,
        }
    if not isinstance(final_event, str) or not final_event.endswith(
        ("_intent", "_prepared")
    ) or not final_event.startswith("preflight_"):
        return None
    if final_event.endswith("_prepared"):
        if len(history) < 2:
            raise PreflightError(
                "integrity_violation", "A pending preflight operation has no intent."
            )
        prefix = history[:-2]
        intent = history[-2]
        prepared = history[-1]
    else:
        prefix = history[:-1]
        intent = history[-1]
        if not isinstance(intent, dict) or not _valid_intent(intent):
            raise PreflightError(
                "integrity_violation", "A pending preflight intent is invalid."
            )
        previous_manifest = intent.get("previous_manifest")
        if not isinstance(previous_manifest, dict):
            raise PreflightError(
                "integrity_violation", "A pending preflight intent lost its previous state."
            )
        previous_private = {
            "schema_version": SCHEMA_VERSION,
            "generation": intent["expected_generation"],
            "source_uploads": [],
            "result_urls": [],
        }
        current_manifest = bundle.read_json("manifest.json", dir_fd=descriptors["root"])
        current_private = bundle.read_json("private.json", dir_fd=descriptors["state"])
        if (
            intent.get("previous_manifest_hash") != object_hash(previous_manifest)
            or intent.get("previous_private_hash") != object_hash(previous_private)
            or not _valid_committed_prefix(prefix, previous_manifest, previous_private)
            or current_manifest != previous_manifest
            or current_private != previous_private
        ):
            raise PreflightError(
                "integrity_violation", "A pending preflight intent does not match committed state."
            )
        if intent.get("event") == "preflight_record_intent":
            _prepare_record_intent(
                descriptors=descriptors,
                intent=intent,
                previous_manifest=previous_manifest,
                previous_private=previous_private,
                at=at,
            )
            return recover_pending_operation(descriptors=descriptors, at=at)
        if intent.get("event") == "preflight_dependency_intent":
            _prepare_dependency_intent(
                descriptors=descriptors,
                intent=intent,
                previous_manifest=previous_manifest,
                previous_private=previous_private,
                at=at,
            )
            return recover_pending_operation(descriptors=descriptors, at=at)
        if intent.get("event") == "preflight_decision_intent":
            _prepare_decision_intent(
                descriptors=descriptors,
                intent=intent,
                previous_manifest=previous_manifest,
                previous_private=previous_private,
                at=at,
            )
            return recover_pending_operation(descriptors=descriptors, at=at)
        if intent.get("event") == "preflight_block_intent":
            _prepare_block_intent(
                descriptors=descriptors,
                intent=intent,
                previous_manifest=previous_manifest,
                previous_private=previous_private,
                at=at,
            )
            return recover_pending_operation(descriptors=descriptors, at=at)
        if intent.get("event") != "preflight_baseline_intent":
            raise PreflightError(
                "pending_operation_incomplete",
                "A pending preflight operation must reconstruct its prepared state.",
            )
        if _artifact_exists("source-inventory.json", dir_fd=descriptors["source"]) or any(
            _artifact_exists(page["final_name"], dir_fd=descriptors["pages"])
            for page in intent["pages"]
        ):
            raise PreflightError(
                "integrity_violation",
                "A baseline artifact was promoted before its prepared record.",
            )
        temporary_presence = [
            _artifact_exists(
                intent["inventory_temporary_name"], dir_fd=descriptors["source"]
            ),
            *[
                _artifact_exists(page["temporary_name"], dir_fd=descriptors["pages"])
                for page in intent["pages"]
            ],
        ]
        if temporary_presence and all(temporary_presence):
            try:
                _prepare_complete_baseline_intent(
                    descriptors=descriptors,
                    intent=intent,
                    previous_manifest=previous_manifest,
                    previous_private=previous_private,
                    at=at,
                )
            except PreflightError:
                pass
            else:
                return recover_pending_operation(descriptors=descriptors, at=at)
        fitz = importlib.import_module("fitz")
        pymupdf_dependency = next(
            (
                item
                for item in intent["dependencies"]
                if isinstance(item, dict) and item.get("name") == "pymupdf"
            ),
            None,
        )
        if (
            not isinstance(pymupdf_dependency, dict)
            or pymupdf_dependency.get("version")
            != _module_version(fitz, "VersionBind", "__version__")
        ):
            raise PreflightError(
                "dependency_drift", "PyMuPDF changed during baseline recovery."
            )
        desired_manifest = build_baseline(
            descriptors=descriptors,
            manifest=previous_manifest,
            private_state=previous_private,
            dependencies=intent["dependencies"],
            fitz=fitz,
            render_dpi=intent["render_dpi"],
            at=at,
            pending_intent=intent,
        )
        return {
            "expected_generation": intent["expected_generation"],
            "generation": intent["new_generation"],
            "manifest": desired_manifest,
            "operation_id": intent["operation_id"],
            "event": intent["event"],
            "intent": intent,
        }
    if not isinstance(intent, dict) or not isinstance(prepared, dict) or not _valid_intent(intent):
        raise PreflightError(
            "integrity_violation", "A pending preflight operation uses an invalid intent."
        )
    previous_manifest = intent.get("previous_manifest")
    if not isinstance(previous_manifest, dict):
        raise PreflightError(
            "integrity_violation", "A pending preflight operation lost its previous state."
        )
    previous_private = {
        "schema_version": SCHEMA_VERSION,
        "generation": intent["expected_generation"],
        "source_uploads": [],
        "result_urls": [],
    }
    if (
        intent.get("previous_manifest_hash") != object_hash(previous_manifest)
        or intent.get("previous_private_hash") != object_hash(previous_private)
        or not _valid_committed_prefix(prefix, previous_manifest, previous_private)
        or not _valid_prepared(intent, prepared)
    ):
        raise PreflightError(
            "integrity_violation", "A pending preflight operation does not match committed history."
        )
    desired_manifest = prepared["desired_manifest"]
    desired_private = dict(previous_private)
    desired_private["generation"] = intent["new_generation"]
    if prepared.get("desired_private_hash") != object_hash(desired_private):
        raise PreflightError(
            "integrity_violation", "A pending preflight private state hash is invalid."
        )
    current_manifest = bundle.read_json("manifest.json", dir_fd=descriptors["root"])
    current_private = bundle.read_json("private.json", dir_fd=descriptors["state"])
    if current_manifest != previous_manifest and current_manifest != desired_manifest:
        raise PreflightError(
            "integrity_violation", "A pending preflight manifest has an impossible state."
        )
    if current_private != previous_private and current_private != desired_private:
        raise PreflightError(
            "integrity_violation", "A pending preflight private state has an impossible state."
        )
    if current_manifest == desired_manifest and current_private != desired_private:
        raise PreflightError(
            "integrity_violation", "The preflight commit point precedes private state."
        )
    if intent["event"] == "preflight_baseline_intent":
        _recover_private_artifact(
            intent["inventory_temporary_name"],
            "source-inventory.json",
            dir_fd=descriptors["source"],
            expected_sha256=prepared["inventory_sha256"],
            max_bytes=MAX_INVENTORY_BYTES,
        )
        plans = intent.get("pages")
        references = prepared.get("page_references")
        if not isinstance(plans, list) or not isinstance(references, list) or len(plans) != len(references):
            raise PreflightError(
                "integrity_violation", "A pending baseline page plan is inconsistent."
            )
        for plan, reference in zip(plans, references):
            expected_path = f"02-pages/{plan.get('final_name')}"
            if (
                reference.get("page_number") != plan.get("page_number")
                or reference.get("path") != expected_path
            ):
                raise PreflightError(
                    "integrity_violation", "A pending baseline page identity is inconsistent."
                )
            _recover_private_artifact(
                plan["temporary_name"],
                plan["final_name"],
                dir_fd=descriptors["pages"],
                expected_sha256=reference["image_sha256"],
                max_bytes=_page_image_byte_limit(plan["pixels"]),
            )
    elif intent["event"] in {"preflight_record_intent", "preflight_block_intent"}:
        _recover_private_artifact(
            intent["payload_temporary_name"],
            "preflight.json",
            dir_fd=descriptors["review"],
            expected_sha256=prepared["preflight_sha256"],
            max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
        )
    if intent.get("event") != "preflight_dependency_intent":
        validate_baseline_artifacts(descriptors=descriptors, manifest=desired_manifest)
    if current_private == previous_private:
        bundle.atomic_write_json(
            "private.json", desired_private, dir_fd=descriptors["state"]
        )
        current_private = desired_private
    if current_manifest == previous_manifest:
        bundle.atomic_write_json(
            "manifest.json", desired_manifest, dir_fd=descriptors["root"]
        )
        current_manifest = desired_manifest
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": intent["event"].removesuffix("_intent") + "_committed",
        "operation_id": intent["operation_id"],
        "previous_generation": intent["expected_generation"],
        "generation": intent["new_generation"],
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return {
        "expected_generation": intent["expected_generation"],
        "generation": intent["new_generation"],
        "manifest": desired_manifest,
        "operation_id": intent["operation_id"],
        "event": intent["event"],
        "intent": intent,
    }


def reduce_preflight_history(history: list[dict]) -> tuple[dict, dict] | None:
    first_operation = next(
        (
            index
            for index, event in enumerate(history)
            if isinstance(event, dict)
            and event.get("event")
            in {
                "preflight_dependency_intent",
                "preflight_baseline_intent",
                "preflight_record_intent",
                "preflight_decision_intent",
                "preflight_block_intent",
            }
        ),
        None,
    )
    if first_operation is None:
        return None
    first_intent = history[first_operation]
    current_manifest = first_intent.get("previous_manifest")
    if not isinstance(current_manifest, dict):
        return None
    source = current_manifest.get("source")
    if not isinstance(source, dict) or not bundle.valid_settings_history(
        history[:first_operation], current_manifest, source.get("sha256")
    ):
        return None
    current_private = {
        "schema_version": SCHEMA_VERSION,
        "generation": current_manifest["generation"],
        "source_uploads": [],
        "result_urls": [],
    }
    operation_ids = set()
    remaining = history[first_operation:]
    offset = 0
    while offset < len(remaining):
        intent = remaining[offset]
        if not isinstance(intent, dict):
            return None
        if intent.get("event") == "settings_override_intent":
            if offset + 2 >= len(remaining):
                return None
            prepared, committed = remaining[offset + 1 : offset + 3]
            transition = bundle.apply_settings_override_events(
                current_manifest,
                current_private,
                intent,
                prepared,
                committed,
            )
            if transition is None:
                return None
            current_manifest, current_private = transition
            offset += 3
            continue
        valid_intent = _valid_intent(intent)
        operation_id = intent.get("operation_id")
        if (
            not valid_intent
            or operation_id in operation_ids
            or intent.get("expected_generation") != current_manifest["generation"]
            or intent.get("source_sha256") != source.get("sha256")
            or intent.get("previous_manifest") != current_manifest
            or intent.get("previous_manifest_hash") != object_hash(current_manifest)
            or intent.get("previous_private_hash") != object_hash(current_private)
        ):
            return None
        if offset + 1 < len(remaining):
            aborted = remaining[offset + 1]
            if isinstance(aborted, dict) and aborted.get("event") == "preflight_baseline_aborted":
                if (
                    intent.get("event") != "preflight_baseline_intent"
                    or set(aborted)
                    != {
                        "schema_version",
                        "event",
                        "operation_id",
                        "expected_generation",
                        "at",
                        "reason_code",
                        "blocker",
                    }
                    or type(aborted.get("schema_version")) is not int
                    or aborted["schema_version"] != SCHEMA_VERSION
                    or aborted.get("operation_id") != operation_id
                    or aborted.get("expected_generation")
                    != intent.get("expected_generation")
                    or not _timestamp(aborted.get("at"))
                    or aborted.get("reason_code")
                    not in {"deterministic_block", "dependency_missing"}
                    or not isinstance(aborted.get("blocker"), dict)
                ):
                    return None
                operation_ids.add(operation_id)
                offset += 2
                continue
        if offset + 2 >= len(remaining):
            return None
        prepared, committed = remaining[offset + 1 : offset + 3]
        if (
            not isinstance(prepared, dict)
            or not isinstance(committed, dict)
            or not _valid_prepared(intent, prepared)
        ):
            return None
        desired_manifest = prepared["desired_manifest"]
        desired_private = dict(current_private)
        desired_private["generation"] = intent["new_generation"]
        if (
            desired_manifest.get("generation") != intent["new_generation"]
            or prepared.get("desired_private_hash") != object_hash(desired_private)
            or not _valid_committed(
                intent, committed, desired_manifest, desired_private
            )
        ):
            return None
        operation_ids.add(operation_id)
        current_manifest = desired_manifest
        current_private = desired_private
        offset += 3
    return current_manifest, current_private


def valid_preflight_history(
    history: list[dict], manifest: dict, private_state: dict
) -> bool:
    reduced = reduce_preflight_history(history)
    return reduced == (manifest, private_state)


def valid_pending_preflight_history(
    history: list[dict], manifest: dict, private_state: dict
) -> bool:
    if not history or not isinstance(history[-1], dict):
        return False
    final_event = history[-1].get("event")
    if final_event == "preflight_baseline_aborted":
        return valid_preflight_history(history, manifest, private_state)
    if not isinstance(final_event, str) or not final_event.startswith(
        "preflight_"
    ) or not final_event.endswith(("_intent", "_prepared")):
        return False
    if final_event.endswith("_intent"):
        prefix = history[:-1]
        intent = history[-1]
        prepared = None
    elif len(history) >= 2:
        prefix = history[:-2]
        intent = history[-2]
        prepared = history[-1]
    else:
        return False
    if not isinstance(intent, dict) or not _valid_intent(intent):
        return False
    if (
        intent.get("previous_manifest") != manifest
        or intent.get("previous_manifest_hash") != object_hash(manifest)
        or intent.get("previous_private_hash") != object_hash(private_state)
        or intent.get("expected_generation") != manifest.get("generation")
        or private_state.get("generation") != manifest.get("generation")
        or not _valid_committed_prefix(prefix, manifest, private_state)
    ):
        return False
    if prepared is None:
        return True
    desired_private = dict(private_state)
    desired_private["generation"] = intent["new_generation"]
    return (
        isinstance(prepared, dict)
        and _valid_prepared(intent, prepared)
        and prepared.get("desired_private_hash") == object_hash(desired_private)
    )


def validate_baseline_artifacts(*, descriptors: dict, manifest: dict) -> None:
    preflight_state = manifest.get("preflight")
    if not isinstance(preflight_state, dict):
        raise PreflightError("integrity_violation", "Preflight state is incomplete.")
    if preflight_state.get("status") == "deterministic_blocked":
        result = preflight_state.get("result")
        if not isinstance(result, dict) or result.get("path") != "04-review/preflight.json":
            raise PreflightError("integrity_violation", "Deterministic block evidence is incomplete.")
        preflight_bytes = _private_file_bytes(
            "preflight.json",
            dir_fd=descriptors["review"],
            max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
        )
        if bytes_hash(preflight_bytes) != result.get("sha256"):
            raise PreflightError("integrity_violation", "The preflight block evidence hash changed.")
        return
    inventory_bytes = _private_file_bytes(
        "source-inventory.json",
        dir_fd=descriptors["source"],
        max_bytes=MAX_INVENTORY_BYTES,
    )
    if bytes_hash(inventory_bytes) != preflight_state.get("inventory_sha256"):
        raise PreflightError("integrity_violation", "The source inventory hash changed.")
    try:
        inventory = bundle.decode_json_object(inventory_bytes)
    except bundle.BundleStateError as exc:
        raise PreflightError("integrity_violation", "The source inventory is invalid.") from exc
    references = preflight_state.get("page_references")
    if not isinstance(references, list) or inventory.get("page_count") != len(references):
        raise PreflightError("integrity_violation", "Page reference coverage is incomplete.")
    for index, reference in enumerate(references, start=1):
        expected_path = f"02-pages/page-{index:0{max(4, len(str(len(references))))}d}.png"
        if reference.get("page_number") != index or reference.get("path") != expected_path:
            raise PreflightError("integrity_violation", "Page reference numbering is discontinuous.")
        pixels = reference.get("pixels")
        if type(pixels) is not int or pixels <= 0:
            raise PreflightError(
                "integrity_violation", "A page reference pixel count is invalid."
            )
        image = _private_file_bytes(
            expected_path.removeprefix("02-pages/"),
            dir_fd=descriptors["pages"],
            max_bytes=_page_image_byte_limit(pixels),
        )
        if bytes_hash(image) != reference.get("image_sha256"):
            raise PreflightError("integrity_violation", "A page reference image hash changed.")
    result = preflight_state.get("result")
    if isinstance(result, dict) and result.get("path") == "04-review/preflight.json":
        preflight_bytes = _private_file_bytes(
            "preflight.json",
            dir_fd=descriptors["review"],
            max_bytes=MAX_PREFLIGHT_RESULT_BYTES,
        )
        if bytes_hash(preflight_bytes) != result.get("sha256"):
            raise PreflightError("integrity_violation", "The preflight record hash changed.")


def result_from_manifest(manifest: dict, *, work_bundle: str, outcome: str) -> dict:
    artifacts = {"manifest": "manifest.json", **manifest["artifacts"]}
    pending_action = manifest.get("preflight", {}).get("pending_action")
    return {
        "schema_version": SCHEMA_VERSION,
        "work_bundle": work_bundle,
        "generation": manifest["generation"],
        "conversion_state": manifest["conversion_state"],
        "publication_state": manifest["publication_state"],
        "outcome": outcome,
        "action_required": None if pending_action is None else pending_action["kind"],
        "action_id": None if pending_action is None else pending_action["action_id"],
        "evidence_hash": (
            f"sha256:{manifest['source']['sha256']}"
            if pending_action is None
            else pending_action["evidence_hash"]
        ),
        "artifacts": artifacts,
        "errors": [],
    }


def dependency_result(manifest: dict, *, work_bundle: str) -> dict:
    result = result_from_manifest(
        manifest, work_bundle=work_bundle, outcome="dependency_missing"
    )
    result["action_required"] = "restore_preflight_dependencies"
    result["missing_dependencies"] = manifest["preflight"]["missing"]
    return result
