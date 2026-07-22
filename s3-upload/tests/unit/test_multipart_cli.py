from datetime import datetime, timezone
import json
import os

import multipart
import planning
import upload
from capabilities import Capability, CapabilityRegistry
from s3 import Response


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
PART_SIZE = 5 * 1024 * 1024


def configure(project):
    target_dir = project / ".s3-upload" / "targets"
    target_dir.mkdir(parents=True)
    target = {
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
        "collision": "replace",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 2 * PART_SIZE,
            "multipart_threshold_bytes": PART_SIZE,
            "part_size_bytes": PART_SIZE,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }
    (target_dir / "multipart.json").write_text(json.dumps(target), encoding="utf-8")
    credentials = {
        "main-key": {
            "access_key_id": "PROJECTKEY1234",
            "secret_access_key": "project-secret-value",
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


def enable_multipart(monkeypatch):
    def registry(_target, key):
        operations = (
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
            "PresignGetObject",
        )
        return CapabilityRegistry(
            ((key, tuple(Capability(name, "enabled", "synthetic-cli") for name in operations)),)
        )

    monkeypatch.setattr(planning, "registry_for_target", registry)
    monkeypatch.setattr(multipart, "registry_for_target", registry)


def test_cli_resumes_only_unknown_part_and_never_recreates_session(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    enable_multipart(monkeypatch)
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    calls = []

    def first_transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if len(calls) == 1:
            return Response(
                200,
                b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId>"
                b"</InitiateMultipartUploadResult>",
            )
        raise OSError("part response lost")

    first_rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:multipart", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=first_transport, now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["status"] == "partial_success"
    assert first["checkpoint_id"] is not None
    assert [call[0] for call in calls] == ["POST", "PUT"]

    def resumed_transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if method == "PUT":
            return Response(200, headers={"ETag": '"part-1"'})
        return Response(200, b"<CompleteMultipartUploadResult/>")

    resumed_rc = upload.main(
        ["resume", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=resumed_transport, now=NOW,
    )
    resumed = json.loads(capsys.readouterr().out)
    assert resumed_rc == 0 and resumed["operation"] == "resume"
    assert resumed["status"] == "ok" and resumed["object_written"] is True
    assert [call[0] for call in calls] == ["POST", "PUT", "PUT", "POST"]
    assert calls[1][1] == calls[2][1]
    assert calls[1][3] == calls[2][3] == b"a" * PART_SIZE
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_multipart_consumes_the_source_descriptor_opened_during_planning(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    enable_multipart(monkeypatch)
    source = tmp_path / "source.bin"
    replacement = tmp_path / "replacement.bin"
    source.write_bytes(b"a" * PART_SIZE)
    replacement.write_bytes(b"b" * PART_SIZE)
    real_build = upload.build_upload_dry_run

    def swap_after_planning(**kwargs):
        planned = real_build(**kwargs)
        replacement.replace(source)
        return planned

    monkeypatch.setattr(upload, "build_upload_dry_run", swap_after_planning)
    part_bodies = []

    def transport(method, url, headers, body):
        if method == "POST" and url.endswith("?uploads="):
            return Response(
                200,
                b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId>"
                b"</InitiateMultipartUploadResult>",
            )
        if method == "PUT":
            part_bodies.append(body)
            return Response(200, headers={"ETag": '"part-1"'})
        return Response(200, b"<CompleteMultipartUploadResult/>")

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:multipart", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=transport,
        now=NOW,
    )

    assert rc == 0
    json.loads(capsys.readouterr().out)
    assert part_bodies == [b"a" * PART_SIZE]


def test_multipart_rejects_in_place_change_before_creating_remote_session(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    enable_multipart(monkeypatch)
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * PART_SIZE)
    real_build = upload.build_upload_dry_run

    def change_after_planning(**kwargs):
        planned = real_build(**kwargs)
        with open(source, "r+b") as changed:
            changed.write(b"b" * PART_SIZE)
            changed.flush()
            os.fsync(changed.fileno())
        return planned

    monkeypatch.setattr(upload, "build_upload_dry_run", change_after_planning)
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:multipart", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 3 and output.out == ""
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()
