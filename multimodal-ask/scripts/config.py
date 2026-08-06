from __future__ import annotations

import os
from typing import NamedTuple

# Canonical key variable name. `X_API_KEY` is the historical name and is still
# accepted everywhere the canonical one is, one release-notice grace period.
KEY_NAME = "AIHUB_API_KEY"
LEGACY_KEY_NAME = "X_API_KEY"
KEY_NAMES = (KEY_NAME, LEGACY_KEY_NAME)


class KeyCandidate(NamedTuple):
    value: str
    source: str
    var_name: str

    @property
    def is_legacy(self) -> bool:
        return self.var_name == LEGACY_KEY_NAME


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


def read_key_entry_from_dotenv(path: str) -> "tuple[str | None, str | None]":
    """Return (value, var_name) for the first key name present in the file,
    canonical name before the legacy one. (None, None) when the file is
    unreadable or holds neither name."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None, None
    values = parse_dotenv(text)
    for name in KEY_NAMES:
        val = values.get(name)
        if val:
            return val, name
    return None, None


def read_key_from_dotenv(path: str) -> "str | None":
    return read_key_entry_from_dotenv(path)[0]


def resolve_api_key_candidates(
    environ: dict, cwd: str, use_local_key: bool, config_dir: str
) -> "list[KeyCandidate]":
    """Ordered candidates (first = highest priority), value-deduped.
    Sources: process env -> $cwd/.env.local -> $cwd/.env -> $config_dir/.env (only
    when use_local_key). $cwd files are read non-recursively (current dir only).
    Within one source the canonical name wins over the legacy one."""
    candidates: "list[KeyCandidate]" = []
    for name in KEY_NAMES:
        env_key = (environ.get(name) or "").strip()
        if env_key:
            candidates.append(KeyCandidate(env_key, "env %s" % name, name))
            break
    paths = [os.path.join(cwd, fname) for fname in (".env.local", ".env")]
    if use_local_key:
        paths.append(os.path.join(config_dir, ".env"))
    for path in paths:
        val, name = read_key_entry_from_dotenv(path)
        if val and name:
            candidates.append(KeyCandidate(val, path, name))
    seen = set()
    result = []
    for cand in candidates:
        if cand.value not in seen:
            seen.add(cand.value)
            result.append(cand)
    return result


def resolve_api_keys(environ: dict, cwd: str, use_local_key: bool, config_dir: str) -> list:
    """Values only, same order as resolve_api_key_candidates()."""
    return [c.value for c in resolve_api_key_candidates(environ, cwd, use_local_key, config_dir)]


def legacy_key_notice(candidates: "list[KeyCandidate]") -> "str | None":
    """One-line deprecation notice when the key was only found under the old
    name, else None."""
    if not candidates or not candidates[0].is_legacy:
        return None
    return (
        "⚠️ %s 已废弃，请改用 %s（本次仍按 %s 读取，来源：%s）"
        % (LEGACY_KEY_NAME, KEY_NAME, LEGACY_KEY_NAME, candidates[0].source)
    )


def mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
