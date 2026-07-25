import json

import pytest

from probe import build_probe


CALLERS = ("pdf2markdown", "vi-pdf2md")

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


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".s3-upload" / "targets").mkdir(parents=True)
    (root / ".s3-upload" / "targets" / "images.json").write_text(json.dumps(TARGET))
    (root / ".s3-upload" / "targets" / "docs.json").write_text(
        json.dumps({**TARGET, "prefix": "markdown/"})
    )
    (root / ".s3-upload" / "targets" / "shared.json").write_text(
        json.dumps({**TARGET, "prefix": "shared/"})
    )
    (root / ".s3-upload" / "config.json").write_text(json.dumps({
        "schema_version": 1,
        "default_target": "project:shared",
        "skill_targets": {"pdf2markdown": "project:images", "vi-pdf2md": "project:docs"},
    }))
    return root


def probe_for(project, caller, target=None):
    return build_probe(
        cwd=str(project), config_home=str(project / "home"), environ={},
        cli_target=target, cli_caller=caller, use_local_key=False,
        executable="/usr/bin/python3", state_root=str(project / ".s3-upload"),
    )


EXPECTED_TARGET_REF = {"pdf2markdown": "project:images", "vi-pdf2md": "project:docs"}


@pytest.mark.parametrize("caller", CALLERS)
def test_each_caller_resolves_its_scoped_target(project, caller):
    item = probe_for(project, caller)
    assert item["caller"] == caller
    assert item["target_ref"] == EXPECTED_TARGET_REF[caller]


def test_two_callers_get_different_target_contracts(project):
    first = probe_for(project, "pdf2markdown")
    second = probe_for(project, "vi-pdf2md")
    assert first["target_ref"] != second["target_ref"]
    assert first["target_contract_hash"] != second["target_contract_hash"]


@pytest.mark.parametrize("caller", CALLERS)
def test_probe_output_is_deterministic(project, caller):
    assert probe_for(project, caller) == probe_for(project, caller)


def test_unknown_caller_falls_back_to_project_default_target(project):
    item = probe_for(project, "unknown-skill")
    assert item["caller"] == "unknown-skill"
    assert item["target_ref"] == "project:shared"
