from __future__ import annotations

import hashlib
import html.entities
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import strict_json


SCHEMA_VERSION = 1
MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
MAX_PANDOC_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_PANDOC_VERSION_BYTES = 64 * 1024
PANDOC_READ_CHUNK_BYTES = 64 * 1024
MAX_AST_DEPTH = 256
MAX_AST_VALUES = 1_000_000
MAX_URL_CHARACTERS = 16_384
PANDOC_TIMEOUT_SECONDS = 30
PANDOC_READER = "gfm+sourcepos"
DIALECT = "gfm+github-dollar-math"
PARSER_PROFILE = "pandoc-gfm-sourcepos-v1"
LEXICAL_PROFILE = "gfm-lexical-v1"
HTML_PROFILE = "safe-html-v1"
COORDINATE_SPACE = "pandoc-sourcepos-nfc-v1"

_EXECUTABLE_IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)

_SOURCE_RANGE_RE = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*-[1-9][0-9]*:[1-9][0-9]*$")
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\r\n]*)$")
_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^( {0,3})\[([^\]\r\n]+)\]:[ \t]*(.*)$"
)
_EXPLICIT_REFERENCE_RE = re.compile(
    r"(!?)\[([^\]\r\n]+)\]\[([^\]\r\n]*)\]"
)
_FOOTNOTE_REFERENCE_RE = re.compile(r"\[\^([^\]\r\n]+)\]")
_INLINE_TARGET_RE = re.compile(r"(!?)\[[^\]\r\n]*\]\(")
_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")
_ENTITY_TOKEN_RE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"
)
_HTML_TAG_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_HTML_ATTRIBUTE_NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_.:-]*")

_ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "blockquote",
        "br",
        "caption",
        "code",
        "col",
        "colgroup",
        "del",
        "div",
        "em",
        "figcaption",
        "figure",
        "hr",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_VOID_HTML_TAGS = frozenset({"br", "col", "hr", "img"})
_BLOCK_HTML_TAGS = frozenset(
    {
        "blockquote",
        "caption",
        "colgroup",
        "div",
        "figure",
        "figcaption",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_BLOCK_SPANNING_HTML_TAGS = frozenset({"blockquote", "div", "figure", "figcaption"})
_ALLOWED_HTML_ATTRIBUTES = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start"}),
}
_REQUIRED_HTML_ATTRIBUTES = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt"}),
}
_HTML_CHILDREN = {
    "table": frozenset({"caption", "colgroup", "thead", "tbody", "tfoot"}),
    "colgroup": frozenset({"col"}),
    "thead": frozenset({"tr"}),
    "tbody": frozenset({"tr"}),
    "tfoot": frozenset({"tr"}),
    "tr": frozenset({"th", "td"}),
    "ul": frozenset({"li"}),
    "ol": frozenset({"li"}),
}
_REQUIRED_HTML_PARENT = {
    "caption": frozenset({"table"}),
    "colgroup": frozenset({"table"}),
    "col": frozenset({"colgroup"}),
    "thead": frozenset({"table"}),
    "tbody": frozenset({"table"}),
    "tfoot": frozenset({"table"}),
    "tr": frozenset({"thead", "tbody", "tfoot"}),
    "th": frozenset({"tr"}),
    "td": frozenset({"tr"}),
    "li": frozenset({"ul", "ol"}),
    "figcaption": frozenset({"figure"}),
}
_NUMERIC_HTML_ATTRIBUTES = frozenset(
    {"width", "height", "colspan", "rowspan", "span"}
)
_UNSAFE_HTML_ATTRIBUTE_NAMES = frozenset(
    {"class", "formaction", "srcdoc", "srcset", "style", "target"}
)


class MarkdownStructureError(ValueError):
    def __init__(self, code: str, message: str, *, context=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = {} if context is None else context


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _object_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pandoc_environment(environ: Dict[str, str]) -> Dict[str, str]:
    explicit_path = environ.get("PATH")
    if explicit_path is None:
        path = os.defpath
    elif explicit_path:
        entries = explicit_path.split(os.pathsep) + os.defpath.split(os.pathsep)
        path = os.pathsep.join(dict.fromkeys(entries))
    else:
        path = ""
    child = {"PATH": path}
    for key, value in environ.items():
        if key in {"LANG", "LANGUAGE", "TMPDIR"} or key.startswith("LC_"):
            child[key] = value
    return child


def _identity_values(info) -> Tuple[int, ...]:
    return tuple(getattr(info, field) for field in _EXECUTABLE_IDENTITY_FIELDS)


def _pandoc_executable_identity(pandoc_executable: str) -> Tuple[str, dict]:
    resolved = os.path.realpath(pandoc_executable)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        before = os.stat(resolved, follow_symlinks=False)
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_mode & 0o111 == 0
            or _identity_values(before) != _identity_values(opened)
        ):
            raise OSError("Pandoc is not a stable executable regular file.")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        final = os.fstat(descriptor)
        current = os.stat(resolved, follow_symlinks=False)
        if (
            size != opened.st_size
            or _identity_values(final) != _identity_values(opened)
            or _identity_values(current) != _identity_values(opened)
        ):
            raise OSError("Pandoc changed while its executable identity was read.")
        identity = {
            "resolved_path": resolved,
            **{
                field: getattr(opened, field)
                for field in _EXECUTABLE_IDENTITY_FIELDS
            },
            "sha256": digest.hexdigest(),
        }
        return resolved, identity
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run_pandoc(
    argv: Sequence[str],
    *,
    stdin_bytes: bytes,
    environ: Dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    limit_code: str,
    stdout_limit_message: str,
    stderr_limit_message: str,
) -> dict:
    with tempfile.TemporaryFile() as process_input:
        with tempfile.TemporaryFile() as stdout_sink:
            process_input.write(stdin_bytes)
            process_input.flush()
            process_input.seek(0)
            process = subprocess.Popen(
                list(argv),
                stdin=process_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_pandoc_environment(environ),
                bufsize=0,
                start_new_session=os.name == "posix",
            )
            assert process.stdout is not None
            assert process.stderr is not None

            kill_lock = threading.Lock()
            kill_requested = [False]

            def kill_process_group() -> None:
                with kill_lock:
                    if kill_requested[0]:
                        return
                    kill_requested[0] = True
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        elif process.poll() is None:
                            process.kill()
                    except OSError:
                        pass

            def capture_stream(stream, *, sink, limit: int, state: dict) -> None:
                digest = hashlib.sha256()
                try:
                    while True:
                        chunk = stream.read(PANDOC_READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        state["size"] += len(chunk)
                        if state["size"] > limit:
                            state["exceeded"] = True
                            kill_process_group()
                            break
                        if chunk.strip():
                            state["has_non_whitespace"] = True
                        if sink is not None:
                            sink.write(chunk)
                except OSError as exc:
                    state["error"] = exc
                    kill_process_group()
                finally:
                    state["sha256"] = digest.hexdigest()
                    try:
                        stream.close()
                    except OSError:
                        pass

            stdout_state = {
                "size": 0,
                "exceeded": False,
                "has_non_whitespace": False,
                "sha256": None,
                "error": None,
            }
            stderr_state = {
                "size": 0,
                "exceeded": False,
                "has_non_whitespace": False,
                "sha256": None,
                "error": None,
            }
            stdout_thread = threading.Thread(
                target=capture_stream,
                kwargs={
                    "stream": process.stdout,
                    "sink": stdout_sink,
                    "limit": stdout_limit,
                    "state": stdout_state,
                },
                name="pandoc-stdout-reader",
            )
            stderr_thread = threading.Thread(
                target=capture_stream,
                kwargs={
                    "stream": process.stderr,
                    "sink": None,
                    "limit": stderr_limit,
                    "state": stderr_state,
                },
                name="pandoc-stderr-reader",
            )
            stdout_thread.start()
            stderr_thread.start()
            timed_out = False
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_group()
                returncode = process.wait()

            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                kill_process_group()
                stdout_thread.join()
                stderr_thread.join()

            if stdout_state["exceeded"]:
                raise MarkdownStructureError(limit_code, stdout_limit_message)
            if stderr_state["exceeded"]:
                raise MarkdownStructureError(limit_code, stderr_limit_message)
            stream_error = stdout_state["error"] or stderr_state["error"]
            if stream_error is not None:
                raise OSError(
                    "Pandoc output could not be captured safely."
                ) from stream_error
            if timed_out:
                raise subprocess.TimeoutExpired(list(argv), timeout)

            stdout_sink.flush()
            stdout_size = os.fstat(stdout_sink.fileno()).st_size
            if stdout_size != stdout_state["size"] or stdout_size > stdout_limit:
                raise OSError("Pandoc stdout changed during bounded capture.")
            stdout_sink.seek(0)
            stdout_bytes = stdout_sink.read(stdout_limit + 1)
            if len(stdout_bytes) != stdout_size or len(stdout_bytes) > stdout_limit:
                raise OSError("Pandoc stdout could not be read within its limit.")
            return {
                "returncode": returncode,
                "stdout": stdout_bytes,
                "stderr_sha256": stderr_state["sha256"],
                "stderr_has_non_whitespace": stderr_state[
                    "has_non_whitespace"
                ],
                "stderr_size": stderr_state["size"],
            }


def _deduplicate(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


class _IssueCollector:
    def __init__(self):
        self._issues: List[dict] = []
        self._keys = set()

    def add(
        self,
        code: str,
        message: str,
        *,
        source_ranges: Sequence[str] = (),
        context: Optional[dict] = None,
    ) -> None:
        valid_ranges = tuple(
            item for item in source_ranges if _SOURCE_RANGE_RE.fullmatch(item)
        )
        safe_context = {} if context is None else context
        key = (code, valid_ranges, _object_hash(safe_context))
        if key in self._keys:
            return
        self._keys.add(key)
        self._issues.append(
            {
                "code": code,
                "severity": "error",
                "message": message,
                "source_ranges": list(valid_ranges),
                "context": safe_context,
            }
        )

    def finish(self) -> List[dict]:
        result = []
        for index, issue in enumerate(self._issues, start=1):
            item = dict(issue)
            item["issue_id"] = f"structure-issue-{index:06d}"
            result.append(item)
        return result


class _Coordinates:
    def __init__(self, text: str):
        starts = [0]
        for match in re.finditer("\n", text):
            starts.append(match.end())
        self.text = text
        self.starts = starts

    def point(self, offset: int) -> Tuple[int, int]:
        low = 0
        high = len(self.starts)
        while low + 1 < high:
            middle = (low + high) // 2
            if self.starts[middle] <= offset:
                low = middle
            else:
                high = middle
        return low + 1, offset - self.starts[low] + 1

    def source_range(self, start: int, end: int) -> str:
        start_line, start_column = self.point(max(0, start))
        end_line, end_column = self.point(max(start, end))
        return f"{start_line}:{start_column}-{end_line}:{end_column}"


def _is_escaped(text: str, offset: int) -> bool:
    backslashes = 0
    current = offset - 1
    while current >= 0 and text[current] == "\\":
        backslashes += 1
        current -= 1
    return backslashes % 2 == 1


def _mark(mask: bytearray, start: int, end: int) -> None:
    if end > start:
        mask[start:end] = b"\x01" * (end - start)


def _is_masked(mask: bytearray, start: int, end: Optional[int] = None) -> bool:
    final = start + 1 if end is None else end
    return mask.find(1, start, final) != -1


def _line_records(text: str) -> List[Tuple[int, int, str]]:
    records = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        records.append((offset, offset + len(content), content))
        offset += len(raw)
    if not records or offset < len(text) or text.endswith(("\n", "\r")):
        records.append((offset, len(text), text[offset:].rstrip("\r\n")))
    return records


def _scan_fences(
    text: str, coordinates: _Coordinates, issues: _IssueCollector
) -> bytearray:
    mask = bytearray(len(text))
    opened = None
    for start, end, line in _line_records(text):
        if opened is None:
            match = _FENCE_OPEN_RE.fullmatch(line)
            if match is None:
                continue
            marker = match.group(2)
            info = match.group(3)
            if marker[0] == "`" and "`" in info:
                continue
            opened = {
                "character": marker[0],
                "length": len(marker),
                "start": start,
                "range": coordinates.source_range(start, end),
            }
            continue
        stripped = line[0:]
        closing = re.fullmatch(
            r" {0,3}" + re.escape(opened["character"]) + "{" + str(opened["length"]) + r",}[ \t]*",
            stripped,
        )
        if closing is not None:
            _mark(mask, opened["start"], end)
            opened = None
    if opened is not None:
        _mark(mask, opened["start"], len(text))
        issues.add(
            "unclosed_fence",
            "A fenced code block does not have a matching closing fence.",
            source_ranges=[opened["range"]],
        )
    return mask


def _scan_code_spans(
    text: str,
    fence_mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> bytearray:
    mask = bytearray(fence_mask)
    offset = 0
    while offset < len(text):
        if _is_masked(mask, offset) or text[offset] != "`" or _is_escaped(text, offset):
            offset += 1
            continue
        end_run = offset
        while end_run < len(text) and text[end_run] == "`":
            end_run += 1
        run_length = end_run - offset
        search = end_run
        closing = None
        while search < len(text):
            candidate = text.find("`", search)
            if candidate < 0:
                break
            if _is_masked(mask, candidate) or _is_escaped(text, candidate):
                search = candidate + 1
                continue
            candidate_end = candidate
            while candidate_end < len(text) and text[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - candidate == run_length:
                closing = candidate_end
                break
            search = candidate_end
        if closing is None:
            issues.add(
                "unclosed_code_span",
                "An inline code span does not have a matching closing delimiter.",
                source_ranges=[coordinates.source_range(offset, end_run)],
            )
            offset = end_run
            continue
        _mark(mask, offset, closing)
        offset = closing
    return mask


def _normalized_label(value: str) -> str:
    unescaped = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", value)
    return " ".join(unescaped.split()).casefold()


def _extract_definition_destination(value: str) -> Optional[str]:
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith("<"):
        closing = stripped.find(">", 1)
        return None if closing < 0 else stripped[1:closing]
    escaped = False
    depth = 0
    for index, character in enumerate(stripped):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif character.isspace() and depth == 0:
            return stripped[:index]
    return stripped


def _url_problem(value: Any, *, target_kind: str) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARACTERS:
        return "invalid_url"
    candidates = []
    current = unicodedata.normalize("NFKC", value)
    for _unused in range(3):
        if current in candidates:
            break
        candidates.append(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    for candidate in candidates:
        if (
            candidate != candidate.strip()
            or "\\" in candidate
            or any(
                ord(character) == 0x7F
                or unicodedata.category(character) in {"Cc", "Cf"}
                for character in candidate
            )
        ):
            return "unsafe_url_characters"
        try:
            parsed = urlsplit(candidate)
            _port = parsed.port
        except (UnicodeError, ValueError):
            return "invalid_url"
        scheme = parsed.scheme.casefold()
        if candidate.startswith("//") or parsed.netloc and not scheme:
            return "protocol_relative_url"
        allowed_schemes = (
            {"http", "https", "mailto"}
            if target_kind == "link"
            else {"https"}
        )
        if scheme and scheme not in allowed_schemes:
            return "unsafe_url_scheme"
        if parsed.username is not None or parsed.password is not None:
            return "url_userinfo"
        if scheme in {"http", "https"} and not parsed.hostname:
            return "invalid_url"
        if scheme == "mailto" and target_kind != "link":
            return "unsafe_url_scheme"
        if not scheme:
            if candidate.startswith("/"):
                return "absolute_local_path"
            path = PurePosixPath(parsed.path)
            if any(part in {"", ".", ".."} for part in path.parts):
                return "unsafe_relative_path"
            prefix = candidate.split("/", 1)[0]
            if ":" in prefix:
                return "ambiguous_url_scheme"
            if target_kind == "image" and (parsed.query or parsed.fragment):
                return "invalid_local_image_url"
    return None


def _add_unsafe_url_issue(
    issues: _IssueCollector,
    value: Any,
    *,
    target_kind: str,
    source_ranges: Sequence[str],
) -> None:
    problem = _url_problem(value, target_kind=target_kind)
    if problem is None:
        return
    encoded = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else b""
    issues.add(
        "unsafe_url",
        "A Markdown or HTML target uses an unsafe or ambiguous URL.",
        source_ranges=source_ranges,
        context={
            "problem": problem,
            "target_kind": target_kind,
            "value_sha256": _bytes_hash(encoded),
        },
    )


def _scan_references(
    text: str,
    mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    definitions: Dict[str, Tuple[int, str]] = {}
    footnote_definitions: Dict[str, int] = {}
    definition_spans = []
    for match in _REFERENCE_DEFINITION_RE.finditer(text):
        if _is_masked(mask, match.start(), match.end()):
            continue
        label = _normalized_label(match.group(2))
        if not label:
            continue
        definition_spans.append((match.start(), match.end()))
        if label.startswith("^"):
            footnote_label = _normalized_label(label[1:])
            if footnote_label in footnote_definitions:
                issues.add(
                    "duplicate_footnote_definition",
                    "A footnote label is defined more than once.",
                    source_ranges=[coordinates.source_range(match.start(), match.end())],
                )
            else:
                footnote_definitions[footnote_label] = match.start()
            continue
        if label in definitions:
            issues.add(
                "duplicate_reference_definition",
                "A Markdown reference label is defined more than once.",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
            )
        else:
            definitions[label] = (match.start(), match.group(3))
        destination = _extract_definition_destination(match.group(3))
        if destination is None:
            issues.add(
                "invalid_reference_definition",
                "A Markdown reference definition has no complete destination.",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
            )
        else:
            _add_unsafe_url_issue(
                issues,
                destination,
                target_kind="link",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
            )
    for match in _EXPLICIT_REFERENCE_RE.finditer(text):
        if _is_masked(mask, match.start(), match.end()) or _is_escaped(text, match.start()):
            continue
        label = _normalized_label(match.group(3) or match.group(2))
        if label not in definitions:
            issues.add(
                "missing_reference_definition",
                "An explicit Markdown reference has no matching definition.",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
                context={"target_kind": "image" if match.group(1) else "link"},
            )
    for match in _FOOTNOTE_REFERENCE_RE.finditer(text):
        if _is_masked(mask, match.start(), match.end()) or _is_escaped(text, match.start()):
            continue
        if any(start <= match.start() < end for start, end in definition_spans):
            continue
        label = _normalized_label(match.group(1))
        if label not in footnote_definitions:
            issues.add(
                "missing_footnote_definition",
                "A Markdown footnote reference has no matching definition.",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
            )


def _find_closing_parenthesis(text: str, start: int, mask: bytearray) -> Optional[int]:
    depth = 1
    angle = False
    quote = None
    offset = start
    while offset < len(text):
        character = text[offset]
        if _is_masked(mask, offset):
            offset += 1
            continue
        if quote is not None:
            if character == quote and not _is_escaped(text, offset):
                quote = None
            offset += 1
            continue
        if character in {'"', "'"} and depth == 1:
            quote = character
        elif character == "<" and depth == 1:
            angle = True
        elif character == ">" and angle:
            angle = False
        elif not angle and not _is_escaped(text, offset):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return offset
            elif character == "\n" and offset + 1 < len(text) and text[offset + 1] == "\n":
                return None
        offset += 1
    return None


def _scan_inline_targets(
    text: str,
    mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    for match in _INLINE_TARGET_RE.finditer(text):
        if _is_masked(mask, match.start(), match.end()) or _is_escaped(text, match.start()):
            continue
        closing = _find_closing_parenthesis(text, match.end(), mask)
        if closing is None:
            issues.add(
                "unclosed_image_destination" if match.group(1) else "unclosed_link_destination",
                "A Markdown inline target has no matching closing parenthesis.",
                source_ranges=[coordinates.source_range(match.start(), match.end())],
                context={"target_kind": "image" if match.group(1) else "link"},
            )
            continue
        destination = _extract_definition_destination(text[match.end() : closing])
        _add_unsafe_url_issue(
            issues,
            destination,
            target_kind="image" if match.group(1) else "link",
            source_ranges=[coordinates.source_range(match.start(), closing + 1)],
        )


def _looks_like_math_start(text: str, offset: int) -> bool:
    if offset + 1 >= len(text) or text[offset + 1].isspace():
        return False
    following = text[offset + 1 : text.find("\n", offset + 1) if "\n" in text[offset + 1 :] else len(text)]
    if not following:
        return False
    if following[0].isdigit():
        token = following.split(maxsplit=1)[0]
        return any(character in token for character in "\\^_{}=+*/<>")
    return following[0].isalpha() or following[0] in "\\{(^_"


def _scan_math(
    text: str,
    mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    display_open = None
    for start, end, line in _line_records(text):
        if _is_masked(mask, start, max(start + 1, end)):
            continue
        if re.fullmatch(r" {0,3}\$\$[ \t]*", line) is None:
            continue
        if display_open is None:
            display_open = (start, end)
        else:
            _mark(mask, display_open[0], end)
            display_open = None
    if display_open is not None:
        issues.add(
            "unclosed_math_delimiter",
            "A display-math delimiter does not have a matching closing delimiter.",
            source_ranges=[coordinates.source_range(*display_open)],
            context={"delimiter": "display"},
        )
        _mark(mask, display_open[0], len(text))
    offset = 0
    while offset < len(text):
        if _is_masked(mask, offset) or text[offset] != "$" or _is_escaped(text, offset):
            offset += 1
            continue
        run_length = 2 if offset + 1 < len(text) and text[offset + 1] == "$" else 1
        search = offset + run_length
        closing = None
        while search < len(text):
            candidate = text.find("$" * run_length, search)
            if candidate < 0:
                break
            if _is_masked(mask, candidate, candidate + run_length) or _is_escaped(text, candidate):
                search = candidate + run_length
                continue
            if run_length == 1 and (
                candidate == offset + 1
                or text[candidate - 1].isspace()
                or (candidate + 1 < len(text) and text[candidate + 1].isdigit())
            ):
                search = candidate + 1
                continue
            closing = candidate + run_length
            break
        if closing is not None:
            _mark(mask, offset, closing)
            offset = closing
            continue
        if run_length == 2 or _looks_like_math_start(text, offset):
            issues.add(
                "unclosed_math_delimiter",
                "An inline-math delimiter does not have a matching closing delimiter.",
                source_ranges=[coordinates.source_range(offset, offset + run_length)],
                context={"delimiter": "display" if run_length == 2 else "inline"},
            )
        offset += run_length


def _split_pipe_cells(line: str) -> Optional[List[str]]:
    separators = []
    for index, character in enumerate(line):
        if character == "|" and not _is_escaped(line, index):
            separators.append(index)
    if not separators:
        return None
    cells = []
    start = 0
    for separator in separators:
        cells.append(line[start:separator])
        start = separator + 1
    cells.append(line[start:])
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return cells


def _scan_tables(
    text: str,
    fence_mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    lines = _line_records(text)
    index = 1
    while index < len(lines):
        start, end, line = lines[index]
        previous_start, previous_end, previous = lines[index - 1]
        if _is_masked(fence_mask, start, max(start + 1, end)) or _is_masked(
            fence_mask, previous_start, max(previous_start + 1, previous_end)
        ):
            index += 1
            continue
        delimiter_cells = _split_pipe_cells(line)
        header_cells = _split_pipe_cells(previous)
        if delimiter_cells is None or header_cells is None or not delimiter_cells:
            index += 1
            continue
        valid = [
            _TABLE_DELIMITER_CELL_RE.fullmatch(cell.strip()) is not None
            for cell in delimiter_cells
        ]
        if not any(valid):
            index += 1
            continue
        table_range = [coordinates.source_range(previous_start, end)]
        if not all(valid):
            issues.add(
                "invalid_pipe_table",
                "A pipe-table delimiter row is malformed.",
                source_ranges=table_range,
            )
            index += 1
            continue
        expected = len(delimiter_cells)
        if len(header_cells) != expected:
            issues.add(
                "table_column_mismatch",
                "A pipe-table header and delimiter row have different column counts.",
                source_ranges=table_range,
                context={"actual_columns": len(header_cells), "expected_columns": expected},
            )
        body_index = index + 1
        while body_index < len(lines):
            row_start, row_end, row = lines[body_index]
            if not row.strip() or _is_masked(
                fence_mask, row_start, max(row_start + 1, row_end)
            ):
                break
            cells = _split_pipe_cells(row)
            if cells is None:
                break
            if len(cells) != expected:
                issues.add(
                    "table_column_mismatch",
                    "A pipe-table body row has a different column count.",
                    source_ranges=[coordinates.source_range(row_start, row_end)],
                    context={"actual_columns": len(cells), "expected_columns": expected},
                )
            body_index += 1
        index = max(index + 1, body_index)


def _find_html_candidate_end(text: str, start: int) -> Optional[int]:
    if text.startswith("<!--", start):
        end = text.find("-->", start + 4)
        return None if end < 0 else end + 3
    quote = None
    offset = start + 1
    while offset < len(text):
        character = text[offset]
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return offset + 1
        elif character == "\n" and not text.startswith("<!--", start):
            return None
        offset += 1
    return None


def _scan_malformed_html_candidates(
    text: str,
    mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    offset = 0
    while offset < len(text):
        candidate = text.find("<", offset)
        if candidate < 0:
            break
        offset = candidate + 1
        if _is_masked(mask, candidate) or _is_escaped(text, candidate):
            continue
        following = text[candidate + 1 : candidate + 4]
        if not (
            following.startswith("!")
            or following.startswith("?")
            or re.match(r"/?[A-Za-z]", following)
        ):
            continue
        end = _find_html_candidate_end(text, candidate)
        if end is None:
            issues.add(
                "malformed_html",
                "A tag-like HTML fragment cannot be completely tokenized.",
                source_ranges=[coordinates.source_range(candidate, candidate + 1)],
            )
            continue
        offset = end


def _looks_like_autolink(raw: str) -> bool:
    if not raw.startswith("<") or not raw.endswith(">"):
        return False
    content = raw[1:-1]
    return (
        re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]{1,31}:[^ <>]*", content)
        is not None
        or re.fullmatch(r"[^ <>@]+@[^ <>@]+", content) is not None
    )


def _scan_complete_html_tokens(
    text: str,
    mask: bytearray,
    coordinates: _Coordinates,
    issues: _IssueCollector,
) -> None:
    parser = _StrictHtmlParser(issues)
    offset = 0
    while offset < len(text):
        candidate = text.find("<", offset)
        if candidate < 0:
            break
        offset = candidate + 1
        if _is_masked(mask, candidate) or _is_escaped(text, candidate):
            continue
        following = text[candidate + 1 : candidate + 4]
        if not (
            following.startswith("!")
            or following.startswith("?")
            or re.match(r"/?[A-Za-z]", following)
        ):
            continue
        end = _find_html_candidate_end(text, candidate)
        if end is None:
            continue
        raw = text[candidate:end]
        offset = end
        # Pandoc supplies real comments as Raw nodes. Ignoring them here keeps this
        # fallback from treating test or producer block markers as HTML structure.
        if raw.startswith("<!--") or _looks_like_autolink(raw):
            continue
        parser.feed_fragment(
            raw, [coordinates.source_range(candidate, end)]
        )
    parser.finish()


def _lexical_issues(text: str, issues: _IssueCollector) -> None:
    coordinates = _Coordinates(text)
    fence_mask = _scan_fences(text, coordinates, issues)
    code_mask = _scan_code_spans(text, fence_mask, coordinates, issues)
    _scan_references(text, code_mask, coordinates, issues)
    _scan_inline_targets(text, code_mask, coordinates, issues)
    math_mask = bytearray(code_mask)
    _scan_math(text, math_mask, coordinates, issues)
    _scan_tables(text, fence_mask, coordinates, issues)
    _scan_malformed_html_candidates(text, code_mask, coordinates, issues)
    _scan_complete_html_tokens(text, code_mask, coordinates, issues)


def _is_attr(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], list)
        and all(isinstance(item, str) for item in value[1])
        and isinstance(value[2], list)
        and all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in value[2]
        )
    )


def _attr_source_ranges(value: Any) -> List[str]:
    if not _is_attr(value):
        return []
    found = []
    for key, raw in value[2]:
        if key == "data-pos":
            found.extend(part for part in raw.split(";") if _SOURCE_RANGE_RE.fullmatch(part))
    return _deduplicate(found)


def _own_source_ranges(node: dict) -> List[str]:
    node_type = node.get("t")
    content = node.get("c")
    if not isinstance(content, list):
        return []
    candidate = None
    if node_type == "Header" and len(content) >= 2:
        candidate = content[1]
    elif node_type in {"CodeBlock", "Div", "Image", "Link", "Span", "Table"} and content:
        candidate = content[0]
    return _attr_source_ranges(candidate)


def _source_positions(value: Any) -> List[str]:
    found = []
    stack = [value]
    while stack:
        current = stack.pop()
        if _is_attr(current):
            found.extend(_attr_source_ranges(current))
            continue
        if isinstance(current, dict):
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return _deduplicate(found)


def _walk_nodes(value: Any, inherited_ranges: Sequence[str] = ()):
    if isinstance(value, dict):
        own = _own_source_ranges(value)
        current_ranges = own or list(inherited_ranges)
        if isinstance(value.get("t"), str):
            yield value, current_ranges
            content = value.get("c")
            if content is not None:
                yield from _walk_nodes(content, current_ranges)
            return
        for child in value.values():
            yield from _walk_nodes(child, current_ranges)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_nodes(child, inherited_ranges)


@dataclass(frozen=True)
class _Splice:
    values: list


def _generated_wrapper(node: dict) -> bool:
    if node.get("t") not in {"Div", "Span"}:
        return False
    content = node.get("c")
    if not isinstance(content, list) or len(content) != 2 or not _is_attr(content[0]):
        return False
    identifier, classes, attributes = content[0]
    return (
        identifier == ""
        and classes == []
        and any(key == "wrapper" and value == "1" for key, value in attributes)
        and all(key in {"data-pos", "wrapper"} for key, _value in attributes)
    )


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        if _generated_wrapper(value):
            children = _canonicalize(value["c"][1])
            return _Splice(children if isinstance(children, list) else [children])
        return {key: _canonicalize(item) for key, item in value.items()}
    if _is_attr(value):
        return [
            value[0],
            list(value[1]),
            [
                [key, item]
                for key, item in value[2]
                if key not in {"data-pos", "wrapper"}
            ],
        ]
    if isinstance(value, list):
        result = []
        for item in value:
            normalized = _canonicalize(item)
            if isinstance(normalized, _Splice):
                result.extend(normalized.values)
            else:
                result.append(normalized)
        return result
    return value


def _semantic_block(block: dict) -> Any:
    value = _canonicalize(block)
    if isinstance(value, _Splice):
        return value.values
    return value


def _semantic_block_type(block: dict, semantic: Any) -> str:
    if isinstance(semantic, list) and len(semantic) == 1 and isinstance(semantic[0], dict):
        value = semantic[0].get("t")
        return value if isinstance(value, str) else "Unknown"
    if isinstance(semantic, dict) and isinstance(semantic.get("t"), str):
        return semantic["t"]
    value = block.get("t")
    return value if isinstance(value, str) else "Unknown"


def _validate_ast(document: Any) -> Tuple[List[int], dict, List[dict]]:
    if not isinstance(document, dict):
        raise MarkdownStructureError(
            "invalid_pandoc_output", "Pandoc did not return a JSON document."
        )
    api_version = document.get("pandoc-api-version")
    metadata = document.get("meta")
    blocks = document.get("blocks")
    if (
        not isinstance(api_version, list)
        or not api_version
        or not all(type(item) is int and item >= 0 for item in api_version)
        or not isinstance(metadata, dict)
        or not isinstance(blocks, list)
        or not all(isinstance(block, dict) and isinstance(block.get("t"), str) for block in blocks)
    ):
        raise MarkdownStructureError(
            "invalid_pandoc_output", "Pandoc returned an incompatible JSON schema."
        )
    stack = [(document, 0)]
    values = 0
    while stack:
        value, depth = stack.pop()
        values += 1
        if values > MAX_AST_VALUES or depth > MAX_AST_DEPTH:
            raise MarkdownStructureError(
                "pandoc_output_limit", "Pandoc structure data exceeds its safety limits."
            )
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return api_version, metadata, blocks


def _safe_tag_context(tag: str) -> dict:
    return {"tag": tag[:128]}


def _valid_start_tag_syntax(raw: str) -> bool:
    if not raw.startswith("<") or not raw.endswith(">"):
        return False
    name = _HTML_TAG_NAME_RE.match(raw, 1)
    if name is None:
        return False
    offset = name.end()
    final = len(raw) - 1
    while offset < final:
        if raw[offset] == "/" and offset + 1 == final:
            return True
        if not raw[offset].isspace():
            return False
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset == final or raw[offset] == "/" and offset + 1 == final:
            return True
        attribute = _HTML_ATTRIBUTE_NAME_RE.match(raw, offset)
        if attribute is None:
            return False
        offset = attribute.end()
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset >= final or raw[offset] != "=":
            return False
        offset += 1
        while offset < final and raw[offset].isspace():
            offset += 1
        if offset >= final or raw[offset] not in {'"', "'"}:
            return False
        quote = raw[offset]
        offset += 1
        closing = raw.find(quote, offset, final)
        if closing < 0:
            return False
        offset = closing + 1
    return True


def _validate_entity_tokens(
    raw: str, source_ranges: Sequence[str], issues: _IssueCollector
) -> None:
    offset = 0
    while True:
        candidate = raw.find("&", offset)
        if candidate < 0:
            return
        match = _ENTITY_TOKEN_RE.match(raw, candidate)
        if match is None:
            issues.add(
                "invalid_html_entity",
                "An HTML entity is missing a semicolon or has invalid syntax.",
                source_ranges=source_ranges,
            )
            offset = candidate + 1
        else:
            offset = match.end()


@dataclass
class _HtmlFrame:
    tag: str
    source_ranges: Tuple[str, ...]


class _StrictHtmlParser(HTMLParser):
    def __init__(self, issues: _IssueCollector):
        super().__init__(convert_charrefs=False)
        self.issues = issues
        self.stack: List[_HtmlFrame] = []
        self.current_ranges: Tuple[str, ...] = ()

    def feed_fragment(self, raw: str, source_ranges: Sequence[str]) -> None:
        self.current_ranges = tuple(source_ranges)
        _validate_entity_tokens(raw, self.current_ranges, self.issues)
        try:
            self.feed(raw)
        except (AssertionError, ValueError) as exc:
            self.issues.add(
                "malformed_html",
                "An HTML fragment cannot be completely tokenized.",
                source_ranges=self.current_ranges,
                context={"parser_error": type(exc).__name__},
            )

    def _issue(self, code: str, message: str, *, context=None) -> None:
        self.issues.add(
            code,
            message,
            source_ranges=self.current_ranges,
            context={} if context is None else context,
        )

    def _validate_attributes(self, tag: str, attrs, raw: str) -> Dict[str, str]:
        if not _valid_start_tag_syntax(raw):
            self._issue(
                "malformed_html",
                "An HTML start tag uses ambiguous or incomplete attribute syntax.",
                context=_safe_tag_context(tag),
            )
        values = {}
        allowed = _ALLOWED_HTML_ATTRIBUTES.get(tag, frozenset())
        for name, value in attrs:
            normalized = name.casefold()
            if normalized in values:
                self._issue(
                    "duplicate_html_attribute",
                    "An HTML start tag repeats an attribute name.",
                    context={"attribute": normalized[:128], "tag": tag[:128]},
                )
                continue
            values[normalized] = value
            if (
                normalized.startswith("on")
                or normalized in _UNSAFE_HTML_ATTRIBUTE_NAMES
                or normalized not in allowed
                or ":" in normalized
            ):
                self._issue(
                    "unsafe_html_attribute",
                    "An HTML attribute is not present in the safe profile.",
                    context={"attribute": normalized[:128], "tag": tag[:128]},
                )
                continue
            if value is None or any(
                ord(character) == 0x7F
                or unicodedata.category(character) in {"Cc", "Cf"}
                for character in value
            ):
                self._issue(
                    "invalid_html_attribute",
                    "An HTML attribute has no safe literal value.",
                    context={"attribute": normalized[:128], "tag": tag[:128]},
                )
                continue
            if normalized in _NUMERIC_HTML_ATTRIBUTES and (
                re.fullmatch(r"[1-9][0-9]{0,5}", value) is None
                or int(value) > 1000
            ):
                self._issue(
                    "invalid_html_attribute",
                    "A numeric HTML attribute is outside the safe profile.",
                    context={"attribute": normalized, "tag": tag},
                )
            elif normalized == "start" and re.fullmatch(r"-?[0-9]{1,6}", value) is None:
                self._issue(
                    "invalid_html_attribute",
                    "An ordered-list start value is invalid.",
                    context={"attribute": normalized, "tag": tag},
                )
            elif normalized == "scope" and value not in {
                "col",
                "colgroup",
                "row",
                "rowgroup",
            }:
                self._issue(
                    "invalid_html_attribute",
                    "A table-header scope value is invalid.",
                    context={"attribute": normalized, "tag": tag},
                )
        for required in _REQUIRED_HTML_ATTRIBUTES.get(tag, frozenset()):
            if required not in values:
                self._issue(
                    "missing_html_attribute",
                    "A safe HTML element is missing a required attribute.",
                    context={"attribute": required, "tag": tag},
                )
        if isinstance(values.get("href"), str):
            _add_unsafe_url_issue(
                self.issues,
                values["href"],
                target_kind="link",
                source_ranges=self.current_ranges,
            )
        if isinstance(values.get("src"), str):
            _add_unsafe_url_issue(
                self.issues,
                values["src"],
                target_kind="image",
                source_ranges=self.current_ranges,
            )
        return values

    def _validate_content_model(self, tag: str) -> None:
        parent = self.stack[-1].tag if self.stack else None
        required = _REQUIRED_HTML_PARENT.get(tag)
        if required is not None and parent not in required:
            self._issue(
                "invalid_html_content_model",
                "An HTML element does not have its required parent.",
                context={"parent": parent, "tag": tag},
            )
        allowed_children = _HTML_CHILDREN.get(parent)
        if allowed_children is not None and tag not in allowed_children:
            self._issue(
                "invalid_html_content_model",
                "An HTML element is not allowed in its current container.",
                context={"parent": parent, "tag": tag},
            )
        if tag in _BLOCK_HTML_TAGS and any(frame.tag == "p" for frame in self.stack):
            self._issue(
                "invalid_html_content_model",
                "A block HTML element cannot be nested inside a paragraph.",
                context={"parent": parent, "tag": tag},
            )
        if tag == "a" and any(frame.tag == "a" for frame in self.stack):
            self._issue(
                "invalid_html_content_model",
                "HTML links cannot be nested.",
                context={"parent": parent, "tag": tag},
            )
        if parent == "pre" and tag != "code":
            self._issue(
                "invalid_html_content_model",
                "A preformatted HTML container only permits a code child.",
                context={"parent": parent, "tag": tag},
            )

    def _start(self, tag: str, attrs, *, self_closing: bool) -> None:
        normalized = tag.casefold()
        raw = self.get_starttag_text() or ""
        if normalized not in _ALLOWED_HTML_TAGS:
            self._issue(
                "unsafe_html_tag",
                "An HTML element is not present in the safe profile.",
                context=_safe_tag_context(normalized),
            )
        self._validate_attributes(normalized, attrs, raw)
        self._validate_content_model(normalized)
        if self_closing and normalized not in _VOID_HTML_TAGS:
            self._issue(
                "nonvoid_self_closing_html",
                "A non-void HTML element uses ambiguous self-closing syntax.",
                context=_safe_tag_context(normalized),
            )
            return
        if not self_closing and normalized not in _VOID_HTML_TAGS:
            self.stack.append(_HtmlFrame(normalized, self.current_ranges))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag):
        normalized = tag.casefold()
        if normalized in _VOID_HTML_TAGS:
            self._issue(
                "void_html_end_tag",
                "A void HTML element has an explicit end tag.",
                context=_safe_tag_context(normalized),
            )
            return
        if not self.stack:
            self._issue(
                "unmatched_html_end_tag",
                "An HTML end tag has no matching start tag.",
                context=_safe_tag_context(normalized),
            )
            return
        if self.stack[-1].tag == normalized:
            self.stack.pop()
            return
        self._issue(
            "misnested_html",
            "HTML start and end tags are not properly nested.",
            context={"actual": normalized[:128], "expected": self.stack[-1].tag[:128]},
        )
        matching = next(
            (index for index in range(len(self.stack) - 1, -1, -1) if self.stack[index].tag == normalized),
            None,
        )
        if matching is not None:
            del self.stack[matching:]

    def _text(self, data: str) -> None:
        if "<" in data:
            self._issue(
                "malformed_html",
                "A tag-like less-than character was not tokenized as HTML.",
            )
        if any(
            ord(character) == 0x7F
            or unicodedata.category(character) == "Cc" and character not in "\t\r\n"
            for character in data
        ):
            self._issue(
                "invalid_html_text",
                "HTML text contains a forbidden control character.",
            )
        parent = self.stack[-1].tag if self.stack else None
        if parent in _HTML_CHILDREN and data.strip():
            self._issue(
                "invalid_html_content_model",
                "A structural HTML container contains text outside a content cell.",
                context={"parent": parent},
            )

    def handle_data(self, data):
        self._text(data)

    def handle_entityref(self, name):
        if name + ";" not in html.entities.html5:
            self._issue(
                "invalid_html_entity",
                "An HTML named entity is unknown.",
                context={"entity": name[:128]},
            )
        self._text("x")

    def handle_charref(self, name):
        try:
            value = int(name[1:], 16) if name[:1].casefold() == "x" else int(name, 10)
        except ValueError:
            value = -1
        if (
            value <= 0
            or value > 0x10FFFF
            or 0xD800 <= value <= 0xDFFF
            or value & 0xFFFF in {0xFFFE, 0xFFFF}
        ):
            self._issue(
                "invalid_html_entity",
                "An HTML numeric entity does not identify a valid Unicode scalar.",
            )
        self._text("x")

    def handle_comment(self, _data):
        self._issue("unsafe_html_comment", "HTML comments are not present in the safe profile.")

    def handle_decl(self, _decl):
        self._issue("unsafe_html_declaration", "HTML declarations are not present in the safe profile.")

    def unknown_decl(self, _data):
        self._issue("unsafe_html_declaration", "Unknown HTML declarations are not present in the safe profile.")

    def handle_pi(self, _data):
        self._issue("unsafe_html_processing_instruction", "HTML processing instructions are not present in the safe profile.")

    def end_markdown_block(self) -> None:
        for frame in self.stack:
            if frame.tag not in _BLOCK_SPANNING_HTML_TAGS:
                self.issues.add(
                    "html_crosses_markdown_block",
                    "A phrasing or structural HTML element crosses a Markdown block boundary.",
                    source_ranges=frame.source_ranges,
                    context=_safe_tag_context(frame.tag),
                )

    def finish(self) -> None:
        try:
            self.close()
        except (AssertionError, ValueError) as exc:
            self._issue(
                "malformed_html",
                "An HTML fragment cannot be completely tokenized.",
                context={"parser_error": type(exc).__name__},
            )
        for frame in self.stack:
            self.issues.add(
                "unclosed_html_tag",
                "An HTML start tag has no matching end tag.",
                source_ranges=frame.source_ranges,
                context=_safe_tag_context(frame.tag),
            )
        self.stack.clear()


def _ast_issues(blocks: Sequence[dict], issues: _IssueCollector) -> None:
    html_parser = _StrictHtmlParser(issues)
    for block in blocks:
        for node, source_ranges in _walk_nodes(block):
            node_type = node.get("t")
            content = node.get("c")
            if node_type in {"Link", "Image"} and isinstance(content, list) and content:
                target = content[-1]
                value = target[0] if isinstance(target, list) and target else None
                _add_unsafe_url_issue(
                    issues,
                    value,
                    target_kind="image" if node_type == "Image" else "link",
                    source_ranges=source_ranges,
                )
            if node_type in {"RawBlock", "RawInline"}:
                if (
                    not isinstance(content, list)
                    or len(content) != 2
                    or not all(isinstance(item, str) for item in content)
                ):
                    issues.add(
                        "invalid_raw_node",
                        "Pandoc returned an invalid raw-content node.",
                        source_ranges=source_ranges,
                    )
                elif content[0] != "html":
                    issues.add(
                        "unsupported_raw_format",
                        "Raw content outside the safe HTML profile is not supported.",
                        source_ranges=source_ranges,
                        context={"format": content[0][:128]},
                    )
                else:
                    html_parser.feed_fragment(content[1], source_ranges)
        html_parser.end_markdown_block()
    html_parser.finish()


def _pandoc_version(
    pandoc_executable: str, *, environ: Dict[str, str]
) -> str:
    try:
        completed = _run_pandoc(
            [pandoc_executable, "--version"],
            stdin_bytes=b"",
            environ=environ,
            timeout=5,
            stdout_limit=MAX_PANDOC_VERSION_BYTES,
            stderr_limit=MAX_PANDOC_VERSION_BYTES,
            limit_code="dependency_missing",
            stdout_limit_message="Pandoc did not report a usable version.",
            stderr_limit_message="Pandoc version diagnostics exceed their limit.",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc could not be executed."
        ) from exc
    version_output = completed["stdout"]
    if completed["returncode"] != 0 or not version_output:
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc did not report a usable version."
        )
    try:
        first_line = version_output.splitlines()[0].decode("utf-8").strip()
    except UnicodeError as exc:
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc reported an invalid version."
        ) from exc
    if not first_line:
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc did not report a usable version."
        )
    return first_line


def _pandoc_document(
    markdown: bytes, *, pandoc_executable: str, environ: Dict[str, str]
) -> Tuple[dict, dict]:
    try:
        completed = _run_pandoc(
            [pandoc_executable, "--from", PANDOC_READER, "--to", "json"],
            stdin_bytes=markdown,
            environ=environ,
            timeout=PANDOC_TIMEOUT_SECONDS,
            stdout_limit=MAX_PANDOC_OUTPUT_BYTES,
            stderr_limit=MAX_PANDOC_OUTPUT_BYTES,
            limit_code="pandoc_output_limit",
            stdout_limit_message="Pandoc structure data exceeds its byte limit.",
            stderr_limit_message="Pandoc diagnostics exceed their byte limit.",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc could not parse the Markdown input."
        ) from exc
    except subprocess.SubprocessError as exc:
        raise MarkdownStructureError(
            "invalid_markdown", "Pandoc could not parse the Markdown input."
        ) from exc
    if completed["returncode"] != 0:
        raise MarkdownStructureError(
            "invalid_markdown",
            "Pandoc rejected the Markdown input.",
            context={"stderr_sha256": completed["stderr_sha256"]},
        )
    try:
        document = strict_json.loads(completed["stdout"])
    except strict_json.StrictJsonError as exc:
        raise MarkdownStructureError(
            "invalid_pandoc_output", "Pandoc returned invalid JSON structure data."
        ) from exc
    return document, completed


def _read_pandoc_identity(
    pandoc_executable: str, *, dependency_changed: bool
) -> Tuple[str, dict]:
    try:
        return _pandoc_executable_identity(pandoc_executable)
    except OSError as exc:
        if dependency_changed:
            raise MarkdownStructureError(
                "dependency_changed",
                "The preflight Pandoc executable is unavailable or has changed.",
            ) from exc
        raise MarkdownStructureError(
            "dependency_missing", "Pandoc could not be inspected safely."
        ) from exc


def _assert_pandoc_identity(
    pandoc_executable: str, expected_identity: dict
) -> None:
    _resolved, current_identity = _read_pandoc_identity(
        pandoc_executable, dependency_changed=True
    )
    if current_identity != expected_identity:
        raise MarkdownStructureError(
            "dependency_changed",
            "The preflight Pandoc executable changed before review completed.",
        )


def inspect_pandoc(
    pandoc_executable: str, *, environ: Dict[str, str]
) -> dict:
    if (
        not isinstance(pandoc_executable, str)
        or not pandoc_executable
        or "\x00" in pandoc_executable
    ):
        raise MarkdownStructureError(
            "dependency_missing", "A Pandoc executable is required."
        )
    if not isinstance(environ, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environ.items()
    ):
        raise MarkdownStructureError(
            "invalid_environment", "The Pandoc environment is invalid."
        )
    resolved_executable, executable_identity = _read_pandoc_identity(
        pandoc_executable, dependency_changed=False
    )
    try:
        version = _pandoc_version(resolved_executable, environ=environ)
    finally:
        _assert_pandoc_identity(resolved_executable, executable_identity)
    try:
        document, _diagnostics = _pandoc_document(
            b"", pandoc_executable=resolved_executable, environ=environ
        )
    finally:
        _assert_pandoc_identity(resolved_executable, executable_identity)
    api_version, _metadata, blocks = _validate_ast(document)
    if blocks:
        raise MarkdownStructureError(
            "invalid_pandoc_output",
            "Pandoc returned content while probing an empty Markdown document.",
        )
    return {
        "executable": resolved_executable,
        "version": version,
        "executable_identity": executable_identity,
        "api_version": api_version,
        "reader": PANDOC_READER,
    }


def analyze(
    markdown: bytes,
    *,
    pandoc_executable: str,
    environ: dict,
    expected_version=None,
    expected_executable_identity=None,
) -> dict:
    if not isinstance(markdown, bytes):
        raise MarkdownStructureError(
            "invalid_markdown", "Markdown input must be supplied as bytes."
        )
    if len(markdown) > MAX_MARKDOWN_BYTES:
        raise MarkdownStructureError(
            "markdown_size_limit", "Markdown input exceeds its byte limit."
        )
    if (
        not isinstance(pandoc_executable, str)
        or not pandoc_executable
        or "\x00" in pandoc_executable
    ):
        raise MarkdownStructureError(
            "dependency_missing", "A Pandoc executable is required."
        )
    if not isinstance(environ, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environ.items()
    ):
        raise MarkdownStructureError(
            "invalid_environment", "The Pandoc environment is invalid."
        )
    try:
        text = markdown.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownStructureError(
            "invalid_markdown", "Markdown input is not valid UTF-8."
        ) from exc
    normalized_text = unicodedata.normalize("NFC", text)
    normalized_bytes = normalized_text.encode("utf-8")
    dependency_expected = (
        expected_version is not None or expected_executable_identity is not None
    )
    if expected_version is not None and (
        not isinstance(expected_version, str) or not expected_version
    ):
        raise MarkdownStructureError(
            "dependency_changed", "The preflight Pandoc version snapshot is invalid."
        )
    if expected_executable_identity is not None and not isinstance(
        expected_executable_identity, dict
    ):
        raise MarkdownStructureError(
            "dependency_changed", "The preflight Pandoc identity snapshot is invalid."
        )
    resolved_executable, executable_identity = _read_pandoc_identity(
        pandoc_executable, dependency_changed=dependency_expected
    )
    if (
        expected_executable_identity is not None
        and executable_identity != expected_executable_identity
    ):
        raise MarkdownStructureError(
            "dependency_changed",
            "The preflight Pandoc executable changed before review began.",
        )
    try:
        version = _pandoc_version(resolved_executable, environ=environ)
    finally:
        _assert_pandoc_identity(resolved_executable, executable_identity)
    if expected_version is not None and version != expected_version:
        raise MarkdownStructureError(
            "dependency_changed",
            "The preflight Pandoc version changed before review began.",
        )
    try:
        document, diagnostics = _pandoc_document(
            markdown, pandoc_executable=resolved_executable, environ=environ
        )
    finally:
        _assert_pandoc_identity(resolved_executable, executable_identity)
    api_version, metadata, pandoc_blocks = _validate_ast(document)

    issue_collector = _IssueCollector()
    if diagnostics["stderr_has_non_whitespace"]:
        issue_collector.add(
            "pandoc_warning",
            "Pandoc emitted a warning while parsing the Markdown input.",
            context={"stderr_sha256": diagnostics["stderr_sha256"]},
        )
    _lexical_issues(normalized_text, issue_collector)
    _ast_issues(pandoc_blocks, issue_collector)

    blocks = []
    semantic_blocks = []
    for index, block in enumerate(pandoc_blocks, start=1):
        semantic = _semantic_block(block)
        semantic_blocks.append(semantic)
        blocks.append(
            {
                "block_id": f"block-{index:06d}",
                "ast_type": _semantic_block_type(block, semantic),
                "source_ranges": _source_positions(block),
                "node_sha256": _object_hash({"node": semantic}),
            }
        )
    if not blocks:
        issue_collector.add(
            "empty_markdown",
            "The Markdown input contains no semantic content blocks.",
        )
    boundaries = []
    for index in range(1, len(blocks)):
        before = blocks[index - 1]
        after = blocks[index]
        boundaries.append(
            {
                "boundary_id": f"boundary-{index:06d}",
                "before_block_id": before["block_id"],
                "after_block_id": after["block_id"],
                "boundary_sha256": _object_hash(
                    {
                        "before_node_sha256": before["node_sha256"],
                        "after_node_sha256": after["node_sha256"],
                    }
                ),
            }
        )
    canonical_document = {
        "dialect": DIALECT,
        "meta": _canonicalize(metadata),
        "blocks": semantic_blocks,
    }
    semantic_hash = _object_hash(canonical_document)
    content_index = {
        "schema_version": SCHEMA_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "parser_profile": PARSER_PROFILE,
        "blocks": blocks,
        "boundaries": boundaries,
    }
    content_index_sha256 = _object_hash(content_index)
    issues = issue_collector.finish()
    structure_evidence = {
        "schema_version": SCHEMA_VERSION,
        "dialect": DIALECT,
        "parser_profile": PARSER_PROFILE,
        "lexical_profile": LEXICAL_PROFILE,
        "html_profile": HTML_PROFILE,
        "coordinate_space": COORDINATE_SPACE,
        "source_sha256": _bytes_hash(markdown),
        "normalized_source_sha256": _bytes_hash(normalized_bytes),
        "semantic_hash": semantic_hash,
        "content_index_sha256": content_index_sha256,
        "issues": issues,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not issues else "correction_required",
        "dialect": DIALECT,
        "parser_profile": PARSER_PROFILE,
        "lexical_profile": LEXICAL_PROFILE,
        "html_profile": HTML_PROFILE,
        "coordinate_space": COORDINATE_SPACE,
        "pandoc": {
            "version": version,
            "api_version": api_version,
            "reader": PANDOC_READER,
            "executable_identity": executable_identity,
        },
        "source_sha256": _bytes_hash(markdown),
        "normalized_source_sha256": _bytes_hash(normalized_bytes),
        "semantic_hash": semantic_hash,
        "content_index_sha256": content_index_sha256,
        "structure_evidence_sha256": _object_hash(structure_evidence),
        "blocks": blocks,
        "boundaries": boundaries,
        "issues": issues,
    }
