from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta

import bundle
import doc2x
import source_staging


SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
POLL_RETRY_BASE_SECONDS = 8
POLL_WINDOW_SECONDS = 8 * 90
RESULT_PENDING_WINDOW_SECONDS = 8 * 90
API_BASE = "https://api.aihubmax.com"
ATTEMPT_ID_PATTERN = re.compile(r"conversion-attempt-(0*[1-9][0-9]*)")
ACTION_ID_PATTERN = re.compile(r"conversion-decision-[0-9a-f]{32}")
ATTEMPT_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "state",
        "api_base",
        "request_summary",
        "request_hash",
        "credential",
        "staging_identity",
        "submitted_at",
        "response_at",
        "http_status",
        "reason_code",
        "task_id",
        "pending_action",
        "authorization",
        "poll_started_at",
        "poll_deadline_at",
        "last_polled_at",
        "poll_count",
        "upstream_status",
        "next_poll_at",
        "consecutive_transient_count",
        "result_url_sha256",
        "result_observed_at",
        "result_validity_hours",
        "result_pending_started_at",
        "result_pending_deadline_at",
    }
)
REQUEST_SUMMARY_KEYS = frozenset(
    {
        "model",
        "pdf_url_sha256",
        "page_count",
        "filename",
        "convert_mode",
        "formula_mode",
        "merge_cross_page_forms",
    }
)
STAGING_IDENTITY_KEYS = frozenset(
    {"attempt_id", "source_sha256", "url_sha256"}
)
PENDING_ACTION_KEYS = frozenset(
    {"kind", "action_id", "generation", "evidence_hash"}
)
AUTHORIZATION_KEYS = frozenset(
    {
        "action_id",
        "evidence_hash",
        "authorized_at",
        "basis_sha256",
        "accepted_risk",
    }
)
RESULT_URL_KEYS = frozenset(
    {
        "attempt_id",
        "task_id",
        "url",
        "url_sha256",
        "observed_at",
        "expires_at",
        "validity_window_hours",
    }
)
POLL_IMMUTABLE_ATTEMPT_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "api_base",
        "request_summary",
        "request_hash",
        "credential",
        "staging_identity",
        "submitted_at",
        "response_at",
        "task_id",
        "authorization",
    }
)
SUBMIT_INTENT_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "expected_generation",
        "new_generation",
        "at",
        "attempt",
        "previous_attempt",
        "previous_manifest_hash",
        "previous_private_hash",
    }
)
RESULT_INTENT_KEYS = frozenset(SUBMIT_INTENT_KEYS - {"previous_attempt"})
RETRY_INTENT_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "expected_generation",
        "new_generation",
        "at",
        "action_id",
        "evidence_hash",
        "basis_sha256",
        "attempt",
        "previous_manifest_hash",
        "previous_private_hash",
    }
)
COMMITTED_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "previous_generation",
        "generation",
        "at",
        "manifest_hash",
        "private_hash",
    }
)


class ConversionAttemptError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def object_hash(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def canonical_state_byte_length(value: dict) -> int:
    """Bytes a candidate manifest/private-state/history-event payload would
    occupy on disk if written now.

    Local-state capacity admission (plan.md 2.2/2.3) needs this number
    *before* writing, to decide whether to fail closed instead of writing a
    truncated file. It delegates to bundle.canonical_json_bytes -- the same
    encoder bundle.atomic_write_json/append_history use to actually persist
    state -- so the estimate can never diverge from what the writer produces.
    """
    return len(bundle.canonical_json_bytes(value))


# --- Worst-case local-state capacity admission (plan.md 2.2) ---------------
#
# create/ordinary-poll/refresh each talk to an *unvalidated* Doc2X response
# before doc2x._classify/_classify_poll have accepted or rejected it. To fail
# closed before that call rather than after, admission must assume the worst
# legal shape that response could still take: a task_id and a result URL each
# at the UTF-8 byte ceiling the spec puts on them, each inflated by
# ensure_ascii=True's worst-case \uXXXX escaping. This module
# only computes and judges those worst cases -- plan.md 2.3 is responsible
# for wiring the verdict into a stop-before-intent behavior.

# Spec's UTF-8 byte upper bound for an unvalidated response task_id. This is
# intentionally *not* doc2x.TASK_ID_PATTERN's 256-char/charset-restricted
# match (doc2x.py:18): that pattern is a stricter, fail-closed *validator*
# applied only after a response is trusted enough to classify, whereas
# admission must bound what an as-yet-unclassified response could contain.
TASK_ID_UPPER_BOUND_BYTES = 4096

# Spec's UTF-8 byte upper bound for a response result URL: spec.md's
# "Completed 结果不安全" scenario makes any result URL over 16,384 UTF-8 bytes
# an unsafe_result_url. doc2x.valid_https_url is the gate that enforces it
# before such a URL can reach private.json; the two numbers are pinned equal
# by test_result_url_upper_bound_matches_doc2x_valid_https_url_boundary in
# tests/unit/test_conversion_attempt.py.
RESULT_URL_UPPER_BOUND_BYTES = 16384

# json.dumps(..., ensure_ascii=True) -- the encoding bundle.canonical_json_bytes
# uses -- can \uXXXX-escape a single raw UTF-8 input byte (e.g. an ASCII
# control character) into up to 6 ASCII output bytes. That is the worst
# inflation ratio across every UTF-8 sequence length, so applying it
# uniformly per raw input byte is a safe (if not perfectly tight) upper
# bound for any string this encoder could produce.
JSON_STRING_ESCAPE_MAX_BYTES_PER_UTF8_BYTE = 6

# The wrapping `"..."` quote bytes json.dumps always adds around a string.
_JSON_STRING_QUOTE_BYTES = 2

# manifest.json / private.json candidate ceiling. workflow.py:34 already fixes
# 8 MiB as workflow._read_json's default max_bytes for both files, so admitting
# a candidate above it would only produce a file workflow can no longer read.
# conversion_attempt.py cannot import workflow.py (workflow.py already imports
# this module), so these are two further *definitions* of that 8 MiB -- only the
# number is shared, not its definition. Nothing but
# test_manifest_and_private_candidate_ceilings_match_workflow_read_ceiling keeps
# the copies equal; collapsing all three onto one owner is tracked separately.
MAX_MANIFEST_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_PRIVATE_CANDIDATE_BYTES = 8 * 1024 * 1024
# history.ndjson's ceiling is bundle.MAX_STATE_BYTES (bundle.py:16, 64 MiB) itself --
# referenced directly at the call site below so there is exactly one place that owns
# that value.


def worst_case_json_string_bytes(raw_utf8_byte_length: int) -> int:
    """Worst-case canonical-JSON bytes a raw UTF-8 string of
    raw_utf8_byte_length could occupy once encoded by
    bundle.canonical_json_bytes (ensure_ascii=True): the two wrapping quote
    bytes plus up to JSON_STRING_ESCAPE_MAX_BYTES_PER_UTF8_BYTE bytes per
    raw input byte.
    """
    return (
        _JSON_STRING_QUOTE_BYTES
        + raw_utf8_byte_length * JSON_STRING_ESCAPE_MAX_BYTES_PER_UTF8_BYTE
    )


# The worst-case JSON sizes of a task_id and a result URL are deliberately
# *not* module constants: evaluated at import they would freeze whatever
# TASK_ID_UPPER_BOUND_BYTES / RESULT_URL_UPPER_BOUND_BYTES said at that moment,
# so injecting smaller bounds to drive boundary cases (design.md:305, "ceiling
# 以可注入常量测试") would not reach the verdict. They are computed inside
# worst_case_admission_for_unknown_response from the module globals, the same
# way the three ceilings already are.


def worst_case_admission_for_unknown_response(
    *,
    manifest_candidate_bytes: int,
    private_candidate_bytes: int,
    history_candidate_bytes: int,
    manifest_unreceived_task_id_count: int = 0,
    private_unreceived_result_url_count: int = 0,
    history_unreceived_task_id_count: int = 0,
    history_unreceived_result_url_count: int = 0,
) -> dict:
    """Operation-local worst-case candidate admission for manifest.json,
    private.json and history.ndjson.

    Each *_candidate_bytes argument is the exact canonical byte length of the
    largest legal candidate for that file (design.md:305: the maximum over
    every direct and crash-recovery candidate the operation could produce),
    built with every not-yet-received bounded string set to the empty-string
    placeholder `""` and measured with canonical_state_byte_length. Because
    that measures the whole serialized document, every byte the candidate
    actually costs is already counted: key names, punctuation, nested objects,
    sha256 digests, timestamps and the single trailing LF. Passing only the
    file's *current* size and letting this function add value-sized deltas
    would omit all of that and under-admit.

    For manifest.json and private.json the candidate is the whole finished
    document. For history.ndjson the candidate is the current file's bytes
    plus every event the operation would append -- a create appends *two*
    events (conversion_submit_intent and conversion_submit_started), each
    carrying a full event shell: the complete attempt object, an operation_id,
    several timestamps and two sha256 digests. Each event's
    canonical_state_byte_length already includes that event's own trailing LF,
    so summing them is exact.

    Each *_unreceived_*_count says how many `""` placeholders of that kind the
    corresponding candidate still holds. Every one of them is upgraded from
    its 2-byte placeholder to the worst case that value could reach --
    `2 + 6 * max_utf8_bytes` JSON bytes (design.md:296) -- before the
    comparison, so a candidate holding two unknown values is charged twice.

    manifest, private and history are judged independently against their own
    ceiling (8 MiB / 8 MiB / 64 MiB) -- headroom in one file can never mask
    an overrun in another.

    This function only computes and judges; it does not raise or stop
    anything. Wiring a verdict into a stop-before-intent/before-external-call
    behavior is plan.md 2.3's responsibility.
    """
    unknown_task_id_bytes = (
        worst_case_json_string_bytes(TASK_ID_UPPER_BOUND_BYTES)
        - _JSON_STRING_QUOTE_BYTES
    )
    unknown_result_url_bytes = (
        worst_case_json_string_bytes(RESULT_URL_UPPER_BOUND_BYTES)
        - _JSON_STRING_QUOTE_BYTES
    )
    manifest_total = (
        manifest_candidate_bytes
        + manifest_unreceived_task_id_count * unknown_task_id_bytes
    )
    private_total = (
        private_candidate_bytes
        + private_unreceived_result_url_count * unknown_result_url_bytes
    )
    history_total = (
        history_candidate_bytes
        + history_unreceived_task_id_count * unknown_task_id_bytes
        + history_unreceived_result_url_count * unknown_result_url_bytes
    )
    return {
        "manifest": manifest_total <= MAX_MANIFEST_CANDIDATE_BYTES,
        "private": private_total <= MAX_PRIVATE_CANDIDATE_BYTES,
        "history": history_total <= bundle.MAX_STATE_BYTES,
    }


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (AttributeError, ValueError) as exc:
        raise ConversionAttemptError(
            "integrity_violation", "A conversion timestamp is invalid."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion timestamp is invalid."
        )
    return parsed


def _shift_timestamp(value: str, seconds: int) -> str:
    shifted = (_parse_timestamp(value) + timedelta(seconds=seconds)).isoformat()
    return shifted[:-6] + "Z" if shifted.endswith("+00:00") else shifted


def _next_backoff_at(*, at: str, deadline: str, consecutive_count: int) -> str:
    current = _parse_timestamp(at)
    limit = _parse_timestamp(deadline)
    remaining = max(0.0, (limit - current).total_seconds())
    delay = POLL_RETRY_BASE_SECONDS
    doublings = consecutive_count - 1
    while doublings > 0 and delay < remaining:
        delay *= 2
        doublings -= 1
    if delay >= remaining:
        return deadline
    return _shift_timestamp(at, delay)


def waiting_for_poll_backoff(attempt: dict, *, at: str) -> bool:
    if not isinstance(attempt, dict) or attempt.get("state") not in {
        "task_unavailable",
        "poll_transient",
    }:
        return False
    next_poll_at = attempt.get("next_poll_at")
    if not isinstance(next_poll_at, str):
        raise ConversionAttemptError(
            "integrity_violation", "The next Doc2X poll time is missing."
        )
    return _parse_timestamp(at) < _parse_timestamp(next_poll_at)


def result_reference_is_expired(attempt: dict, *, at: str) -> bool:
    if not isinstance(attempt, dict) or (
        attempt.get("result_observed_at") is None
        and attempt.get("result_validity_hours") is None
    ):
        return False
    result_observed_at = attempt.get("result_observed_at")
    result_validity_hours = attempt.get("result_validity_hours")
    if not isinstance(result_observed_at, str) or type(result_validity_hours) is not int:
        raise ConversionAttemptError(
            "integrity_violation", "The conversion result reference is missing."
        )
    expires_at = _shift_timestamp(result_observed_at, result_validity_hours * 3600)
    return _parse_timestamp(at) >= _parse_timestamp(expires_at)


def _recorded_result_url(private_state: dict, *, attempt_id, task_id, url):
    records = (
        private_state.get("result_urls") if isinstance(private_state, dict) else None
    )
    if not isinstance(records, list):
        return None
    for record in records:
        if (
            isinstance(record, dict)
            and record.get("attempt_id") == attempt_id
            and record.get("task_id") == task_id
            and record.get("url") == url
        ):
            return record
    return None


def _valid_timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        _parse_timestamp(value)
    except ConversionAttemptError:
        return False
    return True


def _valid_hash(value) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _valid_http_status(value) -> bool:
    return value is None or (type(value) is int and 100 <= value <= 599)


def _valid_credential(value) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "source_id",
        "fingerprint",
        "locator",
    }:
        return False
    source_id = value.get("source_id")
    locator = value.get("locator")
    if (
        not isinstance(source_id, str)
        or not _valid_hash(value.get("fingerprint"))
        or not isinstance(locator, dict)
        or locator.get("name") != "AIHUB_API_KEY"
    ):
        return False
    kind = locator.get("kind")
    if kind == "process_environment":
        return (
            set(locator) == {"kind", "name"}
            and source_id == "process_environment:AIHUB_API_KEY"
        )
    if kind == "dotenv":
        path = locator.get("path")
        return (
            set(locator) == {"kind", "path", "name"}
            and isinstance(path, str)
            and os.path.isabs(path)
            and os.path.normpath(path) == path
            and source_id == f"dotenv:{path}:AIHUB_API_KEY"
        )
    return False


def _valid_authorization(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == AUTHORIZATION_KEYS
        and isinstance(value.get("action_id"), str)
        and ACTION_ID_PATTERN.fullmatch(value["action_id"]) is not None
        and _valid_hash(value.get("evidence_hash"))
        and _valid_timestamp(value.get("authorized_at"))
        and _valid_hash(value.get("basis_sha256"))
        and value.get("accepted_risk") == "possible_duplicate_conversion_charge"
    )


def _valid_pending_action(value, *, attempt: dict, generation: int) -> bool:
    if value is None:
        return True
    expected_kinds = {
        "submission_unknown": "resolve_submission_unknown",
        "failed": "resolve_task_failed",
        "unexpected_result_count": "resolve_unexpected_result_count",
    }
    evidence_attempt = deepcopy(attempt)
    evidence_attempt["pending_action"] = None
    return (
        isinstance(value, dict)
        and set(value) == PENDING_ACTION_KEYS
        and value.get("kind") == expected_kinds.get(attempt.get("state"))
        and isinstance(value.get("action_id"), str)
        and ACTION_ID_PATTERN.fullmatch(value["action_id"]) is not None
        and value.get("generation") == generation
        and value.get("evidence_hash") == object_hash(evidence_attempt)
    )


def _valid_request_identity(attempt: dict, *, manifest: dict) -> bool:
    summary = attempt.get("request_summary")
    staging = attempt.get("staging_identity")
    source = manifest.get("source")
    preflight = manifest.get("preflight")
    if (
        not isinstance(summary, dict)
        or set(summary) != REQUEST_SUMMARY_KEYS
        or summary.get("model") != "doc2x-v3"
        or summary.get("convert_mode") != "md"
        or summary.get("formula_mode") != "dollar"
        or type(summary.get("merge_cross_page_forms")) is not bool
        or not isinstance(source, dict)
        or not isinstance(preflight, dict)
        or type(summary.get("page_count")) is not int
        or summary["page_count"] <= 0
        or summary["page_count"] != preflight.get("page_count")
        or summary.get("filename") != f"document-{source.get('sha256', '')[:8]}"
        or not _valid_hash(summary.get("pdf_url_sha256"))
        or not _valid_hash(attempt.get("request_hash"))
        or not isinstance(staging, dict)
        or set(staging) != STAGING_IDENTITY_KEYS
        or staging.get("source_sha256") != source.get("sha256")
        or staging.get("url_sha256") != summary.get("pdf_url_sha256")
        or not isinstance(staging.get("attempt_id"), str)
        or not _valid_hash(staging.get("url_sha256"))
        or not _valid_credential(attempt.get("credential"))
        or attempt.get("api_base") != API_BASE
        or not _valid_timestamp(attempt.get("submitted_at"))
    ):
        return False
    return True


def _valid_attempt(attempt, *, manifest: dict, generation: int) -> bool:
    if (
        not isinstance(attempt, dict)
        or set(attempt) != ATTEMPT_KEYS
        or attempt.get("schema_version") != SCHEMA_VERSION
        or not isinstance(attempt.get("attempt_id"), str)
        or ATTEMPT_ID_PATTERN.fullmatch(attempt["attempt_id"]) is None
    ):
        return False
    state = attempt.get("state")
    authorization = attempt.get("authorization")
    if authorization is not None and not _valid_authorization(authorization):
        return False
    if state == "not_started":
        return (
            _valid_authorization(authorization)
            and attempt.get("api_base") is None
            and attempt.get("poll_count") == 0
            and attempt.get("consecutive_transient_count") == 0
            and all(
                attempt.get(key) is None
                for key in ATTEMPT_KEYS
                - {
                    "schema_version",
                    "attempt_id",
                    "state",
                    "authorization",
                    "poll_count",
                    "consecutive_transient_count",
                }
            )
        )
    if not _valid_request_identity(attempt, manifest=manifest):
        return False
    if not _valid_pending_action(
        attempt.get("pending_action"), attempt=attempt, generation=generation
    ):
        return False
    if (
        not _valid_http_status(attempt.get("http_status"))
        or type(attempt.get("poll_count")) is not int
        or attempt["poll_count"] < 0
        or type(attempt.get("consecutive_transient_count")) is not int
        or attempt["consecutive_transient_count"] < 0
        or attempt["consecutive_transient_count"] > attempt["poll_count"]
    ):
        return False
    if state == "submitting":
        return (
            attempt.get("response_at") is None
            and attempt.get("http_status") is None
            and attempt.get("reason_code") is None
            and attempt.get("task_id") is None
            and attempt.get("pending_action") is None
            and attempt.get("poll_count") == 0
            and _empty_poll_and_result_fields(attempt)
        )
    response_at = attempt.get("response_at")
    if (
        not _valid_timestamp(response_at)
        or _parse_timestamp(response_at) < _parse_timestamp(attempt["submitted_at"])
    ):
        return False
    if state == "submission_unknown":
        return (
            attempt.get("task_id") is None
            and attempt.get("reason_code")
            in {
                "no_task_id",
                "invalid_transport_result",
                "network_result_unknown",
                "interrupted_before_result_commit",
            }
            and attempt.get("poll_count") == 0
            and _empty_poll_and_result_fields(attempt)
        )
    task_id = attempt.get("task_id")
    if (
        not isinstance(task_id, str)
        or doc2x.TASK_ID_PATTERN.fullmatch(task_id) is None
    ):
        return False
    state_contract = {
        "submitted": (200, None, None),
        "pending": (200, "pending", None),
        "processing": (200, "processing", None),
        "result_pending": (200, "completed", None),
        "result_ready": (200, "completed", None),
        "unsafe_result_url": (200, "completed", "unsafe_result_url"),
        "unexpected_result_count": (200, "completed", "unexpected_result_count"),
        "failed": (200, "failed", "task_failed"),
        "credential_source_missing": (None, None, "credential_source_missing"),
        "credential_source_changed": (None, None, "credential_source_changed"),
        "poll_unauthorized": (401, None, "poll_unauthorized"),
        "task_unavailable": (404, None, "task_unavailable"),
        "poll_timeout": (None, None, "poll_timeout"),
        "result_pending_timeout": (None, "completed", "result_pending_timeout"),
    }
    if state == "poll_transient":
        if attempt.get("upstream_status") is not None or attempt.get(
            "reason_code"
        ) not in {"poll_transient", "result_private_payload_lost"}:
            return False
    elif state not in state_contract or (
        attempt.get("http_status"),
        attempt.get("upstream_status"),
        attempt.get("reason_code"),
    ) != state_contract[state]:
        return False
    if state in {"task_unavailable", "poll_transient"}:
        if (
            attempt.get("consecutive_transient_count", 0) <= 0
            or not _valid_timestamp(attempt.get("next_poll_at"))
            or attempt.get("next_poll_at")
            != _next_backoff_at(
                at=attempt.get("last_polled_at"),
                deadline=attempt.get("poll_deadline_at"),
                consecutive_count=attempt.get("consecutive_transient_count"),
            )
        ):
            return False
    elif state not in {"credential_source_missing", "credential_source_changed"}:
        if (
            attempt.get("consecutive_transient_count") != 0
            or attempt.get("next_poll_at") is not None
        ):
            return False
    if state == "submitted":
        return attempt.get("poll_count") == 0 and _empty_poll_and_result_fields(
            attempt
        )
    if state not in {"credential_source_missing", "credential_source_changed"} and (
        attempt.get("poll_count", 0) <= 0
    ):
        return False
    if not _valid_poll_fields(attempt):
        return False
    if state in {"result_pending", "result_pending_timeout"} and (
        attempt.get("result_pending_started_at") is None
        or attempt.get("result_pending_deadline_at") is None
    ):
        return False
    if state == "result_ready":
        return (
            _valid_hash(attempt.get("result_url_sha256"))
            and _valid_timestamp(attempt.get("result_observed_at"))
            and attempt.get("result_validity_hours") == 24
        )
    return (
        attempt.get("result_url_sha256") is None
        and attempt.get("result_observed_at") is None
        and attempt.get("result_validity_hours") is None
    )


def _empty_poll_and_result_fields(attempt: dict) -> bool:
    return all(
        attempt.get(key) is None
        for key in {
            "poll_started_at",
            "poll_deadline_at",
            "last_polled_at",
            "upstream_status",
            "next_poll_at",
            "result_url_sha256",
            "result_observed_at",
            "result_validity_hours",
            "result_pending_started_at",
            "result_pending_deadline_at",
        }
    ) and attempt.get("consecutive_transient_count") == 0


def _valid_poll_fields(attempt: dict) -> bool:
    count = attempt.get("poll_count")
    poll_times = (
        attempt.get("poll_started_at"),
        attempt.get("poll_deadline_at"),
        attempt.get("last_polled_at"),
    )
    if count == 0:
        if any(value is not None for value in poll_times):
            return False
    else:
        if not all(_valid_timestamp(value) for value in poll_times):
            return False
        started, deadline, last = map(_parse_timestamp, poll_times)
        if (
            not _parse_timestamp(attempt["response_at"]) <= started <= last
            or (deadline - started).total_seconds() != POLL_WINDOW_SECONDS
            or last > deadline
        ):
            return False
    pending_started = attempt.get("result_pending_started_at")
    pending_deadline = attempt.get("result_pending_deadline_at")
    if (pending_started is None) != (pending_deadline is None):
        return False
    if pending_started is not None:
        if not _valid_timestamp(pending_started) or not _valid_timestamp(
            pending_deadline
        ):
            return False
        if _parse_timestamp(pending_started) >= _parse_timestamp(pending_deadline):
            return False
        if (
            _parse_timestamp(pending_deadline) - _parse_timestamp(pending_started)
        ).total_seconds() != RESULT_PENDING_WINDOW_SECONDS:
            return False
    next_poll_at = attempt.get("next_poll_at")
    if next_poll_at is not None:
        if not _valid_timestamp(next_poll_at):
            return False
        next_poll = _parse_timestamp(next_poll_at)
        if count == 0 or not _parse_timestamp(attempt["last_polled_at"]) < next_poll:
            return False
        if next_poll > _parse_timestamp(attempt["poll_deadline_at"]):
            return False
    return True


def build_request(
    *, manifest: dict, source_url: str, preflight_record: dict
) -> tuple[dict, dict]:
    source = manifest["source"]
    page_count = manifest["preflight"]["page_count"]
    request = {
        "model": "doc2x-v3",
        "pdf_url": source_url,
        "page_count": page_count,
        "filename": f"document-{source['sha256'][:8]}",
        "convert_mode": "md",
        "formula_mode": "dollar",
        "merge_cross_page_forms": any(
            isinstance(page, dict)
            and isinstance(page.get("risk_codes"), list)
            and "cross_page_table" in page["risk_codes"]
            for page in preflight_record.get("pages", [])
        ),
    }
    summary = {
        key: value for key, value in request.items() if key != "pdf_url"
    }
    summary["pdf_url_sha256"] = (
        "sha256:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    )
    return request, summary


def begin_attempt(
    *, descriptors: dict, manifest: dict, private_state: dict, credential: dict,
    request: dict, request_summary: dict, at: str
) -> tuple[dict, dict, dict]:
    staging = manifest.get("source_staging")
    attempts = manifest.get("conversion_attempts")
    if (
        manifest.get("conversion_state") != "ready_to_submit"
        or not isinstance(staging, dict)
        or staging.get("state") != "source_upload_ready"
        or not isinstance(attempts, list)
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise ConversionAttemptError(
            "invalid_state_transition", "The work bundle is not ready for conversion."
        )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    placeholder = attempts[-1] if attempts and attempts[-1].get("state") == "not_started" else None
    if placeholder is not None:
        attempt_id = placeholder["attempt_id"]
        previous_attempts = attempts[:-1]
        authorization = deepcopy(placeholder.get("authorization"))
    else:
        attempt_id = f"conversion-attempt-{len(attempts) + 1:04d}"
        previous_attempts = attempts
        authorization = None
    staging_attempt = staging["attempts"][-1]
    attempt = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "state": "submitting",
        "api_base": API_BASE,
        "request_summary": deepcopy(request_summary),
        "request_hash": object_hash(request),
        "credential": deepcopy(credential),
        "staging_identity": {
            "attempt_id": staging_attempt["attempt_id"],
            "source_sha256": staging_attempt["source_sha256"],
            "url_sha256": staging_attempt["url_sha256"],
        },
        "submitted_at": at,
        "response_at": None,
        "http_status": None,
        "reason_code": None,
        "task_id": None,
        "pending_action": None,
        "authorization": authorization,
        "poll_started_at": None,
        "poll_deadline_at": None,
        "last_polled_at": None,
        "poll_count": 0,
        "upstream_status": None,
        "next_poll_at": None,
        "consecutive_transient_count": 0,
        "result_url_sha256": None,
        "result_observed_at": None,
        "result_validity_hours": None,
        "result_pending_started_at": None,
        "result_pending_deadline_at": None,
    }
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = "submitting"
    updated_manifest["conversion_attempts"] = [*deepcopy(previous_attempts), attempt]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if not _valid_attempt(attempt, manifest=updated_manifest, generation=new_generation):
        raise ConversionAttemptError(
            "integrity_violation", "The conversion submission intent is invalid."
        )
    operation_id = f"{attempt_id}-submit"
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "conversion_submit_intent",
            "operation_id": operation_id,
            "expected_generation": expected_generation,
            "new_generation": new_generation,
            "at": at,
            "attempt": attempt,
            "previous_attempt": deepcopy(placeholder),
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
            "event": "conversion_submit_started",
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


def _started_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict, dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    attempt = intent.get("attempt")
    previous_attempt = intent.get("previous_attempt")
    if (
        set(intent) != SUBMIT_INTENT_KEYS
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "conversion_submit_intent"
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or not isinstance(attempt, dict)
        or attempt.get("state") != "submitting"
        or intent.get("operation_id") != f"{attempt.get('attempt_id')}-submit"
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion intent is invalid."
        )

    if object_hash(manifest) == intent.get("previous_manifest_hash"):
        previous_manifest = deepcopy(manifest)
    else:
        previous_manifest = deepcopy(manifest)
        previous_manifest["generation"] = expected_generation
        previous_manifest["conversion_state"] = "ready_to_submit"
        current_attempts = previous_manifest.get("conversion_attempts")
        if (
            not isinstance(current_attempts, list)
            or not current_attempts
            or current_attempts[-1].get("attempt_id") != attempt.get("attempt_id")
        ):
            raise ConversionAttemptError(
                "integrity_violation", "A pending conversion intent has no valid manifest."
            )
        previous_manifest["conversion_attempts"] = [
            *deepcopy(current_attempts[:-1]),
            *([deepcopy(previous_attempt)] if previous_attempt is not None else []),
        ]
    if object_hash(previous_manifest) != intent.get("previous_manifest_hash"):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion manifest is inconsistent."
        )

    if object_hash(private_state) == intent.get("previous_private_hash"):
        previous_private = deepcopy(private_state)
    else:
        previous_private = deepcopy(private_state)
        previous_private["generation"] = expected_generation
    if object_hash(previous_private) != intent.get("previous_private_hash"):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion private state is inconsistent."
        )

    desired_manifest = deepcopy(previous_manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = "submitting"
    previous_attempts = desired_manifest.get("conversion_attempts")
    if not isinstance(previous_attempts, list):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion attempt list is invalid."
        )
    if previous_attempt is not None:
        if (
            not previous_attempts
            or previous_attempts[-1] != previous_attempt
            or previous_attempt.get("state") != "not_started"
        ):
            raise ConversionAttemptError(
                "integrity_violation", "A pending conversion placeholder is invalid."
            )
        previous_attempts = previous_attempts[:-1]
    desired_manifest["conversion_attempts"] = [
        *deepcopy(previous_attempts),
        deepcopy(attempt),
    ]
    desired_private = deepcopy(previous_private)
    desired_private["generation"] = new_generation
    if not _valid_attempt(
        attempt, manifest=desired_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion attempt is invalid."
        )
    if manifest != previous_manifest and manifest != desired_manifest:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion manifest is partially committed."
        )
    if private_state != previous_private and private_state != desired_private:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion private state is partially committed."
        )
    if manifest == desired_manifest and private_state == previous_private:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion commit order is invalid."
        )
    return previous_manifest, previous_private, desired_manifest, desired_private


def finish_submission(
    *, descriptors: dict, manifest: dict, private_state: dict,
    result: doc2x.CreateResult, at: str
) -> tuple[dict, dict]:
    attempts = manifest.get("conversion_attempts")
    if (
        manifest.get("conversion_state") != "submitting"
        or not isinstance(attempts, list)
        or not attempts
        or attempts[-1].get("state") != "submitting"
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise ConversionAttemptError(
            "invalid_state_transition", "The conversion result is not applicable."
        )
    completed = deepcopy(attempts[-1])
    completed.update(
        {
            "state": result.state,
            "response_at": at,
            "http_status": result.http_status,
            "reason_code": result.reason_code,
            "task_id": result.task_id,
        }
    )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    if result.state == "submission_unknown" and (
        manifest["settings_snapshot"]["interaction_mode"] == "confirm"
    ):
        completed["pending_action"] = {
            "kind": "resolve_submission_unknown",
            "action_id": f"conversion-decision-{secrets.token_hex(16)}",
            "generation": new_generation,
            "evidence_hash": object_hash(completed),
        }
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = result.state
    updated_manifest["conversion_attempts"] = [*deepcopy(attempts[:-1]), completed]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if not _valid_attempt(
        completed, manifest=updated_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "The conversion submission result is invalid."
        )
    operation_id = f"{completed['attempt_id']}-submit-result"
    bundle.append_history(
        {
            "schema_version": SCHEMA_VERSION,
            "event": "conversion_submit_result_intent",
            "operation_id": operation_id,
            "expected_generation": expected_generation,
            "new_generation": new_generation,
            "at": at,
            "attempt": completed,
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
            "event": "conversion_submit_result_committed",
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


def _submission_result_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict, dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    completed = intent.get("attempt")
    if (
        set(intent) != RESULT_INTENT_KEYS
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "conversion_submit_result_intent"
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or not isinstance(completed, dict)
        or completed.get("state") not in {"submitted", "submission_unknown"}
        or (
            completed.get("state") == "submitted"
            and (
                not isinstance(completed.get("task_id"), str)
                or doc2x.TASK_ID_PATTERN.fullmatch(completed["task_id"]) is None
            )
        )
        or intent.get("operation_id")
        != f"{completed.get('attempt_id')}-submit-result"
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result intent is invalid."
        )

    if object_hash(manifest) == intent.get("previous_manifest_hash"):
        previous_manifest = deepcopy(manifest)
    else:
        previous_manifest = deepcopy(manifest)
        previous_manifest["generation"] = expected_generation
        previous_manifest["conversion_state"] = "submitting"
        current_attempts = previous_manifest.get("conversion_attempts")
        if (
            not isinstance(current_attempts, list)
            or not current_attempts
            or current_attempts[-1].get("attempt_id")
            != completed.get("attempt_id")
        ):
            raise ConversionAttemptError(
                "integrity_violation", "A pending conversion result has no valid manifest."
            )
        submitting = deepcopy(completed)
        submitting.update(
            {
                "state": "submitting",
                "response_at": None,
                "http_status": None,
                "reason_code": None,
                "task_id": None,
                "pending_action": None,
            }
        )
        previous_manifest["conversion_attempts"] = [
            *deepcopy(current_attempts[:-1]),
            submitting,
        ]
    if object_hash(previous_manifest) != intent.get("previous_manifest_hash"):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result manifest is inconsistent."
        )

    if object_hash(private_state) == intent.get("previous_private_hash"):
        previous_private = deepcopy(private_state)
    else:
        previous_private = deepcopy(private_state)
        previous_private["generation"] = expected_generation
    if object_hash(previous_private) != intent.get("previous_private_hash"):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result private state is inconsistent."
        )

    desired_manifest = deepcopy(previous_manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = completed["state"]
    desired_manifest["conversion_attempts"] = [
        *deepcopy(previous_manifest["conversion_attempts"][:-1]),
        deepcopy(completed),
    ]
    desired_private = deepcopy(previous_private)
    desired_private["generation"] = new_generation
    if not _valid_attempt(
        completed, manifest=desired_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result attempt is invalid."
        )
    if manifest != previous_manifest and manifest != desired_manifest:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result manifest is partial."
        )
    if private_state != previous_private and private_state != desired_private:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result private state is partial."
        )
    if manifest == desired_manifest and private_state == previous_private:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion result commit order is invalid."
        )
    return previous_manifest, previous_private, desired_manifest, desired_private


def _assert_recovery_generation(
    expected_generation: int,
    intent_expected,
    intent_new,
    *,
    message: str,
) -> None:
    if type(intent_expected) is not int or intent_new != intent_expected + 1:
        raise ConversionAttemptError(
            "integrity_violation", "A pending conversion generation is invalid."
        )
    if expected_generation not in (intent_expected, intent_new):
        raise ConversionAttemptError("generation_conflict", message)


def recover_interrupted_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    at: str,
    expected_generation: int,
    resolve_history,
) -> tuple[dict, dict] | None:
    """Finish a conversion operation that a crash left pending.

    `resolve_history` reduces the durable prefix and must be supplied by the
    caller: once a bundle carries raw conversion records, its history holds
    events this module does not know, and only the caller knows which layer's
    reducer understands every event the bundle can hold. It takes the same
    shape as `resolve_history_state` below. Required rather than defaulted so
    a caller cannot silently fall back to a reducer that is too narrow.
    """
    try:
        history = bundle.read_history(state_fd=descriptors["state"])
    except bundle.BundleStateError as exc:
        raise ConversionAttemptError(
            "integrity_violation", "Conversion history cannot be recovered safely."
        ) from exc
    final = history[-1] if history else None
    if isinstance(final, dict) and final.get("event") == "conversion_retry_intent":
        intent_expected = final.get("expected_generation")
        intent_new = final.get("new_generation")
        _assert_recovery_generation(
            expected_generation,
            intent_expected,
            intent_new,
            message="Expected generation does not match the pending conversion retry.",
        )
        previous = resolve_history(
            history[:-1],
            manifest_template=manifest,
            private_template=private_state,
        )
        if previous is None:
            raise ConversionAttemptError(
                "integrity_violation",
                "A pending conversion retry has no valid history prefix.",
            )
        previous_manifest, previous_private = previous
        desired_manifest, desired_private = _retry_state_from_intent(
            previous_manifest, previous_private, final
        )
        manifest_is_previous = manifest == previous_manifest
        manifest_is_desired = manifest == desired_manifest
        private_is_previous = private_state == previous_private
        private_is_desired = private_state == desired_private
        if (
            not (manifest_is_previous or manifest_is_desired)
            or not (private_is_previous or private_is_desired)
            or (manifest_is_desired and private_is_previous)
        ):
            raise ConversionAttemptError(
                "integrity_violation",
                "A pending conversion retry is partially inconsistent.",
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
                "event": "conversion_retry_committed",
                "operation_id": final["operation_id"],
                "previous_generation": intent_expected,
                "generation": intent_new,
                "at": at,
                "manifest_hash": object_hash(desired_manifest),
                "private_hash": object_hash(desired_private),
            },
            state_fd=descriptors["state"],
        )
        return desired_manifest, desired_private
    if (
        isinstance(final, dict)
        and final.get("event") == "conversion_poll_result_intent"
    ):
        intent_expected = final.get("expected_generation")
        intent_new = final.get("new_generation")
        _assert_recovery_generation(
            expected_generation,
            intent_expected,
            intent_new,
            message=(
                "Expected generation does not match the pending conversion poll result."
            ),
        )
        intended_attempt = final.get("attempt")
        previous = resolve_history(
            history[:-1],
            manifest_template=manifest,
            private_template=private_state,
        )
        if (
            type(intent_expected) is not int
            or intent_new != intent_expected + 1
            or not isinstance(intended_attempt, dict)
            or previous is None
        ):
            raise ConversionAttemptError(
                "integrity_violation",
                "A pending conversion poll result cannot be recovered safely.",
            )
        previous_manifest, previous_private = previous
        private_payload = None
        if intended_attempt.get("state") == "result_ready":
            # Look the payload up the way `apply_committed_operations` does:
            # by attempt and URL digest. Counting entries would be wrong,
            # because a refresh answering with the URL already on file appends
            # no new version -- the payload is present but the list length is
            # unchanged, and the private write is a content-level no-op.
            current_results = private_state.get("result_urls")
            if isinstance(current_results, list):
                matching = [
                    record
                    for record in current_results
                    if isinstance(record, dict)
                    and record.get("attempt_id") == intended_attempt.get("attempt_id")
                    and record.get("url_sha256")
                    == intended_attempt.get("result_url_sha256")
                ]
                if len(matching) == 1:
                    private_payload = matching[0]
        recovered_secret_loss = (
            intended_attempt.get("state") == "result_ready" and private_payload is None
        )
        if recovered_secret_loss:
            desired_manifest, desired_private, recovered_attempt = _poll_transition(
                manifest=previous_manifest,
                private_state=previous_private,
                result=doc2x.PollResult(
                    "poll_transient",
                    None,
                    "result_private_payload_lost",
                    None,
                    None,
                ),
                at=final.get("at"),
            )
            committed_event = "conversion_poll_result_recovered_transient"
        else:
            desired_manifest, desired_private = _poll_state_from_intent(
                previous_manifest,
                previous_private,
                final,
                private_payload=private_payload,
            )
            recovered_attempt = intended_attempt
            committed_event = "conversion_poll_result_committed"
        manifest_is_previous = manifest == previous_manifest
        manifest_is_desired = manifest == desired_manifest
        private_is_previous = private_state == previous_private
        private_is_desired = private_state == desired_private
        if (
            not (manifest_is_previous or manifest_is_desired)
            or not (private_is_previous or private_is_desired)
            or (manifest_is_desired and private_is_previous)
        ):
            raise ConversionAttemptError(
                "integrity_violation",
                "A pending conversion poll result is partially inconsistent.",
            )
        if private_is_previous:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        if manifest_is_previous:
            bundle.atomic_write_json(
                "manifest.json", desired_manifest, dir_fd=descriptors["root"]
            )
        committed = {
            "schema_version": SCHEMA_VERSION,
            "event": committed_event,
            "operation_id": final["operation_id"],
            "previous_generation": intent_expected,
            "generation": intent_new,
            "at": at,
            "manifest_hash": object_hash(desired_manifest),
            "private_hash": object_hash(desired_private),
        }
        if recovered_secret_loss:
            committed["attempt"] = recovered_attempt
        bundle.append_history(
            committed,
            state_fd=descriptors["state"],
        )
        return desired_manifest, desired_private
    if (
        isinstance(final, dict)
        and final.get("event") == "conversion_submit_result_intent"
    ):
        intent_expected = final.get("expected_generation")
        intent_new = final.get("new_generation")
        _assert_recovery_generation(
            expected_generation,
            intent_expected,
            intent_new,
            message="Expected generation does not match the pending conversion result.",
        )
        (
            previous_manifest,
            previous_private,
            desired_manifest,
            desired_private,
        ) = _submission_result_state_from_intent(manifest, private_state, final)
        if private_state == previous_private:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        if manifest == previous_manifest:
            bundle.atomic_write_json(
                "manifest.json", desired_manifest, dir_fd=descriptors["root"]
            )
        bundle.append_history(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "conversion_submit_result_committed",
                "operation_id": final["operation_id"],
                "previous_generation": intent_expected,
                "generation": intent_new,
                "at": at,
                "manifest_hash": object_hash(desired_manifest),
                "private_hash": object_hash(desired_private),
            },
            state_fd=descriptors["state"],
        )
        return desired_manifest, desired_private
    if isinstance(final, dict) and final.get("event") == "conversion_submit_intent":
        intent_expected = final.get("expected_generation")
        intent_new = final.get("new_generation")
        _assert_recovery_generation(
            expected_generation,
            intent_expected,
            intent_new,
            message="Expected generation does not match the pending conversion intent.",
        )
        (
            previous_manifest,
            previous_private,
            desired_manifest,
            desired_private,
        ) = _started_state_from_intent(manifest, private_state, final)
        if private_state == previous_private:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        if manifest == previous_manifest:
            bundle.atomic_write_json(
                "manifest.json", desired_manifest, dir_fd=descriptors["root"]
            )
        bundle.append_history(
            {
                "schema_version": SCHEMA_VERSION,
                "event": "conversion_submit_started",
                "operation_id": final["operation_id"],
                "previous_generation": intent_expected,
                "generation": intent_new,
                "at": at,
                "manifest_hash": object_hash(desired_manifest),
                "private_hash": object_hash(desired_private),
            },
            state_fd=descriptors["state"],
        )
        manifest, private_state = desired_manifest, desired_private
        final = bundle.read_history(state_fd=descriptors["state"])[-1]

    attempts = manifest.get("conversion_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    if manifest.get("conversion_state") != "submitting":
        return None
    manifest_generation = manifest.get("generation")
    if type(manifest_generation) is not int:
        raise ConversionAttemptError(
            "integrity_violation", "The interrupted conversion generation is invalid."
        )
    if expected_generation not in (manifest_generation, manifest_generation - 1):
        raise ConversionAttemptError(
            "generation_conflict",
            "Expected generation does not match the interrupted conversion attempt.",
        )
    if (
        not isinstance(final, dict)
        or final.get("event") != "conversion_submit_started"
        or final.get("generation") != manifest.get("generation")
        or final.get("manifest_hash") != object_hash(manifest)
        or final.get("private_hash") != object_hash(private_state)
    ):
        raise ConversionAttemptError(
            "integrity_violation",
            "The interrupted conversion attempt cannot be recovered safely.",
        )
    return finish_submission(
        descriptors=descriptors,
        manifest=manifest,
        private_state=private_state,
        result=doc2x.CreateResult(
            "submission_unknown",
            None,
            "interrupted_before_result_commit",
            None,
        ),
        at=at,
    )


def timeout_before_poll(attempt: dict, *, at: str) -> doc2x.PollResult | None:
    if not isinstance(attempt, dict):
        raise ConversionAttemptError(
            "integrity_violation", "The active conversion attempt is invalid."
        )
    now = _parse_timestamp(at)
    if attempt.get("state") == "result_pending":
        deadline = attempt.get("result_pending_deadline_at")
        if not isinstance(deadline, str):
            raise ConversionAttemptError(
                "integrity_violation", "The result-pending deadline is missing."
            )
        if now >= _parse_timestamp(deadline):
            return doc2x.PollResult(
                "result_pending_timeout",
                None,
                "result_pending_timeout",
                "completed",
                None,
            )
    if attempt.get("state") in {
        "pending",
        "processing",
        "poll_transient",
        "task_unavailable",
        "unsafe_result_url",
    }:
        deadline = attempt.get("poll_deadline_at")
        if not isinstance(deadline, str):
            raise ConversionAttemptError(
                "integrity_violation", "The poll deadline is missing."
            )
        if now >= _parse_timestamp(deadline):
            return doc2x.PollResult(
                "poll_timeout", None, "poll_timeout", None, None
            )
    return None


def _conversion_state_for_attempt(state: str) -> str:
    if state == "result_ready":
        return "result_downloading"
    if state in {"unsafe_result_url", "unexpected_result_count"}:
        return "terminal_error"
    if state == "failed":
        return "awaiting_user"
    if state in {
        "credential_source_missing",
        "credential_source_changed",
        "poll_unauthorized",
        "task_unavailable",
        "poll_transient",
        "poll_timeout",
        "result_pending_timeout",
    }:
        return "recoverable_error"
    return "submitted"


def _poll_transition(
    *, manifest: dict, private_state: dict, result: doc2x.PollResult, at: str
) -> tuple[dict, dict, dict]:
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        manifest.get("conversion_state")
        not in {"submitted", "recoverable_error", "terminal_error"}
        or not isinstance(active, dict)
        or active.get("state")
        not in {
            "submitted",
            "pending",
            "processing",
            "result_pending",
            "credential_source_missing",
            "credential_source_changed",
            "poll_unauthorized",
            "task_unavailable",
            "poll_transient",
            "poll_timeout",
            "result_pending_timeout",
            "unsafe_result_url",
            "result_ready",
        }
        or not isinstance(active.get("task_id"), str)
        or not active["task_id"]
        or private_state.get("generation") != manifest.get("generation")
        or result.state
        not in {
            "pending",
            "processing",
            "result_pending",
            "result_ready",
            "unsafe_result_url",
            "unexpected_result_count",
            "failed",
            "credential_source_missing",
            "credential_source_changed",
            "poll_unauthorized",
            "task_unavailable",
            "poll_transient",
            "poll_timeout",
            "result_pending_timeout",
        }
    ):
        raise ConversionAttemptError(
            "invalid_state_transition", "The Doc2X poll result is not applicable."
        )
    updated_attempt = deepcopy(active)
    updated_attempt["state"] = result.state
    local_credential_error = result.state in {
        "credential_source_missing",
        "credential_source_changed",
    }
    local_timeout = result.state in {"poll_timeout", "result_pending_timeout"}
    if not local_credential_error and not local_timeout:
        reset_window = active.get("state") in {
            "poll_timeout",
            "result_pending_timeout",
            "result_ready",
        }
        updated_attempt["poll_started_at"] = (
            at if reset_window else active.get("poll_started_at") or at
        )
        updated_attempt["poll_deadline_at"] = (
            _shift_timestamp(at, POLL_WINDOW_SECONDS)
            if reset_window or active.get("poll_deadline_at") is None
            else active["poll_deadline_at"]
        )
        updated_attempt["last_polled_at"] = at
        updated_attempt["poll_count"] = active.get("poll_count", 0) + 1
    updated_attempt["upstream_status"] = result.upstream_status
    updated_attempt["http_status"] = result.http_status
    updated_attempt["reason_code"] = result.reason_code
    if result.state in {"task_unavailable", "poll_transient"}:
        consecutive_count = (
            active.get("consecutive_transient_count", 0) + 1
            if active.get("state") in {"task_unavailable", "poll_transient"}
            else 1
        )
        updated_attempt["consecutive_transient_count"] = consecutive_count
        updated_attempt["next_poll_at"] = _next_backoff_at(
            at=at,
            deadline=updated_attempt["poll_deadline_at"],
            consecutive_count=consecutive_count,
        )
    elif not local_credential_error:
        updated_attempt["consecutive_transient_count"] = 0
        updated_attempt["next_poll_at"] = None
    if result.state == "result_pending":
        reset_result_window = active.get("state") == "result_pending_timeout"
        updated_attempt["result_pending_started_at"] = (
            at
            if reset_result_window
            else active.get("result_pending_started_at") or at
        )
        updated_attempt["result_pending_deadline_at"] = (
            _shift_timestamp(at, RESULT_PENDING_WINDOW_SECONDS)
            if reset_result_window
            or active.get("result_pending_deadline_at") is None
            else active["result_pending_deadline_at"]
        )
    recorded_result = None
    if result.state == "result_ready":
        if not isinstance(result.url, str):
            raise ConversionAttemptError(
                "invalid_state_transition", "The Doc2X result URL is missing."
            )
        recorded_result = _recorded_result_url(
            private_state,
            attempt_id=active["attempt_id"],
            task_id=active["task_id"],
            url=result.url,
        )
        updated_attempt["result_url_sha256"] = (
            "sha256:" + hashlib.sha256(result.url.encode("utf-8")).hexdigest()
        )
        updated_attempt["result_observed_at"] = (
            at if recorded_result is None else recorded_result.get("observed_at")
        )
        updated_attempt["result_validity_hours"] = 24
    elif active.get("state") == "result_ready":
        updated_attempt["result_url_sha256"] = None
        updated_attempt["result_observed_at"] = None
        updated_attempt["result_validity_hours"] = None
    pending_action_kind = {
        "failed": "resolve_task_failed",
        "unexpected_result_count": "resolve_unexpected_result_count",
    }.get(result.state)
    if pending_action_kind is not None and (
        manifest["settings_snapshot"]["interaction_mode"] == "confirm"
    ):
        updated_attempt["pending_action"] = {
            "kind": pending_action_kind,
            "action_id": f"conversion-decision-{secrets.token_hex(16)}",
            "generation": manifest["generation"] + 1,
            "evidence_hash": object_hash(updated_attempt),
        }
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = _conversion_state_for_attempt(result.state)
    updated_manifest["conversion_attempts"] = [
        *deepcopy(attempts[:-1]),
        updated_attempt,
    ]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if result.state == "result_ready" and recorded_result is None:
        updated_private["result_urls"] = [
            *deepcopy(private_state["result_urls"]),
            {
                "attempt_id": active["attempt_id"],
                "task_id": active["task_id"],
                "url": result.url,
                "url_sha256": updated_attempt["result_url_sha256"],
                "observed_at": at,
                "expires_at": None,
                "validity_window_hours": 24,
            },
        ]
    if not _valid_attempt(
        updated_attempt, manifest=updated_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "The Doc2X poll transition is invalid."
        )
    return updated_manifest, updated_private, updated_attempt


def commit_poll_result(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    result: doc2x.PollResult,
    at: str,
) -> tuple[dict, dict]:
    updated_manifest, updated_private, updated_attempt = _poll_transition(
        manifest=manifest, private_state=private_state, result=result, at=at
    )
    active = manifest["conversion_attempts"][-1]
    expected_generation = manifest["generation"]
    new_generation = updated_manifest["generation"]
    operation_id = f"{active['attempt_id']}-poll-state-{new_generation:04d}"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_poll_result_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "attempt": updated_attempt,
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
            "event": "conversion_poll_result_committed",
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


def commit_retry_decision(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    basis: str,
    at: str,
) -> dict:
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    attempt_pending = active.get("pending_action") if isinstance(active, dict) else None
    raw_record = manifest.get("raw_conversion")
    raw_pending = (
        raw_record.get("pending_action") if isinstance(raw_record, dict) else None
    )
    pending = attempt_pending if isinstance(attempt_pending, dict) else raw_pending
    if (
        (
            manifest.get("conversion_state"),
            active.get("state") if isinstance(active, dict) else None,
            pending.get("kind") if isinstance(pending, dict) else None,
        )
        not in {
            (
                "submission_unknown",
                "submission_unknown",
                "resolve_submission_unknown",
            ),
            ("awaiting_user", "failed", "resolve_task_failed"),
            (
                "terminal_error",
                "unexpected_result_count",
                "resolve_unexpected_result_count",
            ),
            (
                "terminal_error",
                "result_ready",
                "resolve_unexpected_result_layout",
            ),
        }
        or manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or manifest.get("settings_snapshot", {}).get("interaction_mode") != "confirm"
        or not isinstance(pending, dict)
        or pending.get("generation") != expected_generation
        or pending.get("action_id") != action_id
        or pending.get("evidence_hash") != evidence_hash
        or not isinstance(basis, str)
        or not basis.strip()
    ):
        raise ConversionAttemptError(
            "conversion_action_mismatch",
            "The conversion decision does not match the pending action.",
        )
    new_generation = expected_generation + 1
    basis_sha256 = "sha256:" + hashlib.sha256(
        basis.strip().encode("utf-8")
    ).hexdigest()
    placeholder = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": f"conversion-attempt-{len(attempts) + 1:04d}",
        "state": "not_started",
        "api_base": None,
        "request_summary": None,
        "request_hash": None,
        "credential": None,
        "staging_identity": None,
        "submitted_at": None,
        "response_at": None,
        "http_status": None,
        "reason_code": None,
        "task_id": None,
        "pending_action": None,
        "authorization": {
            "action_id": action_id,
            "evidence_hash": evidence_hash,
            "authorized_at": at,
            "basis_sha256": basis_sha256,
            "accepted_risk": "possible_duplicate_conversion_charge",
        },
        "poll_started_at": None,
        "poll_deadline_at": None,
        "last_polled_at": None,
        "poll_count": 0,
        "upstream_status": None,
        "next_poll_at": None,
        "consecutive_transient_count": 0,
        "result_url_sha256": None,
        "result_observed_at": None,
        "result_validity_hours": None,
        "result_pending_started_at": None,
        "result_pending_deadline_at": None,
    }
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = "ready_to_submit"
    updated_manifest["conversion_attempts"] = [*deepcopy(attempts), placeholder]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    operation_id = f"{placeholder['attempt_id']}-authorize"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_retry_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "basis_sha256": basis_sha256,
        "attempt": placeholder,
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
            "event": "conversion_retry_committed",
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


def _source_prefix_manifest(manifest: dict) -> dict:
    prefix = deepcopy(manifest)
    prefix["conversion_state"] = "ready_to_submit"
    prefix["conversion_attempts"] = []
    return prefix


def _poll_state_from_intent(
    manifest: dict,
    private_state: dict,
    intent: dict,
    *,
    private_payload: dict | None,
    recovered_attempt: dict | None = None,
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    updated_attempt = recovered_attempt or intent.get("attempt")
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        set(intent) != RESULT_INTENT_KEYS
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "conversion_poll_result_intent"
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
        or not isinstance(active, dict)
        or not isinstance(updated_attempt, dict)
        or updated_attempt.get("attempt_id") != active.get("attempt_id")
        or updated_attempt.get("task_id") != active.get("task_id")
        or any(
            updated_attempt.get(key) != active.get(key)
            for key in POLL_IMMUTABLE_ATTEMPT_KEYS
        )
        or not isinstance(updated_attempt.get("task_id"), str)
        or doc2x.TASK_ID_PATTERN.fullmatch(updated_attempt["task_id"]) is None
        or intent.get("operation_id")
        != f"{active.get('attempt_id')}-poll-state-{new_generation:04d}"
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion poll intent is inconsistent."
        )
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = _conversion_state_for_attempt(
        updated_attempt.get("state")
    )
    desired_manifest["conversion_attempts"] = [
        *deepcopy(attempts[:-1]),
        deepcopy(updated_attempt),
    ]
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    if updated_attempt.get("state") == "result_ready":
        if (
            not isinstance(private_payload, dict)
            or private_payload.get("attempt_id") != active.get("attempt_id")
            or private_payload.get("task_id") != active.get("task_id")
            or private_payload.get("url_sha256")
            != updated_attempt.get("result_url_sha256")
        ):
            raise ConversionAttemptError(
                "integrity_violation", "A conversion result URL payload is missing."
            )
        if (
            _recorded_result_url(
                private_state,
                attempt_id=active.get("attempt_id"),
                task_id=active.get("task_id"),
                url=private_payload.get("url"),
            )
            is None
        ):
            desired_private["result_urls"] = [
                *deepcopy(private_state.get("result_urls", [])),
                deepcopy(private_payload),
            ]
    elif private_payload is not None:
        raise ConversionAttemptError(
            "integrity_violation", "A conversion poll intent has an unexpected payload."
        )
    if not _valid_attempt(
        updated_attempt, manifest=desired_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion poll attempt is invalid."
        )
    return desired_manifest, desired_private


def _retry_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    placeholder = intent.get("attempt")
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    attempt_pending = active.get("pending_action") if isinstance(active, dict) else None
    raw_record = manifest.get("raw_conversion")
    raw_pending = (
        raw_record.get("pending_action") if isinstance(raw_record, dict) else None
    )
    pending = attempt_pending if isinstance(attempt_pending, dict) else raw_pending
    authorization = (
        placeholder.get("authorization") if isinstance(placeholder, dict) else None
    )
    if (
        set(intent) != RETRY_INTENT_KEYS
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != "conversion_retry_intent"
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
        or not isinstance(attempts, list)
        or not isinstance(placeholder, dict)
        or placeholder.get("state") != "not_started"
        or not isinstance(pending, dict)
        or not isinstance(authorization, dict)
        or intent.get("action_id") != pending.get("action_id")
        or intent.get("evidence_hash") != pending.get("evidence_hash")
        or authorization.get("action_id") != intent.get("action_id")
        or authorization.get("evidence_hash") != intent.get("evidence_hash")
        or authorization.get("basis_sha256") != intent.get("basis_sha256")
        or authorization.get("authorized_at") != intent.get("at")
        or intent.get("operation_id")
        != f"{placeholder.get('attempt_id')}-authorize"
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion retry intent is inconsistent."
        )
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = "ready_to_submit"
    desired_manifest["conversion_attempts"] = [
        *deepcopy(attempts),
        deepcopy(placeholder),
    ]
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    if not _valid_attempt(
        placeholder, manifest=desired_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion retry placeholder is invalid."
        )
    return desired_manifest, desired_private


def _valid_committed_event(
    event: dict,
    *,
    intent: dict,
    expected_events: set[str],
    desired_manifest: dict,
    desired_private: dict,
) -> bool:
    return (
        isinstance(event, dict)
        and set(event)
        == (
            COMMITTED_EVENT_KEYS | {"attempt"}
            if event.get("event") == "conversion_poll_result_recovered_transient"
            else COMMITTED_EVENT_KEYS
        )
        and event.get("schema_version") == SCHEMA_VERSION
        and event.get("event") in expected_events
        and event.get("operation_id") == intent.get("operation_id")
        and event.get("previous_generation") == intent.get("expected_generation")
        and event.get("generation") == intent.get("new_generation")
        and event.get("manifest_hash") == object_hash(desired_manifest)
        and event.get("private_hash") == object_hash(desired_private)
        and _valid_hash(event.get("manifest_hash"))
        and _valid_hash(event.get("private_hash"))
        and _valid_timestamp(intent.get("at"))
        and _valid_timestamp(event.get("at"))
        and _parse_timestamp(intent["at"]) <= _parse_timestamp(event["at"])
    )


def apply_committed_operations(
    history: list[dict],
    *,
    manifest: dict,
    private_state: dict,
    private_template: dict,
) -> tuple[dict, dict] | None:
    try:
        template_results = private_template.get("result_urls")
        if not isinstance(template_results, list):
            return None
        current_manifest = deepcopy(manifest)
        current_private = deepcopy(private_state)
        offset = 0
        operation_ids = set()
        while offset < len(history):
            intent = history[offset]
            if not isinstance(intent, dict) or offset + 1 >= len(history):
                return None
            operation_id = intent.get("operation_id")
            if not isinstance(operation_id, str) or operation_id in operation_ids:
                return None
            committed = history[offset + 1]
            event = intent.get("event")
            if event == "conversion_submit_intent":
                (
                    _previous_manifest,
                    _previous_private,
                    desired_manifest,
                    desired_private,
                ) = _started_state_from_intent(current_manifest, current_private, intent)
                expected_events = {"conversion_submit_started"}
            elif event == "conversion_submit_result_intent":
                (
                    _previous_manifest,
                    _previous_private,
                    desired_manifest,
                    desired_private,
                ) = _submission_result_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"conversion_submit_result_committed"}
            elif event == "conversion_retry_intent":
                desired_manifest, desired_private = _retry_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {"conversion_retry_committed"}
            elif event == "conversion_poll_result_intent":
                recovered = (
                    isinstance(committed, dict)
                    and committed.get("event")
                    == "conversion_poll_result_recovered_transient"
                )
                intended_attempt = intent.get("attempt")
                replay_attempt = committed.get("attempt") if recovered else intended_attempt
                private_payload = None
                if (
                    isinstance(replay_attempt, dict)
                    and replay_attempt.get("state") == "result_ready"
                ):
                    matching = [
                        record
                        for record in template_results
                        if isinstance(record, dict)
                        and record.get("attempt_id") == replay_attempt.get("attempt_id")
                        and record.get("url_sha256")
                        == replay_attempt.get("result_url_sha256")
                    ]
                    if len(matching) != 1:
                        return None
                    private_payload = matching[0]
                desired_manifest, desired_private = _poll_state_from_intent(
                    current_manifest,
                    current_private,
                    intent,
                    private_payload=private_payload,
                    recovered_attempt=replay_attempt if recovered else None,
                )
                expected_events = {
                    "conversion_poll_result_recovered_transient"
                    if recovered
                    else "conversion_poll_result_committed"
                }
            else:
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
    except (KeyError, IndexError, TypeError, ValueError, ConversionAttemptError):
        return None


def _reduce_history(
    history: list[dict], *, private_template: dict
) -> tuple[dict, dict] | None:
    try:
        first = next(
            (
                index
                for index, event in enumerate(history)
                if isinstance(event, dict)
                and event.get("event") == "conversion_submit_intent"
            ),
            None,
        )
        if first is None or not isinstance(private_template, dict):
            return None
        prefix_private = deepcopy(private_template)
        prefix_private["result_urls"] = []
        reduced_prefix = source_staging.resolve_history_state(
            history[:first], manifest_template={}, private_template=prefix_private
        )
        if reduced_prefix is None:
            return None
        return apply_committed_operations(
            history[first:],
            manifest=reduced_prefix[0],
            private_state=reduced_prefix[1],
            private_template=private_template,
        )
    except (KeyError, IndexError, TypeError, ValueError, ConversionAttemptError):
        return None


def valid_private_state(private_state: dict, manifest: dict) -> bool:
    try:
        attempts = manifest.get("conversion_attempts")
        generation = manifest.get("generation")
        result_urls = private_state.get("result_urls")
        if (
            not isinstance(attempts, list)
            or not attempts
            or type(generation) is not int
            or private_state.get("generation") != generation
            or not isinstance(result_urls, list)
        ):
            return False
        prefix_private = deepcopy(private_state)
        prefix_private["result_urls"] = []
        prefix_manifest = _source_prefix_manifest(manifest)
        if not source_staging.valid_private_state(prefix_private, prefix_manifest):
            return False
        source_staging_state = manifest.get("source_staging")
        staging_attempts = (
            source_staging_state.get("attempts")
            if isinstance(source_staging_state, dict)
            else None
        )
        source_uploads = private_state.get("source_uploads")
        if not isinstance(staging_attempts, list) or not isinstance(
            source_uploads, list
        ):
            return False
        public_staging = {
            item.get("attempt_id"): item
            for item in staging_attempts
            if isinstance(item, dict) and isinstance(item.get("attempt_id"), str)
        }
        private_staging = {
            item.get("attempt_id"): item
            for item in source_uploads
            if isinstance(item, dict) and isinstance(item.get("attempt_id"), str)
        }
        task_ids = set()
        for index, attempt in enumerate(attempts, start=1):
            pending = attempt.get("pending_action") if isinstance(attempt, dict) else None
            attempt_generation = (
                generation
                if index == len(attempts)
                else pending.get("generation")
                if isinstance(pending, dict)
                else generation
            )
            if (
                not _valid_attempt(
                    attempt, manifest=manifest, generation=attempt_generation
                )
                or attempt.get("attempt_id")
                != f"conversion-attempt-{index:04d}"
            ):
                return False
            authorization = attempt.get("authorization")
            if index == 1:
                if authorization is not None:
                    return False
            else:
                previous_pending = attempts[index - 2].get("pending_action")
                raw_record = manifest.get("raw_conversion")
                raw_pending = (
                    raw_record.get("pending_action")
                    if isinstance(raw_record, dict)
                    and raw_record.get("attempt_id")
                    == attempts[index - 2].get("attempt_id")
                    else None
                )
                authorization_source = (
                    previous_pending
                    if isinstance(previous_pending, dict)
                    else raw_pending
                )
                if (
                    not isinstance(authorization, dict)
                    or not isinstance(authorization_source, dict)
                    or authorization.get("action_id")
                    != authorization_source.get("action_id")
                    or authorization.get("evidence_hash")
                    != authorization_source.get("evidence_hash")
                ):
                    return False
            task_id = attempt.get("task_id")
            if task_id is not None:
                if task_id in task_ids:
                    return False
                task_ids.add(task_id)
            if attempt.get("state") == "not_started":
                continue
            staging_identity = attempt["staging_identity"]
            staging_public = public_staging.get(staging_identity["attempt_id"])
            staging_private = private_staging.get(staging_identity["attempt_id"])
            if (
                not isinstance(staging_public, dict)
                or not isinstance(staging_private, dict)
                or staging_public.get("source_sha256")
                != staging_identity["source_sha256"]
                or staging_public.get("url_sha256")
                != staging_identity["url_sha256"]
                or staging_public.get("credential") != attempt.get("credential")
                or staging_private.get("url_sha256")
                != staging_identity["url_sha256"]
                or not doc2x.valid_https_url(staging_private.get("url"))
                or staging_identity["url_sha256"]
                != "sha256:"
                + hashlib.sha256(staging_private["url"].encode("utf-8")).hexdigest()
            ):
                return False
            expires_at = staging_private.get("expires_at")
            if not _valid_timestamp(expires_at) or _parse_timestamp(
                attempt["submitted_at"]
            ) >= _parse_timestamp(expires_at):
                return False
            summary = attempt["request_summary"]
            request = {
                key: value
                for key, value in summary.items()
                if key != "pdf_url_sha256"
            }
            request["pdf_url"] = staging_private["url"]
            if attempt.get("request_hash") != object_hash(request):
                return False
        active = attempts[-1]
        expected_manifest_state = {
            "not_started": "ready_to_submit",
            "submitting": "submitting",
            "submitted": "submitted",
            "pending": "submitted",
            "processing": "submitted",
            "result_pending": "submitted",
            "submission_unknown": "submission_unknown",
            "result_ready": "result_downloading",
            "unsafe_result_url": "terminal_error",
            "unexpected_result_count": "terminal_error",
            "failed": "awaiting_user",
            "credential_source_missing": "recoverable_error",
            "credential_source_changed": "recoverable_error",
            "poll_unauthorized": "recoverable_error",
            "task_unavailable": "recoverable_error",
            "poll_transient": "recoverable_error",
            "poll_timeout": "recoverable_error",
            "result_pending_timeout": "recoverable_error",
        }.get(active.get("state"))
        if manifest.get("conversion_state") != expected_manifest_state:
            return False
        active_pending = active.get("pending_action")
        mode = manifest.get("settings_snapshot", {}).get("interaction_mode")
        if active.get("state") in {
            "submission_unknown",
            "failed",
            "unexpected_result_count",
        }:
            if (mode == "confirm") != isinstance(active_pending, dict):
                return False
        elif active_pending is not None:
            return False
        expected_results = {
            attempt["attempt_id"]: attempt
            for attempt in attempts
            if attempt.get("state") == "result_ready"
        }
        seen_results = set()
        for record in result_urls:
            if (
                not isinstance(record, dict)
                or set(record) != RESULT_URL_KEYS
                or record.get("attempt_id") in seen_results
                or not doc2x.valid_https_url(record.get("url"))
                or record.get("url_sha256")
                != "sha256:"
                + hashlib.sha256(record["url"].encode("utf-8")).hexdigest()
                or record.get("expires_at") is not None
                or record.get("validity_window_hours") != 24
                or not _valid_timestamp(record.get("observed_at"))
            ):
                return False
            expected = expected_results.get(record["attempt_id"])
            if (
                not isinstance(expected, dict)
                or record.get("task_id") != expected.get("task_id")
                or record.get("url_sha256") != expected.get("result_url_sha256")
                or record.get("observed_at") != expected.get("result_observed_at")
            ):
                return False
            seen_results.add(record["attempt_id"])
        return seen_results == set(expected_results)
    except (KeyError, IndexError, TypeError, ValueError, ConversionAttemptError):
        return False


def valid_history(history: list[dict], manifest: dict, private_state: dict) -> bool:
    if not valid_private_state(private_state, manifest):
        return False
    return _reduce_history(history, private_template=private_state) == (
        manifest,
        private_state,
    )


def resolve_history_state(
    history: list[dict], *, manifest_template: dict, private_template: dict
) -> tuple[dict, dict] | None:
    del manifest_template
    return _reduce_history(history, private_template=private_template)


def result_from_manifest(manifest: dict, *, work_bundle: str, outcome: str) -> dict:
    result = source_staging.result_from_manifest(
        manifest, work_bundle=work_bundle, outcome=outcome
    )
    attempt = manifest["conversion_attempts"][-1]
    result["conversion_attempt_state"] = attempt["state"]
    pending = attempt.get("pending_action")
    if isinstance(pending, dict):
        result["action_required"] = pending["kind"]
        result["action_id"] = pending["action_id"]
        result["evidence_hash"] = pending["evidence_hash"]
    return result
