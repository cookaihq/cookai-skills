import json
import os

import pytest

from planning import build_upload_dry_run, derive_contract_key, registry_for_target
from resolver import resolve_target
from target_contract import contract_hash, contract_snapshot


CALLER = "pdf2markdown"

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
    "collision": "replace",
    "object_headers": {"cache_control": None, "content_disposition": None},
    "limits": {"soft_max_bytes": 1048576, "multipart_threshold_bytes": None, "part_size_bytes": None},
    "retry": {"part_max_attempts": 3, "collision_max_attempts": 1},
    "setup": {"exclusive_prefix": True, "integration_test": False, "cors": None},
}

CREDENTIALS = {
    "images-key": {
        "access_key_id": "PROJECTKEY1234",
        "secret_access_key": "project-secret-value",
        "session_token": "",
        "expires_at": None,
    }
}

SOURCE_BYTES = b"hello world"


class Project:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.state_root = root / ".s3-upload"
        self.out = root / "out"
        self.source = root / "a.png"

    @property
    def recovery_out(self):
        return str(self.out / "recovery.json")

    @property
    def result_out(self):
        return str(self.out / "result.json")

    @property
    def ack_out(self):
        return str(self.out / "ack.json")


def write_target(project, **overrides):
    value = dict(TARGET)
    value.update(overrides)
    (project.state_root / "targets" / "images.json").write_text(
        json.dumps(value), encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path):
    item = Project(tmp_path)
    (item.state_root / "targets").mkdir(parents=True)
    item.home.mkdir()
    item.out.mkdir()
    write_target(item)
    (item.state_root / "config.json").write_text(
        json.dumps({
            "schema_version": 1,
            "default_target": "project:images",
            "skill_targets": {CALLER: "project:images", "vi-pdf2md": "project:images"},
        }),
        encoding="utf-8",
    )
    env_local = tmp_path / ".env.local"
    env_local.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON="
        + json.dumps(CREDENTIALS, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)
    item.source.write_bytes(SOURCE_BYTES)
    return item


@pytest.fixture
def resolved(project):
    return resolve_target(
        cwd=str(project.root),
        config_home=str(project.home),
        environ={},
        cli_target=None,
        cli_caller=CALLER,
        use_local_key=False,
    )


@pytest.fixture
def dry_run(project, resolved):
    item = build_upload_dry_run(
        resolved=resolved,
        file_path=str(project.source),
        explicit_key=None,
        content_type=None,
        cache_control=None,
        content_disposition=None,
        presign_expires=None,
        reference_out=None,
        project_root=str(project.root),
        config_home=str(project.home),
        allow_insecure_http=False,
    )
    yield item
    item.close()


@pytest.fixture
def snapshot(project, resolved):
    key = derive_contract_key(resolved.target)
    return contract_snapshot(
        target_ref=resolved.ref,
        config_scope=resolved.ref.scope,
        project_root=str(project.root),
        target=resolved.target,
        contract_key=key,
        registry=registry_for_target(resolved.target, key),
    )


@pytest.fixture
def contract_digest(snapshot):
    return contract_hash(snapshot)
