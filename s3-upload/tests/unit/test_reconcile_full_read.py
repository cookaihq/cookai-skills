"""Caller-contract tests for the read-only put_unknown reconciliation (Task 3.1).

A `put_unknown` checkpoint (including `put_in_flight`, which reconcile
folds into it) is settled by one presigned full-body GET compared against
the checkpointed source size and SHA-256: a double match converges to a
successful result whose object_written stays null (this reconcile claims
no writer identity), a missing object (404) converges to not_started, and
everything else -- unreadable body, truncated read, different bytes --
stays the conservative ambiguous with the checkpoint retained. The whole
path issues zero write requests; the transport counter in every test is
independent of the opener that serves the GET.
"""

from datetime import datetime, timezone
import hashlib
from http.client import IncompleteRead
import json
from urllib.error import HTTPError, URLError

import pytest

import operations
import planning
import upload
from capabilities import Capability, CapabilityRegistry
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
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


def configure(project):
    directory = project / ".s3-upload" / "targets"
    directory.mkdir(parents=True)
    (directory / "objects.json").write_text(json.dumps(target()), encoding="utf-8")
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


class _Stream:
    def __init__(self, body, *, broken_after=None):
        self._body = body
        self.status = 200
        self.headers = {"content-length": str(len(body))}
        self._at = 0
        self._broken_after = broken_after

    def read(self, size):
        if self._broken_after is not None and self._at >= self._broken_after:
            raise IncompleteRead(self._body[: self._at])
        chunk = self._body[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def install_opener(monkeypatch, *, body=None, exc=None, broken_after=None):
    calls = []

    def opener(method, url, headers, timeout=30):
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        if exc is not None:
            raise exc
        return _Stream(body, broken_after=broken_after)

    monkeypatch.setattr(operations, "open_body_stream", opener)
    return calls


def make_unknown_checkpoint(tmp_path, capsys):
    configure(tmp_path)
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
    first = json.loads(capsys.readouterr().out)
    assert rc == 1 and first["status"] == "ambiguous"
    return first["checkpoint_id"]


def run_reconcile(tmp_path, checkpoint_id, writes):
    return upload.main(
        ["reconcile", "--checkpoint", checkpoint_id, "--json"],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: writes.append(args) or Response(200),
        now=NOW,
    )


def checkpoint_files(tmp_path):
    return list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json"))


def test_reconcile_converges_on_a_verified_full_read(tmp_path, capsys, monkeypatch):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)
    gets = install_opener(monkeypatch, body=b"content")
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["operation"] == "reconcile" and result["status"] == "ok"
    # This reconcile proves presence and content, not writer identity.
    assert result["object_written"] is None
    assert result["url"] is not None and result["url_kind"] == "presigned"
    assert result["remote"] == {
        "key": "objects/report.bin", "size": 7, "sha256": CONTENT_SHA256,
    }
    assert result["checkpoint"] is None and result["next_action"] is None
    assert writes == []
    assert len(gets) == 1 and gets[0]["method"] == "GET"
    # The verifying read is observably a presigned URL, asserted on the
    # recorded request itself (V2 review legacy D1).
    assert "X-Amz-Signature=" in gets[0]["url"]
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in gets[0]["url"]
    assert "X-Amz-Expires=" in gets[0]["url"]
    assert gets[0]["url"].split("?")[0].endswith("/objects/report.bin")
    assert checkpoint_files(tmp_path) == []


def test_reconcile_absent_remote_object_converges_to_not_started(
    tmp_path, capsys, monkeypatch
):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)
    gets = install_opener(
        monkeypatch, exc=HTTPError("https://example.test", 404, "Not Found", None, None)
    )
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result["operation"] == "reconcile" and result["status"] == "not_started"
    assert result["object_written"] is False
    assert result["retry_safety"] == "safe"
    assert writes == [] and len(gets) == 1
    assert checkpoint_files(tmp_path) == []


def test_reconcile_mismatched_bytes_stay_ambiguous(tmp_path, capsys, monkeypatch):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)
    gets = install_opener(monkeypatch, body=b"CONTENT")
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["status"] == "ambiguous"
    assert result["checkpoint"] == checkpoint_id
    assert result["next_action"] == "reconcile"
    assert result["retry_safety"] == "unsafe"
    assert writes == [] and len(gets) == 1
    assert len(checkpoint_files(tmp_path)) == 1


@pytest.mark.parametrize(
    "opener_kwargs",
    (
        {"exc": URLError("connection lost")},
        {"exc": HTTPError("https://example.test", 403, "Forbidden", None, None)},
        {"body": b"content", "broken_after": 2},
    ),
    ids=("unreadable", "denied", "truncated-chunked-read"),
)
def test_reconcile_failed_reads_stay_ambiguous(
    tmp_path, capsys, monkeypatch, opener_kwargs
):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)
    gets = install_opener(monkeypatch, **opener_kwargs)
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["status"] == "ambiguous"
    assert result["checkpoint_id"] == checkpoint_id
    assert writes == [] and len(gets) == 1
    assert len(checkpoint_files(tmp_path)) == 1


def test_reconcile_without_presign_capability_stays_ambiguous_without_reads(
    tmp_path, capsys, monkeypatch
):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)

    def registry(_target, key):
        return CapabilityRegistry(
            ((key, (Capability("PutObject", "enabled", "synthetic-test"),)),)
        )

    monkeypatch.setattr(operations, "registry_for_target", registry)
    monkeypatch.setattr(planning, "registry_for_target", registry)
    gets = install_opener(monkeypatch, body=b"content")
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 1 and result["status"] == "ambiguous"
    assert writes == [] and gets == []
    assert len(checkpoint_files(tmp_path)) == 1


def test_put_in_flight_checkpoint_reconciles_like_put_unknown(
    tmp_path, capsys, monkeypatch
):
    checkpoint_id = make_unknown_checkpoint(tmp_path, capsys)
    checkpoint_path = (
        tmp_path / ".s3-upload" / "checkpoints" / (checkpoint_id + ".json")
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["state"] = "put_in_flight"
    checkpoint_path.write_text(
        json.dumps(checkpoint, separators=(",", ":")), encoding="utf-8"
    )
    checkpoint_path.chmod(0o600)
    gets = install_opener(monkeypatch, body=b"content")
    writes = []

    rc = run_reconcile(tmp_path, checkpoint_id, writes)

    result = json.loads(capsys.readouterr().out)
    assert rc == 0 and result["status"] == "ok"
    assert result["object_written"] is None
    assert writes == [] and len(gets) == 1
    assert checkpoint_files(tmp_path) == []
