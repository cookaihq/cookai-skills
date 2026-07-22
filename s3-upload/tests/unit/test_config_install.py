from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from config_install import (
    ConfigurationChanged,
    ConfigurationConflict,
    InstallError,
    InstallSpec,
    NewCredentialNameRequired,
    SelectorChange,
    SimulatedCrash,
    analyze_configuration,
    apply_install_plan,
    preflight_installation,
    repair_installation,
)
from resolver import resolve_target


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def credential(access_key="PROJECTKEY1234", secret="project-secret-value"):
    return {
        "access_key_id": access_key,
        "secret_access_key": secret,
        "session_token": "",
        "expires_at": None,
    }


def target(*, credential_ref="project:images-key", prefix="website-images/"):
    return {
        "schema_version": 1,
        "credential": credential_ref,
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": prefix,
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


def install_spec(project: Path, **changes):
    values = {
        "project_root": str(project),
        "config_home": str(project / "home"),
        "target_ref": "project:website-images",
        "target": target(),
        "credential_ref": "project:images-key",
        "credential": credential(),
        "selector_change": SelectorChange(
            kind="project-default",
            caller_skill=None,
            before=None,
            after="project:website-images",
        ),
        "environ": {},
        "now": NOW,
    }
    values.update(changes)
    return InstallSpec(**values)


def test_new_project_configuration_installs_as_a_resolvable_graph(tmp_path):
    plan = preflight_installation(install_spec(tmp_path))

    assert plan.analysis.credential.disposition == "create"
    assert plan.analysis.target.disposition == "create"
    assert plan.analysis.selector.disposition == "create"

    result = apply_install_plan(plan)

    assert result.status == "installed"
    assert result.stages == ("credential", "target", "selector")
    resolved = resolve_target(
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ=result.environ,
        cli_target=None,
        cli_caller=None,
        use_local_key=False,
        now=NOW,
    )
    assert resolved.ref.text == "project:website-images"
    assert resolved.credential.access_key_id == "PROJECTKEY1234"
    assert (tmp_path / ".env.local").stat().st_mode & 0o777 == 0o600


def test_equal_records_are_idempotent_without_rewriting_files(tmp_path):
    spec = install_spec(tmp_path)
    apply_install_plan(preflight_installation(spec))
    paths = (
        tmp_path / ".env.local",
        tmp_path / ".s3-upload" / "targets" / "website-images.json",
        tmp_path / ".s3-upload" / "config.json",
    )
    identities = tuple(path.stat().st_ino for path in paths)

    plan = preflight_installation(spec)
    result = apply_install_plan(plan)

    assert plan.analysis.credential.disposition == "idempotent"
    assert plan.analysis.target.disposition == "idempotent"
    assert plan.analysis.selector.disposition == "idempotent"
    assert result.status == "idempotent"
    assert result.stages == ()
    assert tuple(path.stat().st_ino for path in paths) == identities


def test_credential_conflict_reports_dependents_and_requires_approval(tmp_path):
    apply_install_plan(preflight_installation(install_spec(tmp_path)))
    second = target(prefix="archive/")
    second_path = tmp_path / ".s3-upload" / "targets" / "archive.json"
    second_path.write_text(json.dumps(second), encoding="utf-8")
    second_path.chmod(0o600)
    changed = install_spec(tmp_path, credential=credential(secret="replacement-secret-value"))
    before = (tmp_path / ".env.local").read_bytes()

    analysis = analyze_configuration(changed)

    assert analysis.credential.disposition == "conflict"
    assert analysis.credential.dependencies == (
        "project:archive",
        "project:website-images",
    )
    assert "replacement-secret-value" not in repr(analysis)
    with pytest.raises(ConfigurationConflict, match="credential"):
        preflight_installation(changed)
    assert (tmp_path / ".env.local").read_bytes() == before


def test_git_secret_gate_can_install_one_exact_local_exclude_rule(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    spec = install_spec(tmp_path)

    with pytest.raises(InstallError, match="ignored"):
        preflight_installation(spec)

    plan = preflight_installation(
        install_spec(tmp_path, install_local_exclude=True)
    )
    result = apply_install_plan(plan)

    assert result.status == "installed"
    exclude = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--git-path", "info/exclude"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    exclude_path = Path(exclude) if Path(exclude).is_absolute() else tmp_path / exclude
    assert "/.env.local" in exclude_path.read_text(encoding="utf-8").splitlines()
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "--no-index", "-q", ".env.local"]
    ).returncode == 0


def test_git_secret_gate_rejects_a_tracked_and_ignored_destination(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env_file = tmp_path / ".env.local"
    env_file.write_text("OTHER=value\n", encoding="utf-8")
    env_file.chmod(0o600)
    exclude = tmp_path / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("/.env.local\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env.local"], check=True)

    with pytest.raises(InstallError, match="tracked"):
        preflight_installation(install_spec(tmp_path))

    assert env_file.read_text(encoding="utf-8") == "OTHER=value\n"
    assert not (tmp_path / ".s3-upload").exists()


def test_credential_map_merge_preserves_unrelated_dotenv_and_collapses_duplicates(tmp_path):
    existing = {"old-key": credential("OLDACCESS1234", "old-secret-value")}
    compact = json.dumps(existing, separators=(",", ":"))
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "# retained comment\nOTHER_BINDING=literal\n"
        f"S3_UPLOAD_PROJECT_CREDENTIALS_JSON={compact}\n"
        f"S3_UPLOAD_PROJECT_CREDENTIALS_JSON='{compact}'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    selector = SelectorChange(
        kind="skill-target",
        caller_skill="image-2",
        before=None,
        after="project:new-images",
    )
    spec = install_spec(
        tmp_path,
        target_ref="project:new-images",
        target=target(credential_ref="project:new-key"),
        credential_ref="project:new-key",
        credential=credential("NEWACCESS1234", "new-secret-value"),
        selector_change=selector,
    )

    apply_install_plan(preflight_installation(spec))

    output = env_file.read_text(encoding="utf-8")
    assert "# retained comment" in output
    assert "OTHER_BINDING=literal" in output
    assignments = [
        line.split("=", 1)[1]
        for line in output.splitlines()
        if line.startswith("S3_UPLOAD_PROJECT_CREDENTIALS_JSON=")
    ]
    assert len(assignments) == 1
    installed = json.loads(assignments[0])
    assert set(installed) == {"old-key", "new-key"}


def test_selector_update_preserves_other_default_and_mappings(tmp_path):
    config_dir = tmp_path / ".s3-upload"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps({
            "schema_version": 1,
            "default_target": "project:temporary-builds",
            "skill_targets": {"pdf2markdown": "global:shared-documents"},
        }),
        encoding="utf-8",
    )
    selector = SelectorChange(
        kind="skill-target",
        caller_skill="image-2",
        before=None,
        after="project:website-images",
    )

    apply_install_plan(preflight_installation(
        install_spec(tmp_path, selector_change=selector)
    ))

    installed = json.loads(config_path.read_text(encoding="utf-8"))
    assert installed == {
        "schema_version": 1,
        "default_target": "project:temporary-builds",
        "skill_targets": {
            "image-2": "project:website-images",
            "pdf2markdown": "global:shared-documents",
        },
    }


def test_plan_snapshots_hide_secret_hashes_and_cas_prevents_all_writes(tmp_path):
    config_dir = tmp_path / ".s3-upload"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "default_target": None, "skill_targets": {}}),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.local"
    env_file.write_text("OTHER_BINDING=literal\n", encoding="utf-8")
    env_file.chmod(0o600)
    plan = preflight_installation(install_spec(tmp_path))
    snapshots = {entry.role: entry.snapshot.public_record() for entry in plan.files}

    assert "sha256" not in snapshots["credential"]
    assert snapshots["credential"]["version_token"]["mode"] == "0600"
    assert snapshots["selector"]["sha256"].startswith("sha256:")
    assert plan.lock_paths == tuple(sorted(plan.lock_paths))

    target_path = tmp_path / ".s3-upload" / "targets" / "website-images.json"
    target_path.parent.mkdir()
    target_path.write_text("concurrent writer", encoding="utf-8")
    before_secret = env_file.read_bytes()

    with pytest.raises(ConfigurationChanged):
        apply_install_plan(plan)

    assert env_file.read_bytes() == before_secret
    assert target_path.read_text(encoding="utf-8") == "concurrent writer"
    assert json.loads(config_path.read_text(encoding="utf-8"))["default_target"] is None


def test_process_memory_credential_preserves_map_and_never_writes_secret_file(tmp_path):
    existing = {"other-key": credential("OTHERKEY12345", "other-secret-value")}
    spec = install_spec(
        tmp_path,
        credential_mode="process-memory",
        environ={
            "S3_UPLOAD_PROJECT_CREDENTIALS_JSON": json.dumps(existing, separators=(",", ":"))
        },
    )

    result = apply_install_plan(preflight_installation(spec))

    assert not (tmp_path / ".env.local").exists()
    process_map = json.loads(result.environ["S3_UPLOAD_PROJECT_CREDENTIALS_JSON"])
    assert set(process_map) == {"other-key", "images-key"}
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        environ=result.environ, cli_target=None, cli_caller=None,
        use_local_key=False, now=NOW,
    )
    assert resolved.credential.access_key_id == "PROJECTKEY1234"


def test_persistent_install_rejects_process_shadow_unless_explicitly_cleared(tmp_path):
    process_map = json.dumps(
        {"other-key": credential("OTHERKEY12345", "other-secret-value")},
        separators=(",", ":"),
    )
    shadowed = install_spec(
        tmp_path,
        environ={"S3_UPLOAD_PROJECT_CREDENTIALS_JSON": process_map},
    )

    with pytest.raises(InstallError, match="shadow"):
        preflight_installation(shadowed)

    cleared = install_spec(
        tmp_path,
        environ={"S3_UPLOAD_PROJECT_CREDENTIALS_JSON": process_map},
        process_shadow="clear",
    )
    result = apply_install_plan(preflight_installation(cleared))
    assert "S3_UPLOAD_PROJECT_CREDENTIALS_JSON" not in result.environ
    assert (tmp_path / ".env.local").exists()


def test_linked_target_and_credential_change_requires_a_new_credential_name(tmp_path):
    apply_install_plan(preflight_installation(install_spec(tmp_path)))
    unsafe = install_spec(
        tmp_path,
        target=target(prefix="replacement/"),
        credential=credential(secret="replacement-secret-value"),
        approved_replacements=frozenset({"target", "credential"}),
    )

    with pytest.raises(NewCredentialNameRequired):
        preflight_installation(unsafe)

    safe = install_spec(
        tmp_path,
        target=target(
            credential_ref="project:images-key-v2",
            prefix="replacement/",
        ),
        credential_ref="project:images-key-v2",
        credential=credential("PROJECTKEY5678", "replacement-secret-value"),
        approved_replacements=frozenset({"target"}),
    )
    result = apply_install_plan(preflight_installation(safe))

    assert result.stages == ("credential", "target")
    assignment = next(
        line.split("=", 1)[1]
        for line in (tmp_path / ".env.local").read_text(encoding="utf-8").splitlines()
        if line.startswith("S3_UPLOAD_PROJECT_CREDENTIALS_JSON=")
    )
    assert set(json.loads(assignment)) == {"images-key", "images-key-v2"}
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"),
        environ={}, cli_target=None, cli_caller=None,
        use_local_key=False, now=NOW,
    )
    assert resolved.credential.access_key_id == "PROJECTKEY5678"


@pytest.mark.parametrize(
    "crash_stage",
    ["after-credential", "after-target", "after-selector"],
)
def test_crash_at_each_staged_boundary_repairs_without_breaking_old_selector(tmp_path, crash_stage):
    old_selector = SelectorChange(
        kind="project-default", caller_skill=None,
        before=None, after="project:old-target",
    )
    old = install_spec(
        tmp_path,
        target_ref="project:old-target",
        target=target(credential_ref="project:old-key", prefix="old/"),
        credential_ref="project:old-key",
        credential=credential("OLDACCESS1234", "old-secret-value"),
        selector_change=old_selector,
    )
    apply_install_plan(preflight_installation(old))
    new_selector = SelectorChange(
        kind="project-default", caller_skill=None,
        before="project:old-target", after="project:new-target",
    )
    proposed = install_spec(
        tmp_path,
        target_ref="project:new-target",
        target=target(credential_ref="project:new-key", prefix="new/"),
        credential_ref="project:new-key",
        credential=credential("NEWACCESS1234", "new-secret-value"),
        selector_change=new_selector,
        approved_replacements=frozenset({"selector"}),
    )

    def crash(boundary):
        if boundary == crash_stage:
            raise SimulatedCrash(boundary)

    with pytest.raises(SimulatedCrash):
        apply_install_plan(preflight_installation(proposed), fault=crash)

    before_repair = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=("project:new-target" if crash_stage != "after-credential" else None),
        cli_caller=None, use_local_key=False, now=NOW,
    )
    if crash_stage == "after-credential":
        assert before_repair.ref.text == "project:old-target"
    elif crash_stage == "after-target":
        assert before_repair.ref.text == "project:new-target"
        selected_default = resolve_target(
            cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
            cli_target=None, cli_caller=None, use_local_key=False, now=NOW,
        )
        assert selected_default.ref.text == "project:old-target"

    repair_installation(proposed)
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller=None, use_local_key=False, now=NOW,
    )
    assert resolved.ref.text == "project:new-target"
    assert resolved.credential.access_key_id == "NEWACCESS1234"


def test_ordinary_failure_rolls_back_unreferenced_staged_records(tmp_path):
    old_selector = SelectorChange(
        kind="project-default", caller_skill=None,
        before=None, after="project:old-target",
    )
    old = install_spec(
        tmp_path,
        target_ref="project:old-target",
        target=target(credential_ref="project:old-key", prefix="old/"),
        credential_ref="project:old-key",
        credential=credential("OLDACCESS1234", "old-secret-value"),
        selector_change=old_selector,
    )
    apply_install_plan(preflight_installation(old))
    proposed = install_spec(
        tmp_path,
        target_ref="project:new-target",
        target=target(credential_ref="project:new-key", prefix="new/"),
        credential_ref="project:new-key",
        credential=credential("NEWACCESS1234", "new-secret-value"),
        selector_change=SelectorChange(
            kind="project-default", caller_skill=None,
            before="project:old-target", after="project:new-target",
        ),
        approved_replacements=frozenset({"selector"}),
    )

    def fail(boundary):
        if boundary == "after-target":
            raise OSError("synthetic ordinary failure")

    with pytest.raises(InstallError, match="rolled back"):
        apply_install_plan(preflight_installation(proposed), fault=fail)

    assert not (tmp_path / ".s3-upload" / "targets" / "new-target.json").exists()
    credentials_line = next(
        line.split("=", 1)[1]
        for line in (tmp_path / ".env.local").read_text(encoding="utf-8").splitlines()
        if line.startswith("S3_UPLOAD_PROJECT_CREDENTIALS_JSON=")
    )
    assert set(json.loads(credentials_line)) == {"old-key"}
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller=None, use_local_key=False, now=NOW,
    )
    assert resolved.ref.text == "project:old-target"


def test_global_install_is_resolvable_without_creating_a_global_selector(tmp_path):
    spec = install_spec(
        tmp_path,
        target_ref="global:shared-documents",
        target=target(
            credential_ref="global:archive-key",
            prefix="documents/",
        ),
        credential_ref="global:archive-key",
        credential=credential("GLOBALKEY12345", "global-secret-value"),
        selector_change=None,
    )

    result = apply_install_plan(preflight_installation(spec))

    home = tmp_path / "home"
    assert result.stages == ("credential", "target")
    assert (home.stat().st_mode & 0o777) == 0o700
    assert ((home / "targets").stat().st_mode & 0o777) == 0o700
    assert ((home / ".env").stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / ".s3-upload" / "config.json").exists()
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(home), environ={},
        cli_target="global:shared-documents", cli_caller=None,
        use_local_key=False, now=NOW,
    )
    assert resolved.credential.access_key_id == "GLOBALKEY12345"


def test_global_target_replacement_requires_unknown_consumer_acknowledgement(tmp_path):
    original = install_spec(
        tmp_path,
        target_ref="global:shared",
        target=target(credential_ref="global:key-main", prefix="old/"),
        credential_ref="global:key-main",
        credential=credential("GLOBALKEY12345", "global-secret-value"),
        selector_change=None,
    )
    apply_install_plan(preflight_installation(original))
    replacement = install_spec(
        tmp_path,
        target_ref="global:shared",
        target=target(credential_ref="global:key-main", prefix="new/"),
        credential_ref="global:key-main",
        credential=credential("GLOBALKEY12345", "global-secret-value"),
        selector_change=None,
        approved_replacements=frozenset({"target"}),
    )
    analysis = analyze_configuration(replacement)
    assert "cannot be enumerated" in analysis.target.warning

    with pytest.raises(ConfigurationConflict, match="unknown-impact"):
        preflight_installation(replacement)

    acknowledged = install_spec(
        tmp_path,
        target_ref="global:shared",
        target=target(credential_ref="global:key-main", prefix="new/"),
        credential_ref="global:key-main",
        credential=credential("GLOBALKEY12345", "global-secret-value"),
        selector_change=None,
        approved_replacements=frozenset({"target"}),
        acknowledge_global_target_impact=True,
    )
    assert apply_install_plan(preflight_installation(acknowledged)).stages == ("target",)


def test_malformed_credential_dependent_disables_replacement(tmp_path):
    apply_install_plan(preflight_installation(install_spec(tmp_path)))
    malformed = tmp_path / ".s3-upload" / "targets" / "broken.json"
    malformed.write_text("{}", encoding="utf-8")
    changed = install_spec(
        tmp_path,
        credential=credential(secret="replacement-secret-value"),
        approved_replacements=frozenset({"credential"}),
    )

    analysis = analyze_configuration(changed)
    assert not analysis.credential.replacement_allowed
    with pytest.raises(ConfigurationConflict, match="new name"):
        preflight_installation(changed)


def test_selector_before_must_match_current_value(tmp_path):
    apply_install_plan(preflight_installation(install_spec(tmp_path)))
    drifted = install_spec(
        tmp_path,
        target_ref="project:new-target",
        target=target(credential_ref="project:new-key", prefix="new/"),
        credential_ref="project:new-key",
        credential=credential("NEWACCESS1234", "new-secret-value"),
        selector_change=SelectorChange(
            kind="project-default", caller_skill=None,
            before="project:not-the-current-target",
            after="project:new-target",
        ),
        approved_replacements=frozenset({"selector"}),
    )

    with pytest.raises(ConfigurationChanged, match="selector"):
        preflight_installation(drifted)


def test_git_state_change_after_preflight_stops_before_configuration_writes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env_file = tmp_path / ".env.local"
    env_file.write_text("OTHER=value\n", encoding="utf-8")
    env_file.chmod(0o600)
    exclude = tmp_path / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("/.env.local\n")
    plan = preflight_installation(install_spec(tmp_path))
    before = env_file.read_bytes()
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env.local"], check=True)

    with pytest.raises(InstallError, match="tracked"):
        apply_install_plan(plan)

    assert env_file.read_bytes() == before
    assert not (tmp_path / ".s3-upload").exists()


def test_preflight_rejects_symlinked_configuration_and_unsafe_secret_mode(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".s3-upload").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallError, match="unsafe"):
        preflight_installation(install_spec(tmp_path))

    (tmp_path / ".s3-upload").unlink()
    env_file = tmp_path / ".env.local"
    env_file.write_text("OTHER=value\n", encoding="utf-8")
    env_file.chmod(0o644)
    with pytest.raises(InstallError, match="unsafe|0600"):
        preflight_installation(install_spec(tmp_path))


def test_non_secret_cas_detects_same_length_change_with_restored_mtime(tmp_path):
    config_dir = tmp_path / ".s3-upload"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    original = b'{"schema_version":1,"default_target":null,"skill_targets":{}}'
    config_path.write_bytes(original)
    plan = preflight_installation(install_spec(tmp_path))
    before = config_path.stat()
    changed = original.replace(b"null", b'"xx"')
    assert len(changed) == len(original)
    config_path.write_bytes(changed)
    os.utime(config_path, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(ConfigurationChanged):
        apply_install_plan(plan)

    assert config_path.read_bytes() == changed
    assert not (tmp_path / ".env.local").exists()


def test_in_memory_plan_and_result_representations_do_not_leak_secrets(tmp_path):
    spec = install_spec(tmp_path, credential_mode="process-memory")
    plan = preflight_installation(spec)
    result = apply_install_plan(plan)

    for rendered in (repr(spec), repr(plan), repr(result), repr(plan.analysis)):
        assert "project-secret-value" not in rendered
        assert "PROJECTKEY1234" not in rendered


def test_local_exclude_change_after_preflight_is_not_overwritten(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    plan = preflight_installation(
        install_spec(tmp_path, install_local_exclude=True)
    )
    exclude = tmp_path / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("/concurrent-entry\n")
    changed = exclude.read_bytes()

    with pytest.raises(ConfigurationChanged):
        apply_install_plan(plan)

    assert exclude.read_bytes() == changed
    assert not (tmp_path / ".env.local").exists()
    assert not (tmp_path / ".s3-upload").exists()


def test_absent_target_publication_never_replaces_a_racing_writer(tmp_path):
    plan = preflight_installation(install_spec(tmp_path))
    target_path = tmp_path / ".s3-upload" / "targets" / "website-images.json"

    def race(boundary):
        if boundary == "before-target":
            target_path.parent.mkdir(parents=True)
            target_path.write_text("racing writer", encoding="utf-8")

    with pytest.raises(InstallError, match="rollback was incomplete"):
        apply_install_plan(plan, fault=race)

    assert target_path.read_text(encoding="utf-8") == "racing writer"
    assert not (tmp_path / ".env.local").exists()
    assert not (tmp_path / ".s3-upload" / "config.json").exists()
