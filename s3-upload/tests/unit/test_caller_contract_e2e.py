"""End-to-end consumer-contract tests (Task 4.2 of the minimal caller contract).

These tests simulate the two real downstream callers on the uploader side,
without touching the consumers' repositories:

- the pdf2markdown face consumes the seventeen-key `upload --json` result
  key by key through a strict closed-set consumer (lineage: the
  publish-mock-online-assets spec requires missing values to be explicit
  null and unknown fields to be rejected);
- the vi-pdf2md face drives a `collision=reject` Target with a
  caller-chosen key, content-type and content-disposition through first
  write, identical re-run (adoption) and divergent re-run (collision),
  with `--result-out` as the cross-process handoff file;
- the reconciliation face settles a `put_unknown` checkpoint through the
  read-only full-body reconcile and consumes its result.

The write-request counters in every test are independent of the opener
that serves verification GETs.
"""

from datetime import datetime, timezone
import hashlib
import json
import os

import pytest

import operations
import upload
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
BODY = b"caller-body-1234"
BODY_SHA256 = hashlib.sha256(BODY).hexdigest()
# Same length as BODY, different bytes: only the SHA-256 comparison can
# separate the two.
DIVERGENT_BODY = b"CALLER-BODY-1234"

RESULT_KEYS = frozenset({
    "schema_version", "operation", "status", "object_written",
    "object_reference", "url", "url_kind", "expires_at", "retention",
    "delete_scope", "deleted_version_id", "checkpoint_id", "plan",
    "remote", "checkpoint", "next_action", "retry_safety",
})


class ContractViolation(ValueError):
    pass


def consume_caller_contract(text):
    """Parse one result line the way the downstream callers do.

    The consumer is strict in both directions: every key of the closed
    seventeen-key set must be present (a missing value arrives as an
    explicit null, never as an absent key), and any key outside the set is
    rejected. It returns the nine caller-contract fields fully typed.
    """
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ContractViolation("result must be one JSON object")
    unknown = set(result) - RESULT_KEYS
    if unknown:
        raise ContractViolation(f"unknown result fields rejected: {sorted(unknown)}")
    missing = RESULT_KEYS - set(result)
    if missing:
        raise ContractViolation(f"missing result fields: {sorted(missing)}")
    contract = {}
    status = result["status"]
    if not isinstance(status, str) or not status:
        raise ContractViolation("status must be a non-empty string")
    contract["status"] = status
    object_written = result["object_written"]
    if object_written is not None and not isinstance(object_written, bool):
        raise ContractViolation("object_written must be boolean or null")
    contract["object_written"] = object_written
    for field in ("url", "url_kind", "expires_at"):
        value = result[field]
        if value is not None and not isinstance(value, str):
            raise ContractViolation(f"{field} must be a string or null")
        contract[field] = value
    retention = result["retention"]
    if not isinstance(retention, dict) or set(retention) != {"mode", "days", "enforcement"}:
        raise ContractViolation("retention must be the closed three-field container")
    contract["retention"] = retention
    remote = result["remote"]
    if not isinstance(remote, dict) or set(remote) != {"key", "size", "sha256"}:
        raise ContractViolation("remote must be the closed key/size/sha256 container")
    contract["remote"] = remote
    checkpoint = result["checkpoint"]
    if checkpoint is not None and not isinstance(checkpoint, str):
        raise ContractViolation("checkpoint must be a string or null")
    if checkpoint != result["checkpoint_id"]:
        raise ContractViolation("checkpoint must mirror checkpoint_id")
    contract["checkpoint"] = checkpoint
    next_action = result["next_action"]
    if next_action not in (None, "reconcile"):
        raise ContractViolation("next_action must be null or reconcile")
    contract["next_action"] = next_action
    retry_safety = result["retry_safety"]
    if retry_safety not in (None, "safe", "unsafe"):
        raise ContractViolation("retry_safety must be null, safe or unsafe")
    contract["retry_safety"] = retry_safety
    return contract


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


def configure(project, target_value, *, skill_targets=None):
    directory = project / ".s3-upload" / "targets"
    directory.mkdir(parents=True)
    (directory / "objects.json").write_text(json.dumps(target_value), encoding="utf-8")
    if skill_targets is not None:
        (project / ".s3-upload" / "config.json").write_text(
            json.dumps({
                "schema_version": 1,
                "default_target": "project:objects",
                "skill_targets": skill_targets,
            }),
            encoding="utf-8",
        )
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
    def __init__(self, body):
        self._body = body
        self.status = 200
        self.headers = {"content-length": str(len(body))}
        self._at = 0

    def read(self, size):
        chunk = self._body[self._at:self._at + size]
        self._at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def install_opener(monkeypatch, *, body):
    calls = []

    def opener(method, url, headers, timeout=30):
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        return _Stream(body)

    monkeypatch.setattr(operations, "open_body_stream", opener)
    return calls


def run_cli(project, argv, transport):
    return upload.main(
        argv,
        environ={},
        cwd=str(project),
        config_home=str(project / "home"),
        transport=transport,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# E2E-1: the pdf2markdown face -- strict seventeen-key consumption.
# ---------------------------------------------------------------------------


def test_pdf2markdown_face_consumes_every_contract_field_of_a_success(
    tmp_path, capsys
):
    configure(
        tmp_path, target(), skill_targets={"pdf2markdown": "project:objects"}
    )
    source = tmp_path / "paper.pdf"
    source.write_bytes(BODY)

    rc = run_cli(
        tmp_path,
        [
            "upload", "--file", str(source),
            "--caller-skill", "pdf2markdown", "--json",
        ],
        lambda *args: Response(200),
    )

    output = capsys.readouterr().out
    assert rc == 0
    contract = consume_caller_contract(output)
    assert contract["status"] == "ok"
    assert contract["object_written"] is True
    assert contract["url"] is not None and contract["url_kind"] == "presigned"
    assert contract["expires_at"] == "2026-07-27T13:00:00Z"
    assert contract["retention"] == {
        "mode": "retain", "days": None, "enforcement": "external-unverified",
    }
    assert contract["remote"] == {
        "key": "objects/paper.pdf", "size": len(BODY), "sha256": BODY_SHA256,
    }
    assert contract["checkpoint"] is None
    assert contract["next_action"] is None
    assert contract["retry_safety"] is None

    # The consumer rejects a result that grew a field it does not know.
    widened = json.loads(output)
    widened["surprise"] = True
    with pytest.raises(ContractViolation, match="unknown"):
        consume_caller_contract(json.dumps(widened))


def test_pdf2markdown_face_reads_explicit_nulls_from_a_pre_request_rejection(
    tmp_path, capsys
):
    # A Target whose collision policy has no capability on this provider is
    # rejected while forming the plan, before any request; the handoff file
    # still carries the full closed key set with explicit nulls.
    blocked = target(
        provider="custom",
        endpoint="https://storage.example.test",
        addressing="path",
        collision="reject",
    )
    configure(tmp_path, blocked)
    source = tmp_path / "paper.pdf"
    source.write_bytes(BODY)
    handoff = tmp_path / "handoff.json"
    requests = []

    rc = run_cli(
        tmp_path,
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--json", "--result-out", str(handoff),
        ],
        lambda *args: requests.append(args) or Response(200),
    )

    assert rc == 2
    assert requests == []
    contract = consume_caller_contract(handoff.read_text(encoding="utf-8"))
    assert contract["status"] == "not_started"
    assert contract["object_written"] is False
    assert contract["url"] is None and contract["url_kind"] is None
    assert contract["expires_at"] is None
    assert contract["remote"] == {"key": None, "size": None, "sha256": None}
    assert contract["checkpoint"] is None and contract["next_action"] is None
    assert contract["retry_safety"] == "safe"


# ---------------------------------------------------------------------------
# E2E-2: the vi-pdf2md face -- reject Target, caller-chosen identity,
# adoption, collision, and the --result-out handoff file.
# ---------------------------------------------------------------------------


VI_ARGS = [
    "--target", "project:objects",
    "--key", "objects/vi/report.pdf",
    "--content-type", "application/pdf",
    "--content-disposition", 'attachment; filename="report.pdf"',
    "--json",
]


def vi_project(tmp_path, body=BODY):
    configure(tmp_path, target(collision="reject"))
    source = tmp_path / "report.pdf"
    source.write_bytes(body)
    return source


def test_vi_pdf2md_face_first_upload_is_conditional_and_lands_the_handoff(
    tmp_path, capsys
):
    source = vi_project(tmp_path)
    handoff = tmp_path / "handoff.json"
    puts = []

    rc = run_cli(
        tmp_path,
        ["upload", "--file", str(source), *VI_ARGS, "--result-out", str(handoff)],
        lambda *args: puts.append(args) or Response(200),
    )

    output = capsys.readouterr().out
    assert rc == 0
    contract = consume_caller_contract(output)
    assert contract["status"] == "ok" and contract["object_written"] is True
    # The caller-chosen identity is what went out on the wire.
    assert len(puts) == 1 and puts[0][0] == "PUT"
    headers = dict(puts[0][2])
    assert headers["if-none-match"] == "*"
    assert headers["content-type"] == "application/pdf"
    assert headers["content-disposition"] == 'attachment; filename="report.pdf"'
    assert puts[0][1].split("?")[0].endswith("/objects/vi/report.pdf")
    assert contract["remote"] == {
        "key": "objects/vi/report.pdf", "size": len(BODY), "sha256": BODY_SHA256,
    }
    # The handoff file is the byte-for-byte twin of stdout, published with
    # the atomic writer's private mode.
    assert handoff.read_text(encoding="utf-8") == output
    assert (os.stat(handoff).st_mode & 0o777) == 0o600


def test_vi_pdf2md_face_identical_rerun_adopts_without_a_second_put(
    tmp_path, capsys, monkeypatch
):
    source = vi_project(tmp_path)
    handoff = tmp_path / "handoff.json"
    first_puts = []
    rc = run_cli(
        tmp_path,
        ["upload", "--file", str(source), *VI_ARGS, "--result-out", str(handoff)],
        lambda *args: first_puts.append(args) or Response(200),
    )
    capsys.readouterr()
    assert rc == 0 and len(first_puts) == 1

    # The caller runs the same upload again; the key now holds the object,
    # so the conditional Put answers 412 and the adoption read finds the
    # identical bytes.
    gets = install_opener(monkeypatch, body=BODY)
    second_puts = []
    rc = run_cli(
        tmp_path,
        ["upload", "--file", str(source), *VI_ARGS, "--result-out", str(handoff)],
        lambda *args: second_puts.append(args) or Response(412),
    )

    output = capsys.readouterr().out
    assert rc == 0
    contract = consume_caller_contract(output)
    assert contract["status"] == "adopted"
    assert contract["object_written"] is False
    assert contract["remote"] == {
        "key": "objects/vi/report.pdf", "size": len(BODY), "sha256": BODY_SHA256,
    }
    # One conditional attempt, no second Put; the adoption evidence is one
    # observably presigned full GET.
    assert len(second_puts) == 1 and second_puts[0][0] == "PUT"
    assert len(gets) == 1 and gets[0]["method"] == "GET"
    assert "X-Amz-Signature=" in gets[0]["url"]
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in gets[0]["url"]
    assert "X-Amz-Expires=" in gets[0]["url"]
    assert gets[0]["url"].split("?")[0].endswith("/objects/vi/report.pdf")
    assert handoff.read_text(encoding="utf-8") == output


def test_vi_pdf2md_face_divergent_rerun_is_a_collision_exit_four(
    tmp_path, capsys, monkeypatch
):
    # The local source changed to same-length different bytes while the
    # remote key still holds the original object.
    source = vi_project(tmp_path, body=DIVERGENT_BODY)
    gets = install_opener(monkeypatch, body=BODY)
    puts = []

    rc = run_cli(
        tmp_path,
        ["upload", "--file", str(source), *VI_ARGS],
        lambda *args: puts.append(args) or Response(412),
    )

    output = capsys.readouterr().out
    assert rc == 4
    contract = consume_caller_contract(output)
    assert contract["status"] == "collision"
    assert contract["object_written"] is False
    assert contract["retry_safety"] == "safe"
    assert len(puts) == 1 and len(gets) == 1


# ---------------------------------------------------------------------------
# E2E-3: the reconciliation face -- put_unknown settled read-only.
# ---------------------------------------------------------------------------


def test_reconcile_face_converges_a_put_unknown_checkpoint_read_only(
    tmp_path, capsys, monkeypatch
):
    configure(tmp_path, target())
    source = tmp_path / "report.pdf"
    source.write_bytes(BODY)
    rc = run_cli(
        tmp_path,
        ["upload", "--file", str(source), "--target", "project:objects", "--json"],
        lambda *args: (_ for _ in ()).throw(OSError("response lost")),
    )
    first = consume_caller_contract(capsys.readouterr().out)
    assert rc == 1 and first["status"] == "ambiguous"
    assert first["object_written"] is None
    assert first["checkpoint"] is not None
    assert first["next_action"] == "reconcile"
    assert first["retry_safety"] == "unsafe"

    gets = install_opener(monkeypatch, body=BODY)
    # This counter records every transport invocation; it is independent of
    # the opener above, which serves the read-only verification GET.
    write_requests = []
    rc = run_cli(
        tmp_path,
        ["reconcile", "--checkpoint", first["checkpoint"], "--json"],
        lambda *args: write_requests.append(args) or Response(200),
    )

    output = capsys.readouterr().out
    assert rc == 0
    contract = consume_caller_contract(output)
    assert contract["status"] == "ok"
    # Presence and content are proven; writer identity is not claimed.
    assert contract["object_written"] is None
    assert contract["remote"] == {
        "key": "objects/report.pdf", "size": len(BODY), "sha256": BODY_SHA256,
    }
    assert contract["checkpoint"] is None and contract["next_action"] is None
    assert contract["retry_safety"] is None
    assert write_requests == []
    assert len(gets) == 1 and gets[0]["method"] == "GET"
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []
