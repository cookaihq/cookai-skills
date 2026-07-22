from __future__ import annotations

import json


class StrictJsonError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise StrictJsonError("duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(_value):
    raise StrictJsonError("non-finite JSON number")


def loads(data: bytes):
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StrictJsonError("invalid JSON") from exc
