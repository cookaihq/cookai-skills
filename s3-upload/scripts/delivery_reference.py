"""Object Reference v2: construction, parsing, and the disposition lock.

Naming note: ``ReferenceError`` below shadows the builtin of the same name.
The builtin is *not* a ``ValueError`` subclass, so a module that imports this
one bare would silently change what ``except ReferenceError`` means at its own
call sites. The name is fixed by the plan and is deliberately not renamed in
this task; importers must spell it ``delivery_reference.ReferenceError`` or
alias it on import instead of pulling the bare name into their namespace.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from artifacts import ArtifactError, validate_provider_identifier
from delivery_schema import (
    DISPOSITIONS, DeliverySchemaError, VERIFICATION_CHANNELS, body_of,
    build_typed, parse_typed,
)
from v2_schema import (
    SchemaError, normalize_endpoint, normalize_public_base, validate_object_key,
)


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

# Unpacked from the vocabulary rather than re-spelled below, so the vocabulary
# is the single source of both literals: the scope table and the derivation in
# build_object_reference_v2 cannot be left behind when a member is renamed.
# Before this binding CONTENT_STABILITIES had no producer and no consumer
# anywhere in the repository -- rewriting it to ("nonsense",) left the whole
# suite green, which is the very "dangling vocabulary entry" this change keeps
# nailing shut elsewhere.
_UNPINNED, _PINNED = CONTENT_STABILITIES

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
    _UNPINNED: ("current-key",),
    _PINNED: ("current-key", "exact-version"),
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

# Same bound artifacts._validate_reference puts on v1 retention days
# (scripts/artifacts.py:198). Without it the constructor returns an artifact
# that serialize_artifact then refuses -- verified by running
# build_object_reference_v2(retention={"mode": "expire", "days": 2 ** 53, ...}),
# which succeeds and only blows up later as
# DeliverySchemaError("artifact is not canonically serializable"). The
# constructor's contract is "returns a writable artifact", so the refusal
# belongs here, not at the fsync in front of result_out.
_MAX_RETENTION_DAYS = (1 << 53) - 1


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


def _credentials(value: Any) -> Tuple[str, ...]:
    """Materialise the credential screening material and check its type.

    Raises TypeError, not ReferenceError, and does so before any field of the
    artifact is looked at. Two reasons, both measured:

    * Blame. Handing credentials=[7] to parse_object_reference_v2 used to
      surface as ReferenceError("object reference body is not constructible"),
      because the TypeError out of validate_provider_identifier was caught by
      the except TypeError that guards the rebuild call. That reports a bug in
      the caller's own argument as "the artifact somebody gave you is broken",
      which inverts the one question this module exists to answer.
    * One-shot iterables. The parameter is typed Iterable, so a generator is
      legal. Consuming it lazily deep inside _location means it is consumed
      only when location.version_id happens to be non-null, and never at all
      otherwise -- so build_object_reference_v2(credentials=[7]) with a null
      version_id used to be accepted in silence. Materialising here makes the
      screen's input independent of the data being screened.
    """
    if isinstance(value, (str, bytes)):
        # A bare secret is iterable, and iterating it screens for single
        # characters instead of for the secret.
        raise TypeError("credentials must be an iterable of strings")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise TypeError("credentials must be an iterable of strings") from exc
    for item in items:
        if not isinstance(item, str):
            raise TypeError("credentials must be an iterable of strings")
    return items


def _content(value: Any) -> Dict[str, Any]:
    item = _object(value, "content")
    _exact(item, ("sha256", "size"), "content")
    return {"sha256": _sha256(item["sha256"]), "size": _size(item["size"])}


def _location(value: Any, credentials: Iterable[str] = ()) -> Dict[str, Any]:
    item = _object(value, "location")
    keys = ("addressing", "bucket", "endpoint", "key", "provider", "region",
            "version_id")
    _exact(item, keys, "location")
    if item["addressing"] not in _ADDRESSING:
        raise ReferenceError("invalid location.addressing")
    endpoint = _text(item["endpoint"], "location.endpoint")
    key = _text(item["key"], "location.key")
    # The checks v1 ran on key, endpoint and version_id
    # (artifacts._validate_reference, scripts/artifacts.py:158-168); the
    # version_id half is a few lines below. This module is the only place that
    # both writes and reads an Object Reference v2, so "the producer would
    # never emit a dirty value" is not an argument for skipping them:
    # parse_object_reference_v2 exists precisely to re-run every construction
    # check against an artifact somebody else supplied.
    try:
        validate_object_key(key)
    except SchemaError as exc:
        # SchemaError, not the wider ValueError the two normalizer clauses
        # below use. The three call sites are not the same shape:
        # normalize_public_base and normalize_endpoint run urlsplit outside
        # any try, so "https://[::1" leaves them as a bare
        # ValueError("Invalid IPv6 URL") -- proven by running them, and each
        # wide clause has a mutation showing it carries payload (#13 and R2).
        # validate_object_key runs no urlsplit: _single_line, the
        # startswith/endswith checks and the segment scan raise SchemaError
        # and nothing else, so widening here catches no known leak. It does
        # cost something: a bare ValueError out of validate_object_key could
        # only be an implementation bug in that function, and reporting a
        # library bug as "the caller's key is invalid" is exactly the kind of
        # misattribution this module exists to prevent. Injecting a bare
        # ValueError into validate_object_key was invisible to the whole
        # suite under the wide clause (M18, full scope), and narrowing back
        # to SchemaError changed nothing (M17, full scope) -- so the narrow
        # clause is free, and test_a_library_bug_in_validate_object_key_is_
        # not_reported_as_an_invalid_key keeps it that way.
        raise ReferenceError("invalid location.key") from exc
    try:
        normalized_endpoint = normalize_endpoint(endpoint)
    except ValueError as exc:
        # Proven leak, same shape as _access: normalize_endpoint("https://[::1")
        # raises a bare ValueError("Invalid IPv6 URL") out of urlsplit.
        raise ReferenceError("invalid location.endpoint") from exc
    if normalized_endpoint != endpoint:
        # This comparison, not the exception above, is what refuses most bad
        # endpoints: normalize_endpoint("not-a-url") does not raise, it
        # returns "https://not-a-url". Measured, not assumed.
        raise ReferenceError("location.endpoint is not normalized")
    version_id = item["version_id"]
    if version_id is not None:
        # Not _text: a version_id is provider-supplied text that lands in an
        # artifact and in log lines, so it gets v1's screen -- 4096-byte cap,
        # printable ASCII only, and a check that no credential the caller
        # handed us appears in it raw or percent-encoded. Global Constraints
        # forbid credential material reaching an artifact; _text alone made
        # that a convention instead of a structural guarantee.
        try:
            version_id = validate_provider_identifier(version_id, credentials)
        except ArtifactError as exc:
            # ArtifactError, not ValueError, and the claim is scoped to the
            # *value* argument: for any version_id whatsoever,
            # IdentifierRejected is the only type validate_provider_identifier
            # raises, because its charset gate runs before
            # _strict_percent_decode and unquote_to_bytes therefore only ever
            # sees printable ASCII (source check, scripts/artifacts.py:108-118).
            # The credentials argument is a different matter and deliberately
            # not converted: a malformed credentials sequence raises TypeError
            # ("v-1", [7] and ("v-1", None) both do -- run, not assumed), and
            # that is a bug in the caller's own argument, not evidence that
            # the artifact is bad. _credentials refuses it at the entry of
            # both halves so it can never reach here. The message deliberately
            # carries no value.
            raise ReferenceError("invalid location.version_id") from exc
    return {
        "addressing": item["addressing"],
        "bucket": _text(item["bucket"], "location.bucket"),
        "endpoint": endpoint,
        "key": key,
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
        if (not isinstance(days, int) or isinstance(days, bool)
                or not 1 <= days <= _MAX_RETENTION_DAYS):
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
                              target_ref: Any, verifications: Any,
                              credentials: Iterable[str] = ()) -> Dict[str, Any]:
    """Build an Object Reference v2 body and wrap it in a typed envelope.

    ``credentials`` is the same screening material v1's build/parse pair took
    (``artifacts.build_object_reference``): every string in it is refused
    inside ``location.version_id``. It defaults to empty so the length and
    charset half of the screen still runs for callers that hold no secrets.
    """
    if disposition not in DISPOSITIONS:
        raise ReferenceError("unregistered disposition")
    credentials = _credentials(credentials)
    content = _content(content)
    location = _location(location, credentials)
    access = _access(access)
    retention = _retention(retention)
    stability = _PINNED if location["version_id"] is not None else _UNPINNED
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


def parse_object_reference_v2(text: str,
                              credentials: Iterable[str] = ()) -> Dict[str, Any]:
    """Parse and re-derive, so reading is as strict as writing.

    parse_typed only proves the envelope and the field set. Rebuilding the
    body from its own non-derived fields is what refuses a hand-edited
    artifact whose disposition and object_written disagree -- the read/write
    asymmetry that let serialize_artifact check type but not version.

    ``credentials`` is threaded through for the same reason the whole rebuild
    is: the read side handles an artifact somebody else produced, so it needs
    the credential screen on ``location.version_id`` at least as much as the
    write side does. v1 took it on both halves too
    (``artifacts.parse_object_reference``).
    """
    # Before the rebuild, not inside it: the except TypeError below is there
    # to turn "this body does not fit the constructor's signature" into a
    # verdict on the artifact, and a malformed credentials argument would
    # otherwise be laundered through it into exactly the wrong verdict.
    credentials = _credentials(credentials)
    item = parse_typed(text, expected_type="s3-upload.object-reference")
    body = body_of(item)
    supplied = {key: value for key, value in body.items()
                if key not in DERIVED_FIELDS}
    try:
        rebuilt = build_object_reference_v2(credentials=credentials, **supplied)
    except TypeError as exc:
        raise ReferenceError("object reference body is not constructible") from exc
    if body_of(rebuilt) != body:
        raise ReferenceError("object reference disagrees with its own derivation")
    return item
