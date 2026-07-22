from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from capabilities import LiveTestInterlock, OperationShape, plan_operation
from config import Connection
from provider_candidates import (
    CANDIDATE_OPERATIONS,
    EXPERIMENTAL_BASELINE_OPERATIONS,
    OSS_ORDINARY_HASH_PROFILE,
    OSS_UNSIGNED_FIXED_LENGTH_PROFILE,
    CandidateError,
    aliyun_oss_candidate,
    build_candidate_request,
    build_candidate_put_request,
    build_candidate_registry,
    tencent_cos_candidate,
)


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def oss_connection(candidate):
    return Connection(
        access_key_id="ALIYUNKEY1234",
        secret_access_key="aliyun-secret-value",
        bucket=candidate.bucket,
        endpoint=candidate.service_endpoint,
        region=candidate.region,
        provider=candidate.provider,
        addressing=candidate.addressing,
    )


def cos_connection(candidate):
    return Connection(
        access_key_id="TENCENTKEY1234",
        secret_access_key="tencent-secret-value",
        bucket=candidate.bucket,
        endpoint=candidate.service_endpoint,
        region=candidate.region,
        provider=candidate.provider,
        addressing=candidate.addressing,
    )


def test_oss_candidate_uses_exact_official_service_endpoint_and_virtual_host():
    candidate = aliyun_oss_candidate(
        region="cn-hangzhou",
        bucket="candidate-bucket",
    )

    assert candidate.service_endpoint == "https://s3.oss-cn-hangzhou.aliyuncs.com"
    assert candidate.object_endpoint == (
        "https://candidate-bucket.s3.oss-cn-hangzhou.aliyuncs.com"
    )
    assert candidate.addressing == "virtual"
    assert candidate.contract_key.as_dict() == {
        "schema_version": 1,
        "provider": "aliyun-oss",
        "scheme": "https",
        "endpoint_family": "aliyun-oss-mainland-default-public",
        "region_class": "cn-hangzhou",
        "network_class": "public",
        "addressing": "virtual",
        "signing_profile": "sigv4-s3",
        "payload_profile": "oss-unsigned-fixed-length",
    }
    assert candidate.normal_mode == "experimental"
    assert candidate.remote_evidence == "not-tested"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://s3.oss-cn-hangzhou.aliyuncs.com.",
        "https://s3%2Eoss-cn-hangzhou.aliyuncs.com",
        "https://s3..oss-cn-hangzhou.aliyuncs.com",
        "https://s3.oss-cn-shanghai.aliyuncs.com",
    ],
)
def test_oss_candidate_rejects_noncanonical_or_wrong_region_endpoint(endpoint):
    with pytest.raises(CandidateError):
        aliyun_oss_candidate(
            region="cn-hangzhou",
            bucket="candidate-bucket",
            endpoint=endpoint,
        )


def test_oss_internal_and_cname_are_separate_noninheriting_contracts():
    internal = aliyun_oss_candidate(
        region="cn-hangzhou",
        bucket="candidate-bucket",
        network_class="eligible-vpc",
    )
    cname = aliyun_oss_candidate(
        region="cn-hangzhou",
        bucket="candidate-bucket",
        network_class="bucket-bound-cname",
        endpoint="https://objects.example.test",
    )

    assert internal.service_endpoint == (
        "https://s3.oss-cn-hangzhou-internal.aliyuncs.com"
    )
    assert internal.contract_key.network_class == "eligible-vpc"
    assert internal.operation_evidence
    internal_registry = build_candidate_registry((internal,))
    assert all(
        internal_registry.lookup(internal.contract_key, operation).state == "test-only"
        for operation in EXPERIMENTAL_BASELINE_OPERATIONS
    )
    assert cname.addressing == "bucket-bound"
    assert cname.object_endpoint == "https://objects.example.test"
    assert cname.contract_key.endpoint_family == "aliyun-oss-cname"
    assert cname.operation_evidence == ()


def test_oss_ordinary_hash_profile_does_not_inherit_experimental_baseline():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
        payload_profile=OSS_ORDINARY_HASH_PROFILE,
    )
    registry = build_candidate_registry((candidate,))

    assert all(
        registry.lookup(candidate.contract_key, operation).state == "test-only"
        for operation in EXPERIMENTAL_BASELINE_OPERATIONS
    )


def test_docs_baseline_is_normal_while_live_test_mode_requires_exact_interlock():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
    )
    registry = build_candidate_registry((candidate,))
    shape = OperationShape(
        operation="upload",
        access_mode="private",
        upload_mode="single-put",
        collision="replace",
    )

    normal = plan_operation(
        shape,
        contract_key=candidate.contract_key,
        registry=registry,
        execution_mode="normal",
        target_ref="project:oss-live",
    )
    wrong_target = plan_operation(
        shape,
        contract_key=candidate.contract_key,
        registry=registry,
        execution_mode="test-only",
        target_ref="project:oss-live",
        live_test_interlock=LiveTestInterlock(
            enabled=True,
            target_ref="project:other",
        ),
    )
    exact_target = plan_operation(
        shape,
        contract_key=candidate.contract_key,
        registry=registry,
        execution_mode="test-only",
        target_ref="project:oss-live",
        live_test_interlock=LiveTestInterlock(
            enabled=True,
            target_ref="project:oss-live",
        ),
    )

    assert normal.executable is True
    assert normal.blocking_reasons == ()
    assert wrong_target.blocking_reasons == ("live_interlock_missing",)
    assert exact_target.executable is True
    assert all(
        registry.lookup(candidate.contract_key, operation).state == "experimental"
        for operation in EXPERIMENTAL_BASELINE_OPERATIONS
    )
    assert all(
        registry.lookup(candidate.contract_key, operation).state == "test-only"
        for operation in CANDIDATE_OPERATIONS
        if operation not in EXPERIMENTAL_BASELINE_OPERATIONS
    )
    assert all(
        registry.lookup(candidate.contract_key, operation).evidence_id.startswith(
            "aliyun-oss-hypothesis-"
        )
        for operation in CANDIDATE_OPERATIONS
        if operation not in EXPERIMENTAL_BASELINE_OPERATIONS
    )


def test_oss_docs_profile_signs_unsigned_payload_with_fixed_length_and_no_chunking():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
    )

    request = build_candidate_put_request(
        candidate,
        oss_connection(candidate),
        key="matrix/fixed.txt",
        body=b"provider-vector",
        content_type="text/plain",
        now=NOW,
    )

    assert request.url == (
        "https://candidate-bucket.s3.oss-eu-central-1.aliyuncs.com/"
        "matrix/fixed.txt"
    )
    assert request.headers["content-length"] == "15"
    assert request.headers["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert request.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert "transfer-encoding" not in request.headers
    assert "content-encoding" not in request.headers
    assert request.canonical_request == (
        "PUT\n"
        "/matrix/fixed.txt\n"
        "\n"
        "content-length:15\n"
        "content-type:text/plain\n"
        "host:candidate-bucket.s3.oss-eu-central-1.aliyuncs.com\n"
        "x-amz-content-sha256:UNSIGNED-PAYLOAD\n"
        "x-amz-date:20260722T120000Z\n"
        "\n"
        "content-length;content-type;host;x-amz-content-sha256;x-amz-date\n"
        "UNSIGNED-PAYLOAD"
    )


def test_oss_candidate_request_builder_applies_one_profile_to_non_put_operations():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
    )
    connection = oss_connection(candidate)

    head = build_candidate_request(
        candidate,
        connection,
        method="HEAD",
        key="matrix/fixed.txt",
        now=NOW,
    )
    part = build_candidate_request(
        candidate,
        connection,
        method="PUT",
        key="matrix/fixed.txt",
        query=(("partNumber", "1"), ("uploadId", "upload-1")),
        body=b"part-bytes",
        headers=(("content-length", "10"),),
        now=NOW,
    )

    assert head.headers["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert part.headers["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert head.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert part.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert "partNumber=1&uploadId=upload-1" in part.url


@pytest.mark.parametrize(
    "header",
    [
        ("transfer-encoding", "chunked"),
        ("content-encoding", "aws-chunked"),
        ("x-amz-content-sha256", "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"),
    ],
)
def test_oss_docs_profile_rejects_streaming_and_chunked_headers(header):
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
    )

    with pytest.raises(CandidateError, match="chunked|streaming"):
        build_candidate_put_request(
            candidate,
            oss_connection(candidate),
            key="matrix/fixed.txt",
            body=b"provider-vector",
            content_type="text/plain",
            extra_headers=(header,),
            now=NOW,
        )


def test_oss_ordinary_payload_hash_is_a_separate_hypothesis_profile():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
        payload_profile=OSS_ORDINARY_HASH_PROFILE,
    )

    request = build_candidate_put_request(
        candidate,
        oss_connection(candidate),
        key="matrix/ordinary.txt",
        body=b"provider-vector",
        content_type="text/plain",
        now=NOW,
    )

    assert candidate.payload_evidence == "hypothesis"
    assert candidate.contract_key.payload_profile == OSS_ORDINARY_HASH_PROFILE
    assert request.headers["x-amz-content-sha256"] == (
        "7d295cb1cffe0d0b031e5a9e2e0d18106c3bc24bdb99ab490ecc109cd7fd24b2"
    )
    assert "x-oss-content-sha256" not in request.headers
    assert "UNSIGNED-PAYLOAD" not in request.canonical_request


def test_cos_candidate_uses_service_address_full_bucket_identity_and_virtual_host():
    candidate = tencent_cos_candidate(
        region="ap-guangzhou",
        bucket="website-images-1250000000",
    )

    assert candidate.service_endpoint == "https://cos.ap-guangzhou.myqcloud.com"
    assert candidate.object_endpoint == (
        "https://website-images-1250000000.cos.ap-guangzhou.myqcloud.com"
    )
    assert candidate.addressing == "virtual"
    assert candidate.contract_key.signing_profile == "aws-v4-format-hypothesis"
    assert candidate.signature_format_evidence == "docs-derived"
    assert candidate.canonical_request_evidence == "hypothesis"
    assert candidate.remote_evidence == "not-tested"


@pytest.mark.parametrize(
    "bucket",
    ["website-images", "website-images-appid", "Website-1250000000"],
)
def test_cos_candidate_requires_explicit_bucket_name_appid_identity(bucket):
    with pytest.raises(CandidateError, match="BucketName-APPID"):
        tencent_cos_candidate(region="ap-guangzhou", bucket=bucket)


def test_cos_candidate_requires_the_full_bucket_identity_to_fit_one_dns_label():
    with pytest.raises(CandidateError, match="BucketName-APPID"):
        tencent_cos_candidate(
            region="ap-guangzhou",
            bucket="a" * 50 + "-12345678901234567890",
        )


def test_cos_candidate_never_generates_path_style_and_rejects_endpoint_disguises():
    with pytest.raises(CandidateError, match="path-style"):
        tencent_cos_candidate(
            region="ap-guangzhou",
            bucket="website-images-1250000000",
            addressing="path",
        )
    with pytest.raises(CandidateError):
        tencent_cos_candidate(
            region="ap-guangzhou",
            bucket="website-images-1250000000",
            endpoint="https://cos.ap-guangzhou.myqcloud.com.",
        )


def test_cos_offline_v4_vector_is_explicitly_hypothesis_not_remote_evidence():
    candidate = tencent_cos_candidate(
        region="ap-guangzhou",
        bucket="website-images-1250000000",
    )

    request = build_candidate_put_request(
        candidate,
        cos_connection(candidate),
        key="matrix/cos.txt",
        body=b"provider-vector",
        content_type="text/plain",
        now=NOW,
    )

    assert request.url == (
        "https://website-images-1250000000.cos.ap-guangzhou.myqcloud.com/"
        "matrix/cos.txt"
    )
    assert (
        "host:website-images-1250000000.cos.ap-guangzhou.myqcloud.com\n"
        in request.canonical_request
    )
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert candidate.remote_evidence == "not-tested"


def test_cos_candidate_rejects_aws_trailer_checksum_profile():
    candidate = tencent_cos_candidate(
        region="ap-guangzhou",
        bucket="website-images-1250000000",
    )

    with pytest.raises(CandidateError, match="trailer|streaming"):
        build_candidate_put_request(
            candidate,
            cos_connection(candidate),
            key="matrix/cos.txt",
            body=b"provider-vector",
            content_type="text/plain",
            extra_headers=(("x-amz-trailer", "x-amz-checksum-sha256"),),
            now=NOW,
        )


def test_versioned_source_manifest_is_official_only_and_tracks_claim_boundaries():
    manifest_path = (
        Path(__file__).parents[2]
        / "references"
        / "maintainers"
        / "provider-candidate-sources.v1.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["retrieved_at"] == "2026-07-22"
    assert {source["publisher"] for source in manifest["sources"]} == {
        "Alibaba Cloud",
        "Tencent Cloud",
    }
    assert all(
        source["url"].startswith(
            (
                "https://help.aliyun.com/",
                "https://www.alibabacloud.com/",
                "https://cloud.tencent.com/",
                "https://www.tencentcloud.com/",
                "https://github.com/tencentyun/",
            )
        )
        for source in manifest["sources"]
    )
    claims = {claim["id"]: claim for claim in manifest["claims"]}
    assert claims["oss-unsigned-payload"]["evidence"] == "docs-derived"
    assert claims["oss-ordinary-payload-sha256"]["evidence"] == "hypothesis"
    assert claims["cos-v4-exact-canonical-request"]["evidence"] == "hypothesis"
    assert claims["cos-remote-operation-compatibility"]["evidence"] == "not-tested"
    assert all(claim["source_ids"] for claim in manifest["claims"] if claim["evidence"] == "docs-derived")
