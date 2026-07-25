import pytest

from action_registry import (
    ACTIONS,
    AUTHORIZED_ACTIONS,
    AUTHORIZED_ACTION_ORDER,
    ActionRegistryError,
    RECOVERY_STATES,
    _RETRY_SAFE,
    _TABLE,
    allowed_actions,
    retry_safe,
)


def test_recovery_states_are_exactly():
    assert RECOVERY_STATES == (
        "not_started",
        "known_not_applied",
        "in_flight_unknown",
        "multipart_resumable",
        "terminal_unacknowledged",
        "terminal_acknowledged",
        "blocked",
    )


def test_actions_are_exactly():
    assert ACTIONS == (
        "inspect",
        "publish",
        "reconcile",
        "verify_public",
        "resume",
        "abort",
        "ack",
    )


def test_authorized_actions_are_exactly():
    assert AUTHORIZED_ACTIONS == frozenset(
        {"publish", "reconcile", "verify_public", "resume", "abort"}
    )


def test_authorized_actions_is_subset_of_actions():
    assert AUTHORIZED_ACTIONS <= set(ACTIONS)


def test_every_state_has_a_registered_action_set():
    for state in RECOVERY_STATES:
        assert isinstance(allowed_actions(state, capabilities_ok=True), tuple)


def test_unregistered_state_is_rejected():
    with pytest.raises(ActionRegistryError):
        allowed_actions("mystery", capabilities_ok=True)


def test_retry_safe_rejects_unregistered_state():
    with pytest.raises(ActionRegistryError):
        retry_safe("mystery")


def test_all_returned_actions_are_registered():
    for state in RECOVERY_STATES:
        for capabilities_ok in (True, False):
            for action in allowed_actions(state, capabilities_ok=capabilities_ok):
                assert action in ACTIONS


def test_unregistered_action_is_never_authorized_or_allowed():
    assert "delete_object" not in ACTIONS
    assert "delete_object" not in AUTHORIZED_ACTIONS
    for state in RECOVERY_STATES:
        for capabilities_ok in (True, False):
            for action in allowed_actions(state, capabilities_ok=capabilities_ok):
                assert action in ACTIONS


def test_tables_cover_exactly_the_declared_states():
    assert tuple(_TABLE) == RECOVERY_STATES
    assert tuple(_RETRY_SAFE) == RECOVERY_STATES


def test_authorized_action_order_is_exactly():
    assert AUTHORIZED_ACTION_ORDER == (
        "publish",
        "reconcile",
        "verify_public",
        "resume",
        "abort",
    )


def test_inspect_is_always_allowed():
    for state in RECOVERY_STATES:
        for capabilities_ok in (True, False):
            assert "inspect" in allowed_actions(state, capabilities_ok=capabilities_ok)


def test_not_started_allowed_actions_exactly():
    assert allowed_actions("not_started", capabilities_ok=True) == ("inspect", "publish")


def test_known_not_applied_allowed_actions_exactly():
    assert allowed_actions("known_not_applied", capabilities_ok=True) == (
        "inspect",
        "publish",
    )


def test_in_flight_unknown_allows_read_only_reconcile_only():
    actions = allowed_actions("in_flight_unknown", capabilities_ok=True)
    assert actions == ("inspect", "reconcile")


def test_multipart_resumable_allowed_actions_exactly():
    actions = allowed_actions("multipart_resumable", capabilities_ok=True)
    assert actions == ("inspect", "resume", "abort")


def test_terminal_unacknowledged_allows_only_read_and_ack():
    actions = allowed_actions("terminal_unacknowledged", capabilities_ok=True)
    assert actions == ("inspect", "ack")


def test_terminal_acknowledged_is_read_only():
    assert allowed_actions("terminal_acknowledged", capabilities_ok=True) == ("inspect",)


def test_blocked_state_has_no_mutation_action():
    assert allowed_actions("blocked", capabilities_ok=True) == ("inspect",)


def test_missing_capabilities_removes_every_mutation_action_exactly():
    expected = {
        "not_started": ("inspect",),
        "known_not_applied": ("inspect",),
        "in_flight_unknown": ("inspect",),
        "multipart_resumable": ("inspect",),
        "terminal_unacknowledged": ("inspect", "ack"),
        "terminal_acknowledged": ("inspect",),
        "blocked": ("inspect",),
    }
    for state in RECOVERY_STATES:
        assert allowed_actions(state, capabilities_ok=False) == expected[state]


def test_retry_safe_is_false_for_unknown_in_flight():
    assert retry_safe("in_flight_unknown") is False
    assert retry_safe("not_started") is True
    assert retry_safe("known_not_applied") is True


def test_retry_safe_for_every_registered_state_is_a_bool():
    for state in RECOVERY_STATES:
        assert isinstance(retry_safe(state), bool)


def test_authorized_actions_exclude_inspect_and_ack():
    assert "inspect" not in AUTHORIZED_ACTIONS
    assert "ack" not in AUTHORIZED_ACTIONS
