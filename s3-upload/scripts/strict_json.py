from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Tuple


MAX_SAFE_INTEGER = (1 << 53) - 1


class StrictJSONError(ValueError):
    pass


def _object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> Any:
    raise StrictJSONError("floating-point JSON values are not supported")


def _parse_int(value: str) -> int:
    number = int(value)
    if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
        raise StrictJSONError("JSON integer exceeds the safe integer range")
    return number


def _reject_constant(value: str) -> Any:
    raise StrictJSONError(f"non-finite JSON value is not supported: {value}")


def loads(text: str) -> Any:
    if not isinstance(text, str):
        raise StrictJSONError("JSON input must be text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_float=_reject_float,
            parse_int=_parse_int,
            parse_constant=_reject_constant,
        )
    except StrictJSONError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrictJSONError("invalid JSON") from exc
    _reject_surrogates(value)
    return value


def _reject_surrogates(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise StrictJSONError("JSON strings must not contain lone surrogates")
    elif isinstance(value, list):
        for item in value:
            _reject_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)


def _sort_key(value: str) -> bytes:
    return value.encode("utf-16-be")


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _canonical_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise StrictJSONError("JSON integer exceeds the safe integer range")
        return str(value)
    if isinstance(value, float):
        raise StrictJSONError("floating-point JSON values are not supported")
    if isinstance(value, str):
        _reject_surrogates(value)
        return _string(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_text(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise StrictJSONError("JSON object keys must be strings")
        return "{" + ",".join(
            _string(key) + ":" + _canonical_text(value[key])
            for key in sorted(value, key=_sort_key)
        ) + "}"
    raise StrictJSONError(f"unsupported JSON value type: {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    _reject_surrogates(value)
    return _canonical_text(value).encode("utf-8")
