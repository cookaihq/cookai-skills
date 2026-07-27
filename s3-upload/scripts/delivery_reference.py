from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Optional, Sequence, Tuple

from delivery_schema import (
    DISPOSITIONS, DeliverySchemaError, VERIFICATION_CHANNELS, body_of,
    build_typed, parse_typed,
)
from v2_schema import normalize_public_base


class ReferenceError(ValueError):
    pass


# Domain separator for whoever hashes an Object Reference v2. Nothing in this
# module hashes one -- the reference is carried inside the result envelope and
# the result's own digest covers it -- so this exists for the envelope work,
# not as leftover code.
REFERENCE_DOMAIN = "s3-upload/object-reference/v2"

VERIFICATION_FIELDS: Tuple[str, ...] = (
    "channel", "sha256", "size", "url_scope", "verified_at",
)

URL_SCOPES: Tuple[str, ...] = ("current-key", "exact-version")

CONTENT_STABILITIES: Tuple[str, ...] = ("current_key_unpinned", "version_pinned")

DISPOSITION_WRITE_CERTAINTY: Dict[str, Optional[bool]] = {
    "adopted": False,
    "created": True,
    "reconciled": None,
}

# adopted and reconciled are claims about bytes this operation did not
# necessarily write, so both are only reachable through a full authenticated
# read. created claims only what the Put response already proved, so it
# requires no channel of its own; a public delivery adds the anonymous
# channel on top, it does not replace this one.
DISPOSITION_REQUIRED_CHANNELS: Dict[str, Tuple[str, ...]] = {
    "adopted": ("authenticated_full_get",),
    "created": (),
    "reconciled": ("authenticated_full_get",),
}

# What a verification is allowed to claim it looked at. A reference whose
# location carries no version_id records one observation of the current key;
# calling that scope "exact-version" would turn a moment into a permanent
# proof, so the unpinned side admits only the honest scope.
_SCOPES_FOR_STABILITY: Dict[str, Tuple[str, ...]] = {
    "current_key_unpinned": ("current-key",),
    "version_pinned": ("current-key", "exact-version"),
}

# Derived from the rest of the body, never accepted from the caller.
DERIVED_FIELDS: Tuple[str, ...] = ("content_stability", "object_written")

_ADDRESSING = frozenset({"path", "virtual", "bucket-bound"})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
# Same shape as results.RFC3339_RE (scripts/results.py:16) and
# artifacts.RFC3339_RE (scripts/artifacts.py:29). Kept local rather than
# imported so a V2 module does not take a dependency on a v1 one for a regex.
_RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_MAX_PRESIGN_SECONDS = 604800


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReferenceError(f"{label} must be an object")
    return value


def _exact(value: Dict[str, Any], keys: Sequence[str], label: str) -> None:
    expected = set(keys)
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ReferenceError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ReferenceError(f"{label} is missing fields: {', '.join(missing)}")


def _size(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReferenceError("size must be a non-negative integer")
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReferenceError("sha256 must be 64 lowercase hex characters")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ReferenceError(f"{label} must be a sha256: digest")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReferenceError(f"{label} must be 32 lowercase hex characters")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReferenceError(f"{label} must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ReferenceError(f"{label} must not contain control characters")
    return value


def _content(value: Any) -> Dict[str, Any]:
    item = _object(value, "content")
    _exact(item, ("sha256", "size"), "content")
    return {"sha256": _sha256(item["sha256"]), "size": _size(item["size"])}


def _location(value: Any) -> Dict[str, Any]:
    item = _object(value, "location")
    keys = ("addressing", "bucket", "endpoint", "key", "provider", "region",
            "version_id")
    _exact(item, keys, "location")
    if item["addressing"] not in _ADDRESSING:
        raise ReferenceError("invalid location.addressing")
    version_id = item["version_id"]
    if version_id is not None:
        _text(version_id, "location.version_id")
    return {
        "addressing": item["addressing"],
        "bucket": _text(item["bucket"], "location.bucket"),
        "endpoint": _text(item["endpoint"], "location.endpoint"),
        "key": _text(item["key"], "location.key"),
        "provider": _text(item["provider"], "location.provider"),
        "region": _text(item["region"], "location.region"),
        "version_id": version_id,
    }


def _access(value: Any) -> Dict[str, Any]:
    item = _object(value, "access")
    _exact(item, ("mode", "presign_expires_seconds", "public_base_url"), "access")
    mode = item["mode"]
    expires = item["presign_expires_seconds"]
    base = item["public_base_url"]
    if mode == "private":
        if base is not None:
            raise ReferenceError("private access requires public_base_url=null")
        if (not isinstance(expires, int) or isinstance(expires, bool)
                or not 1 <= expires <= _MAX_PRESIGN_SECONDS):
            raise ReferenceError("invalid presign_expires_seconds")
    elif mode == "public":
        if expires is not None or not isinstance(base, str):
            raise ReferenceError(
                "public access requires an HTTPS base and null presign expiry"
            )
        try:
            normalized = normalize_public_base(base)
        except ValueError as exc:
            # Not only SchemaError: normalize_public_base runs urlsplit, and
            # urlsplit("https://[::1") raises a bare ValueError("Invalid IPv6
            # URL") that no SchemaError clause would catch. Verified by
            # running it, not assumed -- SchemaError is a ValueError subclass,
            # so the wider clause covers both.
            raise ReferenceError("invalid public_base_url") from exc
        if normalized != base:
            raise ReferenceError("public_base_url is not normalized")
    else:
        raise ReferenceError("access.mode must be private or public")
    return {"mode": mode, "presign_expires_seconds": expires,
            "public_base_url": base}


def _retention(value: Any) -> Dict[str, Any]:
    item = _object(value, "retention")
    _exact(item, ("days", "enforcement", "mode"), "retention")
    if item["enforcement"] != "external-unverified":
        raise ReferenceError("invalid retention enforcement")
    days = item["days"]
    if item["mode"] == "retain":
        if days is not None:
            raise ReferenceError("retain requires days=null")
    elif item["mode"] == "expire":
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            raise ReferenceError("invalid retention days")
    else:
        raise ReferenceError("invalid retention mode")
    return {"days": days, "enforcement": item["enforcement"], "mode": item["mode"]}


def build_verification(*, channel: str, size: Any, sha256: Any, url_scope: str,
                       verified_at: Any) -> Dict[str, Any]:
    if channel not in VERIFICATION_CHANNELS:
        raise ReferenceError("unregistered verification channel")
    if url_scope not in URL_SCOPES:
        raise ReferenceError("unregistered url_scope")
    if not isinstance(verified_at, str) or not _RFC3339_RE.fullmatch(verified_at):
        raise ReferenceError("verified_at must be RFC3339 UTC seconds")
    return {
        "channel": channel,
        "sha256": _sha256(sha256),
        "size": _size(size),
        "url_scope": url_scope,
        "verified_at": verified_at,
    }


def _verifications(value: Any, content: Dict[str, Any],
                   stability: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise ReferenceError("verifications must be a list")
    scopes = _SCOPES_FOR_STABILITY[stability]
    entries = []
    for item in value:
        _exact(_object(item, "verification"), VERIFICATION_FIELDS, "verification")
        entry = build_verification(**item)
        if entry["size"] != content["size"] or entry["sha256"] != content["sha256"]:
            raise ReferenceError(
                "verification does not certify the content it is attached to"
            )
        if entry["url_scope"] not in scopes:
            raise ReferenceError("url_scope claims more than content_stability allows")
        entries.append(entry)
    channels = [entry["channel"] for entry in entries]
    if len(set(channels)) != len(channels):
        raise ReferenceError("a channel may appear at most once in verifications")
    return sorted(entries, key=lambda entry: entry["channel"])


def build_object_reference_v2(*, access: Any, content: Any, disposition: str,
                              location: Any, operation_id: Any, plan_hash: Any,
                              plan_id: Any, retention: Any, root_recovery_id: Any,
                              target_contract: Any, target_contract_hash: Any,
                              target_ref: Any,
                              verifications: Any) -> Dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise ReferenceError("unregistered disposition")
    content = _content(content)
    location = _location(location)
    access = _access(access)
    retention = _retention(retention)
    stability = (
        "version_pinned" if location["version_id"] is not None
        else "current_key_unpinned"
    )
    entries = _verifications(verifications, content, stability)
    carried = {entry["channel"] for entry in entries}
    for channel in DISPOSITION_REQUIRED_CHANNELS[disposition]:
        if channel not in carried:
            raise ReferenceError(
                "disposition requires a verification channel it does not carry"
            )
    body = {
        "access": access,
        "content": content,
        "content_stability": stability,
        "disposition": disposition,
        "location": location,
        "object_written": DISPOSITION_WRITE_CERTAINTY[disposition],
        "operation_id": _identifier(operation_id, "operation_id"),
        "plan_hash": _digest(plan_hash, "plan_hash"),
        "plan_id": _identifier(plan_id, "plan_id"),
        "retention": retention,
        "root_recovery_id": _identifier(root_recovery_id, "root_recovery_id"),
        # deepcopy for the same reason plan_store.build_plan_body does it
        # (scripts/plan_store.py:83): the caller keeps its own handle on this
        # nested object and a shallow alias lets a later mutation reach inside
        # an artifact that has already been validated.
        "target_contract": deepcopy(_object(target_contract, "target_contract")),
        "target_contract_hash": _digest(target_contract_hash, "target_contract_hash"),
        "target_ref": _text(target_ref, "target_ref"),
        "verifications": entries,
    }
    try:
        return build_typed("s3-upload.object-reference", body)
    except DeliverySchemaError as exc:
        raise ReferenceError("object reference does not match its field set") from exc


def parse_object_reference_v2(text: str) -> Dict[str, Any]:
    """Parse and re-derive, so reading is as strict as writing.

    parse_typed only proves the envelope and the field set. Rebuilding the
    body from its own non-derived fields is what refuses a hand-edited
    artifact whose disposition and object_written disagree -- the read/write
    asymmetry that let serialize_artifact check type but not version.
    """
    item = parse_typed(text, expected_type="s3-upload.object-reference")
    body = body_of(item)
    supplied = {key: value for key, value in body.items()
                if key not in DERIVED_FIELDS}
    try:
        rebuilt = build_object_reference_v2(**supplied)
    except TypeError as exc:
        raise ReferenceError("object reference body is not constructible") from exc
    if body_of(rebuilt) != body:
        raise ReferenceError("object reference disagrees with its own derivation")
    return item
