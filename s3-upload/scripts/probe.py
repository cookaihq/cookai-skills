from __future__ import annotations

from typing import Any, Dict, Optional

from delivery_schema import envelope
from planning import derive_contract_key, registry_for_target
from resolver import ResolutionError, resolve_target
from safe_io import FileSecurityError
from target_contract import CONTRACT_VERSION, contract_hash, contract_snapshot
from v2_schema import SchemaError


def build_probe(*, cwd: str, config_home: str, environ: Dict[str, str],
                cli_target: Optional[str], cli_caller: Optional[str],
                use_local_key: bool, executable: str, state_root: str) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "contract_versions": [CONTRACT_VERSION],
        "executable": executable,
        "caller": cli_caller,
        "cwd": cwd,
        "state_root": state_root,
        "readiness": "installed_unconfigured",
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
    except (ResolutionError, SchemaError, FileSecurityError, FileNotFoundError) as exc:
        body["blocking_reason"] = type(exc).__name__
        return envelope("s3-upload.probe", body)

    key = derive_contract_key(resolved.target)
    snapshot = contract_snapshot(
        target_ref=resolved.ref,
        config_scope=resolved.ref.scope,
        project_root=cwd,
        target=resolved.target,
        contract_key=key,
        registry=registry_for_target(resolved.target, key),
    )
    body["target_ref"] = resolved.ref.text
    body["target_contract"] = snapshot
    body["target_contract_hash"] = contract_hash(snapshot)
    body["readiness"] = "ready" if resolved.credential_state == "available" else "installed_unconfigured"
    return envelope("s3-upload.probe", body)
