"""The stderr notice a real (non-dry-run) upload emits for an experimental preset.

`aliyun-oss` / `tencent-cos` capabilities are `experimental`, not live-verified.
dry-run already says so inside `plan.capabilities`, but an operator who skips
the dry-run and goes straight to the write sees nothing. One stderr line
immediately before the first remote request closes that gap. It must not appear
for a baseline provider, must not appear for dry-run (whose plan already
carries the states), and must not disturb stdout, which the `--result-out`
handoff is required to match byte for byte.
"""

from datetime import datetime, timezone
import io
import json
import sys

import upload
from s3 import Response


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

NOTICE_PREFIX = "[s3-upload] experimental "


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


def run_upload(tmp_path, *, target_value, extra=(), transport=None):
    configure(tmp_path, target_value)
    source = tmp_path / "report.bin"
    source.write_bytes(b"content")
    return upload.main(
        [
            "upload", "--file", str(source), "--target", "project:objects",
            "--json", *extra,
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=transport or (lambda *args: Response(200)),
        now=NOW,
    )


def notice_lines(stderr):
    return [line for line in stderr.splitlines() if line.startswith(NOTICE_PREFIX)]


def test_experimental_preset_announces_provider_and_capabilities_on_stderr(
    tmp_path, capsys
):
    oss = target(
        provider="aliyun-oss",
        region="cn-hangzhou",
        bucket="candidate-bucket",
    )

    rc = run_upload(tmp_path, target_value=oss)

    captured = capsys.readouterr()
    assert rc == 0
    assert notice_lines(captured.err) == [
        "[s3-upload] experimental provider=aliyun-oss "
        "capabilities=PutObject,PresignGetObject"
    ]
    # The notice is stderr-only and carries no endpoint, credential or URL.
    result = json.loads(captured.out)
    assert result["status"] == "ok"
    assert "experimental" not in captured.out
    assert "project-secret-value" not in captured.err
    assert "PROJECTKEY1234" not in captured.err
    assert "X-Amz-Signature" not in captured.err


def test_experimental_notice_precedes_every_remote_request(
    tmp_path, capsys, monkeypatch
):
    # "Announced before the write" is the whole point of the line: a warning
    # that only lands after the object exists warns nobody. Asserting on the
    # final stderr text cannot tell the two apart, so the buffer is read from
    # inside the transport -- a notice printed later records as absent here,
    # which is exactly what moving the print past execute_single_put would do.
    oss = target(
        provider="aliyun-oss",
        region="cn-hangzhou",
        bucket="candidate-bucket",
    )
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    seen = []

    def transport(*args):
        seen.append(notice_lines(buffer.getvalue()))
        return Response(200)

    rc = run_upload(tmp_path, target_value=oss, transport=transport)

    capsys.readouterr()
    assert rc == 0
    assert seen == [
        [
            "[s3-upload] experimental provider=aliyun-oss "
            "capabilities=PutObject,PresignGetObject"
        ]
    ]


def test_experimental_notice_also_covers_the_tencent_cos_preset(tmp_path, capsys):
    cos = target(
        provider="tencent-cos",
        region="ap-guangzhou",
        bucket="candidate-bucket-1250000000",
    )

    rc = run_upload(tmp_path, target_value=cos)

    captured = capsys.readouterr()
    assert rc == 0
    assert notice_lines(captured.err) == [
        "[s3-upload] experimental provider=tencent-cos "
        "capabilities=PutObject,PresignGetObject"
    ]


def test_baseline_provider_upload_emits_no_experimental_notice(tmp_path, capsys):
    rc = run_upload(tmp_path, target_value=target())

    captured = capsys.readouterr()
    assert rc == 0
    assert notice_lines(captured.err) == []


def test_dry_run_of_an_experimental_preset_emits_no_notice(tmp_path, capsys):
    oss = target(
        provider="aliyun-oss",
        region="cn-hangzhou",
        bucket="candidate-bucket",
    )
    calls = []

    rc = run_upload(
        tmp_path,
        target_value=oss,
        extra=("--dry-run",),
        transport=lambda *args: calls.append(args),
    )

    captured = capsys.readouterr()
    assert rc == 0 and calls == []
    assert notice_lines(captured.err) == []
    # The plan itself is where dry-run already reports the states.
    plan = json.loads(captured.out)["plan"]
    assert {item["state"] for item in plan["capabilities"]} == {"experimental"}


def test_notice_does_not_disturb_the_result_out_handoff_bytes(tmp_path, capsys):
    oss = target(
        provider="aliyun-oss",
        region="cn-hangzhou",
        bucket="candidate-bucket",
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handoff = out_dir / "handoff.json"

    rc = run_upload(
        tmp_path, target_value=oss, extra=("--result-out", str(handoff))
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert handoff.read_text(encoding="utf-8") == captured.out
    assert len(notice_lines(captured.err)) == 1
