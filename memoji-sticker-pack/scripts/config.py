from __future__ import annotations

import os


def parse_dotenv(text: str) -> dict:
    """Parse the minimal, non-shell .env syntax used by public skills."""
    out: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def read_key_from_dotenv(path: str) -> "str | None":
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    return parse_dotenv(text).get("X_API_KEY")


def resolve_api_keys(environ: dict, cwd: str, use_local_key: bool, config_dir: str) -> list:
    """Resolve X_API_KEY values in first-found order and remove duplicates."""
    candidates = []
    env_key = (environ.get("X_API_KEY") or "").strip()
    if env_key:
        candidates.append(env_key)
    for fname in (".env.local", ".env"):
        key = read_key_from_dotenv(os.path.join(cwd, fname))
        if key:
            candidates.append(key)
    if use_local_key:
        key = read_key_from_dotenv(os.path.join(config_dir, ".env"))
        if key:
            candidates.append(key)

    seen = set()
    result = []
    for key in candidates:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
