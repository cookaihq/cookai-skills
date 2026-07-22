from datetime import datetime, timezone
import json

import pytest

import upload
from s3 import Response


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


PRESETS = (
    (
        "aliyun-oss",
        "cn-hangzhou",
        "docs-bucket",
        "https://s3.oss-cn-hangzhou.aliyuncs.com",
        "oss-unsigned-fixed-length",
    ),
    (
        "tencent-cos",
        "ap-guangzhou",
        "docs-bucket-1250000000",
        "https://cos.ap-guangzhou.myqcloud.com",
        "ordinary-payload-sha256-hypothesis",
    ),
)

EXPLICIT_ENDPOINT_FAMILIES = {
    "aliyun-oss": (
        "exact-e3245042262ca0d741c6760ede8c63e8b2dddb1b49946ddf1b9367443de4fa18"
    ),
    "tencent-cos": (
        "exact-681e795af6ebd437cc1b855066fc358501c94088d58705fcc562d7e822e9d2b6"
    ),
}


def configure_preset(project, *, provider, region, bucket, temporary=False):
    target_dir = project / ".s3-upload" / "targets"
    target_dir.mkdir(parents=True)
    target = {
        "schema_version": 1,
        "credential": "project:provider-key",
        "provider": provider,
        "region": region,
        "endpoint": None,
        "addressing": None,
        "bucket": bucket,
        "prefix": "documents/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 300,
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
    (target_dir / "provider.json").write_text(json.dumps(target), encoding="utf-8")
    credentials = {
        "provider-key": {
            "access_key_id": "PROVIDERKEY1234",
            "secret_access_key": "provider-secret-value",
            "session_token": "provider-session-token" if temporary else "",
            "expires_at": "2026-07-22T12:03:00Z" if temporary else None,
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
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_experimental_preset_is_available_in_normal_dry_run(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
    )
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--dry-run",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    plan = result["plan"]
    assert rc == 0 and result["status"] == "dry_run"
    assert plan["executable"] is True and plan["blocking_reasons"] == []
    assert plan["provider"] == provider
    assert plan["endpoint"] == endpoint and plan["addressing"] == "virtual"
    assert plan["contract_key"]["payload_profile"] == payload_profile
    assert [item["operation"] for item in plan["capabilities"]] == [
        "PutObject",
        "PresignGetObject",
    ]
    assert {item["state"] for item in plan["capabilities"]} == {"experimental"}
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()
    assert "provider-secret-value" not in output.out + output.err


@pytest.mark.parametrize(
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_experimental_preset_executes_one_fixed_length_put_and_presigns(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
    )
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        return Response(200)

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=transport,
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 0 and result["status"] == "ok"
    assert result["object_reference"]["location"]["provider"] == provider
    assert result["object_reference"]["location"]["endpoint"] == endpoint
    object_base = endpoint.replace("https://", f"https://{bucket}.")
    assert result["url"].startswith(
        object_base + "/documents/report.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    )
    assert len(calls) == 1
    method, url, headers, body = calls[0]
    assert method == "PUT" and url == object_base + "/documents/report.txt"
    assert body == b"docs-first" and headers["content-length"] == "10"
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert headers["x-amz-content-sha256"] == (
        "UNSIGNED-PAYLOAD"
        if provider == "aliyun-oss"
        else "7443ad270ec3f9554fa25e854880d49bf7e813f97bab08a3cca4592f3d94f1e7"
    )
    if provider == "aliyun-oss":
        assert headers["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    else:
        assert "x-oss-content-sha256" not in headers
    assert "transfer-encoding" not in headers
    assert "content-encoding" not in headers
    assert list((tmp_path / ".s3-upload" / "checkpoints").glob("*.json")) == []
    assert "provider-secret-value" not in output.out + output.err


@pytest.mark.parametrize(
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_experimental_preset_url_rejects_endpoint_override_before_signing(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
    )
    target_path = tmp_path / ".s3-upload" / "targets" / "provider.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["endpoint"] = "https://objects.example.test"
    target_path.write_text(json.dumps(target), encoding="utf-8")

    rc = upload.main(
        [
            "url",
            "--target",
            "project:provider",
            "--key",
            "documents/report.txt",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert "exact" in output.err or "endpoint" in output.err
    assert "provider-secret-value" not in output.err
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


@pytest.mark.parametrize(
    "region,bucket,endpoint",
    (
        (
            "us-west-1",
            "docs-bucket",
            "https://s3.oss-us-west-1.aliyuncs.com",
        ),
        (
            "cn-hangzhou",
            "docs-bucket",
            "https://oss-cn-hangzhou.aliyuncs.com",
        ),
        (
            "cn-hangzhou",
            "docs-bucket",
            "https://docs-bucket.oss-cn-hangzhou.aliyuncs.com",
        ),
        (
            "cn-hangzhou",
            "docs-bucket",
            "https://oss-accelerate.aliyuncs.com",
        ),
        (
            "ap-guangzhou",
            "docs-bucket-1250000000",
            "https://cos.ap-guangzhou.myqcloud.com",
        ),
    ),
)
def test_known_provider_service_endpoint_cannot_inherit_custom_baseline(
    tmp_path,
    capsys,
    region,
    bucket,
    endpoint,
):
    configure_preset(
        tmp_path,
        provider="custom",
        region=region,
        bucket=bucket,
    )
    target_path = tmp_path / ".s3-upload" / "targets" / "provider.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["endpoint"] = endpoint
    target["addressing"] = "virtual"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--dry-run",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 2
    assert result["plan"]["executable"] is False
    assert "capability_disabled" in result["plan"]["blocking_reasons"]
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


@pytest.mark.parametrize(
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_explicit_preset_endpoint_is_an_isolated_test_only_contract(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
    )
    target_path = tmp_path / ".s3-upload" / "targets" / "provider.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["endpoint"] = endpoint
    target_path.write_text(json.dumps(target), encoding="utf-8")
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--dry-run",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    plan = result["plan"]
    assert rc == 2 and plan["executable"] is False
    assert plan["contract_key"]["endpoint_family"] == (
        EXPLICIT_ENDPOINT_FAMILIES[provider]
    )
    assert {item["state"] for item in plan["capabilities"]} == {"test-only"}
    assert "capability_disabled" in plan["blocking_reasons"]
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


def test_oss_nonvirtual_addressing_is_rejected_before_plan_or_request(
    tmp_path,
    capsys,
):
    configure_preset(
        tmp_path,
        provider="aliyun-oss",
        region="cn-hangzhou",
        bucket="docs-bucket",
    )
    target_path = tmp_path / ".s3-upload" / "targets" / "provider.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["addressing"] = "path"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--dry-run",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    assert rc == 2 and output.out == ""
    assert "virtual" in output.err or "addressing" in output.err
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()


@pytest.mark.parametrize(
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_experimental_preset_carries_temporary_token_and_caps_presign(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
        temporary=True,
    )
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: (calls.append(args) or Response(200)),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 0 and result["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][2]["x-amz-security-token"] == "provider-session-token"
    assert "X-Amz-Security-Token=provider-session-token" in result["url"]
    assert "X-Amz-Expires=120" in result["url"]
    assert result["expires_at"] == "2026-07-22T12:02:00Z"
    assert "provider-session-token" not in output.err


@pytest.mark.parametrize(
    "provider,region,bucket,endpoint,payload_profile",
    PRESETS,
)
def test_experimental_preset_keeps_conditional_put_test_only(
    tmp_path,
    capsys,
    provider,
    region,
    bucket,
    endpoint,
    payload_profile,
):
    configure_preset(
        tmp_path,
        provider=provider,
        region=region,
        bucket=bucket,
    )
    target_path = tmp_path / ".s3-upload" / "targets" / "provider.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["collision"] = "unique"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    source = tmp_path / "report.txt"
    source.write_bytes(b"docs-first")
    calls = []

    rc = upload.main(
        [
            "upload",
            "--file",
            str(source),
            "--target",
            "project:provider",
            "--dry-run",
            "--json",
        ],
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        transport=lambda *args: calls.append(args),
        now=NOW,
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert rc == 2 and result["plan"]["executable"] is False
    conditional = next(
        item
        for item in result["plan"]["capabilities"]
        if item["operation"] == "ConditionalPutObject"
    )
    assert conditional["state"] == "test-only"
    assert "capability_disabled" in result["plan"]["blocking_reasons"]
    assert calls == []
    assert not (tmp_path / ".s3-upload" / "checkpoints").exists()
