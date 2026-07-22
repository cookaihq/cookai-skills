from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union


REGISTRY_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")

BASELINE_DISABLED_OPERATIONS = (
    "GetObject",
    "PublicGetObject",
    "HeadObject",
    "ReservedMetadataRoundTrip",
    "ConditionalPutObject",
    "DeleteObjectCurrentKey",
    "ObserveDeleteCurrentKey",
    "DeleteObjectVersion",
    "ObserveDeleteVersion",
    "CreateMultipartUpload",
    "UploadPart",
    "ListParts",
    "CompleteMultipartUpload",
    "ConditionalCompleteMultipartUpload",
    "AbortMultipartUpload",
    "ObserveMultipartSession",
)


class CapabilityContractError(ValueError):
    pass


@dataclass(frozen=True)
class ContractKey:
    schema_version: int
    provider: str
    scheme: str
    endpoint_family: str
    region_class: str
    network_class: str
    addressing: str
    signing_profile: str
    payload_profile: str

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise CapabilityContractError("contract key schema_version must be 1")
        if self.scheme not in {"http", "https"}:
            raise CapabilityContractError("contract key scheme must be http or https")
        if self.addressing not in {"path", "virtual", "bucket-bound"}:
            raise CapabilityContractError("invalid contract key addressing")
        for name in (
            "provider",
            "endpoint_family",
            "region_class",
            "network_class",
            "signing_profile",
            "payload_profile",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not REGISTRY_IDENTIFIER.fullmatch(value):
                raise CapabilityContractError(f"invalid contract key {name}")

    def as_dict(self) -> Dict[str, Union[int, str]]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "scheme": self.scheme,
            "endpoint_family": self.endpoint_family,
            "region_class": self.region_class,
            "network_class": self.network_class,
            "addressing": self.addressing,
            "signing_profile": self.signing_profile,
            "payload_profile": self.payload_profile,
        }


@dataclass(frozen=True)
class Capability:
    operation: str
    state: str
    evidence_id: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9]{0,127}", self.operation
        ):
            raise CapabilityContractError("invalid capability operation")
        if self.state not in {"enabled", "test-only", "disabled", "unknown"}:
            raise CapabilityContractError("invalid capability state")
        if self.state == "unknown":
            if self.evidence_id is not None:
                raise CapabilityContractError("unknown capability cannot name evidence")
        elif not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise CapabilityContractError("capability evidence_id is required")

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }


class CapabilityRegistry:
    def __init__(
        self,
        entries: Iterable[Tuple[ContractKey, Sequence[Capability]]],
    ) -> None:
        contracts: Dict[ContractKey, Mapping[str, Capability]] = {}
        for key, capabilities in entries:
            if key in contracts:
                raise CapabilityContractError("duplicate capability contract")
            by_operation: Dict[str, Capability] = {}
            for capability in capabilities:
                if capability.operation in by_operation:
                    raise CapabilityContractError(
                        f"duplicate capability: {capability.operation}"
                    )
                by_operation[capability.operation] = capability
            contracts[key] = MappingProxyType(by_operation)
        self._contracts = MappingProxyType(contracts)

    def lookup(self, key: ContractKey, operation: str) -> Capability:
        contract = self._contracts.get(key)
        if contract is None or operation not in contract:
            return Capability(operation, "unknown", None)
        return contract[operation]


@dataclass(frozen=True)
class OperationShape:
    operation: str
    access_mode: Optional[str] = None
    upload_mode: Optional[str] = None
    collision: Optional[str] = None
    delete_scope: Optional[str] = None


@dataclass(frozen=True)
class LiveTestInterlock:
    enabled: bool
    target_ref: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise CapabilityContractError("live-test enabled must be boolean")
        if self.target_ref is not None and (
            not isinstance(self.target_ref, str) or not self.target_ref
        ):
            raise CapabilityContractError("invalid live-test target")


@dataclass(frozen=True)
class OperationPlan:
    executable: bool
    blocking_reasons: Tuple[str, ...]
    remote_operations: Tuple[str, ...]
    capabilities: Tuple[Capability, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "executable": self.executable,
            "blocking_reasons": list(self.blocking_reasons),
            "remote_operations": list(self.remote_operations),
            "capabilities": [item.as_dict() for item in self.capabilities],
        }


def plan_operation(
    shape: OperationShape,
    *,
    contract_key: ContractKey,
    registry: CapabilityRegistry,
    execution_mode: str = "normal",
    target_ref: Optional[str] = None,
    live_test_interlock: Optional[LiveTestInterlock] = None,
    allow_insecure_http: bool = False,
    credential_available: bool = True,
) -> OperationPlan:
    if execution_mode not in {"normal", "test-only"}:
        raise CapabilityContractError("invalid execution mode")
    if not isinstance(allow_insecure_http, bool):
        raise CapabilityContractError("allow_insecure_http must be boolean")
    if not isinstance(credential_available, bool):
        raise CapabilityContractError("credential_available must be boolean")
    if shape.operation == "upload":
        if shape.delete_scope is not None:
            raise CapabilityContractError("invalid operation shape")
        if shape.access_mode not in {"private", "public"}:
            raise CapabilityContractError("upload access_mode is required")
        if shape.upload_mode not in {
            "single-put", "multipart"
        } or shape.collision not in {"replace", "unique", "reject"}:
            raise CapabilityContractError("unsupported upload shape")
        if shape.upload_mode == "single-put":
            remote_operations = ("PutObject",)
            required = ["PutObject"]
            if shape.collision != "replace":
                required.append("ConditionalPutObject")
        else:
            remote_operations = (
                "CreateMultipartUpload",
                "UploadPart",
                "CompleteMultipartUpload",
            )
            required = list(remote_operations)
            if shape.collision != "replace":
                required.append("ConditionalCompleteMultipartUpload")
        if shape.access_mode == "private":
            required.append("PresignGetObject")
    elif shape.operation == "delete":
        if any(
            value is not None
            for value in (shape.access_mode, shape.upload_mode, shape.collision)
        ):
            raise CapabilityContractError("invalid operation shape")
        if shape.delete_scope == "current-key":
            required = ["DeleteObjectCurrentKey", "ObserveDeleteCurrentKey"]
        elif shape.delete_scope == "exact-version":
            required = ["DeleteObjectVersion", "ObserveDeleteVersion"]
        else:
            raise CapabilityContractError("delete_scope is required")
        remote_operations = (required[0],)
    else:
        raise CapabilityContractError("unsupported operation shape")
    capabilities = tuple(
        registry.lookup(contract_key, operation) for operation in required
    )
    blocking = []
    if not credential_available:
        blocking.append("credential_unavailable")
    if contract_key.scheme == "http" and not allow_insecure_http:
        blocking.append("insecure_http_opt_in_required")
    for capability in capabilities:
        if capability.state == "test-only":
            if execution_mode == "normal":
                if "capability_disabled" not in blocking:
                    blocking.append("capability_disabled")
            elif not (
                live_test_interlock is not None
                and live_test_interlock.enabled
                and target_ref is not None
                and live_test_interlock.target_ref == target_ref
            ):
                if "live_interlock_missing" not in blocking:
                    blocking.append("live_interlock_missing")
            continue
        if (
            capability.operation in {
                "DeleteObjectCurrentKey",
                "ObserveDeleteCurrentKey",
                "DeleteObjectVersion",
                "ObserveDeleteVersion",
            }
            and capability.state != "enabled"
        ):
            if "delete_capability_missing" not in blocking:
                blocking.append("delete_capability_missing")
            continue
        if (
            capability.operation in {
                "CreateMultipartUpload",
                "UploadPart",
                "CompleteMultipartUpload",
            }
            and capability.state != "enabled"
        ):
            if "multipart_capability_missing" not in blocking:
                blocking.append("multipart_capability_missing")
            continue
        if (
            capability.operation in {
                "ConditionalPutObject",
                "ConditionalCompleteMultipartUpload",
            }
            and capability.state != "enabled"
        ):
            if "collision_capability_missing" not in blocking:
                blocking.append("collision_capability_missing")
            continue
        if capability.state == "unknown" and "capability_unknown" not in blocking:
            blocking.append("capability_unknown")
        elif capability.state != "enabled" and "capability_disabled" not in blocking:
            blocking.append("capability_disabled")
    return OperationPlan(
        executable=not blocking,
        blocking_reasons=tuple(blocking),
        remote_operations=remote_operations,
        capabilities=capabilities,
    )


def _baseline_capabilities(evidence_prefix: str) -> Tuple[Capability, ...]:
    enabled = (
        Capability("PutObject", "enabled", evidence_prefix + "-put"),
        Capability("PresignGetObject", "enabled", evidence_prefix + "-presign"),
    )
    disabled = tuple(
        Capability(operation, "disabled", "v2-baseline-disabled")
        for operation in BASELINE_DISABLED_OPERATIONS
    )
    return enabled + disabled


def _preset_evidence_prefix(key: ContractKey) -> str:
    common = (
        key.scheme == "https"
        and key.network_class == "public"
        and key.signing_profile == "sigv4-s3"
        and key.payload_profile == "fixed-content-length"
        and not key.region_class.startswith("exact-")
    )
    if (
        common
        and key.provider == "aws-s3"
        and key.endpoint_family == "aws-public"
        and key.addressing == "virtual"
    ):
        return "v1-aws"
    if (
        common
        and key.provider == "cloudflare-r2"
        and key.endpoint_family == "cloudflare-r2-public"
        and key.region_class == "auto"
        and key.addressing == "path"
    ):
        return "v1-r2"
    raise CapabilityContractError("contract is not an approved v2 baseline preset")


def _validate_asserted_custom(key: ContractKey) -> None:
    exact = re.compile(r"exact-[0-9a-f]{64}\Z")
    if not (
        key.provider == "custom"
        and exact.fullmatch(key.endpoint_family)
        and exact.fullmatch(key.region_class)
        and key.addressing in {"path", "virtual"}
        and key.signing_profile == "sigv4-s3"
        and key.payload_profile == "fixed-content-length"
    ):
        raise CapabilityContractError("invalid asserted custom baseline contract")


def build_v2_baseline_registry(
    *,
    preset_contracts: Sequence[ContractKey] = (),
    asserted_custom_contracts: Sequence[ContractKey] = (),
) -> CapabilityRegistry:
    entries = []
    for key in preset_contracts:
        entries.append((key, _baseline_capabilities(_preset_evidence_prefix(key))))
    for key in asserted_custom_contracts:
        _validate_asserted_custom(key)
        entries.append((key, _baseline_capabilities("v1-custom")))
    return CapabilityRegistry(entries)
