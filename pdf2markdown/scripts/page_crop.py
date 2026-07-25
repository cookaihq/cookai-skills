from __future__ import annotations

import hashlib
import importlib
import os
import re
import stat
import struct
import unicodedata
from copy import deepcopy
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
COORDINATE_SPACE = "page-png-pixels-v1"
MAX_PAGE_COUNT = 2_000
MAX_PAGE_PIXELS = 25_000_000
PNG_CONTAINER_OVERHEAD_BYTES = 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_DEPTH = 128
MAX_BASIS_REASONS = 32
MAX_BASIS_REASON_BYTES = 4096
MAX_VISUAL_KIND_BYTES = 255
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VISUAL_KIND_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


class PageCropError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _png_byte_limit(pixels: int) -> int:
    return pixels * 4 + PNG_CONTAINER_OVERHEAD_BYTES


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if (
        len(data) < 24
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
    ):
        raise PageCropError(
            "integrity_violation", "The page reference is not a PNG image."
        )
    return struct.unpack(">II", data[16:24])


def _validate_page_reference(page_reference: dict) -> dict:
    expected_keys = {
        "page_number",
        "path",
        "pixel_width",
        "pixel_height",
        "pixels",
        "image_sha256",
    }
    if not isinstance(page_reference, dict) or set(page_reference) != expected_keys:
        raise PageCropError(
            "invalid_page_reference", "The page reference schema is invalid."
        )

    page_number = page_reference.get("page_number")
    pixel_width = page_reference.get("pixel_width")
    pixel_height = page_reference.get("pixel_height")
    pixels = page_reference.get("pixels")
    image_sha256 = page_reference.get("image_sha256")
    expected_path = (
        f"02-pages/page-{page_number:04d}.png"
        if type(page_number) is int
        else None
    )
    if (
        type(page_number) is not int
        or not 1 <= page_number <= MAX_PAGE_COUNT
        or page_reference.get("path") != expected_path
        or type(pixel_width) is not int
        or pixel_width <= 0
        or type(pixel_height) is not int
        or pixel_height <= 0
        or type(pixels) is not int
        or pixels != pixel_width * pixel_height
        or pixels > MAX_PAGE_PIXELS
        or not isinstance(image_sha256, str)
        or SHA256_RE.fullmatch(image_sha256) is None
    ):
        raise PageCropError(
            "invalid_page_reference", "The page reference identity is invalid."
        )
    return deepcopy(page_reference)


def _validate_reasons(value, *, stage: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_BASIS_REASONS
    ):
        raise PageCropError(
            "invalid_crop_request", f"The {stage} fallback basis is invalid."
        )
    reasons = []
    for reason in value:
        try:
            reason_bytes = len(reason.encode("utf-8")) if isinstance(reason, str) else 0
        except UnicodeEncodeError:
            reason_bytes = MAX_BASIS_REASON_BYTES + 1
        if (
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or unicodedata.normalize("NFC", reason) != reason
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in reason
            )
            or reason_bytes > MAX_BASIS_REASON_BYTES
        ):
            raise PageCropError(
                "invalid_crop_request", f"The {stage} fallback basis is invalid."
            )
        reasons.append(reason)
    if len(set(reasons)) != len(reasons):
        raise PageCropError(
            "invalid_crop_request", f"The {stage} fallback basis is duplicated."
        )
    return reasons


def _validate_basis(value) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "markdown",
        "html",
        "visual_object",
    }:
        raise PageCropError(
            "invalid_crop_request", "The crop fallback basis is invalid."
        )

    normalized = {}
    for stage in ("markdown", "html"):
        entry = value.get(stage)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"status", "reasons"}
            or entry.get("status") != "insufficient"
        ):
            raise PageCropError(
                "invalid_crop_request",
                f"The {stage} fallback basis must record insufficiency.",
            )
        normalized[stage] = {
            "status": "insufficient",
            "reasons": _validate_reasons(entry.get("reasons"), stage=stage),
        }

    visual_object = value.get("visual_object")
    kind = visual_object.get("kind") if isinstance(visual_object, dict) else None
    try:
        kind_bytes = len(kind.encode("utf-8")) if isinstance(kind, str) else 0
    except UnicodeEncodeError:
        kind_bytes = MAX_VISUAL_KIND_BYTES + 1
    if (
        not isinstance(visual_object, dict)
        or set(visual_object) != {"content_class", "kind", "reasons"}
        or visual_object.get("content_class") != "visual_object"
        or not isinstance(kind, str)
        or VISUAL_KIND_RE.fullmatch(kind) is None
        or kind_bytes > MAX_VISUAL_KIND_BYTES
    ):
        raise PageCropError(
            "invalid_crop_request", "The visual-object fallback basis is invalid."
        )
    normalized["visual_object"] = {
        "content_class": "visual_object",
        "kind": kind,
        "reasons": _validate_reasons(
            visual_object.get("reasons"), stage="visual-object"
        ),
    }
    return normalized


def _validate_request(request: dict, *, page_reference: dict) -> tuple[dict, dict]:
    if not isinstance(request, dict) or set(request) != {
        "page_number",
        "coordinate_space",
        "bbox",
        "basis",
        "whole_page_visual_object",
    }:
        raise PageCropError(
            "invalid_crop_request", "The crop request schema is invalid."
        )
    if (
        type(request.get("page_number")) is not int
        or request["page_number"] != page_reference["page_number"]
        or request.get("coordinate_space") != COORDINATE_SPACE
        or type(request.get("whole_page_visual_object")) is not bool
    ):
        raise PageCropError(
            "invalid_crop_request", "The crop request identity is invalid."
        )

    bbox = request.get("bbox")
    if not isinstance(bbox, dict) or set(bbox) != {"x0", "y0", "x1", "y1"}:
        raise PageCropError(
            "invalid_crop_request", "The crop bounding box schema is invalid."
        )
    if any(type(bbox.get(key)) is not int for key in ("x0", "y0", "x1", "y1")):
        raise PageCropError(
            "invalid_crop_request", "Crop coordinates must be integers."
        )

    x0, y0, x1, y1 = (bbox[key] for key in ("x0", "y0", "x1", "y1"))
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise PageCropError(
            "invalid_crop_request", "The crop bounding box is empty or invalid."
        )
    if x1 > page_reference["pixel_width"] or y1 > page_reference["pixel_height"]:
        raise PageCropError(
            "invalid_crop_request", "The crop bounding box exceeds the source page."
        )
    output_pixels = (x1 - x0) * (y1 - y0)
    if output_pixels > MAX_PAGE_PIXELS:
        raise PageCropError(
            "crop_limit_exceeded", "The crop exceeds the output pixel limit."
        )

    basis = _validate_basis(request.get("basis"))
    whole_or_near_whole_page = (
        output_pixels * 100 >= page_reference["pixels"] * 95
    )
    whole_page_claim = request["whole_page_visual_object"]
    visual_kind = basis["visual_object"]["kind"]
    if whole_or_near_whole_page:
        if not whole_page_claim or visual_kind != "whole_page":
            raise PageCropError(
                "whole_page_crop_disallowed",
                "A whole or near-whole-page crop requires dedicated evidence.",
            )
    elif whole_page_claim or visual_kind == "whole_page":
        raise PageCropError(
            "invalid_crop_request",
            "Whole-page evidence may only authorize a whole or near-whole-page crop.",
        )

    return dict(bbox), basis


def _validate_output_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise PageCropError(
            "invalid_output_path", "The crop output path is invalid."
        )
    parsed = PurePosixPath(value)
    parts = parsed.parts
    try:
        encoded_length = len(value.encode("utf-8"))
        component_lengths = [len(part.encode("utf-8")) for part in parts]
    except UnicodeEncodeError as exc:
        raise PageCropError(
            "invalid_output_path", "The crop output path is invalid."
        ) from exc
    if (
        parsed.is_absolute()
        or unicodedata.normalize("NFC", value) != value
        or value != parsed.as_posix()
        or len(parts) < 3
        or len(parts) > MAX_PATH_DEPTH
        or parts[:2] != ("04-review", "assets")
        or any(part in {"", ".", ".."} for part in parts)
        or encoded_length > MAX_PATH_BYTES
        or any(length > MAX_PATH_COMPONENT_BYTES for length in component_lengths)
        or parsed.suffix != ".png"
        or parsed.name == ".png"
    ):
        raise PageCropError(
            "invalid_output_path",
            "The crop output must be a canonical PNG path under 04-review/assets.",
        )
    return value


def _open_private_directory(
    name, *, dir_fd: int | None = None
) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise PageCropError(
            "integrity_violation", "The page-reference path is missing or unsafe."
        ) from exc
    opened = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise PageCropError(
            "integrity_violation",
            "The page-reference path is not a private directory.",
        )
    return descriptor, opened


def _recheck_private_directory(
    name,
    *,
    descriptor: int,
    opened: os.stat_result,
    dir_fd: int | None = None,
) -> None:
    try:
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise PageCropError(
            "integrity_violation", "The page-reference path changed while it was read."
        ) from exc
    if (
        (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
        or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISDIR(final.st_mode)
        or stat.S_IMODE(final.st_mode) != 0o700
        or (final.st_mtime_ns, final.st_ctime_ns)
        != (opened.st_mtime_ns, opened.st_ctime_ns)
    ):
        raise PageCropError(
            "integrity_violation", "The page-reference path changed while it was read."
        )


def _read_private_png(name: str, *, dir_fd: int, max_bytes: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PageCropError(
            "integrity_violation", "The page reference is missing or unsafe."
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size <= 0
            or opened.st_size > max_bytes
        ):
            raise PageCropError(
                "integrity_violation",
                "The page reference is not a bounded private regular file.",
            )

        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise PageCropError(
                    "integrity_violation", "The page reference exceeds its read limit."
                )

        try:
            final = os.fstat(descriptor)
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise PageCropError(
                "integrity_violation",
                "The page reference changed while it was read.",
            ) from exc
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or stat.S_IMODE(final.st_mode) != 0o600
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise PageCropError(
                "integrity_violation", "The page reference changed while it was read."
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_source_page(bundle_root: Path, *, page_reference: dict) -> bytes:
    if not isinstance(bundle_root, Path):
        raise PageCropError(
            "integrity_violation", "The work-bundle root must be a Path."
        )

    root_fd, root_stat = _open_private_directory(bundle_root)
    pages_fd = None
    try:
        pages_fd, pages_stat = _open_private_directory("02-pages", dir_fd=root_fd)
        source = _read_private_png(
            PurePosixPath(page_reference["path"]).name,
            dir_fd=pages_fd,
            max_bytes=_png_byte_limit(page_reference["pixels"]),
        )
        _recheck_private_directory(
            "02-pages", descriptor=pages_fd, opened=pages_stat, dir_fd=root_fd
        )
        _recheck_private_directory(
            bundle_root, descriptor=root_fd, opened=root_stat
        )
        return source
    finally:
        if pages_fd is not None:
            os.close(pages_fd)
        os.close(root_fd)


def _pymupdf_version(fitz) -> str | None:
    for name in ("VersionBind", "__version__"):
        value = getattr(fitz, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def build_lossless_crop(
    *,
    bundle_root: Path,
    page_reference: dict,
    request: dict,
    output_relative_path: str,
) -> dict:
    """Return deterministic crop bytes and journal-ready provenance metadata."""

    reference = _validate_page_reference(page_reference)
    requested_bbox, basis = _validate_request(request, page_reference=reference)
    output_path = _validate_output_relative_path(output_relative_path)
    source_png = _read_source_page(bundle_root, page_reference=reference)

    source_sha256 = hashlib.sha256(source_png).hexdigest()
    if source_sha256 != reference["image_sha256"]:
        raise PageCropError(
            "integrity_violation", "The page reference hash does not match its manifest."
        )
    if _png_dimensions(source_png) != (
        reference["pixel_width"],
        reference["pixel_height"],
    ):
        raise PageCropError(
            "integrity_violation",
            "The page reference dimensions do not match its manifest.",
        )

    try:
        fitz = importlib.import_module("fitz")
    except ImportError as exc:
        raise PageCropError(
            "dependency_missing", "PyMuPDF is required for lossless page cropping."
        ) from exc
    try:
        source = fitz.Pixmap(source_png)
    except Exception as exc:
        raise PageCropError(
            "integrity_violation", "The page reference PNG cannot be decoded."
        ) from exc

    if (
        source.width != reference["pixel_width"]
        or source.height != reference["pixel_height"]
        or source.x != 0
        or source.y != 0
        or source.n != 3
        or bool(source.alpha)
    ):
        raise PageCropError(
            "integrity_violation",
            "The decoded page reference is not the expected lossless RGB image.",
        )

    x0, y0, x1, y1 = (
        requested_bbox[key] for key in ("x0", "y0", "x1", "y1")
    )
    try:
        clip = fitz.IRect(x0, y0, x1, y1)
        cropped = fitz.Pixmap(source, source.width, source.height, clip)
        actual_irect = tuple(cropped.irect)
        output_png = cropped.tobytes("png")
    except Exception as exc:
        raise PageCropError(
            "crop_failed", "The page crop could not be created."
        ) from exc

    actual_bbox = dict(zip(("x0", "y0", "x1", "y1"), actual_irect))
    output_width = x1 - x0
    output_height = y1 - y0
    output_pixels = output_width * output_height
    if (
        actual_bbox != requested_bbox
        or cropped.width != output_width
        or cropped.height != output_height
        or cropped.n != source.n
        or bool(cropped.alpha) != bool(source.alpha)
    ):
        raise PageCropError(
            "crop_failed", "The crop engine changed the requested pixel geometry."
        )
    if not isinstance(output_png, bytes) or len(output_png) > _png_byte_limit(
        output_pixels
    ):
        raise PageCropError(
            "crop_limit_exceeded", "The crop exceeds the output byte limit."
        )
    if _png_dimensions(output_png) != (output_width, output_height):
        raise PageCropError(
            "crop_failed", "The encoded crop dimensions do not match the request."
        )

    output_sha256 = "sha256:" + hashlib.sha256(output_png).hexdigest()
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "output_relative_path": output_path,
        "source_page_number": reference["page_number"],
        "source_path": reference["path"],
        "source_image_sha256_algorithm": "sha256",
        "source_image_sha256": reference["image_sha256"],
        "source_size_bytes": len(source_png),
        "source_pixel_width": reference["pixel_width"],
        "source_pixel_height": reference["pixel_height"],
        "requested_bbox": requested_bbox,
        "actual_bbox": actual_bbox,
        "output_sha256": output_sha256,
        "output_size_bytes": len(output_png),
        "output_pixel_width": output_width,
        "output_pixel_height": output_height,
        "output_pixels": output_pixels,
        "coordinate_space": COORDINATE_SPACE,
        "provenance": {
            "operation": "lossless_png_crop",
            "version": 1,
            "engine": "pymupdf.Pixmap",
            "engine_version": _pymupdf_version(fitz),
            "method": "Pixmap(src, src.width, src.height, IRect(bbox))",
            "source_format": "png",
            "output_format": "png",
            "lossless": True,
            "resized": False,
            "enhanced": False,
            "redrawn": False,
            "whole_page_visual_object": request["whole_page_visual_object"],
            "basis": basis,
        },
    }
    return {"png_bytes": output_png, "metadata": metadata}
