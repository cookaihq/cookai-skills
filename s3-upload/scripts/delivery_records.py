from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, Optional, Tuple

from action_registry import (
    AUTHORIZED_ACTION_ORDER, AUTHORIZED_ACTIONS, RECOVERY_STATES,
    allowed_actions, retry_safe,
)
# Whole module, not "from delivery_reference import ReferenceError": that name
# shadows the builtin, and the builtin is not a ValueError subclass, so pulling
# it bare would silently change what except ReferenceError means here.
import delivery_reference
from delivery_reference import URL_SCOPES
from delivery_schema import (
    BLOCKING_REASONS, OPERATIONS, OUTCOME_WRITE_CERTAINTY, OUTCOMES, URL_KINDS,
    VERIFICATION_CHANNELS, DeliverySchemaError, artifact_digest, body_of,
    build_typed, serialize_artifact,
)


class RecordError(ValueError):
    pass


RECOVERY_DOMAIN = "s3-upload/recovery-descriptor/v1"
RESULT_DOMAIN = "s3-upload/result/v1"
ACK_DOMAIN = "s3-upload/ack/v1"


def _state(state: str) -> str:
    if state not in RECOVERY_STATES:
        raise RecordError("unregistered recovery state")
    return state


def authorization_required(state: str, *, capabilities_ok: bool,
                           pending_remote_steps: Iterable[str] = ()) -> Tuple[str, ...]:
    """List every action this result says still needs its own confirmation.

    Not AUTHORIZED_ACTIONS & allowed_actions(state): verify_public is an
    internal pipeline step, never offered to the caller in any state, yet it
    produces remote traffic and therefore carries a challenge of its own. A
    consumer computing the intersection would silently drop that challenge.

    pending_remote_steps is where those internal steps come in. It is screened
    against AUTHORIZED_ACTIONS rather than against ACTIONS: inspect and ack
    reach nothing remote, so listing one here would demand a confirmation for
    an action that has nothing to confirm.
    """
    state = _state(state)
    pending = tuple(pending_remote_steps)
    for action in pending:
        if action not in AUTHORIZED_ACTIONS:
            raise RecordError("pending remote step does not require action authorization")
    available = set(allowed_actions(state, capabilities_ok=capabilities_ok)) | set(pending)
    return tuple(action for action in AUTHORIZED_ACTION_ORDER if action in available)


def _state_fields(state: str, capabilities_ok: bool) -> Dict[str, Any]:
    state = _state(state)
    return {
        "allowed_actions": list(allowed_actions(state, capabilities_ok=capabilities_ok)),
        "recovery_state": state,
        "retry_safe": retry_safe(state, capabilities_ok=capabilities_ok),
    }


def _operation(operation: str) -> str:
    if operation not in OPERATIONS:
        raise RecordError("unregistered operation")
    return operation


def result_hash(body: Dict[str, Any]) -> str:
    material = {key: value for key, value in body.items() if key != "result_hash"}
    return artifact_digest(RESULT_DOMAIN, material)


def build_recovery_descriptor(*, recovery_id: str, root_recovery_id: str, operation: str,
                              operation_id: str, plan_id: str, plan_hash: str,
                              target_contract_hash: str, object_key: str, result_out: str,
                              state: str, capabilities_ok: bool) -> Dict[str, Any]:
    body = {
        "object_key": object_key,
        "operation": _operation(operation),
        "operation_id": operation_id,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "recovery_id": recovery_id,
        "result_out": result_out,
        "root_recovery_id": root_recovery_id,
        "target_contract_hash": target_contract_hash,
    }
    state_fields = _state_fields(state, capabilities_ok)
    if body["operation"] not in state_fields["allowed_actions"]:
        raise RecordError("operation is not permitted by recovery state")
    body.update(state_fields)
    return build_typed("s3-upload.recovery-descriptor", body)


# Same shape as delivery_reference._RFC3339_RE and results.RFC3339_RE. Kept
# local for the same reason: a regex is not worth a module dependency.
_RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

CONTENT_VERIFICATION_FIELDS: Tuple[str, ...] = (
    "channels", "sha256", "size", "verified_at",
)

# Not None when nothing has been verified: "no verification has happened" is a
# closed container with nothing in it, while null would say the field does not
# apply to this result -- and it always applies.
EMPTY_CONTENT_VERIFICATION: Dict[str, Any] = {
    "channels": [], "sha256": None, "size": None, "verified_at": None,
}


class _Unset:
    pass


_UNSET = _Unset()


def _content_verification(value: Any) -> Dict[str, Any]:
    if value is None:
        # deepcopy, not dict(): a shallow copy hands every result the same
        # channels list, so appending to one result's container reaches inside
        # every other one. Same class of aliasing the reference's deepcopy of
        # target_contract closes, found by the test rather than by reading.
        return deepcopy(EMPTY_CONTENT_VERIFICATION)
    if not isinstance(value, dict):
        raise RecordError("content_verification must be an object")
    unknown = sorted(set(value) - set(CONTENT_VERIFICATION_FIELDS))
    missing = sorted(set(CONTENT_VERIFICATION_FIELDS) - set(value))
    if unknown or missing:
        raise RecordError("content_verification field set is not exact")
    channels = value["channels"]
    if not isinstance(channels, (list, tuple)):
        raise RecordError("content_verification channels must be a list")
    channels = list(channels)
    for channel in channels:
        if channel not in VERIFICATION_CHANNELS:
            raise RecordError("unregistered verification channel")
    if channels != sorted(set(channels)):
        raise RecordError("verification channels must be unique and ascending")
    sha256, size, verified_at = value["sha256"], value["size"], value["verified_at"]
    if not channels:
        # A container carrying a digest but naming no channel would claim a
        # verification nobody performed; the reference's own verifications
        # list refuses the mirror image of this (a channel certifying content
        # it does not match).
        if (sha256, size, verified_at) != (None, None, None):
            raise RecordError("a verification with no channel certifies nothing")
        return {"channels": [], "sha256": None, "size": None, "verified_at": None}
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise RecordError("content_verification sha256 must be 64 lowercase hex characters")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RecordError("content_verification size must be a non-negative integer")
    if not isinstance(verified_at, str) or not _RFC3339_RE.fullmatch(verified_at):
        raise RecordError("content_verification verified_at must be RFC3339 UTC seconds")
    return {"channels": channels, "sha256": sha256, "size": size,
            "verified_at": verified_at}


def _url_fields(url: Any, kind: Any, scope: Any, expires_at: Any) -> Dict[str, Any]:
    if url is None:
        if (kind, scope, expires_at) != (None, None, None):
            raise RecordError("url_kind, url_scope and url_expires_at require a url")
        return {"url": None, "url_kind": None, "url_scope": None,
                "url_expires_at": None}
    if not isinstance(url, str) or not url:
        raise RecordError("url must be a non-empty string")
    if kind not in URL_KINDS:
        raise RecordError("unregistered url_kind")
    if scope not in URL_SCOPES:
        raise RecordError("unregistered url_scope")
    if kind == "presigned":
        if not isinstance(expires_at, str) or not _RFC3339_RE.fullmatch(expires_at):
            raise RecordError("a presigned url requires an RFC3339 expiry")
    elif expires_at is not None:
        # A public URL that carries an expiry invites the caller to treat it as
        # decaying, or worse, to treat the absence of an expiry elsewhere as
        # proof of permanence. The two kinds differ precisely here.
        raise RecordError("a public url must not carry an expiry")
    return {"url": url, "url_kind": kind, "url_scope": scope,
            "url_expires_at": expires_at}


def _object_reference(value: Any, outcome: str, object_written: Optional[bool],
                      credentials: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    versioned = isinstance(value, dict) and isinstance(
        value.get("location"), dict) and value["location"].get("version_id") is not None
    if versioned and isinstance(credentials, _Unset):
        # v1's discipline, ported: the write side passes credentials whenever a
        # real provider version_id is in play (operations.py:338,
        # multipart.py:491) and omits them only where version_id is None
        # (operations.py:170). Defaulting to () here would keep that promise
        # by accident rather than structurally -- a caller that simply forgot
        # would embed an unscreened provider string in a public artifact.
        raise RecordError(
            "a versioned object reference must be screened against credentials"
        )
    screen = () if isinstance(credentials, _Unset) else credentials
    # Serialise and re-parse rather than trusting the dict in hand: the read
    # side is the strict half, and a reference that cannot survive the round
    # trip has no business being embedded in an artifact that will be written.
    # One try over the whole derivation; the except names every type the pair
    # can raise. delivery_reference.ReferenceError is spelled out rather than
    # imported bare because it shadows the builtin.
    try:
        item = delivery_reference.parse_object_reference_v2(
            serialize_artifact(value).decode("utf-8"), credentials=screen
        )
    except (DeliverySchemaError, delivery_reference.ReferenceError) as exc:
        raise RecordError("object reference is not a valid Object Reference v2") from exc
    body = body_of(item)
    if body["disposition"] != outcome:
        raise RecordError("object reference disposition disagrees with the outcome")
    if body["object_written"] is not object_written:
        # Defence in depth, and known to be unreachable today: deleting it left
        # the whole suite green (T12, full scope) and no input can currently
        # reach it. Three facts have to hold at once for that -- the reference
        # derives object_written from its disposition, this envelope derives it
        # from the outcome, and the two certainty tables agree on the three
        # shared keys (pinned by test_disposition_write_certainty_agrees_with_
        # the_outcome_table). Break any one of them and this becomes the only
        # thing standing between a result and a reference that contradict each
        # other about whether bytes were written, so it is not dead code.
        raise RecordError("object reference disagrees with object_written")
    return item


def build_result(*, operation: str, operation_id: str, plan_id: str, plan_hash: str,
                 target_contract_hash: str, recovery_id: str, root_recovery_id: str,
                 state: str, capabilities_ok: bool, outcome: str,
                 blocking_reasons: Iterable[str] = (),
                 predecessor_operation_id: Optional[str] = None,
                 predecessor_result_hash: Optional[str] = None,
                 object_reference: Optional[Dict[str, Any]] = None,
                 content_verification: Optional[Dict[str, Any]] = None,
                 url: Optional[str] = None, url_kind: Optional[str] = None,
                 url_scope: Optional[str] = None,
                 url_expires_at: Optional[str] = None,
                 pending_remote_steps: Iterable[str] = (),
                 credentials: Any = _UNSET) -> Dict[str, Any]:
    reasons = list(blocking_reasons)
    for reason in reasons:
        if reason not in BLOCKING_REASONS:
            raise RecordError("unregistered blocking reason")
    if outcome not in OUTCOMES:
        raise RecordError("unregistered outcome")
    # Derived, never accepted: an outcome and a write certainty that disagree
    # is the one combination this envelope must not be able to express, and a
    # parameter is exactly how it would come to.
    object_written = OUTCOME_WRITE_CERTAINTY[outcome]
    body = {
        "authorization_required": list(authorization_required(
            state, capabilities_ok=capabilities_ok,
            pending_remote_steps=pending_remote_steps,
        )),
        "blocking_reasons": reasons,
        "content_verification": _content_verification(content_verification),
        "object_reference": _object_reference(
            object_reference, outcome, object_written, credentials
        ),
        "object_written": object_written,
        "operation": _operation(operation),
        "operation_id": operation_id,
        "outcome": outcome,
        "plan_hash": plan_hash,
        "plan_id": plan_id,
        "predecessor_operation_id": predecessor_operation_id,
        "predecessor_result_hash": predecessor_result_hash,
        "recovery_id": recovery_id,
        "result_hash": None,
        "root_recovery_id": root_recovery_id,
        "target_contract_hash": target_contract_hash,
    }
    body.update(_url_fields(url, url_kind, url_scope, url_expires_at))
    body.update(_state_fields(state, capabilities_ok))
    body["result_hash"] = result_hash(body)
    return build_typed("s3-upload.result", body)


def build_ack(*, caller: str, plan_id: str, recovery_id: str, root_recovery_id: str,
              predecessor_operation_id: str, result_hash_value: str) -> Dict[str, Any]:
    body = {
        "acknowledged": True,
        "caller": caller,
        "plan_id": plan_id,
        "predecessor_operation_id": predecessor_operation_id,
        "recovery_id": recovery_id,
        "result_hash": result_hash_value,
        "root_recovery_id": root_recovery_id,
    }
    return build_typed("s3-upload.ack", body)
