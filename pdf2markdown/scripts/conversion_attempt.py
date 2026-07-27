from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from typing import NamedTuple

import bundle
import doc2x
import source_staging


# Schema version 2 (task 2.1b) is a hard break from version 1: it replaces the
# single `reason_code` attempt field with `reason`/`reason_detail` and adds the
# `authorization_kind` and `result_refresh_round_count` columns. design.md
# fixes this as a break with no migrator and no dual-write compatibility
# window, so _valid_attempt's exact `set(attempt) != ATTEMPT_KEYS` comparison
# and this version number both refuse a version 1 record: a v1 bundle fails
# closed as invalid_bundle rather than being upgraded in place. This number
# belongs to the conversion attempt record alone -- workflow.SCHEMA_VERSION and
# the bundle/source_staging/preflight/review constants of the same name are
# independent (design.md Decision 7) and stay at 1.
SCHEMA_VERSION = 2
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
POLL_RETRY_BASE_SECONDS = 8
POLL_WINDOW_SECONDS = 8 * 90
RESULT_PENDING_WINDOW_SECONDS = 8 * 90
# The upper bound on result_refresh_round_count (task 2.1d). None is the
# sentinel this change ships with -- unbounded -- so that
# result_refresh_rounds_exhausted's gate stays fully short-circuited: the
# default introduces no new decision branch. Which finite value to use, and
# which state transition an
# exhausted count feeds into, are Decision 10, deferred to a later change:
# this substep only lands the accounting and the injectable gate. The count
# records distinct result URLs only: the first delivery is 1, so count k means
# k - 1 genuine refreshes. Repeated delivery of the current URL and later
# reappearance of an older URL do not increase it, so this accounting does not
# bound a loop that continually returns one stale URL.
#
# Read from the module global inside result_refresh_rounds_exhausted at call
# time, never baked into a derived value at import -- the same pattern
# worst_case_admission_for_unknown_response's docstring establishes for
# TASK_ID_UPPER_BOUND_BYTES / RESULT_URL_UPPER_BOUND_BYTES, so a test can
# monkeypatch a small ceiling and reach the exhausted branch (design.md:305,
# "ceiling 以可注入常量测试").
RESULT_REFRESH_ROUND_CEILING = None
API_BASE = "https://api.aihubmax.com"
ATTEMPT_ID_PATTERN = re.compile(r"conversion-attempt-(0*[1-9][0-9]*)")
ACTION_ID_PATTERN = re.compile(r"conversion-decision-[0-9a-f]{32}")
# The schema v2 attempt field set. `reason_code` is gone; the four fields
# marked below replace it (design.md Migration Plan step 3 requires the whole
# change to land at once, because _valid_attempt compares the key set exactly
# and every partial step would be its own schema version).
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
        # --- schema v2, replacing v1's single `reason_code` -----------------
        # The closed, folded reason a record carries: a single-valued
        # function of the WIRE classification (FLAT_STATE_MIGRATION's key),
        # via its reason column -- not of the stored, folded `state` alone:
        # post-fold, `state == "failed"` alone spans ten distinct `reason`
        # values (task 2.1c changes `state`, not this field). It is *not*
        # the wire reason code -- it disagrees with the wire on the two
        # *renamed* contract rows named in LEGAL_TRIPLES' docstring.
        "reason",
        # The branch the wire actually took, kept only where `reason` cannot
        # carry it: `no_task_id` and `poll_transient` are the two *reasons*
        # whose wire reason code ranges over a set rather than a single value
        # (SUBMISSION_UNKNOWN_REASON_CODES / POLL_TRANSIENT_REASON_CODES) --
        # `REASON_DETAILS` (task 2.2a's public name for what task 2.1c built
        # as `_REASON_DETAIL_DOMAIN`, which is now only an alias to the same
        # dict) has been keyed by `reason`, not by the flat state, since task
        # 2.1c. None everywhere else -- no information is
        # lost, because for every other legal (state, reason) pair the wire
        # value is recoverable from that pair via LEGAL_TRIPLES' reason_code
        # column, not from `state` alone.
        "reason_detail",
        # authorization_kind was a global-None placeholder from task 2.1b
        # through task 2.2; task 2.3a gives it its first real value, "retry",
        # written only onto "authorized"-state records by
        # commit_retry_decision. Every other state still requires None (see
        # _valid_attempt). "initial" is still not a legal value of this
        # column: task 2.3b admits initial *authorizations* (by their key
        # set, see AUTHORIZATION_KEYS_BY_KIND), but the only writer of an
        # initial authorized record is task 2.3c, which is what extends this
        # column's domain.
        # result_refresh_round_count is no longer a placeholder as of task
        # 2.1d: it is the cumulative count of distinct result URLs
        # _poll_transition has observed for this attempt (see
        # RESULT_REFRESH_ROUND_CEILING and result_refresh_rounds_exhausted).
        "authorization_kind",
        "result_refresh_round_count",
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
# The two authorization shapes, keyed by the kind each one *is*. Task 2.3b
# replaces the single AUTHORIZATION_KEYS this used to be, because there are
# now two ways an attempt can come to be authorized and they carry different
# evidence:
#
#   * "retry" -- a consumed confirm decision. It names the pending action it
#     answers (action_id / evidence_hash), the basis text the operator typed
#     (basis_sha256), and the risk that operator accepted.
#   * "initial" -- task 2.3c's blocked recorded-credential gate, on the very
#     first attempt. Its evidence is the frozen source/preflight evidence,
#     and it accepts no duplicate-charge risk at all: not one create has been
#     sent yet, so there is no duplicate to risk and no decision to record.
#     action_id / basis_sha256 / accepted_risk are therefore absent, not None.
#
# This table is also the *discriminator*: a stored authorization declares its
# kind by its own key set (see _authorization_kind_of). The alternative --
# reading the attempt's `authorization_kind` column -- does not work, because
# that column does not survive the "authorized" -> "submitting" fold (see
# _submit_state and POLL_IMMUTABLE_ATTEMPT_KEYS' note below), while the
# authorization object itself rides along for the attempt's whole life.
#
# Note that "initial" is a proper subset of "retry". That is safe here only
# because every dispatch below tests set *equality*, never containment: no
# object's key set can equal both, so the two kinds stay mutually exclusive.
AUTHORIZATION_KEYS_BY_KIND = {
    "initial": frozenset(
        {
            "evidence_hash",
            "authorized_at",
        }
    ),
    "retry": frozenset(
        {
            "action_id",
            "evidence_hash",
            "authorized_at",
            "basis_sha256",
            "accepted_risk",
        }
    ),
}
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
# The keys _poll_state_from_intent (below) requires unchanged between the
# attempt an intent's `updated_attempt` proposes and the attempt already on
# file: identity/credential/authorization fields fixed at create/authorize
# time. An ATTEMPT_KEYS member absent here is simply not constrained by this
# comparison. Several such fields legitimately move within a single poll
# transition -- state, reason(_detail), http_status, upstream_status,
# poll_count, consecutive_transient_count, the poll/result timestamps, and
# the result_url fields.
#
# task 2.1d decision: result_refresh_round_count stays OUT of this set, for
# the same reason poll_count and consecutive_transient_count already are.
# It is not "the value at intent-write time must still hold at recovery
# time" (that window is never actually open -- once an intent is durable in
# history it cannot be tampered with; recovery only ever replays it) but
# "does this key change within the one poll transition the intent/active
# comparison spans". result_refresh_round_count is incremented by
# _poll_transition inside that very transition whenever a new result URL is
# observed, exactly like poll_count increments on every polling transition
# and consecutive_transient_count resets or increments on every one --
# putting it in POLL_IMMUTABLE_ATTEMPT_KEYS would turn every legitimate
# refresh into a spurious integrity_violation.
#
# authorization_kind decision (task 2.3a, resolving the "left undecided"
# note this comment used to carry): IN this set, alongside attempt_id and
# authorization.
#
# The membership test is not "is the value physically frozen" but "does this
# key change within the one poll transition the intent/active comparison
# spans" -- the same test result_refresh_round_count's note above applies,
# just with the opposite answer. Tracing every constructor and recovery path
# that can produce a poll's `updated_attempt`:
#   * commit_retry_decision is the only site that ever writes a non-None
#     authorization_kind ("retry", as of 2.3a), and it only ever writes it
#     onto a state == "authorized" record.
#   * "authorized" is not a member of POLL_ACTIVE_ATTEMPT_PAIRS (see that
#     set below), so _poll_transition's precondition check rejects any
#     attempt in that state before doing anything else -- an authorized
#     placeholder is never the `active` attempt a poll transition reads.
#   * _submit_state (the "authorized" -> "submitting" transition) does not
#     carry the placeholder's authorization_kind forward: it rebuilds the
#     next attempt from _attempt_state_columns("submitting") with no
#     authorization_kind argument, which defaults to None. So by the time an
#     attempt becomes pollable, its authorization_kind is already back to
#     None, same as every pre-2.3a record.
#   * _poll_transition (L2880-ish) and _submission_result_state (the
#     "submitting" -> "submitted"/"submission_unknown" fold) both call
#     _attempt_reason_columns without an authorization_kind argument, so
#     they always re-derive None -- an idempotent overwrite of the None the
#     attempt already carried, not a real change.
# So within any single poll transition's intent/active window,
# authorization_kind's value never actually changes -- it is None going in
# and None coming out, for the same structural reason attempt_id and
# authorization can't change: the field that could vary it (the "retry"
# value) never survives past the state that never gets polled.
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
        "authorization_kind",
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
# The shell every authorization intent carries, whatever kind it authorizes:
# the operation's identity and generations, the placeholder attempt it appends
# and the two hashes it is conditioned on. The per-kind evidence fields are
# added on top in AUTHORIZE_INTENT_KEYS_BY_KIND below (task 2.3c), which is
# defined next to AUTHORIZATION_KEYS_BY_KIND's other consumers.
_AUTHORIZE_INTENT_BASE_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "operation_id",
        "expected_generation",
        "new_generation",
        "at",
        "attempt",
        "previous_manifest_hash",
        "previous_private_hash",
    }
)
RETRY_INTENT_KEYS = _AUTHORIZE_INTENT_BASE_KEYS | frozenset(
    {"action_id", "evidence_hash", "basis_sha256"}
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

# The reason codes a create can land on when it produced no task ID.
SUBMISSION_UNKNOWN_REASON_CODES = frozenset(
    {
        "no_task_id",
        "invalid_transport_result",
        "network_result_unknown",
        "interrupted_before_result_commit",
    }
)

# poll_transient is the one wire classification POLL_STATE_CONTRACT cannot
# describe: it carries no upstream status and admits either of two reason
# codes, the second of which only a crash recovery produces.
POLL_TRANSIENT_REASON_CODES = frozenset(
    {"poll_transient", "result_private_payload_lost"}
)

# design.md Decision 1 -- the single enumeration of the 18 *wire* (flat)
# classifications doc2x still produces and the folded (attempt state, reason,
# conversion_state) triple each one stores from task 2.1c onward.
#
# --- What "flat state" means after the 2.1c fold ---------------------------
#
# Before 2.1c the 18 keys of this table were simultaneously (a) the value
# doc2x._classify / _classify_poll return in CreateResult.state /
# PollResult.state and (b) the value an attempt record stored in its `state`
# field. Task 2.1c splits those two roles apart:
#
#   * the KEYS stay the wire vocabulary -- doc2x.py is untouched by this
#     task, so a poll observation is still classified as one of these 18;
#   * the stored attempt `state` is now the folded value in column 0, drawn
#     from the closed 7-value ATTEMPT_STATES, with column 1's `reason`
#     carrying the discrimination the folded name gave up.
#
# So every table below that is keyed by one of these 18 names is keyed by a
# WIRE classification, not by a stored attempt state; the names say so
# (`_..._BY_FLAT_STATE`, `_poll_response_branches`). Anything that reads a
# stored record keys on `(state, reason)` instead -- and, for the one place
# the fold is not injective, on `upstream_status` as well (see
# _MANIFEST_STATE_BY_FOLDED_STATE below).
FLAT_STATE_MIGRATION = {
    # flat state: (attempt state, reason, top-level conversion_state)
    "not_started": ("authorized", None, "ready_to_submit"),
    "submitting": ("submitting", None, "submitting"),
    "submitted": ("submitted", None, "submitted"),
    "submission_unknown": ("submission_unknown", "no_task_id", "submission_unknown"),
    "pending": ("processing", None, "submitted"),
    "processing": ("processing", None, "submitted"),
    "result_pending": ("processing", None, "submitted"),
    "result_ready": ("result_ready", None, "result_downloading"),
    "unsafe_result_url": ("failed", "unsafe_result_url", "terminal_error"),
    "unexpected_result_count": (
        "failed", "unexpected_result_count", "terminal_error"
    ),
    "failed": ("failed", "task_failed", "awaiting_user"),
    "poll_transient": ("failed", "poll_transient", "recoverable_error"),
    "poll_unauthorized": (
        "failed", "poll_authentication_rejected", "recoverable_error"
    ),
    "task_unavailable": ("failed", "task_unavailable", "recoverable_error"),
    "credential_source_missing": (
        "failed", "credential_source_missing", "recoverable_error"
    ),
    "credential_source_changed": (
        "failed", "credential_fingerprint_changed", "recoverable_error"
    ),
    "poll_timeout": ("failed", "poll_timeout", "recoverable_error"),
    "result_pending_timeout": (
        "failed", "result_pending_timeout", "recoverable_error"
    ),
}
FLAT_STATE_DOMAIN = frozenset(FLAT_STATE_MIGRATION)

# design.md Decision 1 -- the closed seven-value domain a stored attempt
# `state` may take from task 2.1c onward. Derived from the migration table's
# target column rather than restated, so the fold has exactly one owner;
# test_attempt_state_domain_is_closed_to_seven_values pins the seven names
# against an independent literal.
ATTEMPT_STATES = frozenset(
    state for state, _reason, _conversion_state in FLAT_STATE_MIGRATION.values()
)

# design.md Decision 1 row 8, second form. Locally *detected* -- no wire
# classification ever produces it, so it lives outside the two
# characterization tables (FLAT_STATE_MIGRATION / LEGAL_TRIPLES stay 18
# rows of measured wire domain) and is spliced into every derivation
# that defines pair legality. Write side lands in 2.2c.
LOCALLY_DETECTED_PAIRS = {
    ("result_ready", "result_url_expired"): "recoverable_error",
}

# --- The recorded-credential gate's own pairs (task 2.3c) ------------------
#
# design.md Decision 2's `initial` branch. Like LOCALLY_DETECTED_PAIRS these
# are locally detected -- no wire classification produces them -- but they
# cannot join that table: _locally_detected_observations below *inherits* the
# wire columns of the single non-placeholder LEGAL_TRIPLES row that shares the
# re-labelled state, and `authorized`'s only row is `not_started`, which is a
# _NON_CONTRACT_STATES placeholder. The single-element unpacking would raise
# at import. The reason it cannot inherit is the same reason it does not need
# to: an authorized record is not a poll observation at all, so it never
# reaches the _LEGAL_POLL_OBSERVATIONS gate (_valid_attempt's "authorized"
# branch returns before it).
#
# The two ConfigError codes the gate can fail with are FLAT_STATE_MIGRATION
# keys, so design Decision 4's boundary rename (credential_source_changed ->
# credential_fingerprint_changed) is *read off* the migration table here
# rather than restated -- this dict is the one place the create-path gate's
# code -> reason mapping lives, and _CREDENTIAL_ERROR_PAIRS below is derived
# from it instead of carrying a second copy of the two folded names.
CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR = {
    config_error_code: FLAT_STATE_MIGRATION[config_error_code][1]
    for config_error_code in ("credential_source_missing", "credential_source_changed")
}

# The top-level conversion_state a gate record projects to: `ready_to_submit`,
# the same value the `retry` placeholder's ("authorized", None) pair projects
# to via FLAT_STATE_MIGRATION's `not_started` row.
#
# It is *not* `recoverable_error`, which is what FLAT_STATE_MIGRATION rows 15
# and 16 give the credential reasons. Those two rows describe a different
# pair: a credential failure observed while polling an already-submitted task,
# whose attempt state is `failed`. This pair's attempt has never been
# submitted, and design Decision 2 fixes its top-level state by requiring that
# "credential 恢复后走同一条路径进入 submitting" through _submit_state's
# placeholder consumption -- and _submit_state only admits a manifest in
# `ready_to_submit`. Projecting to `recoverable_error` would take the bundle
# out of the branch that both reuses the record and later consumes it.
_CREDENTIAL_GATE_CONVERSION_STATE = "ready_to_submit"
CREDENTIAL_GATE_AUTHORIZED_PAIRS = {
    ("authorized", reason): _CREDENTIAL_GATE_CONVERSION_STATE
    for reason in CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR.values()
}

# The legal `(state, reason)` pairs a stored attempt may carry. 18 wire rows
# collapse onto 16 pairs (pending/processing/result_pending all fold onto
# ("processing", None)), plus LOCALLY_DETECTED_PAIRS' keys and task 2.3c's
# CREDENTIAL_GATE_AUTHORIZED_PAIRS. This replaces schema v2's per-state
# `reason == FLAT_STATE_MIGRATION[state][1]` check: for the wire-derived pairs
# the set of legal (state, reason) combinations is identical, it is just no
# longer indexed by a name the record has stopped carrying.
LEGAL_STATE_REASON_PAIRS = (
    frozenset(
        (state, reason)
        for state, reason, _conversion_state in FLAT_STATE_MIGRATION.values()
    )
    | frozenset(LOCALLY_DETECTED_PAIRS)
    | frozenset(CREDENTIAL_GATE_AUTHORIZED_PAIRS)
)

# design.md Decision 4 / task 2.2a -- the closed twelve-value domain a stored
# attempt's `reason` column may take. Eleven of the twelve are already the
# distinct non-null values FLAT_STATE_MIGRATION's `reason` column (column 1)
# produces -- LEGAL_TRIPLES.reason is read straight off that same column, so
# deriving from either table is equivalent and there is no second literal of
# those eleven values to drift out of sync with the migration table.
#
# `result_url_expired` is the twelfth and is deliberately NOT derived: no
# flat_state folds onto it yet. Task 2.2a only closes the reason *vocabulary*;
# wiring the local-expiry branch (raw_conversion.py's
# result_reference_is_expired) to actually emit a `result_url_expired`
# attempt, and adding its LEGAL_TRIPLES row, is the remaining half of design.md
# Decision 4 -- out of this substep's allowed file set (FLAT_STATE_MIGRATION
# and LEGAL_TRIPLES' values are unchanged here). It is listed below so the
# vocabulary itself closes at twelve values in one place, ahead of the
# write-side wiring that will start emitting the twelfth member.
_REASONS_FROM_FLAT_STATE_MIGRATION = frozenset(
    reason
    for _state, reason, _conversion_state in FLAT_STATE_MIGRATION.values()
    if reason is not None
)
CONVERSION_REASONS = _REASONS_FROM_FLAT_STATE_MIGRATION | frozenset(
    {"result_url_expired"}
)

# Which folded reasons may carry a non-null reason_detail, and the closed set
# each one may draw from. The two domained reasons are exactly the ones whose
# wire value ranges over a set instead of being a single-valued function of
# the wire classification: every other classification's wire code is
# recoverable from LEGAL_TRIPLES' reason_code column, so keeping it in
# reason_detail would store a second copy of something already implied.
#
# Keyed by `reason` rather than by state since 2.1c: the two keys used to be
# the flat states submission_unknown and poll_transient, which fold onto
# ("submission_unknown", "no_task_id") and ("failed", "poll_transient"). The
# reason alone is enough -- "no_task_id" and "poll_transient" each occur on
# exactly one row of FLAT_STATE_MIGRATION -- and keying on the pair would
# carry a redundant first element.
#
# Anything outside the domain must be None: _valid_reason_detail enforces
# this and _attempt_reason_columns produces it by reading this same dict, so
# the writer and the validator read one table rather than two agreeing
# conditionals.
#
# REASON_DETAILS is task 2.2a's public name for this table.
# `_REASON_DETAIL_DOMAIN` is kept only as an alias to the exact same dict
# object (not a second literal), because task 2.1c's producer
# (_attempt_reason_columns) and validator (_valid_reason_detail) below, and
# test_reason_detail_producer_and_validator_read_one_table's monkeypatch, all
# still read it by that name.
REASON_DETAILS = {
    "no_task_id": SUBMISSION_UNKNOWN_REASON_CODES,
    "poll_transient": POLL_TRANSIENT_REASON_CODES,
}
_REASON_DETAIL_DOMAIN = REASON_DETAILS


def _attempt_reason_columns(
    flat_state: str, reason_code: str | None, *, authorization_kind: str | None = None
) -> dict:
    """The three schema v2 attempt columns rewritten fresh on every write:
    `reason`/`reason_detail`, which replace v1's single `reason_code` and are
    recomputed from the wire classification, plus `authorization_kind`, which
    is not derived from anything -- it is an explicit keyword argument that
    defaults to None (see below).

    Every site that writes an attempt builds these three fields here, so the
    folded vocabulary has exactly one producer and cannot drift between the
    create path, the poll path and the two recovery paths.

    `flat_state` is a WIRE classification (a FLAT_STATE_MIGRATION key -- what
    doc2x returned, or what the caller is about to record); `reason_code` is
    the wire code the transport reported for it (None where there was no
    response, e.g. the placeholder and submitting records).

    reason is FLAT_STATE_MIGRATION's folded target reason -- deliberately not
    the wire code, which disagrees with it on poll_unauthorized and
    credential_source_changed. reason_detail keeps the wire code only for the
    two reasons whose code is not implied by the classification.

    authorization_kind defaults to None -- the correct value for every write
    site except one: commit_retry_decision's authorized placeholder is the
    sole caller (task 2.3a) that passes `authorization_kind="retry"` through
    _attempt_state_columns below, because it is the only site that is ever
    writing a real, non-None discriminator. Every other call site (the
    "submitting" columns in _submit_state/its crash-recovery twin, the
    submission-result fold in _submission_result_state, and _poll_transition's
    per-poll `.update()`) leaves the parameter at its default, which is why
    the field never survives past the one "authorized" record it was set on:
    _submit_state builds the next attempt from _attempt_state_columns("submitting")
    fresh rather than carrying the placeholder's authorization_kind forward
    (see _submit_state).

    `result_refresh_round_count` is deliberately NOT one of this function's
    columns as of task 2.1d, even though it was carried here (always 0) as a
    2.1b placeholder: unlike the three columns above, it is not a pure
    function of (flat_state, reason_code) recomputed fresh on every write --
    it is a cumulative, carry-forward-or-increment counter that must survive
    poll observations unrelated to a result URL. A caller that `.update()`s
    an existing attempt dict with this function's return value (as
    _poll_transition does on every poll) relies on that omission to leave
    result_refresh_round_count exactly as the attempt already carried it;
    only _poll_transition's own new-URL branch and _attempt_state_columns
    below (a genuinely fresh attempt) ever set it.
    """
    reason = FLAT_STATE_MIGRATION[flat_state][1]
    return {
        "reason": reason,
        "reason_detail": (
            reason_code
            if reason_code in _REASON_DETAIL_DOMAIN.get(reason, frozenset())
            else None
        ),
        "authorization_kind": authorization_kind,
    }


def _attempt_state_columns(
    flat_state: str, *, authorization_kind: str | None = None
) -> dict:
    """The folded `state` a record for `flat_state` stores, plus its reason
    columns and a fresh `result_refresh_round_count` of 0.

    The one place the fold is applied on the write side for a *single*
    FLAT_STATE_MIGRATION row. Callers used to write `"state": <wire
    classification>` next to a separate _attempt_reason_columns call; going
    through one helper means the stored state and the stored reason can
    never come from different rows of FLAT_STATE_MIGRATION.

    `_credential_gate_state_columns` (task 2.3c) is a deliberate exception,
    not a second copy of this helper: a gate record's state and reason are
    read off two *different* rows on purpose (the placeholder's own
    "not_started" state paired with the config-error code's wire reason), so
    it cannot be built by calling this helper with a single `flat_state`.

    Fresh-attempt creation and recovery reconstruction of the submitting
    predecessor both use this helper. In either case 0 is correct: no
    distinct result URL has yet been recorded for that attempt.

    `authorization_kind` passes straight through to _attempt_reason_columns;
    see that function's docstring for why only commit_retry_decision's
    "not_started" (folds to "authorized") call ever supplies a non-None
    value.
    """
    return {
        "state": FLAT_STATE_MIGRATION[flat_state][0],
        **_attempt_reason_columns(
            flat_state, None, authorization_kind=authorization_kind
        ),
        "result_refresh_round_count": 0,
    }


def _valid_reason_detail(reason: str | None, reason_detail) -> bool:
    domain = _REASON_DETAIL_DOMAIN.get(reason)
    return reason_detail is None if domain is None else reason_detail in domain


class _LegalTriple(NamedTuple):
    """One row of the single owner table of attempt state legality.

    There is one row per WIRE classification (`flat_state`, a
    FLAT_STATE_MIGRATION key), because that is what the columns describe: the
    HTTP status, upstream status and wire reason_code a response so classified
    may carry. `conversion_state`, `attempt_state` and `reason` are the folded
    triple that classification stores, read straight off FLAT_STATE_MIGRATION
    so this table cannot hold a second, drifting copy of the fold.

    `reason_code` is today's on-the-wire code and is deliberately *not* the
    same column as `reason`: within this one table they disagree on exactly
    four rows, and that is by design, not drift.

      * two contract rows, where the folded vocabulary renames the wire code:

          flat_state                | reason_code               | reason
          --------------------------+---------------------------+------------------------------
          poll_unauthorized         | poll_unauthorized         | poll_authentication_rejected
          credential_source_changed | credential_source_changed | credential_fingerprint_changed

      * two placeholder rows, submission_unknown and poll_transient, whose
        wire code ranges over a set (SUBMISSION_UNKNOWN_REASON_CODES /
        POLL_TRANSIENT_REASON_CODES) rather than being single valued. Their
        reason_code is None here by construction and their real wire value
        lives in the attempt's `reason_detail` field, never in this table.

    The four states that are not single-valued poll observations --
    _NON_CONTRACT_STATES: not_started, submitting, submission_unknown and
    poll_transient, whose reason_code/http_status vary across
    POLL_TRANSIENT_REASON_CODES and _WORST_CASE_HTTP_STATUSES -- carry None
    placeholders in reason_code/http_status/upstream_status. Nothing derives
    from them there, and test_legal_triples_is_the_single_owner_of_state_
    legality pins all twelve of those cells to None so a reader that does
    start using them (the schema v1 downgrade in the tests already does)
    cannot silently pick up a stray value.
    """

    flat_state: str
    conversion_state: str
    attempt_state: str
    reason: str | None
    reason_code: str | None
    http_status: int | None
    upstream_status: str | None


def _legal_triple(
    flat_state: str,
    reason_code: str | None,
    http_status: int | None,
    upstream_status: str | None,
) -> _LegalTriple:
    """A LEGAL_TRIPLES row: the wire columns spelled out, the folded columns
    read off FLAT_STATE_MIGRATION."""
    attempt_state, reason, conversion_state = FLAT_STATE_MIGRATION[flat_state]
    return _LegalTriple(
        flat_state,
        conversion_state,
        attempt_state,
        reason,
        reason_code,
        http_status,
        upstream_status,
    )


# design.md Decision 1 / Task 2.1a -- the single owner table of attempt state
# legality. It is the union of what used to be two independently maintained
# copies: POLL_STATE_CONTRACT (the (http_status, upstream_status) pair a poll
# observation may carry) and valid_private_state's expected_manifest_state
# (the top-level conversion_state each state projects to). Both are derived
# from this table below, so the two can never drift out of sync.
LEGAL_TRIPLES = (
    _legal_triple("not_started", None, None, None),
    _legal_triple("submitting", None, None, None),
    _legal_triple("submitted", None, 200, None),
    _legal_triple("submission_unknown", None, None, None),
    _legal_triple("pending", None, 200, "pending"),
    _legal_triple("processing", None, 200, "processing"),
    _legal_triple("result_pending", None, 200, "completed"),
    _legal_triple("result_ready", None, 200, "completed"),
    _legal_triple("unsafe_result_url", "unsafe_result_url", 200, "completed"),
    _legal_triple(
        "unexpected_result_count", "unexpected_result_count", 200, "completed"
    ),
    _legal_triple("failed", "task_failed", 200, "failed"),
    _legal_triple("poll_transient", None, None, None),
    _legal_triple("poll_unauthorized", "poll_unauthorized", 401, None),
    _legal_triple("task_unavailable", "task_unavailable", 404, None),
    _legal_triple(
        "credential_source_missing", "credential_source_missing", None, None
    ),
    _legal_triple(
        "credential_source_changed", "credential_source_changed", None, None
    ),
    _legal_triple("poll_timeout", "poll_timeout", None, None),
    _legal_triple(
        "result_pending_timeout", "result_pending_timeout", None, "completed"
    ),
)

# The wire classifications LEGAL_TRIPLES carries that are not single-valued
# poll observations: not_started and submitting precede any poll response, and
# submission_unknown is written by the create path directly, never by a poll
# observation. POLL_STATE_CONTRACT and _poll_response_branches must both
# exclude these three; they exclude poll_transient alongside them, which is
# why the exclusion domain has its own name (_NON_CONTRACT_STATES below)
# rather than being spelled out per use.
NON_POLL_OBSERVATIONS = frozenset({"not_started", "submitting", "submission_unknown"})

# The exclusion domain both derivations below apply: the wire classifications
# whose LEGAL_TRIPLES row is a placeholder rather than a single-valued
# contract entry. It has a name and a single definition because both
# _LEGAL_TRIPLE_BY_FLAT_STATE and POLL_STATE_CONTRACT must filter on
# *exactly* the same set: if the index domain and the contract domain ever
# disagree, a classification is either indexed without a contract or given a
# contract it cannot satisfy. Written inline in each comprehension's `if`,
# that invariant depended on two expressions staying textually identical --
# the same drift shape task 2.1a removed from POLL_STATE_CONTRACT and
# expected_manifest_state, reintroduced one level down.
_NON_CONTRACT_STATES = NON_POLL_OBSERVATIONS | frozenset({"poll_transient"})

# Indexed over the same domain POLL_STATE_CONTRACT describes -- every
# non-transient poll observation, excluding _NON_CONTRACT_STATES. Filtering
# here (rather than indexing all 18 rows) means a future widening of
# _poll_response_branches' input domain raises KeyError instead of silently
# returning a placeholder row shaped like a single-valued contract entry -- a
# shape _valid_attempt would reject, and exactly the shape
# _poll_response_branches' docstring promises never to budget capacity for.
_LEGAL_TRIPLE_BY_FLAT_STATE = {
    row.flat_state: row
    for row in LEGAL_TRIPLES
    if row.flat_state not in _NON_CONTRACT_STATES
}

# The top-level conversion_state each *stored* attempt projects to, keyed by
# the folded (state, reason) pair, over LEGAL_TRIPLES' full 18-row domain (16
# distinct pairs). valid_private_state's expected_manifest_state and
# _conversion_state_for_attempt's (POLL_RESULT_STATES-filtered)
# _FOLDED_POLL_RESULT_CONVERSION_STATE both derive from this single dict.
#
# The pair is enough here even though the fold is not injective: the three
# rows that collapse onto ("processing", None) -- pending, processing,
# result_pending -- all project to "submitted", so the collapsed key is
# still single valued. (The one place the collapse *does* matter is where a
# rule applied to result_pending but not to its two siblings; those sites
# discriminate on upstream_status, which is exactly what distinguishes the
# three LEGAL_TRIPLES rows. design.md Decision 1 note 3.)
#
# Spliced with LOCALLY_DETECTED_PAIRS, whose one member has no LEGAL_TRIPLES
# row to read a conversion_state off (see that constant): the dict's values
# *are* the top-level state those pairs project to.
_WIRE_MANIFEST_STATE_BY_FOLDED_STATE = {
    (row.attempt_state, row.reason): row.conversion_state for row in LEGAL_TRIPLES
}
# Task 2.2c -- a plain `|` union has the right operand silently win on a key
# collision (test_locally_detected_pairs_have_no_wire_row_key_collision pins
# today's disjointness as a value-level assertion, but nothing short of that
# test failing stopped a future colliding pair from being merged in
# production). This mirrors _locally_detected_observations' single-element-
# unpacking guard, which is the production-side counterpart already raising
# at import time: a colliding addition here must fail exactly the same way,
# not just when the test suite happens to run.
#
# Minor fix (review round 1): a bare `assert` is stripped under `python -O`,
# which would silently drop this guard in exactly the deployment mode where
# import-time invariants matter most. `raise` is what actually makes this
# symmetric with the unpacking guard above -- that guard is an unconditional
# ValueError, not an `assert`.
#
# Task 2.3c adds a second locally-owned table (CREDENTIAL_GATE_AUTHORIZED_PAIRS)
# to splice in, so the guard is written once over both rather than copied: two
# spliced tables must also not collide with *each other*, which a per-table
# copy of the wire check would not have noticed.
_LOCALLY_OWNED_PAIR_TABLES = {
    "LOCALLY_DETECTED_PAIRS": LOCALLY_DETECTED_PAIRS,
    "CREDENTIAL_GATE_AUTHORIZED_PAIRS": CREDENTIAL_GATE_AUTHORIZED_PAIRS,
}
_MANIFEST_STATE_BY_FOLDED_STATE = dict(_WIRE_MANIFEST_STATE_BY_FOLDED_STATE)
for _table_name, _table in _LOCALLY_OWNED_PAIR_TABLES.items():
    _collision = frozenset(_table) & frozenset(_MANIFEST_STATE_BY_FOLDED_STATE)
    if _collision:
        raise ValueError(
            f"{_table_name} collides with an already-owned (state, reason) "
            f"pair: {sorted(_collision)!r}"
        )
    _MANIFEST_STATE_BY_FOLDED_STATE.update(_table)
# Task 2.2c review round 2, Minor #7 -- these names have no reader past the
# guard above; deleting them keeps them from lingering as module-level
# attributes someone could mistake for maintained constants.
del _table_name, _table, _collision

# The (http_status, upstream_status) pair each non-transient wire
# classification must carry.
#
# The wire reason_code used to be a third element of this tuple. It has had no
# production reader since schema v2 -- an attempt no longer stores the wire
# code at all, _poll_response_branches builds its worst-case observations from
# _LEGAL_TRIPLE_BY_FLAT_STATE directly, and _valid_attempt now judges a stored
# record against _LEGAL_POLL_OBSERVATIONS -- so task 2.1c drops it. The wire
# column itself is unchanged and still owned by LEGAL_TRIPLES.reason_code;
# only this derived copy of it is gone.
POLL_STATE_CONTRACT = {
    row.flat_state: (row.http_status, row.upstream_status)
    for row in LEGAL_TRIPLES
    if row.flat_state not in _NON_CONTRACT_STATES
}

# What _valid_attempt judges a stored poll observation against: the legal
# (state, reason, http_status, upstream_status) quadruples, over the same
# contract domain POLL_STATE_CONTRACT covers.
#
# This is exactly as tight as the pre-fold `(http_status, upstream_status) ==
# POLL_STATE_CONTRACT[state][:2]` check it replaces. Before the fold, the
# stored flat state named one row and the pair had to match that row; after
# the fold the record no longer names a row, so legality is membership of the
# whole quadruple. The set of legal records is unchanged -- an attempt that
# used to be `state=pending, upstream=completed` (illegal) is now
# `state=processing, reason=None, upstream=completed`, which is the
# result_pending row and was always legal under that name.
_WIRE_POLL_OBSERVATIONS = frozenset(
    (row.attempt_state, row.reason, row.http_status, row.upstream_status)
    for row in LEGAL_TRIPLES
    if row.flat_state not in _NON_CONTRACT_STATES
)


def _locally_detected_observations():
    """The quadruples LOCALLY_DETECTED_PAIRS' members occupy in the gate above.

    A locally detected pair does not describe a *new* observation: local
    detection only re-labels the `reason` of a record some wire row already
    wrote. `result_url_expired` is found by checking the validity window of a
    result URL a `result_ready` record is already holding -- nothing is
    re-polled, so that record still carries the http_status and
    upstream_status of the poll that produced it. The wire columns are
    therefore *inherited* from the row being re-labelled rather than restated:
    LEGAL_TRIPLES stays their single owner and there is no second literal of
    them to drift out of sync with it.

    Single-element unpacking (rather than a `for` that yields one quadruple
    per matching row) is what keeps that inheritance honest. It is well
    defined only while the re-labelled state has exactly one wire row --
    true of `result_ready`, false of `failed`, which nine non-transient wire
    rows share (ten flat states fold onto `failed`, but `poll_transient` is
    filtered out by the `_NON_CONTRACT_STATES` check above, leaving nine
    candidates for the set this unpacks). A future locally-detected pair
    re-labelling `failed` must state which row it inherits from; unpacking
    makes that an import-time ValueError instead of silently widening the
    gate by nine quadruples.
    """
    for state, reason in LOCALLY_DETECTED_PAIRS:
        (wire_row,) = {
            row
            for row in LEGAL_TRIPLES
            if row.attempt_state == state
            and row.flat_state not in _NON_CONTRACT_STATES
        }
        yield (state, reason, wire_row.http_status, wire_row.upstream_status)


_LEGAL_POLL_OBSERVATIONS = _WIRE_POLL_OBSERVATIONS | frozenset(
    _locally_detected_observations()
)

# The folded pair a poll_transient observation stores. Named because five
# separate rules key on it and `("failed", "poll_transient")` spelled out five
# times is five chances to typo a reason the fold made load-bearing.
_POLL_TRANSIENT_PAIR = ("failed", "poll_transient")

# The two pairs whose records carry an exponential backoff: a positive
# consecutive_transient_count and a next_poll_at derived from it. Pre-fold
# this was the flat set {"task_unavailable", "poll_transient"}; both fold onto
# `failed`, so the reason is now the whole of the discrimination.
_BACKOFF_PAIRS = frozenset({("failed", "task_unavailable"), _POLL_TRANSIENT_PAIR})

# The two pairs a *local* credential failure produces on the poll path. These
# never reached the network, so they are exempt from the poll accounting the
# other observations must satisfy (poll_count > 0, consecutive_transient_count
# reset, next_poll_at cleared). Pre-fold this was the flat set
# {"credential_source_missing", "credential_source_changed"}; note the second
# reason is `credential_fingerprint_changed`, the folded rename.
#
# Task 2.3c derives the two reasons from CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR
# rather than restating them: that table already owns "which ConfigError codes
# are credential failures, and what each one folds to", and a second literal of
# the folded rename here is exactly the drift
# test_every_refolded_pair_set_names_a_legal_pair exists to catch. The `failed`
# first element is what distinguishes these from the create-path gate's
# ("authorized", <same reason>) pairs.
_CREDENTIAL_ERROR_PAIRS = frozenset(
    ("failed", reason)
    for reason in CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR.values()
)

# The one pair the fold left ambiguous, plus the discriminator that resolves
# it. `result_pending` folds onto ("processing", None) together with `pending`
# and `processing`; the three are distinguished by upstream_status, which is
# what already distinguishes their LEGAL_TRIPLES rows (design.md Decision 1
# note 3). Rules that applied to `result_pending` but not to its two siblings
# -- the result-pending window requirement in _valid_attempt, the deadline
# check in timeout_before_poll -- must therefore test upstream_status too.
_PROCESSING_PAIR = ("processing", None)
_RESULT_PENDING_UPSTREAM_STATUS = "completed"
_RESULT_PENDING_TIMEOUT_PAIR = ("failed", "result_pending_timeout")


def _is_result_pending(state, reason, upstream_status) -> bool:
    """Whether a stored record is the folded form of flat `result_pending`."""
    return (state, reason) == _PROCESSING_PAIR and (
        upstream_status == _RESULT_PENDING_UPSTREAM_STATUS
    )


# The result classifications a poll observation may commit. "submitted" is in
# POLL_STATE_CONTRACT because an attempt can be *in* that state, but no poll
# response can return it.
POLL_RESULT_STATES = frozenset(POLL_STATE_CONTRACT) - {"submitted"} | {
    "poll_transient"
}

# The folded (state, reason) pairs an active attempt may be in when a poll
# observation is applied to it -- the re-keyed POLL_ACTIVE_ATTEMPT_STATES,
# whose 13 flat members collapse onto these 11 pairs, plus task 2.2c's
# locally-detected twelfth: design.md Decision 5 case 4c admits a
# result_url_expired attempt to re-poll the very same Doc2X task in order to
# refresh its result URL (resume_same_conversion_task semantics) --
# raw_conversion.py's local-expiry branch now writes exactly this pair (see
# LOCALLY_DETECTED_PAIRS), and that re-poll runs through the same
# commit_poll_result -> _poll_transition path as every other admitted pair.
#
# The fold is why this had to become a pair set rather than a state set: eight
# of the ten reasons `failed` now covers may keep polling, but
# ("failed", "task_failed") and ("failed", "unexpected_result_count") may not.
# Keying on `failed` alone would admit both of those and let a terminal
# attempt be polled again.
POLL_ACTIVE_ATTEMPT_PAIRS = frozenset(
    {
        ("submitted", None),
        # pending / processing / result_pending; all three were admitted
        # before the fold, so the collapsed pair loses nothing.
        _PROCESSING_PAIR,
        ("failed", "credential_source_missing"),
        ("failed", "credential_fingerprint_changed"),
        ("failed", "poll_authentication_rejected"),
        ("failed", "task_unavailable"),
        ("failed", "poll_transient"),
        ("failed", "poll_timeout"),
        ("failed", "result_pending_timeout"),
        ("failed", "unsafe_result_url"),
        ("result_ready", None),
        ("result_ready", "result_url_expired"),
    }
)

# The folded pairs whose records may carry a pending action, and the kind each
# one takes. Pre-fold this was three flat states -- submission_unknown,
# failed, unexpected_result_count -- and both _valid_pending_action's
# expected_kinds and valid_private_state's confirm-mode invariant listed them
# separately. They are one table now: `failed` covers ten reasons after the
# fold, and reusing the flat set would demand a pending action from all ten,
# instantly invalidating every recoverable record (poll_transient,
# task_unavailable, ...).
#
# Task 2.4 (design.md Decision 5/9.3): the closed, workflow-facing action
# vocabulary a conversion attempt or a raw_conversion record's pending_action
# may carry. The four `resolve_*` kinds this replaces --
# resolve_submission_unknown, resolve_task_failed,
# resolve_unexpected_result_count (all three below) and
# raw_conversion.py's resolve_unexpected_result_layout -- all asked the
# operator for the exact same decision (authorize a new, separately charged
# conversion attempt), so distinguishing them by kind name was spurious
# detail, not real discrimination. Folding them here means
# commit_retry_decision's old `pending.kind` comparison loses the (state,
# reason) information it used to get for free from the kind name; that
# information moves onto RETRY_AUTHORIZABLE_TRIPLES below, which
# discriminates directly on (conversion_state, attempt state, reason)
# instead.
#
# Disjoint from workflow.ERROR_PATH_ACTIONS by production mechanism, not by
# value-clash avoidance -- workflow.py:57-66 spells out why the two tables
# are allowed to share the action_required key without being each other's
# complement. Some members (e.g. resume_pending_conversion_operation) have no
# consumer yet; task 3.1 wires those up. This table only has to be closed and
# disjoint from the error-path vocabulary now.
CONVERSION_ACTIONS = frozenset(
    {
        "resume_pending_conversion_operation",
        "restore_recorded_aihub_credential",
        "resume_same_conversion_task",
        "authorize_new_conversion_attempt",
        "adopt_conversion_result",
    }
)

# The single CONVERSION_ACTIONS member every confirmable pending_action now
# carries. Named once so every producer -- CONFIRMABLE_PENDING_KINDS below,
# the submission-result and poll-result writers further down, and
# raw_conversion.py's layout-ambiguity writer -- reads the same value instead
# of repeating the string literal.
AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND = "authorize_new_conversion_attempt"
assert AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND in CONVERSION_ACTIONS

CONFIRMABLE_PENDING_KINDS = {
    ("submission_unknown", "no_task_id"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ("failed", "task_failed"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ("failed", "unexpected_result_count"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
}
CONFIRMABLE_PAIRS = frozenset(CONFIRMABLE_PENDING_KINDS)

# The folded pairs timeout_before_poll judges against the *poll* deadline.
# Pre-fold flat set: {"pending", "processing", "poll_transient",
# "task_unavailable", "unsafe_result_url"} -- note it excluded
# `result_pending`, so the ("processing", None) member has to be qualified by
# upstream_status at the call site.
_POLL_DEADLINE_PAIRS = frozenset(
    {
        _PROCESSING_PAIR,
        _POLL_TRANSIENT_PAIR,
        ("failed", "task_unavailable"),
        ("failed", "unsafe_result_url"),
    }
)

# The folded pairs whose successor poll restarts the poll window instead of
# continuing it. Pre-fold flat set: {"poll_timeout", "result_pending_timeout",
# "result_ready"}. Task 2.2c adds ("result_ready", "result_url_expired"):
# re-polling a locally-detected-expired result_ready attempt is the exact same
# event ("restart the window, this task is being asked for its result again")
# as re-polling a wire ("result_ready", None) one -- only the reason label
# changed. Without this, the stale window from the original poll survives
# untouched (reset_window stays False for the new pair) and last_polled_at
# (set to the refresh's own `at`) ends up past the untouched poll_deadline_at,
# failing _valid_poll_fields' `last > deadline` check.
#
# Flow-debt record (review round 1, Important #3): this extension is a
# production-semantics change, made under a prompt-level authorization that
# covered "no new livelock" but not this deeper change, and was correctly
# flagged as owed a BLOCKED report that the round-1 implementation skipped.
# The stronger argument that survived review: before this change, the exact
# physical path this pair now names (a re-poll observation applied to a
# stored ("result_ready", ...) attempt) was already routed through this same
# `active_pair in _POLL_WINDOW_RESET_PAIRS` check via ("result_ready", None)
# -- the only member of POLL_ACTIVE_ATTEMPT_PAIRS a result_ready attempt
# could ever carry pre-2.2c. Adding ("result_ready", "result_url_expired")
# does not admit a new physical path into window-reset handling; it keeps
# the *same* path reachable after 2.2c renamed that attempt's reason without
# changing its state. Relation preserved, not extended -- and in particular
# it does not widen the livelock surface task 2.1d closed (see
# LEDGER_RESULT_REJECTIONS / raw_conversion._reference_already_unavailable),
# since that surface is about a rejection record repeating forever, not
# about which pairs restart a poll window.
_POLL_WINDOW_RESET_PAIRS = frozenset(
    {
        ("failed", "poll_timeout"),
        _RESULT_PENDING_TIMEOUT_PAIR,
        ("result_ready", None),
        ("result_ready", "result_url_expired"),
    }
)

# Every module-level set of folded (state, reason) pairs, so
# test_every_refolded_pair_set_names_a_legal_pair can prove none of them
# names a pair no record can ever carry. A mistyped reason in any of these
# does not raise -- the rule it keys simply stops firing, silently.
#
# Not every inline `(state, reason) == (...)` comparison in the codebase is
# registered here on purpose: registration exists for a pair that is *shared*
# across multiple rules or call sites -- the way `_POLL_TRANSIENT_PAIR` is
# spelled out five times -- where a typo in one copy would silently diverge
# from the others. A pair literal compared exactly once, inline, at its own
# call site (workflow.py's `== ("failed", "task_failed")` and
# `== ("failed", "unsafe_result_url")`) has only one reader: a typo there
# breaks that call site's own behaviour and its own test directly, not
# silently, so it does not need this registry's cross-copy protection.
#
# (Review round 1, Minor #6: this used to also cite conversion_attempt.py's
# own `elif active_pair == ("result_ready", None):` as a single-reader
# example. That branch was generalized to `elif active.get("state") ==
# "result_ready":` -- see the Important #3 flow-debt note at its definition
# -- so it is no longer a `(state, reason)` pair comparison at all and the
# citation is removed rather than updated to a non-example.)
_REFOLDED_PAIR_SETS = {
    "POLL_ACTIVE_ATTEMPT_PAIRS": POLL_ACTIVE_ATTEMPT_PAIRS,
    "CONFIRMABLE_PAIRS": CONFIRMABLE_PAIRS,
    "_BACKOFF_PAIRS": _BACKOFF_PAIRS,
    "_CREDENTIAL_ERROR_PAIRS": _CREDENTIAL_ERROR_PAIRS,
    "_POLL_DEADLINE_PAIRS": _POLL_DEADLINE_PAIRS,
    "_POLL_WINDOW_RESET_PAIRS": _POLL_WINDOW_RESET_PAIRS,
    "_PROCESSING_PAIR": frozenset({_PROCESSING_PAIR}),
    "_POLL_TRANSIENT_PAIR": frozenset({_POLL_TRANSIENT_PAIR}),
    "_RESULT_PENDING_TIMEOUT_PAIR": frozenset({_RESULT_PENDING_TIMEOUT_PAIR}),
}

# Task 2.3c -- the `reason` column each authorization kind may pair with on an
# "authorized" record. Widening authorization_kind's authorized-state domain
# from {"retry"} to {"retry", "initial"} (see _valid_attempt) is not enough on
# its own: without this table an "initial" record could carry reason None (a
# gate record that records no gate) and a "retry" placeholder could carry a
# credential reason it never observed. Both are refused here.
#
# Keyed by kind and pinned to AUTHORIZATION_KEYS_BY_KIND's key set below, so a
# third kind cannot be added to that table without either giving it a reason
# domain here or failing at import -- rather than silently inheriting one.
AUTHORIZED_STATE_REASONS_BY_KIND = {
    # A retry placeholder's reason is read off the same row "not_started"'s
    # own fold reads (FLAT_STATE_MIGRATION["not_started"] == ("authorized",
    # None, "ready_to_submit")) rather than restated as a literal `None`, so
    # this cell and the fold it describes cannot silently drift apart.
    "retry": frozenset({FLAT_STATE_MIGRATION["not_started"][1]}),
    "initial": frozenset(
        reason for _state, reason in CREDENTIAL_GATE_AUTHORIZED_PAIRS
    ),
}
if set(AUTHORIZED_STATE_REASONS_BY_KIND) != set(AUTHORIZATION_KEYS_BY_KIND):
    raise ValueError(
        "AUTHORIZED_STATE_REASONS_BY_KIND must name exactly the kinds "
        "AUTHORIZATION_KEYS_BY_KIND names: "
        f"{sorted(AUTHORIZATION_KEYS_BY_KIND)!r}"
    )
# The whole of authorization_kind's authorized-state domain, derived from the
# kind table rather than spelled out, so the validator's widening and the two
# shapes it dispatches on can never name different sets.
AUTHORIZED_STATE_KINDS = frozenset(AUTHORIZATION_KEYS_BY_KIND)

# The durable journal each kind of authorization writes. Task 2.3c gives the
# initial authorization its own event names rather than reusing the retry
# pair: the event name is the one part of an append-only record that can never
# be corrected later, and calling a first-attempt credential gate a "retry"
# would be a permanent lie in the ledger. The three sites that consume these
# events -- _authorize_state_from_intent, recover_interrupted_attempt's
# dangling-intent branch and apply_committed_operations' reducer -- all read
# these tables instead of a literal, so the pair stays a two-line addition.
AUTHORIZE_INTENT_EVENT_BY_KIND = {
    "retry": "conversion_retry_intent",
    "initial": "conversion_authorize_initial_intent",
}
AUTHORIZE_COMMITTED_EVENT_BY_KIND = {
    "retry": "conversion_retry_committed",
    "initial": "conversion_authorize_initial_committed",
}
#
# Version stance (same shape as task 2.2c's and _valid_attempt's below, at
# its own authorization_kind widening): this new event pair is a
# persisted-format extension, not a compatible widening.
# It is a deliberate hard break *inside an unreleased change* -- there is no
# migrator and no dual-write window, and SCHEMA_VERSION stays 2, because no
# released version has ever written or read
# conversion_authorize_initial_intent / conversion_authorize_initial_committed.
# A bundle whose history is currently parked between one of these intents and
# its matching committed event (a crash mid-write) can only be recovered by
# the version of this module that knows this event pair -- an older reducer
# would not recognize the event names and would fail closed rather than
# silently misreading them. Once this change ships, adding a third event to
# this vocabulary must bump SCHEMA_VERSION instead of extending these tables
# in place.
# The intent key set each kind carries: the shared shell plus exactly that
# kind's evidence, minus `authorized_at` (which the intent already carries as
# `at`) and `accepted_risk` (a constant of the retry shape, not evidence).
AUTHORIZE_INTENT_KEYS_BY_KIND = {
    "retry": RETRY_INTENT_KEYS,
    "initial": _AUTHORIZE_INTENT_BASE_KEYS | frozenset({"evidence_hash"}),
}
# Minor fix (2.3c round 1): run this check inside a function rather than as a
# bare module-level `for` loop. A bare loop leaves its loop variables as
# module attributes once it finishes, which the original version cleaned up
# with a trailing `del _kind_table_name, _kind_table` -- a statement that
# only holds together because the tuple it iterates is a fixed 3-element
# literal above, and would raise NameError on either name if that literal
# were ever emptied. A function has no such dependency: its locals are gone
# the moment it returns, with nothing to `del` and nothing that can outlive
# an empty iterable.
def _check_authorize_kind_tables_are_complete() -> None:
    for kind_table_name, kind_table in (
        ("AUTHORIZE_INTENT_EVENT_BY_KIND", AUTHORIZE_INTENT_EVENT_BY_KIND),
        ("AUTHORIZE_COMMITTED_EVENT_BY_KIND", AUTHORIZE_COMMITTED_EVENT_BY_KIND),
        ("AUTHORIZE_INTENT_KEYS_BY_KIND", AUTHORIZE_INTENT_KEYS_BY_KIND),
    ):
        if set(kind_table) != AUTHORIZED_STATE_KINDS:
            raise ValueError(
                f"{kind_table_name} must name exactly the authorization kinds "
                f"{sorted(AUTHORIZED_STATE_KINDS)!r}"
            )


_check_authorize_kind_tables_are_complete()

_AUTHORIZE_COMMITTED_EVENT_BY_INTENT_EVENT = {
    AUTHORIZE_INTENT_EVENT_BY_KIND[kind]: AUTHORIZE_COMMITTED_EVENT_BY_KIND[kind]
    for kind in AUTHORIZED_STATE_KINDS
}

# Every event name a conversion authorization/submission/poll operation can
# open or close a durable transaction with. raw_conversion.py's reducer reads
# the intent half of this to decide which events to hand back to
# apply_committed_operations; owning it here means a new conversion event pair
# cannot be added without that reducer learning about it.
CONVERSION_INTENTS = frozenset(
    {
        "conversion_submit_intent",
        "conversion_submit_result_intent",
        "conversion_poll_result_intent",
    }
) | frozenset(AUTHORIZE_INTENT_EVENT_BY_KIND.values())


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


# --- Wiring the verdict into a stop-before-intent behavior (plan.md 2.3) ---

CREATE_OPERATION = "create"
ORDINARY_POLL_OPERATION = "ordinary_poll"
RESULT_REFRESH_OPERATION = "result_refresh"
POLL_OPERATIONS = frozenset({ORDINARY_POLL_OPERATION, RESULT_REFRESH_OPERATION})
# Task 2.3c / design Decision 9.4. The recorded-credential gate is the one
# admitted operation that never talks to the network: it is admitted anyway
# because admission is about local state, not about the response -- this
# change turns a zero-write path into a writing one, and a write path without
# admission is exactly what Decision 9.4 forbids.
AUTHORIZE_INITIAL_OPERATION = "authorize_initial"

# Stand-ins for the two bounded strings an operation has not received yet when
# admission runs. Each is the *shortest* legal value of its kind, so the
# candidate it builds carries only the field's structural cost; the value's
# worst case is added on top by worst_case_admission_for_unknown_response's
# *_unreceived_*_count arguments, which assume a 2-byte `""` placeholder.
#
# The task ID cannot use `""`: doc2x.TASK_ID_PATTERN, and through it
# _valid_attempt, rejects an empty task ID, so a `submitted` branch built with
# `""` would be discarded as unproducible instead of measured. "0" is the
# shortest task ID the pattern accepts and costs one byte more than the `""`
# the count arithmetic assumes -- a one-byte *over*-count, which can only fail
# closed earlier, never later.
_UNRECEIVED_TASK_ID_PLACEHOLDER = "0"
_UNRECEIVED_RESULT_URL_PLACEHOLDER = ""

# _valid_http_status admits 100..599, so every non-null HTTP status renders as
# exactly three JSON bytes; `null` renders as four. Both are measured because
# the wider one is not the one an intuition would pick.
_WORST_CASE_HTTP_STATUSES = (None, 599)


def _worst_case_timestamp(at: str) -> str:
    """The longest legal timestamp an operation running at `at` could write.

    workflow._isoformat drops the microsecond field when it is zero, so two
    timestamps taken from the same operation can differ in length by the seven
    bytes of ".999999". Admission measures candidates with a single timestamp
    value, so it uses the longest form that instant can take; the real write
    can then only be shorter.
    """
    moment = _parse_timestamp(at)
    if moment.microsecond:
        return at
    padded = moment.replace(microsecond=999999).isoformat()
    return padded[:-6] + "Z" if padded.endswith("+00:00") else padded


def _create_response_branches() -> list[doc2x.CreateResult]:
    """Every classification a create POST could still come back as."""
    branches = [
        doc2x.CreateResult(
            "submitted", 200, None, _UNRECEIVED_TASK_ID_PLACEHOLDER
        )
    ]
    for reason_code in sorted(SUBMISSION_UNKNOWN_REASON_CODES):
        for http_status in _WORST_CASE_HTTP_STATUSES:
            branches.append(
                doc2x.CreateResult(
                    "submission_unknown", http_status, reason_code, None
                )
            )
    return branches


def _poll_response_branches() -> list[doc2x.PollResult]:
    """Every observation a poll GET could still come back as.

    Built from POLL_RESULT_STATES and LEGAL_TRIPLES -- the same table
    _poll_transition and _valid_attempt (via POLL_STATE_CONTRACT, itself
    derived from LEGAL_TRIPLES) judge against -- so admission cannot budget
    for a shape the writer would refuse, or miss one it would accept.
    """
    branches = []
    for state in sorted(POLL_RESULT_STATES - {"poll_transient"}):
        row = _LEGAL_TRIPLE_BY_FLAT_STATE[state]
        branches.append(
            doc2x.PollResult(
                state,
                row.http_status,
                row.reason_code,
                row.upstream_status,
                None,
                _UNRECEIVED_RESULT_URL_PLACEHOLDER
                if state == "result_ready"
                else None,
            )
        )
    for reason_code in sorted(POLL_TRANSIENT_REASON_CODES):
        for http_status in _WORST_CASE_HTTP_STATUSES:
            branches.append(
                doc2x.PollResult(
                    "poll_transient", http_status, reason_code, None, None
                )
            )
    return branches


def _create_capacity_candidates(
    *, manifest: dict, private_state: dict, history_bytes: int,
    credential: dict, request: dict, request_summary: dict, at: str
) -> dict:
    """Largest candidate each file reaches across a create's legal branches.

    A create writes four history events -- conversion_submit_intent and
    conversion_submit_started around the submitting write, then
    conversion_submit_result_intent and conversion_submit_result_committed
    around the result write -- and rewrites manifest.json and private.json
    twice. The intent/started pair is identical on every branch, so it is
    counted once; the result pair and the two finished documents are maximized
    over every classification the POST could return.

    The maximum is taken per file. Judging each file against its own ceiling is
    what worst_case_admission_for_unknown_response already does, so a branch
    that is largest for manifest.json need not be the one that is largest for
    history.ndjson.
    """
    submitting = _submit_state(
        manifest=manifest,
        private_state=private_state,
        credential=credential,
        request=request,
        request_summary=request_summary,
        at=at,
    )
    submit_intent, submit_started = _submit_events(submitting)
    prefix_bytes = canonical_state_byte_length(
        submit_intent
    ) + canonical_state_byte_length(submit_started)
    manifest_bytes = max(
        canonical_state_byte_length(manifest),
        canonical_state_byte_length(submitting["updated_manifest"]),
    )
    private_bytes = max(
        canonical_state_byte_length(private_state),
        canonical_state_byte_length(submitting["updated_private"]),
    )
    tail_bytes = 0
    for result in _create_response_branches():
        try:
            finished = _submission_result_state(
                manifest=submitting["updated_manifest"],
                private_state=submitting["updated_private"],
                result=result,
                at=at,
            )
        except ConversionAttemptError:
            # A branch this module refuses to build is a branch it can never
            # write, so it can never consume capacity either.
            continue
        result_intent, result_committed = _submission_result_events(finished)
        manifest_bytes = max(
            manifest_bytes, canonical_state_byte_length(finished["updated_manifest"])
        )
        private_bytes = max(
            private_bytes, canonical_state_byte_length(finished["updated_private"])
        )
        tail_bytes = max(
            tail_bytes,
            canonical_state_byte_length(result_intent)
            + canonical_state_byte_length(result_committed),
        )
    return {
        "manifest_candidate_bytes": manifest_bytes,
        "private_candidate_bytes": private_bytes,
        "history_candidate_bytes": history_bytes + prefix_bytes + tail_bytes,
        # The finished manifest and conversion_submit_result_intent each hold
        # exactly one task ID the POST has not answered with yet. No other
        # candidate field of this operation is unreceived: the submitting
        # attempt's task_id is the literal None begin_attempt writes, a known
        # value rather than a placeholder.
        "manifest_unreceived_task_id_count": 1,
        "history_unreceived_task_id_count": 1,
    }


def _recovered_transient_continuation(
    *, manifest: dict, private_state: dict, at: str
) -> tuple[dict, dict, dict] | None:
    """The state recover_interrupted_attempt settles on when the URL is lost.

    Mirrors the synthetic observation that recovery classifies when it finds a
    durable conversion_poll_result_intent for a result_ready decision whose
    private payload never reached the disk. Returns None when this bundle
    cannot reach that branch at all, which also means it can never write it.
    """
    try:
        return _poll_transition(
            manifest=manifest,
            private_state=private_state,
            result=doc2x.PollResult(
                "poll_transient", None, "result_private_payload_lost", None, None
            ),
            at=at,
        )
    except ConversionAttemptError:
        return None


def _recovered_transient_event(
    committed: dict, continuation: tuple[dict, dict, dict]
) -> dict:
    """The committed event recovery appends instead of `committed`.

    Same operation_id and generations -- recovery replays them off the durable
    intent -- but a different event name, the hashes of the downgraded
    documents, and the downgraded attempt embedded whole. That embedded attempt
    is the entire reason this branch is larger than the direct one.
    """
    recovered_manifest, recovered_private, recovered_attempt = continuation
    return {
        **committed,
        "event": "conversion_poll_result_recovered_transient",
        "manifest_hash": object_hash(recovered_manifest),
        "private_hash": object_hash(recovered_private),
        "attempt": recovered_attempt,
    }


def _poll_capacity_candidates(
    *, manifest: dict, private_state: dict, history_bytes: int, at: str
) -> dict:
    """Largest candidate each file reaches across a poll's legal branches.

    Shared by the ordinary poll and the result refresh. They classify the same
    observations through the same _poll_transition, and the refresh does not
    end up with the smaller processing-window shape: whatever makes a refresh
    bigger -- the result URL already recorded in private_state, the poll window
    and result reference already on the active attempt -- is inside the
    manifest and private_state it is handed, and is therefore measured exactly.

    The tail also covers the continuation a crash would leave for the *next*
    resume to finish. recover_interrupted_attempt runs at the top of the
    caller's advance, ahead of every admission call site and returning as soon
    as it succeeds, so nothing else ever budgets for it; and when the crash
    landed between conversion_poll_result_intent and the private write of a
    result_ready decision, the event it appends
    (conversion_poll_result_recovered_transient) is strictly larger than the
    conversion_poll_result_committed it stands in for, because it embeds the
    downgraded attempt. Admitting on the direct branch alone would let a poll
    start that its own recovery cannot finish, and that recovery has no way to
    give up: it would raise on append_history at every later resume.
    """
    manifest_bytes = canonical_state_byte_length(manifest)
    private_bytes = canonical_state_byte_length(private_state)
    tail_bytes = 0
    recovery_continuation = _recovered_transient_continuation(
        manifest=manifest, private_state=private_state, at=at
    )
    for result in _poll_response_branches():
        try:
            updated_manifest, updated_private, updated_attempt = _poll_transition(
                manifest=manifest, private_state=private_state, result=result, at=at
            )
        except ConversionAttemptError:
            continue
        intent, committed = _poll_result_events(
            manifest=manifest,
            private_state=private_state,
            updated_manifest=updated_manifest,
            updated_private=updated_private,
            updated_attempt=updated_attempt,
            at=at,
        )
        manifest_bytes = max(
            manifest_bytes, canonical_state_byte_length(updated_manifest)
        )
        private_bytes = max(
            private_bytes, canonical_state_byte_length(updated_private)
        )
        tail_bytes = max(
            tail_bytes,
            canonical_state_byte_length(intent)
            + canonical_state_byte_length(committed),
        )
        if (
            recovery_continuation is not None
            and updated_attempt.get("state") == "result_ready"
        ):
            tail_bytes = max(
                tail_bytes,
                canonical_state_byte_length(intent)
                + canonical_state_byte_length(
                    _recovered_transient_event(committed, recovery_continuation)
                ),
            )
    return {
        "manifest_candidate_bytes": manifest_bytes,
        "private_candidate_bytes": private_bytes,
        # The recovery's own manifest.json and private.json need no separate
        # term: it writes the same documents the poll_transient branches above
        # already measured, and those branches carry an HTTP status the
        # recovery's synthetic observation leaves None.
        "history_candidate_bytes": history_bytes + tail_bytes,
        # private.json gains one result_urls record holding the full URL the
        # GET has not answered with yet. The task ID is already known and
        # measured exactly, and the raw URL never reaches history.ndjson or
        # manifest.json -- both carry only its fixed-width sha256 digest.
        "private_unreceived_result_url_count": 1,
    }


def _authorize_initial_capacity_candidates(
    *, manifest: dict, private_state: dict, history_bytes: int,
    config_error_code: str, at: str
) -> dict:
    """Exactly what the recorded-credential gate would write.

    The only admitted operation with no unreceived value to bound: the gate
    never sends a request, so there is no task ID and no result URL still to
    come, and every byte of the two documents and the two events is already
    known here. That is why this returns no `*_unreceived_*_count` -- not an
    omission, an absence.
    """
    state = _initial_authorization_state(
        manifest=manifest,
        private_state=private_state,
        config_error_code=config_error_code,
        at=at,
    )
    intent, committed = _initial_authorization_events(state)
    return {
        "manifest_candidate_bytes": max(
            canonical_state_byte_length(manifest),
            canonical_state_byte_length(state["updated_manifest"]),
        ),
        "private_candidate_bytes": max(
            canonical_state_byte_length(private_state),
            canonical_state_byte_length(state["updated_private"]),
        ),
        "history_candidate_bytes": (
            history_bytes
            + canonical_state_byte_length(intent)
            + canonical_state_byte_length(committed)
        ),
    }


# Task 2.4/Decision 9.4: the local-state capacity admission operation shared
# by commit_retry_decision's direct write and recover_interrupted_attempt's
# retry-intent crash-recovery replay. Both write the same shape (one
# placeholder attempt, one intent/committed event pair) -- see
# _retry_decision_capacity_candidates.
RETRY_DECISION_OPERATION = "retry_decision"


def _retry_decision_capacity_candidates(
    *,
    manifest: dict,
    private_state: dict,
    history_bytes: int,
    updated_manifest: dict,
    updated_private: dict,
    history_tail_bytes: int,
) -> dict:
    """Bytes a retry-decision write would add to each file.

    Unlike create/poll admission, a retry decision is fully deterministic
    given its inputs -- there is no unvalidated wire response to budget a
    worst case for -- so this takes the already-built candidate documents
    directly rather than rebuilding them from raw inputs the way
    _authorize_initial_capacity_candidates does. `history_tail_bytes` is the
    caller's own not-yet-durable-events sum: intent+committed for a fresh
    commit_retry_decision call, or just the committed event for
    recover_interrupted_attempt finishing an already-durable intent (whose
    bytes `history_bytes` already counts) -- only the caller knows which half
    of that pair is still ahead of it.
    """
    return {
        "manifest_candidate_bytes": max(
            canonical_state_byte_length(manifest),
            canonical_state_byte_length(updated_manifest),
        ),
        "private_candidate_bytes": max(
            canonical_state_byte_length(private_state),
            canonical_state_byte_length(updated_private),
        ),
        "history_candidate_bytes": history_bytes + history_tail_bytes,
    }


def assert_local_state_capacity(
    *, operation: str, manifest: dict, private_state: dict, history_bytes: int,
    at: str, credential: dict | None = None, request: dict | None = None,
    request_summary: dict | None = None, config_error_code: str | None = None,
    updated_manifest: dict | None = None, updated_private: dict | None = None,
    history_tail_bytes: int | None = None,
) -> None:
    """Fail closed before the first intent when local state cannot hold the
    operation's worst-case result.

    design.md:305 / spec.md "本地状态容量在外部调用前耗尽": callers must invoke
    this before appending their first history event and before their external
    call, so a refusal leaves manifest.json, private.json and history.ndjson at
    their exact previous bytes with no temporary file and no network access.
    Raising is the only signal; nothing here writes.
    """
    at = _worst_case_timestamp(at)
    if operation == CREATE_OPERATION:
        candidates = _create_capacity_candidates(
            manifest=manifest,
            private_state=private_state,
            history_bytes=history_bytes,
            credential=credential,
            request=request,
            request_summary=request_summary,
            at=at,
        )
    elif operation in POLL_OPERATIONS:
        candidates = _poll_capacity_candidates(
            manifest=manifest,
            private_state=private_state,
            history_bytes=history_bytes,
            at=at,
        )
    elif operation == AUTHORIZE_INITIAL_OPERATION:
        candidates = _authorize_initial_capacity_candidates(
            manifest=manifest,
            private_state=private_state,
            history_bytes=history_bytes,
            config_error_code=config_error_code,
            at=at,
        )
    elif operation == RETRY_DECISION_OPERATION:
        candidates = _retry_decision_capacity_candidates(
            manifest=manifest,
            private_state=private_state,
            history_bytes=history_bytes,
            updated_manifest=updated_manifest,
            updated_private=updated_private,
            history_tail_bytes=history_tail_bytes,
        )
    else:
        raise ConversionAttemptError(
            "integrity_violation",
            "The conversion operation has no capacity admission.",
        )
    verdict = worst_case_admission_for_unknown_response(**candidates)
    if not all(verdict.values()):
        raise ConversionAttemptError(
            "local_state_capacity_exhausted",
            "The work bundle cannot hold this conversion operation's "
            "worst-case local state.",
        )


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
    # Re-keyed by the fold: the flat pair {"task_unavailable",
    # "poll_transient"} both fold onto `failed`, so the reason carries the
    # whole discrimination (_BACKOFF_PAIRS).
    if not isinstance(attempt, dict) or (
        attempt.get("state"),
        attempt.get("reason"),
    ) not in _BACKOFF_PAIRS:
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


def result_refresh_rounds_exhausted(attempt: dict) -> bool:
    """Whether `attempt`'s cumulative result-refresh round count has reached
    RESULT_REFRESH_ROUND_CEILING.

    Not wired into any state transition as of task 2.1d -- which finite
    ceiling to configure, and what an exhausted attempt should do, are
    Decision 10, deferred to a later change. RESULT_REFRESH_ROUND_CEILING
    defaults to None, in which case this branch is fully short-circuited:
    the function returns False unconditionally, without even reading
    `attempt`, so today's observable behaviour cannot depend on it.
    """
    if RESULT_REFRESH_ROUND_CEILING is None:
        return False
    return attempt.get("result_refresh_round_count", 0) >= RESULT_REFRESH_ROUND_CEILING


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


def _authorization_kind_of(value) -> str | None:
    """The kind an authorization object declares by its own key set, or None
    for anything that is not exactly one of the two shapes.

    Fail-closed by construction: a shape that is neither key set -- a "retry"
    missing a field, a superset carrying an unknown one, the union of the two
    -- is not folded onto the nearest kind, it is refused. Callers rely on
    this to be the only place a kind is derived, so that the validator and
    valid_private_state's two invariants can never disagree about which kind
    a stored authorization is.
    """
    if not isinstance(value, dict):
        return None
    keys = set(value)
    for kind, expected in AUTHORIZATION_KEYS_BY_KIND.items():
        if keys == expected:
            return kind
    return None


def _valid_authorization(value) -> bool:
    kind = _authorization_kind_of(value)
    if kind is None:
        return False
    # The two fields both kinds carry -- and the whole of "initial".
    if not (
        _valid_hash(value.get("evidence_hash"))
        and _valid_timestamp(value.get("authorized_at"))
    ):
        return False
    if kind == "initial":
        return True
    # kind == "retry": the only other member of AUTHORIZATION_KEYS_BY_KIND,
    # and _authorization_kind_of returns nothing outside that table. The
    # exhaustiveness of this two-way split is pinned by the test that asserts
    # the table names exactly {"initial", "retry"} -- adding a third kind
    # without extending this function fails there rather than silently
    # validating the newcomer against retry's rules.
    return (
        isinstance(value.get("action_id"), str)
        and ACTION_ID_PATTERN.fullmatch(value["action_id"]) is not None
        and _valid_hash(value.get("basis_sha256"))
        and value.get("accepted_risk") == "possible_duplicate_conversion_charge"
    )


def frozen_source_evidence_hash(manifest: dict) -> str:
    """The evidence an `initial` authorization is authorized against.

    design.md Decision 2: an initial authorization accepts no
    duplicate-charge risk and answers no pending action, so the only thing it
    can be bound to is the frozen evidence that already exists when the
    recorded-credential gate blocks -- the manifest's `source` record (the
    frozen input and its digest) and its `preflight` record (the page
    baseline, dependency inventory and decision). Both are settled before the
    bundle can reach `ready_to_submit` and neither is rewritten between the
    gate and the create that consumes the placeholder, so the hash a gate
    record stores stays computable from the manifest for the placeholder's
    whole life.
    """
    return object_hash(
        {"source": manifest.get("source"), "preflight": manifest.get("preflight")}
    )


def _valid_authorized_evidence(kind, authorization, manifest: dict) -> bool:
    """Whether an authorization's evidence is bound to something real.

    Task 2.3c's root fix for the residue task 2.3b left behind: "initial"'s
    key set is a *proper subset* of "retry"'s, so a retry authorization
    stripped of action_id / basis_sha256 / accepted_risk has a key set
    identical to a genuine initial one. `_authorization_kind_of` reads it as
    "initial" and every shape check passes -- a damaged retry is accepted on
    attempt #1 while an intact one is refused. Shape cannot separate them;
    content can, which is what `evidence_hash` is in the initial key set for.

    Exhaustive over AUTHORIZATION_KEYS_BY_KIND and fail-closed outside it: a
    third kind added without a rule here is refused, not waved through.
    """
    if kind == "initial":
        return authorization.get("evidence_hash") == frozen_source_evidence_hash(
            manifest
        )
    if kind == "retry":
        # A retry's evidence_hash is bound by valid_private_state's
        # predecessor check instead: it must equal, field for field, the
        # pending action of the attempt before it. That check has nothing to
        # compare against on attempt #1, which is exactly why a retry
        # authorization is illegal there.
        return True
    return False


def _valid_pending_action(value, *, attempt: dict, generation: int) -> bool:
    if value is None:
        return True
    evidence_attempt = deepcopy(attempt)
    evidence_attempt["pending_action"] = None
    return (
        isinstance(value, dict)
        and set(value) == PENDING_ACTION_KEYS
        # Re-keyed by the fold: CONFIRMABLE_PENDING_KINDS replaces the local
        # `expected_kinds` dict that used to be keyed by flat state. `failed`
        # alone would now name ten reasons, only two of which -- `task_failed`
        # and `unexpected_result_count` -- take a pending action at all (task
        # 2.4: both now take AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND).
        and value.get("kind")
        == CONFIRMABLE_PENDING_KINDS.get(
            (attempt.get("state"), attempt.get("reason"))
        )
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
    reason = attempt.get("reason")
    upstream_status = attempt.get("upstream_status")
    authorization = attempt.get("authorization")
    if authorization is not None and not _valid_authorization(authorization):
        return False
    # Schema v2's four reason columns are checked once here, for every state,
    # rather than state by state: the legal (state, reason) combinations are
    # a closed set (FLAT_STATE_MIGRATION owns it) and the other three columns
    # are the same shape everywhere, so a per-branch check would be as many
    # chances to disagree with the writer as there are branches.
    #
    # Task 2.1c re-keys this gate from `reason == FLAT_STATE_MIGRATION[state]
    # [1]` to membership of LEGAL_STATE_REASON_PAIRS. The admitted set is
    # identical -- both accept exactly the pairs FLAT_STATE_MIGRATION lists --
    # it is just no longer indexed by a flat name the record has stopped
    # carrying. Rejecting an illegal pair up front is what makes the branches
    # below total.
    if (state, reason) not in LEGAL_STATE_REASON_PAIRS:
        return False
    # Task 2.3a gave authorization_kind its first real value, "retry"; task
    # 2.3c adds the second, "initial", written only by the recorded-credential
    # gate (authorize_initial_attempt). Both live only on "authorized"
    # records. Every other state's attempt is still built through a write path
    # that leaves the field at its _attempt_reason_columns default of None (see
    # that function's docstring), so the two-way split below -- "authorized"
    # requires a member of AUTHORIZED_STATE_KINDS, every other state still
    # requires None -- is exhaustive over ATTEMPT_STATES and fails closed on
    # anything else.
    #
    # The domain is read off AUTHORIZATION_KEYS_BY_KIND rather than spelled
    # out, and is immediately narrowed twice more, so widening it is not a
    # blanket admission: the reason column must belong to that kind's domain
    # (AUTHORIZED_STATE_REASONS_BY_KIND), and the authorization's own shape
    # must equal the kind (the cross-check in the authorized branch below,
    # which task 2.3b put there for exactly this widening -- it makes the
    # shape follow the kind automatically, with no second place to update).
    #
    # Version stance (same shape as task 2.2c's): this is a deliberate hard
    # break *inside an unreleased change*, not a compatible widening. The
    # kind=None authorized placeholder that tasks 2.1c-2.2 wrote is rejected
    # by the split below -- there is no migrator and no dual-write window,
    # and SCHEMA_VERSION stays 2 precisely because no released version ever
    # stored one. The blast radius is bounded to two things, both of which
    # only exist mid-flight: a bundle currently parked in the "authorized"
    # state, and a retry intent that has not been replayed yet. Authorized
    # records do not accumulate in history (_submit_state replaces the
    # placeholder in place rather than appending past it), so nothing older
    # can be holding one. Once this change ships, any further edit to this
    # column's value domain -- task 2.3c's "initial" included, if it lands
    # after release -- must bump SCHEMA_VERSION instead.
    #
    # Task 2.3b opened two other dimensions ahead of this one: index (attempt
    # #1 only, where valid_private_state's predecessor check has nothing to
    # match against) and the authorization's own shape (discriminated by key
    # set, see AUTHORIZATION_KEYS_BY_KIND). Task 2.3c widens this column to
    # match, and the seam 2.3b left below absorbs the widening.
    authorization_kind = attempt.get("authorization_kind")
    valid_authorization_kind = (
        (
            authorization_kind in AUTHORIZED_STATE_KINDS
            # An authorized record's kind and its reason column are not free
            # of each other: only the credential gate writes "initial", and
            # it always writes a credential reason, while the retry
            # placeholder records no observation at all. Without this the
            # widened domain would also admit an "initial" record carrying
            # reason None -- a gate record that records no gate.
            and reason
            in AUTHORIZED_STATE_REASONS_BY_KIND[authorization_kind]
        )
        if state == "authorized"
        else authorization_kind is None
    )
    if (
        not _valid_reason_detail(reason, attempt.get("reason_detail"))
        or not valid_authorization_kind
        # task 2.1d: result_refresh_round_count is no longer pinned to
        # exactly 0 -- it is a real, cumulative counter now. Its only
        # snapshot-checkable self-consistency here is int/non-negative; the
        # tighter `<= poll_count` bound below (same shape as
        # consecutive_transient_count's) and the "authorized" state's
        # explicit `== 0` check are what actually constrain it once polling
        # is in play. True non-regression (it never decreases across polls)
        # is guaranteed by _poll_transition's construction -- carry-forward
        # or increment, never reset -- not by this single-snapshot function,
        # which has no access to the attempt's prior value.
        or type(attempt.get("result_refresh_round_count")) is not int
        or attempt["result_refresh_round_count"] < 0
    ):
        return False
    if state == "authorized":
        return (
            # Cross-check kind against the authorization's own declared
            # shape: valid_authorization_kind above only pins the *column*,
            # and _valid_authorization only checks the *authorization* is one
            # of the two legal shapes on its own merits -- neither ties the
            # two together, so a record could otherwise claim kind "retry"
            # while carrying a legal "initial" authorization (or vice versa).
            # This is the seam task 2.3b left for 2.3c's widening of
            # authorization_kind's authorized-state domain: the equality makes
            # the shape follow the kind automatically, with no second place to
            # update.
            _authorization_kind_of(authorization) == authorization_kind
            and _valid_authorization(authorization)
            # ...and shape agreeing with kind is still not enough on its own,
            # because "initial"'s key set is a proper subset of "retry"'s (see
            # _valid_authorized_evidence). The evidence has to be bound to
            # something the manifest can recompute.
            and _valid_authorized_evidence(
                authorization_kind, authorization, manifest
            )
            and attempt.get("api_base") is None
            and attempt.get("poll_count") == 0
            and attempt.get("consecutive_transient_count") == 0
            # A freshly authorized/not-yet-submitted attempt has never
            # polled, so no distinct result URL can have been recorded yet. The
            # general `<= poll_count` bound below never runs for this state
            # (it returns here first), so this has to be checked explicitly,
            # the same way poll_count/consecutive_transient_count are on the
            # two lines above.
            and attempt.get("result_refresh_round_count") == 0
            # The four columns below are excluded from the all-None sweep not
            # because they can be anything, but because each is already
            # pinned tighter than "is None" would be:
            #   * authorization_kind -- to AUTHORIZED_STATE_KINDS, by
            #     valid_authorization_kind above;
            #   * authorization -- to its kind's exact shape and bound
            #     evidence, by the three checks above;
            #   * reason (task 2.3c) -- to AUTHORIZED_STATE_REASONS_BY_KIND's
            #     domain for this record's kind, which is {None} for "retry"
            #     and the two credential-gate reasons for "initial", and is
            #     additionally inside LEGAL_STATE_REASON_PAIRS;
            #   * reason_detail (task 2.3c) -- to None for every authorized
            #     record, by _valid_reason_detail: neither credential reason
            #     is a REASON_DETAILS key, and neither is None.
            # Leaving reason/reason_detail in the sweep would refuse every
            # gate record outright, since the sweep demands None.
            and all(
                attempt.get(key) is None
                for key in ATTEMPT_KEYS
                - {
                    "schema_version",
                    "attempt_id",
                    "state",
                    "reason",
                    "reason_detail",
                    "authorization",
                    "authorization_kind",
                    "poll_count",
                    "consecutive_transient_count",
                    "result_refresh_round_count",
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
        # Same shape as the consecutive_transient_count bound directly
        # above: a round can only have been observed by way of an actual
        # poll, so the cumulative count can never exceed how many polls this
        # attempt has made.
        or attempt["result_refresh_round_count"] > attempt["poll_count"]
    ):
        return False
    if state == "submitting":
        return (
            attempt.get("response_at") is None
            and attempt.get("http_status") is None
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
        # The wire branch is enforced by the reason_detail gate above, which
        # requires it to be one of SUBMISSION_UNKNOWN_REASON_CODES.
        return (
            attempt.get("task_id") is None
            and attempt.get("poll_count") == 0
            and _empty_poll_and_result_fields(attempt)
        )
    task_id = attempt.get("task_id")
    if (
        not isinstance(task_id, str)
        or doc2x.TASK_ID_PATTERN.fullmatch(task_id) is None
    ):
        return False
    if (state, reason) == _POLL_TRANSIENT_PAIR:
        # The reason_detail gate above already required one of
        # POLL_TRANSIENT_REASON_CODES; only the upstream status is left.
        if upstream_status is not None:
            return False
    elif (
        state,
        reason,
        attempt.get("http_status"),
        upstream_status,
        # Re-keyed by the fold from `POLL_STATE_CONTRACT[state][:2]` to
        # membership of _LEGAL_POLL_OBSERVATIONS; see that constant for why
        # the two are equally tight.
    ) not in _LEGAL_POLL_OBSERVATIONS:
        return False
    if (state, reason) in _BACKOFF_PAIRS:
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
    elif (state, reason) not in _CREDENTIAL_ERROR_PAIRS:
        if (
            attempt.get("consecutive_transient_count") != 0
            or attempt.get("next_poll_at") is not None
        ):
            return False
    if state == "submitted":
        return attempt.get("poll_count") == 0 and _empty_poll_and_result_fields(
            attempt
        )
    if (state, reason) not in _CREDENTIAL_ERROR_PAIRS and (
        attempt.get("poll_count", 0) <= 0
    ):
        return False
    if not _valid_poll_fields(attempt):
        return False
    # Pre-fold: `state in {"result_pending", "result_pending_timeout"}`.
    # result_pending is the one row the fold collapsed onto a pair shared with
    # two siblings that do NOT carry this window, so it is recovered from
    # upstream_status -- already pinned to "completed" for that row by the
    # observation gate above.
    if (
        _is_result_pending(state, reason, upstream_status)
        or (state, reason) == _RESULT_PENDING_TIMEOUT_PAIR
    ) and (
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


def _submit_state(
    *, manifest: dict, private_state: dict, credential: dict,
    request: dict, request_summary: dict, at: str
) -> dict:
    """The in-memory submitting state a create would durably commit.

    Split out of begin_attempt so the local-state capacity admission can size
    exactly the documents and events begin_attempt is about to write, without
    writing anything. It performs no I/O and mutates nothing.
    """
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
    # The retry placeholder commit_retry_decision leaves behind. Task 2.1c
    # folds its stored state from "not_started" to "authorized".
    placeholder = (
        attempts[-1]
        if attempts and attempts[-1].get("state") == "authorized"
        else None
    )
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
        **_attempt_state_columns("submitting"),
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
    return {
        "manifest": manifest,
        "private_state": private_state,
        "updated_manifest": updated_manifest,
        "updated_private": updated_private,
        "attempt": attempt,
        "placeholder": placeholder,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
    }


def _submit_events(state: dict) -> tuple[dict, dict]:
    """The intent and started events a create appends around its two writes."""
    operation_id = f"{state['attempt']['attempt_id']}-submit"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_submit_intent",
        "operation_id": operation_id,
        "expected_generation": state["expected_generation"],
        "new_generation": state["new_generation"],
        "at": state["at"],
        "attempt": state["attempt"],
        "previous_attempt": deepcopy(state["placeholder"]),
        "previous_manifest_hash": object_hash(state["manifest"]),
        "previous_private_hash": object_hash(state["private_state"]),
    }
    started = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_submit_started",
        "operation_id": operation_id,
        "previous_generation": state["expected_generation"],
        "generation": state["new_generation"],
        "at": state["at"],
        "manifest_hash": object_hash(state["updated_manifest"]),
        "private_hash": object_hash(state["updated_private"]),
    }
    return intent, started


def begin_attempt(
    *, descriptors: dict, manifest: dict, private_state: dict, credential: dict,
    request: dict, request_summary: dict, at: str
) -> tuple[dict, dict, dict]:
    state = _submit_state(
        manifest=manifest,
        private_state=private_state,
        credential=credential,
        request=request,
        request_summary=request_summary,
        at=at,
    )
    intent, started = _submit_events(state)
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", state["updated_private"], dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", state["updated_manifest"], dir_fd=descriptors["root"]
    )
    bundle.append_history(started, state_fd=descriptors["state"])
    return state["updated_manifest"], state["updated_private"], state["attempt"]


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
            or previous_attempt.get("state") != "authorized"
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


def _submission_result_state(
    *, manifest: dict, private_state: dict, result: doc2x.CreateResult, at: str
) -> dict:
    """The in-memory state a classified create response would commit.

    Split out of finish_submission for the same reason as _submit_state: the
    capacity admission must size this operation's largest legal end state
    before the create POST happens. It performs no I/O.
    """
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
            # result.state is doc2x's wire classification; the record stores
            # the folded state next to the reason it folded onto.
            "state": FLAT_STATE_MIGRATION[result.state][0],
            "response_at": at,
            "http_status": result.http_status,
            **_attempt_reason_columns(result.state, result.reason_code),
            "task_id": result.task_id,
        }
    )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    if result.state == "submission_unknown" and (
        manifest["settings_snapshot"]["interaction_mode"] == "confirm"
    ):
        completed["pending_action"] = {
            "kind": AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
            "action_id": f"conversion-decision-{secrets.token_hex(16)}",
            "generation": new_generation,
            "evidence_hash": object_hash(completed),
        }
    updated_manifest = deepcopy(manifest)
    updated_manifest["generation"] = new_generation
    updated_manifest["conversion_state"] = FLAT_STATE_MIGRATION[result.state][2]
    updated_manifest["conversion_attempts"] = [*deepcopy(attempts[:-1]), completed]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if not _valid_attempt(
        completed, manifest=updated_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation", "The conversion submission result is invalid."
        )
    return {
        "manifest": manifest,
        "private_state": private_state,
        "updated_manifest": updated_manifest,
        "updated_private": updated_private,
        "attempt": completed,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
    }


def _submission_result_events(state: dict) -> tuple[dict, dict]:
    """The intent and committed events a classified create response appends."""
    operation_id = f"{state['attempt']['attempt_id']}-submit-result"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_submit_result_intent",
        "operation_id": operation_id,
        "expected_generation": state["expected_generation"],
        "new_generation": state["new_generation"],
        "at": state["at"],
        "attempt": state["attempt"],
        "previous_manifest_hash": object_hash(state["manifest"]),
        "previous_private_hash": object_hash(state["private_state"]),
    }
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_submit_result_committed",
        "operation_id": operation_id,
        "previous_generation": state["expected_generation"],
        "generation": state["new_generation"],
        "at": state["at"],
        "manifest_hash": object_hash(state["updated_manifest"]),
        "private_hash": object_hash(state["updated_private"]),
    }
    return intent, committed


def finish_submission(
    *, descriptors: dict, manifest: dict, private_state: dict,
    result: doc2x.CreateResult, at: str
) -> tuple[dict, dict]:
    state = _submission_result_state(
        manifest=manifest, private_state=private_state, result=result, at=at
    )
    intent, committed = _submission_result_events(state)
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", state["updated_private"], dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", state["updated_manifest"], dir_fd=descriptors["root"]
    )
    bundle.append_history(committed, state_fd=descriptors["state"])
    return state["updated_manifest"], state["updated_private"]


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
                "response_at": None,
                "http_status": None,
                # Rebuilding the predecessor is an attempt construction site
                # too -- it must produce the same folded state and the same
                # four schema v2 columns _submit_state wrote, or the
                # recomputed previous_manifest_hash can never match the
                # durable intent.
                **_attempt_state_columns("submitting"),
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
    # completed["state"] is already folded and, for the two states this
    # branch admits, is its own conversion_state; read it off the table
    # anyway so the projection has one owner.
    desired_manifest["conversion_state"] = _MANIFEST_STATE_BY_FOLDED_STATE[
        (completed["state"], completed.get("reason"))
    ]
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
    # Both authorization kinds land here: their intents differ only in the
    # event name and the evidence fields, and _authorize_state_from_intent
    # replays either. The committed event name is read back from the same
    # table the writer used, keyed by the intent that is being finished.
    authorize_committed_event = next(
        (
            AUTHORIZE_COMMITTED_EVENT_BY_KIND[kind]
            for kind, intent_event in AUTHORIZE_INTENT_EVENT_BY_KIND.items()
            if isinstance(final, dict) and final.get("event") == intent_event
        ),
        None,
    )
    if authorize_committed_event is not None:
        intent_expected = final.get("expected_generation")
        intent_new = final.get("new_generation")
        _assert_recovery_generation(
            expected_generation,
            intent_expected,
            intent_new,
            message=(
                "Expected generation does not match the pending conversion "
                "authorization."
            ),
        )
        previous = resolve_history(
            history[:-1],
            manifest_template=manifest,
            private_template=private_state,
        )
        if previous is None:
            raise ConversionAttemptError(
                "integrity_violation",
                "A pending conversion authorization has no valid history prefix.",
            )
        previous_manifest, previous_private = previous
        desired_manifest, desired_private = _authorize_state_from_intent(
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
                "A pending conversion authorization is partially inconsistent.",
            )
        committed_event = {
            "schema_version": SCHEMA_VERSION,
            "event": authorize_committed_event,
            "operation_id": final["operation_id"],
            "previous_generation": intent_expected,
            "generation": intent_new,
            "at": at,
            "manifest_hash": object_hash(desired_manifest),
            "private_hash": object_hash(desired_private),
        }
        # Task 2.4/Decision 9.4: this branch finishes either authorization
        # kind (the initial credential gate or a retry decision), but only
        # the retry kind has a capacity admission to enforce here --
        # RETRY_DECISION_OPERATION's docstring explains why the initial kind
        # is out of this task's scope. `final["attempt"]["authorization_kind"]`
        # is the same discriminator _valid_attempt cross-checks the event name
        # against (only commit_retry_decision's placeholder ever sets it to
        # "retry"; the credential gate's placeholder leaves it at its default
        # None), so no extra bookkeeping is needed to tell the two intents
        # apart here. `history` already includes `final` (the durable intent)
        # -- its bytes are already spent -- so only the not-yet-written
        # committed event is sized on top, unlike commit_retry_decision's own
        # admission, which still has both intent and committed ahead of it.
        if (
            isinstance(final.get("attempt"), dict)
            and final["attempt"].get("authorization_kind") == "retry"
        ):
            assert_local_state_capacity(
                operation=RETRY_DECISION_OPERATION,
                manifest=manifest,
                private_state=private_state,
                history_bytes=sum(
                    canonical_state_byte_length(event) for event in history
                ),
                updated_manifest=desired_manifest,
                updated_private=desired_private,
                history_tail_bytes=canonical_state_byte_length(committed_event),
                at=at,
            )
        if private_is_previous:
            bundle.atomic_write_json(
                "private.json", desired_private, dir_fd=descriptors["state"]
            )
        if manifest_is_previous:
            bundle.atomic_write_json(
                "manifest.json", desired_manifest, dir_fd=descriptors["root"]
            )
        bundle.append_history(committed_event, state_fd=descriptors["state"])
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
    state = attempt.get("state")
    reason = attempt.get("reason")
    upstream_status = attempt.get("upstream_status")
    # Re-keyed by the fold. `result_pending` shares ("processing", None) with
    # `pending` and `processing`, which must NOT be judged against the
    # result-pending deadline, so upstream_status carries the split (design.md
    # Decision 1 note 3).
    if _is_result_pending(state, reason, upstream_status):
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
    # The pre-fold set was the flat {"pending", "processing", "poll_transient",
    # "task_unavailable", "unsafe_result_url"} -- note it excluded
    # `result_pending`, the third row that folds onto ("processing", None), so
    # the upstream_status filter below is load-bearing rather than cosmetic.
    if (state, reason) in _POLL_DEADLINE_PAIRS and not (
        (state, reason) == _PROCESSING_PAIR
        and upstream_status == _RESULT_PENDING_UPSTREAM_STATUS
    ):
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


# The projection the two poll commit paths apply. Its domain is
# POLL_RESULT_STATES -- what a poll observation can commit -- which is
# narrower than LEGAL_TRIPLES' full 18 rows: not_started, submitting and
# submission_unknown never reach this projection (see NON_POLL_OBSERVATIONS),
# so they are deliberately absent from both tables and any input outside them
# -- including those three -- falls to the "submitted" default, exactly as the
# pre-refactor if/elif chain did.
#
# There are two of them because the two call sites hold different vocabulary:
# _poll_transition has just been handed doc2x's wire classification, while
# _poll_state_from_intent reads a stored attempt whose state is already
# folded. Both are filtered on the same POLL_RESULT_STATES rows of
# LEGAL_TRIPLES, so they cannot disagree about a row; they only differ in what
# they are keyed by.
_POLL_RESULT_CONVERSION_STATE_BY_FLAT_STATE = {
    row.flat_state: row.conversion_state
    for row in LEGAL_TRIPLES
    if row.flat_state in POLL_RESULT_STATES
}
_POLL_RESULT_CONVERSION_STATE_BY_FOLDED_STATE = {
    (row.attempt_state, row.reason): row.conversion_state
    for row in LEGAL_TRIPLES
    if row.flat_state in POLL_RESULT_STATES
}


def _conversion_state_for_poll_result(flat_state: str) -> str:
    """Keyed by doc2x's wire classification."""
    return _POLL_RESULT_CONVERSION_STATE_BY_FLAT_STATE.get(flat_state, "submitted")


def _conversion_state_for_attempt(state, reason) -> str:
    """Keyed by a stored attempt's folded (state, reason) pair."""
    return _POLL_RESULT_CONVERSION_STATE_BY_FOLDED_STATE.get(
        (state, reason), "submitted"
    )


def _poll_transition(
    *, manifest: dict, private_state: dict, result: doc2x.PollResult, at: str
) -> tuple[dict, dict, dict]:
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        manifest.get("conversion_state")
        not in {"submitted", "recoverable_error", "terminal_error"}
        or not isinstance(active, dict)
        or (active.get("state"), active.get("reason"))
        not in POLL_ACTIVE_ATTEMPT_PAIRS
        or not isinstance(active.get("task_id"), str)
        or not active["task_id"]
        or private_state.get("generation") != manifest.get("generation")
        or result.state not in POLL_RESULT_STATES
    ):
        raise ConversionAttemptError(
            "invalid_state_transition", "The Doc2X poll result is not applicable."
        )
    active_pair = (active.get("state"), active.get("reason"))
    updated_attempt = deepcopy(active)
    # result.state is doc2x's wire classification; the stored state is the
    # folded value, written together with its reason columns below.
    updated_attempt["state"] = FLAT_STATE_MIGRATION[result.state][0]
    local_credential_error = result.state in {
        "credential_source_missing",
        "credential_source_changed",
    }
    local_timeout = result.state in {"poll_timeout", "result_pending_timeout"}
    if not local_credential_error and not local_timeout:
        # Pre-fold flat set {"poll_timeout", "result_pending_timeout",
        # "result_ready"}; the first two fold onto `failed` and are recovered
        # by their reason.
        reset_window = active_pair in _POLL_WINDOW_RESET_PAIRS
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
    updated_attempt.update(_attempt_reason_columns(result.state, result.reason_code))
    if result.state in {"task_unavailable", "poll_transient"}:
        consecutive_count = (
            active.get("consecutive_transient_count", 0) + 1
            if active_pair in _BACKOFF_PAIRS
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
        reset_result_window = active_pair == _RESULT_PENDING_TIMEOUT_PAIR
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
        if recorded_result is None:
            # private_state has never recorded this exact URL for this attempt.
            # The first result delivery counts as 1; every later distinct URL
            # adds one, so count k means k - 1 genuine refreshes. A repeated
            # current URL or reappearance of an older URL does not increment
            # this counter and therefore cannot bound a stale-URL loop. The
            # count is never reset, so every other branch leaves it exactly as
            # `updated_attempt = deepcopy(active)` above already carried it in.
            updated_attempt["result_refresh_round_count"] = (
                active.get("result_refresh_round_count", 0) + 1
            )
            updated_attempt["result_observed_at"] = at
        else:
            updated_attempt["result_observed_at"] = recorded_result.get("observed_at")
        updated_attempt["result_validity_hours"] = 24
    elif active.get("state") == "result_ready":
        # Downgrading a ready attempt (a poll observation other than another
        # result_ready) must clear its stale result reference regardless of
        # which of the two legal result_ready reasons it carried -- task
        # 2.2c's ("result_ready", "result_url_expired") is downgraded by the
        # very same poll_transient/credential/timeout branches a wire
        # ("result_ready", None) attempt is, and the non-result_ready branch
        # of _valid_attempt requires these three fields to be None.
        #
        # Flow-debt record (review round 1, Important #3): generalizing this
        # branch from `elif active_pair == ("result_ready", None):` to
        # `elif active.get("state") == "result_ready":` is a production-
        # semantics change made under a prompt-level authorization that
        # covered "no new livelock" but not this deeper change, and was
        # correctly flagged as owed a BLOCKED report the round-1
        # implementation skipped. The stronger argument that survived
        # review: pre-2.2c, ("result_ready", None) was the *only* pair a
        # result_ready attempt could ever carry, so keying on the pair or on
        # the bare state named the identical set of records. The
        # generalization is a relation-preserving rewrite of that
        # coincidence into its true condition, not new semantics -- it lets
        # the field-clearing keep firing on the same physical attempts after
        # 2.2c added a second, differently-reasoned result_ready member.
        updated_attempt["result_url_sha256"] = None
        updated_attempt["result_observed_at"] = None
        updated_attempt["result_validity_hours"] = None
    # Task 2.4: both wire states below folded onto the same
    # AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND, so the dict's values are read off
    # CONFIRMABLE_PENDING_KINDS rather than repeating the literal a second
    # time; `.get(result.state)` still returns None for every other wire
    # state, unchanged.
    pending_action_kind = {
        "failed": CONFIRMABLE_PENDING_KINDS[("failed", "task_failed")],
        "unexpected_result_count": CONFIRMABLE_PENDING_KINDS[
            ("failed", "unexpected_result_count")
        ],
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
    updated_manifest["conversion_state"] = _conversion_state_for_poll_result(
        result.state
    )
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


def _poll_result_events(
    *,
    manifest: dict,
    private_state: dict,
    updated_manifest: dict,
    updated_private: dict,
    updated_attempt: dict,
    at: str,
) -> tuple[dict, dict]:
    """The intent and committed events a poll observation appends.

    Split out of commit_poll_result so the capacity admission can size the
    exact events this operation is about to append. It performs no I/O.
    """
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
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "conversion_poll_result_committed",
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(updated_manifest),
        "private_hash": object_hash(updated_private),
    }
    return intent, committed


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
    intent, committed = _poll_result_events(
        manifest=manifest,
        private_state=private_state,
        updated_manifest=updated_manifest,
        updated_private=updated_private,
        updated_attempt=updated_attempt,
        at=at,
    )
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(committed, state_fd=descriptors["state"])
    return updated_manifest, updated_private


def _credential_gate_state_columns(config_error_code: str) -> dict:
    """The five folded columns a recorded-credential gate record stores.

    The state half and the reason half are read off two *different*
    FLAT_STATE_MIGRATION rows, which is exactly what a gate record is: the
    folded state of a not-yet-submitted authorized attempt (`not_started`'s
    target state) paired with the reason the credential wire row folds to.
    Neither half is restated here, so design Decision 4's boundary rename
    (credential_source_changed -> credential_fingerprint_changed) is applied
    by the same single producer every other write site goes through -- pass
    the ConfigError code in as both the classification and the wire reason
    code and `_attempt_reason_columns` performs the rename and correctly
    derives a null reason_detail (neither credential reason is a
    REASON_DETAILS key).
    """
    return {
        "state": FLAT_STATE_MIGRATION["not_started"][0],
        **_attempt_reason_columns(
            config_error_code, config_error_code, authorization_kind="initial"
        ),
        "result_refresh_round_count": 0,
    }


def initial_authorization_is_recorded(manifest: dict) -> bool:
    """Whether the recorded-credential gate has already authorized this
    bundle's first attempt.

    The idempotence judgement design Decision 2 requires ("重复调用 MUST 复用
    该 attempt"), and the reason `authorize_initial_attempt` can be called
    unconditionally. It reads the same two columns `_submit_state`'s
    placeholder consumption reads, so the record this predicate calls "already
    authorized" is exactly the record the next successful create consumes.

    Note the predicate does not compare the *reason*: if the gate blocks a
    second time for a different ConfigError code, the recorded reason stays
    the one first observed. Rewriting it would mean mutating a durable attempt
    in place, and appending a second one is structurally impossible -- attempt
    #2 onwards may only carry a "retry" authorization (valid_private_state).
    """
    attempts = manifest.get("conversion_attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    active = attempts[-1]
    return (
        isinstance(active, dict)
        and active.get("state") == "authorized"
        and active.get("authorization_kind") == "initial"
    )


def active_attempt_is_authorized(manifest: dict) -> bool:
    """Whether the active attempt is already an authorized placeholder --
    of *any* kind -- that the next create will consume rather than append
    past.

    Review fix (2.3c round 1, Critical #1). `initial_authorization_is_
    recorded` above answers a narrower question ("has the gate specifically
    authorized attempt #1"), and workflow.py's recorded-credential gate used
    to gate its reuse decision on that narrower predicate alone. A "retry"
    placeholder -- left behind by `commit_retry_decision` after an operator
    accepts a submission_unknown risk -- is just as much an already-
    authorized, not-yet-submitted record as an "initial" one, but it read as
    "not yet authorized" to the narrower check. The gate then tried to
    author a *second* authorization on top of it, which
    `_initial_authorization_state`'s `or attempts` guard correctly refuses
    (the gate authorizes attempt #1 and only attempt #1) -- but a refusal
    there is reported as `invalid_state_transition` /
    `repair_or_restore_work_bundle`: a false corruption verdict against a
    perfectly valid bundle that merely has nothing left to authorize.

    This predicate reads the same two columns `_submit_state`
    (conversion_attempt.py:2427, `attempts[-1].get("state") ==
    "authorized"`) reads to decide whether the next create consumes a
    placeholder in place rather than appending a new attempt -- the same
    source, because both questions are the same question asked from two
    sides: "is the active attempt a placeholder the next create will
    consume?" A caller that finds this true has nothing to authorize and
    nothing to write; it should report the bundle's current state as-is.

    "The next create consumes it" holds only under `_submit_state`'s own
    preconditions (`conversion_state == "ready_to_submit"` and
    `source_staging.state == "source_upload_ready"`); a caller that has not
    already confirmed those may find this true on a bundle -- e.g. an
    authorized placeholder under `conversion_state == "recoverable_error"`
    -- where a create would still refuse with `invalid_state_transition`.
    """
    attempts = manifest.get("conversion_attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    active = attempts[-1]
    return isinstance(active, dict) and active.get("state") == "authorized"


def _initial_authorization_state(
    *, manifest: dict, private_state: dict, config_error_code: str, at: str
) -> dict:
    """The in-memory state a blocked recorded-credential gate would commit.

    Split out of `authorize_initial_attempt` for the same reason
    `_submit_state` is split out of `begin_attempt`: the local-state capacity
    admission has to size exactly the documents and events the writer is about
    to append, without writing anything. It performs no I/O and mutates
    nothing.
    """
    attempts = manifest.get("conversion_attempts")
    staging = manifest.get("source_staging")
    if (
        config_error_code not in CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR
        or manifest.get("conversion_state") != _CREDENTIAL_GATE_CONVERSION_STATE
        or not isinstance(staging, dict)
        or staging.get("state") != "source_upload_ready"
        or not isinstance(attempts, list)
        # The gate authorizes attempt #1 and only attempt #1: an "initial"
        # authorization is legal nowhere else (valid_private_state's index
        # branches), so a non-empty list here means the caller skipped the
        # `active_attempt_is_authorized` reuse check.
        or attempts
        or private_state.get("generation") != manifest.get("generation")
    ):
        raise ConversionAttemptError(
            "invalid_state_transition",
            "The work bundle cannot record an initial conversion authorization.",
        )
    expected_generation = manifest["generation"]
    new_generation = expected_generation + 1
    placeholder = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": "conversion-attempt-0001",
        **_credential_gate_state_columns(config_error_code),
        "api_base": None,
        "request_summary": None,
        "request_hash": None,
        "credential": None,
        "staging_identity": None,
        "submitted_at": None,
        "response_at": None,
        "http_status": None,
        "task_id": None,
        "pending_action": None,
        # No action_id, basis_sha256 or accepted_risk: not one create has been
        # sent, so there is no duplicate charge to risk and no operator
        # decision to record. The evidence is the frozen source/preflight
        # record, and it is checked back by _valid_authorized_evidence.
        "authorization": {
            "evidence_hash": frozen_source_evidence_hash(manifest),
            "authorized_at": at,
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
    updated_manifest["conversion_state"] = _MANIFEST_STATE_BY_FOLDED_STATE[
        (placeholder["state"], placeholder["reason"])
    ]
    updated_manifest["conversion_attempts"] = [placeholder]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    if not _valid_attempt(
        placeholder, manifest=updated_manifest, generation=new_generation
    ):
        raise ConversionAttemptError(
            "integrity_violation",
            "The initial conversion authorization is invalid.",
        )
    return {
        "manifest": manifest,
        "private_state": private_state,
        "updated_manifest": updated_manifest,
        "updated_private": updated_private,
        "attempt": placeholder,
        "kind": "initial",
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
    }


def _initial_authorization_events(state: dict) -> tuple[dict, dict]:
    """The intent and committed events the gate appends around its two writes."""
    placeholder = state["attempt"]
    kind = state["kind"]
    operation_id = f"{placeholder['attempt_id']}-authorize"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": AUTHORIZE_INTENT_EVENT_BY_KIND[kind],
        "operation_id": operation_id,
        "expected_generation": state["expected_generation"],
        "new_generation": state["new_generation"],
        "at": state["at"],
        "evidence_hash": placeholder["authorization"]["evidence_hash"],
        "attempt": placeholder,
        "previous_manifest_hash": object_hash(state["manifest"]),
        "previous_private_hash": object_hash(state["private_state"]),
    }
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": AUTHORIZE_COMMITTED_EVENT_BY_KIND[kind],
        "operation_id": operation_id,
        "previous_generation": state["expected_generation"],
        "generation": state["new_generation"],
        "at": state["at"],
        "manifest_hash": object_hash(state["updated_manifest"]),
        "private_hash": object_hash(state["updated_private"]),
    }
    return intent, committed


def authorize_initial_attempt(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    config_error_code: str,
    at: str,
) -> dict:
    """Record that the recorded-credential gate blocked before any create.

    Idempotent by construction: if the gate has already authorized this
    bundle's first attempt, the manifest is returned untouched -- no second
    attempt, no history event, no generation bump. Callers still have to ask
    `active_attempt_is_authorized` first, because only they can decide
    whether to spend a capacity admission on a write that may not happen.
    """
    if initial_authorization_is_recorded(manifest):
        return manifest
    state = _initial_authorization_state(
        manifest=manifest,
        private_state=private_state,
        config_error_code=config_error_code,
        at=at,
    )
    intent, committed = _initial_authorization_events(state)
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", state["updated_private"], dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", state["updated_manifest"], dir_fd=descriptors["root"]
    )
    bundle.append_history(committed, state_fd=descriptors["state"])
    return state["updated_manifest"]


# Task 2.4 (design.md Decision 5): commit_retry_decision's discriminator,
# re-keyed off (conversion_state, active attempt state, active attempt
# reason) now that every CONFIRMABLE_PENDING_KINDS/raw_conversion.py kind
# folds onto the single AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND and can no
# longer tell the four originating cases apart. Each row below is the
# pre-fold triple with its old third element (a `resolve_*` kind) replaced by
# the active record's own `reason` column -- the same rename
# CONFIRMABLE_PENDING_KINDS' keys already carry, just with `conversion_state`
# restored as the first element since commit_retry_decision (unlike
# CONFIRMABLE_PENDING_KINDS) also has to pin the top-level manifest state.
#
# Known and accepted loss of discriminating granularity: the last row,
# ("terminal_error", "result_ready", None), is shared by two distinct
# originating cases -- a conversion attempt's own resolve_unexpected_result_count
# never reaches "result_ready" (it stores onto "failed"), so this row is hit
# only by raw_conversion.py's resolve_unexpected_result_layout (raw layout
# ambiguity) and by task 3.1's result_url_not_renewed. Do not split this row:
# both cases legitimately want the identical response (authorize a new,
# separately charged attempt), and pending_action.evidence_hash -- computed
# over the whole attempt with pending_action itself cleared -- already
# uniquely binds a decision to the exact (state, reason) record it was issued
# for, without needing kind's help. Splitting the row would only resurrect
# the very discrimination this fold intentionally retired.
RETRY_AUTHORIZABLE_TRIPLES = frozenset(
    {
        ("submission_unknown", "submission_unknown", "no_task_id"),
        ("awaiting_user", "failed", "task_failed"),
        ("terminal_error", "failed", "unexpected_result_count"),
        ("terminal_error", "result_ready", None),
    }
)


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
        # Task 2.4: re-keyed off RETRY_AUTHORIZABLE_TRIPLES -- see its
        # docstring for why the third element is now the active record's
        # `reason` instead of the pending action's `kind`, which lost its
        # discriminating power when every kind folded onto
        # AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND.
        (
            manifest.get("conversion_state"),
            active.get("state") if isinstance(active, dict) else None,
            active.get("reason") if isinstance(active, dict) else None,
        )
        not in RETRY_AUTHORIZABLE_TRIPLES
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
        **_attempt_state_columns("not_started", authorization_kind="retry"),
        "api_base": None,
        "request_summary": None,
        "request_hash": None,
        "credential": None,
        "staging_identity": None,
        "submitted_at": None,
        "response_at": None,
        "http_status": None,
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
    # Read off the projection table, not hardcoded: _authorize_state_from_intent
    # replays this same write from the durable intent and derives the top-level
    # state the same way, and a disagreement between the two would only ever
    # surface as a manifest-hash mismatch during crash recovery.
    updated_manifest["conversion_state"] = _MANIFEST_STATE_BY_FOLDED_STATE[
        (placeholder["state"], placeholder["reason"])
    ]
    updated_manifest["conversion_attempts"] = [*deepcopy(attempts), placeholder]
    updated_private = deepcopy(private_state)
    updated_private["generation"] = new_generation
    operation_id = f"{placeholder['attempt_id']}-authorize"
    intent = {
        "schema_version": SCHEMA_VERSION,
        # Read off the kind tables, like every other reader of these names:
        # a literal here could be renamed apart from the reducer and the
        # crash-recovery branch that have to recognise it.
        "event": AUTHORIZE_INTENT_EVENT_BY_KIND["retry"],
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
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": AUTHORIZE_COMMITTED_EVENT_BY_KIND["retry"],
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(updated_manifest),
        "private_hash": object_hash(updated_private),
    }
    # Decision 9.4: this write must pass the same local-state capacity
    # admission every other conversion write does, before its first history
    # append and before a single byte of the bundle changes.
    assert_local_state_capacity(
        operation=RETRY_DECISION_OPERATION,
        manifest=manifest,
        private_state=private_state,
        history_bytes=sum(
            canonical_state_byte_length(event)
            for event in bundle.read_history(state_fd=descriptors["state"])
        ),
        updated_manifest=updated_manifest,
        updated_private=updated_private,
        history_tail_bytes=(
            canonical_state_byte_length(intent) + canonical_state_byte_length(committed)
        ),
        at=at,
    )
    bundle.append_history(intent, state_fd=descriptors["state"])
    bundle.atomic_write_json(
        "private.json", updated_private, dir_fd=descriptors["state"]
    )
    bundle.atomic_write_json(
        "manifest.json", updated_manifest, dir_fd=descriptors["root"]
    )
    bundle.append_history(committed, state_fd=descriptors["state"])
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
        updated_attempt.get("state"), updated_attempt.get("reason")
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


def _authorize_evidence_matches_kind(
    kind, *, manifest: dict, attempts: list, intent: dict, authorization: dict
) -> bool:
    """The half of an authorization intent's consistency that is kind-specific.

    Exhaustive over AUTHORIZATION_KEYS_BY_KIND and fail-closed outside it: a
    third kind added without a rule here replays as inconsistent rather than
    being admitted on the shared checks alone.
    """
    if kind == "retry":
        active = attempts[-1] if attempts else None
        attempt_pending = (
            active.get("pending_action") if isinstance(active, dict) else None
        )
        raw_record = manifest.get("raw_conversion")
        raw_pending = (
            raw_record.get("pending_action") if isinstance(raw_record, dict) else None
        )
        pending = attempt_pending if isinstance(attempt_pending, dict) else raw_pending
        return (
            isinstance(pending, dict)
            and intent.get("action_id") == pending.get("action_id")
            and intent.get("evidence_hash") == pending.get("evidence_hash")
            and authorization.get("action_id") == intent.get("action_id")
            and authorization.get("basis_sha256") == intent.get("basis_sha256")
        )
    if kind == "initial":
        # The gate authorizes attempt #1 only, against evidence the manifest
        # can recompute rather than against a predecessor pending action.
        return not attempts and intent.get(
            "evidence_hash"
        ) == frozen_source_evidence_hash(manifest)
    return False


def _authorize_state_from_intent(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    """Replay a durable authorization intent of either kind.

    The kind is taken from the placeholder authorization's own key set, the
    same discriminator `_valid_attempt` cross-checks the `authorization_kind`
    column against -- so an intent cannot claim one kind's event name while
    carrying the other kind's evidence.
    """
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    placeholder = intent.get("attempt")
    attempts = manifest.get("conversion_attempts")
    authorization = (
        placeholder.get("authorization") if isinstance(placeholder, dict) else None
    )
    kind = _authorization_kind_of(authorization)
    if (
        # `kind is None` covers every shape this function used to reject with
        # `not isinstance(placeholder, dict)` / `not isinstance(authorization,
        # dict)`, and short-circuits before any table lookup or attribute
        # access below.
        kind is None
        or set(intent) != AUTHORIZE_INTENT_KEYS_BY_KIND[kind]
        or intent.get("schema_version") != SCHEMA_VERSION
        or intent.get("event") != AUTHORIZE_INTENT_EVENT_BY_KIND[kind]
        or type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or manifest.get("generation") != expected_generation
        or private_state.get("generation") != expected_generation
        or object_hash(manifest) != intent.get("previous_manifest_hash")
        or object_hash(private_state) != intent.get("previous_private_hash")
        or not isinstance(attempts, list)
        or placeholder.get("state") != "authorized"
        or authorization.get("evidence_hash") != intent.get("evidence_hash")
        or authorization.get("authorized_at") != intent.get("at")
        or intent.get("operation_id")
        != f"{placeholder.get('attempt_id')}-authorize"
        or not _authorize_evidence_matches_kind(
            kind,
            manifest=manifest,
            attempts=attempts,
            intent=intent,
            authorization=authorization,
        )
    ):
        raise ConversionAttemptError(
            "integrity_violation", "A conversion authorization intent is inconsistent."
        )
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    # Read off the projection table rather than hardcoded, so this replay and
    # the two writers (commit_retry_decision, _initial_authorization_state)
    # cannot disagree about the top-level state -- a disagreement would only
    # ever surface as a manifest-hash mismatch during crash recovery.
    desired_manifest["conversion_state"] = _MANIFEST_STATE_BY_FOLDED_STATE[
        (placeholder.get("state"), placeholder.get("reason"))
    ]
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
            "integrity_violation",
            "A conversion authorization placeholder is invalid.",
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
            elif event in _AUTHORIZE_COMMITTED_EVENT_BY_INTENT_EVENT:
                desired_manifest, desired_private = _authorize_state_from_intent(
                    current_manifest, current_private, intent
                )
                expected_events = {
                    _AUTHORIZE_COMMITTED_EVENT_BY_INTENT_EVENT[event]
                }
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
        # Where this module's segment of the history begins. Task 2.3c widens
        # this from the literal "conversion_submit_intent" to any member of
        # CONVERSION_INTENTS -- the same set apply_committed_operations
        # branches on -- because the recorded-credential gate's authorization
        # intent can now be the *first* conversion event a bundle ever
        # appends. It is a no-op for every pre-2.3c bundle: the retry intent
        # is only ever written after a submit, so conversion_submit_intent was
        # necessarily first.
        first = next(
            (
                index
                for index, event in enumerate(history)
                if isinstance(event, dict)
                and event.get("event") in CONVERSION_INTENTS
            ),
            None,
        )
        if not isinstance(private_template, dict):
            return None
        prefix_private = deepcopy(private_template)
        prefix_private["result_urls"] = []
        reduced_prefix = source_staging.resolve_history_state(
            history[: len(history) if first is None else first],
            manifest_template={},
            private_template=prefix_private,
        )
        if reduced_prefix is None:
            return None
        if first is None:
            # A history with no conversion operation in it at all. Task 2.3c
            # makes that reachable through this module: the gate's
            # authorization intent can be the first conversion event a bundle
            # ever appends, and recover_interrupted_attempt reduces
            # `history[:-1]` -- which then holds nothing but source staging.
            # Returning None here reported "a pending conversion
            # authorization has no valid history prefix" for a prefix that is
            # perfectly valid, wedging every crash boundary of that write.
            #
            # This does not weaken valid_history: its caller only reaches this
            # module when the manifest already carries a conversion attempt,
            # and a source-staging-only reduction cannot produce one, so such
            # a bundle still compares unequal and is still refused.
            return reduced_prefix
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
            # Task 2.3b routes both invariants below through the
            # authorization's own shape rather than the attempt's
            # `authorization_kind` column. This loop runs over the whole
            # attempt history, and an authorized record's kind is dropped the
            # moment it folds to "submitting" (_submit_state rebuilds the
            # columns from _attempt_state_columns), so keying on the column
            # would mean an initial attempt's own history entry stops being
            # recognisable one transition after it is written -- and, on the
            # index >= 2 branch, would silently stop enforcing predecessor
            # equality for every retry that has already been submitted.
            authorization_shape = _authorization_kind_of(authorization)
            if index == 1:
                # Opened for exactly one shape, not lifted. "initial" is the
                # only authorization with no predecessor pending action to
                # answer for -- it is authorized against the frozen
                # source/preflight evidence before any create has been sent.
                # A "retry"-shaped authorization on attempt #1 claims an
                # action_id and evidence_hash that must equal a predecessor
                # that does not exist, so it stays refused here, exactly as
                # every non-None authorization was before this task.
                #
                # Task 2.3c adds the content half. Shape alone cannot separate
                # a genuine initial authorization from a retry one stripped of
                # its three retry-only keys -- the two key sets are then
                # identical -- so the evidence itself is checked here, over
                # the attempt's whole life rather than only while it is still
                # in the "authorized" state (_valid_attempt's authorized
                # branch stops applying the moment _submit_state folds the
                # record to "submitting", but the authorization rides along).
                if authorization is not None and (
                    authorization_shape != "initial"
                    or not _valid_authorized_evidence(
                        authorization_shape, authorization, manifest
                    )
                ):
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
                # Only a "retry" authorization can stand here, and it still
                # has to match the pending action it answers field for
                # field. `authorization_shape != "retry"` subsumes the
                # `not isinstance(authorization, dict)` this used to open
                # with -- a None or non-dict authorization has no shape --
                # and additionally refuses an "initial" authorization, which
                # is legal only on attempt #1.
                if (
                    authorization_shape != "retry"
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
            if attempt.get("state") == "authorized":
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
        active_pair = (active.get("state"), active.get("reason"))
        expected_manifest_state = _MANIFEST_STATE_BY_FOLDED_STATE.get(active_pair)
        if manifest.get("conversion_state") != expected_manifest_state:
            return False
        active_pending = active.get("pending_action")
        mode = manifest.get("settings_snapshot", {}).get("interaction_mode")
        # Re-keyed by the fold. The pre-fold set was the flat
        # {"submission_unknown", "failed", "unexpected_result_count"};
        # carrying it over verbatim would demand a pending action from all ten
        # reasons `failed` now covers, making every recoverable record
        # (poll_transient, task_unavailable, ...) instantly invalid.
        if active_pair in CONFIRMABLE_PAIRS:
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
    result["conversion_attempt_reason"] = attempt["reason"]
    result["conversion_attempt_reason_detail"] = attempt["reason_detail"]
    pending = attempt.get("pending_action")
    if isinstance(pending, dict):
        result["action_required"] = pending["kind"]
        result["action_id"] = pending["action_id"]
        result["evidence_hash"] = pending["evidence_hash"]
    return result
