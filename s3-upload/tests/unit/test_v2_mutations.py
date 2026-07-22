from datetime import datetime, timedelta, timezone
import json

import pytest

import operations
import planning
import upload
from artifacts import build_object_reference, serialize_object_reference
from capabilities import Capability, CapabilityRegistry
from resolver import resolve_target
from s3 import Response
from v2_schema import parse_target


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def target(*, collision="replace"):
    return {
        "schema_version": 1,
        "credential": "project:main-key",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "objects/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 3600,
        },
        "retention": {"mode": "retain", "days": None},
        "collision": collision,
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": None,
            "part_size_bytes": None,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }


def configure(project, target_value, *, credential_expires_at=None):
    directory = project / ".s3-upload" / "targets"
    directory.mkdir(parents=True)
    (directory / "objects.json").write_text(json.dumps(target_value), encoding="utf-8")
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
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON="
        + json.dumps(credentials, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)


def enable(monkeypatch, *operation_names):
    def registry(_target, key):
        capabilities = tuple(
            Capability(name, "enabled", "synthetic-command-test")
            for name in operation_names
        )
        return CapabilityRegistry(((key, capabilities),))

    monkeypatch.setattr(planning, "registry_for_target", registry)
    monkeypatch.setattr(operations, "registry_for_target", registry)


def reference_file(project, target_value, *, version_id):
    reference = build_object_reference(
        target_ref="project:objects",
        target=parse_target(target_value, expected_scope="project"),
        key="objects/report.bin",
        version_id=version_id,
    )
    path = project / "reference.json"
    path.write_bytes(serialize_object_reference(reference))
    path.chmod(0o600)
    return path, reference


def test_reject_single_put_uses_one_conditional_request_and_reports_collision(
    tmp_path, capsys, monkeypatch
):
    configured = target(collision="reject")
    configure(tmp_path, configured)
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(monkeypatch, "PutObject", "ConditionalPutObject", "PresignGetObject")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(412)),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 4
    assert result["status"] == "collision"
    assert result["object_written"] is False
    assert result["object_reference"]["location"]["key"] == "objects/report.bin"
    assert result["checkpoint_id"] is None
    assert len(calls) == 1
    assert calls[0][2]["if-none-match"] == "*"
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_unique_single_put_changes_key_only_after_verified_collision(
    tmp_path, capsys, monkeypatch
):
    configured = target(collision="unique")
    configure(tmp_path, configured)
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(monkeypatch, "PutObject", "ConditionalPutObject", "PresignGetObject")
    calls = []
    responses = iter((Response(412), Response(200)))

    def transport(*args):
        calls.append(args)
        return next(responses)

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=transport,
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "ok"
    assert len(calls) == 2
    first_key = calls[0][1].rsplit("/", 1)[-1]
    second_key = calls[1][1].rsplit("/", 1)[-1]
    assert first_key != second_key
    assert first_key.startswith("report-") and first_key.endswith(".bin")
    assert second_key.startswith("report-") and second_key.endswith(".bin")
    assert result["object_reference"]["location"]["key"].endswith(second_key)
    assert all(call[2]["if-none-match"] == "*" for call in calls)


def test_unique_put_rechecks_temporary_credential_before_each_signed_attempt(
    tmp_path, monkeypatch
):
    configured = target(collision="unique")
    configure(
        tmp_path,
        configured,
        credential_expires_at="2026-07-22T12:02:00Z",
    )
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(monkeypatch, "PutObject", "ConditionalPutObject", "PresignGetObject")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:objects",
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

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.execute_single_put(
            resolved=resolved,
            plan=dry_run.plan,
            transport=lambda *args: (calls.append(args) or Response(412)),
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            now=lambda: next(times),
            checkpoint_notice=lambda _value: None,
            source=dry_run.source,
        )

    assert len(calls) == 1


def test_unique_single_put_stops_at_configured_collision_bound(
    tmp_path, capsys, monkeypatch
):
    configured = target(collision="unique")
    configure(tmp_path, configured)
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(monkeypatch, "PutObject", "ConditionalPutObject", "PresignGetObject")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(412)), now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 4 and result["status"] == "collision"
    assert len(calls) == configured["retry"]["collision_max_attempts"]
    assert len({call[1] for call in calls}) == len(calls)
    assert result["object_reference"]["location"]["key"] in calls[-1][1]


def test_unique_single_put_never_retries_an_unverified_conflict(
    tmp_path, capsys, monkeypatch
):
    configured = target(collision="unique")
    configure(tmp_path, configured)
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(monkeypatch, "PutObject", "ConditionalPutObject", "PresignGetObject")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(409)), now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 1 and output.out == ""
    assert len(calls) == 1
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_exact_version_delete_is_checkpointed_and_sends_version_once(
    tmp_path, capsys, monkeypatch
):
    configured = target()
    configure(tmp_path, configured)
    path, reference = reference_file(tmp_path, configured, version_id="version-7")
    enable(monkeypatch, "DeleteObjectVersion", "ObserveDeleteVersion")
    calls = []

    rc = upload.main(
        ["delete", "--reference-file", str(path), "--confirm-delete", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(204)),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["status"] == "deleted"
    assert result["object_reference"] == reference
    assert result["delete_scope"] == "exact-version"
    assert result["deleted_version_id"] == "version-7"
    assert result["checkpoint_id"] is None
    assert len(calls) == 1 and calls[0][0] == "DELETE"
    assert calls[0][1].endswith("/objects/report.bin?versionId=version-7")
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_delete_rechecks_temporary_credential_before_signing(tmp_path, monkeypatch):
    configured = target()
    configure(
        tmp_path,
        configured,
        credential_expires_at="2026-07-22T12:02:00Z",
    )
    _path, reference = reference_file(tmp_path, configured, version_id="version-7")
    enable(monkeypatch, "DeleteObjectVersion", "ObserveDeleteVersion")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:objects",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    dry_run = planning.build_delete_dry_run(
        resolved=resolved,
        reference=reference,
        allow_insecure_http=False,
        now=NOW,
    )
    times = iter((NOW, NOW + timedelta(seconds=60)))
    calls = []

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.execute_delete(
            resolved=resolved,
            reference=reference,
            plan=dry_run.plan,
            transport=lambda *args: calls.append(args),
            project_root=str(tmp_path),
            now=lambda: next(times),
            checkpoint_notice=lambda _value: None,
        )

    assert calls == []
    checkpoint_dir = tmp_path / ".s3-upload" / "checkpoints"
    assert not checkpoint_dir.exists() or list(checkpoint_dir.glob("*.json")) == []


def test_unknown_current_key_delete_reconciles_with_head_and_never_redeletes(
    tmp_path, capsys, monkeypatch
):
    configured = target()
    configure(tmp_path, configured)
    path, reference = reference_file(tmp_path, configured, version_id=None)
    enable(monkeypatch, "DeleteObjectCurrentKey", "ObserveDeleteCurrentKey")
    calls = []

    def disconnected(*args):
        calls.append(args)
        raise OSError("response lost")

    first_rc = upload.main(
        ["delete", "--reference-file", str(path), "--confirm-delete", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=disconnected,
        now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["status"] == "ambiguous"
    assert first["checkpoint_id"] is not None
    assert len(calls) == 1 and calls[0][0] == "DELETE"

    def observe(method, url, headers, body):
        calls.append((method, url, headers, body))
        return Response(404)

    reconcile_rc = upload.main(
        ["reconcile", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=observe,
        now=NOW,
    )
    recovered = json.loads(capsys.readouterr().out)
    assert reconcile_rc == 0
    assert recovered["operation"] == "reconcile"
    assert recovered["status"] == "deleted"
    assert recovered["object_reference"] == reference
    assert recovered["delete_scope"] == "current-key"
    assert recovered["checkpoint_id"] is None
    assert [call[0] for call in calls] == ["DELETE", "HEAD"]


def test_delete_reconcile_rechecks_temporary_credential_before_head(
    tmp_path, monkeypatch
):
    configured = target()
    configure(
        tmp_path,
        configured,
        credential_expires_at="2026-07-22T12:02:00Z",
    )
    _path, reference = reference_file(tmp_path, configured, version_id=None)
    enable(monkeypatch, "DeleteObjectCurrentKey", "ObserveDeleteCurrentKey")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:objects",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    dry_run = planning.build_delete_dry_run(
        resolved=resolved,
        reference=reference,
        allow_insecure_http=False,
        now=NOW,
    )
    first = operations.execute_delete(
        resolved=resolved,
        reference=reference,
        plan=dry_run.plan,
        transport=lambda *args: (_ for _ in ()).throw(OSError("response lost")),
        project_root=str(tmp_path),
        now=NOW,
        checkpoint_notice=lambda _value: None,
    )
    checkpoint = first.store.load(first.checkpoint_id)
    calls = []

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.reconcile_delete(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            now=lambda: NOW + timedelta(seconds=60),
        )

    assert calls == []
    assert first.store.load(first.checkpoint_id)["state"] == "delete_unknown"


def test_put_reconcile_rechecks_temporary_credential_before_head(
    tmp_path, monkeypatch
):
    configured = target()
    configure(
        tmp_path,
        configured,
        credential_expires_at="2026-07-22T12:02:00Z",
    )
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    enable(
        monkeypatch,
        "PutObject",
        "PresignGetObject",
        "HeadObject",
        "ReservedMetadataRoundTrip",
    )
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:objects",
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
    first = operations.execute_single_put(
        resolved=resolved,
        plan=dry_run.plan,
        transport=lambda *args: (_ for _ in ()).throw(OSError("response lost")),
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
        checkpoint_notice=lambda _value: None,
        source=dry_run.source,
    )
    checkpoint = first.store.load(first.checkpoint_id)
    calls = []

    with pytest.raises(
        operations.OperationError,
        match="more than 60 whole seconds",
    ):
        operations.reconcile_put(
            resolved=resolved,
            checkpoint=checkpoint,
            store=first.store,
            transport=lambda *args: calls.append(args),
            config_home=str(tmp_path / "home"),
            now=lambda: NOW + timedelta(seconds=60),
        )

    assert calls == []
    assert first.store.load(first.checkpoint_id)["state"] == "put_unknown"


@pytest.mark.parametrize(
    ("version_id", "operations_enabled", "observer_status", "expected_status", "expected_rc"),
    (
        (None, ("DeleteObjectCurrentKey", "ObserveDeleteCurrentKey"), 200, "not_deleted", 1),
        (None, ("DeleteObjectCurrentKey", "ObserveDeleteCurrentKey"), 403, "ambiguous", 1),
        ("version-9", ("DeleteObjectVersion", "ObserveDeleteVersion"), 404, "deleted", 0),
    ),
)
def test_delete_reconcile_uses_only_scope_specific_observer_evidence(
    tmp_path,
    capsys,
    monkeypatch,
    version_id,
    operations_enabled,
    observer_status,
    expected_status,
    expected_rc,
):
    configured = target()
    configure(tmp_path, configured)
    path, _reference = reference_file(tmp_path, configured, version_id=version_id)
    enable(monkeypatch, *operations_enabled)
    calls = []

    first_rc = upload.main(
        ["delete", "--reference-file", str(path), "--confirm-delete", "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or (_ for _ in ()).throw(OSError("lost"))),
        now=NOW,
    )
    first = json.loads(capsys.readouterr().out)
    assert first_rc == 1 and first["status"] == "ambiguous"

    recovered_rc = upload.main(
        ["reconcile", "--checkpoint", first["checkpoint_id"], "--json"],
        environ={}, cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(observer_status)), now=NOW,
    )
    recovered = json.loads(capsys.readouterr().out)
    assert recovered_rc == expected_rc and recovered["status"] == expected_status
    assert [call[0] for call in calls] == ["DELETE", "HEAD"]
    if version_id is None:
        assert "versionId=" not in calls[-1][1]
    else:
        assert calls[-1][1].endswith("?versionId=" + version_id)
    checkpoint = tmp_path / ".s3-upload" / "checkpoints" / (first["checkpoint_id"] + ".json")
    assert checkpoint.exists() is (expected_status == "ambiguous")
