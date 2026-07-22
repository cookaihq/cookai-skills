from datetime import datetime, timezone
import json

import upload
from artifacts import CheckpointStore, build_object_reference
from resolver import resolve_target
from s3 import Response


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def configure_candidate(project):
    target_dir = project / ".s3-upload" / "targets"
    target_dir.mkdir(parents=True)
    target = {
        "schema_version": 1,
        "credential": "project:oss-key",
        "provider": "aliyun-oss",
        "region": "cn-hangzhou",
        "endpoint": None,
        "addressing": None,
        "bucket": "candidate-bucket",
        "prefix": "bounded/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 300,
        },
        "retention": {"mode": "retain", "days": None},
        "collision": "replace",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": 5 * 1024 * 1024,
            "part_size_bytes": 5 * 1024 * 1024,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": True, "integration_test": True, "cors": None},
    }
    (target_dir / "oss-live.json").write_text(json.dumps(target), encoding="utf-8")
    credentials = {
        "oss-key": {
            "access_key_id": "OSSACCESS1234",
            "secret_access_key": "oss-secret-value",
            "session_token": "",
            "expires_at": None,
        }
    }
    env_local = project / ".env.local"
    env_local.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON="
        + json.dumps(credentials, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)


def test_candidate_dry_run_requires_process_switch_and_exact_target_allowlist(
    tmp_path, capsys
):
    configure_candidate(tmp_path)
    source = tmp_path / "sample.bin"
    source.write_bytes(b"candidate")
    calls = []
    argv = [
        "upload", "--file", str(source), "--target", "project:oss-live",
        "--dry-run", "--json",
    ]

    normal_rc = upload.main(
        argv,
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )
    normal = capsys.readouterr()
    assert normal_rc == 2 and normal.out == ""

    blocked_rc = upload.main(
        argv,
        environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": "project:another-target",
        },
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )
    blocked = json.loads(capsys.readouterr().out)
    assert blocked_rc == 2
    assert blocked["plan"]["executable"] is False
    assert blocked["plan"]["blocking_reasons"] == ["live_interlock_missing"]

    enabled_rc = upload.main(
        argv,
        environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": "project:oss-live",
        },
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )
    enabled = json.loads(capsys.readouterr().out)
    assert enabled_rc == 0
    assert enabled["plan"]["executable"] is True
    assert enabled["plan"]["contract_key"]["provider"] == "aliyun-oss"
    assert enabled["plan"]["contract_key"]["payload_profile"] == "oss-unsigned-fixed-length"
    assert {item["state"] for item in enabled["plan"]["capabilities"]} == {"test-only"}
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_candidate_execution_requires_the_authorized_evidence_harness(
    tmp_path, capsys
):
    configure_candidate(tmp_path)
    source = tmp_path / "sample.bin"
    source.write_bytes(b"candidate")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:oss-live", "--json"],
        environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": "project:oss-live",
        },
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert "authorized evidence harness" in output.err
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_candidate_reconcile_requires_evidence_harness_before_observer_request(
    tmp_path, capsys
):
    configure_candidate(tmp_path)
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:oss-live",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
        allow_candidates=True,
    )
    key = "bounded/multipart.bin"
    reference = build_object_reference(
        target_ref=resolved.ref.text,
        target=resolved.target,
        key=key,
        version_id=None,
    )
    checkpoint_id = "11111111111141118111111111111111"
    checkpoint = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "kind": "multipart",
        "state": "completion_unknown",
        "operation_id": "22222222222242228222222222222222",
        "created_at": "2026-07-22T12:00:00Z",
        "updated_at": "2026-07-22T12:00:00Z",
        "target_ref": resolved.ref.text,
        "target_fingerprint": resolved.target_fingerprint,
        "object_reference_draft": reference,
        "upload_plan": {
            "content_type": "application/octet-stream",
            "cache_control": None,
            "content_disposition": None,
            "presign_expires_seconds": 300,
        },
        "collision": {
            "policy": "replace",
            "base_key": key,
            "attempt": 1,
            "max_attempts": 1,
        },
        "source": {
            "path": str((tmp_path / "multipart.bin").absolute()),
            "size": 5 * 1024 * 1024,
            "mtime_ns": "1",
            "device": "1",
            "inode": "1",
            "sha256": "a" * 64,
        },
        "reference_out": None,
        "multipart": {
            "upload_id": "upload-1",
            "part_size_bytes": 5 * 1024 * 1024,
            "part_max_attempts": 3,
            "return_state": None,
            "in_flight_part": None,
            "acknowledged_parts": [
                {
                    "part_number": 1,
                    "size": 5 * 1024 * 1024,
                    "sha256": "b" * 64,
                    "etag": "etag-1",
                }
            ],
        },
        "delete_scope": None,
    }
    CheckpointStore(str(tmp_path)).create(checkpoint)
    calls = []

    rc = upload.main(
        ["reconcile", "--checkpoint", checkpoint_id, "--json"],
        environ={
            "S3_UPLOAD_LIVE_TEST": "1",
            "S3_UPLOAD_LIVE_TEST_TARGET": "project:oss-live",
        },
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(404)),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 1 and output.out == ""
    assert "authorized evidence harness" in output.err
    assert calls == []
