from __future__ import annotations

import re
from typing import Collection, Dict, Optional


_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


class DotenvError(ValueError):
    pass


def parse_dotenv(
    text: Optional[str], *, allowed_keys: Collection[str], label: str,
) -> Dict[str, str]:
    allowed = frozenset(allowed_keys)
    if not allowed or any(not _KEY_RE.fullmatch(key) for key in allowed):
        raise ValueError("allowed dotenv keys must be uppercase environment names")
    values: Dict[str, str] = {}
    if text is None:
        return values
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line in allowed:
                raise DotenvError(f"invalid {label} line {number}")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            suffix = value[end + 1:].strip() if end >= 0 else ""
            if end < 0 or (suffix and not suffix.startswith("#")):
                raise DotenvError(f"invalid quoted {label} value on line {number}")
            value = value[1:end]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values
