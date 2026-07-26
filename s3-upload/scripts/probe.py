from __future__ import annotations

import os
from typing import Any, Dict, Optional

from capabilities import CapabilityContractError
from delivery_schema import envelope
from planning import PlanError, derive_contract_key, registry_for_target
from resolver import CredentialExpiringError, ResolutionError, resolve_target
from target_contract import CONTRACT_VERSION, contract_hash, contract_snapshot

READINESS = ("ready", "installed_unconfigured")
BLOCKING_REASONS = (
    "target_unresolved", "provider_contract_mismatch", "credential_expiring", "internal_error",
)
READY, INSTALLED_UNCONFIGURED = READINESS
TARGET_UNRESOLVED, PROVIDER_CONTRACT_MISMATCH, CREDENTIAL_EXPIRING, INTERNAL_ERROR = BLOCKING_REASONS


def _canonical_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def build_probe(*, cwd: str, config_home: str, environ: Dict[str, str],
                cli_target: Optional[str], cli_caller: Optional[str],
                use_local_key: bool, executable_path: str, state_root: str) -> Dict[str, Any]:
    cwd = os.path.abspath(cwd)
    body: Dict[str, Any] = {
        "contract_versions": [CONTRACT_VERSION],
        "executable_path": _canonical_text(executable_path),
        "caller": _canonical_text(cli_caller),
        "cwd": _canonical_text(cwd),
        "state_root": _canonical_text(state_root),
        "readiness": INSTALLED_UNCONFIGURED,
        "target_ref": None,
        "target_contract": None,
        "target_contract_hash": None,
        "blocking_reason": None,
    }
    snapshot: Optional[Dict[str, Any]] = None
    contract_hash_value: Optional[str] = None
    try:
        try:
            resolved = resolve_target(
                cwd=cwd, config_home=config_home, environ=environ, cli_target=cli_target,
                cli_caller=cli_caller, use_local_key=use_local_key, allow_candidates=True,
            )
        except CredentialExpiringError as exc:
            resolved = exc.resolved
            body["blocking_reason"] = CREDENTIAL_EXPIRING
        body["target_ref"] = resolved.ref.text
        key = derive_contract_key(resolved.target)
        snapshot = contract_snapshot(
            target_ref=resolved.ref,
            config_scope=resolved.ref.scope,
            project_root=_canonical_text(cwd),
            target=resolved.target,
            contract_key=key,
            registry=registry_for_target(resolved.target, key),
        )
        contract_hash_value = contract_hash(snapshot)
    except ResolutionError:
        body["blocking_reason"] = TARGET_UNRESOLVED
        snapshot = None
        contract_hash_value = None
    except (PlanError, CapabilityContractError):
        body["blocking_reason"] = PROVIDER_CONTRACT_MISMATCH
        snapshot = None
        contract_hash_value = None
    except Exception:
        body["blocking_reason"] = INTERNAL_ERROR
        snapshot = None
        contract_hash_value = None
    else:
        body["readiness"] = READY if resolved.credential_state == "available" else INSTALLED_UNCONFIGURED
    body["target_contract"] = snapshot
    body["target_contract_hash"] = contract_hash_value
    return envelope("s3-upload.probe", body)
