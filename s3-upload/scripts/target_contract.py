from __future__ import annotations

import hashlib
from typing import Any, Dict

from capabilities import BASELINE_DISABLED_OPERATIONS, CapabilityRegistry, ContractKey
from strict_json import canonicalize
from v2_schema import ScopedReference, UploadTarget


CONTRACT_VERSION = 1

CAPABILITY_OPERATIONS = tuple(sorted(BASELINE_DISABLED_OPERATIONS))

CREDENTIAL_BINDING_DOMAIN = "s3-upload/credential-binding/v1"
CONTRACT_DOMAIN = "s3-upload/target-contract/v1"


def _digest(domain: str, value: Any) -> str:
    payload = canonicalize({"domain": domain, "value": value})
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def credential_binding_hash(reference: ScopedReference) -> str:
    return _digest(CREDENTIAL_BINDING_DOMAIN, reference.text)


def contract_snapshot(*, target_ref: ScopedReference, config_scope: str, project_root: str,
                      target: UploadTarget, contract_key: ContractKey,
                      registry: CapabilityRegistry) -> Dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "target_ref": target_ref.text,
        "config_scope": config_scope,
        "project_root": project_root,
        "credential_binding_hash": credential_binding_hash(target.credential),
        "provider": target.provider,
        "endpoint": target.endpoint,
        "addressing": target.addressing,
        "region": target.region,
        "bucket": target.bucket,
        "prefix": target.prefix,
        "access": {
            "mode": target.access.mode,
            "public_base_url": target.access.public_base_url,
            "presign_expires_seconds": target.access.presign_expires_seconds,
        },
        "retention": {"mode": target.retention.mode, "days": target.retention.days},
        "collision": target.collision,
        "object_headers": {
            "cache_control": target.object_headers.cache_control,
            "content_disposition": target.object_headers.content_disposition,
        },
        "limits": {
            "soft_max_bytes": target.limits.soft_max_bytes,
            "multipart_threshold_bytes": target.limits.multipart_threshold_bytes,
            "part_size_bytes": target.limits.part_size_bytes,
        },
        "retry": {
            "part_max_attempts": target.retry.part_max_attempts,
            "collision_max_attempts": target.retry.collision_max_attempts,
        },
        "setup": {
            "exclusive_prefix": target.setup.exclusive_prefix,
            "integration_test": target.setup.integration_test,
            "cors": target.setup.cors,
        },
        "contract_key": contract_key.as_dict(),
        "capabilities": [
            registry.lookup(contract_key, operation).as_dict()
            for operation in CAPABILITY_OPERATIONS
        ],
    }


def contract_hash(snapshot: Dict[str, Any]) -> str:
    return _digest(CONTRACT_DOMAIN, snapshot)
