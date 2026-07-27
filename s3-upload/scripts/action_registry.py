from __future__ import annotations

from typing import Dict, Tuple


class ActionRegistryError(ValueError):
    pass


RECOVERY_STATES: Tuple[str, ...] = (
    "not_started",
    "known_not_applied",
    "in_flight_unknown",
    "multipart_resumable",
    "terminal_unacknowledged",
    "terminal_acknowledged",
    "blocked",
)

ACTIONS: Tuple[str, ...] = (
    "inspect",
    "publish",
    "reconcile",
    "verify_public",
    "resume",
    "abort",
    "ack",
)

AUTHORIZED_ACTIONS: "frozenset[str]" = frozenset(
    {"publish", "reconcile", "verify_public", "resume", "abort"}
)

AUTHORIZED_ACTION_ORDER: Tuple[str, ...] = tuple(
    action for action in ACTIONS if action in AUTHORIZED_ACTIONS
)

NO_REMOTE_EFFECT_ACTIONS: "frozenset[str]" = frozenset({"inspect", "ack"})

_TABLE: Dict[str, Tuple[str, ...]] = {
    "not_started": ("inspect", "publish"),
    "known_not_applied": ("inspect", "publish"),
    "in_flight_unknown": ("inspect", "reconcile"),
    "multipart_resumable": ("inspect", "resume", "abort"),
    "terminal_unacknowledged": ("inspect", "ack"),
    "terminal_acknowledged": ("inspect",),
    "blocked": ("inspect",),
}

_RETRY_SAFE: Dict[str, bool] = {
    "not_started": True,
    "known_not_applied": True,
    "in_flight_unknown": False,
    "multipart_resumable": False,
    "terminal_unacknowledged": False,
    "terminal_acknowledged": False,
    "blocked": False,
}


def allowed_actions(state: str, *, capabilities_ok: bool) -> Tuple[str, ...]:
    if state not in RECOVERY_STATES:
        raise ActionRegistryError("unregistered recovery state")
    actions = _TABLE[state]
    if capabilities_ok:
        return actions
    return tuple(action for action in actions if action in NO_REMOTE_EFFECT_ACTIONS)


def retry_safe(state: str, *, capabilities_ok: bool = True) -> bool:
    if state not in RECOVERY_STATES:
        raise ActionRegistryError("unregistered recovery state")
    if not _RETRY_SAFE[state]:
        return False
    # A caller told retry_safe=True must have somewhere to retry to. Reading
    # only _RETRY_SAFE answered True for a state whose capability filter had
    # already removed every mutating action, so the caller was handed
    # "safe to retry" together with allowed_actions=["inspect"].
    return bool(
        set(allowed_actions(state, capabilities_ok=capabilities_ok))
        - NO_REMOTE_EFFECT_ACTIONS
    )
