from __future__ import annotations

import difflib
import hashlib
import json
import os
import posixpath
import re
import stat
import unicodedata
from copy import deepcopy
from pathlib import Path, PurePosixPath

import bundle
import markdown_assets
import page_crop


SCHEMA_VERSION = 1
MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_ALT_TEXT_BYTES = 1024
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
CORRECTION_ID_RE = re.compile(r"correction-item-[0-9]{4,}\Z")
ALLOWED_CATEGORIES = frozenset(
    {
        "text",
        "hierarchy",
        "reading_order",
        "tables",
        "formulas",
        "footnotes",
        "links",
        "captions",
        "images",
    }
)
CROP_CATEGORIES = frozenset({"tables", "formulas", "images"})


class CorrectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _utf8_bytes(value) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return None


def load_record_input(path: Path, *, cwd: Path) -> dict:
    candidate = path if path.is_absolute() else cwd / path
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(candidate, follow_symlinks=False)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise CorrectionError(
            "invalid_correction_record", "The correction record cannot be read."
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_RECORD_BYTES
        ):
            raise CorrectionError(
                "invalid_correction_record",
                "The correction record must be a bounded regular file.",
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
                raise CorrectionError(
                    "invalid_correction_record",
                    "The correction record exceeds its size limit.",
                )
        final = os.fstat(descriptor)
        try:
            current = os.stat(candidate, follow_symlinks=False)
        except OSError as exc:
            raise CorrectionError(
                "invalid_correction_record",
                "The correction record changed while it was read.",
            ) from exc
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise CorrectionError(
                "invalid_correction_record",
                "The correction record changed while it was read.",
            )
        try:
            return bundle.decode_json_object(b"".join(chunks))
        except bundle.BundleStateError as exc:
            raise CorrectionError(
                "invalid_correction_record",
                "The correction record is not strict JSON.",
            ) from exc
    finally:
        os.close(descriptor)


def _page_range(value, *, required_pages: set[int]) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"start", "end"}
        or type(value.get("start")) is not int
        or type(value.get("end")) is not int
        or not required_pages
        or value["start"] < 1
        or value["end"] < value["start"]
        or value["end"] > max(required_pages)
    ):
        return False
    return set(range(value["start"], value["end"] + 1)) <= required_pages


def _affected_segment_ids(round_record: dict, finding_id: str) -> set[str]:
    segment_ids = {
        segment["segment_id"]
        for segment in round_record["segments"]
        if any(
            finding_id in check["finding_ids"] for check in segment.get("checks", [])
        )
    }
    for boundary in round_record.get("boundaries", []):
        if finding_id in boundary.get("finding_ids", []):
            segment_ids.update(
                {
                    boundary["before_segment_id"],
                    boundary["after_segment_id"],
                }
            )
    return segment_ids


def _affected_boundary_pairs(
    round_record: dict, segment_ids: set[str]
) -> set[tuple[str, str]]:
    return {
        (boundary["before_segment_id"], boundary["after_segment_id"])
        for boundary in round_record.get("boundaries", [])
        if boundary.get("before_segment_id") in segment_ids
        or boundary.get("after_segment_id") in segment_ids
    }


def _position_offset(text: str, line: int, column: int, *, inclusive_end: bool) -> int:
    lines = text.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        raise CorrectionError(
            "invalid_correction_record", "A Markdown source range is invalid."
        )
    prefix = sum(len(value) for value in lines[: line - 1])
    content = lines[line - 1].rstrip("\r\n")
    maximum = len(content) if inclusive_end else len(content) + 1
    if column < 1 or column > maximum:
        raise CorrectionError(
            "invalid_correction_record", "A Markdown source range is invalid."
        )
    return prefix + column if inclusive_end else prefix + column - 1


def _source_range_bytes(text: str, value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)-([1-9][0-9]*):([1-9][0-9]*)", value)
    if match is None:
        raise CorrectionError(
            "invalid_correction_record", "A Markdown source range is invalid."
        )
    start_line, start_column, end_line, end_column = map(int, match.groups())
    start_character = _position_offset(
        text, start_line, start_column, inclusive_end=False
    )
    lines = text.splitlines(keepends=True)
    if (
        end_line == len(lines) + 1
        and end_column == 1
        and text.endswith(("\n", "\r"))
    ):
        end_character = len(text)
    else:
        end_character = _position_offset(
            text, end_line, end_column, inclusive_end=False
        )
    if end_character < start_character:
        raise CorrectionError(
            "invalid_correction_record", "A Markdown source range is invalid."
        )
    return (
        len(text[:start_character].encode("utf-8")),
        len(text[:end_character].encode("utf-8")),
    )


def _anchor_belongs_to_blocks(
    *,
    start: int,
    end: int,
    block_ids: list[str],
    source_markdown: bytes,
    review_evidence: dict,
) -> bool:
    text = source_markdown.decode("utf-8")
    if unicodedata.normalize("NFC", text) != text:
        raise CorrectionError(
            "invalid_correction_record",
            "Exact correction anchors require NFC source Markdown.",
        )
    blocks = {
        item.get("block_id"): item
        for item in review_evidence.get("markdown_blocks", [])
        if isinstance(item, dict)
    }
    if set(block_ids) - set(blocks):
        return False
    ranges = []
    for block_id in block_ids:
        source_ranges = blocks[block_id].get("source_ranges")
        if not isinstance(source_ranges, list) or not source_ranges:
            return False
        valid_ranges = []
        for value in source_ranges:
            try:
                valid_ranges.append(_source_range_bytes(text, value))
            except CorrectionError:
                continue
        if not valid_ranges:
            return False
        ranges.extend(valid_ranges)
    if start == end:
        return any(range_start <= start <= range_end for range_start, range_end in ranges)
    return any(
        range_start <= start and end <= range_end for range_start, range_end in ranges
    )


def _valid_fallback_stage(value, *, status: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"status", "reasons"}
        and value.get("status") == status
        and isinstance(value.get("reasons"), list)
        and value["reasons"]
        and all(isinstance(reason, str) and reason for reason in value["reasons"])
    )


def _valid_representation(item: dict) -> bool:
    representation = item.get("representation")
    fallback = item.get("fallback")
    replacement = item.get("replacement")
    if representation == "markdown":
        return fallback is None
    if representation == "safe_html":
        return (
            isinstance(fallback, dict)
            and set(fallback) == {"markdown", "html"}
            and _valid_fallback_stage(
                fallback.get("markdown"), status="insufficient"
            )
            and _valid_fallback_stage(fallback.get("html"), status="sufficient")
            and isinstance(replacement, str)
            and "<" in replacement
            and ">" in replacement
        )
    if representation == "lossless_crop":
        crop = item.get("crop")
        alt_text = crop.get("alt_text") if isinstance(crop, dict) else None
        try:
            alt_text_bytes = (
                len(alt_text.encode("utf-8")) if isinstance(alt_text, str) else 0
            )
        except UnicodeEncodeError:
            alt_text_bytes = MAX_ALT_TEXT_BYTES + 1
        return (
            replacement is None
            and isinstance(fallback, dict)
            and set(fallback) == {"markdown", "html", "visual_object"}
            and isinstance(crop, dict)
            and set(crop)
            == {
                "page_number",
                "coordinate_space",
                "bbox",
                "whole_page_visual_object",
                "alt_text",
            }
            and isinstance(alt_text, str)
            and alt_text
            and alt_text == alt_text.strip()
            and unicodedata.normalize("NFC", alt_text) == alt_text
            and alt_text_bytes <= MAX_ALT_TEXT_BYTES
            and not any(ord(character) < 32 or ord(character) == 127 for character in alt_text)
        )
    return False


def _markdown_image_alt(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def apply_corrections(
    payload: dict,
    *,
    review_document: dict,
    review_evidence: dict,
    source_markdown: bytes,
    source_target: dict,
    correction_id: str,
    corrected_path: str,
    bundle_root: Path,
    at: str,
    expected_references=None,
    reference_oracle=None,
) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "corrections"}
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != SCHEMA_VERSION
        or not isinstance(payload.get("corrections"), list)
        or not payload["corrections"]
        or not isinstance(review_document, dict)
        or review_document.get("status") != "correction_required"
        or not isinstance(review_document.get("rounds"), list)
        or not review_document["rounds"]
        or not isinstance(source_markdown, bytes)
        or len(source_markdown) > MAX_MARKDOWN_BYTES
        or not isinstance(source_target, dict)
        or bytes_hash(source_markdown) != "sha256:" + source_target.get("sha256", "")
        or not isinstance(review_evidence, dict)
        or review_evidence.get("target") != source_target
    ):
        raise CorrectionError(
            "invalid_correction_record",
            "The correction record or its reviewed target is invalid.",
        )
    try:
        source_markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorrectionError(
            "invalid_correction_record", "The reviewed Markdown is not valid UTF-8."
        ) from exc

    round_record = review_document["rounds"][-1]
    findings = {
        item.get("finding_id"): item
        for item in round_record.get("findings", [])
        if isinstance(item, dict)
        and item.get("kind") == "difference"
        and item.get("status") == "open"
    }
    if not findings or len(findings) != len(round_record.get("findings", [])):
        raise CorrectionError(
            "invalid_correction_record", "The open difference findings are invalid."
        )
    required_pages = {
        item["page_number"]
        for item in round_record.get("baseline", {}).get("page_references", [])
        if isinstance(item, dict) and type(item.get("page_number")) is int
    }
    if not required_pages:
        raise CorrectionError(
            "invalid_correction_record", "The correction source-page evidence is missing."
        )

    normalized = []
    seen_correction_ids = set()
    seen_finding_ids = set()
    spans = []
    for item in payload["corrections"]:
        anchor = item.get("anchor") if isinstance(item, dict) else None
        source_pages = item.get("source_pages") if isinstance(item, dict) else None
        finding = findings.get(item.get("finding_id")) if isinstance(item, dict) else None
        affected_boundaries = (
            item.get("affected_boundaries") if isinstance(item, dict) else None
        )
        boundary_pairs = (
            {
                (entry.get("before_segment_id"), entry.get("after_segment_id"))
                for entry in affected_boundaries
                if isinstance(entry, dict)
                and set(entry) == {"before_segment_id", "after_segment_id"}
                and all(
                    isinstance(entry.get(key), str) and entry[key]
                    for key in ("before_segment_id", "after_segment_id")
                )
            }
            if isinstance(affected_boundaries, list)
            else set()
        )
        start = anchor.get("start_byte") if isinstance(anchor, dict) else None
        end = anchor.get("end_byte") if isinstance(anchor, dict) else None
        expected_text = anchor.get("expected_text") if isinstance(anchor, dict) else None
        expected_bytes = _utf8_bytes(expected_text)
        replacement_bytes = _utf8_bytes(item.get("replacement")) if isinstance(item, dict) else None
        actual_bytes = (
            source_markdown[start:end]
            if type(start) is int
            and type(end) is int
            and 0 <= start <= end <= len(source_markdown)
            else None
        )
        segment_ids = (
            _affected_segment_ids(round_record, item.get("finding_id"))
            if isinstance(item, dict)
            else set()
        )
        expected_boundaries = _affected_boundary_pairs(round_record, segment_ids)
        expected_item_keys = {
            "correction_id",
            "finding_id",
            "category",
            "source_pages",
            "markdown_blocks",
            "review_segment_ids",
            "affected_boundaries",
            "anchor",
            "replacement",
            "basis",
            "representation",
            "fallback",
        }
        if isinstance(item, dict) and item.get("representation") == "lossless_crop":
            expected_item_keys.add("crop")
        if (
            not isinstance(item, dict)
            or set(item) != expected_item_keys
            or not isinstance(item.get("correction_id"), str)
            or CORRECTION_ID_RE.fullmatch(item["correction_id"]) is None
            or item["correction_id"] in seen_correction_ids
            or not isinstance(item.get("finding_id"), str)
            or item["finding_id"] in seen_finding_ids
            or not isinstance(finding, dict)
            or item.get("category") not in ALLOWED_CATEGORIES
            or item.get("category") != finding.get("category")
            or (
                item.get("representation") == "lossless_crop"
                and item.get("category") not in CROP_CATEGORIES
            )
            or (
                isinstance(finding.get("selected_content"), str)
                and item.get("replacement") != finding["selected_content"]
            )
            or not _page_range(source_pages, required_pages=required_pages)
            or source_pages != finding.get("source_pages")
            or not isinstance(item.get("markdown_blocks"), list)
            or not item["markdown_blocks"]
            or len(set(item["markdown_blocks"])) != len(item["markdown_blocks"])
            or item["markdown_blocks"] != finding.get("markdown_blocks")
            or not isinstance(item.get("review_segment_ids"), list)
            or set(item["review_segment_ids"]) != segment_ids
            or len(item["review_segment_ids"]) != len(segment_ids)
            or len(boundary_pairs) != len(affected_boundaries or [])
            or boundary_pairs != expected_boundaries
            or not isinstance(anchor, dict)
            or set(anchor)
            != {"start_byte", "end_byte", "expected_text", "expected_sha256"}
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
            or end > len(source_markdown)
            or not isinstance(expected_text, str)
            or not isinstance(anchor.get("expected_sha256"), str)
            or SHA256_RE.fullmatch(anchor["expected_sha256"]) is None
            or actual_bytes != expected_bytes
            or bytes_hash(actual_bytes or b"") != anchor.get("expected_sha256")
            or not _anchor_belongs_to_blocks(
                start=start if type(start) is int else -1,
                end=end if type(end) is int else -1,
                block_ids=item.get("markdown_blocks", []),
                source_markdown=source_markdown,
                review_evidence=review_evidence,
            )
            or (
                item.get("representation") != "lossless_crop"
                and replacement_bytes is None
            )
            or (
                replacement_bytes is not None and replacement_bytes == actual_bytes
            )
            or not isinstance(item.get("basis"), list)
            or not item["basis"]
            or not all(isinstance(value, str) and value for value in item["basis"])
            or not _valid_representation(item)
        ):
            raise CorrectionError(
                "invalid_correction_record",
                "A correction is unbound, unsupported, or lacks exact source evidence.",
            )
        try:
            source_markdown[:start].decode("utf-8")
            actual_bytes.decode("utf-8")
            source_markdown[end:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorrectionError(
                "invalid_correction_record",
                "A correction anchor splits a UTF-8 character.",
            ) from exc
        seen_correction_ids.add(item["correction_id"])
        seen_finding_ids.add(item["finding_id"])
        spans.append((start, end, item["correction_id"]))
        normalized.append(deepcopy(item))

    if seen_finding_ids != set(findings):
        raise CorrectionError(
            "invalid_correction_record",
            "Every open difference finding must be corrected exactly once.",
        )
    ordered_spans = sorted(spans)
    for previous, current in zip(ordered_spans, ordered_spans[1:]):
        if current[0] < previous[1] or (
            current[0] == previous[0] and current[1] == previous[1]
        ):
            raise CorrectionError(
                "invalid_correction_record", "Correction anchors must not overlap."
            )

    page_references = {
        item.get("page_number"): item
        for item in review_evidence.get("baseline", {}).get("page_references", [])
        if isinstance(item, dict) and type(item.get("page_number")) is int
    }
    generated_replacements = {}
    crop_artifacts = {}
    crop_records = []
    registered_assets = {}
    recorded_crop_replacements = {}
    for item in normalized:
        if item["representation"] != "lossless_crop":
            generated_replacements[item["correction_id"]] = item["replacement"]
            continue
        crop = item["crop"]
        page_number = crop["page_number"]
        page_reference = page_references.get(page_number)
        if (
            page_reference is None
            or not item["source_pages"]["start"]
            <= page_number
            <= item["source_pages"]["end"]
        ):
            raise CorrectionError(
                "invalid_crop_request",
                "The crop page is not bound to the correction source-page evidence.",
            )
        asset_name = f"{correction_id}-{item['correction_id']}.png"
        bundle_path = f"04-review/assets/{asset_name}"
        request = {
            key: deepcopy(crop[key])
            for key in (
                "page_number",
                "coordinate_space",
                "bbox",
                "whole_page_visual_object",
            )
        }
        request["basis"] = deepcopy(item["fallback"])
        try:
            built_crop = page_crop.build_lossless_crop(
                bundle_root=bundle_root,
                page_reference=page_reference,
                request=request,
                output_relative_path=bundle_path,
            )
        except page_crop.PageCropError as exc:
            raise CorrectionError(exc.code, exc.message) from None
        relative_target = f"assets/{asset_name}"
        source_relative_target = posixpath.relpath(
            bundle_path,
            start=PurePosixPath(source_target["path"]).parent.as_posix(),
        )
        generated_replacements[item["correction_id"]] = (
            f"![{_markdown_image_alt(crop['alt_text'])}]({source_relative_target})"
        )
        recorded_crop_replacements[item["correction_id"]] = (
            f"![{_markdown_image_alt(crop['alt_text'])}]({relative_target})"
        )
        crop_artifacts[relative_target] = built_crop["png_bytes"]
        crop_record = deepcopy(built_crop["metadata"])
        crop_record.update(
            {
                "correction_item_id": item["correction_id"],
                "finding_id": item["finding_id"],
                "alt_text": crop["alt_text"],
            }
        )
        crop_records.append(crop_record)
        registered_assets[bundle_path] = {
            "data": built_crop["png_bytes"],
            "provenance": {
                "kind": "lossless_crop",
                "correction_id": correction_id,
                "correction_item_id": item["correction_id"],
                "finding_id": item["finding_id"],
                "source_page_number": crop_record["source_page_number"],
                "source_image_sha256": crop_record["source_image_sha256"],
                "requested_bbox": deepcopy(crop_record["requested_bbox"]),
                "output_sha256": crop_record["output_sha256"],
            },
        }

    corrected = source_markdown
    by_id = {item["correction_id"]: item for item in normalized}
    for start, end, correction_item_id in sorted(spans, reverse=True):
        replacement = generated_replacements[correction_item_id].encode("utf-8")
        corrected = corrected[:start] + replacement + corrected[end:]
    if len(corrected) > MAX_MARKDOWN_BYTES:
        raise CorrectionError(
            "invalid_correction_record", "The corrected Markdown exceeds its size limit."
        )
    try:
        corrected.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorrectionError(
            "invalid_correction_record", "The corrected Markdown is not valid UTF-8."
        ) from exc

    try:
        if expected_references is None:
            if not callable(reference_oracle):
                raise CorrectionError(
                    "invalid_correction_record",
                    "A Pandoc resource-reference oracle is required for correction.",
                )
            expected_references = reference_oracle(corrected)
        elif reference_oracle is not None:
            raise CorrectionError(
                "invalid_correction_record",
                "Correction cannot use two resource-reference oracle sources.",
            )
        rebased = markdown_assets.rebase_local_references(
            corrected,
            bundle_root=bundle_root,
            source_markdown_path=source_target["path"],
            destination_markdown_path=corrected_path,
            expected_references=expected_references,
            authorized_references=source_target.get("local_resources", {}).get(
                "references"
            ),
            registered_assets=registered_assets,
        )
    except markdown_assets.MarkdownAssetError as exc:
        raise CorrectionError(exc.code, exc.message) from None
    corrected = rebased["rewritten_markdown"]
    if len(corrected) > MAX_MARKDOWN_BYTES:
        raise CorrectionError(
            "invalid_correction_record", "The corrected Markdown exceeds its size limit."
        )

    original_lines = source_markdown.decode("utf-8").splitlines(keepends=True)
    corrected_lines = corrected.decode("utf-8").splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            original_lines,
            corrected_lines,
            fromfile=source_target["path"],
            tofile=corrected_path,
        )
    ).encode("utf-8")
    if not diff:
        raise CorrectionError(
            "invalid_correction_record", "The correction does not change the Markdown."
        )
    recorded_items = []
    for item in normalized:
        recorded = deepcopy(item)
        recorded["original_content"] = item["anchor"]["expected_text"]
        corrected_content = recorded_crop_replacements.get(
            item["correction_id"], generated_replacements[item["correction_id"]]
        )
        recorded["corrected_content"] = corrected_content
        recorded_items.append(recorded)
    record = {
        "schema_version": SCHEMA_VERSION,
        "correction_id": correction_id,
        "status": "applied_pending_review",
        "source_target": deepcopy(source_target),
        "source_sha256": bytes_hash(source_markdown),
        "corrected_sha256": bytes_hash(corrected),
        "resource_reference_oracle": deepcopy(expected_references),
        "resource_rewrites": deepcopy(rebased["references"]),
        "crops": crop_records,
        "corrections": recorded_items,
        "created_at": at,
    }
    return {
        "payload": {"schema_version": SCHEMA_VERSION, "corrections": normalized},
        "corrected_markdown": corrected,
        "diff": diff,
        "record": record,
        "record_bytes": _json_bytes(record),
        "artifacts": crop_artifacts,
        "resource_reference_oracle": deepcopy(expected_references),
    }
