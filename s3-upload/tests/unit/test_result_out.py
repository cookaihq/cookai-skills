"""Caller-contract tests for `upload --result-out <path>` (Task 2.2).

The result JSON is atomically written to a caller-chosen handoff file. The
destination goes through the same preflight discipline as --reference-out
(protected namespaces, unsafe parents, source aliasing, foreign existing
content), and a preflight failure rejects the upload before any remote
request exists. Success, failure-with-a-durable-result, and the
rejected-before-any-request plan all leave the same nine-field JSON that
stdout --json would print.
"""

from datetime import datetime, timezone
import json
import os

import upload
from results import build_result
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


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


def run_upload(tmp_path, result_out, *, transport, extra=(), target_value=None):
    configure(tmp_path, target_value or target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    return upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--json", "--result-out", str(result_out), *extra,
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=transport,
        now=NOW,
    )


def test_result_out_file_is_the_atomic_twin_of_stdout_json(tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handoff = out_dir / "handoff.json"

    rc = run_upload(tmp_path, handoff, transport=lambda *args: Response(200))

    output = capsys.readouterr().out
    assert rc == 0
    assert handoff.read_text(encoding="utf-8") == output
    result = json.loads(handoff.read_text(encoding="utf-8"))
    assert result["status"] == "ok" and result["remote"]["size"] == 7
    # The published file carries the private handoff mode the atomic writer
    # promises, and no temporary spillover stays behind.
    assert (os.stat(handoff).st_mode & 0o777) == 0o600
    assert os.listdir(out_dir) == ["handoff.json"]


def test_result_out_is_written_for_a_durable_ambiguous_failure(tmp_path, capsys):
    handoff = tmp_path / "handoff.json"

    rc = run_upload(
        tmp_path, handoff,
        transport=lambda *args: (_ for _ in ()).throw(OSError("response lost")),
    )

    output = capsys.readouterr().out
    assert rc == 1
    assert handoff.read_text(encoding="utf-8") == output
    result = json.loads(output)
    assert result["status"] == "ambiguous"
    assert result["next_action"] == "reconcile" and result["checkpoint"] is not None


def test_result_out_preflight_rejects_before_any_remote_request(tmp_path, capsys):
    protected = tmp_path / ".s3-upload" / "checkpoints" / "handoff.json"
    calls = []

    rc = run_upload(
        tmp_path, protected, transport=lambda *args: calls.append(args) or Response(200),
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert "config_error" in output.err
    # The zero-request counter is independent of the preflight assertion: a
    # preflight that ran after the Put would leave one recorded call here.
    assert calls == []
    assert not protected.exists()


def test_result_out_rejects_aliasing_the_upload_source(tmp_path, capsys):
    configure(tmp_path, target())
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    source.chmod(0o600)
    calls = []

    rc = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--json", "--result-out", str(source),
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args) or Response(200),
        now=NOW,
    )

    assert rc == 2 and calls == []
    assert "config_error" in capsys.readouterr().err
    assert source.read_bytes() == b"content"


def test_result_out_rejects_an_existing_foreign_file(tmp_path, capsys):
    handoff = tmp_path / "handoff.json"
    handoff.write_text("not a result", encoding="utf-8")
    handoff.chmod(0o600)
    calls = []

    rc = run_upload(
        tmp_path, handoff, transport=lambda *args: calls.append(args) or Response(200),
    )

    assert rc == 2 and calls == []
    assert handoff.read_text(encoding="utf-8") == "not a result"


def test_result_out_replaces_a_prior_result_file(tmp_path, capsys):
    handoff = tmp_path / "handoff.json"
    prior = build_result(
        "upload", "not_started", object_written=False,
        retention={"mode": "retain", "days": None, "enforcement": "external-unverified"},
    )
    handoff.write_text(
        json.dumps(prior, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    handoff.chmod(0o600)

    rc = run_upload(tmp_path, handoff, transport=lambda *args: Response(200))

    output = capsys.readouterr().out
    assert rc == 0
    assert handoff.read_text(encoding="utf-8") == output
    assert json.loads(output)["status"] == "ok"


def test_result_out_pre_request_rejection_writes_nine_explicit_nulls(tmp_path, capsys):
    handoff = tmp_path / "handoff.json"
    blocked = target(
        provider="custom",
        endpoint="https://storage.example.test",
        addressing="path",
        collision="reject",
    )
    calls = []

    rc = run_upload(
        tmp_path, handoff,
        transport=lambda *args: calls.append(args) or Response(200),
        target_value=blocked,
    )

    output = capsys.readouterr()
    assert rc == 2 and calls == []
    assert "upload plan is blocked" in output.err
    result = json.loads(handoff.read_text(encoding="utf-8"))
    assert result["status"] == "not_started"
    assert result["object_written"] is False
    assert result["url"] is None and result["url_kind"] is None
    assert result["expires_at"] is None
    assert result["remote"] == {"key": None, "size": None, "sha256": None}
    assert result["checkpoint"] is None and result["next_action"] is None
    assert result["retry_safety"] == "safe"


def test_result_out_dry_run_writes_the_plan_result(tmp_path, capsys):
    handoff = tmp_path / "handoff.json"

    rc = run_upload(
        tmp_path, handoff,
        transport=lambda *args: (_ for _ in ()).throw(AssertionError("no network")),
        extra=("--dry-run",),
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert handoff.read_text(encoding="utf-8") == output
    assert json.loads(output)["status"] == "dry_run"


def test_result_out_never_leaves_a_previous_run_result_behind(tmp_path, capsys):
    # A cross-process verifier reads the handoff file, not this process's
    # exit code. A second run that ends without a durable result must not
    # leave the first run's terminal `ok` readable as if it were its own.
    handoff = tmp_path / "handoff.json"

    first = run_upload(tmp_path, handoff, transport=lambda *args: Response(200))
    capsys.readouterr()
    assert first == 0
    assert json.loads(handoff.read_text(encoding="utf-8"))["status"] == "ok"

    source = tmp_path / "report.bin"
    second = upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--json", "--result-out", str(handoff),
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: Response(403),
        now=NOW,
    )

    output = capsys.readouterr()
    assert second == 1 and output.out == ""
    result = json.loads(handoff.read_text(encoding="utf-8"))
    assert result["status"] == "not_started"
    assert result["object_written"] is False and result["retry_safety"] == "safe"


def test_result_out_placeholder_precedes_every_remote_request(tmp_path, capsys):
    # The placeholder has to be on disk before the first request, not merely
    # before the terminal write: the counter below is read inside the
    # transport, so a placeholder written afterwards records "absent" here.
    handoff = tmp_path / "handoff.json"
    seen = []

    def transport(*args):
        seen.append(
            json.loads(handoff.read_text(encoding="utf-8"))["status"]
            if handoff.exists()
            else "absent"
        )
        return Response(200)

    rc = run_upload(tmp_path, handoff, transport=transport)

    capsys.readouterr()
    assert rc == 0
    assert seen == ["not_started"]
    assert json.loads(handoff.read_text(encoding="utf-8"))["status"] == "ok"


def test_result_out_refuses_a_destination_that_appeared_after_preflight(tmp_path, capsys):
    # The preflight window is the whole upload. A file that shows up at the
    # destination while the Put is in flight is not what preflight cleared,
    # so the terminal write must refuse it instead of replacing it.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handoff = out_dir / "handoff.json"

    def transport(*args):
        handoff.write_text("foreign", encoding="utf-8")
        handoff.chmod(0o600)
        return Response(200)

    rc = run_upload(tmp_path, handoff, transport=transport)

    output = capsys.readouterr()
    assert rc == 1
    assert "result_error" in output.err
    assert json.loads(output.out)["status"] == "ok"
    assert handoff.read_text(encoding="utf-8") == "foreign"


def test_result_out_parent_swap_after_preflight_cannot_redirect_the_write(tmp_path, capsys):
    # Same window, one level up: the parent directory preflight cleared is
    # replaced mid-upload. The write must not land in the substitute.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handoff = out_dir / "handoff.json"
    displaced = tmp_path / "displaced"

    def transport(*args):
        out_dir.rename(displaced)
        out_dir.mkdir()
        return Response(200)

    rc = run_upload(tmp_path, handoff, transport=transport)

    output = capsys.readouterr()
    assert rc == 1
    assert "result_error" in output.err
    assert json.loads(output.out)["status"] == "ok"
    # Nothing lands in the substitute directory, and the real destination
    # still holds this run's placeholder rather than its terminal result.
    assert os.listdir(out_dir) == []
    displaced_result = json.loads((displaced / "handoff.json").read_text(encoding="utf-8"))
    assert displaced_result["status"] == "not_started"


def test_result_out_write_failure_never_clobbers_and_fails_loud(tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handoff = out_dir / "handoff.json"
    prior = build_result(
        "upload", "not_started", object_written=False,
        retention={"mode": "retain", "days": None, "enforcement": "external-unverified"},
    )
    prior_text = json.dumps(prior, ensure_ascii=False, separators=(",", ":")) + "\n"
    handoff.write_text(prior_text, encoding="utf-8")
    handoff.chmod(0o600)

    def transport(*args):
        # Between the preflight and the final write the destination's parent
        # stops accepting new entries: the atomic temp-file + rename cannot
        # publish, so the prior handoff must survive byte for byte. A
        # non-atomic in-place write would still open the existing 0600 file
        # and clobber it.
        os.chmod(out_dir, 0o500)
        return Response(200)

    try:
        rc = run_upload(tmp_path, handoff, transport=transport)
    finally:
        os.chmod(out_dir, 0o700)

    output = capsys.readouterr()
    assert rc == 1
    assert "result_error" in output.err
    assert json.loads(output.out)["status"] == "ok"
    assert handoff.read_text(encoding="utf-8") == prior_text
