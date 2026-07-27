import pytest

import action_registry
from action_registry import (
    ACTIONS, NO_REMOTE_EFFECT_ACTIONS, RECOVERY_STATES, allowed_actions, retry_safe,
)
from delivery_schema import (
    BLOCKING_REASONS, DISPOSITIONS, OPERATIONS, OUTCOMES, OUTCOME_WRITE_CERTAINTY,
    REMOTE_REJECTIONS, VERIFICATION_CHANNELS,
)


def test_outcomes_vocabulary_is_locked():
    assert OUTCOMES == (
        "adopted", "blocked", "created", "not_applied", "reconciled", "rejected",
        "unknown",
    )


def test_dispositions_are_exactly_the_three_reference_states():
    assert DISPOSITIONS == ("adopted", "created", "reconciled")
    assert set(DISPOSITIONS) < set(OUTCOMES)


def test_verification_channels_are_locked_and_separate():
    assert VERIFICATION_CHANNELS == ("anonymous_public_get", "authenticated_full_get")


def test_remote_rejections_are_a_closed_dimension_inside_blocking_reasons():
    assert REMOTE_REJECTIONS == (
        "remote_rejected_absent", "remote_rejected_denied", "remote_rejected_invalid",
        "remote_rejected_unclassified",
    )
    assert set(REMOTE_REJECTIONS) <= set(BLOCKING_REASONS)


def test_blocking_reasons_vocabulary_is_locked():
    assert BLOCKING_REASONS == (
        "abort_unavailable",
        "already_acknowledged",
        "authorization_missing",
        "authorization_stale",
        "caller_drift",
        "capability_missing",
        "collision_unverified",
        "content_mismatch",
        "cwd_drift",
        "executable_drift",
        "handoff_unsafe",
        "handoff_write_failed",
        "plan_not_executable",
        "public_verification_failed",
        "remote_rejected_absent",
        "remote_rejected_denied",
        "remote_rejected_invalid",
        "remote_rejected_unclassified",
        "resume_unavailable",
        "source_drift",
        "state_root_drift",
        "target_contract_drift",
        "token_invalid",
        "unclassified_outcome",
        "verification_capability_missing",
        "verification_incomplete",
    )


def test_every_vocabulary_is_sorted_and_unique():
    for name, table in (
        ("outcomes", OUTCOMES), ("dispositions", DISPOSITIONS),
        ("channels", VERIFICATION_CHANNELS), ("rejections", REMOTE_REJECTIONS),
        ("reasons", BLOCKING_REASONS),
    ):
        assert list(table) == sorted(table), name
        assert len(set(table)) == len(table), name


def test_outcome_write_certainty_covers_every_outcome_exactly():
    assert set(OUTCOME_WRITE_CERTAINTY) == set(OUTCOMES)
    assert OUTCOME_WRITE_CERTAINTY["created"] is True
    assert OUTCOME_WRITE_CERTAINTY["adopted"] is False
    assert OUTCOME_WRITE_CERTAINTY["reconciled"] is None
    assert OUTCOME_WRITE_CERTAINTY["not_applied"] is False
    assert OUTCOME_WRITE_CERTAINTY["rejected"] is False
    assert OUTCOME_WRITE_CERTAINTY["unknown"] is None
    assert OUTCOME_WRITE_CERTAINTY["blocked"] is False


def test_operations_vocabulary_is_untouched():
    assert OPERATIONS == ("publish", "reconcile", "resume", "abort", "ack", "inspect")


@pytest.mark.parametrize("capabilities_ok", [True, False])
def test_retry_safe_implies_a_mutation_action_is_actually_available(capabilities_ok):
    for state in RECOVERY_STATES:
        available = set(allowed_actions(state, capabilities_ok=capabilities_ok))
        mutating = available - NO_REMOTE_EFFECT_ACTIONS
        assert retry_safe(state, capabilities_ok=capabilities_ok) == bool(
            mutating and state in {"not_started", "known_not_applied"}
        ), state


def test_retry_safe_is_false_whenever_capabilities_are_missing():
    for state in RECOVERY_STATES:
        assert retry_safe(state, capabilities_ok=False) is False, state


def test_retry_safe_default_keeps_the_pre_existing_answer():
    assert retry_safe("not_started") is True
    assert retry_safe("known_not_applied") is True
    assert retry_safe("in_flight_unknown") is False
    assert retry_safe("blocked") is False


def test_retry_safe_follows_the_action_table_not_only_the_retry_table(monkeypatch):
    # test_retry_safe_implies_a_mutation_action_is_actually_available reads the
    # same allowed_actions/NO_REMOTE_EFFECT_ACTIONS pair the implementation
    # subtracts, so that half of its assertion is an identity; what it really
    # pins is the _RETRY_SAFE literal. Replacing the derivation with a lookup
    # of _RETRY_SAFE survived the whole suite. This case moves the action table
    # out from under retry_safe and asserts a literal answer instead, so the
    # injected value is not something the assertion reads back.
    monkeypatch.setitem(action_registry._TABLE, "not_started", ("inspect",))
    assert retry_safe("not_started", capabilities_ok=True) is False


def test_verify_public_is_authorized_but_never_a_caller_action():
    assert "verify_public" in ACTIONS
    for state in RECOVERY_STATES:
        for capabilities_ok in (True, False):
            assert "verify_public" not in allowed_actions(
                state, capabilities_ok=capabilities_ok
            ), state
