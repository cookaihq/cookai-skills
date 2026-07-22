from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from evidence import (
    MATRIX_OPERATIONS,
    CleanupObservation,
    EvidenceError,
    EvidenceObservation,
    EvidenceRunConfig,
    UnitTestResult,
    RequestObservation,
    TrackedObject,
    TrackedSession,
    create_evidence_run_config,
    run_evidence_matrix,
)
from provider_candidates import aliyun_oss_candidate, build_candidate_registry
from v2_schema import CredentialProfile


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
SOURCE = b"provider-evidence-source"
SECRET = "provider-secret-value"
TOKEN = "provider-session-token"
PYTEST_OUTPUT = (
    b"============================= test session starts ==============================\n"
    b"collected 329 items\n\n"
    b"============================= 329 passed in 4.20s ==============================\n"
)


def credential(*, temporary=False, expired=False):
    expiry = None
    token = ""
    if temporary:
        token = TOKEN
        expiry = NOW + (-timedelta(minutes=1) if expired else timedelta(hours=1))
    return CredentialProfile(
        access_key_id="PROVIDERKEY1234",
        secret_access_key=SECRET,
        session_token=token,
        expires_at=expiry,
    )


def candidate_and_registry():
    candidate = aliyun_oss_candidate(
        region="eu-central-1",
        bucket="candidate-bucket",
    )
    return candidate, build_candidate_registry((candidate,))


def live_environ(target="project:oss-live"):
    return {
        "S3_UPLOAD_LIVE_TEST": "1",
        "S3_UPLOAD_LIVE_TEST_TARGET": target,
    }


def config(tmp_path, *, authorizations=None, privilege="least-privilege-confirmed"):
    return EvidenceRunConfig(
        target_ref="project:oss-live",
        target_integration_test=True,
        provider="aliyun-oss",
        exact_endpoint="https://s3.oss-eu-central-1.aliyuncs.com",
        account_applicability="dedicated test bucket; public network",
        privilege_verdict=privilege,
        authorized_operations=frozenset(
            MATRIX_OPERATIONS if authorizations is None else authorizations
        ),
        evidence_dir=str(tmp_path / "evidence"),
        run_id="123e4567e89b42d3a456426614174000",
    )


def unit_test_result(*, output=PYTEST_OUTPUT):
    return UnitTestResult(
        command=("python3", "-m", "pytest", "s3-upload/tests/unit"),
        output=output,
        returncode=0,
        total=329,
        passed=329,
        failed=0,
        errors=0,
        skipped=0,
        python_version="3.12.4",
        pytest_version="8.4.1",
    )


def passing_observation(operation, prefix):
    get_operations = {"GetObject", "PublicGetObject", "PresignGetObject"}
    status = 200
    body = SOURCE if operation in get_operations else b""
    objects = ()
    sessions = ()
    if operation in {
        "PutObject",
        "ConditionalPutObject",
        "CompleteMultipartUpload",
        "ConditionalCompleteMultipartUpload",
        "ReservedMetadataRoundTrip",
    }:
        objects = (
            TrackedObject(
                key=prefix + operation.lower(),
                version_id="version-1",
                checkpoint_id="checkpoint-object",
            ),
        )
    if operation in {"CreateMultipartUpload", "UploadPart"}:
        sessions = (
            TrackedSession(
                key=prefix + "multipart",
                upload_id="upload-1",
                checkpoint_id="checkpoint-session",
            ),
        )
    if operation in {
        "DeleteObjectCurrentKey",
        "DeleteObjectVersion",
        "AbortMultipartUpload",
    }:
        status = 204
    return EvidenceObservation(
        passed=True,
        request_count=1,
        requests=(
            RequestObservation(
                method=(
                    "GET"
                    if operation in get_operations
                    else "DELETE"
                    if operation.startswith("Delete")
                    or operation == "AbortMultipartUpload"
                    else "HEAD"
                    if operation.startswith("Head")
                    or operation.startswith("Observe")
                    else "PUT"
                ),
                url=(
                    "https://candidate-bucket.s3.oss-eu-central-1.aliyuncs.com/"
                    + operation.lower()
                ),
                header_names=("host", "x-amz-date"),
                body_size=(len(SOURCE) if operation.startswith("Put") else 0),
                authorization_mode=(
                    "public-unsigned"
                    if operation == "PublicGetObject"
                    else "presigned-query"
                    if operation == "PresignGetObject"
                    else "authorization-header"
                ),
            ),
        ),
        response_status=status,
        response_headers=(("content-type", "application/octet-stream"),),
        response_body=body,
        raw_response=b"HTTP/1.1 200 OK\r\n\r\n" + body,
        response_proved_nonsecret=True,
        redirect_followup_count=0,
        created_objects=objects,
        created_sessions=sessions,
    )


class FakeAdapter:
    def __init__(self, overrides=None, cleanup_failure=False):
        self.calls = []
        self.object_cleanups = []
        self.session_cleanups = []
        self.overrides = overrides or {}
        self.cleanup_failure = cleanup_failure

    def execute(self, operation, context):
        self.calls.append(operation)
        override = self.overrides.get(operation)
        if override is not None:
            return override
        return passing_observation(operation, context.run_prefix)

    def cleanup_object(self, reference, context):
        self.object_cleanups.append(reference)
        return CleanupObservation(
            passed=not self.cleanup_failure,
            request_count=1,
            status=204 if not self.cleanup_failure else 500,
            manual_cleanup=(
                None if not self.cleanup_failure else "delete exact object reference"
            ),
        )

    def abort_session(self, reference, context):
        self.session_cleanups.append(reference)
        return CleanupObservation(
            passed=not self.cleanup_failure,
            request_count=1,
            status=204 if not self.cleanup_failure else 500,
            manual_cleanup=(
                None if not self.cleanup_failure else "abort exact upload id"
            ),
        )


def run(
    tmp_path, adapter, *, environ=None, run_config=None, credentials=None,
    unit_tests=None,
):
    candidate, registry = candidate_and_registry()
    return run_evidence_matrix(
        config=run_config or config(tmp_path),
        process_environ=live_environ() if environ is None else environ,
        contract_key=candidate.contract_key,
        registry=registry,
        credential=credential() if credentials is None else credentials,
        source_bytes=SOURCE,
        adapter=adapter,
        unit_tests=unit_test_result() if unit_tests is None else unit_tests,
        now=NOW,
    )


def all_persisted_bytes(path):
    return b"".join(
        item.read_bytes() for item in path.rglob("*") if item.is_file()
    )


def test_missing_or_mismatched_process_interlock_makes_zero_requests_and_writes_nothing(tmp_path):
    for environ in ({}, live_environ("project:other")):
        adapter = FakeAdapter()
        result = run(tmp_path, adapter, environ=environ)

        assert result.gate_status == "not-authorized"
        assert result.persisted is False
        assert adapter.calls == []
        assert not (tmp_path / "evidence").exists()


def test_target_must_be_marked_as_an_integration_test_before_any_request(tmp_path):
    adapter = FakeAdapter()
    run_config = config(tmp_path)
    run_config = EvidenceRunConfig(
        **{**run_config.__dict__, "target_integration_test": False}
    )

    result = run(tmp_path, adapter, run_config=run_config)

    assert result.gate_status == "not-authorized"
    assert result.persisted is False
    assert adapter.calls == []


def test_mutation_without_cleanup_authorization_is_not_authorized_and_sends_zero_requests(tmp_path):
    adapter = FakeAdapter()
    run_config = config(tmp_path, authorizations={"PutObject"})

    result = run(tmp_path, adapter, run_config=run_config)

    put = next(
        item for item in result.report["operations"] if item["operation"] == "PutObject"
    )
    assert put["status"] == "not-authorized"
    assert put["reason"] == "cleanup_authorization_missing"
    assert "PutObject" not in adapter.calls


def test_unknown_capability_is_not_supported_and_sends_zero_requests(tmp_path):
    candidate, _ = candidate_and_registry()
    adapter = FakeAdapter()
    from capabilities import CapabilityRegistry

    result = run_evidence_matrix(
        config=config(tmp_path),
        process_environ=live_environ(),
        contract_key=candidate.contract_key,
        registry=CapabilityRegistry(()),
        credential=credential(),
        source_bytes=SOURCE,
        adapter=adapter,
        unit_tests=unit_test_result(),
        now=NOW,
    )

    assert {item["status"] for item in result.report["operations"]} == {
        "not-supported"
    }
    assert adapter.calls == []


def test_expired_temporary_credential_records_not_tested_without_requests(tmp_path):
    adapter = FakeAdapter()

    result = run(tmp_path, adapter, credentials=credential(temporary=True, expired=True))

    assert {item["status"] for item in result.report["operations"]} == {
        "not-tested"
    }
    assert adapter.calls == []
    assert result.report["credential"] == {
        "kind": "temporary",
        "expires_at": "2026-07-22T11:59:00Z",
        "unexpired": False,
    }


def test_fake_full_matrix_validates_get_bytes_cleans_resources_and_writes_restricted_bundle(tmp_path):
    adapter = FakeAdapter()

    result = run(tmp_path, adapter)

    evidence_dir = tmp_path / "evidence"
    assert result.gate_status == "authorized"
    assert result.persisted is True
    assert len(result.report["operations"]) == len(MATRIX_OPERATIONS)
    assert {item["status"] for item in result.report["operations"]} == {"passed"}
    for operation in ("GetObject", "PublicGetObject", "PresignGetObject"):
        item = next(
            row for row in result.report["operations"] if row["operation"] == operation
        )
        assert item["response_body"] == {
            "size": len(SOURCE),
            "sha256": hashlib.sha256(SOURCE).hexdigest(),
            "matches_source": True,
        }
    assert adapter.object_cleanups
    assert adapter.session_cleanups
    assert all(
        reference.key.startswith(result.report["run_prefix"])
        for reference in adapter.object_cleanups + adapter.session_cleanups
    )
    assert {item["status"] for item in result.report["cleanup"]} == {"passed"}
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((evidence_dir / "report.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((evidence_dir / "report.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((evidence_dir / "test_output.log").stat().st_mode) == 0o600
    persisted_report = json.loads((evidence_dir / "report.json").read_text())
    assert persisted_report["contract_key"] == candidate_and_registry()[0].contract_key.as_dict()
    assert persisted_report["exact_endpoint"] == (
        "https://s3.oss-eu-central-1.aliyuncs.com"
    )
    assert persisted_report["target_ref"] == "project:oss-live"
    assert persisted_report["credential"] == {
        "kind": "permanent",
        "expires_at": None,
        "unexpired": True,
    }
    persisted = all_persisted_bytes(evidence_dir)
    assert SECRET.encode() not in persisted
    assert TOKEN.encode() not in persisted
    assert b"PROVIDERKEY1234" not in persisted


def test_bundle_preserves_pytest_output_and_has_the_five_required_report_parts(tmp_path):
    result = run(tmp_path, FakeAdapter())

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "test_output.log").read_bytes() == PYTEST_OUTPUT
    markdown = (evidence_dir / "report.md").read_text(encoding="utf-8")
    for heading in (
        "## 1. Test summary",
        "## 2. Unit test results",
        "## 3. Integration test results",
        "## 4. Assumption verification",
        "## 5. Findings",
    ):
        assert heading in markdown
    assert "2026-07-22T12:00:00Z" in markdown
    assert "Python `3.12.4`; pytest `8.4.1`" in markdown
    assert "329 passed; 0 failed; 0 errors; 0 skipped" in markdown
    assert "| Scenario | Expected | Actual | Outcome |" in markdown
    persisted_report = json.loads((evidence_dir / "report.json").read_text())
    assert persisted_report == result.report
    assert "unit_tests" not in persisted_report


def test_get_requires_complete_matching_bytes_not_only_2xx(tmp_path):
    adapter = FakeAdapter(
        overrides={
            "GetObject": EvidenceObservation(
                passed=True,
                request_count=1,
                requests=(
                    RequestObservation(
                        method="GET",
                        url="https://candidate.example/get",
                        header_names=("authorization",),
                        body_size=0,
                        authorization_mode="authorization-header",
                    ),
                ),
                response_status=200,
                response_body=b"truncated",
                raw_response=b"HTTP/1.1 200 OK\r\n\r\ntruncated",
                response_proved_nonsecret=True,
            )
        }
    )

    result = run(tmp_path, adapter)

    get = next(
        item for item in result.report["operations"] if item["operation"] == "GetObject"
    )
    assert get["status"] == "failed"
    assert get["reason"] == "response_bytes_mismatch"
    assert get["response_body"]["matches_source"] is False


def test_redirect_is_exposed_as_failure_and_adapter_records_no_followup(tmp_path):
    adapter = FakeAdapter(
        overrides={
            "PresignGetObject": EvidenceObservation(
                passed=True,
                request_count=1,
                requests=(
                    RequestObservation(
                        method="GET",
                        url=(
                            "https://candidate.example/object?"
                            "X-Amz-Credential=PROVIDERKEY1234&X-Amz-Signature=abcdef"
                        ),
                        header_names=("host",),
                        body_size=0,
                        authorization_mode="presigned-query",
                    ),
                ),
                response_status=302,
                response_headers=(("location", "https://other.example/object"),),
                response_body=b"",
                raw_response=b"HTTP/1.1 302 Found\r\nLocation: https://other.example/object\r\n\r\n",
                response_proved_nonsecret=True,
                redirect_followup_count=0,
            )
        }
    )

    result = run(tmp_path, adapter)

    get = next(
        item
        for item in result.report["operations"]
        if item["operation"] == "PresignGetObject"
    )
    assert get["status"] == "failed"
    assert get["reason"] == "redirect_response"
    assert get["request_count"] == 1
    assert get["redirect_followup_count"] == 0
    assert get["requests"] == [
        {
            "sequence": 1,
            "method": "GET",
            "url": "https://candidate.example/object",
            "query_redacted": True,
            "header_names": ["host"],
            "body_size": 0,
            "authorization_mode": "presigned-query",
        }
    ]


def test_secret_reflection_is_redacted_before_hashing_or_persistence(tmp_path):
    unsafe = (
        b"HTTP/1.1 403 Forbidden\r\nSet-Cookie: session="
        + TOKEN.encode()
        + b"\r\n\r\n"
        + SECRET.encode()
        + b"&X-Amz-Signature=abcdef"
    )
    adapter = FakeAdapter(
        overrides={
            "HeadObject": EvidenceObservation(
                passed=False,
                request_count=1,
                requests=(
                    RequestObservation(
                        method="HEAD",
                        url="https://candidate.example/object",
                        header_names=("authorization",),
                        body_size=0,
                        authorization_mode="authorization-header",
                    ),
                ),
                response_status=403,
                raw_response=unsafe,
                response_proved_nonsecret=False,
            )
        }
    )

    result = run(tmp_path, adapter, credentials=credential(temporary=True))

    head = next(
        item for item in result.report["operations"] if item["operation"] == "HeadObject"
    )
    assert head["persisted_response_hash_kind"] == "redacted_bytes_sha256"
    persisted = all_persisted_bytes(tmp_path / "evidence")
    assert SECRET.encode() not in persisted
    assert TOKEN.encode() not in persisted
    assert b"Set-Cookie: session=" not in persisted
    assert b"X-Amz-Signature=abcdef" not in persisted
    assert hashlib.sha256(unsafe).hexdigest().encode() not in persisted


def test_cleanup_failure_preserves_exact_references_and_manual_instructions(tmp_path):
    adapter = FakeAdapter(cleanup_failure=True)

    result = run(tmp_path, adapter)

    assert any(item["status"] == "failed" for item in result.report["cleanup"])
    assert result.report["residuals"]
    residual = result.report["residuals"][0]
    assert residual["checkpoint_id"] in {"checkpoint-object", "checkpoint-session"}
    assert residual["manual_cleanup"] in {
        "delete exact object reference",
        "abort exact upload id",
    }
    assert result.report["release_eligible"] is False


def test_unknown_privilege_can_run_bounded_matrix_but_never_release_evidence(tmp_path):
    adapter = FakeAdapter()

    result = run(
        tmp_path,
        adapter,
        run_config=config(tmp_path, privilege="unknown"),
    )

    assert adapter.calls
    assert result.report["privilege_verdict"] == "unknown"
    assert result.report["release_eligible"] is False


def test_unexpected_resource_is_not_cleaned_without_explicit_cleanup_authorization(tmp_path):
    prefix = "s3-upload-live-test/123e4567e89b42d3a456426614174000/"
    adapter = FakeAdapter(
        overrides={
            "HeadObject": EvidenceObservation(
                passed=True,
                request_count=1,
                requests=(
                    RequestObservation(
                        method="HEAD",
                        url="https://candidate.example/unexpected",
                        header_names=("authorization",),
                        body_size=0,
                        authorization_mode="authorization-header",
                    ),
                ),
                response_status=200,
                created_objects=(
                    TrackedObject(
                        key=prefix + "unexpected",
                        version_id="version-1",
                        checkpoint_id="checkpoint-unexpected",
                    ),
                ),
            )
        }
    )

    result = run(
        tmp_path,
        adapter,
        run_config=config(tmp_path, authorizations={"HeadObject"}),
    )

    assert adapter.object_cleanups == []
    cleanup = result.report["cleanup"]
    assert cleanup == [
        {
            "resource_type": "object",
            "key": prefix + "unexpected",
            "version_id": "version-1",
            "upload_id": None,
            "checkpoint_id": "checkpoint-unexpected",
            "status": "not-authorized",
            "request_count": 0,
            "response_status": None,
            "manual_cleanup": "DeleteObjectCurrentKey cleanup was not authorized",
        }
    ]
    assert result.report["residuals"][0]["checkpoint_id"] == (
        "checkpoint-unexpected"
    )


def test_unignored_evidence_destination_fails_before_any_adapter_request(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    adapter = FakeAdapter()
    run_config = config(tmp_path)
    run_config = EvidenceRunConfig(
        **{**run_config.__dict__, "evidence_dir": str(repository / "evidence")}
    )

    with pytest.raises(EvidenceError, match="Git ignored"):
        run(tmp_path, adapter, run_config=run_config)

    assert adapter.calls == []
    assert not (repository / "evidence").exists()


def test_ignored_evidence_destination_is_accepted_inside_a_worktree(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / ".gitignore").write_text("evidence/\n", encoding="utf-8")
    adapter = FakeAdapter()
    run_config = config(tmp_path)
    run_config = EvidenceRunConfig(
        **{**run_config.__dict__, "evidence_dir": str(repository / "evidence")}
    )

    result = run(tmp_path, adapter, run_config=run_config)

    assert result.persisted is True
    assert (repository / "evidence" / "report.json").is_file()


def test_evidence_config_factory_generates_distinct_uuid4_run_ids(tmp_path):
    values = {
        "target_ref": "project:oss-live",
        "target_integration_test": True,
        "provider": "aliyun-oss",
        "exact_endpoint": "https://s3.oss-eu-central-1.aliyuncs.com",
        "account_applicability": "dedicated test bucket",
        "privilege_verdict": "unknown",
        "authorized_operations": frozenset({"HeadObject"}),
        "evidence_dir": str(tmp_path / "evidence"),
    }

    first = create_evidence_run_config(**values)
    second = create_evidence_run_config(**values)

    assert first.run_id != second.run_id
    assert first.run_id[12] == "4" and second.run_id[12] == "4"


def test_evidence_config_rejects_a_relative_destination():
    with pytest.raises(EvidenceError, match="evidence directory"):
        EvidenceRunConfig(
            target_ref="project:oss-live",
            target_integration_test=True,
            provider="aliyun-oss",
            exact_endpoint="https://s3.oss-eu-central-1.aliyuncs.com",
            account_applicability="dedicated test bucket",
            privilege_verdict="unknown",
            authorized_operations=frozenset({"HeadObject"}),
            evidence_dir="relative/evidence",
            run_id="123e4567e89b42d3a456426614174000",
        )
