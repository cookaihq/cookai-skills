"""Caller-contract tests for atomic no-overwrite (`collision=reject`).

Task 1.1 of the minimal caller contract: the aws-s3 and cloudflare-r2
baseline registries carry ConditionalPutObject as an enabled capability
(backed by both providers' official conditional-write documentation), the
conditional Put goes out with `If-None-Match: *` inside the signed header
set, and a remote 412 is classified as a collision -- never as ambiguous.
"""

from datetime import datetime, timezone
import json

import pytest

import upload
from capabilities import (
    ContractKey,
    OperationShape,
    build_v2_baseline_registry,
    plan_operation,
)
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def contract_key(**overrides):
    values = {
        "schema_version": 1,
        "provider": "aws-s3",
        "scheme": "https",
        "endpoint_family": "aws-public",
        "region_class": "us-east-1",
        "network_class": "public",
        "addressing": "virtual",
        "signing_profile": "sigv4-s3",
        "payload_profile": "fixed-content-length",
    }
    values.update(overrides)
    return ContractKey(**values)


def aws_key():
    return contract_key()


def r2_key():
    return contract_key(
        provider="cloudflare-r2",
        endpoint_family="cloudflare-r2-public",
        region_class="auto",
        addressing="path",
    )


def target(**overrides):
    value = {
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
        "collision": "reject",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": None,
            "part_size_bytes": None,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }
    value.update(overrides)
    return value


def configure(project, target_value):
    directory = project / ".s3-upload" / "targets"
    directory.mkdir(parents=True)
    (directory / "objects.json").write_text(json.dumps(target_value), encoding="utf-8")
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


@pytest.mark.parametrize(
    "key,evidence_id",
    [
        (aws_key(), "v1-aws-conditional-put"),
        (r2_key(), "v1-r2-conditional-put"),
    ],
)
def test_baseline_presets_enable_conditional_put_with_named_evidence(key, evidence_id):
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    capability = registry.lookup(key, "ConditionalPutObject")

    assert capability.state == "enabled"
    assert capability.evidence_id == evidence_id


def test_asserted_custom_baseline_keeps_conditional_put_disabled():
    custom = contract_key(
        provider="custom",
        endpoint_family="exact-" + "1" * 64,
        region_class="exact-" + "2" * 64,
        addressing="path",
    )
    registry = build_v2_baseline_registry(asserted_custom_contracts=(custom,))

    capability = registry.lookup(custom, "ConditionalPutObject")

    assert capability.state == "disabled"
    assert capability.evidence_id == "v2-baseline-disabled"


@pytest.mark.parametrize("key", [aws_key(), r2_key()])
@pytest.mark.parametrize("collision", ["unique", "reject"])
def test_conditional_single_put_is_executable_on_baseline_presets(key, collision):
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision=collision,
        ),
        contract_key=key,
        registry=registry,
    )

    assert plan.blocking_reasons == ()
    assert plan.executable is True
    assert plan.remote_operations == ("PutObject",)


def test_reject_upload_on_real_aws_baseline_sends_atomic_condition(
    tmp_path, capsys
):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(200)),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][0] == "PUT"
    assert calls[0][2]["if-none-match"] == "*"


def test_reject_upload_maps_412_to_collision_not_ambiguous(tmp_path, capsys):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
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
    assert result["checkpoint_id"] is None
    assert len(calls) == 1
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_cli_collision_override_turns_a_replace_target_into_reject(
    tmp_path, capsys
):
    configure(tmp_path, target(collision="replace"))
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    calls = []

    rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--collision", "reject", "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(412)),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 4 and result["status"] == "collision"
    assert len(calls) == 1
    assert calls[0][2]["if-none-match"] == "*"


def test_cli_collision_override_is_visible_in_the_dry_run_plan(tmp_path, capsys):
    configure(tmp_path, target(collision="replace"))
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")

    rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--collision", "reject", "--dry-run", "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: pytest.fail("dry run must not touch the network"),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["plan"]["executable"] is True
    assert result["plan"]["collision"] == {"policy": "reject", "max_attempts": 1}


def test_replace_upload_still_sends_no_condition_header(tmp_path, capsys):
    configure(tmp_path, target(collision="replace"))
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    calls = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(200)),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "ok"
    assert len(calls) == 1
    assert "if-none-match" not in calls[0][2]
