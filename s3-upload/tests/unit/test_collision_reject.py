"""Caller-contract tests for atomic no-overwrite (`collision=reject`).

Task 1.1 of the minimal caller contract: the aws-s3 and cloudflare-r2
baseline registries carry ConditionalPutObject as an enabled capability
(backed by both providers' official conditional-write documentation), the
conditional Put goes out with `If-None-Match: *` inside the signed header
set, and a remote 412 is classified as a collision -- never as ambiguous.

Task 1.2: a 412 under reject is followed by one presigned full-body GET;
only a size+SHA-256 double match turns the outcome into `adopted`
(object_written=false, exit 0, no second Put). Anything short of that
proof -- different bytes, different length, or a body that could not be
read in full -- ends as a collision with exit 4.
"""

from datetime import datetime, timezone
import json
from urllib.error import URLError

import pytest

import operations
import upload
from capabilities import (
    ContractKey,
    OperationShape,
    build_v2_baseline_registry,
    plan_operation,
)
from results import ResultError, build_result, exit_code_for_result
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


def test_reject_upload_maps_412_to_collision_not_ambiguous(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    # The adoption read (task 1.2) finds a different object, so the 412
    # stays what it is: a collision, never an ambiguous outcome.
    gets = install_opener(monkeypatch, body=b"someone-elses-object")
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
    assert len(gets) == 1
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_cli_collision_override_turns_a_replace_target_into_reject(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path, target(collision="replace"))
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    install_opener(monkeypatch, body=b"someone-elses-object")
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


class _Stream:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = (
            {"content-length": str(len(body))} if headers is None else headers
        )
        self._at = 0

    def read(self, size):
        chunk = self._body[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def install_opener(monkeypatch, *, body=None, exc=None):
    calls = []

    def opener(method, url, headers, timeout=30):
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        if exc is not None:
            raise exc
        return _Stream(body)

    monkeypatch.setattr(operations, "open_body_stream", opener)
    return calls


def run_reject_upload(tmp_path, put_status=412, **target_overrides):
    configure(tmp_path, target(**target_overrides))
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    puts = []

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (puts.append(args) or Response(put_status)),
        now=NOW,
    )
    return rc, puts


def test_reject_412_with_identical_remote_content_is_adopted(
    tmp_path, capsys, monkeypatch
):
    gets = install_opener(monkeypatch, body=b"content")

    rc, puts = run_reject_upload(tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["status"] == "adopted"
    assert result["object_written"] is False
    assert result["object_reference"]["location"]["key"] == "objects/report.bin"
    assert result["url_kind"] == "presigned" and result["url"] is not None
    # No second Put: the one conditional attempt is the only write request.
    assert len(puts) == 1 and puts[0][0] == "PUT"
    assert len(gets) == 1 and gets[0]["method"] == "GET"
    # V2 review legacy D1: the adoption GET must observably be a presigned
    # URL, so the signature parameters are asserted here, on the recorded
    # request itself.
    assert "X-Amz-Signature=" in gets[0]["url"]
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in gets[0]["url"]
    assert "X-Amz-Expires=" in gets[0]["url"]
    assert gets[0]["url"].split("?")[0].endswith("/objects/report.bin")
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []


def test_reject_412_with_same_size_different_bytes_is_collision(
    tmp_path, capsys, monkeypatch
):
    # Same length as the planned source, different content: only the SHA-256
    # comparison can tell them apart, so a size-only check would wrongly
    # adopt this object.
    gets = install_opener(monkeypatch, body=b"CONTENT")

    rc, puts = run_reject_upload(tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert rc == 4
    assert result["status"] == "collision"
    assert result["object_written"] is False
    assert len(puts) == 1
    assert len(gets) == 1


def test_reject_412_with_different_size_body_is_collision(
    tmp_path, capsys, monkeypatch
):
    gets = install_opener(monkeypatch, body=b"other-bytes")

    rc, puts = run_reject_upload(tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert rc == 4 and result["status"] == "collision"
    assert result["object_written"] is False
    assert len(puts) == 1 and len(gets) == 1


def test_reject_412_with_unreadable_remote_body_stays_collision(
    tmp_path, capsys, monkeypatch
):
    # An unreadable far end proves nothing either way; adoption must not be
    # claimed, and the caller-facing outcome stays the conservative
    # collision.
    gets = install_opener(monkeypatch, exc=URLError("connection lost"))

    rc, puts = run_reject_upload(tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert rc == 4 and result["status"] == "collision"
    assert result["object_written"] is False
    assert len(puts) == 1 and len(gets) == 1


def test_public_target_adoption_uses_presigned_get_and_public_result_url(
    tmp_path, capsys, monkeypatch
):
    gets = install_opener(monkeypatch, body=b"content")

    rc, puts = run_reject_upload(
        tmp_path,
        access={
            "mode": "public",
            "public_base_url": "https://cdn.example.com/",
            "presign_expires_seconds": None,
        },
        setup={"exclusive_prefix": True, "integration_test": False, "cors": None},
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "adopted"
    assert result["object_written"] is False
    assert result["url"] == "https://cdn.example.com/objects/report.bin"
    assert result["url_kind"] == "public"
    # Adoption evidence is still an authenticated full GET even when the
    # Target serves a public URL: the verification request itself is signed.
    assert len(gets) == 1
    assert "X-Amz-Signature=" in gets[0]["url"]
    assert len(puts) == 1


def test_adopted_result_contract_is_exit_zero_and_unwritten():
    adopted = build_result(
        "upload",
        "adopted",
        object_written=False,
        retention={"mode": None, "days": None, "enforcement": None},
    )
    assert exit_code_for_result(adopted) == 0

    with pytest.raises(ResultError):
        build_result(
            "upload",
            "adopted",
            object_written=True,
            retention={"mode": None, "days": None, "enforcement": None},
        )
    with pytest.raises(ResultError):
        build_result(
            "reconcile",
            "adopted",
            object_written=False,
            retention={"mode": None, "days": None, "enforcement": None},
        )


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
