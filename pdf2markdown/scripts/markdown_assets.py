from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import posixpath
import re
import stat
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote_to_bytes, urlsplit


MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_TARGET_CHARACTERS = 16_384
MAX_REFERENCE_COUNT = 100_000
READ_CHUNK_BYTES = 64 * 1024
SCAN_WORK_FACTOR = 64
SCAN_WORK_BASE = 4_096
MAX_BRACKET_NESTING = 4_096

_ESCAPABLE = frozenset(
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
)
_HTML_NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_.:-]*")
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\r\n]*)$")
_LIST_MARKER_RE = re.compile(r"^( {0,3})(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)")
_BLANK_LINE_RE = re.compile(r"(?:\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class MarkdownAssetError(ValueError):
    def __init__(self, code: str, message: str, *, context=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = {} if context is None else context


@dataclass
class _ScanBudget:
    limit: int
    used: int = 0

    def charge(self, units: int = 1) -> None:
        self.used += max(0, units)
        if self.used > self.limit:
            raise MarkdownAssetError(
                "asset_scan_budget_exceeded",
                "Markdown resource scanning exceeded its deterministic work budget.",
                context={"limit": self.limit, "used": self.used},
            )


@dataclass
class _Occurrence:
    start: int
    end: int
    raw_target: str
    kind: str
    title: Optional[str]
    reference_label: Optional[str] = None
    kind_counts: Dict[str, int] = field(default_factory=dict)


@dataclass
class _Definition:
    normalized_label: str
    original_label: str
    occurrence: _Occurrence
    kind_counts: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Patch:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class _AssetIdentity:
    bundle_path: str
    sha256: str
    size_bytes: int
    media_type: str
    identity: Tuple[int, int]
    metadata: Tuple[int, int, int, int, int]


class _SingleTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: List[Tuple[str, list]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.tags.append((tag.casefold(), attrs))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.tags.append((tag.casefold(), attrs))


def _escaped_mask(text: str) -> bytearray:
    escaped = bytearray(len(text))
    backslashes = 0
    for offset, character in enumerate(text):
        if character == "\\":
            backslashes += 1
            continue
        if backslashes % 2 == 1:
            escaped[offset] = 1
        backslashes = 0
    return escaped


def _mark(mask: bytearray, start: int, end: int) -> None:
    if end > start:
        mask[start:end] = b"\x01" * (end - start)


def _is_masked(mask: bytearray, start: int, end: Optional[int] = None) -> bool:
    final = start + 1 if end is None else end
    return mask.find(1, start, final) != -1


def _line_records(text: str) -> Iterable[Tuple[int, int, str]]:
    offset = 0
    for raw in text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        yield offset, offset + len(content), content
        offset += len(raw)
    if not text or offset < len(text):
        yield offset, len(text), text[offset:].rstrip("\r\n")


def _indent_columns(value: str) -> int:
    columns = 0
    for character in value:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - columns % 4
        else:
            break
    return columns


def _column_width(value: str) -> int:
    columns = 0
    for character in value:
        if character == "\t":
            columns += 4 - columns % 4
        else:
            columns += 1
    return columns


def _blockquote_content_start(line: str) -> int:
    offset = 0
    while offset < len(line):
        marker = offset
        spaces = 0
        while marker < len(line) and line[marker] == " " and spaces < 3:
            marker += 1
            spaces += 1
        if marker >= len(line) or line[marker] != ">":
            break
        marker += 1
        if marker < len(line) and line[marker] in " \t":
            marker += 1
        offset = marker
    return offset


def _code_mask(
    text: str, *, escaped: bytearray, budget: _ScanBudget
) -> bytearray:
    mask = bytearray(len(text))
    opened: Optional[Tuple[str, int, int]] = None
    for start, end, line in _line_records(text):
        budget.charge(end - start + 1)
        if opened is None:
            match = _FENCE_RE.fullmatch(line)
            if match is None:
                continue
            marker = match.group(2)
            if marker[0] == "`" and "`" in match.group(3):
                continue
            opened = (marker[0], len(marker), start)
            continue
        character, length, opened_at = opened
        if re.fullmatch(
            r" {0,3}" + re.escape(character) + "{" + str(length) + r",}[ \t]*",
            line,
        ):
            _mark(mask, opened_at, end)
            opened = None
    if opened is not None:
        _mark(mask, opened[2], len(text))

    previous_blank = True
    in_indented_code = False
    active_list_indent = 0
    for start, end, line in _line_records(text):
        budget.charge(end - start + 1)
        if start < len(mask) and _is_masked(mask, start):
            previous_blank = False
            in_indented_code = False
            active_list_indent = 0
            continue
        content_start = _blockquote_content_start(line)
        content = line[content_start:]
        if content_start:
            active_list_indent = 0
        if not content.strip(" \t"):
            previous_blank = True
            continue
        list_marker = _LIST_MARKER_RE.match(content)
        if list_marker is not None:
            active_list_indent = _column_width(content[: list_marker.end()])
            previous_blank = False
            in_indented_code = False
            continue
        columns = _indent_columns(content)
        if active_list_indent and columns >= active_list_indent:
            columns -= active_list_indent
        elif active_list_indent:
            active_list_indent = 0
        if columns >= 4 and (previous_blank or in_indented_code):
            _mark(mask, start, end)
            in_indented_code = True
        else:
            in_indented_code = False
        previous_blank = False

    offset = 0
    while offset < len(text):
        budget.charge()
        if _is_masked(mask, offset) or text[offset] != "`" or escaped[offset]:
            offset += 1
            continue
        run_end = offset
        while run_end < len(text) and text[run_end] == "`":
            budget.charge()
            run_end += 1
        run_length = run_end - offset
        search = run_end
        closing = None
        while search < len(text):
            candidate = text.find("`", search)
            if candidate < 0:
                budget.charge(len(text) - search)
                break
            budget.charge(candidate - search + 1)
            if _is_masked(mask, candidate) or escaped[candidate]:
                search = candidate + 1
                continue
            candidate_end = candidate
            while candidate_end < len(text) and text[candidate_end] == "`":
                budget.charge()
                candidate_end += 1
            if candidate_end - candidate == run_length:
                closing = candidate_end
                break
            search = candidate_end
        if closing is None:
            offset = run_end
            continue
        _mark(mask, offset, closing)
        offset = closing
    return mask


def _bracket_pairs(
    text: str,
    *,
    mask: bytearray,
    escaped: bytearray,
    budget: _ScanBudget,
) -> Dict[int, int]:
    stack: List[int] = []
    pairs: Dict[int, int] = {}
    for offset, character in enumerate(text):
        budget.charge()
        if _is_masked(mask, offset) or escaped[offset]:
            continue
        if character == "[":
            stack.append(offset)
            if len(stack) > MAX_BRACKET_NESTING:
                raise MarkdownAssetError(
                    "asset_scan_budget_exceeded",
                    "Markdown resource scanning exceeded its bracket nesting budget.",
                    context={
                        "limit": MAX_BRACKET_NESTING,
                        "used": len(stack),
                    },
                )
        elif character == "]" and stack:
            pairs[stack.pop()] = offset
    return pairs


def _markdown_unescape(value: str) -> str:
    result: List[str] = []
    offset = 0
    while offset < len(value):
        if (
            value[offset] == "\\"
            and offset + 1 < len(value)
            and value[offset + 1] in _ESCAPABLE
        ):
            result.append(value[offset + 1])
            offset += 2
        else:
            result.append(value[offset])
            offset += 1
    return html.unescape("".join(result))


def _normalized_label(value: str) -> str:
    return " ".join(_markdown_unescape(value).split()).casefold()


def _parse_title(
    text: str, start: int, limit: int, *, budget: _ScanBudget
) -> Tuple[Optional[str], int]:
    offset = start
    while offset < limit and text[offset].isspace():
        budget.charge()
        offset += 1
    if offset >= limit:
        return None, offset
    opening = text[offset]
    if opening not in {'"', "'", "("}:
        return None, start
    closing = ")" if opening == "(" else opening
    content_start = offset + 1
    offset = content_start
    while offset < limit:
        budget.charge()
        if text[offset] == "\\":
            offset += 2
            continue
        if text[offset] == closing:
            return _markdown_unescape(text[content_start:offset]), offset + 1
        offset += 1
    return None, start


def _parse_inline_target(
    text: str,
    opening_parenthesis: int,
    limit: Optional[int] = None,
    *,
    budget: _ScanBudget,
) -> Optional[Tuple[int, int, Optional[str], int]]:
    limit = len(text) if limit is None else min(limit, len(text))

    def result(
        target_start: int,
        target_end: int,
        title: Optional[str],
        after: int,
    ) -> Optional[Tuple[int, int, Optional[str], int]]:
        if _BLANK_LINE_RE.search(text[opening_parenthesis + 1 : after - 1]):
            return None
        return target_start, target_end, title, after

    offset = opening_parenthesis + 1
    while offset < limit and text[offset].isspace():
        budget.charge()
        offset += 1
    if offset >= limit:
        return None

    if text[offset] == "<":
        target_start = offset + 1
        offset = target_start
        while offset < limit:
            budget.charge()
            if text[offset] == "\\":
                offset += 2
                continue
            if text[offset] == ">":
                target_end = offset
                offset += 1
                break
            if text[offset] in "\r\n<":
                return None
            offset += 1
        else:
            return None
    else:
        target_start = offset
        depth = 0
        while offset < limit:
            budget.charge()
            character = text[offset]
            if character == "\\":
                offset += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    target_end = offset
                    return result(target_start, target_end, None, offset + 1)
                depth -= 1
            elif character.isspace() and depth == 0:
                target_end = offset
                break
            elif character in "\r\n" and depth > 0:
                return None
            offset += 1
        else:
            return None

    whitespace_start = offset
    while offset < limit and text[offset].isspace():
        budget.charge()
        offset += 1
    if offset < limit and text[offset] == ")":
        return result(target_start, target_end, None, offset + 1)
    if offset == whitespace_start:
        return None
    title, after_title = _parse_title(
        text, offset, limit, budget=budget
    )
    if title is None:
        return None
    offset = after_title
    while offset < limit and text[offset].isspace():
        budget.charge()
        offset += 1
    if offset >= limit or text[offset] != ")":
        return None
    return result(target_start, target_end, title, offset + 1)


def _parse_definition_target(
    text: str, start: int, line_end: int, *, budget: _ScanBudget
) -> Optional[Tuple[int, int, Optional[str]]]:
    offset = start
    while offset < line_end and text[offset] in " \t":
        budget.charge()
        offset += 1
    if offset >= line_end:
        return None
    if text[offset] == "<":
        target_start = offset + 1
        offset = target_start
        while offset < line_end:
            budget.charge()
            if text[offset] == "\\":
                offset += 2
                continue
            if text[offset] == ">":
                target_end = offset
                offset += 1
                break
            if text[offset] == "<":
                return None
            offset += 1
        else:
            return None
    else:
        target_start = offset
        depth = 0
        while offset < line_end:
            budget.charge()
            character = text[offset]
            if character == "\\":
                offset += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    return None
                depth -= 1
            elif character in " \t" and depth == 0:
                break
            offset += 1
        if depth != 0:
            return None
        target_end = offset
    if target_start == target_end:
        return None
    title, after_title = _parse_title(
        text, offset, line_end, budget=budget
    )
    if title is None:
        if text[offset:line_end].strip():
            return None
        return target_start, target_end, None
    if text[after_title:line_end].strip():
        return None
    return target_start, target_end, title


def _scan_definitions(
    text: str,
    mask: bytearray,
    *,
    bracket_pairs: Dict[int, int],
    budget: _ScanBudget,
) -> Tuple[Dict[str, _Definition], bytearray]:
    definitions: Dict[str, _Definition] = {}
    definition_mask = bytearray(mask)
    lines = list(_line_records(text))
    for line_index, (line_start, line_end, line) in enumerate(lines):
        budget.charge()
        indent = len(line) - len(line.lstrip(" "))
        if indent > 3:
            continue
        opening = line_start + indent
        if (
            opening >= line_end
            or _is_masked(mask, opening)
            or text[opening] != "["
        ):
            continue
        closing = bracket_pairs.get(opening)
        if closing is None or closing >= line_end or closing + 1 >= line_end:
            continue
        if text[closing + 1] != ":":
            continue
        original_label = text[opening + 1 : closing]
        normalized = _normalized_label(original_label)
        if not normalized or normalized.startswith("^"):
            continue
        parsed = _parse_definition_target(
            text, closing + 2, line_end, budget=budget
        )
        definition_end = line_end
        if (
            parsed is None
            and not text[closing + 2 : line_end].strip()
            and line_index + 1 < len(lines)
        ):
            next_start, next_end, next_line = lines[line_index + 1]
            if next_line.strip(" \t"):
                parsed = _parse_definition_target(
                    text, next_start, next_end, budget=budget
                )
                if parsed is not None:
                    definition_end = next_end
        if parsed is None:
            continue
        if normalized in definitions:
            raise MarkdownAssetError(
                "duplicate_reference_definition",
                "A Markdown reference label is defined more than once.",
            )
        target_start, target_end, title = parsed
        occurrence = _Occurrence(
            start=target_start,
            end=target_end,
            raw_target=text[target_start:target_end],
            kind="markdown_reference_definition",
            title=title,
            reference_label=original_label,
        )
        definition = _Definition(
            normalized_label=normalized,
            original_label=original_label,
            occurrence=occurrence,
        )
        occurrence.kind_counts = definition.kind_counts
        definitions[normalized] = definition
        _mark(definition_mask, line_start, definition_end)
    return definitions, definition_mask


def _scan_markdown_links(
    text: str,
    mask: bytearray,
    definitions: Dict[str, _Definition],
    *,
    bracket_pairs: Dict[int, int],
    escaped: bytearray,
    budget: _ScanBudget,
    start: int = 0,
    end: Optional[int] = None,
    images_only: bool = False,
) -> List[_Occurrence]:
    occurrences: List[_Occurrence] = []
    offset = start
    limit = len(text) if end is None else min(end, len(text))
    while offset < limit:
        budget.charge()
        if _is_masked(mask, offset) or escaped[offset]:
            offset += 1
            continue
        image = text.startswith("![", offset)
        if image:
            opening = offset + 1
        elif not images_only and text[offset] == "[" and not (
            offset > 0 and text[offset - 1] == "!" and not escaped[offset - 1]
        ):
            opening = offset
        else:
            offset += 1
            continue
        closing = bracket_pairs.get(opening)
        if closing is None or closing >= limit:
            offset = opening + 1
            continue
        label_text = text[opening + 1 : closing]
        budget.charge(len(label_text))
        following = closing + 1
        if following < limit and text[following] == "(":
            parsed = _parse_inline_target(
                text, following, limit, budget=budget
            )
            if parsed is None:
                offset = following + 1
                continue
            target_start, target_end, title, after = parsed
            if not image:
                occurrences.extend(
                    _scan_markdown_links(
                        text,
                        mask,
                        definitions,
                        bracket_pairs=bracket_pairs,
                        escaped=escaped,
                        budget=budget,
                        start=opening + 1,
                        end=closing,
                        images_only=True,
                    )
                )
            if target_start != target_end:
                kind = "markdown_inline_image" if image else "markdown_inline_link"
                occurrences.append(
                    _Occurrence(
                        start=target_start,
                        end=target_end,
                        raw_target=text[target_start:target_end],
                        kind=kind,
                        title=title,
                        kind_counts={kind: 1},
                    )
                )
            offset = after
            continue

        normalized = None
        after = following
        if following < limit and text[following] == "[":
            second_closing = bracket_pairs.get(following)
            if second_closing is not None and second_closing < limit:
                explicit_label = text[following + 1 : second_closing]
                normalized = _normalized_label(explicit_label or label_text)
                after = second_closing + 1
        else:
            normalized = _normalized_label(label_text)
        definition = definitions.get(normalized or "")
        if not image:
            occurrences.extend(
                _scan_markdown_links(
                    text,
                    mask,
                    definitions,
                    bracket_pairs=bracket_pairs,
                    escaped=escaped,
                    budget=budget,
                    start=opening + 1,
                    end=closing,
                    images_only=True,
                )
            )
        if definition is not None:
            kind = "markdown_reference_image" if image else "markdown_reference_link"
            definition.kind_counts[kind] = definition.kind_counts.get(kind, 0) + 1
        offset = max(after, closing + 1)
    return occurrences


def _find_html_token_end(
    text: str, start: int, *, budget: _ScanBudget
) -> Optional[int]:
    if text.startswith("<!--", start):
        closing = text.find("-->", start + 4)
        budget.charge(
            len(text) - start if closing < 0 else closing + 3 - start
        )
        return None if closing < 0 else closing + 3
    quote_character = None
    offset = start + 1
    while offset < len(text):
        budget.charge()
        character = text[offset]
        if quote_character is not None:
            if character == quote_character:
                quote_character = None
        elif character in {'"', "'"}:
            quote_character = character
        elif character == ">":
            return offset + 1
        offset += 1
    return None


def _html_attributes(
    raw: str,
) -> Optional[List[Tuple[str, Optional[int], Optional[int], Optional[str]]]]:
    if not raw.startswith("<") or not raw.endswith(">"):
        return None
    offset = 1
    while offset < len(raw) and raw[offset].isspace():
        offset += 1
    if offset < len(raw) and raw[offset] == "/":
        offset += 1
    tag = _HTML_NAME_RE.match(raw, offset)
    if tag is None:
        return None
    offset = tag.end()
    attributes: List[Tuple[str, Optional[int], Optional[int], Optional[str]]] = []
    final = len(raw) - 1
    while offset < final:
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset >= final or raw[offset] == "/" and raw[offset + 1 : final].strip() == "":
            break
        name_match = _HTML_NAME_RE.match(raw, offset)
        if name_match is None:
            return None
        name = name_match.group(0).casefold()
        offset = name_match.end()
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset >= final or raw[offset] != "=":
            attributes.append((name, None, None, None))
            continue
        offset += 1
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset >= final:
            return None
        if raw[offset] in {'"', "'"}:
            quote_character = raw[offset]
            value_start = offset + 1
            value_end = raw.find(quote_character, value_start, final)
            if value_end < 0:
                return None
            value = raw[value_start:value_end]
            offset = value_end + 1
        else:
            value_start = offset
            while offset < final and not raw[offset].isspace() and raw[offset] != ">":
                offset += 1
            value_end = offset
            value = raw[value_start:value_end]
        attributes.append((name, value_start, value_end, value))
    return attributes


def _scan_html_images(
    text: str,
    code_mask: bytearray,
    *,
    escaped: bytearray,
    budget: _ScanBudget,
) -> Tuple[List[_Occurrence], bytearray]:
    occurrences: List[_Occurrence] = []
    html_mask = bytearray(code_mask)
    offset = 0
    while offset < len(text):
        candidate = text.find("<", offset)
        if candidate < 0:
            budget.charge(len(text) - offset)
            break
        budget.charge(candidate - offset + 1)
        offset = candidate + 1
        if _is_masked(code_mask, candidate) or escaped[candidate]:
            continue
        end = _find_html_token_end(text, candidate, budget=budget)
        if end is None:
            if text[candidate : candidate + 5].casefold().startswith("<img"):
                raise MarkdownAssetError(
                    "invalid_html_image",
                    "An HTML image tag cannot be completely tokenized.",
                )
            continue
        raw = text[candidate:end]
        offset = end
        _mark(html_mask, candidate, end)
        if raw.startswith("<!--") or raw.startswith(("<!", "<?", "</")):
            continue
        parser = _SingleTagParser()
        try:
            parser.feed(raw)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise MarkdownAssetError(
                "invalid_html_image", "An HTML tag cannot be safely tokenized."
            ) from exc
        resource_tags = [entry for entry in parser.tags if entry[0] in {"a", "img"}]
        if not resource_tags:
            continue
        if len(resource_tags) != 1 or len(parser.tags) != 1:
            raise MarkdownAssetError(
                "invalid_html_resource", "An HTML resource tag is structurally ambiguous."
            )
        tag_name = resource_tags[0][0]
        attributes = _html_attributes(raw)
        if attributes is None:
            raise MarkdownAssetError(
                "invalid_html_resource", "An HTML resource tag has invalid attributes."
            )
        target_attribute = "src" if tag_name == "img" else "href"
        targets = [entry for entry in attributes if entry[0] == target_attribute]
        if len(targets) > 1:
            raise MarkdownAssetError(
                "invalid_html_resource",
                "An HTML resource tag repeats its target attribute.",
            )
        if not targets or targets[0][1] is None or targets[0][3] is None:
            continue
        titles = [entry for entry in attributes if entry[0] == "title" and entry[3] is not None]
        title = html.unescape(titles[0][3]) if titles else None
        source = targets[0]
        source_start = candidate + int(source[1])
        source_end = candidate + int(source[2])
        kind = "html_image" if tag_name == "img" else "html_link"
        occurrences.append(
            _Occurrence(
                start=source_start,
                end=source_end,
                raw_target=source[3] or "",
                kind=kind,
                title=title,
                kind_counts={kind: 1},
            )
        )
    return occurrences, html_mask


def _clean_document_path(value, *, field_name: str) -> PurePosixPath:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise MarkdownAssetError(
            "invalid_bundle_path", f"{field_name} must be a POSIX bundle-relative path."
        ) from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or "\x00" in raw
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise MarkdownAssetError(
            "invalid_bundle_path", f"{field_name} must be a clean bundle-relative path."
        )
    return PurePosixPath(raw)


def _open_bundle_root(bundle_root: Path) -> Tuple[int, Path]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(bundle_root, follow_symlinks=False)
        descriptor = os.open(bundle_root, flags)
    except OSError as exc:
        raise MarkdownAssetError(
            "invalid_bundle_root", "The work bundle root cannot be opened safely."
        ) from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise MarkdownAssetError(
            "invalid_bundle_root", "The work bundle root is not a stable directory."
        )
    try:
        canonical = bundle_root.resolve(strict=True)
        current = os.stat(bundle_root, follow_symlinks=False)
    except (OSError, RuntimeError) as exc:
        os.close(descriptor)
        raise MarkdownAssetError(
            "invalid_bundle_root", "The work bundle root cannot be resolved safely."
        ) from exc
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(descriptor)
        raise MarkdownAssetError(
            "invalid_bundle_root", "The work bundle root changed while it was opened."
        )
    return descriptor, canonical


def _valid_percent_encoding(value: str) -> bool:
    offset = 0
    while offset < len(value):
        candidate = value.find("%", offset)
        if candidate < 0:
            return True
        if (
            candidate + 2 >= len(value)
            or value[candidate + 1] not in _HEX_DIGITS
            or value[candidate + 2] not in _HEX_DIGITS
        ):
            return False
        offset = candidate + 3
    return True


def _target_components(
    raw_target: str, *, html_target: bool
) -> Optional[Tuple[str, str, Optional[str], Optional[str], str]]:
    semantic = html.unescape(raw_target) if html_target else _markdown_unescape(raw_target)
    if not semantic or len(semantic) > MAX_TARGET_CHARACTERS:
        return None
    try:
        parsed = urlsplit(semantic)
    except (UnicodeError, ValueError) as exc:
        raise MarkdownAssetError(
            "invalid_asset_target", "A local resource target is not a valid URL reference."
        ) from exc
    if parsed.scheme or parsed.netloc or semantic.startswith("//"):
        return None
    path_part = parsed.path
    if not path_part:
        return None
    if path_part.startswith("/"):
        raise MarkdownAssetError(
            "asset_path_escape", "A local resource target is outside the work bundle."
        )
    if not _valid_percent_encoding(path_part):
        raise MarkdownAssetError(
            "invalid_asset_target", "A local resource target has invalid percent encoding."
        )
    try:
        decoded_path = unquote_to_bytes(path_part).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownAssetError(
            "invalid_asset_target", "A local resource path is not valid UTF-8."
        ) from exc
    if (
        not decoded_path
        or "\\" in decoded_path
        or "\x00" in decoded_path
        or any(
            ord(character) == 0x7F
            or unicodedata.category(character) in {"Cc", "Cf"}
            for character in decoded_path
        )
    ):
        raise MarkdownAssetError(
            "invalid_asset_target", "A local resource path contains unsafe characters."
        )
    query_marker = "?" in semantic.split("#", 1)[0]
    fragment_marker = "#" in semantic
    query = parsed.query if query_marker else None
    fragment = parsed.fragment if fragment_marker else None
    path_end = len(semantic)
    if query_marker:
        path_end = semantic.find("?")
    if fragment_marker:
        path_end = min(path_end, semantic.find("#"))
    suffix = semantic[path_end:]
    return decoded_path, path_part, query, fragment, suffix


def _rewrite_target(
    relative_path: str,
    *,
    query: Optional[str],
    fragment: Optional[str],
    html_target: bool,
) -> str:
    rewritten = quote(relative_path, safe="/-._~")
    component_safe = "/?:@-._~!$&'*+,;=%"
    if query is not None:
        rewritten += "?" + quote(query, safe=component_safe)
    if fragment is not None:
        rewritten += "#" + quote(fragment, safe=component_safe)
    return html.escape(rewritten, quote=True) if html_target else rewritten


def _canonical_bundle_path(
    source_parent: PurePosixPath, decoded_path: str
) -> PurePosixPath:
    if decoded_path.endswith("/"):
        raise MarkdownAssetError(
            "unsafe_asset_file",
            "A referenced local resource target does not name a regular file.",
        )
    joined = posixpath.normpath(posixpath.join(source_parent.as_posix(), decoded_path))
    if joined in {"", ".", ".."} or joined.startswith("../") or joined.startswith("/"):
        raise MarkdownAssetError(
            "asset_path_escape", "A local resource target escapes the work bundle."
        )
    candidate = PurePosixPath(joined)
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise MarkdownAssetError(
            "asset_path_escape", "A local resource target escapes the work bundle."
        )
    return candidate


def _asset_error_for_oserror(exc: OSError) -> MarkdownAssetError:
    if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
        return MarkdownAssetError(
            "asset_missing", "A referenced local resource does not exist."
        )
    if exc.errno in {errno.ELOOP, errno.EMLINK}:
        return MarkdownAssetError(
            "unsafe_asset_file", "A referenced local resource uses a symbolic link."
        )
    return MarkdownAssetError(
        "unsafe_asset_file", "A referenced local resource cannot be opened safely."
    )


def _metadata(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _detect_media_type(prefix: bytes) -> str:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def _read_asset(root_fd: int, bundle_path: PurePosixPath) -> _AssetIdentity:
    parts = bundle_path.parts
    descriptors = [os.dup(root_fd)]
    directory_entries: List[Tuple[int, str, int, Tuple[int, int, int, int, int]]] = []
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for component in parts[:-1]:
            try:
                following = os.open(component, directory_flags, dir_fd=descriptors[-1])
            except OSError as exc:
                raise _asset_error_for_oserror(exc) from exc
            opened = os.fstat(following)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(following)
                raise MarkdownAssetError(
                    "unsafe_asset_file",
                    "A referenced local resource has a non-directory parent.",
                )
            directory_entries.append(
                (
                    descriptors[-1],
                    component,
                    following,
                    _metadata(opened),
                )
            )
            descriptors.append(following)

        name = parts[-1]
        try:
            before = os.stat(name, dir_fd=descriptors[-1], follow_symlinks=False)
        except OSError as exc:
            raise _asset_error_for_oserror(exc) from exc
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MarkdownAssetError(
                "unsafe_asset_file",
                "A referenced local resource must be a single-link regular file.",
            )
        if before.st_size > MAX_ASSET_BYTES:
            raise MarkdownAssetError(
                "asset_size_limit", "A referenced local resource exceeds its size limit."
            )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(name, file_flags, dir_fd=descriptors[-1])
        except OSError as exc:
            raise _asset_error_for_oserror(exc) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or _metadata(before) != _metadata(opened)
            ):
                raise MarkdownAssetError(
                    "unsafe_asset_file",
                    "A referenced local resource changed before it was read.",
                )
            digest = hashlib.sha256()
            size = 0
            prefix = bytearray()
            while True:
                chunk = os.read(descriptor, READ_CHUNK_BYTES)
                if not chunk:
                    break
                if len(prefix) < 16:
                    prefix.extend(chunk[: 16 - len(prefix)])
                size += len(chunk)
                if size > MAX_ASSET_BYTES:
                    raise MarkdownAssetError(
                        "asset_size_limit",
                        "A referenced local resource exceeds its size limit.",
                    )
                digest.update(chunk)
            final = os.fstat(descriptor)
            try:
                current_entry = os.stat(
                    name, dir_fd=descriptors[-1], follow_symlinks=False
                )
            except OSError as exc:
                raise MarkdownAssetError(
                    "asset_changed", "A referenced local resource changed while read."
                ) from exc
            if (
                (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
                or (current_entry.st_dev, current_entry.st_ino)
                != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(final.st_mode)
                or not stat.S_ISREG(current_entry.st_mode)
                or final.st_nlink != 1
                or current_entry.st_nlink != 1
                or final.st_size != size
                or _metadata(final) != _metadata(opened)
                or _metadata(current_entry) != _metadata(opened)
            ):
                raise MarkdownAssetError(
                    "asset_changed", "A referenced local resource changed while read."
                )
            for parent_fd, component, child_fd, initial_metadata in directory_entries:
                try:
                    child = os.fstat(child_fd)
                    current_child = os.stat(
                        component, dir_fd=parent_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    raise MarkdownAssetError(
                        "asset_changed",
                        "A referenced local resource parent changed while read.",
                    ) from exc
                if (
                    not stat.S_ISDIR(child.st_mode)
                    or not stat.S_ISDIR(current_child.st_mode)
                    or (child.st_dev, child.st_ino)
                    != (current_child.st_dev, current_child.st_ino)
                    or _metadata(child) != initial_metadata
                    or _metadata(current_child) != initial_metadata
                ):
                    raise MarkdownAssetError(
                        "asset_changed",
                        "A referenced local resource parent changed while read.",
                    )
            return _AssetIdentity(
                bundle_path=bundle_path.as_posix(),
                sha256="sha256:" + digest.hexdigest(),
                size_bytes=size,
                media_type=_detect_media_type(bytes(prefix)),
                identity=(opened.st_dev, opened.st_ino),
                metadata=_metadata(opened),
            )
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _assert_canonical_containment(
    bundle_root: Path, bundle_path: PurePosixPath
) -> None:
    candidate = bundle_root.joinpath(*bundle_path.parts)
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MarkdownAssetError(
            "asset_missing", "A referenced local resource does not exist."
        ) from exc
    try:
        canonical.relative_to(bundle_root)
    except ValueError as exc:
        raise MarkdownAssetError(
            "asset_path_escape", "A local resource target escapes the work bundle."
        ) from exc


def _reference_record(
    occurrence: _Occurrence,
    *,
    rewritten_target: str,
    identity: _AssetIdentity,
    query: Optional[str],
    fragment: Optional[str],
    provenance: dict,
) -> dict:
    kind_counts = dict(occurrence.kind_counts)
    if not kind_counts:
        kind_counts = {occurrence.kind: 1}
    preserved = []
    if occurrence.title is not None:
        preserved.append("title")
    if query is not None:
        preserved.append("query")
    if fragment is not None:
        preserved.append("fragment")
    return {
        "kind": occurrence.kind,
        "kinds": sorted(kind_counts),
        "kind_counts": {key: kind_counts[key] for key in sorted(kind_counts)},
        "count": sum(kind_counts.values()),
        "original_target": occurrence.raw_target,
        "rewritten_target": rewritten_target,
        "bundle_path": identity.bundle_path,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "media_type": identity.media_type,
        "title": occurrence.title,
        "query": query,
        "fragment": fragment,
        "preserved_components": preserved,
        "reference_label": occurrence.reference_label,
        "provenance": provenance,
    }


def _record_key(record: dict) -> tuple:
    return (
        record["kind"],
        tuple(record["kinds"]),
        record["original_target"],
        record["rewritten_target"],
        record["bundle_path"],
        record["sha256"],
        record["size_bytes"],
        record["media_type"],
        record["title"],
        record["query"],
        record["fragment"],
        record["reference_label"],
        json.dumps(record["provenance"], sort_keys=True, separators=(",", ":")),
    )


_ORACLE_KINDS = {
    "markdown_inline_image": "image",
    "markdown_reference_image": "image",
    "markdown_inline_link": "link",
    "markdown_reference_link": "link",
    "html_link": "html_link",
    "html_image": "html_image",
}

_REFERENCE_ROLES = {
    "markdown_inline_image": "image",
    "markdown_reference_image": "image",
    "markdown_inline_link": "link",
    "markdown_reference_link": "link",
    "html_image": "image",
    "html_link": "link",
}


def _local_reference_identity(kind: str, raw_target: str) -> Optional[tuple]:
    components = _target_components(raw_target, html_target=kind.startswith("html_"))
    if components is None:
        return None
    decoded_path, _encoded_path, query, fragment, _suffix = components
    return kind, decoded_path, query, fragment


def _scanner_reference_inventory(occurrences: Sequence[_Occurrence]) -> Counter:
    inventory = Counter()
    for occurrence in occurrences:
        counts = occurrence.kind_counts
        for scanner_kind, count in counts.items():
            oracle_kind = _ORACLE_KINDS.get(scanner_kind)
            if oracle_kind is None or type(count) is not int or count <= 0:
                raise MarkdownAssetError(
                    "asset_reference_mismatch",
                    "The Markdown resource scanner produced an invalid reference inventory.",
                )
            identity = _local_reference_identity(oracle_kind, occurrence.raw_target)
            if identity is not None:
                inventory[identity] += count
    return inventory


def _oracle_reference_inventory(expected_references) -> Counter:
    if (
        not isinstance(expected_references, list)
        or len(expected_references) > MAX_REFERENCE_COUNT
    ):
        raise MarkdownAssetError(
            "asset_reference_mismatch",
            "The Pandoc resource-reference oracle uses an invalid contract.",
        )
    inventory = Counter()
    for reference in expected_references:
        if (
            not isinstance(reference, dict)
            or set(reference) != {"kind", "target"}
            or reference.get("kind")
            not in {"image", "link", "html_image", "html_link"}
            or not isinstance(reference.get("target"), str)
        ):
            raise MarkdownAssetError(
                "asset_reference_mismatch",
                "The Pandoc resource-reference oracle uses an invalid contract.",
            )
        identity = _local_reference_identity(reference["kind"], reference["target"])
        if identity is not None:
            inventory[identity] += 1
    return inventory


def _authorized_reference_inventory(records) -> Counter:
    if not isinstance(records, list) or len(records) > MAX_REFERENCE_COUNT:
        raise MarkdownAssetError(
            "invalid_asset_authorization",
            "The local resource authorization snapshot is invalid.",
        )
    inventory = Counter()
    for record in records:
        kind_counts = record.get("kind_counts") if isinstance(record, dict) else None
        bundle_path = record.get("bundle_path") if isinstance(record, dict) else None
        try:
            clean_path = _clean_document_path(
                bundle_path, field_name="authorized_resource_path"
            ).as_posix()
        except MarkdownAssetError as exc:
            raise MarkdownAssetError(
                "invalid_asset_authorization",
                "The local resource authorization snapshot is invalid.",
            ) from exc
        if not isinstance(kind_counts, dict) or not kind_counts:
            raise MarkdownAssetError(
                "invalid_asset_authorization",
                "The local resource authorization snapshot is invalid.",
            )
        for kind, count in kind_counts.items():
            role = _REFERENCE_ROLES.get(kind)
            if role is None or type(count) is not int or count <= 0:
                raise MarkdownAssetError(
                    "invalid_asset_authorization",
                    "The local resource authorization snapshot is invalid.",
                )
            inventory[(clean_path, role)] += count
    if sum(inventory.values()) > MAX_REFERENCE_COUNT:
        raise MarkdownAssetError(
            "invalid_asset_authorization",
            "The local resource authorization snapshot exceeds its reference limit.",
        )
    return inventory


def _inherited_provenance(records, bundle_path: str) -> dict:
    for record in records or []:
        if isinstance(record, dict) and record.get("bundle_path") == bundle_path:
            provenance = record.get("provenance")
            if isinstance(provenance, dict) and isinstance(
                provenance.get("kind"), str
            ):
                return dict(provenance)
    return {"kind": "raw_reference"}


def _registered_asset_identities(value) -> Dict[str, Tuple[_AssetIdentity, dict]]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_REFERENCE_COUNT:
        raise MarkdownAssetError(
            "invalid_asset_authorization", "Registered crop assets are invalid."
        )
    registered = {}
    for raw_path, entry in value.items():
        try:
            bundle_path = _clean_document_path(
                raw_path, field_name="registered_asset_path"
            )
        except MarkdownAssetError as exc:
            raise MarkdownAssetError(
                "invalid_asset_authorization", "A registered crop path is invalid."
            ) from exc
        data = entry.get("data") if isinstance(entry, dict) else None
        provenance = entry.get("provenance") if isinstance(entry, dict) else None
        if (
            set(entry) != {"data", "provenance"}
            if isinstance(entry, dict)
            else True
        ) or not isinstance(data, bytes) or not isinstance(provenance, dict):
            raise MarkdownAssetError(
                "invalid_asset_authorization", "A registered crop grant is invalid."
            )
        path = bundle_path.as_posix()
        if (
            len(bundle_path.parts) != 3
            or bundle_path.parts[:2] != ("04-review", "assets")
            or not bundle_path.name.endswith(".png")
            or not data
            or len(data) > MAX_ASSET_BYTES
            or provenance.get("kind") != "lossless_crop"
        ):
            raise MarkdownAssetError(
                "invalid_asset_authorization", "A registered crop grant is invalid."
            )
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if provenance.get("output_sha256") != digest:
            raise MarkdownAssetError(
                "invalid_asset_authorization",
                "A registered crop grant does not match its generated bytes.",
            )
        media_type = _detect_media_type(data[:16])
        if media_type != "image/png":
            raise MarkdownAssetError(
                "unsupported_asset_media_type",
                "A registered crop does not contain PNG media.",
            )
        registered[path] = (
            _AssetIdentity(
                bundle_path=path,
                sha256=digest,
                size_bytes=len(data),
                media_type=media_type,
                identity=(0, 0),
                metadata=(stat.S_IFREG | 0o600, 1, len(data), 0, 0),
            ),
            dict(provenance),
        )
    return registered


def rebase_local_references(
    markdown: bytes,
    *,
    bundle_root: Path,
    source_markdown_path: str,
    destination_markdown_path: str,
    expected_references: list[dict],
    allowed_bundle_root: Optional[str] = None,
    authorized_references: Optional[list[dict]] = None,
    registered_assets: Optional[dict] = None,
) -> dict:
    """Rebase local Markdown resources without copying or mutating those files.

    Both document paths are POSIX paths relative to ``bundle_root``. Local targets
    are resolved relative to the source document and rewritten relative to the
    destination document. URI targets and same-document query/fragment targets
    are left byte-for-byte unchanged.
    """

    if not isinstance(markdown, bytes):
        raise MarkdownAssetError(
            "invalid_markdown", "Markdown input must be supplied as bytes."
        )
    if len(markdown) > MAX_MARKDOWN_BYTES:
        raise MarkdownAssetError(
            "markdown_size_limit", "Markdown input exceeds its byte limit."
        )
    try:
        text = markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownAssetError(
            "invalid_markdown", "Markdown input is not valid UTF-8."
        ) from exc
    source_path = _clean_document_path(
        source_markdown_path, field_name="source_markdown_path"
    )
    destination_path = _clean_document_path(
        destination_markdown_path, field_name="destination_markdown_path"
    )
    allowed_root = (
        None
        if allowed_bundle_root is None
        else _clean_document_path(
            allowed_bundle_root, field_name="allowed_bundle_root"
        )
    )
    registered = _registered_asset_identities(registered_assets)
    root_fd, canonical_root = _open_bundle_root(Path(bundle_root))
    try:
        scan_budget = _ScanBudget(
            limit=SCAN_WORK_BASE + len(text) * SCAN_WORK_FACTOR
        )
        escaped = _escaped_mask(text)
        scan_budget.charge(len(text))
        code_mask = _code_mask(text, escaped=escaped, budget=scan_budget)
        html_occurrences, html_mask = _scan_html_images(
            text,
            code_mask,
            escaped=escaped,
            budget=scan_budget,
        )
        bracket_pairs = _bracket_pairs(
            text,
            mask=html_mask,
            escaped=escaped,
            budget=scan_budget,
        )
        definitions, definition_mask = _scan_definitions(
            text,
            html_mask,
            bracket_pairs=bracket_pairs,
            budget=scan_budget,
        )
        inline_occurrences = _scan_markdown_links(
            text,
            definition_mask,
            definitions,
            bracket_pairs=bracket_pairs,
            escaped=escaped,
            budget=scan_budget,
        )
        occurrences = [
            *html_occurrences,
            *inline_occurrences,
            *(
                definition.occurrence
                for definition in definitions.values()
                if definition.kind_counts
            ),
        ]
        occurrences.sort(key=lambda item: (item.start, item.end))
        if len(occurrences) > MAX_REFERENCE_COUNT:
            raise MarkdownAssetError(
                "asset_scan_budget_exceeded",
                "Markdown contains too many local resource spans.",
            )
        scanner_inventory = _scanner_reference_inventory(occurrences)
        oracle_inventory = _oracle_reference_inventory(expected_references)
        if scanner_inventory != oracle_inventory:
            raise MarkdownAssetError(
                "asset_reference_mismatch",
                "Exact Markdown resource spans do not match the Pandoc reference oracle.",
                context={
                    "scanner_count": sum(scanner_inventory.values()),
                    "oracle_count": sum(oracle_inventory.values()),
                },
            )

        patches: List[_Patch] = []
        records: List[dict] = []
        records_by_key: Dict[tuple, dict] = {}
        identities: Dict[str, _AssetIdentity] = {}
        actual_authorized_inventory = Counter()
        authorized_inventory = (
            None
            if authorized_references is None
            else _authorized_reference_inventory(authorized_references)
        )
        if authorized_inventory is not None:
            for path in registered:
                authorized_inventory[(path, "image")] += 1
        source_parent = source_path.parent
        destination_parent = destination_path.parent
        for occurrence in occurrences:
            components = _target_components(
                occurrence.raw_target,
                html_target=occurrence.kind in {"html_image", "html_link"},
            )
            if components is None:
                continue
            decoded_path, _encoded_path, query, fragment, _suffix = components
            bundle_path = _canonical_bundle_path(source_parent, decoded_path)
            if allowed_root is not None and not (
                bundle_path == allowed_root or allowed_root in bundle_path.parents
            ):
                raise MarkdownAssetError(
                    "unauthorized_asset_reference",
                    "A local resource is outside the authorized raw resource tree.",
                )
            for kind, count in occurrence.kind_counts.items():
                role = _REFERENCE_ROLES.get(kind)
                if role is None:
                    raise MarkdownAssetError(
                        "asset_reference_mismatch",
                        "The Markdown resource scanner produced an invalid reference role.",
                    )
                actual_authorized_inventory[(bundle_path.as_posix(), role)] += count
            registered_entry = registered.get(bundle_path.as_posix())
            if registered_entry is not None:
                identity, provenance = registered_entry
            else:
                _assert_canonical_containment(canonical_root, bundle_path)
                identity = identities.get(bundle_path.as_posix())
                if identity is None:
                    identity = _read_asset(root_fd, bundle_path)
                provenance = (
                    {
                        "kind": "raw_reference",
                        "raw_root": allowed_root.as_posix(),
                    }
                    if allowed_root is not None
                    else _inherited_provenance(
                        authorized_references, bundle_path.as_posix()
                    )
                )
            if occurrence.kind in {
                "markdown_inline_image",
                "markdown_reference_image",
                "html_image",
            } and not identity.media_type.startswith("image/"):
                raise MarkdownAssetError(
                    "unsupported_asset_media_type",
                    "A local image reference does not contain a supported image media type.",
                )
            previous = identities.get(identity.bundle_path)
            if previous is not None and (
                previous.identity != identity.identity
                or previous.metadata != identity.metadata
                or previous.sha256 != identity.sha256
                or previous.size_bytes != identity.size_bytes
            ):
                raise MarkdownAssetError(
                    "asset_changed",
                    "A referenced local resource changed during Markdown rebasing.",
                )
            identities[identity.bundle_path] = identity
            relative = posixpath.relpath(
                bundle_path.as_posix(), start=destination_parent.as_posix()
            )
            rewritten_target = _rewrite_target(
                relative,
                query=query,
                fragment=fragment,
                html_target=occurrence.kind in {"html_image", "html_link"},
            )
            patches.append(
                _Patch(
                    start=occurrence.start,
                    end=occurrence.end,
                    replacement=rewritten_target,
                )
            )
            record = _reference_record(
                occurrence,
                rewritten_target=rewritten_target,
                identity=identity,
                query=query,
                fragment=fragment,
                provenance=provenance,
            )
            key = _record_key(record)
            existing = records_by_key.get(key)
            if existing is None:
                records_by_key[key] = record
                records.append(record)
            else:
                for kind, count in record["kind_counts"].items():
                    existing["kind_counts"][kind] = (
                        existing["kind_counts"].get(kind, 0) + count
                    )
                existing["kinds"] = sorted(existing["kind_counts"])
                existing["count"] = sum(existing["kind_counts"].values())

        if authorized_inventory is not None:
            if actual_authorized_inventory - authorized_inventory:
                raise MarkdownAssetError(
                    "unauthorized_asset_reference",
                    "A correction introduces a local resource reference that was not reviewed.",
                )
            if any(
                actual_authorized_inventory[(path, "image")] != 1
                for path in registered
            ):
                raise MarkdownAssetError(
                    "invalid_asset_authorization",
                    "Every registered crop must be referenced exactly once by its correction.",
                )

        for previous, current in zip(patches, patches[1:]):
            if current.start < previous.end:
                raise MarkdownAssetError(
                    "ambiguous_asset_reference",
                    "Local resource target spans overlap.",
                )
        rewritten_parts = []
        cursor = 0
        for patch in patches:
            rewritten_parts.extend(
                (text[cursor : patch.start], patch.replacement)
            )
            cursor = patch.end
        rewritten_parts.append(text[cursor:])
        rewritten = "".join(rewritten_parts)
        rewritten_bytes = rewritten.encode("utf-8")
        if len(rewritten_bytes) > MAX_MARKDOWN_BYTES:
            raise MarkdownAssetError(
                "markdown_size_limit", "Rebased Markdown exceeds its byte limit."
            )
        return {
            "rewritten_markdown": rewritten_bytes,
            "references": records,
            "scan_work_units": scan_budget.used,
            "scan_work_limit": scan_budget.limit,
        }
    finally:
        os.close(root_fd)


def _resource_fingerprint(records) -> Counter:
    if not isinstance(records, list) or len(records) > MAX_REFERENCE_COUNT:
        raise MarkdownAssetError(
            "invalid_asset_snapshot", "The local resource snapshot is invalid."
        )
    fingerprint = Counter()
    for record in records:
        if not isinstance(record, dict):
            raise MarkdownAssetError(
                "invalid_asset_snapshot", "The local resource snapshot is invalid."
            )
        kind_counts = record.get("kind_counts")
        provenance = record.get("provenance")
        if (
            not isinstance(record.get("bundle_path"), str)
            or not isinstance(record.get("sha256"), str)
            or not isinstance(record.get("size_bytes"), int)
            or not isinstance(record.get("media_type"), str)
            or not isinstance(kind_counts, dict)
            or not isinstance(provenance, dict)
        ):
            raise MarkdownAssetError(
                "invalid_asset_snapshot", "The local resource snapshot is invalid."
            )
        provenance_key = json.dumps(
            provenance, sort_keys=True, separators=(",", ":")
        )
        for kind, count in kind_counts.items():
            role = _REFERENCE_ROLES.get(kind)
            if role is None or type(count) is not int or count <= 0:
                raise MarkdownAssetError(
                    "invalid_asset_snapshot", "The local resource snapshot is invalid."
                )
            fingerprint[
                (
                    record["bundle_path"],
                    role,
                    record["sha256"],
                    record["size_bytes"],
                    record["media_type"],
                    provenance_key,
                )
            ] += count
    return fingerprint


def validate_local_reference_snapshot(
    markdown: bytes,
    *,
    bundle_root: Path,
    snapshot: dict,
    raw_root: str,
    crop_assets: dict,
) -> dict:
    """Revalidate a durable local-resource snapshot against current filesystem state."""

    if (
        not isinstance(snapshot, dict)
        or set(snapshot)
        != {
            "schema_version",
            "markdown_path",
            "markdown_sha256",
            "oracle",
            "reference_count",
            "references",
        }
        or snapshot.get("schema_version") != 1
        or not isinstance(snapshot.get("markdown_path"), str)
        or snapshot.get("markdown_sha256")
        != "sha256:" + hashlib.sha256(markdown).hexdigest()
        or type(snapshot.get("reference_count")) is not int
        or snapshot["reference_count"] < 0
        or not isinstance(crop_assets, dict)
    ):
        raise MarkdownAssetError(
            "invalid_asset_snapshot", "The local resource snapshot is invalid."
        )
    clean_raw_root = _clean_document_path(
        raw_root, field_name="raw_resource_root"
    )
    expected_fingerprint = _resource_fingerprint(snapshot["references"])
    if sum(expected_fingerprint.values()) != snapshot["reference_count"]:
        raise MarkdownAssetError(
            "invalid_asset_snapshot",
            "The local resource snapshot reference count is inconsistent.",
        )
    for record in snapshot["references"]:
        bundle_path = _clean_document_path(
            record["bundle_path"], field_name="snapshot_resource_path"
        )
        provenance = record["provenance"]
        if provenance.get("kind") == "raw_reference":
            if provenance.get("raw_root") != clean_raw_root.as_posix() or not (
                bundle_path == clean_raw_root
                or clean_raw_root in bundle_path.parents
            ):
                raise MarkdownAssetError(
                    "unauthorized_asset_reference",
                    "A snapshotted resource is outside the active raw resource tree.",
                )
        elif provenance.get("kind") == "lossless_crop":
            authorized = crop_assets.get(bundle_path.as_posix())
            if (
                not isinstance(authorized, dict)
                or authorized.get("correction_id")
                != provenance.get("correction_id")
                or authorized.get("sha256") != record.get("sha256")
                or authorized.get("size_bytes") != record.get("size_bytes")
            ):
                raise MarkdownAssetError(
                    "unauthorized_asset_reference",
                    "A snapshotted crop lacks committed correction provenance.",
                )
        else:
            raise MarkdownAssetError(
                "invalid_asset_snapshot",
                "A snapshotted resource uses an unknown provenance kind.",
            )

    current = rebase_local_references(
        markdown,
        bundle_root=bundle_root,
        source_markdown_path=snapshot["markdown_path"],
        destination_markdown_path=snapshot["markdown_path"],
        expected_references=snapshot["oracle"],
        authorized_references=snapshot["references"],
    )
    current_fingerprint = _resource_fingerprint(current["references"])
    if (
        current_fingerprint != expected_fingerprint
        or sum(current_fingerprint.values()) != snapshot["reference_count"]
    ):
        raise MarkdownAssetError(
            "asset_snapshot_mismatch",
            "A local resource no longer matches its reviewed snapshot.",
        )
    return {
        "reference_count": snapshot["reference_count"],
        "scan_work_units": current["scan_work_units"],
        "scan_work_limit": current["scan_work_limit"],
    }
