from __future__ import annotations

import os
from typing import Any, Dict, Optional

from capabilities import CapabilityContractError
from delivery_schema import envelope
from planning import PlanError, derive_contract_key, registry_for_target
from resolver import CredentialExpiringError, ResolutionError, resolve_target
from target_contract import CONTRACT_VERSION, contract_hash, contract_snapshot

READINESS = ("ready", "installed_unconfigured")
BLOCKING_REASONS = ("target_unresolved", "provider_contract_mismatch", "credential_expiring")
READY, INSTALLED_UNCONFIGURED = READINESS
TARGET_UNRESOLVED, PROVIDER_CONTRACT_MISMATCH, CREDENTIAL_EXPIRING = BLOCKING_REASONS


def build_probe(*, cwd: str, config_home: str, environ: Dict[str, str],
                cli_target: Optional[str], cli_caller: Optional[str],
                use_local_key: bool, executable: str, state_root: str) -> Dict[str, Any]:
    cwd = os.path.abspath(cwd)
    body: Dict[str, Any] = {
        "contract_versions": [CONTRACT_VERSION],
        "executable": executable,
        "caller": cli_caller,
        "cwd": cwd,
        "state_root": state_root,
        "readiness": INSTALLED_UNCONFIGURED,
        "target_ref": None,
        "target_contract": None,
        "target_contract_hash": None,
        "blocking_reason": None,
    }
    try:
        resolved = resolve_target(
            cwd=cwd, config_home=config_home, environ=environ, cli_target=cli_target,
            cli_caller=cli_caller, use_local_key=use_local_key, allow_candidates=True,
        )
        key = derive_contract_key(resolved.target)
        snapshot = contract_snapshot(
            target_ref=resolved.ref,
            config_scope=resolved.ref.scope,
            project_root=cwd,
            target=resolved.target,
            contract_key=key,
            registry=registry_for_target(resolved.target, key),
        )
    except CredentialExpiringError:
        body["blocking_reason"] = CREDENTIAL_EXPIRING
        return envelope("s3-upload.probe", body)
    except ResolutionError:
        body["blocking_reason"] = TARGET_UNRESOLVED
        return envelope("s3-upload.probe", body)
    except (PlanError, CapabilityContractError):
        body["blocking_reason"] = PROVIDER_CONTRACT_MISMATCH
        return envelope("s3-upload.probe", body)

    body["target_ref"] = resolved.ref.text
    body["target_contract"] = snapshot
    body["target_contract_hash"] = contract_hash(snapshot)
    body["readiness"] = READY if resolved.credential_state == "available" else INSTALLED_UNCONFIGURED
    return envelope("s3-upload.probe", body)
