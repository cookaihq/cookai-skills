from datetime import datetime, timedelta, timezone
import json

import pytest

from capabilities import Capability, CapabilityRegistry, LiveTestInterlock
from artifacts import CheckpointStore
from multipart import (
    abort_multipart,
    execute_multipart as _execute_multipart,
    MultipartError,
    reconcile_multipart,
    resume_multipart,
)
from planning import derive_contract_key
from resolver import ResolvedTarget
from s3 import Response, build_signed_request
from source_file import VerifiedSource
from v2_schema import parse_credential_map, parse_reference, parse_target


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
PART_SIZE = 5 * 1024 * 1024


def execute_multipart(**kwargs):
    plan = kwargs["plan"]
    resolved = kwargs["resolved"]
    with VerifiedSource.open(
        plan["source"]["path"],
        soft_max_bytes=resolved.target.limits.soft_max_bytes,
    ) as source:
        return _execute_multipart(source=source, **kwargs)


def target_value(*, collision="replace"):
    return {
        "schema_version": 1,
        "credential": "project:main-key",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "multipart/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 3600,
        },
        "retention": {"mode": "retain", "days": None},
        "collision": collision,
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 2 * PART_SIZE,
            "multipart_threshold_bytes": PART_SIZE,
            "part_size_bytes": PART_SIZE,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": True, "cors": None},
    }


def resolved_target(*, collision="replace"):
    target = parse_target(target_value(collision=collision), expected_scope="project")
    credential = parse_credential_map({
        "main-key": {
            "access_key_id": "PROJECTKEY1234",
            "secret_access_key": "project-secret-value",
            "session_token": "",
            "expires_at": None,
        }
    })["main-key"]
    return ResolvedTarget(
        ref=parse_reference("project:images", "Target reference"),
        source="cli",
        target=target,
        credential=credential,
        credential_source="process-project-credentials",
        credential_state="available",
    )


def temporary_resolved_target():
    target = parse_target(target_value(), expected_scope="project")
    credential = parse_credential_map({
        "main-key": {
            "access_key_id": "TEMPKEY12345",
            "secret_access_key": "temporary-secret-value",
            "session_token": "temporary-session-token",
            "expires_at": "2026-07-22T12:02:00Z",
        }
    })["main-key"]
    return ResolvedTarget(
        ref=parse_reference("project:images", "Target reference"),
        source="cli",
        target=target,
        credential=credential,
        credential_source="process-project-credentials",
        credential_state="available",
    )


def multipart_registry(resolved, *extra_operations):
    key = derive_contract_key(resolved.target)
    operations = [
        "CreateMultipartUpload",
        "UploadPart",
        "CompleteMultipartUpload",
        "PresignGetObject",
        *extra_operations,
    ]
    return CapabilityRegistry([
        (key, [Capability(name, "test-only", "fixture-" + name.lower()) for name in operations])
    ])


def upload_plan(source, resolved, *, collision="replace"):
    return {
        "executable": True,
        "upload_mode": "multipart",
        "object_key": "multipart/source.bin",
        "source": {"path": str(source), "size": source.stat().st_size},
        "headers": {
            "content_type": "application/octet-stream",
            "cache_control": None,
            "content_disposition": None,
        },
        "access": {
            "mode": "private",
            "url_kind": "presigned",
            "presign_expires_seconds": 3600,
            "presign_effective_seconds": 3600,
            "public_base_url": None,
        },
        "collision": {"policy": collision, "max_attempts": 1},
        "reference_out": None,
    }


def checkpoint_snapshot(project):
    files = list((project / ".s3-upload" / "checkpoints").glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def begin_with_unknown_part(project, source, resolved, *, unknown_part=1):
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>")
        if len(calls) - 1 == unknown_part:
            raise OSError("response lost")
        return Response(200, headers={"ETag": '"part-%d"' % (len(calls) - 1)})

    outcome = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=transport,
        project_root=str(project),
        config_home=str(project / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = outcome.store.load(outcome.checkpoint_id)
    return outcome, checkpoint, calls


def test_fresh_multipart_persists_each_mutation_boundary_and_uses_exact_parts(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    calls = []
    snapshots = []
    built_methods = []

    def request_builder(connection, **kwargs):
        built_methods.append(kwargs["method"])
        return build_signed_request(connection, **kwargs)

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        snapshots.append(checkpoint_snapshot(tmp_path))
        if len(calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>")
        if len(calls) in {2, 3}:
            return Response(200, headers={"ETag": '"part-%d"' % (len(calls) - 1)})
        return Response(200, b"<CompleteMultipartUploadResult><VersionId>version-1</VersionId></CompleteMultipartUploadResult>")

    outcome = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
        request_builder=request_builder,
    )

    assert [snapshot["state"] for snapshot in snapshots] == [
        "initiating", "uploading", "uploading", "completing",
    ]
    assert snapshots[0]["multipart"]["upload_id"] is None
    assert snapshots[1]["multipart"]["in_flight_part"] == {
        "part_number": 1,
        "size": PART_SIZE,
        "sha256": "a29968fad2e782aa9f2040a35f05adb97ed8979eb1f572c8c8ea78637e275f3c",
        "attempt": 1,
    }
    assert snapshots[2]["multipart"]["in_flight_part"]["part_number"] == 2
    assert snapshots[2]["multipart"]["in_flight_part"]["size"] == 4
    assert [call[0] for call in calls] == ["POST", "PUT", "PUT", "POST"]
    assert built_methods == ["POST", "PUT", "PUT", "POST"]
    assert calls[0][3] == b""
    assert calls[1][3] == b"a" * PART_SIZE
    assert calls[2][3] == b"tail"
    assert b"<PartNumber>1</PartNumber>" in calls[3][3]
    assert b'<ETag>"part-2"</ETag>' in calls[3][3]
    assert outcome.result["status"] == "ok"
    assert outcome.result["object_written"] is True
    assert outcome.result["object_reference"]["location"]["version_id"] == "version-1"
    assert outcome.result["url"].startswith("https://project-artifacts.s3.amazonaws.com/multipart/source.bin?")
    assert checkpoint_snapshot(tmp_path)["state"] == "complete"
    outcome.finalize()
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_multipart_reference_out_failure_downgrades_to_a_reconcilable_partial(tmp_path):
    # The object completed remotely; only the caller's reference file could
    # not be published. That is not a plain success, and it is not a blank
    # failure either: the result has to keep the confirmed object, URL and
    # remote identity while picking up the retained checkpoint and the
    # reconcile/unsafe pair that a retained checkpoint implies. Editing the
    # success result in place would keep next_action and retry_safety on
    # their success values.
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    reference_out = out_dir / "reference.json"
    plan = upload_plan(source, resolved)
    plan["reference_out"] = {"path": str(reference_out), "state": "absent"}
    calls = []

    def transport(method, url, headers, body):
        calls.append(method)
        if len(calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>")
        if len(calls) in {2, 3}:
            return Response(200, headers={"ETag": '"part-%d"' % (len(calls) - 1)})
        # The destination preflight cleared stops matching what it cleared
        # while the completion is in flight. The injection is a filesystem
        # fact set from the transport double, independent of the assertions
        # below.
        out_dir.chmod(0o500)
        return Response(200, b"<CompleteMultipartUploadResult><VersionId>version-1</VersionId></CompleteMultipartUploadResult>")

    try:
        outcome = execute_multipart(
            resolved=resolved,
            plan=plan,
            transport=transport,
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW,
            checkpoint_notice=lambda checkpoint_id: None,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )
    finally:
        out_dir.chmod(0o700)

    result = outcome.result
    assert result["status"] == "partial_success"
    assert result["object_written"] is True
    assert result["checkpoint"] == outcome.checkpoint_id
    assert result["checkpoint"] == result["checkpoint_id"]
    assert result["next_action"] == "reconcile"
    assert result["retry_safety"] == "unsafe"
    assert outcome.retain_checkpoint is True
    # Nothing was published at the destination, and the completed object is
    # still fully described by the result the caller does get.
    assert not reference_out.exists()
    assert result["object_reference"]["location"]["version_id"] == "version-1"
    assert result["url"].startswith(
        "https://project-artifacts.s3.amazonaws.com/multipart/source.bin?"
    )
    assert result["url_kind"] == "presigned" and result["expires_at"] is not None
    assert result["remote"]["key"] == "multipart/source.bin"


def test_resume_retries_only_the_same_unknown_part_after_persisting_next_attempt(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    first, checkpoint, first_calls = begin_with_unknown_part(
        tmp_path, source, resolved
    )
    assert first.result["status"] == "partial_success"
    assert len(first_calls) == 2
    assert checkpoint["multipart"]["in_flight_part"]["attempt"] == 1
    resumed_calls = []
    retry_snapshots = []

    def transport(method, url, headers, body):
        resumed_calls.append((method, url, headers, body))
        retry_snapshots.append(checkpoint_snapshot(tmp_path))
        if method == "PUT":
            part_number = 1 if len(resumed_calls) == 1 else 2
            return Response(200, headers={"ETag": '"part-%d"' % part_number})
        return Response(200, b"<CompleteMultipartUploadResult />")

    outcome = resume_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=CheckpointStore(str(tmp_path)),
        transport=transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert [call[0] for call in resumed_calls] == ["PUT", "PUT", "POST"]
    assert "partNumber=1" in resumed_calls[0][1]
    assert "uploadId=upload-1" in resumed_calls[0][1]
    assert resumed_calls[0][3] == b"a" * PART_SIZE
    assert retry_snapshots[0]["multipart"]["in_flight_part"]["attempt"] == 2
    assert "partNumber=2" in resumed_calls[1][1]
    assert resumed_calls[1][3] == b"tail"
    assert outcome.result["operation"] == "resume"
    assert outcome.result["status"] == "ok"


def test_resume_prepared_verifies_source_then_creates_exactly_one_session(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    checkpoint["state"] = "prepared"
    checkpoint["multipart"]["upload_id"] = None
    checkpoint["multipart"]["in_flight_part"] = None
    checkpoint["multipart"]["acknowledged_parts"] = []
    first.store.replace(checkpoint)
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            assert checkpoint_snapshot(tmp_path)["state"] == "initiating"
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>resume-upload</UploadId></InitiateMultipartUploadResult>")
        if method == "PUT":
            return Response(200, headers={"ETag": '"etag"'})
        return Response(200, b"<CompleteMultipartUploadResult />")

    outcome = resume_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert [call[0] for call in calls] == ["POST", "PUT", "PUT", "POST"]
    assert calls[0][1].endswith("?uploads=")
    assert all("resume-upload" in call[1] for call in calls[1:])
    assert outcome.result["status"] == "ok"


def test_fresh_definitive_part_failure_stops_before_any_later_part_or_completion(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>")
        return Response(400, b"<Error><Code>InvalidRequest</Code></Error>")

    outcome = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert [call[0] for call in calls] == ["POST", "PUT"]
    assert outcome.result["status"] == "partial_success"
    checkpoint = outcome.store.load(outcome.checkpoint_id)
    assert checkpoint["state"] == "uploading"
    assert checkpoint["multipart"]["in_flight_part"] is None
    assert checkpoint["multipart"]["acknowledged_parts"] == []


def test_completion_unknown_reconcile_uses_one_exact_head_and_never_repeats_complete(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    mutation_calls = []

    def incomplete_transport(method, url, headers, body):
        mutation_calls.append((method, url, headers, body))
        if len(mutation_calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>")
        if method == "PUT":
            return Response(200, headers={"ETag": '"etag"'})
        raise OSError("completion response lost")

    first = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=incomplete_transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(
            resolved, "HeadObject", "ReservedMetadataRoundTrip"
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = first.store.load(first.checkpoint_id)
    assert first.result["status"] == "ambiguous"
    assert checkpoint["state"] == "completion_unknown"
    assert [call[0] for call in mutation_calls].count("POST") == 2
    assert mutation_calls[0][2]["x-amz-meta-s3-upload-operation-id"] == checkpoint["operation_id"]
    assert mutation_calls[0][2]["x-amz-meta-s3-upload-sha256"] == checkpoint["source"]["sha256"]
    observations = []

    with pytest.raises(MultipartError, match="completion_unknown"):
        resume_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected mutation")),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    reflected = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: Response(200, headers={
            "x-amz-meta-s3-upload-operation-id": checkpoint["operation_id"],
            "x-amz-meta-s3-upload-sha256": checkpoint["source"]["sha256"],
            "content-length": str(checkpoint["source"]["size"]),
            "x-amz-version-id": "prefix-project-secret-value",
        }),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(
            resolved, "HeadObject", "ReservedMetadataRoundTrip"
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    assert reflected.result["status"] == "ambiguous"
    assert "project-secret-value" not in repr(reflected.result)
    assert checkpoint_snapshot(tmp_path)["state"] == "completion_unknown"

    def observer(method, url, headers, body):
        observations.append((method, url, headers, body))
        return Response(200, headers={
            "x-amz-meta-s3-upload-operation-id": checkpoint["operation_id"],
            "x-amz-meta-s3-upload-sha256": checkpoint["source"]["sha256"],
            "content-length": str(checkpoint["source"]["size"]),
            "x-cos-version-id": "reconciled-version",
        })

    outcome = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=observer,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(
            resolved, "HeadObject", "ReservedMetadataRoundTrip"
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert len(observations) == 1 and observations[0][0] == "HEAD"
    assert len(mutation_calls) == 4
    assert outcome.result["operation"] == "reconcile"
    assert outcome.result["status"] == "ok"
    assert outcome.result["object_reference"]["location"]["version_id"] == "reconciled-version"
    assert checkpoint_snapshot(tmp_path)["state"] == "complete"


def test_conditional_completion_collision_requires_one_explicit_abort(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target(collision="reject")
    calls = []

    def collision_transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(200, b"<InitiateMultipartUploadResult><UploadId>collision-upload</UploadId></InitiateMultipartUploadResult>")
        if method == "PUT":
            return Response(200, headers={"ETag": '"etag"'})
        return Response(412)

    first = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved, collision="reject"),
        transport=collision_transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(
            resolved, "ConditionalCompleteMultipartUpload"
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = first.store.load(first.checkpoint_id)
    assert first.result["status"] == "collision"
    assert first.result["object_reference"]["location"]["key"] == "multipart/source.bin"
    assert checkpoint["state"] == "collision_detected"
    abort_snapshots = []

    def abort_transport(method, url, headers, body):
        abort_snapshots.append(checkpoint_snapshot(tmp_path))
        assert method == "DELETE" and "uploadId=collision-upload" in url
        return Response(204)

    outcome = abort_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=abort_transport,
        confirm_abort=True,
        now=NOW,
        registry=multipart_registry(resolved, "AbortMultipartUpload"),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert len(abort_snapshots) == 1
    assert abort_snapshots[0]["state"] == "aborting"
    assert abort_snapshots[0]["multipart"]["return_state"] == "collision_detected"
    assert outcome.result["operation"] == "abort"
    assert outcome.result["status"] == "aborted"
    terminal = checkpoint_snapshot(tmp_path)
    assert terminal["state"] == "aborted"
    assert terminal["multipart"]["return_state"] is None


@pytest.mark.parametrize(
    "list_response, expected_status, expected_state",
    [
        (
            Response(404, b"<Error><Code>NoSuchUpload</Code></Error>"),
            "aborted",
            "aborted",
        ),
        (
            Response(200, b"<ListPartsResult />"),
            "partial_success",
            "uploading",
        ),
        (Response(503), "ambiguous", "abort_unknown"),
    ],
)
def test_abort_unknown_reconcile_distinguishes_absent_open_and_inconclusive_session(
    tmp_path, list_response, expected_status, expected_state
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    abort_calls = []

    def lost_abort(method, url, headers, body):
        abort_calls.append((method, url, headers, body))
        raise OSError("abort response lost")

    unknown = abort_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lost_abort,
        confirm_abort=True,
        now=NOW,
        registry=multipart_registry(resolved, "AbortMultipartUpload"),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = first.store.load(first.checkpoint_id)
    assert unknown.result["status"] == "ambiguous"
    assert checkpoint["state"] == "abort_unknown"
    assert len(abort_calls) == 1 and abort_calls[0][0] == "DELETE"
    observer_calls = []

    def observer(method, url, headers, body):
        observer_calls.append((method, url, headers, body))
        if method == "HEAD":
            return Response(404)
        return list_response

    outcome = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=observer,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(
            resolved,
            "HeadObject",
            "ReservedMetadataRoundTrip",
            "ListParts",
            "ObserveMultipartSession",
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert [call[0] for call in observer_calls] == ["HEAD", "GET"]
    assert all(call[0] not in {"PUT", "POST", "DELETE"} for call in observer_calls)
    assert outcome.result["status"] == expected_status
    assert checkpoint_snapshot(tmp_path)["state"] == expected_state


def test_abort_unknown_stops_when_completion_head_is_inconclusive(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    abort_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: (_ for _ in ()).throw(OSError("lost")),
        confirm_abort=True,
        now=NOW,
        registry=multipart_registry(resolved, "AbortMultipartUpload"),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = first.store.load(first.checkpoint_id)
    calls = []

    outcome = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: (calls.append(args) or Response(503)),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(
            resolved,
            "HeadObject",
            "ReservedMetadataRoundTrip",
            "ListParts",
            "ObserveMultipartSession",
        ),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert len(calls) == 1 and calls[0][0] == "HEAD"
    assert outcome.result["status"] == "ambiguous"
    assert checkpoint_snapshot(tmp_path)["state"] == "abort_unknown"


@pytest.mark.parametrize(
    "execution_mode, interlock",
    [
        ("normal", LiveTestInterlock(True, "project:images")),
        ("test-only", LiveTestInterlock(True, "project:other")),
    ],
)
def test_test_only_gate_blocks_before_checkpoint_or_network(
    tmp_path, execution_mode, interlock
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = resolved_target()
    calls = []

    with pytest.raises(MultipartError, match="blocked"):
        execute_multipart(
            resolved=resolved,
            plan=upload_plan(source, resolved),
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW,
            checkpoint_notice=lambda checkpoint_id: None,
            registry=multipart_registry(resolved),
            execution_mode=execution_mode,
            live_test_interlock=interlock,
        )

    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_initiation_unknown_is_never_recreated_by_resume_or_reconcile(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = resolved_target()
    create_calls = []

    first = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=lambda *args: (create_calls.append(args) or (_ for _ in ()).throw(OSError("lost"))),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    checkpoint = first.store.load(first.checkpoint_id)
    assert checkpoint["state"] == "initiation_unknown" and len(create_calls) == 1
    no_network = lambda *args: (_ for _ in ()).throw(AssertionError("unexpected network"))

    resumed = resume_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=no_network,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    reconciled = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=no_network,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert resumed.result["status"] == reconciled.result["status"] == "ambiguous"
    assert len(create_calls) == 1


def test_resume_retry_exhaustion_and_source_drift_make_zero_remote_calls(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE + b"tail")
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    checkpoint["multipart"]["part_max_attempts"] = 1
    first.store.replace(checkpoint)
    calls = []
    exhausted = resume_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: calls.append(args),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    assert exhausted.result["status"] == "partial_success" and calls == []

    checkpoint["multipart"]["part_max_attempts"] = 3
    first.store.replace(checkpoint)
    source.write_bytes(b"b" + b"a" * (PART_SIZE - 1) + b"tail")
    drifted = resume_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: calls.append(args),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    assert drifted.result["status"] == "partial_success" and calls == []
    assert first.store.load(first.checkpoint_id)["multipart"]["in_flight_part"]["attempt"] == 1


def test_abort_without_confirmation_and_terminal_replay_use_zero_mutations(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    calls = []
    with pytest.raises(MultipartError, match="confirmation"):
        abort_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            confirm_abort=False,
            now=NOW,
            registry=multipart_registry(resolved, "AbortMultipartUpload"),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )
    assert calls == []

    checkpoint["multipart"]["in_flight_part"] = None
    checkpoint["multipart"]["acknowledged_parts"] = [{
        "part_number": 1,
        "size": PART_SIZE,
        "sha256": "a29968fad2e782aa9f2040a35f05adb97ed8979eb1f572c8c8ea78637e275f3c",
        "etag": '"etag"',
    }]
    checkpoint["state"] = "complete"
    first.store.replace(checkpoint)
    replay = reconcile_multipart(
        resolved=resolved,
        checkpoint=checkpoint,
        store=first.store,
        transport=lambda *args: calls.append(args),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )
    assert replay.result["operation"] == "reconcile"
    assert replay.result["status"] == "ok"
    assert calls == []


def test_recovery_rejects_provider_identifier_reflecting_active_secret(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    checkpoint["multipart"]["upload_id"] = "prefix-project-secret-value"
    first.store.replace(checkpoint)
    calls = []

    with pytest.raises(MultipartError, match="identifier"):
        resume_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )
    assert calls == []


def test_temporary_credential_lifetime_is_checked_before_every_request(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    times = iter((NOW, NOW, NOW + timedelta(seconds=90)))
    transport_calls = []

    def transport(method, url, headers, body):
        transport_calls.append((method, url, headers, body))
        return Response(200, b"<InitiateMultipartUploadResult><UploadId>temporary-upload</UploadId></InitiateMultipartUploadResult>")

    with pytest.raises(MultipartError, match="more than 60"):
        execute_multipart(
            resolved=resolved,
            plan=upload_plan(source, resolved),
            transport=transport,
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=lambda: next(times),
            checkpoint_notice=lambda checkpoint_id: None,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    assert len(transport_calls) == 1 and transport_calls[0][0] == "POST"
    checkpoint = checkpoint_snapshot(tmp_path)
    assert checkpoint["state"] == "initiated"
    assert checkpoint["multipart"]["in_flight_part"] is None


def test_expired_before_first_multipart_signature_leaves_no_checkpoint(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    times = iter((NOW, NOW + timedelta(seconds=90)))
    transport_calls = []
    notices = []

    with pytest.raises(MultipartError, match="more than 60"):
        execute_multipart(
            resolved=resolved,
            plan=upload_plan(source, resolved),
            transport=lambda *args: transport_calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=lambda: next(times),
            checkpoint_notice=notices.append,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    assert transport_calls == []
    assert notices == []
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_expiry_before_resume_create_preserves_prepared_checkpoint(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    checkpoint["state"] = "prepared"
    checkpoint["multipart"]["upload_id"] = None
    checkpoint["multipart"]["in_flight_part"] = None
    first.store.replace(checkpoint)
    calls = []

    with pytest.raises(MultipartError, match="more than 60"):
        resume_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW + timedelta(seconds=90),
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    assert calls == []
    assert first.store.load(first.checkpoint_id)["state"] == "prepared"


def test_expiry_before_resume_part_preserves_attempt_count(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    calls = []

    with pytest.raises(MultipartError, match="more than 60"):
        resume_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=NOW + timedelta(seconds=90),
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    retained = first.store.load(first.checkpoint_id)
    assert calls == []
    assert retained["state"] == "uploading"
    assert retained["multipart"]["in_flight_part"]["attempt"] == 1


def test_expiry_before_complete_preserves_uploading_checkpoint(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    times = iter((NOW, NOW, NOW, NOW + timedelta(seconds=90)))
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(
                200,
                b"<InitiateMultipartUploadResult><UploadId>temporary-upload</UploadId>"
                b"</InitiateMultipartUploadResult>",
            )
        return Response(200, headers={"ETag": '"part-1"'})

    with pytest.raises(MultipartError, match="more than 60"):
        execute_multipart(
            resolved=resolved,
            plan=upload_plan(source, resolved),
            transport=transport,
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=lambda: next(times),
            checkpoint_notice=lambda checkpoint_id: None,
            registry=multipart_registry(resolved),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    retained = checkpoint_snapshot(tmp_path)
    assert [call[0] for call in calls] == ["POST", "PUT"]
    assert retained["state"] == "uploading"
    assert retained["multipart"]["in_flight_part"] is None
    assert len(retained["multipart"]["acknowledged_parts"]) == 1


def test_expiry_before_abort_preserves_prior_checkpoint_state(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    first, checkpoint, _ = begin_with_unknown_part(tmp_path, source, resolved)
    calls = []

    with pytest.raises(MultipartError, match="more than 60"):
        abort_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            confirm_abort=True,
            now=NOW + timedelta(seconds=90),
            registry=multipart_registry(resolved, "AbortMultipartUpload"),
            execution_mode="test-only",
            live_test_interlock=LiveTestInterlock(True, "project:images"),
        )

    retained = first.store.load(first.checkpoint_id)
    assert calls == []
    assert retained["state"] == "uploading"
    assert retained["multipart"]["return_state"] is None


def test_multipart_rechecks_temporary_credential_before_result_presign(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    resolved = temporary_resolved_target()
    times = iter((NOW, NOW, NOW, NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)))
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(
                200,
                b"<InitiateMultipartUploadResult><UploadId>temporary-upload</UploadId>"
                b"</InitiateMultipartUploadResult>",
            )
        if len(calls) == 2:
            return Response(200, headers={"ETag": '"part-1"'})
        return Response(
            200,
            b"<CompleteMultipartUploadResult><VersionId>version-1</VersionId>"
            b"</CompleteMultipartUploadResult>",
        )

    outcome = execute_multipart(
        resolved=resolved,
        plan=upload_plan(source, resolved),
        transport=transport,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=lambda: next(times),
        checkpoint_notice=lambda checkpoint_id: None,
        registry=multipart_registry(resolved),
        execution_mode="test-only",
        live_test_interlock=LiveTestInterlock(True, "project:images"),
    )

    assert [call[0] for call in calls] == ["POST", "PUT", "POST"]
    assert outcome.result["status"] == "partial_success"
    assert outcome.result["object_written"] is True
    assert outcome.result["url"] is None
    assert outcome.result["checkpoint_id"] == outcome.checkpoint_id
    assert outcome.retain_checkpoint is True
    assert outcome.store.load(outcome.checkpoint_id)["state"] == "complete"
