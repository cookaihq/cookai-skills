from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

from config import Connection
from evidence import (
    EvidenceOperationContext,
    TrackedObject,
    create_evidence_run_config,
    run_evidence_matrix,
)
from live_adapter import S3EvidenceAdapter
from provider_candidates import aliyun_oss_candidate, build_candidate_registry
from s3 import Response
from v2_schema import CredentialProfile


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
AUTHORIZED = frozenset(
    {
        "PutObject",
        "HeadObject",
        "GetObject",
        "PresignGetObject",
        "DeleteObjectCurrentKey",
        "ObserveDeleteCurrentKey",
        "CreateMultipartUpload",
        "UploadPart",
        "ListParts",
        "CompleteMultipartUpload",
        "AbortMultipartUpload",
        "ObserveMultipartSession",
        "ReservedMetadataRoundTrip",
        "ResponseParsing",
        "Reconciliation",
    }
)


class FakeProvider:
    def __init__(self):
        self.objects = {}
        self.sessions = {}
        self.next_upload = 1
        self.calls = []

    @staticmethod
    def _key(url):
        return unquote(urlsplit(url).path.lstrip("/"))

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, tuple(sorted(headers)), len(body)))
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        key = self._key(url)
        upload_id = query.get("uploadId", [None])[0]
        if method == "POST" and "uploads" in query:
            upload_id = f"upload-{self.next_upload}"
            self.next_upload += 1
            self.sessions[upload_id] = {"key": key, "parts": {}}
            xml = (
                "<InitiateMultipartUploadResult><UploadId>"
                + upload_id
                + "</UploadId></InitiateMultipartUploadResult>"
            ).encode()
            return Response(200, xml)
        if upload_id is not None and method == "PUT":
            session = self.sessions.get(upload_id)
            if session is None:
                return Response(404)
            number = int(query["partNumber"][0])
            etag = f'"etag-{number}"'
            session["parts"][number] = (body, etag)
            return Response(200, headers={"ETag": etag})
        if upload_id is not None and method == "GET":
            session = self.sessions.get(upload_id)
            if session is None:
                return Response(404)
            root = ElementTree.Element("ListPartsResult")
            for number, (_data, etag) in sorted(session["parts"].items()):
                part = ElementTree.SubElement(root, "Part")
                ElementTree.SubElement(part, "PartNumber").text = str(number)
                ElementTree.SubElement(part, "ETag").text = etag
            return Response(200, ElementTree.tostring(root))
        if upload_id is not None and method == "POST":
            session = self.sessions.pop(upload_id, None)
            if session is None:
                return Response(404)
            data = b"".join(value[0] for _, value in sorted(session["parts"].items()))
            self.objects[key] = {"body": data, "headers": {}}
            return Response(200, b"<CompleteMultipartUploadResult/>")
        if upload_id is not None and method == "DELETE":
            existed = self.sessions.pop(upload_id, None) is not None
            return Response(204 if existed else 404)
        if method == "PUT":
            metadata = {
                name.lower(): value
                for name, value in headers.items()
                if name.lower().startswith("x-amz-meta-")
            }
            self.objects[key] = {"body": body, "headers": metadata}
            return Response(200)
        if method == "HEAD":
            item = self.objects.get(key)
            if item is None:
                return Response(404)
            response_headers = dict(item["headers"])
            response_headers["content-length"] = str(len(item["body"]))
            return Response(200, headers=response_headers)
        if method == "GET":
            item = self.objects.get(key)
            return Response(404) if item is None else Response(200, item["body"])
        if method == "DELETE":
            self.objects.pop(key, None)
            return Response(204)
        return Response(400)


def run_adapter_matrix(tmp_path, provider, *, authorized_operations=AUTHORIZED):
    candidate = aliyun_oss_candidate(
        region="cn-hangzhou", bucket="candidate-bucket"
    )
    credential = CredentialProfile(
        "ALIYUNKEY1234", "aliyun-secret-value", "", None
    )
    connection = Connection(
        access_key_id=credential.access_key_id,
        secret_access_key=credential.secret_access_key,
        session_token=credential.session_token,
        bucket=candidate.bucket,
        endpoint=candidate.service_endpoint,
        region=candidate.region,
        provider=candidate.provider,
        addressing=candidate.addressing,
    )
    target_ref = "project:oss-live"
    config = create_evidence_run_config(
        target_ref=target_ref,
        target_integration_test=True,
        provider=candidate.provider,
        exact_endpoint=candidate.service_endpoint,
        account_applicability="synthetic account/bucket fixture",
        privilege_verdict="unknown",
        authorized_operations=authorized_operations,
        evidence_dir=str(tmp_path / "evidence"),
    )
    return run_evidence_matrix(
        config=config,
        process_environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": target_ref,
        },
        contract_key=candidate.contract_key,
        registry=build_candidate_registry((candidate,)),
        credential=credential,
        source_bytes=b"bounded-provider-evidence",
        adapter=S3EvidenceAdapter(candidate, connection, provider),
        now=NOW,
    )


def test_metadata_object_is_cleaned_when_followup_response_is_lost(tmp_path):
    class MetadataHeadResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            if (
                not self.failed
                and method == "HEAD"
                and self._key(url).endswith("metadata-object.bin")
            ):
                self.failed = True
                raise OSError("metadata HEAD response lost")
            return response

    provider = MetadataHeadResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    metadata = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "ReservedMetadataRoundTrip"
    )
    assert metadata["status"] == "failed"
    assert any(
        row["key"].endswith("metadata-object.bin")
        for row in result.report["cleanup"]
    )
    assert result.report["residuals"] == []
    assert provider.objects == {}


def test_metadata_object_is_cleaned_when_put_response_is_lost(tmp_path):
    class MetadataPutResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            if (
                not self.failed
                and method == "PUT"
                and self._key(url).endswith("metadata-object.bin")
            ):
                self.failed = True
                raise OSError("metadata PutObject response lost")
            return response

    provider = MetadataPutResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    metadata = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "ReservedMetadataRoundTrip"
    )
    assert metadata["status"] == "failed"
    assert any(
        row["resource_type"] == "object"
        and row["key"].endswith("metadata-object.bin")
        and row["status"] == "passed"
        for row in result.report["cleanup"]
    )
    assert result.report["residuals"] == []
    assert not any(
        key.endswith("metadata-object.bin") for key in provider.objects
    )


def test_put_object_is_cleaned_when_mutation_response_is_lost(tmp_path):
    class PutResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            if (
                not self.failed
                and method == "PUT"
                and self._key(url).endswith("core-object.bin")
            ):
                self.failed = True
                raise OSError("PutObject response lost")
            return response

    provider = PutResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    put = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "PutObject"
    )
    assert put["status"] == "failed"
    assert any(
        row["resource_type"] == "object"
        and row["key"].endswith("core-object.bin")
        and row["status"] == "passed"
        for row in result.report["cleanup"]
    )
    assert result.report["residuals"] == []
    assert not any(key.endswith("core-object.bin") for key in provider.objects)


def test_completed_multipart_object_is_cleaned_when_response_is_lost(tmp_path):
    class CompleteResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            if (
                not self.failed
                and method == "POST"
                and "uploadId" in query
                and self._key(url).endswith("multipart-object.bin")
            ):
                self.failed = True
                raise OSError("CompleteMultipartUpload response lost")
            return response

    provider = CompleteResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    complete = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "CompleteMultipartUpload"
    )
    assert complete["status"] == "failed"
    assert any(
        row["resource_type"] == "object"
        and row["key"].endswith("multipart-object.bin")
        and row["status"] == "passed"
        for row in result.report["cleanup"]
    )
    assert result.report["residuals"] == []
    assert not any(
        key.endswith("multipart-object.bin") for key in provider.objects
    )


def test_abort_session_is_tracked_when_abort_response_is_lost(tmp_path):
    class AbortResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            if (
                not self.failed
                and method == "DELETE"
                and "uploadId" in query
                and self._key(url).endswith("abort-object.bin")
            ):
                self.failed = True
                raise OSError("abort response lost")
            return response

    provider = AbortResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    abort = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "AbortMultipartUpload"
    )
    assert abort["status"] == "failed"
    assert any(
        row["resource_type"] == "multipart-session"
        and row["key"].endswith("abort-object.bin")
        and row["upload_id"] is not None
        and row["status"] == "passed"
        for row in result.report["cleanup"]
    )
    assert result.report["residuals"] == []
    assert provider.sessions == {}


def test_abort_create_response_loss_preserves_manual_cleanup_residual(tmp_path):
    class AbortCreateResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            if (
                not self.failed
                and method == "POST"
                and "uploads" in query
                and self._key(url).endswith("abort-object.bin")
            ):
                self.failed = True
                raise OSError("abort fixture create response lost")
            return response

    provider = AbortCreateResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    abort = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "AbortMultipartUpload"
    )
    assert abort["status"] == "failed"
    residual = next(
        row
        for row in result.report["residuals"]
        if row["resource_type"] == "multipart-session"
        and row["key"].endswith("abort-object.bin")
    )
    assert residual["upload_id"] is None
    assert residual["checkpoint_id"]
    assert residual["manual_cleanup"] == (
        "CreateMultipartUpload response was lost during abort evidence; "
        "inspect the exact run-prefix session"
    )
    assert any(
        session["key"].endswith("abort-object.bin")
        for session in provider.sessions.values()
    )


def test_create_session_response_loss_preserves_manual_cleanup_residual(tmp_path):
    class CreateResponseLost(FakeProvider):
        failed = False

        def __call__(self, method, url, headers, body):
            response = super().__call__(method, url, headers, body)
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            if (
                not self.failed
                and method == "POST"
                and "uploads" in query
                and self._key(url).endswith("multipart-object.bin")
            ):
                self.failed = True
                raise OSError("create multipart response lost")
            return response

    provider = CreateResponseLost()

    result = run_adapter_matrix(tmp_path, provider)

    create = next(
        row
        for row in result.report["operations"]
        if row["operation"] == "CreateMultipartUpload"
    )
    assert create["status"] == "failed"
    residual = next(
        row
        for row in result.report["residuals"]
        if row["resource_type"] == "multipart-session"
        and row["key"].endswith("multipart-object.bin")
    )
    assert residual["upload_id"] is None
    assert residual["checkpoint_id"]
    assert residual["manual_cleanup"] == (
        "CreateMultipartUpload response was lost; inspect the exact run-prefix session"
    )
    cleanup = next(
        row
        for row in result.report["cleanup"]
        if row["checkpoint_id"] == residual["checkpoint_id"]
    )
    assert cleanup["status"] == "failed"
    assert cleanup["request_count"] == 0
    assert any(
        session["key"].endswith("multipart-object.bin")
        for session in provider.sessions.values()
    )


def test_known_version_is_not_cleaned_with_current_key_delete_without_authorization():
    candidate = aliyun_oss_candidate(
        region="cn-hangzhou", bucket="candidate-bucket"
    )
    connection = Connection(
        access_key_id="ALIYUNKEY1234",
        secret_access_key="aliyun-secret-value",
        session_token="",
        bucket=candidate.bucket,
        endpoint=candidate.service_endpoint,
        region=candidate.region,
        provider=candidate.provider,
        addressing=candidate.addressing,
    )
    provider = FakeProvider()
    key = "s3-upload-live-test/123e4567e89b42d3a456426614174000/versioned.bin"
    provider.objects[key] = {"body": b"current-version", "headers": {}}
    context = EvidenceOperationContext(
        target_ref="project:oss-live",
        provider=candidate.provider,
        exact_endpoint=candidate.service_endpoint,
        contract_key=candidate.contract_key,
        run_prefix="s3-upload-live-test/123e4567e89b42d3a456426614174000/",
        source_bytes=b"bounded-provider-evidence",
        source_size=len(b"bounded-provider-evidence"),
        source_sha256="0" * 64,
        now=NOW,
    )
    adapter = S3EvidenceAdapter(candidate, connection, provider)

    cleanup = adapter.cleanup_object(
        TrackedObject(key, "version-7", "checkpoint-versioned"), context
    )

    assert cleanup.passed is False
    assert cleanup.request_count == 0
    assert cleanup.status is None
    assert cleanup.manual_cleanup == (
        "DeleteObjectVersion cleanup requires explicit authorization; "
        "delete the exact version from the restricted checkpoint"
    )
    assert provider.calls == []
    assert provider.objects[key]["body"] == b"current-version"


def test_authorized_adapter_runs_bounded_data_plane_and_cleans_every_resource(tmp_path):
    candidate = aliyun_oss_candidate(
        region="cn-hangzhou", bucket="candidate-bucket"
    )
    credential = CredentialProfile(
        "ALIYUNKEY1234", "aliyun-secret-value", "", None
    )
    connection = Connection(
        access_key_id=credential.access_key_id,
        secret_access_key=credential.secret_access_key,
        session_token=credential.session_token,
        bucket=candidate.bucket,
        endpoint=candidate.service_endpoint,
        region=candidate.region,
        provider=candidate.provider,
        addressing=candidate.addressing,
    )
    provider = FakeProvider()
    target_ref = "project:oss-live"
    config = create_evidence_run_config(
        target_ref=target_ref,
        target_integration_test=True,
        provider=candidate.provider,
        exact_endpoint=candidate.service_endpoint,
        account_applicability="synthetic account/bucket fixture",
        privilege_verdict="unknown",
        authorized_operations=AUTHORIZED,
        evidence_dir=str(tmp_path / "evidence"),
    )

    result = run_evidence_matrix(
        config=config,
        process_environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": target_ref,
        },
        contract_key=candidate.contract_key,
        registry=build_candidate_registry((candidate,)),
        credential=credential,
        source_bytes=b"bounded-provider-evidence",
        adapter=S3EvidenceAdapter(candidate, connection, provider),
        now=NOW,
    )

    statuses = {
        row["operation"]: row["status"] for row in result.report["operations"]
    }
    assert result.gate_status == "authorized" and result.persisted is True
    assert all(statuses[name] == "passed" for name in AUTHORIZED)
    assert result.report["release_eligible"] is False
    assert result.report["privilege_verdict"] == "unknown"
    assert result.report["residuals"] == []
    assert result.report["cleanup"]
    assert {row["status"] for row in result.report["cleanup"]} == {"passed"}
    assert provider.objects == {} and provider.sessions == {}
    assert all(
        urlsplit(call[1]).path.startswith("/s3-upload-live-test/")
        for call in provider.calls
    )
