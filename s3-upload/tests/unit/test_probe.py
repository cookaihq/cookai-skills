import json

import pytest

from delivery_schema import parse_artifact
from probe import build_probe
from target_contract import contract_hash, credential_binding_hash
from v2_schema import ScopedReference


FAKE_ACCESS_KEY = "AKIAPROBEFAKE0001"
FAKE_SECRET = "probe-fixture-secret-must-never-leak"

CREDENTIAL_PROFILE = {
    "access_key_id": FAKE_ACCESS_KEY,
    "secret_access_key": FAKE_SECRET,
    "session_token": "",
    "expires_at": None,
}

TARGET = {
    "schema_version": 1,
    "credential": "project:images-key",
    "provider": "aws-s3",
    "region": "us-east-1",
    "endpoint": None,
    "addressing": None,
    "bucket": "example-bucket",
    "prefix": "images/",
    "access": {"mode": "private", "public_base_url": None, "presign_expires_seconds": 3600},
    "retention": {"mode": "retain", "days": None},
    "collision": "reject",
    "object_headers": {"cache_control": None, "content_disposition": None},
    "limits": {"soft_max_bytes": 1048576, "multipart_threshold_bytes": None, "part_size_bytes": None},
    "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
    "setup": {"exclusive_prefix": True, "integration_test": False, "cors": None},
}


def _write_env_local(path, credentials):
    path.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON='"
        + json.dumps(credentials, separators=(",", ":"))
        + "'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".s3-upload").mkdir(parents=True)
    (root / ".s3-upload" / "targets").mkdir()
    (root / ".s3-upload" / "targets" / "images.json").write_text(json.dumps(TARGET))
    (root / ".s3-upload" / "config.json").write_text(json.dumps({
        "schema_version": 1, "default_target": "project:images", "skill_targets": {},
    }))
    _write_env_local(root / ".env.local", {"images-key": CREDENTIAL_PROFILE})
    return root


def probe_for(project, **overrides):
    kwargs = dict(
        cwd=str(project),
        config_home=str(project / "home"),
        environ={},
        cli_target="project:images",
        cli_caller="pdf2markdown",
        use_local_key=False,
        executable="/usr/bin/python3",
        state_root=str(project / ".s3-upload"),
    )
    kwargs.update(overrides)
    return build_probe(**kwargs)


def test_probe_is_a_valid_v2_artifact(project):
    item = probe_for(project)
    assert item["artifact_type"] == "s3-upload.probe"
    assert item["schema_version"] == 1


def test_probe_reports_identity_fields(project):
    item = probe_for(project)
    assert item["caller"] == "pdf2markdown"
    assert item["cwd"] == str(project)
    assert item["executable"] == "/usr/bin/python3"
    assert item["state_root"] == str(project / ".s3-upload")
    assert item["contract_versions"] == [1]


def test_probe_reports_target_contract_hash(project):
    item = probe_for(project)
    assert item["readiness"] == "ready"
    assert item["blocking_reason"] is None
    assert item["target_contract_hash"] == contract_hash(item["target_contract"])
    assert item["target_contract_hash"].startswith("sha256:")
    assert item["target_contract"]["bucket"] == "example-bucket"
    assert item["target_contract"]["provider"] == "aws-s3"
    assert item["target_contract"]["target_ref"] == "project:images"
    assert item["target_contract"]["contract_version"] == 1


def test_probe_never_reports_credential_values(project):
    item = probe_for(project)
    dumped = json.dumps(item)
    assert FAKE_ACCESS_KEY not in dumped
    assert FAKE_SECRET not in dumped
    assert "images-key" not in dumped
    expected_binding_hash = credential_binding_hash(ScopedReference("project", "images-key"))
    assert item["target_contract"]["credential_binding_hash"] == expected_binding_hash


def test_probe_creates_no_files(project, tmp_path):
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    probe_for(project)
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert before == after


def test_probe_makes_no_network_call(project, monkeypatch):
    import urllib.request

    import s3

    def explode(*args, **kwargs):
        raise AssertionError("probe must not perform network I/O")

    monkeypatch.setattr(s3, "http_request", explode)
    monkeypatch.setattr(urllib.request, "urlopen", explode)
    item = probe_for(project)
    assert item["artifact_type"] == "s3-upload.probe"
    assert item["readiness"] == "ready"


def test_probe_is_unconfigured_without_target(project, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    item = probe_for(project, cwd=str(empty), cli_target=None)
    assert item["readiness"] == "installed_unconfigured"
    assert item["target_contract"] is None
    assert item["target_contract_hash"] is None
    assert item["blocking_reason"] == "ResolutionError"


def test_probe_blocks_indirect_global_target_without_local_key(project, monkeypatch):
    home = project / "home"
    (home / "targets").mkdir(parents=True)
    home.chmod(0o700)
    (home / "targets").chmod(0o700)
    target_path = home / "targets" / "images.json"
    target_path.write_text(json.dumps(TARGET | {"credential": "global:images-key"}))
    target_path.chmod(0o600)
    (project / ".s3-upload" / "config.json").write_text(json.dumps({
        "schema_version": 1, "default_target": "global:images", "skill_targets": {},
    }))

    import resolver

    real_validate_directory = resolver.validate_directory

    def guarded(path, **kwargs):
        if str(home) in path:
            raise AssertionError("probe must not read unauthorized home configuration")
        return real_validate_directory(path, **kwargs)

    monkeypatch.setattr(resolver, "validate_directory", guarded)

    item = probe_for(project, cli_target=None, use_local_key=False)
    assert item["readiness"] == "installed_unconfigured"
    assert item["target_contract"] is None
    assert item["target_contract_hash"] is None
    assert item["blocking_reason"] == "ResolutionError"


def test_probe_reads_home_config_when_authorized(project):
    home = project / "home"
    (home / "targets").mkdir(parents=True)
    home.chmod(0o700)
    (home / "targets").chmod(0o700)
    target_path = home / "targets" / "images.json"
    target_path.write_text(json.dumps(TARGET | {"credential": "global:images-key"}))
    target_path.chmod(0o600)
    (project / ".s3-upload" / "config.json").write_text(json.dumps({
        "schema_version": 1, "default_target": "global:images", "skill_targets": {},
    }))

    item = probe_for(project, cli_target=None, use_local_key=True)
    assert item["blocking_reason"] is None
    assert item["target_contract"] is not None
    assert item["target_contract"]["bucket"] == "example-bucket"
    assert item["readiness"] == "installed_unconfigured"


def test_probe_cli_emits_canonical_artifact(project, capsys, monkeypatch):
    import upload

    monkeypatch.chdir(project)
    code = upload.main(
        ["probe", "--target", "project:images", "--caller-skill", "pdf2markdown"],
        environ={}, cwd=str(project), config_home=str(project / "home"),
    )
    assert code == 0
    raw = capsys.readouterr().out.strip()
    artifact = parse_artifact(raw, expected_type="s3-upload.probe")
    assert artifact["caller"] == "pdf2markdown"
    assert artifact["readiness"] == "ready"
    assert artifact["blocking_reason"] is None
