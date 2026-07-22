from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from artifacts import preflight_reference_output
from capabilities import (
    Capability, CapabilityRegistry, ContractKey, LiveTestInterlock, OperationShape,
    build_v2_baseline_registry, plan_operation,
)
from headers import resolve_upload_headers
from provider_candidates import (
    CandidateError, ProviderCandidate, aliyun_oss_candidate,
    build_candidate_registry, tencent_cos_candidate,
)
from resolver import ResolvedTarget
from strict_json import canonicalize
from source_file import SourceError, VerifiedSource
from v2_schema import UploadTarget, validate_object_key


class PlanError(ValueError):
    pass


class LocalFileError(PlanError):
    pass


@dataclass(frozen=True)
class UploadDryRun:
    plan: Dict[str, Any]
    source: VerifiedSource

    @property
    def executable(self) -> bool:
        return bool(self.plan["executable"])

    def close(self) -> None:
        self.source.close()


@dataclass(frozen=True)
class DeleteDryRun:
    plan: Dict[str, Any]

    @property
    def executable(self) -> bool:
        return bool(self.plan["executable"])


def _exact_hash(value: Dict[str, Any]) -> str:
    return "exact-" + hashlib.sha256(canonicalize(value)).hexdigest()


def _reviewed_region_class(target: UploadTarget) -> str:
    if target.provider == "aws-s3" and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", target.region):
        return target.region
    if target.provider == "cloudflare-r2" and target.region == "auto":
        return "auto"
    return _exact_hash({"region": target.region})


def _r2_preset_authority(target: UploadTarget) -> bool:
    parts = urlsplit(target.endpoint)
    host = parts.hostname or ""
    return bool(
        parts.scheme == "https"
        and parts.port is None
        and target.region == "auto"
        and target.addressing == "path"
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}\.r2\.cloudflarestorage\.com", host)
    )


def derive_contract_key(target: UploadTarget) -> ContractKey:
    candidate = provider_candidate_for_target(target)
    if candidate is not None:
        return candidate.contract_key
    parts = urlsplit(target.endpoint)
    host = parts.hostname or ""
    port = parts.port
    if (
        target.provider == "aws-s3"
        and not target.endpoint_explicit
        and not target.addressing_explicit
        and target.addressing == "virtual"
    ):
        endpoint_family = "aws-public"
        region_class = _reviewed_region_class(target)
    elif target.provider == "cloudflare-r2" and _r2_preset_authority(target):
        endpoint_family = "cloudflare-r2-public"
        region_class = "auto"
    else:
        endpoint_family = _exact_hash({"host": host, "port": port})
        region_class = _exact_hash({"region": target.region})
    return ContractKey(
        schema_version=1,
        provider=target.provider,
        scheme=parts.scheme,
        endpoint_family=endpoint_family,
        region_class=region_class,
        network_class="public",
        addressing=target.addressing,
        signing_profile="sigv4-s3",
        payload_profile="fixed-content-length",
    )


def registry_for_target(target: UploadTarget, key: ContractKey) -> CapabilityRegistry:
    candidate = provider_candidate_for_target(target)
    if candidate is not None:
        if candidate.contract_key != key:
            raise PlanError("candidate Contract Key does not match the resolved Target")
        return build_candidate_registry((candidate,))
    if key.provider == "aws-s3" and key.endpoint_family == "aws-public":
        return build_v2_baseline_registry(preset_contracts=(key,))
    if key.provider == "cloudflare-r2" and key.endpoint_family == "cloudflare-r2-public":
        return build_v2_baseline_registry(preset_contracts=(key,))
    if key.provider == "custom":
        if is_reserved_provider_endpoint(target):
            return CapabilityRegistry(
                (
                    (
                        key,
                        (
                            Capability(
                                "PutObject",
                                "disabled",
                                "reserved-provider-endpoint",
                            ),
                            Capability(
                                "PresignGetObject",
                                "disabled",
                                "reserved-provider-endpoint",
                            ),
                        ),
                    ),
                )
            )
        return build_v2_baseline_registry(asserted_custom_contracts=(key,))
    return CapabilityRegistry(())


def provider_candidate_for_target(target: UploadTarget) -> Optional[ProviderCandidate]:
    try:
        if target.provider == "aliyun-oss":
            candidate = aliyun_oss_candidate(
                region=target.region,
                bucket=target.bucket,
                endpoint=target.endpoint,
            )
        elif target.provider == "tencent-cos":
            candidate = tencent_cos_candidate(
                region=target.region,
                bucket=target.bucket,
                endpoint=target.endpoint,
                addressing=target.addressing,
            )
        else:
            return None
    except CandidateError as exc:
        raise PlanError(str(exc)) from exc
    if target.addressing != candidate.addressing:
        raise PlanError("addressing does not match the provider preset contract")
    if not (target.endpoint_explicit or target.addressing_explicit):
        return candidate
    original = candidate.contract_key
    endpoint = urlsplit(target.endpoint)
    return replace(
        candidate,
        contract_key=ContractKey(
            schema_version=original.schema_version,
            provider=original.provider,
            scheme=original.scheme,
            endpoint_family=_exact_hash(
                {"host": endpoint.hostname or "", "port": endpoint.port}
            ),
            region_class=_exact_hash({"region": target.region}),
            network_class="explicit",
            addressing=original.addressing,
            signing_profile=original.signing_profile,
            payload_profile=original.payload_profile,
        ),
        normal_mode="test-only",
    )


def is_reserved_provider_endpoint(target: UploadTarget) -> bool:
    if target.provider != "custom":
        return False
    host = urlsplit(target.endpoint).hostname or ""
    provider_hosts = (
        r"(?:[a-z0-9][a-z0-9.-]*\.)?s3\.oss-[a-z0-9-]+"
        r"(?:-internal)?\.aliyuncs\.com",
        r"(?:[a-z0-9][a-z0-9.-]*\.)?oss-[a-z0-9-]+"
        r"(?:-internal)?\.aliyuncs\.com",
        r"(?:[a-z0-9][a-z0-9.-]*\.)?cos\.[a-z0-9-]+\.myqcloud\.com",
    )
    return any(re.fullmatch(pattern, host) for pattern in provider_hosts)


def _source(path: str, maximum: int) -> VerifiedSource:
    try:
        return VerifiedSource.open(path, soft_max_bytes=maximum)
    except SourceError as exc:
        raise LocalFileError("cannot read local source") from exc


def _object_key(target: UploadTarget, source_path: str, explicit: Optional[str]) -> str:
    value = explicit if explicit is not None else target.prefix + os.path.basename(source_path)
    value = validate_object_key(value)
    if (target.access.mode == "public" or target.retention.mode == "expire") and not value.startswith(target.prefix):
        raise PlanError("Object Key is outside the Target policy prefix")
    return value


def build_upload_dry_run(*, resolved: ResolvedTarget, file_path: str,
                         explicit_key: Optional[str], content_type: Optional[str],
                         cache_control: Optional[str], content_disposition: Optional[str],
                         presign_expires: Optional[int], reference_out: Optional[str],
                         project_root: str, config_home: str,
                         allow_insecure_http: bool, now: Optional[datetime] = None,
                         execution_mode: str = "normal",
                         live_test_interlock: Optional[LiveTestInterlock] = None) -> UploadDryRun:
    target = resolved.target
    source = _source(file_path, target.limits.soft_max_bytes)
    try:
        snapshot = source.snapshot
        key = _object_key(target, snapshot.path, explicit_key)
        headers = resolve_upload_headers(
            os.path.basename(snapshot.path),
            target.object_headers.cache_control,
            target.object_headers.content_disposition,
            content_type,
            cache_control,
            content_disposition,
        )
        requested = target.access.presign_expires_seconds
        if presign_expires is not None:
            if target.access.mode != "private" or not 1 <= presign_expires <= 604800:
                raise PlanError("--presign-expires requires private access and a value from 1 to 604800")
            requested = presign_expires
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        effective = resolved.presign_effective_seconds(requested, moment)
        threshold = target.limits.multipart_threshold_bytes
        upload_mode = "multipart" if threshold is not None and snapshot.size >= threshold else "single-put"
        contract_key = derive_contract_key(target)
        registry = registry_for_target(target, contract_key)
        capability_plan = plan_operation(
            OperationShape(
                operation="upload",
                access_mode=target.access.mode,
                upload_mode=upload_mode,
                collision=target.collision,
            ),
            contract_key=contract_key,
            registry=registry,
            execution_mode=execution_mode,
            target_ref=resolved.ref.text,
            live_test_interlock=live_test_interlock,
            allow_insecure_http=allow_insecure_http,
            credential_available=resolved.credential is not None,
        )
        blocking = list(capability_plan.blocking_reasons)
        if execution_mode == "test-only" and not target.setup.integration_test:
            if "live_interlock_missing" not in blocking:
                blocking.insert(0, "live_interlock_missing")
        reference_plan = None
        if reference_out is not None:
            reference_snapshot = preflight_reference_output(
                reference_out,
                project_root=project_root,
                config_home=config_home,
                source_identity=(snapshot.device, snapshot.inode),
            )
            reference_plan = {
                "path": reference_snapshot.value["path"],
                "state": reference_snapshot.value["final_snapshot"]["state"],
            }
        if target.collision == "unique" and upload_mode == "single-put":
            collision_attempts = target.retry.collision_max_attempts
        else:
            collision_attempts = 1
        plan = {
            "executable": not blocking,
            "blocking_reasons": blocking,
            "target_ref": resolved.ref.text,
            "target_source": resolved.source,
            "target_fingerprint": resolved.target_fingerprint,
            "provider": target.provider,
            "endpoint": target.endpoint,
            "addressing": target.addressing,
            "region": target.region,
            "bucket": target.bucket,
            "prefix": target.prefix,
            "object_key": key,
            "source": {"path": snapshot.path, "size": snapshot.size},
            "contract_key": contract_key.as_dict(),
            "remote_operations": list(capability_plan.remote_operations),
            "capabilities": [entry.as_dict() for entry in capability_plan.capabilities],
            "upload_mode": upload_mode,
            "collision": {"policy": target.collision, "max_attempts": collision_attempts},
            "headers": headers,
            "access": {
                "mode": target.access.mode,
                "url_kind": "presigned" if target.access.mode == "private" else "public",
                "presign_expires_seconds": requested,
                "presign_effective_seconds": effective,
                "public_base_url": target.access.public_base_url,
            },
            "retention": target.retention.result(),
            "delete_scope": None,
            "reference_out": reference_plan,
        }
        return UploadDryRun(plan, source)
    except Exception:
        source.close()
        raise


def build_delete_dry_run(*, resolved: ResolvedTarget, reference: Dict[str, Any],
                         allow_insecure_http: bool,
                         now: Optional[datetime] = None,
                         execution_mode: str = "normal",
                         live_test_interlock: Optional[LiveTestInterlock] = None) -> DeleteDryRun:
    if reference["target_fingerprint"] != resolved.target_fingerprint:
        raise PlanError("Object Reference Target fingerprint does not match the selected Target")
    target = resolved.target
    scope = "exact-version" if reference["location"]["version_id"] is not None else "current-key"
    contract_key = derive_contract_key(target)
    registry = registry_for_target(target, contract_key)
    capability_plan = plan_operation(
        OperationShape(operation="delete", delete_scope=scope),
        contract_key=contract_key,
        registry=registry,
        execution_mode=execution_mode,
        target_ref=resolved.ref.text,
        live_test_interlock=live_test_interlock,
        allow_insecure_http=allow_insecure_http,
        credential_available=resolved.credential is not None,
    )
    blocking = list(capability_plan.blocking_reasons)
    if execution_mode == "test-only" and not target.setup.integration_test:
        if "live_interlock_missing" not in blocking:
            blocking.insert(0, "live_interlock_missing")
    access = reference["access"]
    requested = access["presign_expires_seconds"]
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    effective = resolved.presign_effective_seconds(requested, moment) if requested is not None else None
    plan = {
        "executable": not blocking,
        "blocking_reasons": blocking,
        "target_ref": resolved.ref.text,
        "target_source": resolved.source,
        "target_fingerprint": resolved.target_fingerprint,
        "provider": reference["location"]["provider"],
        "endpoint": reference["location"]["endpoint"],
        "addressing": reference["location"]["addressing"],
        "region": reference["location"]["region"],
        "bucket": reference["location"]["bucket"],
        "prefix": target.prefix,
        "object_key": reference["location"]["key"],
        "source": None,
        "contract_key": contract_key.as_dict(),
        "remote_operations": list(capability_plan.remote_operations),
        "capabilities": [entry.as_dict() for entry in capability_plan.capabilities],
        "upload_mode": None,
        "collision": None,
        "headers": None,
        "access": {
            "mode": access["mode"],
            "url_kind": "presigned" if access["mode"] == "private" else "public",
            "presign_expires_seconds": requested,
            "presign_effective_seconds": effective,
            "public_base_url": access["public_base_url"],
        },
        "retention": reference["retention"],
        "delete_scope": scope,
        "reference_out": None,
    }
    return DeleteDryRun(plan)
