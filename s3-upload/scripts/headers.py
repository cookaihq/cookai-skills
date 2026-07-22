from __future__ import annotations

import mimetypes
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote_to_bytes


TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
TOKEN_RE = re.compile(TOKEN + r"\Z")
MEDIA_RE = re.compile(rf"{TOKEN}/{TOKEN}\Z")
LANGUAGE_RE = re.compile(r"[A-Za-z0-9-]*\Z")
ATTR_CHAR_RE = re.compile(r"[!#$&+\-.^_`|~0-9A-Za-z]\Z")


class HeaderError(ValueError):
    pass


def _ascii_field(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip(" \t"):
        raise HeaderError(f"{label} must be a non-empty ASCII field value without surrounding whitespace")
    if any(not 0x20 <= ord(character) <= 0x7E for character in value):
        raise HeaderError(f"{label} must contain only ASCII field-value bytes")
    return value


def _split(value: str, delimiter: str) -> List[str]:
    parts: List[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and character == "\\":
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            continue
        if character == delimiter and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        raise HeaderError("unterminated quoted string")
    parts.append(value[start:])
    return parts


def _quoted(value: str) -> bool:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return False
    escaped = False
    for character in value[1:-1]:
        code = ord(character)
        if escaped:
            if not 0x20 <= code <= 0x7E:
                return False
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"' or not 0x20 <= code <= 0x7E:
            return False
    return not escaped


def _token_or_quoted(value: str) -> bool:
    return bool(TOKEN_RE.fullmatch(value) or _quoted(value))


def _parameterized(value: str, label: str, *, media: bool = False) -> Tuple[str, List[Tuple[str, str]]]:
    pieces = _split(value, ";")
    first = pieces[0].strip()
    if (media and not MEDIA_RE.fullmatch(first)) or (not media and not TOKEN_RE.fullmatch(first)):
        raise HeaderError(f"invalid {label}")
    parameters: List[Tuple[str, str]] = []
    seen = set()
    for raw in pieces[1:]:
        item = raw.strip()
        if not item or "=" not in item:
            raise HeaderError(f"invalid {label} parameter")
        name, parameter_value = item.split("=", 1)
        name = name.strip().lower()
        parameter_value = parameter_value.strip()
        if not TOKEN_RE.fullmatch(name) or not parameter_value or name in seen:
            raise HeaderError(f"invalid {label} parameter")
        seen.add(name)
        parameters.append((name, parameter_value))
    return first, parameters


def validate_content_type(value: object) -> str:
    text = _ascii_field(value, "Content-Type")
    _, parameters = _parameterized(text, "Content-Type", media=True)
    if any(not _token_or_quoted(parameter_value) for _, parameter_value in parameters):
        raise HeaderError("invalid Content-Type parameter")
    return text


def validate_cache_control(value: object) -> str:
    text = _ascii_field(value, "Cache-Control")
    for raw in _split(text, ","):
        directive = raw.strip()
        if not directive:
            raise HeaderError("invalid Cache-Control directive")
        if "=" in directive:
            name, parameter = directive.split("=", 1)
            if not TOKEN_RE.fullmatch(name.strip()) or not _token_or_quoted(parameter.strip()):
                raise HeaderError("invalid Cache-Control directive")
        elif not TOKEN_RE.fullmatch(directive):
            raise HeaderError("invalid Cache-Control directive")
    return text


def _extended_filename(value: str) -> None:
    pieces = value.split("'", 2)
    if len(pieces) != 3:
        raise HeaderError("invalid RFC 8187 filename parameter")
    charset, language, encoded = pieces
    canonical_charset = charset.upper()
    if canonical_charset not in {"UTF-8", "ISO-8859-1"} or not LANGUAGE_RE.fullmatch(language) or not encoded:
        raise HeaderError("invalid RFC 8187 filename parameter")
    index = 0
    while index < len(encoded):
        character = encoded[index]
        if character == "%":
            if index + 2 >= len(encoded) or not re.fullmatch(r"[0-9A-Fa-f]{2}", encoded[index + 1:index + 3]):
                raise HeaderError("invalid RFC 8187 percent escape")
            index += 3
        elif ATTR_CHAR_RE.fullmatch(character):
            index += 1
        else:
            raise HeaderError("invalid RFC 8187 encoded value")
    try:
        unquote_to_bytes(encoded).decode("utf-8" if canonical_charset == "UTF-8" else "latin-1")
    except UnicodeDecodeError as exc:
        raise HeaderError("invalid RFC 8187 encoded value") from exc


def validate_content_disposition(value: object) -> str:
    text = _ascii_field(value, "Content-Disposition")
    _, parameters = _parameterized(text, "Content-Disposition")
    for name, parameter_value in parameters:
        if name.endswith("*"):
            if name != "filename*":
                raise HeaderError("unsupported extended Content-Disposition parameter")
            _extended_filename(parameter_value)
        elif not _token_or_quoted(parameter_value):
            raise HeaderError("invalid Content-Disposition parameter")
    return text


def resolve_upload_headers(filename: str, target_cache_control: Optional[str],
                           target_content_disposition: Optional[str],
                           content_type_override: Optional[str],
                           cache_control_override: Optional[str],
                           content_disposition_override: Optional[str]) -> Dict[str, Optional[str]]:
    inferred = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    content_type = validate_content_type(content_type_override or inferred)
    cache_control_value = cache_control_override if cache_control_override is not None else target_cache_control
    content_disposition_value = (
        content_disposition_override
        if content_disposition_override is not None
        else target_content_disposition
    )
    cache_control = None if cache_control_value is None else validate_cache_control(cache_control_value)
    content_disposition = (
        None
        if content_disposition_value is None
        else validate_content_disposition(content_disposition_value)
    )
    return {
        "content_type": content_type,
        "cache_control": cache_control,
        "content_disposition": content_disposition,
    }
