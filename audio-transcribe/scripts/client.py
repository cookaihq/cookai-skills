from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from collections import namedtuple

# api.foxapi.cc sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" User-Agent with HTTP 403 / "error code: 1010". A browser
# UA clears that gate. Callers may override by passing their own User-Agent.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

Resp = namedtuple("Resp", "status json text")


def http_request(method: str, url: str, headers: dict,
                 body: "bytes | None" = None, timeout: int = 60) -> Resp:
    """HTTP request returning Resp(status, json, text) for any HTTP status
    (incl. 4xx/5xx). Raises urllib.error.URLError only on network failure
    (caller treats that as fatal, not a key-fallback trigger)."""
    if not any(h.lower() == "user-agent" for h in headers):
        headers = {**headers, "User-Agent": DEFAULT_USER_AGENT}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(); status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read(); status = e.code
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    return Resp(status, parsed, text)


def call_with_key_fallback(keys: list, attempt) -> tuple:
    """Try each key via attempt(key)->Resp. Advance to the next key ONLY on
    HTTP 401 (401 does not consume credits). Any other status stops immediately.
    Returns (Resp, used_key). Raises ValueError if no keys."""
    if not keys:
        raise ValueError("no API key available (X_API_KEY not found)")
    last = None
    for k in keys:
        last = attempt(k)
        if last.status != 401:
            return last, k
    return last, keys[-1]
