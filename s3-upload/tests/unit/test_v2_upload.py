from datetime import datetime, timedelta, timezone
import json

import pytest

import operations
import planning
import upload
from artifacts import build_object_reference, serialize_object_reference
from resolver import resolve_target
from s3 import Response
from v2_schema import parse_target


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def target():
    return {
        "schema_version": 1,
        "credential": "project:main-key",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "website-images/",
        "access": {"mode": "private", "public_base_url": None, "presign_expires_seconds": 3600},
        "retention": {"mode": "retain", "days": None},
        "collision": "replace",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {"soft_max_bytes": 104857600, "multipart_threshold_bytes": None, "part_size_bytes": None},
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }


def configure(project, target_value=None, *, credential_expires_at=None):
    directory = project / ".s3-upload" / "targets"
    directory.mkdir(parents=True)
    (directory / "images.json").write_text(json.dumps(target_value or target()), encoding="utf-8")
    credentials = {
        "main-key": {
            "access_key_id": "PROJECTKEY1234",
            "secret_access_key": "project-secret-value",
            "session_token": (
                "temporary-session-token" if credential_expires_at is not None else ""
            ),
            "expires_at": credential_expires_at,
        }
    }
    env_local = project / ".env.local"
    env_local.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON=" + json.dumps(credentials, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)


def test_private_baseline_dry_run_has_complete_v2_plan_and_no_side_effect(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"png")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json", "--dry-run"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 0
    assert set(result) == {
        "schema_version", "operation", "status", "object_written", "object_reference",
        "url", "url_kind", "expires_at", "retention", "delete_scope",
        "deleted_version_id", "checkpoint_id", "plan",
    }
    assert result["operation"] == "upload"
    assert result["status"] == "dry_run"
    assert result["object_written"] is False
    assert result["url_kind"] == "presigned"
    assert result["retention"] == {"mode": "retain", "days": None, "enforcement": "external-unverified"}
    assert result["object_reference"] is None and result["checkpoint_id"] is None
    plan = result["plan"]
    assert set(plan) == {
        "executable", "blocking_reasons", "target_ref", "target_source", "target_fingerprint",
        "provider", "endpoint", "addressing", "region", "bucket", "prefix", "object_key",
        "source", "contract_key", "remote_operations", "capabilities", "upload_mode",
        "collision", "headers", "access", "retention", "delete_scope", "reference_out",
    }
    assert plan["executable"] is True and plan["blocking_reasons"] == []
    assert plan["target_ref"] == "project:images" and plan["target_source"] == "cli"
    assert plan["endpoint"] == "https://s3.amazonaws.com" and plan["addressing"] == "virtual"
    assert plan["object_key"] == "website-images/cover.png"
    assert plan["source"] == {"path": str(source.absolute()), "size": 3}
    assert plan["remote_operations"] == ["PutObject"]
    assert [entry["operation"] for entry in plan["capabilities"]] == ["PutObject", "PresignGetObject"]
    assert plan["upload_mode"] == "single-put"
    assert plan["collision"] == {"policy": "replace", "max_attempts": 1}
    assert plan["headers"] == {"content_type": "image/png", "cache_control": None, "content_disposition": None}
    assert plan["access"] == {
        "mode": "private", "url_kind": "presigned", "presign_expires_seconds": 3600,
        "presign_effective_seconds": 3600, "public_base_url": None,
    }
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()
    assert "project-secret-value" not in output.out + output.err


def test_private_single_put_creates_checkpoint_and_returns_object_reference(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        return Response(200, headers={"x-amz-version-id": "version-1"})

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=transport, now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 0 and result["status"] == "ok" and result["object_written"] is True
    assert len(calls) == 1
    assert calls[0][0] == "PUT"
    assert calls[0][1] == "https://project-artifacts.s3.amazonaws.com/website-images/cover.png"
    assert calls[0][3] == b"png-bytes"
    assert calls[0][2]["content-length"] == "9"
    assert calls[0][2]["content-type"] == "image/png"
    assert calls[0][2]["authorization"].startswith("AWS4-HMAC-SHA256 ")
    reference = result["object_reference"]
    assert reference["location"] == {
        "provider": "aws-s3", "endpoint": "https://s3.amazonaws.com",
        "addressing": "virtual", "region": "us-east-1", "bucket": "project-artifacts",
        "key": "website-images/cover.png", "version_id": "version-1",
    }
    assert result["url_kind"] == "presigned" and "X-Amz-Signature=" in result["url"]
    assert result["expires_at"] == "2026-07-22T13:00:00Z"
    assert result["checkpoint_id"] is None and result["plan"] is None
    assert "checkpoint_id=" in output.err
    checkpoint_dir = tmp_path / ".s3-upload" / "checkpoints"
    assert list(checkpoint_dir.glob("*.json")) == []
    assert "project-secret-value" not in output.out + output.err


def test_single_put_rechecks_temporary_credential_before_result_presign(tmp_path):
    configure(tmp_path, credential_expires_at="2026-07-22T12:02:00Z")
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:images",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    dry_run = planning.build_upload_dry_run(
        resolved=resolved,
        file_path=str(source),
        explicit_key=None,
        content_type=None,
        cache_control=None,
        content_disposition=None,
        presign_expires=None,
        reference_out=None,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        allow_insecure_http=False,
        now=NOW,
    )
    times = iter((NOW, NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)))
    calls = []

    outcome = operations.execute_single_put(
        resolved=resolved,
        plan=dry_run.plan,
        transport=lambda *args: (calls.append(args) or Response(200)),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=lambda: next(times),
        checkpoint_notice=lambda _value: None,
        source=dry_run.source,
    )

    assert len(calls) == 1
    assert outcome.result["status"] == "partial_success"
    assert outcome.result["object_written"] is True
    assert outcome.result["url"] is None
    assert outcome.result["checkpoint_id"] == outcome.checkpoint_id
    assert outcome.retain_checkpoint is True
    assert outcome.store.load(outcome.checkpoint_id)["state"] == "complete"


def test_text_upload_presign_failure_reports_recoverable_checkpoint(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")
    real_presign = operations.presign_get
    calls = []

    def fail_presign(*args, **kwargs):
        raise operations.OperationError("injected presign failure")

    monkeypatch.setattr(operations, "presign_get", fail_presign)
    first_rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(200)),
        now=NOW,
    )

    first_output = capsys.readouterr()
    checkpoints = list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json"))
    assert first_rc == 1
    assert first_output.out == ""
    assert len(checkpoints) == 1
    checkpoint_id = checkpoints[0].stem
    assert f"partial_success checkpoint_id={checkpoint_id}" in first_output.err
    assert "checkpoint_id=none" not in first_output.err

    monkeypatch.setattr(operations, "presign_get", real_presign)
    recovered_rc = upload.main(
        ["reconcile", "--checkpoint", checkpoint_id],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    recovered_output = capsys.readouterr()
    assert recovered_rc == 0
    assert recovered_output.out.startswith("https://")
    assert len(calls) == 1
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_single_put_expiry_before_first_signature_leaves_no_checkpoint_or_request(
    tmp_path
):
    configure(tmp_path, credential_expires_at="2026-07-22T12:02:00Z")
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:images",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    dry_run = planning.build_upload_dry_run(
        resolved=resolved,
        file_path=str(source),
        explicit_key=None,
        content_type=None,
        cache_control=None,
        content_disposition=None,
        presign_expires=None,
        reference_out=None,
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        allow_insecure_http=False,
        now=NOW,
    )
    times = iter((NOW, NOW + timedelta(seconds=60)))
    calls = []

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.execute_single_put(
            resolved=resolved,
            plan=dry_run.plan,
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=lambda: next(times),
            checkpoint_notice=lambda _value: None,
            source=dry_run.source,
        )

    assert calls == []
    checkpoint_dir = tmp_path / ".s3-upload" / "checkpoints"
    assert not checkpoint_dir.exists() or list(checkpoint_dir.glob("*.json")) == []


def test_single_put_consumes_the_source_descriptor_opened_during_planning(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    source = tmp_path / "cover.bin"
    replacement = tmp_path / "replacement.bin"
    source.write_bytes(b"original")
    replacement.write_bytes(b"replaced")
    real_build = upload.build_upload_dry_run

    def swap_after_planning(**kwargs):
        planned = real_build(**kwargs)
        replacement.replace(source)
        return planned

    monkeypatch.setattr(upload, "build_upload_dry_run", swap_after_planning)
    bodies = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda method, url, headers, body: (
            bodies.append(body) or Response(200)
        ),
        now=NOW,
    )

    assert rc == 0
    json.loads(capsys.readouterr().out)
    assert bodies == [b"original"]


def test_url_from_object_reference_presigns_current_key_without_remote_request(tmp_path, capsys):
    configure(tmp_path)
    reference = build_object_reference(
        target_ref="project:images",
        target=parse_target(target(), expected_scope="project"),
        key="website-images/cover.png",
        version_id="captured-version",
    )
    reference_file = tmp_path / "reference.json"
    reference_file.write_bytes(serialize_object_reference(reference))
    reference_file.chmod(0o600)
    calls = []

    rc = upload.main(
        ["url", "--reference-file", str(reference_file), "--json", "--presign-expires", "120"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args), now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["operation"] == "url" and result["status"] == "ok"
    assert result["object_reference"] == reference
    assert "versionId" not in result["url"]
    assert "X-Amz-Expires=120" in result["url"]
    assert result["expires_at"] == "2026-07-22T12:02:00Z"
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_url_presign_rechecks_temporary_credential_at_signing_time(tmp_path):
    configure(tmp_path, credential_expires_at="2026-07-22T12:02:00Z")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:images",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    reference = build_object_reference(
        target_ref="project:images",
        target=resolved.target,
        key="website-images/cover.png",
    )

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.generate_object_url(
            resolved=resolved,
            reference=reference,
            presign_expires=None,
            now=lambda: NOW + timedelta(seconds=60),
        )


def test_url_rejects_object_reference_whose_location_does_not_match_its_fingerprint(
    tmp_path, capsys
):
    configure(tmp_path)
    reference = build_object_reference(
        target_ref="project:images",
        target=parse_target(target(), expected_scope="project"),
        key="website-images/cover.png",
    )
    reference["location"]["bucket"] = "forged-artifacts"
    reference_file = tmp_path / "forged-reference.json"
    reference_file.write_text(
        json.dumps(reference, separators=(",", ":")), encoding="utf-8"
    )
    reference_file.chmod(0o600)
    calls = []

    rc = upload.main(
        ["url", "--reference-file", str(reference_file), "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert calls == []


def test_reference_identifier_is_revalidated_against_resolved_credentials(
    tmp_path, capsys
):
    configure(tmp_path)
    reflected = "prefix-project-secret-value-suffix"
    reference = build_object_reference(
        target_ref="project:images",
        target=parse_target(target(), expected_scope="project"),
        key="website-images/cover.png",
        version_id=reflected,
    )
    reference_file = tmp_path / "reference.json"
    reference_file.write_bytes(serialize_object_reference(reference))
    reference_file.chmod(0o600)

    rc = upload.main(
        ["url", "--reference-file", str(reference_file), "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected request")),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert reflected not in output.err


def test_delete_reference_identifier_is_revalidated_before_plan_output(
    tmp_path, capsys
):
    configure(tmp_path)
    reflected = "prefix-PROJECTKEY1234-suffix"
    reference = build_object_reference(
        target_ref="project:images",
        target=parse_target(target(), expected_scope="project"),
        key="website-images/cover.png",
        version_id=reflected,
    )
    reference_file = tmp_path / "reference.json"
    reference_file.write_bytes(serialize_object_reference(reference))
    reference_file.chmod(0o600)
    calls = []

    rc = upload.main(
        ["delete", "--reference-file", str(reference_file), "--dry-run", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert reflected not in output.err
    assert calls == []


def test_delete_dry_run_is_scope_specific_and_blocked_without_capability(tmp_path, capsys):
    configure(tmp_path)
    reference = build_object_reference(
        target_ref="project:images",
        target=parse_target(target(), expected_scope="project"),
        key="website-images/cover.png",
        version_id="captured-version",
    )
    reference_file = tmp_path / "reference.json"
    reference_file.write_bytes(serialize_object_reference(reference))
    reference_file.chmod(0o600)
    calls = []

    rc = upload.main(
        ["delete", "--reference-file", str(reference_file), "--dry-run", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args), now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 2 and result["operation"] == "delete" and result["status"] == "dry_run"
    assert result["object_reference"] == reference
    assert result["object_written"] is None and result["delete_scope"] == "exact-version"
    plan = result["plan"]
    assert plan["executable"] is False
    assert plan["blocking_reasons"] == ["delete_capability_missing"]
    assert plan["remote_operations"] == ["DeleteObjectVersion"]
    assert [entry["operation"] for entry in plan["capabilities"]] == [
        "DeleteObjectVersion", "ObserveDeleteVersion",
    ]
    assert plan["delete_scope"] == "exact-version"
    assert plan["source"] is None and plan["collision"] is None and plan["headers"] is None
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_unknown_put_reconcile_never_repeats_mutation_without_observer_capability(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")
    calls = []

    def disconnected(*args):
        calls.append(args)
        raise OSError("connection lost")

    first_rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=disconnected, now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["status"] == "ambiguous"
    assert first["checkpoint_id"] is not None and len(calls) == 1

    reconcile_rc = upload.main(
        ["reconcile", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected observer")),
        now=NOW,
    )
    recovered = json.loads(capsys.readouterr().out)
    assert reconcile_rc == 1
    assert recovered["operation"] == "reconcile" and recovered["status"] == "ambiguous"
    assert recovered["checkpoint_id"] == first["checkpoint_id"]
    assert len(calls) == 1


def test_checkpoint_identifier_is_revalidated_after_target_resolution(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"png-bytes")

    first_rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(OSError("response lost")),
        now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["checkpoint_id"]

    reflected = "prefix-project-secret-value-suffix"
    checkpoint_path = (
        tmp_path / ".s3-upload" / "checkpoints" / (first["checkpoint_id"] + ".json")
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["state"] = "complete"
    checkpoint["object_reference_draft"]["location"]["version_id"] = reflected
    checkpoint_path.write_text(
        json.dumps(checkpoint, separators=(",", ":")), encoding="utf-8"
    )
    checkpoint_path.chmod(0o600)

    rc = upload.main(
        ["reconcile", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected request")),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 1 and output.out == ""
    assert reflected not in output.err


def test_public_expiring_upload_uses_declared_base_without_presign_fallback(tmp_path, capsys):
    public_target = target()
    public_target["prefix"] = "public-images/"
    public_target["access"] = {
        "mode": "public", "public_base_url": "https://cdn.example/base/",
        "presign_expires_seconds": None,
    }
    public_target["retention"] = {"mode": "expire", "days": 7}
    public_target["setup"]["exclusive_prefix"] = True
    configure(tmp_path, public_target)
    source = tmp_path / "中 +%2E.png"
    source.write_bytes(b"public")

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: Response(200), now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["url_kind"] == "public" and result["expires_at"] is None
    assert result["url"] == "https://cdn.example/base/public-images/%E4%B8%AD%20%2B%252E.png"
    assert "X-Amz-" not in result["url"]
    assert result["retention"] == {
        "mode": "expire", "days": 7, "enforcement": "external-unverified",
    }


def test_header_overrides_are_identical_in_plan_checkpoint_and_wire(tmp_path, capsys):
    configured = target()
    configured["object_headers"] = {
        "cache_control": "public, max-age=3600",
        "content_disposition": 'inline; filename="default.png"',
    }
    configure(tmp_path, configured)
    source = tmp_path / "cover.png"
    source.write_bytes(b"headers")
    calls = []

    rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:images", "--json",
            "--content-type", "image/webp", "--cache-control", "private, max-age=0",
            "--content-disposition", "attachment; filename*=UTF-8''cover.webp",
        ],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(200)), now=NOW,
    )

    assert rc == 0
    json.loads(capsys.readouterr().out)
    wire = calls[0][2]
    assert wire["content-type"] == "image/webp"
    assert wire["cache-control"] == "private, max-age=0"
    assert wire["content-disposition"] == "attachment; filename*=UTF-8''cover.webp"
    assert all(value in wire["authorization"] for value in ("content-type", "cache-control", "content-disposition"))


def test_reflected_version_id_becomes_ambiguous_without_persisting_secret(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"version")

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:images", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: Response(200, headers={"x-amz-version-id": "prefix-project-secret-value"}),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 1 and result["status"] == "ambiguous" and result["checkpoint_id"]
    assert "project-secret-value" not in output.out + output.err
    checkpoint = tmp_path / ".s3-upload" / "checkpoints" / (result["checkpoint_id"] + ".json")
    assert "project-secret-value" not in checkpoint.read_text(encoding="utf-8")


def test_reference_out_cas_drift_after_put_is_partial_and_never_retries(tmp_path, capsys):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"reference")
    reference_out = tmp_path / "object-reference.json"
    calls = []

    def transport(*args):
        calls.append(args)
        reference_out.write_text("concurrent value", encoding="utf-8")
        reference_out.chmod(0o600)
        return Response(200)

    rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:images", "--json",
            "--reference-out", str(reference_out),
        ],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=transport, now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["status"] == "partial_success"
    assert result["object_written"] is True and result["object_reference"] is not None
    assert result["url"] is not None and result["checkpoint_id"] is not None
    assert len(calls) == 1
    assert reference_out.read_text(encoding="utf-8") == "concurrent value"


def test_reconcile_complete_retries_only_snapshotted_reference_output(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path)
    source = tmp_path / "cover.png"
    source.write_bytes(b"reference")
    reference_out = tmp_path / "object-reference.json"
    real_write = operations.write_reference_output
    attempts = []

    def fail_first(snapshot, reference):
        attempts.append(snapshot.value["path"])
        raise operations.ArtifactError("injected local write failure")

    monkeypatch.setattr(operations, "write_reference_output", fail_first)
    first_rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:images", "--json",
            "--reference-out", str(reference_out),
        ],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: Response(200), now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["status"] == "partial_success"
    assert first["checkpoint_id"] is not None and not reference_out.exists()

    monkeypatch.setattr(operations, "write_reference_output", real_write)
    recovered_rc = upload.main(
        ["reconcile", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("unexpected remote request")),
        now=NOW,
    )
    recovered = json.loads(capsys.readouterr().out)
    assert recovered_rc == 0 and recovered["operation"] == "reconcile"
    assert recovered["status"] == "ok"
    assert reference_out.read_bytes() == serialize_object_reference(recovered["object_reference"])
    assert attempts == [str(reference_out)]
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []
