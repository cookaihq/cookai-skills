"""The admitted Mobile custom-header authentication contract."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from access_policy import normalize_access

AUTH_TYPE = "mobile_key_v1"
KEY_HEADER = "X-Hermes-Mobile-Key"


def valid_key(value):
    # A canonical, unpadded encoding of 256 random bits. Syntax cannot prove
    # entropy; the UI generates all 32 bytes with the browser CSPRNG.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except ValueError:
        return False
    return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode().rstrip("=") == value


def generate_key():
    return secrets.token_urlsafe(32)


def binding_digest(source, connection_id, record):
    """Stable process-local comparison; never serialize this record to logs."""
    material = {key: record[key] for key in ("domain_prefix", "hermes", "frp", "auth")}
    material['access'] = normalize_access(record.get('access'))
    return hashlib.sha256(json.dumps([source, connection_id, material],
                                    sort_keys=True, ensure_ascii=True).encode()).hexdigest()
