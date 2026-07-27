"""Caller-contract tests for the nine-field closed result (Task 2.1).

The v1 result schema is extended -- not mirrored by a mapping layer -- to
carry the minimal caller contract: `status`, `object_written`, `url`,
`url_kind`, `expires_at`, `retention`, the remote identity container
(`remote.key` + `remote.size` + `remote.sha256`), `checkpoint`, and the
`next_action` + `retry_safety` pair. Every field is always present;
inapplicable values are explicit null. `checkpoint`, `next_action` and
`retry_safety` are derived at construction so a caller can never build a
result whose fields contradict each other.
"""

from datetime import datetime, timezone
import hashlib
import json

import pytest

import upload
from results import OPERATIONS, ResultError, build_result, validate_result
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
RETAIN = {"mode": "retain", "days": None, "enforcement": "external-unverified"}
CHECKPOINT_ID = "123456789abc4def8123456789abcdef"
LOCKED_KEYS = [
    "schema_version", "operation", "status", "object_written", "object_reference",
    "url", "url_kind", "expires_at", "retention", "delete_scope",
    "deleted_version_id", "checkpoint_id", "plan",
    "remote", "checkpoint", "next_action", "retry_safety",
]
CONTENT_SHA256 = hashlib.sha256(b"content").hexdigest()


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
        "collision": "replace",
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


def test_result_key_order_is_the_locked_v1_superset():
    result = build_result("upload", "ok", object_written=True, retention=RETAIN)
    assert list(result) == LOCKED_KEYS


def test_pre_request_rejection_carries_all_nine_fields_as_explicit_nulls():
    result = build_result("upload", "not_started", object_written=False, retention=RETAIN)
    assert result["status"] == "not_started"
    assert result["object_written"] is False
    assert result["url"] is None and result["url_kind"] is None
    assert result["expires_at"] is None
    assert result["retention"] == RETAIN
    assert result["remote"] == {"key": None, "size": None, "sha256": None}
    assert result["checkpoint"] is None
    assert result["next_action"] is None
    assert result["retry_safety"] == "safe"


def test_checkpoint_field_mirrors_checkpoint_id():
    result = build_result(
        "upload", "ambiguous", object_written=None, retention=RETAIN,
        checkpoint_id=CHECKPOINT_ID,
    )
    assert result["checkpoint"] == CHECKPOINT_ID

    result["checkpoint"] = None
    with pytest.raises(ResultError, match="checkpoint"):
        validate_result(result)


def test_next_action_is_reconcile_exactly_when_a_checkpoint_is_retained():
    ambiguous = build_result(
        "upload", "ambiguous", object_written=None, retention=RETAIN,
        checkpoint_id=CHECKPOINT_ID,
    )
    assert ambiguous["next_action"] == "reconcile"

    ok = build_result("upload", "ok", object_written=True, retention=RETAIN)
    assert ok["next_action"] is None

    ok["next_action"] = "reconcile"
    with pytest.raises(ResultError, match="next_action"):
        validate_result(ok)
    ambiguous["next_action"] = None
    with pytest.raises(ResultError, match="next_action"):
        validate_result(ambiguous)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    (
        (dict(operation="upload", status="ok", object_written=True), None),
        (dict(operation="upload", status="adopted", object_written=False), None),
        (dict(operation="abort", status="aborted"), None),
        (dict(operation="upload", status="collision", object_written=False), "safe"),
        (dict(operation="reconcile", status="not_started", object_written=False), "safe"),
        (dict(operation="reconcile", status="not_deleted"), "safe"),
        (
            dict(
                operation="upload", status="ambiguous", object_written=None,
                checkpoint_id=CHECKPOINT_ID,
            ),
            "unsafe",
        ),
        (
            dict(
                operation="upload", status="partial_success", object_written=True,
                checkpoint_id=CHECKPOINT_ID,
            ),
            "unsafe",
        ),
    ),
)
def test_retry_safety_is_derived_from_the_status(kwargs, expected):
    kwargs = dict(kwargs)
    operation = kwargs.pop("operation")
    status = kwargs.pop("status")
    result = build_result(operation, status, retention=RETAIN, **kwargs)
    assert result["retry_safety"] == expected

    result["retry_safety"] = "unsafe" if expected != "unsafe" else "safe"
    with pytest.raises(ResultError, match="retry_safety"):
        validate_result(result)


def test_remote_container_is_closed_and_bound_to_the_reference():
    reference = {"location": {"key": "objects/report.bin"}}
    bound = build_result(
        "upload", "ok", object_written=True, retention=RETAIN,
        object_reference=reference, validate_reference=False,
    )
    assert bound["remote"] == {"key": "objects/report.bin", "size": None, "sha256": None}

    explicit = build_result(
        "upload", "ok", object_written=True, retention=RETAIN,
        object_reference=reference,
        remote={"key": "objects/report.bin", "size": 7, "sha256": CONTENT_SHA256},
        validate_reference=False,
    )
    assert explicit["remote"]["size"] == 7

    with pytest.raises(ResultError, match="remote"):
        build_result(
            "upload", "ok", object_written=True, retention=RETAIN,
            object_reference=reference,
            remote={"key": "objects/other.bin", "size": None, "sha256": None},
            validate_reference=False,
        )
    with pytest.raises(ResultError, match="remote"):
        build_result(
            "upload", "ok", object_written=True, retention=RETAIN,
            remote={"key": None, "size": 7, "sha256": None},
        )
    with pytest.raises(ResultError, match="remote"):
        build_result(
            "upload", "ok", object_written=True, retention=RETAIN,
            object_reference=reference,
            remote={"key": "objects/report.bin", "size": None, "sha256": "junk"},
            validate_reference=False,
        )
    with pytest.raises(ResultError, match="remote"):
        build_result(
            "upload", "ok", object_written=True, retention=RETAIN,
            remote={"key": None, "size": None, "sha256": None, "extra": 1},
        )


def test_ambiguous_result_keeps_the_remote_identity_fully_null():
    with pytest.raises(ResultError, match="remote"):
        build_result(
            "upload", "ambiguous", object_written=None, retention=RETAIN,
            checkpoint_id=CHECKPOINT_ID,
            remote={"key": "objects/report.bin", "size": None, "sha256": None},
        )


def test_status_object_written_contradictions_are_rejected_at_construction():
    with pytest.raises(ResultError, match="object_written"):
        build_result("upload", "ok", object_written=False, retention=RETAIN)
    with pytest.raises(ResultError, match="object_written"):
        build_result("upload", "ok", object_written=None, retention=RETAIN)
    with pytest.raises(ResultError):
        build_result("upload", "adopted", object_written=True, retention=RETAIN)
    with pytest.raises(ResultError):
        build_result("resume", "ok", object_written=False, retention=RETAIN)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_not_started_must_not_claim_a_written_object(operation):
    # not_started is the "no request reached the remote" status: it carries
    # retry_safety="safe", so a written object would let one result claim
    # both "the object is there" and "a blind retry is provably safe".
    # Every operation is covered because build_result takes the operation as
    # a free parameter -- the lock cannot live in one operation's branch.
    with pytest.raises(ResultError, match="not_started"):
        build_result(operation, "not_started", object_written=True, retention=RETAIN)
    truthful = build_result(
        operation, "not_started", object_written=False, retention=RETAIN,
    )
    assert truthful["object_written"] is False and truthful["retry_safety"] == "safe"


def test_collision_may_retain_a_checkpoint_for_an_unclosed_session():
    # retry_safety="safe" and a retained checkpoint are not contradictory: a
    # conditional CompleteMultipartUpload that loses the race writes no
    # object, yet leaves a live multipart session that only an explicit abort
    # closes. That pair stays constructible on purpose.
    result = build_result(
        "upload", "collision", object_written=False, retention=RETAIN,
        checkpoint_id=CHECKPOINT_ID,
    )
    assert result["retry_safety"] == "safe"
    assert result["checkpoint"] == CHECKPOINT_ID
    assert result["next_action"] == "reconcile"


def test_cli_upload_success_reports_the_remote_identity(tmp_path, capsys):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: Response(200),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "ok"
    assert result["remote"] == {
        "key": "objects/report.bin", "size": 7, "sha256": CONTENT_SHA256,
    }
    assert result["checkpoint"] is None
    assert result["next_action"] is None
    assert result["retry_safety"] is None


def test_cli_ambiguous_upload_reports_reconcile_as_the_next_action(tmp_path, capsys):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")

    rc = upload.main(
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (_ for _ in ()).throw(OSError("response lost")),
        now=NOW,
    )

    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["status"] == "ambiguous"
    assert result["checkpoint"] == result["checkpoint_id"] is not None
    assert result["next_action"] == "reconcile"
    assert result["retry_safety"] == "unsafe"
    assert result["remote"] == {"key": None, "size": None, "sha256": None}
