from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from resolver import ResolutionError, resolve_target
from strict_json import StrictJSONError, canonicalize, loads


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
PROJECT_KEY = "PROJECTKEY1234"
PROJECT_SECRET = "project-secret-value"
GLOBAL_KEY = "GLOBALKEY12345"
GLOBAL_SECRET = "global-secret-value"


def target(*, credential="project:images-key", provider="aws-s3", **overrides):
    value = {
        "schema_version": 1,
        "credential": credential,
        "provider": provider,
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "website-images/",
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


def credential(access_key=PROJECT_KEY, secret=PROJECT_SECRET, token="", expires=None):
    return {
        "access_key_id": access_key,
        "secret_access_key": secret,
        "session_token": token,
        "expires_at": expires,
    }


def write_project(project: Path, *, targets, config=None, credentials=None):
    target_dir = project / ".s3-upload" / "targets"
    target_dir.mkdir(parents=True)
    for name, value in targets.items():
        (target_dir / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    if config is not None:
        (project / ".s3-upload" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if credentials is not None:
        env_local = project / ".env.local"
        env_local.write_text(
            "S3_UPLOAD_PROJECT_CREDENTIALS_JSON='"
            + json.dumps(credentials, separators=(",", ":"))
            + "'\n",
            encoding="utf-8",
        )
        env_local.chmod(0o600)


def write_home(home: Path, *, targets, credentials):
    target_dir = home / "targets"
    target_dir.mkdir(parents=True)
    home.chmod(0o700)
    target_dir.chmod(0o700)
    for name, value in targets.items():
        path = target_dir / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
    env_file = home / ".env"
    env_file.write_text(
        "S3_UPLOAD_GLOBAL_CREDENTIALS_JSON='"
        + json.dumps(credentials, separators=(",", ":"))
        + "'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)


def test_strict_json_rejects_duplicates_and_has_stable_jcs():
    with pytest.raises(StrictJSONError, match="duplicate key"):
        loads('{"outer":{"name":1,"name":2}}')
    with pytest.raises(StrictJSONError, match="floating-point"):
        loads('{"value":1.5}')
    assert canonicalize({"z": "中", "a": 1}) == b'{"a":1,"z":"\xe4\xb8\xad"}'


def test_explicit_project_target_loads_one_atomic_credential_map(tmp_path):
    write_project(
        tmp_path,
        targets={"website-images": target()},
        credentials={"images-key": credential()},
    )

    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ={},
        cli_target="project:website-images",
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )

    assert resolved.ref.text == "project:website-images"
    assert resolved.source == "cli"
    assert resolved.endpoint == "https://s3.amazonaws.com"
    assert resolved.addressing == "virtual"
    assert resolved.credential.access_key_id == PROJECT_KEY
    assert resolved.credential_source == "project-env-local"
    assert resolved.target_fingerprint.startswith("sha256:")


def test_project_credential_map_duplicate_assignment_uses_last_value(tmp_path):
    write_project(
        tmp_path,
        targets={"website-images": target()},
        credentials=None,
    )
    first = json.dumps(
        {"images-key": credential("FIRSTKEY1234", "first-secret-value")},
        separators=(",", ":"),
    )
    last = json.dumps(
        {"images-key": credential("LASTKEY12345", "last-secret-value")},
        separators=(",", ":"),
    )
    env_local = tmp_path / ".env.local"
    env_local.write_text(
        f"S3_UPLOAD_PROJECT_CREDENTIALS_JSON='{first}'\n"
        f"S3_UPLOAD_PROJECT_CREDENTIALS_JSON='{last}'\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)

    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target="project:website-images", cli_caller=None,
        use_local_key=False, now=NOW,
    )

    assert resolved.credential.access_key_id == "LASTKEY12345"


@pytest.mark.parametrize(
    "selector_line",
    [
        'S3_UPLOAD_TARGET = "project:website-images" # selected target',
        "S3_UPLOAD_TARGET=project:website-images # selected target",
    ],
)
def test_selector_dotenv_preserves_supported_inline_comment_forms(
    tmp_path, selector_line,
):
    write_project(
        tmp_path,
        targets={"website-images": target()},
        credentials={"images-key": credential()},
    )
    with (tmp_path / ".env.local").open("a", encoding="utf-8") as stream:
        stream.write(selector_line + "\n")

    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller=None, use_local_key=False, now=NOW,
    )

    assert resolved.ref.text == "project:website-images"


def test_mapping_beats_default_and_cli_beats_mapping(tmp_path):
    write_project(
        tmp_path,
        targets={
            "website-images": target(),
            "temporary-builds": target(prefix="temporary-builds/"),
        },
        config={
            "schema_version": 1,
            "default_target": "project:temporary-builds",
            "skill_targets": {"image-2": "project:website-images"},
        },
        credentials={"images-key": credential()},
    )

    mapped = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller="image-2", use_local_key=False, now=NOW,
    )
    explicit = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target="project:temporary-builds", cli_caller="image-2",
        use_local_key=False, now=NOW,
    )

    assert (mapped.ref.name, mapped.source) == ("website-images", "skill-mapping")
    assert (explicit.ref.name, explicit.source) == ("temporary-builds", "cli")


def test_indirect_global_target_requires_home_authorization(tmp_path):
    config_home = tmp_path / "home"
    write_home(
        config_home,
        targets={"shared": target(credential="global:archive-key")},
        credentials={"archive-key": credential(GLOBAL_KEY, GLOBAL_SECRET)},
    )
    write_project(
        tmp_path,
        targets={},
        config={
            "schema_version": 1,
            "default_target": None,
            "skill_targets": {"pdf2markdown": "global:shared"},
        },
    )

    with pytest.raises(ResolutionError, match="requires --use-local-key"):
        resolve_target(
            cwd=str(tmp_path), config_home=str(config_home), environ={},
            cli_target=None, cli_caller="pdf2markdown", use_local_key=False, now=NOW,
        )

    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(config_home), environ={},
        cli_target=None, cli_caller="pdf2markdown", use_local_key=True, now=NOW,
    )
    assert resolved.ref.text == "global:shared"
    assert resolved.credential.access_key_id == GLOBAL_KEY


def test_explicit_process_global_selection_authorizes_home(tmp_path):
    config_home = tmp_path / "home"
    write_home(
        config_home,
        targets={"shared": target(credential="global:archive-key")},
        credentials={"archive-key": credential(GLOBAL_KEY, GLOBAL_SECRET)},
    )

    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(config_home),
        environ={"S3_UPLOAD_TARGET": "global:shared"}, cli_target=None,
        cli_caller=None, use_local_key=False, now=NOW,
    )
    assert (resolved.source, resolved.credential_source) == ("process", "global-env")


def test_global_credential_map_duplicate_assignment_uses_last_value(tmp_path):
    config_home = tmp_path / "home"
    write_home(
        config_home,
        targets={"shared": target(credential="global:archive-key")},
        credentials={},
    )
    first = json.dumps(
        {"archive-key": credential("FIRSTGLOBAL1", "first-global-secret")},
        separators=(",", ":"),
    )
    last = json.dumps(
        {"archive-key": credential("LASTGLOBAL12", "last-global-secret")},
        separators=(",", ":"),
    )
    env_file = config_home / ".env"
    env_file.write_text(
        f"S3_UPLOAD_GLOBAL_CREDENTIALS_JSON='{first}'\n"
        f"S3_UPLOAD_GLOBAL_CREDENTIALS_JSON='{last}'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(config_home), environ={},
        cli_target="global:shared", cli_caller=None,
        use_local_key=False, now=NOW,
    )

    assert resolved.credential.access_key_id == "LASTGLOBAL12"


def test_project_target_never_reads_global_credential(tmp_path):
    config_home = tmp_path / "home"
    write_home(
        config_home,
        targets={},
        credentials={"archive-key": credential(GLOBAL_KEY, GLOBAL_SECRET)},
    )
    write_project(
        tmp_path,
        targets={"bad": target(credential="global:archive-key")},
        credentials={"images-key": credential()},
    )

    with pytest.raises(ResolutionError, match="same scope"):
        resolve_target(
            cwd=str(tmp_path), config_home=str(config_home), environ={},
            cli_target="project:bad", cli_caller=None, use_local_key=True, now=NOW,
        )


def test_selected_missing_credential_is_a_preflight_state(tmp_path):
    write_project(tmp_path, targets={"images": target()}, credentials={"other": credential()})
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target="project:images", cli_caller=None, use_local_key=False, now=NOW,
    )
    assert resolved.credential is None
    assert resolved.credential_state == "credential_unavailable"


def test_temporary_credential_requires_more_than_sixty_seconds(tmp_path):
    write_project(
        tmp_path,
        targets={"images": target()},
        credentials={
            "images-key": credential(
                token="temporary-session-token",
                expires="2026-07-22T12:01:00Z",
            )
        },
    )
    with pytest.raises(ResolutionError, match="more than 60") as exc:
        resolve_target(
            cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
            cli_target="project:images", cli_caller=None, use_local_key=False, now=NOW,
        )
    assert exc.value.resolved.credential is None


def test_env_file_may_not_contain_project_secret_map(tmp_path):
    write_project(tmp_path, targets={"images": target()}, credentials={"images-key": credential()})
    (tmp_path / ".env").write_text("S3_UPLOAD_PROJECT_CREDENTIALS_JSON={}\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="must not appear in .env"):
        resolve_target(
            cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
            cli_target="project:images", cli_caller=None, use_local_key=False, now=NOW,
        )


def test_target_rejects_unknown_fields_before_credentials(tmp_path):
    invalid = target()
    invalid["surprise"] = True
    write_project(tmp_path, targets={"images": invalid}, credentials={"images-key": credential()})
    with pytest.raises(ResolutionError, match="unknown fields"):
        resolve_target(
            cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
            cli_target="project:images", cli_caller=None, use_local_key=False, now=NOW,
        )


def test_private_retained_target_allows_an_empty_prefix(tmp_path):
    write_project(
        tmp_path,
        targets={"root": target(prefix="")},
        credentials={"images-key": credential()},
    )
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target="project:root", cli_caller=None, use_local_key=False, now=NOW,
    )
    assert resolved.target.prefix == ""
