from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from copy import deepcopy
from datetime import datetime, timedelta

import aihub_upload
import bundle
import config
import preflight
import settings


SCHEMA_VERSION = 1
SOURCE_UPLOAD_STATES = frozenset(
    {
        "source_upload_not_started",
        "source_upload_started",
        "source_upload_ready",
        "source_upload_rejected",
        "source_upload_unknown",
        "source_upload_expired",
    }
)
PUBLIC_ATTEMPT_KEYS = frozenset(
    {
        "attempt_id",
        "state",
        "source_sha256",
        "credential",
        "started_at",
        "completed_at",
        "http_status",
        "reason_code",
        "url_sha256",
    }
)
PRIVATE_ONLY_ATTEMPT_KEYS = frozenset({"url", "expires_at"})
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ATTEMPT_ID_PATTERN = re.compile(r"source-upload-(0*[1-9][0-9]*)")
ACTION_ID_PATTERN = re.compile(r"source-upload-decision-[0-9a-f]{32}")
UNKNOWN_REASON_CODES = frozenset(
    {
        "invalid_transport_result",
        "invalid_success_response",
        "unverified_upload_result",
        "network_result_unknown",
        "interrupted_before_result_commit",
        "result_private_payload_lost",
    }
)


class SourceStagingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _valid_timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return _parse_timestamp(value).utcoffset() == timedelta(0)
    except SourceStagingError:
        return False


def _valid_hash(value) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _valid_credential(value) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "source_id",
        "fingerprint",
        "locator",
    }:
        return False
    source_id = value.get("source_id")
    locator = value.get("locator")
    if not isinstance(source_id, str) or not _valid_hash(value.get("fingerprint")):
        return False
    if not isinstance(locator, dict) or locator.get("name") != config.KEY_NAME:
        return False
    kind = locator.get("kind")
    if kind == "process_environment":
        return (
            set(locator) == {"kind", "name"}
            and source_id == f"process_environment:{config.KEY_NAME}"
        )
    if kind == "dotenv":
        path = locator.get("path")
        return (
            set(locator) == {"kind", "path", "name"}
            and isinstance(path, str)
            and os.path.isabs(path)
            and os.path.normpath(path) == path
            and source_id == f"dotenv:{path}:{config.KEY_NAME}"
        )
    return False


def _timestamp_order(*values) -> bool:
    try:
        parsed = [_parse_timestamp(value) for value in values]
    except SourceStagingError:
        return False
    return all(left <= right for left, right in zip(parsed, parsed[1:]))


def _valid_public_attempt(attempt, *, source_sha256: str) -> bool:
    if not isinstance(attempt, dict):
        return False
    state = attempt.get("state")
    expected_keys = set(PUBLIC_ATTEMPT_KEYS)
    if state == "source_upload_expired":
        expected_keys.add("expired_at")
    if set(attempt) != expected_keys:
        return False
    attempt_id = attempt.get("attempt_id")
    if (
        not isinstance(attempt_id, str)
        or ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None
        or not isinstance(source_sha256, str)
        or attempt.get("source_sha256") != source_sha256
        or SOURCE_SHA256_PATTERN.fullmatch(source_sha256) is None
        or state not in SOURCE_UPLOAD_STATES
    ):
        return False
    credential = attempt.get("credential")
    started_at = attempt.get("started_at")
    completed_at = attempt.get("completed_at")
    http_status = attempt.get("http_status")
    reason_code = attempt.get("reason_code")
    url_sha256 = attempt.get("url_sha256")
    if state == "source_upload_not_started":
        return all(
            value is None
            for value in (
                credential,
                started_at,
                completed_at,
                http_status,
                reason_code,
                url_sha256,
            )
        )
    if not _valid_credential(credential) or not _valid_timestamp(started_at):
        return False
    if state == "source_upload_started":
        return all(
            value is None
            for value in (completed_at, http_status, reason_code, url_sha256)
        )
    if (
        not _valid_timestamp(completed_at)
        or not _timestamp_order(started_at, completed_at)
        or (http_status is not None and type(http_status) is not int)
    ):
        return False
    if state == "source_upload_ready":
        return http_status == 200 and reason_code is None and _valid_hash(url_sha256)
    if state == "source_upload_rejected":
        return (
            http_status == 403
            and reason_code == "storage_capacity_exhausted"
            and url_sha256 is None
        )
    if state == "source_upload_unknown":
        return reason_code in UNKNOWN_REASON_CODES and url_sha256 is None
    if state == "source_upload_expired":
        expired_at = attempt.get("expired_at")
        return (
            http_status == 200
            and reason_code == "source_url_expired"
            and _valid_hash(url_sha256)
            and _valid_timestamp(expired_at)
            and _timestamp_order(completed_at, expired_at)
        )
    return False


def _valid_pending_action(value, *, manifest: dict, attempt: dict) -> bool:
    if value is None:
        return True
    kinds = {
        "source_upload_unknown": "resolve_source_upload_unknown",
        "source_upload_rejected": "retry_source_upload",
        "source_upload_expired": "retry_expired_source_upload",
    }
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "action_id", "generation", "evidence_hash"}
        and value.get("kind") == kinds.get(attempt.get("state"))
        and isinstance(value.get("action_id"), str)
        and ACTION_ID_PATTERN.fullmatch(value["action_id"]) is not None
        and value.get("generation") == manifest.get("generation")
        and value.get("evidence_hash") == object_hash(attempt)
    )


def _valid_wait_record(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        == {"decided_at", "wait_until", "action_id", "evidence_hash", "basis_sha256"}
        and _valid_timestamp(value.get("decided_at"))
        and _valid_timestamp(value.get("wait_until"))
        and _timestamp_order(value["decided_at"], value["wait_until"])
        and isinstance(value.get("action_id"), str)
        and ACTION_ID_PATTERN.fullmatch(value["action_id"]) is not None
        and _valid_hash(value.get("evidence_hash"))
        and _valid_hash(value.get("basis_sha256"))
    )


def object_hash(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (AttributeError, ValueError) as exc:
        raise SourceStagingError(
            "integrity_violation", "A source staging timestamp is invalid."
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise SourceStagingError(
            "integrity_violation", "A source staging timestamp is invalid."
        )
    return parsed


def _isoformat(moment: datetime) -> str:
    value = moment.isoformat()
    return value[:-6] + "Z" if value.endswith("+00:00") else value


def _attempt_id(number: int) -> str:
    return f"source-upload-{number:04d}"


def _public_attempt(
    *,
    attempt_id: str,
    state: str,
    source_sha256: str,
    credential: dict,
    started_at: str,
    completed_at=None,
    http_status=None,
    reason_code=None,
    url_sha256=None,
) -> dict:
    return {
        "attempt_id": attempt_id,
        "state": state,
        "source_sha256": source_sha256,
        "credential": None if credential is None else dict(credential),
        "started_at": started_at,
        "completed_at": completed_at,
        "http_status": http_status,
        "reason_code": reason_code,
        "url_sha256": url_sha256,
    }


def _private_attempt(public: dict, *, url=None, expires_at=None) -> dict:
    return {**deepcopy(public), "url": url, "expires_at": expires_at}


def _pending_action(manifest: dict, attempt: dict) -> dict | None:
    if (
        manifest["settings_snapshot"]["interaction_mode"] != "confirm"
        and attempt.get("state") != "source_upload_expired"
    ):
        return None
    kinds = {
        "source_upload_unknown": "resolve_source_upload_unknown",
        "source_upload_rejected": "retry_source_upload",
        "source_upload_expired": "retry_expired_source_upload",
    }
    kind = kinds.get(attempt.get("state"))
    if kind is None:
        return None
    evidence_hash = object_hash(attempt)
    return {
        "kind": kind,
        "action_id": f"source-upload-decision-{secrets.token_hex(16)}",
        "generation": manifest["generation"],
        "evidence_hash": evidence_hash,
    }


def apply_settings_override_transition(previous: dict, updated: dict) -> dict:
    transitioned = deepcopy(updated)
    staging = transitioned.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return transitioned
    active = attempts[-1]
    state = active.get("state")
    mode = transitioned.get("settings_snapshot", {}).get("interaction_mode")
    if staging.get("wait_until") is not None:
        staging["pending_action"] = None
        return transitioned
    if state in {"source_upload_unknown", "source_upload_rejected"}:
        if mode == "auto":
            staging["pending_action"] = None
            return transitioned
        if mode != "confirm":
            return transitioned
    elif state != "source_upload_expired":
        staging["pending_action"] = None
        return transitioned
    existing = staging.get("pending_action")
    if isinstance(existing, dict):
        rebound = deepcopy(existing)
        rebound["generation"] = transitioned["generation"]
        staging["pending_action"] = rebound
        return transitioned
    evidence_hash = object_hash(active)
    material = (
        f"settings-override:{transitioned['generation']}:{evidence_hash}".encode("ascii")
    )
    kind = {
        "source_upload_unknown": "resolve_source_upload_unknown",
        "source_upload_rejected": "retry_source_upload",
        "source_upload_expired": "retry_expired_source_upload",
    }[state]
    staging["pending_action"] = {
        "kind": kind,
        "action_id": "source-upload-decision-"
        + hashlib.sha256(material).hexdigest()[:32],
        "generation": transitioned["generation"],
        "evidence_hash": evidence_hash,
    }
    return transitioned


def _assert_base_state(manifest: dict, private_state: dict) -> None:
    source = manifest.get("source")
    if (
        manifest.get("conversion_state") != "ready_to_submit"
        or not isinstance(source, dict)
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        or private_state.get("generation") != manifest.get("generation")
        or not isinstance(private_state.get("source_uploads"), list)
        or private_state.get("result_urls") != []
    ):
        raise SourceStagingError(
            "invalid_state_transition",
            "The work bundle is not ready for source staging.",
        )


def begin_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    credential: config.Credential,
    at: str,
) -> tuple[dict, dict, dict]:
    _assert_base_state(manifest, private_state)
    source_staging = manifest.get("source_staging")
    existing_attempt_id = None
    if source_staging is None:
        previous_attempts = []
    elif (
        isinstance(source_staging, dict)
        and source_staging.get("state") == "source_upload_not_started"
        and isinstance(source_staging.get("attempts"), list)
        and source_staging["attempts"]
        and source_staging["attempts"][-1].get("state")
        == "source_upload_not_started"
        and source_staging.get("pending_action") is None
    ):
        previous_attempts = deepcopy(source_staging["attempts"][:-1])
        existing_attempt_id = source_staging["attempts"][-1].get("attempt_id")
    elif (
        isinstance(source_staging, dict)
        and source_staging.get("state")
        in {"source_upload_rejected", "source_upload_expired", "source_upload_unknown"}
        and isinstance(source_staging.get("attempts"), list)
        and source_staging.get("pending_action") is None
    ):
        previous_attempts = deepcopy(source_staging["attempts"])
    else:
        raise SourceStagingError(
            "invalid_state_transition",
            "The active source staging attempt cannot be replayed.",
        )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    attempt_id = existing_attempt_id or _attempt_id(len(previous_attempts) + 1)
    attempt = _public_attempt(
        attempt_id=attempt_id,
        state="source_upload_started",
        source_sha256=manifest["source"]["sha256"],
        credential=credential.public_identity,
        started_at=at,
    )
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["source_staging"] = {
        "state": "source_upload_started",
        "active_attempt_id": attempt_id,
        "attempts": [*previous_attempts, attempt],
        "pending_action": None,
        **(
            {"wait_history": deepcopy(source_staging["wait_history"])}
            if isinstance(source_staging, dict)
            and isinstance(source_staging.get("wait_history"), list)
            else {}
        ),
    }
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    private_prefix = (
        private_state["source_uploads"][:-1]
        if existing_attempt_id is not None
        else private_state["source_uploads"]
    )
    updated_private["source_uploads"] = [*private_prefix, _private_attempt(attempt)]
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "The source staging intent is invalid."
        )
    operation_id = f"{attempt_id}-start"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "source_upload_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "source_sha256": manifest["source"]["sha256"],
        "attempt": attempt,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_started",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest, updated_private, attempt


def _state_after_result(
    *,
    manifest: dict,
    private_state: dict,
    completed: dict,
    url,
    expires_at,
    pending_action,
) -> tuple[dict, dict]:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    private_attempts = private_state.get("source_uploads")
    if (
        not isinstance(attempts, list)
        or not attempts
        or not isinstance(private_attempts, list)
        or len(private_attempts) != len(attempts)
    ):
        raise SourceStagingError(
            "integrity_violation", "The source staging result has no valid base."
        )
    new_generation = manifest["generation"] + 1
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_staging = {
        "state": completed["state"],
        "active_attempt_id": completed["attempt_id"],
        "attempts": [*deepcopy(attempts[:-1]), deepcopy(completed)],
        "pending_action": deepcopy(pending_action),
        **(
            {"wait_history": deepcopy(staging["wait_history"])}
            if isinstance(staging.get("wait_history"), list)
            else {}
        ),
    }
    updated_manifest["source_staging"] = updated_staging
    if completed["state"] == "source_upload_rejected":
        updated_manifest["conversion_state"] = "recoverable_error"
    elif completed["state"] == "source_upload_unknown":
        updated_manifest["conversion_state"] = "awaiting_user"
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    updated_private["source_uploads"] = [
        *deepcopy(private_attempts[:-1]),
        _private_attempt(completed, url=url, expires_at=expires_at),
    ]
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "The source staging result is invalid."
        )
    return updated_manifest, updated_private


def finish_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    result: aihub_upload.UploadResult,
    at: str,
    expires_at: str | None,
) -> tuple[dict, dict]:
    source_staging = manifest.get("source_staging")
    attempts = source_staging.get("attempts") if isinstance(source_staging, dict) else None
    private_attempts = private_state.get("source_uploads")
    if (
        source_staging is None
        or source_staging.get("state") != "source_upload_started"
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(private_attempts, list)
        or len(private_attempts) != len(attempts)
        or private_state.get("generation") != manifest.get("generation")
        or result.state
        not in {
            "source_upload_ready",
            "source_upload_rejected",
            "source_upload_unknown",
        }
    ):
        raise SourceStagingError(
            "invalid_state_transition", "The source staging result is not applicable."
        )
    previous = attempts[-1]
    completed = _public_attempt(
        attempt_id=previous["attempt_id"],
        state=result.state,
        source_sha256=previous["source_sha256"],
        credential=previous["credential"],
        started_at=previous["started_at"],
        completed_at=at,
        http_status=result.http_status,
        reason_code=result.reason_code,
        url_sha256=result.url_sha256,
    )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    pending_action = None
    if result.state in {"source_upload_rejected", "source_upload_unknown"}:
        action_manifest = deepcopy(manifest)
        action_manifest["generation"] = new_generation
        pending_action = _pending_action(action_manifest, completed)
    updated_manifest, updated_private = _state_after_result(
        manifest=manifest,
        private_state=private_state,
        completed=completed,
        url=result.url,
        expires_at=expires_at,
        pending_action=pending_action,
    )
    recovery_unknown_action = None
    if result.state == "source_upload_ready":
        lost = _public_attempt(
            attempt_id=completed["attempt_id"],
            state="source_upload_unknown",
            source_sha256=completed["source_sha256"],
            credential=completed["credential"],
            started_at=completed["started_at"],
            completed_at=completed["completed_at"],
            http_status=completed["http_status"],
            reason_code="result_private_payload_lost",
        )
        action_manifest = deepcopy(manifest)
        action_manifest["generation"] = new_generation
        recovery_unknown_action = _pending_action(action_manifest, lost)
    operation_id = f"{completed['attempt_id']}-result"
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_result_intent",
            "operation_id": operation_id,
            "expected_generation": expected_generation,
            "new_generation": new_generation,
            "at": at,
            "attempt": completed,
            "pending_action": pending_action,
            "recovery_unknown_action": recovery_unknown_action,
            "previous_manifest_hash": object_hash(manifest),
            "previous_private_hash": object_hash(private_state),
        },
        state_fd=descriptors["state"],
    )
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_result_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest, updated_private


def ready_is_expired(
    *, manifest: dict, private_state: dict, now: datetime
) -> bool:
    staging = manifest.get("source_staging")
    uploads = private_state.get("source_uploads")
    if (
        not isinstance(staging, dict)
        or staging.get("state") != "source_upload_ready"
        or not isinstance(uploads, list)
        or not uploads
        or uploads[-1].get("attempt_id") != staging.get("active_attempt_id")
        or not isinstance(uploads[-1].get("expires_at"), str)
    ):
        raise SourceStagingError(
            "integrity_violation", "The ready source staging expiry is incomplete."
        )
    value = uploads[-1]["expires_at"]
    expires_at = _parse_timestamp(value)
    if (
        expires_at.tzinfo is None
        or expires_at.utcoffset() is None
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise SourceStagingError(
            "integrity_violation", "The ready source staging expiry is invalid."
        )
    return now >= expires_at


def expire_ready_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    at: str,
) -> tuple[dict, dict]:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    uploads = private_state.get("source_uploads")
    if (
        manifest.get("conversion_state") != "ready_to_submit"
        or not isinstance(staging, dict)
        or staging.get("state") != "source_upload_ready"
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(uploads, list)
        or len(uploads) != len(attempts)
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise SourceStagingError(
            "invalid_state_transition", "The ready source upload cannot expire."
        )
    expired = deepcopy(attempts[-1])
    expired["state"] = "source_upload_expired"
    expired["reason_code"] = "source_url_expired"
    expired["expired_at"] = at
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = "recoverable_error"
    updated_staging = {
        "state": "source_upload_expired",
        "active_attempt_id": expired["attempt_id"],
        "attempts": [*deepcopy(attempts[:-1]), expired],
        "pending_action": None,
        **(
            {"wait_history": deepcopy(staging["wait_history"])}
            if isinstance(staging.get("wait_history"), list)
            else {}
        ),
    }
    updated_manifest["source_staging"] = updated_staging
    updated_staging["pending_action"] = _pending_action(updated_manifest, expired)
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    expired_private = deepcopy(uploads[-1])
    expired_private.update(expired)
    updated_private["source_uploads"] = [
        *deepcopy(uploads[:-1]),
        expired_private,
    ]
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "The expired source staging state is invalid."
        )
    operation_id = f"{expired['attempt_id']}-expiry"
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_expiry_intent",
            "operation_id": operation_id,
            "expected_generation": expected_generation,
            "new_generation": new_generation,
            "at": at,
            "attempt": expired,
            "pending_action": deepcopy(updated_staging["pending_action"]),
            "previous_manifest_hash": object_hash(manifest),
            "previous_private_hash": object_hash(private_state),
        },
        state_fd=descriptors["state"],
    )
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_expiry_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest, updated_private


def _started_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    attempt = intent.get("attempt")
    source = manifest.get("source")
    if (
        set(intent)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "source_sha256",
            "attempt",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "source_upload_intent"
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or not isinstance(source, dict)
        or not _valid_public_attempt(attempt, source_sha256=source.get("sha256"))
        or attempt.get("state") != "source_upload_started"
        or intent.get("operation_id") != f"{attempt.get('attempt_id')}-start"
        or intent.get("source_sha256") != source.get("sha256")
        or not _valid_timestamp(intent.get("at"))
        or not _valid_hash(intent.get("previous_manifest_hash"))
        or not _valid_hash(intent.get("previous_private_hash"))
        or manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
    ):
        raise SourceStagingError(
            "integrity_violation", "A pending source staging intent is inconsistent."
        )
    previous_staging = manifest.get("source_staging")
    if previous_staging is None:
        manifest_prefix = []
        private_prefix = private_state.get("source_uploads")
    elif (
        isinstance(previous_staging, dict)
        and previous_staging.get("state") == "source_upload_not_started"
        and isinstance(previous_staging.get("attempts"), list)
        and bool(previous_staging["attempts"])
        and isinstance(previous_staging["attempts"][-1], dict)
        and previous_staging["attempts"][-1].get("attempt_id") == attempt.get("attempt_id")
    ):
        manifest_prefix = previous_staging["attempts"][:-1]
        private_prefix = private_state.get("source_uploads", [])[:-1]
    else:
        raise SourceStagingError(
            "integrity_violation", "A pending source staging intent has no valid base."
        )
    if not isinstance(private_prefix, list):
        raise SourceStagingError(
            "integrity_violation", "A pending source staging intent has no private base."
        )
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["source_staging"] = {
        "state": "source_upload_started",
        "active_attempt_id": attempt["attempt_id"],
        "attempts": [*deepcopy(manifest_prefix), deepcopy(attempt)],
        "pending_action": None,
        **(
            {"wait_history": deepcopy(previous_staging["wait_history"])}
            if isinstance(previous_staging, dict)
            and isinstance(previous_staging.get("wait_history"), list)
            else {}
        ),
    }
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    desired_private["source_uploads"] = [
        *deepcopy(private_prefix),
        _private_attempt(attempt),
    ]
    if not valid_private_state(desired_private, desired_manifest):
        raise SourceStagingError(
            "integrity_violation", "A pending source staging intent is invalid."
        )
    return desired_manifest, desired_private


def recover_interrupted_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    at: str,
    expected_generation: int,
) -> tuple[dict, dict] | None:
    history = bundle.read_history(state_fd=descriptors["state"])
    final_event = history[-1] if history else None
    source_intents = {
        "source_upload_intent",
        "source_upload_result_intent",
        "source_upload_expiry_intent",
        "source_upload_decision_intent",
        "source_upload_wait_elapsed_intent",
    }
    recovered_start = False
    if isinstance(final_event, dict) and final_event.get("event") in source_intents:
        intent_expected = final_event.get("expected_generation")
        intent_new = final_event.get("new_generation")
        if expected_generation not in {intent_expected, intent_new}:
            raise SourceStagingError(
                "generation_conflict",
                "Expected generation does not match the pending source operation.",
            )
        prefix = history[:-1]
        if any(
            isinstance(event, dict)
            and event.get("event") == "source_upload_intent"
            for event in prefix
        ):
            previous = reduce_history(prefix, private_template=private_state)
        else:
            previous = preflight.reduce_preflight_history(prefix)
        if previous is None:
            raise SourceStagingError(
                "integrity_violation", "A pending source operation has no valid history prefix."
            )
        previous_manifest, previous_private = previous
        event = final_event["event"]
        if event == "source_upload_intent":
            desired_manifest, desired_private = _started_state_from_intent(
                previous_manifest, previous_private, final_event
            )
            committed_event = "source_upload_started"
            recovered_start = True
        elif event == "source_upload_result_intent":
            completed = final_event.get("attempt")
            saved_uploads = private_state.get("source_uploads")
            saved = (
                saved_uploads[-1]
                if isinstance(saved_uploads, list)
                and saved_uploads
                and isinstance(saved_uploads[-1], dict)
                else None
            )
            recovered_unknown = (
                isinstance(completed, dict)
                and completed.get("state") == "source_upload_ready"
                and not (
                    private_state.get("generation") == intent_new
                    and isinstance(saved, dict)
                    and saved.get("state") == "source_upload_ready"
                )
            )
            desired_manifest, desired_private = _result_state_from_intent(
                previous_manifest,
                previous_private,
                final_event,
                private_payload=saved,
                recovered_unknown=recovered_unknown,
            )
            committed_event = (
                "source_upload_result_recovered_unknown"
                if recovered_unknown
                else "source_upload_result_committed"
            )
        elif event == "source_upload_expiry_intent":
            desired_manifest, desired_private = _expired_state_from_intent(
                previous_manifest, previous_private, final_event
            )
            committed_event = "source_upload_expiry_committed"
        elif event == "source_upload_decision_intent":
            desired_manifest, desired_private = _decision_state_from_intent(
                previous_manifest, previous_private, final_event
            )
            committed_event = "source_upload_decision_committed"
        else:
            desired_manifest, desired_private = _wait_state_from_intent(
                previous_manifest, previous_private, final_event
            )
            committed_event = "source_upload_wait_elapsed_committed"
        manifest_is_previous = manifest == previous_manifest
        manifest_is_desired = manifest == desired_manifest
        private_is_previous = private_state == previous_private
        private_is_desired = private_state == desired_private
        if (
            not (manifest_is_previous or manifest_is_desired)
            or not (private_is_previous or private_is_desired)
            or (manifest_is_desired and private_is_previous)
        ):
            raise SourceStagingError(
                "integrity_violation", "A pending source operation is partially inconsistent."
            )
        if private_is_previous:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        if manifest_is_previous:
            bundle.atomic_write_json(
                "manifest.json", desired_manifest, dir_fd=descriptors["root"]
            )
        bundle.append_history(
            {
                "schema_version": SCHEMA_VERSION,
                "event": committed_event,
                "operation_id": final_event["operation_id"],
                "previous_generation": intent_expected,
                "generation": intent_new,
                "at": at,
                "manifest_hash": object_hash(desired_manifest),
                "private_hash": object_hash(desired_private),
            },
            state_fd=descriptors["state"],
        )
        manifest, private_state = desired_manifest, desired_private
        if not recovered_start:
            return manifest, private_state
    staging = manifest.get("source_staging")
    if (
        isinstance(staging, dict)
        and staging.get("state") == "source_upload_started"
    ):
        if not recovered_start and expected_generation != manifest.get("generation"):
            raise SourceStagingError(
                "generation_conflict",
                "Expected generation does not match the source staging state.",
            )
        return finish_attempt(
            descriptors=descriptors,
            manifest=manifest,
            private_state=private_state,
            result=aihub_upload.UploadResult(
                "source_upload_unknown",
                None,
                "interrupted_before_result_commit",
                None,
                None,
            ),
            at=at,
            expires_at=None,
        )
    return None


def commit_decision(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    decision: str,
    basis: str,
    at: str,
) -> dict:
    staging = manifest.get("source_staging")
    pending = staging.get("pending_action") if isinstance(staging, dict) else None
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    current_state = staging.get("state") if isinstance(staging, dict) else None
    expected_conversion = (
        "awaiting_user"
        if current_state == "source_upload_unknown"
        else "recoverable_error"
    )
    allowed_action = {
        "source_upload_unknown": "resolve_source_upload_unknown",
        "source_upload_rejected": "retry_source_upload",
        "source_upload_expired": "retry_expired_source_upload",
    }.get(current_state)
    if (
        manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or manifest.get("conversion_state") != expected_conversion
        or current_state
        not in {
            "source_upload_unknown",
            "source_upload_rejected",
            "source_upload_expired",
        }
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(pending, dict)
        or pending.get("kind") != allowed_action
        or pending.get("generation") != expected_generation
        or pending.get("action_id") != action_id
        or pending.get("evidence_hash") != evidence_hash
        or decision not in {"retry", "wait"}
        or (decision == "wait" and current_state != "source_upload_unknown")
        or not isinstance(basis, str)
        or not basis.strip()
    ):
        raise SourceStagingError(
            "source_upload_action_mismatch",
            "The source staging decision does not match the pending action.",
        )
    new_generation = expected_generation + 1
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if decision == "retry":
        new_attempt = _public_attempt(
            attempt_id=_attempt_id(len(attempts) + 1),
            state="source_upload_not_started",
            source_sha256=manifest["source"]["sha256"],
            credential=None,
            started_at=None,
        )
        updated_manifest["source_staging"] = {
            "state": "source_upload_not_started",
            "active_attempt_id": new_attempt["attempt_id"],
            "attempts": [*deepcopy(attempts), new_attempt],
            "pending_action": None,
            **(
                {"wait_history": deepcopy(staging["wait_history"])}
                if isinstance(staging.get("wait_history"), list)
                else {}
            ),
        }
        updated_manifest["conversion_state"] = "ready_to_submit"
        updated_private["source_uploads"] = [
            *private_state["source_uploads"],
            _private_attempt(new_attempt),
        ]
    else:
        updated_staging = deepcopy(staging)
        updated_staging["pending_action"] = None
        wait_until = _isoformat(_parse_timestamp(at) + timedelta(hours=72))
        wait_record = {
            "decided_at": at,
            "wait_until": wait_until,
            "action_id": action_id,
            "evidence_hash": evidence_hash,
            "basis_sha256": f"sha256:{hashlib.sha256(basis.strip().encode('utf-8')).hexdigest()}",
        }
        updated_staging["wait_until"] = wait_until
        updated_staging["wait_history"] = [
            *deepcopy(staging.get("wait_history", [])),
            wait_record,
        ]
        updated_manifest["source_staging"] = updated_staging
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "The source staging decision is invalid."
        )
    operation_id = f"{attempts[-1]['attempt_id']}-decision-{new_generation}"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "source_upload_decision_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "decision": decision,
        "basis_sha256": f"sha256:{hashlib.sha256(basis.strip().encode('utf-8')).hexdigest()}",
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_decision_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def unknown_wait_has_elapsed(*, manifest: dict, now: datetime) -> bool:
    staging = manifest.get("source_staging")
    wait_until = staging.get("wait_until") if isinstance(staging, dict) else None
    if (
        staging is None
        or staging.get("state") != "source_upload_unknown"
        or staging.get("pending_action") is not None
        or not isinstance(wait_until, str)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise SourceStagingError(
            "integrity_violation", "The source staging wait state is invalid."
        )
    return now >= _parse_timestamp(wait_until)


def renew_unknown_action(
    *, descriptors: dict, manifest: dict, private_state: dict, at: str
) -> dict:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    if (
        manifest.get("conversion_state") != "awaiting_user"
        or not isinstance(staging, dict)
        or staging.get("state") != "source_upload_unknown"
        or staging.get("pending_action") is not None
        or not isinstance(staging.get("wait_until"), str)
        or not isinstance(attempts, list)
        or not attempts
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise SourceStagingError(
            "invalid_state_transition", "The source staging wait cannot be renewed."
        )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_staging = deepcopy(staging)
    updated_staging.pop("wait_until", None)
    updated_staging["pending_action"] = _pending_action(
        updated_manifest, attempts[-1]
    )
    updated_manifest["source_staging"] = updated_staging
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "The renewed source staging action is invalid."
        )
    operation_id = f"{attempts[-1]['attempt_id']}-wait-{new_generation}"
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_wait_elapsed_intent",
            "operation_id": operation_id,
            "expected_generation": expected_generation,
            "new_generation": new_generation,
            "at": at,
            "attempt_id": attempts[-1]["attempt_id"],
            "pending_action": deepcopy(updated_staging["pending_action"]),
            "previous_manifest_hash": object_hash(manifest),
            "previous_private_hash": object_hash(private_state),
        },
        state_fd=descriptors["state"],
    )
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "source_upload_wait_elapsed_committed",
            "operation_id": operation_id,
            "previous_generation": expected_generation,
            "generation": new_generation,
            "at": at,
            "manifest_hash": object_hash(updated_manifest),
            "private_hash": object_hash(updated_private),
        },
        state_fd=descriptors["state"],
    )
    return updated_manifest


def open_frozen_source(*, source_fd: int, manifest: dict) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open("source.pdf", flags, dir_fd=source_fd)
    except OSError as exc:
        raise SourceStagingError(
            "integrity_violation", "The frozen source PDF is missing or unsafe."
        ) from exc
    opened = os.fstat(descriptor)
    source = manifest.get("source")
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or not isinstance(source, dict)
        or opened.st_size != source.get("size_bytes")
    ):
        os.close(descriptor)
        raise SourceStagingError(
            "integrity_violation", "The frozen source PDF is missing or unsafe."
        )
    return descriptor


def valid_private_state(private_state: dict, manifest: dict) -> bool:
    uploads = private_state.get("source_uploads")
    source_staging = manifest.get("source_staging")
    summaries = source_staging.get("attempts") if isinstance(source_staging, dict) else None
    source = manifest.get("source")
    if (
        not isinstance(private_state, dict)
        or set(private_state)
        != {"schema_version", "generation", "source_uploads", "result_urls"}
        or private_state.get("schema_version") != SCHEMA_VERSION
        or not isinstance(uploads, list)
        or not isinstance(source_staging, dict)
        or not isinstance(source, dict)
        or SOURCE_SHA256_PATTERN.fullmatch(str(source.get("sha256", ""))) is None
        or not isinstance(summaries, list)
        or len(uploads) != len(summaries)
        or not uploads
        or private_state.get("generation") != manifest.get("generation")
        or private_state.get("result_urls") != []
    ):
        return False
    allowed_envelope_keys = {
        "state",
        "active_attempt_id",
        "attempts",
        "pending_action",
        "wait_until",
        "wait_history",
    }
    required_envelope_keys = {
        "state",
        "active_attempt_id",
        "attempts",
        "pending_action",
    }
    if (
        not required_envelope_keys.issubset(source_staging)
        or not set(source_staging).issubset(allowed_envelope_keys)
    ):
        return False
    for number, (upload, summary) in enumerate(zip(uploads, summaries), start=1):
        if (
            not _valid_public_attempt(summary, source_sha256=source["sha256"])
            or summary.get("attempt_id") != _attempt_id(number)
            or not isinstance(upload, dict)
            or set(upload) != set(summary) | set(PRIVATE_ONLY_ATTEMPT_KEYS)
        ):
            return False
        if {key: upload[key] for key in summary} != summary:
            return False
        state = summary.get("state")
        url = upload.get("url")
        expires_at = upload.get("expires_at")
        if state in {"source_upload_ready", "source_upload_expired"}:
            if (
                not aihub_upload.valid_https_url(url)
                or not _valid_timestamp(expires_at)
                or summary.get("url_sha256")
                != f"sha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"
                or _parse_timestamp(expires_at)
                != _parse_timestamp(summary["completed_at"]) + timedelta(hours=72)
                or (
                    state == "source_upload_expired"
                    and not _timestamp_order(expires_at, summary["expired_at"])
                )
            ):
                return False
        elif url is not None or expires_at is not None:
            return False
    active = summaries[-1]
    expected_conversion = {
        "source_upload_not_started": "ready_to_submit",
        "source_upload_started": "ready_to_submit",
        "source_upload_ready": "ready_to_submit",
        "source_upload_rejected": "recoverable_error",
        "source_upload_unknown": "awaiting_user",
        "source_upload_expired": "recoverable_error",
    }
    pending = source_staging.get("pending_action")
    if (
        source_staging.get("state") != active.get("state")
        or source_staging.get("active_attempt_id") != active.get("attempt_id")
        or manifest.get("conversion_state") != expected_conversion.get(active.get("state"))
        or not _valid_pending_action(pending, manifest=manifest, attempt=active)
    ):
        return False
    wait_history = source_staging.get("wait_history")
    wait_until = source_staging.get("wait_until")
    if wait_history is not None:
        if (
            not isinstance(wait_history, list)
            or not wait_history
            or not all(_valid_wait_record(record) for record in wait_history)
        ):
            return False
    if wait_until is not None:
        if (
            active.get("state") != "source_upload_unknown"
            or pending is not None
            or not _valid_timestamp(wait_until)
            or not isinstance(wait_history, list)
            or wait_history[-1].get("wait_until") != wait_until
        ):
            return False
    return True


def _valid_committed_event(
    committed,
    *,
    intent: dict,
    expected_events: set[str],
    desired_manifest: dict,
    desired_private: dict,
) -> bool:
    return (
        isinstance(committed, dict)
        and set(committed)
        == {
            "schema_version",
            "event",
            "operation_id",
            "previous_generation",
            "generation",
            "at",
            "manifest_hash",
            "private_hash",
        }
        and committed.get("schema_version") == SCHEMA_VERSION
        and committed.get("event") in expected_events
        and committed.get("operation_id") == intent.get("operation_id")
        and committed.get("previous_generation")
        == intent.get("expected_generation")
        and committed.get("generation") == intent.get("new_generation")
        and _valid_timestamp(committed.get("at"))
        and committed.get("manifest_hash") == object_hash(desired_manifest)
        and committed.get("private_hash") == object_hash(desired_private)
    )


def _expired_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    uploads = private_state.get("source_uploads")
    expired = intent.get("attempt")
    source = manifest.get("source")
    if (
        set(intent)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "attempt",
            "pending_action",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "source_upload_expiry_intent"
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or not _valid_timestamp(intent.get("at"))
        or not isinstance(source, dict)
        or not _valid_public_attempt(expired, source_sha256=source.get("sha256"))
        or expired.get("state") != "source_upload_expired"
        or intent.get("operation_id") != f"{expired.get('attempt_id')}-expiry"
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(uploads, list)
        or len(uploads) != len(attempts)
        or attempts[-1].get("state") != "source_upload_ready"
        or attempts[-1].get("attempt_id") != expired.get("attempt_id")
    ):
        raise SourceStagingError(
            "integrity_violation", "A source staging expiry intent is invalid."
        )
    expected_expired = deepcopy(attempts[-1])
    expected_expired.update(
        {
            "state": "source_upload_expired",
            "reason_code": "source_url_expired",
            "expired_at": intent["at"],
        }
    )
    if expired != expected_expired:
        raise SourceStagingError(
            "integrity_violation", "A source staging expiry attempt is invalid."
        )
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = intent["new_generation"]
    updated_manifest["conversion_state"] = "recoverable_error"
    updated_staging = deepcopy(staging)
    updated_staging["state"] = "source_upload_expired"
    updated_staging["attempts"] = [*deepcopy(attempts[:-1]), deepcopy(expired)]
    updated_staging["pending_action"] = deepcopy(intent.get("pending_action"))
    updated_manifest["source_staging"] = updated_staging
    updated_private = deepcopy(private_state)
    updated_private["generation"] = intent["new_generation"]
    expired_private = deepcopy(uploads[-1])
    expired_private.update(expired)
    updated_private["source_uploads"] = [
        *deepcopy(uploads[:-1]),
        expired_private,
    ]
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "A source staging expiry state is invalid."
        )
    return updated_manifest, updated_private


def _result_state_from_intent(
    manifest: dict,
    private_state: dict,
    intent: dict,
    *,
    private_payload=None,
    recovered_unknown: bool,
) -> tuple[dict, dict]:
    completed = intent.get("attempt")
    source = manifest.get("source")
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    if (
        set(intent)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "attempt",
            "pending_action",
            "recovery_unknown_action",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "source_upload_result_intent"
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or not _valid_timestamp(intent.get("at"))
        or not isinstance(source, dict)
        or not _valid_public_attempt(completed, source_sha256=source.get("sha256"))
        or completed.get("state")
        not in {
            "source_upload_ready",
            "source_upload_rejected",
            "source_upload_unknown",
        }
        or intent.get("operation_id") != f"{completed.get('attempt_id')}-result"
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
        or not isinstance(attempts, list)
        or not attempts
        or attempts[-1].get("state") != "source_upload_started"
        or attempts[-1].get("attempt_id") != completed.get("attempt_id")
    ):
        raise SourceStagingError(
            "integrity_violation", "A source staging result intent is invalid."
        )
    effective = completed
    pending_action = intent.get("pending_action")
    url = expires_at = None
    if recovered_unknown:
        if completed["state"] != "source_upload_ready":
            raise SourceStagingError(
                "integrity_violation", "A source staging result recovery is invalid."
            )
        effective = _public_attempt(
            attempt_id=completed["attempt_id"],
            state="source_upload_unknown",
            source_sha256=completed["source_sha256"],
            credential=completed["credential"],
            started_at=completed["started_at"],
            completed_at=completed["completed_at"],
            http_status=completed["http_status"],
            reason_code="result_private_payload_lost",
        )
        pending_action = intent.get("recovery_unknown_action")
    elif completed["state"] == "source_upload_ready":
        if not isinstance(private_payload, dict):
            raise SourceStagingError(
                "integrity_violation", "A ready source staging result has no private payload."
            )
        url = private_payload.get("url")
        expires_at = private_payload.get("expires_at")
    return _state_after_result(
        manifest=manifest,
        private_state=private_state,
        completed=effective,
        url=url,
        expires_at=expires_at,
        pending_action=pending_action,
    )


def _decision_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    pending = staging.get("pending_action") if isinstance(staging, dict) else None
    decision = intent.get("decision")
    if (
        set(intent)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "action_id",
            "evidence_hash",
            "decision",
            "basis_sha256",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "source_upload_decision_intent"
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or not _valid_timestamp(intent.get("at"))
        or not _valid_hash(intent.get("evidence_hash"))
        or not _valid_hash(intent.get("basis_sha256"))
        or decision not in {"retry", "wait"}
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(pending, dict)
        or pending.get("action_id") != intent.get("action_id")
        or pending.get("evidence_hash") != intent.get("evidence_hash")
        or (decision == "wait" and staging.get("state") != "source_upload_unknown")
        or intent.get("operation_id")
        != f"{attempts[-1].get('attempt_id')}-decision-{intent.get('new_generation')}"
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
    ):
        raise SourceStagingError(
            "integrity_violation", "A source staging decision intent is invalid."
        )
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = intent["new_generation"]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = intent["new_generation"]
    if decision == "retry":
        new_attempt = _public_attempt(
            attempt_id=_attempt_id(len(attempts) + 1),
            state="source_upload_not_started",
            source_sha256=manifest["source"]["sha256"],
            credential=None,
            started_at=None,
        )
        updated_manifest["source_staging"] = {
            "state": "source_upload_not_started",
            "active_attempt_id": new_attempt["attempt_id"],
            "attempts": [*deepcopy(attempts), new_attempt],
            "pending_action": None,
            **(
                {"wait_history": deepcopy(staging["wait_history"])}
                if isinstance(staging.get("wait_history"), list)
                else {}
            ),
        }
        updated_manifest["conversion_state"] = "ready_to_submit"
        uploads = private_state.get("source_uploads")
        if not isinstance(uploads, list):
            raise SourceStagingError(
                "integrity_violation", "A source staging decision has no private base."
            )
        updated_private["source_uploads"] = [*deepcopy(uploads), _private_attempt(new_attempt)]
    else:
        updated_staging = deepcopy(staging)
        updated_staging["pending_action"] = None
        wait_until = _isoformat(_parse_timestamp(intent["at"]) + timedelta(hours=72))
        updated_staging["wait_until"] = wait_until
        updated_staging["wait_history"] = [
            *deepcopy(staging.get("wait_history", [])),
            {
                "decided_at": intent["at"],
                "wait_until": wait_until,
                "action_id": intent["action_id"],
                "evidence_hash": intent["evidence_hash"],
                "basis_sha256": intent["basis_sha256"],
            },
        ]
        updated_manifest["source_staging"] = updated_staging
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "A source staging decision state is invalid."
        )
    return updated_manifest, updated_private


def _wait_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    staging = manifest.get("source_staging")
    attempts = staging.get("attempts") if isinstance(staging, dict) else None
    if (
        set(intent)
        != {
            "schema_version",
            "event",
            "operation_id",
            "expected_generation",
            "new_generation",
            "at",
            "attempt_id",
            "pending_action",
            "previous_manifest_hash",
            "previous_private_hash",
        }
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "source_upload_wait_elapsed_intent"
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or not _valid_timestamp(intent.get("at"))
        or not isinstance(staging, dict)
        or staging.get("state") != "source_upload_unknown"
        or staging.get("pending_action") is not None
        or not _valid_timestamp(staging.get("wait_until"))
        or not _timestamp_order(staging["wait_until"], intent["at"])
        or not isinstance(attempts, list)
        or not attempts
        or attempts[-1].get("attempt_id") != intent.get("attempt_id")
        or intent.get("operation_id")
        != f"{intent.get('attempt_id')}-wait-{intent.get('new_generation')}"
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
    ):
        raise SourceStagingError(
            "integrity_violation", "A source staging wait intent is invalid."
        )
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = intent["new_generation"]
    updated_staging = deepcopy(staging)
    updated_staging.pop("wait_until", None)
    updated_staging["pending_action"] = deepcopy(intent.get("pending_action"))
    updated_manifest["source_staging"] = updated_staging
    updated_private = deepcopy(private_state)
    updated_private["generation"] = intent["new_generation"]
    if not valid_private_state(updated_private, updated_manifest):
        raise SourceStagingError(
            "integrity_violation", "A source staging wait state is invalid."
        )
    return updated_manifest, updated_private


def reduce_history(
    history: list[dict], *, private_template: dict
) -> tuple[dict, dict] | None:
    first = next(
        (
            index
            for index, event in enumerate(history)
            if isinstance(event, dict) and event.get("event") == "source_upload_intent"
        ),
        None,
    )
    if first is None or not isinstance(private_template, dict):
        return None
    reduced_prefix = preflight.reduce_preflight_history(history[:first])
    if reduced_prefix is None:
        return None
    current_manifest, current_private = reduced_prefix
    template_uploads = private_template.get("source_uploads")
    if not isinstance(template_uploads, list):
        return None
    payloads = {
        upload.get("attempt_id"): upload
        for upload in template_uploads
        if isinstance(upload, dict) and isinstance(upload.get("attempt_id"), str)
    }
    offset = first
    operation_ids = set()
    while offset < len(history):
        intent = history[offset]
        if not isinstance(intent, dict):
            return None
        operation_id = intent.get("operation_id")
        if not isinstance(operation_id, str) or operation_id in operation_ids:
            return None
        if intent.get("event") == "settings_override_intent":
            if offset + 2 >= len(history):
                return None
            prepared, committed = history[offset + 1 : offset + 3]
            if not all(
                _valid_timestamp(event.get("at"))
                for event in (intent, prepared, committed)
                if isinstance(event, dict)
            ):
                return None
            transition = bundle.apply_settings_override_events(
                current_manifest,
                current_private,
                intent,
                prepared,
                committed,
                manifest_transform=apply_settings_override_transition,
            )
            if transition is None:
                return None
            current_manifest, current_private = transition
            operation_ids.add(operation_id)
            offset += 3
            continue
        if offset + 1 >= len(history):
            return None
        committed = history[offset + 1]
        try:
            event = intent.get("event")
            if event == "source_upload_intent":
                desired_manifest, desired_private = _started_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"source_upload_started"}
            elif event == "source_upload_result_intent":
                completed = intent.get("attempt")
                if not isinstance(completed, dict):
                    return None
                recovered_unknown = (
                    isinstance(committed, dict)
                    and committed.get("event")
                    == "source_upload_result_recovered_unknown"
                )
                payload = None
                if recovered_unknown:
                    pass
                elif completed.get("state") == "source_upload_ready":
                    payload = payloads.get(completed.get("attempt_id"))
                    if not isinstance(payload, dict):
                        return None
                desired_manifest, desired_private = _result_state_from_intent(
                    current_manifest,
                    current_private,
                    intent,
                    private_payload=payload,
                    recovered_unknown=recovered_unknown,
                )
                expected_events = {
                    "source_upload_result_recovered_unknown"
                    if recovered_unknown
                    else "source_upload_result_committed"
                }
            elif event == "source_upload_expiry_intent":
                desired_manifest, desired_private = _expired_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"source_upload_expiry_committed"}
            elif event == "source_upload_decision_intent":
                desired_manifest, desired_private = _decision_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"source_upload_decision_committed"}
            elif event == "source_upload_wait_elapsed_intent":
                desired_manifest, desired_private = _wait_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"source_upload_wait_elapsed_committed"}
            else:
                return None
        except (KeyError, TypeError, SourceStagingError):
            return None
        if not _valid_committed_event(
            committed,
            intent=intent,
            expected_events=expected_events,
            desired_manifest=desired_manifest,
            desired_private=desired_private,
        ):
            return None
        operation_ids.add(operation_id)
        current_manifest, current_private = desired_manifest, desired_private
        offset += 2
    return current_manifest, current_private


def resolve_history_state(
    history: list[dict], *, manifest_template: dict, private_template: dict
) -> tuple[dict, dict] | None:
    del manifest_template
    return reduce_history(history, private_template=private_template)


def valid_history(history: list[dict], manifest: dict, private_state: dict) -> bool:
    if not valid_private_state(private_state, manifest):
        return False
    return reduce_history(history, private_template=private_state) == (
        manifest,
        private_state,
    )


def result_from_manifest(manifest: dict, *, work_bundle: str, outcome: str) -> dict:
    result = preflight.result_from_manifest(
        manifest, work_bundle=work_bundle, outcome=outcome
    )
    staging = manifest.get("source_staging")
    result["source_upload_state"] = (
        None if not isinstance(staging, dict) else staging.get("state")
    )
    pending_action = staging.get("pending_action") if isinstance(staging, dict) else None
    if isinstance(pending_action, dict):
        result["action_required"] = pending_action["kind"]
        result["action_id"] = pending_action["action_id"]
        result["evidence_hash"] = pending_action["evidence_hash"]
    return result
