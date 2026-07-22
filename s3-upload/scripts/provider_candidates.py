from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence, Tuple
from urllib.parse import urlsplit

from capabilities import Capability, CapabilityRegistry, ContractKey
from config import Connection
from s3 import SignedRequest, build_signed_request


OSS_UNSIGNED_FIXED_LENGTH_PROFILE = "oss-unsigned-fixed-length"
OSS_ORDINARY_HASH_PROFILE = "ordinary-payload-sha256-hypothesis"
COS_PAYLOAD_PROFILE = "ordinary-payload-sha256-hypothesis"

CANDIDATE_OPERATIONS = (
    "PutObject",
    "HeadObject",
    "ConditionalPutObject",
    "GetObject",
    "PublicGetObject",
    "PresignGetObject",
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
    "ReservedMetadataRoundTrip",
    "ResponseParsing",
    "Reconciliation",
)

_REGION_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_COS_BUCKET_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?-[1-9][0-9]{4,19}\Z"
)


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderCandidate:
    provider: str
    region: str
    bucket: str
    service_endpoint: str
    object_endpoint: str
    addressing: str
    contract_key: ContractKey
    operation_evidence: Tuple[Tuple[str, str], ...]
    normal_mode: str
    remote_evidence: str
    source_manifest_version: int
    payload_evidence: str
    signature_format_evidence: str
    canonical_request_evidence: str


def _region(value: str) -> str:
    if not isinstance(value, str) or not _REGION_RE.fullmatch(value):
        raise CandidateError("invalid provider region")
    return value


def _bucket(value: str) -> str:
    if not isinstance(value, str) or not _DNS_LABEL_RE.fullmatch(value):
        raise CandidateError("bucket must be one canonical virtual-host DNS label")
    return value


def _endpoint(value: str) -> Tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise CandidateError("candidate endpoint is required")
    parts = urlsplit(value)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError as exc:
        raise CandidateError("candidate endpoint has an invalid port") from exc
    if (
        parts.scheme != "https"
        or parts.netloc != host
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or "%" in host
        or host.endswith(".")
        or ".." in host
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in host.split("."))
    ):
        raise CandidateError("candidate endpoint must be canonical HTTPS DNS")
    return value.rstrip("/"), host


def _operation_evidence(provider: str, family: str) -> Tuple[Tuple[str, str], ...]:
    def slug(operation: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "-", operation).lower()

    return tuple(
        (operation, f"{provider}-hypothesis-{family}-{slug(operation)}")
        for operation in CANDIDATE_OPERATIONS
    )


def aliyun_oss_candidate(
    *,
    region: str,
    bucket: str,
    endpoint: Optional[str] = None,
    network_class: str = "public",
    payload_profile: str = OSS_UNSIGNED_FIXED_LENGTH_PROFILE,
) -> ProviderCandidate:
    region = _region(region)
    bucket = _bucket(bucket)
    if payload_profile not in {
        OSS_UNSIGNED_FIXED_LENGTH_PROFILE,
        OSS_ORDINARY_HASH_PROFILE,
    }:
        raise CandidateError("unsupported OSS candidate payload profile")
    if network_class == "public":
        expected = f"https://s3.oss-{region}.aliyuncs.com"
        family = (
            "aliyun-oss-mainland-default-public"
            if region.startswith("cn-")
            else "aliyun-oss-default-public"
        )
        addressing = "virtual"
        operation_evidence = _operation_evidence("aliyun-oss", "public")
    elif network_class == "eligible-vpc":
        expected = f"https://s3.oss-{region}-internal.aliyuncs.com"
        family = "aliyun-oss-internal"
        addressing = "virtual"
        operation_evidence = _operation_evidence("aliyun-oss", "eligible-vpc")
    elif network_class == "bucket-bound-cname":
        if endpoint is None:
            raise CandidateError("OSS CNAME candidate requires an exact endpoint")
        expected, _ = _endpoint(endpoint)
        family = "aliyun-oss-cname"
        addressing = "bucket-bound"
        operation_evidence = ()
    else:
        raise CandidateError("invalid OSS candidate network class")
    actual, host = _endpoint(expected if endpoint is None else endpoint)
    if actual != expected:
        raise CandidateError("endpoint does not match the exact OSS candidate contract")
    object_endpoint = actual if addressing == "bucket-bound" else f"https://{bucket}.{host}"
    contract = ContractKey(
        schema_version=1,
        provider="aliyun-oss",
        scheme="https",
        endpoint_family=family,
        region_class=region,
        network_class=network_class,
        addressing=addressing,
        signing_profile="sigv4-s3",
        payload_profile=payload_profile,
    )
    return ProviderCandidate(
        provider="aliyun-oss",
        region=region,
        bucket=bucket,
        service_endpoint=actual,
        object_endpoint=object_endpoint,
        addressing=addressing,
        contract_key=contract,
        operation_evidence=operation_evidence,
        normal_mode="disabled",
        remote_evidence="not-tested",
        source_manifest_version=1,
        payload_evidence=(
            "docs-derived"
            if payload_profile == OSS_UNSIGNED_FIXED_LENGTH_PROFILE
            else "hypothesis"
        ),
        signature_format_evidence="docs-derived",
        canonical_request_evidence="hypothesis",
    )


def tencent_cos_candidate(
    *,
    region: str,
    bucket: str,
    endpoint: Optional[str] = None,
    addressing: str = "virtual",
) -> ProviderCandidate:
    region = _region(region)
    if (
        not isinstance(bucket, str)
        or not _COS_BUCKET_RE.fullmatch(bucket)
        or not _DNS_LABEL_RE.fullmatch(bucket)
    ):
        raise CandidateError("COS bucket must be the complete BucketName-APPID identity")
    if addressing != "virtual":
        raise CandidateError("COS candidate does not generate path-style requests")
    expected = f"https://cos.{region}.myqcloud.com"
    actual, host = _endpoint(expected if endpoint is None else endpoint)
    if actual != expected:
        raise CandidateError("endpoint does not match the exact COS service contract")
    contract = ContractKey(
        schema_version=1,
        provider="tencent-cos",
        scheme="https",
        endpoint_family="tencent-cos-third-party-s3-public",
        region_class=region,
        network_class="public",
        addressing="virtual",
        signing_profile="aws-v4-format-hypothesis",
        payload_profile=COS_PAYLOAD_PROFILE,
    )
    return ProviderCandidate(
        provider="tencent-cos",
        region=region,
        bucket=bucket,
        service_endpoint=actual,
        object_endpoint=f"https://{bucket}.{host}",
        addressing="virtual",
        contract_key=contract,
        operation_evidence=_operation_evidence("tencent-cos", "public"),
        normal_mode="disabled",
        remote_evidence="not-tested",
        source_manifest_version=1,
        payload_evidence="hypothesis",
        signature_format_evidence="docs-derived",
        canonical_request_evidence="hypothesis",
    )


def build_candidate_registry(
    candidates: Sequence[ProviderCandidate],
) -> CapabilityRegistry:
    return CapabilityRegistry(
        (
            candidate.contract_key,
            tuple(
                Capability(operation, "test-only", evidence_id)
                for operation, evidence_id in candidate.operation_evidence
            ),
        )
        for candidate in candidates
        if candidate.operation_evidence
    )


def _validate_connection(candidate: ProviderCandidate, connection: Connection) -> None:
    if (
        connection.provider != candidate.provider
        or connection.region != candidate.region
        or connection.bucket != candidate.bucket
        or connection.endpoint != candidate.service_endpoint
        or connection.addressing != candidate.addressing
    ):
        raise CandidateError("connection does not match the exact candidate contract")


def build_candidate_request(
    candidate: ProviderCandidate,
    connection: Connection,
    *,
    method: str,
    key: str,
    query: Sequence[Tuple[str, str]] = (),
    body: bytes = b"",
    headers: Sequence[Tuple[str, str]] = (),
    now: Optional[datetime] = None,
) -> SignedRequest:
    _validate_connection(candidate, connection)
    if not isinstance(body, bytes):
        raise CandidateError("candidate request body must be bytes")
    checked_headers = []
    for name, value in headers:
        lowered = name.lower()
        if lowered == "transfer-encoding" or (
            lowered == "content-encoding" and "aws-chunked" in value.lower()
        ) or (
            lowered in {"x-amz-content-sha256", "x-oss-content-sha256"}
            and value.startswith("STREAMING-")
        ):
            raise CandidateError("chunked or streaming candidate requests are forbidden")
        checked_headers.append((name, value))
    if candidate.contract_key.payload_profile == OSS_UNSIGNED_FIXED_LENGTH_PROFILE:
        request = build_signed_request(
            connection,
            method=method,
            key=key,
            query=query,
            body=body,
            headers=tuple(checked_headers),
            payload_hash="UNSIGNED-PAYLOAD",
            now=now,
        )
        headers_with_oss_requirement = dict(request.headers)
        headers_with_oss_requirement["x-oss-content-sha256"] = "UNSIGNED-PAYLOAD"
        return SignedRequest(
            method=request.method,
            url=request.url,
            headers=headers_with_oss_requirement,
            body=request.body,
            canonical_request=request.canonical_request,
        )
    payload_hash = hashlib.sha256(body).hexdigest()
    return build_signed_request(
        connection,
        method=method,
        key=key,
        query=query,
        body=body,
        headers=tuple(checked_headers),
        payload_hash=payload_hash,
        now=now,
    )


def build_candidate_put_request(
    candidate: ProviderCandidate,
    connection: Connection,
    *,
    key: str,
    body: bytes,
    content_type: str,
    extra_headers: Sequence[Tuple[str, str]] = (),
    now: Optional[datetime] = None,
) -> SignedRequest:
    headers = [
        ("content-length", str(len(body))),
        ("content-type", content_type),
    ]
    for name, value in extra_headers:
        if name.lower() in {"content-length", "content-type"}:
            raise CandidateError("candidate request header duplicates a fixed field")
        headers.append((name, value))
    return build_candidate_request(
        candidate,
        connection,
        method="PUT",
        key=key,
        body=body,
        headers=tuple(headers),
        now=now,
    )
