from __future__ import annotations

import os


def parse_dotenv(text: str) -> dict:
    """Minimal, non-shell .env parser. Supports KEY=value / KEY="value" /
    KEY='value', whitespace around =, leading-# comment lines, blank lines.
    Last occurrence wins. No ${X} / $(...) / line-continuation expansion.
    Does not strip an `export ` prefix or trailing inline `# comments` — values are literal."""
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
    """Ordered candidate X_API_KEY list (first = highest priority), value-deduped.
    Sources: process env -> $cwd/.env.local -> $cwd/.env -> $config_dir/.env (only
    when use_local_key). $cwd files are read non-recursively (current dir only)."""
    candidates = []
    env_key = (environ.get("X_API_KEY") or "").strip()
    if env_key:
        candidates.append(env_key)
    for fname in (".env.local", ".env"):
        k = read_key_from_dotenv(os.path.join(cwd, fname))
        if k:
            candidates.append(k)
    if use_local_key:
        k = read_key_from_dotenv(os.path.join(config_dir, ".env"))
        if k:
            candidates.append(k)
    seen = set()
    result = []
    for k in candidates:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
