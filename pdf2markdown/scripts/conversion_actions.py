from __future__ import annotations

from typing import Callable, NamedTuple


# Task 3.1d (design.md Decision 5, plus its 2026-07-27 correction ①) -- the
# ordered rule table that replaces the five `result_from_manifest` wrappers'
# sequential override chain, and the closed vocabularies its guards read.
#
# WHY THIS MODULE EXISTS AT ALL, rather than the table living in
# conversion_attempt.py where task 3.1a first landed it:
#
# The wrapper chain's import DAG is
#
#     preflight <- source_staging <- conversion_attempt <- raw_conversion
#                <- review
#
# (each module imports the one to its left). A projector defined in
# conversion_attempt.py is therefore invisible to preflight.py and
# source_staging.py, the two layers at the bottom of the chain -- which is
# why 3.1a could only wire the top two layers and had to leave a SECOND
# implementation of tier 2 inside source_staging.result_from_manifest, a
# live drift channel that task 3.1a fix round 1 could only lock with an
# equivalence test rather than remove.
#
# This module is a LEAF: it imports nothing from the five wrapper modules
# (nothing from this package at all). Every one of the five can therefore
# import it, and the projector is applied exactly ONCE, at the bottom of the
# chain (preflight.result_from_manifest), with every layer above it adding
# only its own layer-specific keys. There is no override chain left to be
# "last" in.
#
# That leaf constraint is also why the vocabularies below live here rather
# than in conversion_attempt.py: the guards derive their reason domains from
# FLAT_STATE_MIGRATION and the action names from CONVERSION_ACTIONS /
# SOURCE_STAGING_ACTIONS, and a leaf module cannot read them across an
# import it is not allowed to have. Restating them here as second literals
# and locking the copies with a test would reproduce exactly the drift
# channel this task exists to remove, so the tables moved down whole and
# their original modules re-export the same objects (conversion_attempt.py
# and source_staging.py both bind the names to these very objects, so there
# is no copy to drift).


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
# So every table keyed by one of these 18 names is keyed by a WIRE
# classification, not by a stored attempt state; the names say so
# (`_..._BY_FLAT_STATE`, `_poll_response_branches`). Anything that reads a
# stored record keys on `(state, reason)` instead -- and, for the one place
# the fold is not injective, on `upstream_status` as well (see
# conversion_attempt._MANIFEST_STATE_BY_FOLDED_STATE).
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

# design.md Decision 1 row 8, second form. Locally *detected* -- no wire
# classification ever produces it, so it lives outside the two
# characterization tables (FLAT_STATE_MIGRATION / LEGAL_TRIPLES stay 18
# rows of measured wire domain) and is spliced into every derivation
# that defines pair legality. Write side lands in 2.2c.
LOCALLY_DETECTED_PAIRS = {
    ("result_ready", "result_url_expired"): "recoverable_error",
}

# The two ConfigError codes the recorded-credential gate can fail with are
# FLAT_STATE_MIGRATION keys, so design Decision 4's boundary rename
# (credential_source_changed -> credential_fingerprint_changed) is *read off*
# the migration table here rather than restated -- this dict is the one place
# the create-path gate's code -> reason mapping lives, and
# conversion_attempt._CREDENTIAL_ERROR_PAIRS is derived from it instead of
# carrying a second copy of the two folded names.
CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR = {
    config_error_code: FLAT_STATE_MIGRATION[config_error_code][1]
    for config_error_code in ("credential_source_missing", "credential_source_changed")
}

# Task 2.4 (design.md Decision 5/9.3): the closed, workflow-facing action
# vocabulary a conversion attempt or a raw_conversion record's pending_action
# may carry. The four `resolve_*` kinds this replaced --
# resolve_submission_unknown, resolve_task_failed,
# resolve_unexpected_result_count and raw_conversion.py's
# resolve_unexpected_result_layout -- all asked the operator for the exact
# same decision (authorize a new, separately charged conversion attempt), so
# distinguishing them by kind name was spurious detail, not real
# discrimination. Folding them means commit_retry_decision's old
# `pending.kind` comparison loses the (state, reason) information it used to
# get for free from the kind name; that information moves onto
# conversion_attempt.RETRY_AUTHORIZABLE_TRIPLES, which discriminates directly
# on (conversion_state, attempt state, reason) instead.
#
# Disjoint from workflow.ERROR_PATH_ACTIONS by production mechanism, not by
# value-clash avoidance -- workflow.py spells out why the two tables are
# allowed to share the action_required key without being each other's
# complement.
CONVERSION_ACTIONS = frozenset(
    {
        "resume_pending_conversion_operation",
        "restore_recorded_aihub_credential",
        "resume_same_conversion_task",
        "authorize_new_conversion_attempt",
        "adopt_conversion_result",
    }
)

# The single CONVERSION_ACTIONS member every confirmable pending_action
# carries. Named once so every producer -- CONFIRMABLE_PENDING_KINDS below,
# the submission-result and poll-result writers in conversion_attempt.py, and
# raw_conversion.py's layout-ambiguity writer -- reads the same value instead
# of repeating the string literal.
AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND = "authorize_new_conversion_attempt"
# Minor fix (task 3.1a fix round 1, carried from the 2.4 review's M4): a bare
# `assert` is stripped under `python -O`, silently dropping this import-time
# guard in exactly the deployment mode where it matters most.
if AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND not in CONVERSION_ACTIONS:
    raise ValueError(
        f"{AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND!r} is not a member of "
        "CONVERSION_ACTIONS"
    )

# The folded pairs whose records may carry a pending action, and the kind each
# one takes. Pre-fold this was three flat states -- submission_unknown,
# failed, unexpected_result_count -- and both _valid_pending_action's
# expected_kinds and valid_private_state's confirm-mode invariant listed them
# separately. They are one table now: `failed` covers ten reasons after the
# fold, and reusing the flat set would demand a pending action from all ten,
# instantly invalidating every recoverable record (poll_transient,
# task_unavailable, ...).
CONFIRMABLE_PENDING_KINDS = {
    ("submission_unknown", "no_task_id"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ("failed", "task_failed"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ("failed", "unexpected_result_count"): AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
}
CONFIRMABLE_PAIRS = frozenset(CONFIRMABLE_PENDING_KINDS)

# Task 3.1a (design.md Decision 5's M6 review point) -- the single owner of
# which pending_action `kind` a source-staging attempt state carries. Used to
# be three separately-typed inline dict literals in source_staging.py
# (_valid_pending_action, _pending_action, commit_source_staging_decision's
# allowed_action lookup) with no mechanical link between them.
# project_conversion_action's tier "2-source-staging" passes this closed
# vocabulary's value through unchanged rather than folding it into
# CONVERSION_ACTIONS -- design.md Decision 5's table says the source-staging
# tier "沿用 source-staging 既有 kind（不属于 conversion 闭合表）" -- so the
# value domain needs a name of its own for the projector to validate a value
# against at the point it reads one back out of a live pending_action object.
#
# Task 3.1d moves both names down here from source_staging.py, which now
# re-exports these same two objects: the projector has to see the staging
# vocabulary (both for the disjointness guard below and for the live-value
# check in project_conversion_action), and a leaf module cannot import
# source_staging.py. Moving the table rather than copying its three values
# is what keeps "one owner" true.
PENDING_ACTION_KIND_BY_STATE = {
    "source_upload_unknown": "resolve_source_upload_unknown",
    "source_upload_rejected": "retry_source_upload",
    "source_upload_expired": "retry_expired_source_upload",
}
SOURCE_STAGING_ACTIONS = frozenset(PENDING_ACTION_KIND_BY_STATE.values())


# design.md Decision 5, tier 3's reason domain -- read literally off the
# table: "attempt reason ∈ {...}", no attempt-state qualifier. That also
# covers Decision 2's "initial" credential-gate placeholder (attempt state
# "authorized", not "failed" -- see conversion_attempt.
# AUTHORIZED_STATE_REASONS_BY_KIND["initial"] /
# CREDENTIAL_GATE_AUTHORIZED_PAIRS), which never reaches "failed" at all: the
# gate blocking before create and a credential failure observed while polling
# an already-submitted task both mean "go fix the recorded credential", so
# both project the same action. Derived from FLAT_STATE_MIGRATION /
# CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR rather than restated, same as every
# other folded-name table.
_CREDENTIAL_TIER_REASONS = frozenset(
    CREDENTIAL_GATE_REASON_BY_CONFIG_ERROR.values()
) | {FLAT_STATE_MIGRATION["poll_unauthorized"][1]}

# design.md Decision 5, tier 4c's `failed`-branch reasons: the recoverable,
# auto-resumable observations (excludes the two confirmable reasons, which
# are tier 4b's, and the three credential reasons, which are tier 3's).
_RECOVERABLE_TASK_REASONS = frozenset(
    FLAT_STATE_MIGRATION[flat_state][1]
    for flat_state in (
        "task_unavailable",
        "poll_transient",
        "poll_timeout",
        "result_pending_timeout",
    )
)

RESUME_PENDING_CONVERSION_OPERATION_KIND = "resume_pending_conversion_operation"
RESTORE_RECORDED_AIHUB_CREDENTIAL_KIND = "restore_recorded_aihub_credential"
RESUME_SAME_CONVERSION_TASK_KIND = "resume_same_conversion_task"
ADOPT_CONVERSION_RESULT_KIND = "adopt_conversion_result"
for _literal_kind in (
    RESUME_PENDING_CONVERSION_OPERATION_KIND,
    RESTORE_RECORDED_AIHUB_CREDENTIAL_KIND,
    RESUME_SAME_CONVERSION_TASK_KIND,
    ADOPT_CONVERSION_RESULT_KIND,
):
    if _literal_kind not in CONVERSION_ACTIONS:
        raise ValueError(f"{_literal_kind!r} is not a member of CONVERSION_ACTIONS")
del _literal_kind

# The one value Rule.kind may hold instead of a literal action string: "this
# tier's action is not ours to name, read it back off the matched
# pending_action instead" (design.md Decision 5 tier 2: "沿用 source-staging
# 既有 kind（不属于 conversion 闭合表）"). A plain object() so it can never
# collide with a real action name by accident.
_KIND_FROM_STAGING_PENDING_ACTION = object()


# _ActionContext is the read-only view every Rule's guard may see: design.md
# Decision 5 pins this to "(conversion_state, attempt state, reason) 加
# pending flag、staging state、interaction_mode" -- explicitly NOT
# reason_detail (task 3.2's cartesian-product uniqueness proof depends on
# that exclusion holding). `conversion_state` itself is not a separate field
# here: for every LEGAL (attempt_state, reason) pair it is a single-valued
# function of that pair (conversion_attempt._MANIFEST_STATE_BY_FOLDED_STATE),
# so re-reading it from the manifest would only ever repeat information the
# pair already carries, never add new information a guard could act on.
class _ActionContext(NamedTuple):
    pending_conversion_operation: bool
    staging_pending_action: dict | None
    raw_pending_action: dict | None
    raw_conversion_exists_for_active_attempt: bool
    attempt_state: str | None
    attempt_reason: str | None
    attempt_pending_action: dict | None
    interaction_mode: str | None


def _action_context(
    manifest: dict, *, pending_conversion_operation: bool = False
) -> _ActionContext:
    """Build the guard-visible view of `manifest` project_conversion_action
    scans ACTION_RULES against.

    CALLER OBLIGATION (task 3.1a review M2, landed in 3.1d): tier 1 --
    "resume_pending_conversion_operation", the tier that outranks every
    other row of the table -- fires *only* when the caller passes
    `pending_conversion_operation=True` explicitly. The default is False and
    that default is FAIL-OPEN: a caller that is sitting on an unclosed
    conversion intent but forgets the argument gets a silently
    tier-1-less projection (whatever tier 2/3/4 the manifest happens to
    match, or None), not an error. Nothing in the manifest can restore the
    signal, because it is deliberately not a manifest field (see below). The
    only production producer is workflow._inspect_open_bundle, which reads it
    off conversion_attempt.at_pending_conversion_boundary -- the same single
    predicate that decides the `WorkflowError` narrow mouth's action, so the
    two paths cannot disagree about whether a bundle is at a pending
    boundary.

    `pending_conversion_operation` is not a persisted manifest field -- it is
    a signal the caller passes as an explicit keyword-only argument, not a
    key written onto the manifest dict. Task 3.1a fix round 1 (I5) closes a
    sentinel key (`manifest["_pending_conversion_operation"]`) this used to
    read instead: bundle.py / workflow.py's exact-key-set comparisons
    (`set(manifest) == <closed key set>`) would fold a manifest carrying
    that key into `invalid_bundle`, and nothing structurally stopped a
    `deepcopy(manifest)` call along some future path from writing it to
    disk. A keyword-only parameter cannot leak into the persisted manifest
    key set at all, closing that illegal state at the type level rather than
    trusting every caller to strip the key back out. It is never written to
    manifest.json: nothing in this module or its callers persists it.

    `raw_pending_action` and `raw_conversion_exists_for_active_attempt` are
    both scoped to the *active* attempt (matched by attempt_id), not to
    whatever `manifest["raw_conversion"]` happens to hold. A retry
    authorized after a raw rejection (design.md Decision 5's own case 9.3)
    appends a new attempt but does not clear the old raw_conversion record
    (commit_retry_decision only ever touches generation/conversion_state/
    conversion_attempts) -- so an unscoped read would let a *previous*
    attempt's already-resolved raw record either wrongly re-arm tier 4a for
    an attempt that never asked for adoption, or wrongly suppress tier 4d
    for a fresh result_ready attempt that has never been adopted at all.
    Scoping by attempt_id is what test_layout_retry_action_is_removed_when_
    override_switches_to_auto (tests/unit/test_raw_conversion.py) requires:
    once raw_conversion concludes (committed or terminally rejected) for the
    active attempt with no pending_action, tier 4d must not re-offer
    "adopt_conversion_result" for a result that was never re-submitted.
    """
    attempts = manifest.get("conversion_attempts")
    active = attempts[-1] if isinstance(attempts, list) and attempts else None
    active_attempt_id = active.get("attempt_id") if isinstance(active, dict) else None
    staging = manifest.get("source_staging")
    raw = manifest.get("raw_conversion")
    raw_belongs_to_active_attempt = (
        active_attempt_id is not None
        and isinstance(raw, dict)
        and raw.get("attempt_id") == active_attempt_id
    )
    settings_snapshot = manifest.get("settings_snapshot")
    return _ActionContext(
        pending_conversion_operation=pending_conversion_operation,
        staging_pending_action=(
            staging.get("pending_action") if isinstance(staging, dict) else None
        ),
        raw_pending_action=(
            raw.get("pending_action") if raw_belongs_to_active_attempt else None
        ),
        raw_conversion_exists_for_active_attempt=raw_belongs_to_active_attempt,
        attempt_state=active.get("state") if isinstance(active, dict) else None,
        attempt_reason=active.get("reason") if isinstance(active, dict) else None,
        attempt_pending_action=(
            active.get("pending_action") if isinstance(active, dict) else None
        ),
        interaction_mode=(
            settings_snapshot.get("interaction_mode")
            if isinstance(settings_snapshot, dict)
            else None
        ),
    )


class Rule(NamedTuple):
    """One row of the ordered precedence table design.md Decision 5 defines.

    `matches` reads only the fields _ActionContext exposes -- never
    reason_detail. `kind` is a literal CONVERSION_ACTIONS member, `None`
    (the catch-all: no action), or `_KIND_FROM_STAGING_PENDING_ACTION` for
    the one tier whose action is not from the closed conversion vocabulary
    at all.
    """

    tier: str
    matches: Callable[[_ActionContext], bool]
    kind: str | None


# design.md Decision 5's four-tier precedence table, in matching order:
# scanned top to bottom, first match wins. "唯一性" (task 3.2) is the claim
# that for every legal combination exactly one row matches; this table does
# not prove that on its own, it only has to be consistent with it. The
# trailing catch-all (kind=None) is design.md's "null 视为一条兜底规则":
# every _ActionContext matches something.
ACTION_RULES: tuple[Rule, ...] = (
    Rule(
        "1-pending-operation",
        lambda context: context.pending_conversion_operation,
        RESUME_PENDING_CONVERSION_OPERATION_KIND,
    ),
    Rule(
        "2-source-staging",
        lambda context: context.staging_pending_action is not None,
        _KIND_FROM_STAGING_PENDING_ACTION,
    ),
    Rule(
        "3-credential",
        lambda context: context.attempt_reason in _CREDENTIAL_TIER_REASONS,
        RESTORE_RECORDED_AIHUB_CREDENTIAL_KIND,
    ),
    Rule(
        "4a-raw",
        lambda context: context.raw_pending_action is not None,
        AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ),
    Rule(
        "4b-attempt",
        lambda context: (
            (context.attempt_state, context.attempt_reason) in CONFIRMABLE_PAIRS
            and context.interaction_mode == "confirm"
        ),
        AUTHORIZE_NEW_CONVERSION_ATTEMPT_KIND,
    ),
    Rule(
        "4c-recoverable",
        lambda context: (
            context.attempt_state == "failed"
            and context.attempt_reason in _RECOVERABLE_TASK_REASONS
        )
        or (context.attempt_state, context.attempt_reason) in LOCALLY_DETECTED_PAIRS,
        RESUME_SAME_CONVERSION_TASK_KIND,
    ),
    Rule(
        "4d-ready",
        lambda context: (
            context.attempt_state == "result_ready"
            and context.attempt_reason is None
            and not context.raw_conversion_exists_for_active_attempt
        ),
        ADOPT_CONVERSION_RESULT_KIND,
    ),
    Rule("none", lambda context: True, None),
)


# M6 (task 2.4 review, carried into 3.1a): import-time closure check on the
# rule table. "conversion" tiers (every row except the staging one and the
# catch-all) must name a CONVERSION_ACTIONS member; the staging tier must
# NOT -- it is a different, independently-closed vocabulary
# (SOURCE_STAGING_ACTIONS) -- so a blanket `kind in CONVERSION_ACTIONS`
# check across the whole table would be wrong, not just incomplete
# (design.md Decision 5's own table: "source-staging -> 沿用 source-staging
# 既有 kind（不属于 conversion 闭合表）"). `python -O` strips bare `assert`,
# so this raises explicitly -- the same fail-loud shape as CONVERSION_ACTIONS'
# own import-time guard above.
#
# Task 3.1a-M3 (folded in by 3.1d): the tier NAMES must also be unique.
# _EVIDENCE_SOURCE_BY_TIER is keyed by tier name, so two rows sharing a name
# would silently make the second row inherit the first's evidence source --
# a mis-binding that produces a wrong action_id/evidence_hash rather than an
# error, and that the overreach check below cannot see.
def _validate_action_rules() -> None:
    if SOURCE_STAGING_ACTIONS & CONVERSION_ACTIONS:
        raise ValueError(
            "SOURCE_STAGING_ACTIONS collides with CONVERSION_ACTIONS -- the "
            "staging and conversion action vocabularies must stay disjoint."
        )
    seen: set[str] = set()
    for rule in ACTION_RULES:
        if rule.tier in seen:
            raise ValueError(
                f"ACTION_RULES has more than one row named {rule.tier!r}; tier "
                "names key _EVIDENCE_SOURCE_BY_TIER and must be unique"
            )
        seen.add(rule.tier)
        if rule.kind is _KIND_FROM_STAGING_PENDING_ACTION:
            continue
        if rule.kind is not None and rule.kind not in CONVERSION_ACTIONS:
            raise ValueError(
                f"ACTION_RULES row {rule.tier!r} names {rule.kind!r}, which "
                "is not a member of CONVERSION_ACTIONS"
            )


_validate_action_rules()

# The stored pending_action object each matched tier's action_id/
# evidence_hash come from, keyed by tier. Tiers not listed here (1, 3, 4c,
# 4d, and the catch-all) are informational: design.md Decision 5 projects an
# action for them from (state, reason, mode) alone, not from a stored
# confirmable decision -- "resume the pending operation", "go fix the
# credential" and "just resume/adopt" are none of them answered through
# `record conversion --decision`, so there is no pending_action object to
# bind an action_id/evidence_hash to. Tier 2 and 4a/4b's actions ARE
# `record ... --decision`-shaped confirmable decisions in the well-formed
# manifests these rules' guards are matched against -- e.g. tier 4b's
# mode==confirm guard is exactly the condition production code already
# gates writing that pending_action on (conversion_attempt.
# _submission_result_state / _poll_transition) -- so a stored pending_action
# is present whenever these three tiers match.
_EVIDENCE_SOURCE_BY_TIER: dict[str, Callable[[_ActionContext], dict | None]] = {
    "2-source-staging": lambda context: context.staging_pending_action,
    "4a-raw": lambda context: context.raw_pending_action,
    "4b-attempt": lambda context: context.attempt_pending_action,
}
# Minor fix (task 3.1a fix round 1): _EVIDENCE_SOURCE_BY_TIER's keys must
# name real ACTION_RULES rows -- a typo'd or stale tier name here would
# silently never fire (dict.get returns None, same as "this tier has no
# evidence source") rather than raising, which is indistinguishable from an
# intentionally informational tier. `raise`, not a bare `assert`, for the
# same `python -O` reason as every other import-time guard here.
_ACTION_RULE_TIERS = frozenset(rule.tier for rule in ACTION_RULES)
_evidence_source_tier_overreach = frozenset(_EVIDENCE_SOURCE_BY_TIER) - _ACTION_RULE_TIERS
if _evidence_source_tier_overreach:
    raise ValueError(
        "_EVIDENCE_SOURCE_BY_TIER has keys outside ACTION_RULES' tier "
        f"domain: {sorted(_evidence_source_tier_overreach)!r}"
    )
del _evidence_source_tier_overreach


def project_conversion_action(
    manifest: dict, *, pending_conversion_operation: bool = False
) -> dict | None:
    """The single source of `action_required` / `action_id` / `evidence_hash`
    for the closed conversion action vocabulary (design.md Decision 5).

    Scans ACTION_RULES in order and returns the first match's projection
    (`None` when only the trailing catch-all matches). This replaces the
    five `result_from_manifest` wrappers' sequential override chain.

    Applied exactly once per result, at the bottom of that chain
    (preflight.result_from_manifest, which every other wrapper reaches
    through its own base call). It reads the WHOLE manifest, not a per-layer
    slice, so the bottom layer already has the fully precedence-correct
    answer and no layer above it needs -- or is allowed -- to override those
    three keys off its own pending_action. review.result_from_manifest is
    the one wrapper that still writes them, and only on the branch where
    this function returned None: its own pending_action vocabulary is
    outside design.md Decision 5's tiers entirely, so "no conversion action"
    is exactly when the review layer's own answer is the right one.

    CALLER OBLIGATION for tier 1: `pending_conversion_operation` defaults to
    False and that default is fail-open -- see `_action_context`'s docstring.
    A caller at a pending conversion boundary that omits the argument gets a
    projection with tier 1 silently not firing.
    """
    context = _action_context(
        manifest, pending_conversion_operation=pending_conversion_operation
    )
    rule = next(candidate for candidate in ACTION_RULES if candidate.matches(context))
    if rule.kind is _KIND_FROM_STAGING_PENDING_ACTION:
        pending = context.staging_pending_action
        kind = pending["kind"]
        if kind not in SOURCE_STAGING_ACTIONS:
            raise ValueError(
                "source_staging pending_action kind is not a member of "
                f"SOURCE_STAGING_ACTIONS: {kind!r}"
            )
    else:
        kind = rule.kind
    if kind is None:
        return None
    evidence_source = _EVIDENCE_SOURCE_BY_TIER.get(rule.tier)
    evidence = None if evidence_source is None else evidence_source(context)
    return {
        "action_required": kind,
        "action_id": None if evidence is None else evidence["action_id"],
        "evidence_hash": None if evidence is None else evidence["evidence_hash"],
    }
