import fcntl
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import workflow


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PDF_SHA256 = "d7dd0115be8b79ae057b3f6ca0fcee578085ba6919dcb70e8643a2aff537d9b5"
NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
DEFAULT_CONFIG_HOME = object()


def no_network(*_args, **_kwargs):
    raise AssertionError("local bundle creation must not use the network")


def invoke(
    capsys,
    argv,
    *,
    cwd,
    environ=None,
    now=NOW,
    config_home=DEFAULT_CONFIG_HOME,
):
    injected_config_home = (
        str(Path(cwd) / "config-home")
        if config_home is DEFAULT_CONFIG_HOME
        else config_home
    )
    rc = workflow.main(
        argv,
        environ={} if environ is None else environ,
        cwd=str(cwd),
        config_home=injected_config_home,
        transport=no_network,
        now=now,
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    return rc, json.loads(lines[0]), captured.err


def test_settings_commands_initialize_report_and_update_private_behavior_settings(
    tmp_path, capsys
):
    config_home = tmp_path / "config-home"
    settings_path = config_home / "settings.json"

    status_rc, status, status_stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=tmp_path,
    )

    assert status_rc == 0
    assert status["outcome"] == "settings_status"
    assert status["settings"] == {
        "path": str(settings_path),
        "exists": False,
        "persisted": None,
        "effective": {
            "schema_version": 1,
            "interaction_mode": "confirm",
            "publishing": {
                "mode": "skip",
                "publisher_binding": None,
            },
            "sources": {
                "interaction_mode": "built_in_default",
                "publishing.mode": "built_in_default",
            },
        },
        "content_hash": None,
        "home_config_authorized": False,
        "publication_execution": {
            "executable": False,
            "reason_code": "publication_skipped",
        },
    }
    assert "settings_status" in status_stderr
    assert not settings_path.exists()

    previous_umask = os.umask(0)
    try:
        init_rc, initialized, init_stderr = invoke(
            capsys,
            ["settings", "init"],
            cwd=tmp_path,
        )
    finally:
        os.umask(previous_umask)

    assert init_rc == 0
    assert initialized["outcome"] == "settings_initialized"
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert json.loads(settings_path.read_text()) == {
        "schema_version": 1,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
    }
    assert "settings_initialized" in init_stderr

    update_rc, updated, update_stderr = invoke(
        capsys,
        ["settings", "set-mode", "auto"],
        cwd=tmp_path,
    )

    assert update_rc == 0
    assert updated["outcome"] == "settings_updated"
    assert updated["settings"]["persisted"]["interaction_mode"] == "auto"
    assert updated["settings"]["effective"]["interaction_mode"] == "auto"
    assert updated["settings"]["effective"]["sources"]["interaction_mode"] == (
        "persistent_settings"
    )
    assert json.loads(settings_path.read_text()) == {
        "schema_version": 1,
        "interaction_mode": "auto",
        "publishing": {"mode": "skip", "publisher_binding": None},
    }
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert "settings_updated" in update_stderr

    publish_rc, publish_updated, publish_stderr = invoke(
        capsys,
        ["settings", "set-publish-mode", "upload"],
        cwd=tmp_path,
    )

    assert publish_rc == 0
    assert publish_updated["outcome"] == "settings_updated"
    assert publish_updated["settings"]["persisted"]["publishing"] == {
        "mode": "upload",
        "publisher_binding": None,
    }
    assert publish_updated["settings"]["publication_execution"] == {
        "executable": False,
        "reason_code": "publisher_binding_missing",
    }
    assert "settings_updated" in publish_stderr


def test_settings_reject_malformed_duplicate_unknown_and_invalid_schema_values(
    tmp_path, capsys
):
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    settings_path = config_home / "settings.json"
    valid = {
        "schema_version": 1,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
    }
    cases = [
        "{",
        '{"schema_version":1,"schema_version":1,"interaction_mode":"confirm",'
        '"publishing":{"mode":"skip","publisher_binding":null}}',
        '{"schema_version":1,"interaction_mode":"confirm","publishing":'
        '{"mode":"skip","mode":"skip","publisher_binding":null}}',
        json.dumps({**valid, "unexpected": True}),
        json.dumps(
            {
                **valid,
                "publishing": {
                    **valid["publishing"],
                    "unexpected": True,
                },
            }
        ),
        json.dumps({**valid, "schema_version": 2}),
        json.dumps({**valid, "schema_version": True}),
        json.dumps({**valid, "interaction_mode": 1}),
        json.dumps({**valid, "interaction_mode": []}),
        json.dumps({**valid, "interaction_mode": "AUTO"}),
        json.dumps({**valid, "publishing": []}),
        json.dumps(
            {
                **valid,
                "publishing": {
                    "mode": "publish",
                    "publisher_binding": None,
                },
            }
        ),
        json.dumps(
            {
                **valid,
                "publishing": {"mode": {}, "publisher_binding": None},
            }
        ),
        json.dumps(
            {
                **valid,
                "publishing": {
                    "mode": "upload",
                    "publisher_binding": {"partial": True},
                },
            }
        ),
        json.dumps(
            {
                **valid,
                "publishing": {
                    "mode": "upload",
                    "target_ref": "https://storage.example/object?signature=secret",
                    "publisher_binding": None,
                },
            }
        ),
    ]

    for raw in cases:
        settings_path.write_text(raw, encoding="utf-8")
        before = settings_path.read_bytes()

        rc, result, stderr = invoke(
            capsys,
            ["settings", "status"],
            cwd=tmp_path,
        )

        assert rc == 6
        assert result["outcome"] == "error"
        assert result["action_required"] == "repair_settings"
        assert result["errors"] == [
            {
                "code": "configuration_invalid",
                "message": "Persistent settings are invalid.",
            }
        ]
        assert "configuration_invalid" in stderr
        assert settings_path.read_bytes() == before

    settings_path.write_text(json.dumps(valid), encoding="utf-8")
    secret_target = "https://storage.example/object?signature=must-not-leak"
    secret_rc, secret_error, secret_stderr = invoke(
        capsys,
        ["settings", "status", "--publish-target", secret_target],
        cwd=tmp_path,
    )
    assert secret_rc == 6
    assert secret_error["errors"][0]["code"] == "configuration_invalid"
    assert secret_target not in json.dumps(secret_error) + secret_stderr


def test_settings_atomic_update_failure_preserves_the_previous_complete_document(
    tmp_path, capsys, monkeypatch
):
    settings_path = tmp_path / "config-home" / "settings.json"
    init_rc, _initialized, _stderr = invoke(
        capsys,
        ["settings", "init"],
        cwd=tmp_path,
    )
    before = settings_path.read_bytes()
    original_replace = workflow.settings_module.os.replace

    def fail_replace(source, destination):
        temporary = Path(source)
        assert destination == settings_path
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
        assert json.loads(temporary.read_text())["interaction_mode"] == "auto"
        raise OSError("injected replace failure")

    monkeypatch.setattr(workflow.settings_module.os, "replace", fail_replace)
    rc, result, stderr = invoke(
        capsys,
        ["settings", "set-mode", "auto"],
        cwd=tmp_path,
    )
    monkeypatch.setattr(workflow.settings_module.os, "replace", original_replace)

    assert init_rc == 0
    assert rc == 6
    assert result["outcome"] == "error"
    assert result["action_required"] == "retry_settings_write"
    assert result["errors"] == [
        {
            "code": "settings_write_failed",
            "message": "Persistent settings could not be written atomically.",
        }
    ]
    assert "settings_write_failed" in stderr
    assert settings_path.read_bytes() == before
    assert list(settings_path.parent.glob(".settings.json.*")) == []


def test_settings_commands_validate_effective_layers_before_writing(tmp_path, capsys):
    settings_path = tmp_path / "config-home" / "settings.json"

    init_rc, init_error, _stderr = invoke(
        capsys,
        ["settings", "init"],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_INTERACTION_MODE": "invalid"},
    )

    assert init_rc == 6
    assert init_error["errors"][0]["code"] == "configuration_invalid"
    assert not settings_path.exists()

    valid = {
        "schema_version": 1,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
    }
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps(valid), encoding="utf-8")
    before = settings_path.read_bytes()

    update_rc, update_error, _stderr = invoke(
        capsys,
        ["settings", "set-mode", "auto"],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_PUBLISH_MODE": "invalid"},
    )

    assert update_rc == 6
    assert update_error["errors"][0]["code"] == "configuration_invalid"
    assert settings_path.read_bytes() == before


def test_settings_resolve_each_nonsecret_value_by_first_nonempty_source(
    tmp_path, capsys
):
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    settings_path = config_home / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "interaction_mode": "auto",
                "publishing": {
                    "mode": "upload",
                    "uploader": "skill:persisted",
                    "target_ref": "persisted-target",
                    "publisher_binding": None,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "PDF2MARKDOWN_INTERACTION_MODE=confirm\n"
        "PDF2MARKDOWN_PUBLISH_MODE=skip\n"
        "PDF2MARKDOWN_UPLOADER=skill:dotenv\n"
        "PDF2MARKDOWN_UPLOAD_TARGET=dotenv-target\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text(
        "PDF2MARKDOWN_INTERACTION_MODE=\n"
        "PDF2MARKDOWN_PUBLISH_MODE=upload\n"
        "PDF2MARKDOWN_UPLOADER=\n"
        "PDF2MARKDOWN_UPLOAD_TARGET=local-target\n",
        encoding="utf-8",
    )

    cli_rc, cli_result, _stderr = invoke(
        capsys,
        [
            "settings",
            "status",
            "--interaction-mode",
            "confirm",
            "--publish-mode",
            "skip",
            "--publish-with",
            "tool:cli",
            "--publish-target",
            "cli-target",
        ],
        cwd=tmp_path,
        environ={
            "PDF2MARKDOWN_INTERACTION_MODE": "auto",
            "PDF2MARKDOWN_PUBLISH_MODE": "",
            "PDF2MARKDOWN_UPLOADER": "tool:environment",
            "PDF2MARKDOWN_UPLOAD_TARGET": "",
        },
    )
    cli_effective = cli_result["settings"]["effective"]

    layered_rc, layered_result, _stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=tmp_path,
        environ={
            "PDF2MARKDOWN_INTERACTION_MODE": "auto",
            "PDF2MARKDOWN_PUBLISH_MODE": "",
            "PDF2MARKDOWN_UPLOADER": "tool:environment",
            "PDF2MARKDOWN_UPLOAD_TARGET": "",
        },
    )
    layered_effective = layered_result["settings"]["effective"]

    dotenv_rc, dotenv_result, _stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=tmp_path,
        environ={},
    )
    dotenv_effective = dotenv_result["settings"]["effective"]

    assert cli_rc == layered_rc == dotenv_rc == 0
    assert cli_effective["interaction_mode"] == "confirm"
    assert cli_effective["publishing"] == {
        "mode": "skip",
        "uploader": "tool:cli",
        "target_ref": "cli-target",
        "publisher_binding": None,
    }
    assert cli_effective["sources"] == {
        "interaction_mode": "command_line",
        "publishing.mode": "command_line",
        "publishing.uploader": "command_line",
        "publishing.target_ref": "command_line",
    }
    assert layered_effective["interaction_mode"] == "auto"
    assert layered_effective["publishing"] == {
        "mode": "upload",
        "uploader": "tool:environment",
        "target_ref": "local-target",
        "publisher_binding": None,
    }
    assert layered_effective["sources"] == {
        "interaction_mode": "process_environment",
        "publishing.mode": "cwd_dotenv_local",
        "publishing.uploader": "process_environment",
        "publishing.target_ref": "cwd_dotenv_local",
    }
    assert layered_result["settings"]["publication_execution"] == {
        "executable": False,
        "reason_code": "publisher_binding_missing",
    }
    assert dotenv_effective["interaction_mode"] == "confirm"
    assert dotenv_effective["publishing"] == {
        "mode": "upload",
        "uploader": "skill:dotenv",
        "target_ref": "local-target",
        "publisher_binding": None,
    }
    assert dotenv_effective["sources"] == {
        "interaction_mode": "cwd_dotenv",
        "publishing.mode": "cwd_dotenv_local",
        "publishing.uploader": "cwd_dotenv",
        "publishing.target_ref": "cwd_dotenv_local",
    }

    (tmp_path / ".env.local").unlink()
    (tmp_path / ".env").unlink()
    persistent_rc, persistent_result, _stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=tmp_path,
        environ={},
    )
    assert persistent_rc == 0
    assert persistent_result["settings"]["effective"]["publishing"] == {
        "mode": "upload",
        "uploader": "skill:persisted",
        "target_ref": "persisted-target",
        "publisher_binding": None,
    }
    assert set(
        persistent_result["settings"]["effective"]["sources"].values()
    ) == {"persistent_settings"}


def test_settings_dotenv_is_literal_last_wins_local_only_and_home_is_gated(
    tmp_path, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    config_home = project / "config-home"
    config_home.mkdir()
    (tmp_path / ".env").write_text(
        "PDF2MARKDOWN_PUBLISH_MODE=upload\n", encoding="utf-8"
    )
    (config_home / ".env").write_text(
        "PDF2MARKDOWN_PUBLISH_MODE=upload\n", encoding="utf-8"
    )
    marker = project / "must-not-exist"
    (project / ".env.local").write_text(
        "IGNORED LINE WITHOUT EQUALS\n"
        "PDF2MARKDOWN_INTERACTION_MODE=confirm\n"
        "PDF2MARKDOWN_INTERACTION_MODE='auto'\n"
        "PDF2MARKDOWN_UPLOAD_TARGET=local-target\n",
        encoding="utf-8",
    )

    local_rc, local_result, _stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=project,
    )
    home_rc, home_result, _stderr = invoke(
        capsys,
        ["settings", "status", "--use-local-key"],
        cwd=project,
    )

    local_effective = local_result["settings"]["effective"]
    home_effective = home_result["settings"]["effective"]
    assert local_rc == home_rc == 0
    assert local_effective["interaction_mode"] == "auto"
    assert local_effective["publishing"]["mode"] == "skip"
    assert local_effective["publishing"]["target_ref"] == "local-target"
    assert local_effective["sources"] == {
        "interaction_mode": "cwd_dotenv_local",
        "publishing.mode": "built_in_default",
        "publishing.target_ref": "cwd_dotenv_local",
    }
    assert local_result["settings"]["home_config_authorized"] is False
    assert home_effective["interaction_mode"] == "auto"
    assert home_effective["publishing"]["mode"] == "upload"
    assert home_effective["sources"]["publishing.mode"] == "home_dotenv"
    assert home_result["settings"]["home_config_authorized"] is True
    assert not marker.exists()

    (project / ".env.local").write_text(
        "PDF2MARKDOWN_INTERACTION_MODE=${MODE}\n"
        f"PDF2MARKDOWN_UPLOAD_TARGET=$(touch {marker})\n",
        encoding="utf-8",
    )
    invalid_rc, invalid, _stderr = invoke(
        capsys,
        ["settings", "status"],
        cwd=project,
        environ={"MODE": "confirm"},
    )
    assert invalid_rc == 6
    assert invalid["errors"][0]["code"] == "configuration_invalid"
    assert not marker.exists()


def test_settings_default_config_home_uses_xdg_then_home(tmp_path, capsys):
    xdg_root = tmp_path / "xdg"
    home_root = tmp_path / "home"

    xdg_rc, xdg_result, _stderr = invoke(
        capsys,
        ["settings", "init"],
        cwd=tmp_path,
        environ={"XDG_CONFIG_HOME": str(xdg_root), "HOME": str(home_root)},
        config_home=None,
    )
    home_rc, home_result, _stderr = invoke(
        capsys,
        ["settings", "init"],
        cwd=tmp_path,
        environ={"XDG_CONFIG_HOME": "", "HOME": str(home_root)},
        config_home=None,
    )

    assert xdg_rc == home_rc == 0
    assert xdg_result["settings"]["path"] == str(
        xdg_root / "pdf2markdown" / "settings.json"
    )
    assert home_result["settings"]["path"] == str(
        home_root / ".config" / "pdf2markdown" / "settings.json"
    )


def test_start_freezes_effective_settings_sources_cwd_identity_and_settings_hash(
    tmp_path, capsys
):
    project = tmp_path / "real-project"
    project.mkdir()
    project_alias = tmp_path / "project-alias"
    project_alias.symlink_to(project, target_is_directory=True)
    source = project / "source.pdf"
    source.write_bytes(PDF_BYTES)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    settings_path = config_home / "settings.json"
    settings_raw = (
        '{"schema_version":1,"interaction_mode":"auto","publishing":'
        '{"mode":"skip","publisher_binding":null}}\n'
    )
    settings_path.write_text(settings_raw, encoding="utf-8")
    (project / ".env.local").write_text(
        "PDF2MARKDOWN_PUBLISH_MODE=upload\n", encoding="utf-8"
    )
    (config_home / ".env").write_text(
        "PDF2MARKDOWN_UPLOAD_TARGET=home-target\n", encoding="utf-8"
    )
    secret = "sk-secret-must-never-persist"

    rc, result, stdout_log = invoke(
        capsys,
        [
            "start",
            "--source",
            "source.pdf",
            "--interaction-mode",
            "confirm",
            "--publish-with",
            "tool:configured-uploader",
            "--use-local-key",
        ],
        cwd=project_alias,
        environ={
            "PDF2MARKDOWN_UPLOADER": "tool:environment-uploader",
            "AIHUB_API_KEY": secret,
            "UNRELATED_SECRET": secret,
        },
        config_home=str(config_home),
    )

    bundle = Path(result["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    cwd_info = project.stat()

    assert rc == 0
    assert result["publication_state"] == "blocked"
    assert manifest["publication_state"] == "blocked"
    assert manifest["settings_snapshot"] == {
        "schema_version": 1,
        "interaction_mode": "confirm",
        "publishing": {
            "mode": "upload",
            "uploader": "tool:configured-uploader",
            "target_ref": "home-target",
            "publisher_binding": None,
        },
        "sources": {
            "interaction_mode": "command_line",
            "publishing.mode": "cwd_dotenv_local",
            "publishing.uploader": "command_line",
            "publishing.target_ref": "home_dotenv",
        },
        "invocation_cwd": {
            "path": str(project),
            "device": cwd_info.st_dev,
            "inode": cwd_info.st_ino,
        },
        "settings_file": {
            "path": str(settings_path),
            "content_hash": (
                "sha256:d85a314aa812f29d8f7386f7f0f17239cb2631e323ee85d3aadcea1d0a101886"
            ),
        },
    }
    persisted = (
        (bundle / "manifest.json").read_text()
        + history_text
        + json.dumps(result)
        + stdout_log
    )
    assert secret not in persisted
    assert "use_local_key" not in persisted


def test_start_rejects_invalid_settings_before_creating_a_work_bundle(
    tmp_path, capsys
):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    settings_path = config_home / "settings.json"
    settings_path.write_text("{invalid", encoding="utf-8")

    malformed_rc, malformed, malformed_stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )

    settings_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "interaction_mode": "confirm",
                "publishing": {"mode": "skip", "publisher_binding": None},
            }
        ),
        encoding="utf-8",
    )
    enum_rc, enum_error, enum_stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_INTERACTION_MODE": "AUTO"},
    )

    assert malformed_rc == enum_rc == 6
    assert malformed["work_bundle"] is enum_error["work_bundle"] is None
    assert malformed["errors"][0]["code"] == "configuration_invalid"
    assert enum_error["errors"][0]["code"] == "configuration_invalid"
    assert "configuration_invalid" in malformed_stderr
    assert "configuration_invalid" in enum_stderr
    assert not (tmp_path / "pdf2markdown-output").exists()


def test_resume_uses_snapshot_and_explicit_overrides_append_a_new_generation(
    tmp_path, capsys
):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    settings_path = config_home / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "interaction_mode": "auto",
                "publishing": {"mode": "skip", "publisher_binding": None},
            }
        ),
        encoding="utf-8",
    )
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    original_manifest = json.loads((bundle / "manifest.json").read_text())
    original_history = (bundle / ".state" / "history.ndjson").read_bytes()
    secret = "changed-secret-must-not-persist"
    settings_path.write_text("{invalid", encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "PDF2MARKDOWN_INTERACTION_MODE=confirm\n"
        "PDF2MARKDOWN_PUBLISH_MODE=upload\n",
        encoding="utf-8",
    )
    (config_home / ".env").write_text(
        f"PDF2MARKDOWN_UPLOAD_TARGET={secret}\n", encoding="utf-8"
    )

    unchanged_rc, unchanged, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
            "--use-local-key",
        ],
        cwd=tmp_path,
        environ={
            "PDF2MARKDOWN_INTERACTION_MODE": "confirm",
            "PDF2MARKDOWN_PUBLISH_MODE": "upload",
        },
    )

    assert unchanged_rc == 0
    assert unchanged["outcome"] == "no_progress"
    assert unchanged["generation"] == 1
    assert json.loads((bundle / "manifest.json").read_text()) == original_manifest
    assert (bundle / ".state" / "history.ndjson").read_bytes() == original_history

    override_rc, overridden, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
            "--interaction-mode",
            "confirm",
            "--publish-mode",
            "upload",
        ],
        cwd=tmp_path,
        environ={
            "PDF2MARKDOWN_INTERACTION_MODE": "auto",
            "PDF2MARKDOWN_PUBLISH_MODE": "skip",
            "UNRELATED_SECRET": secret,
        },
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history_bytes = (bundle / ".state" / "history.ndjson").read_bytes()
    history = [json.loads(line) for line in history_bytes.splitlines()]
    assert override_rc == 0
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["generation"] == 2
    assert overridden["publication_state"] == "blocked"
    assert manifest["generation"] == private_state["generation"] == 2
    assert manifest["settings_snapshot"]["interaction_mode"] == "confirm"
    assert manifest["settings_snapshot"]["publishing"]["mode"] == "upload"
    assert manifest["settings_snapshot"]["sources"] == {
        "interaction_mode": "resume_command_line",
        "publishing.mode": "resume_command_line",
    }
    assert manifest["settings_snapshot"]["invocation_cwd"] == original_manifest[
        "settings_snapshot"
    ]["invocation_cwd"]
    assert manifest["settings_snapshot"]["settings_file"] == original_manifest[
        "settings_snapshot"
    ]["settings_file"]
    assert history_bytes.startswith(original_history)
    assert history[0]["settings_snapshot"] == original_manifest["settings_snapshot"]
    assert [event["event"] for event in history[-3:]] == [
        "settings_override_intent",
        "settings_override_prepared",
        "settings_override_committed",
    ]
    assert history[-1]["generation"] == 2
    assert history[-1]["settings_snapshot"] == manifest["settings_snapshot"]
    assert secret not in history_bytes.decode("utf-8")

    before_invalid = state_snapshot(bundle)
    invalid_rc, invalid, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "2",
            "--publish-with",
            "../../unsafe",
        ],
        cwd=tmp_path,
    )
    assert invalid_rc == 6
    assert invalid["errors"][0]["code"] == "configuration_invalid"
    assert state_snapshot(bundle) == before_invalid


def test_resume_recovers_each_settings_override_commit_crash_point(
    tmp_path, capsys, monkeypatch
):
    class SimulatedProcessCrash(BaseException):
        pass

    for crash_point in (
        "after_intent",
        "before_private",
        "before_manifest",
        "after_manifest",
    ):
        project = tmp_path / crash_point
        project.mkdir()
        source = project / "source.pdf"
        source.write_bytes(PDF_BYTES)
        _rc, started, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=project,
        )
        bundle = Path(started["work_bundle"])
        original_replace = workflow.os.replace
        original_fsync = workflow.os.fsync
        directory_fsync_crashed = False

        def crash_replace(source_name, destination_name, *args, **kwargs):
            destination = os.fspath(destination_name)
            should_crash = (
                crash_point == "before_private" and destination == "private.json"
            ) or (
                crash_point in {"before_manifest", "after_manifest"}
                and destination == "manifest.json"
            )
            if not should_crash:
                return original_replace(source_name, destination_name, *args, **kwargs)
            if crash_point == "after_manifest":
                original_replace(source_name, destination_name, *args, **kwargs)
            raise SimulatedProcessCrash()

        def crash_after_history_directory_fsync(descriptor):
            nonlocal directory_fsync_crashed
            result = original_fsync(descriptor)
            if (
                not directory_fsync_crashed
                and stat.S_ISDIR(os.fstat(descriptor).st_mode)
            ):
                directory_fsync_crashed = True
                raise SimulatedProcessCrash()
            return result

        resume_argv = [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
            "--interaction-mode",
            "auto",
        ]
        with monkeypatch.context() as patch_context:
            if crash_point == "after_intent":
                patch_context.setattr(
                    workflow.os, "fsync", crash_after_history_directory_fsync
                )
            else:
                patch_context.setattr(workflow.os, "replace", crash_replace)
            with pytest.raises(SimulatedProcessCrash):
                workflow.main(
                    resume_argv,
                    environ={},
                    cwd=str(project),
                    config_home=str(project / "config-home"),
                    transport=no_network,
                    now=NOW,
                )
        capsys.readouterr()

        recovered_rc, recovered, _stderr = invoke(
            capsys,
            resume_argv,
            cwd=project,
        )
        manifest = json.loads((bundle / "manifest.json").read_text())
        private_state = json.loads((bundle / ".state" / "private.json").read_text())
        history = [
            json.loads(line)
            for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
        ]

        assert recovered_rc == 0
        assert recovered["outcome"] == "settings_overridden"
        assert recovered["generation"] == 2
        assert manifest["generation"] == private_state["generation"] == 2
        assert manifest["settings_snapshot"]["interaction_mode"] == "auto"
        assert [event["event"] for event in history].count(
            "settings_override_intent"
        ) == 1
        assert [event["event"] for event in history].count(
            "settings_override_prepared"
        ) == 1
        assert [event["event"] for event in history].count(
            "settings_override_committed"
        ) == 1


def state_snapshot(bundle):
    snapshot = {}
    for path in sorted(bundle.rglob("*")):
        relative = str(path.relative_to(bundle))
        info = path.lstat()
        snapshot[relative] = {
            "mode": stat.S_IMODE(info.st_mode),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "content": path.read_bytes() if path.is_file() else None,
        }
    return snapshot


def test_start_preserves_local_pdf_bytes_in_a_versioned_work_bundle(tmp_path, capsys):
    source = tmp_path / "Report 2024.pdf"
    source.write_bytes(PDF_BYTES)
    output_root = tmp_path / "bundles"

    rc, result, stderr = invoke(
        capsys,
        ["start", "--source", str(source), "--output-dir", str(output_root)],
        cwd=tmp_path,
    )

    bundle = Path(result["work_bundle"])
    assert rc == 0
    assert result == {
        "schema_version": 1,
        "work_bundle": str(bundle),
        "generation": 1,
        "conversion_state": "preparing",
        "publication_state": "not_requested",
        "outcome": "created",
        "action_required": None,
        "action_id": None,
        "evidence_hash": f"sha256:{PDF_SHA256}",
        "artifacts": {
            "manifest": "manifest.json",
            "source_pdf": "01-source/source.pdf",
        },
        "errors": [],
    }
    assert bundle.name == f"20240102-030405-report-2024-{PDF_SHA256[:8]}"
    assert (bundle / "01-source" / "source.pdf").read_bytes() == PDF_BYTES
    assert "created" in stderr


def test_start_persists_a_private_recoverable_initial_state(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    secret = "must-not-be-persisted"
    previous_umask = os.umask(0)
    try:
        rc, result, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=tmp_path,
            environ={"AIHUB_API_KEY": secret, "UNRELATED_SECRET": secret},
        )
    finally:
        os.umask(previous_umask)

    bundle = Path(result["work_bundle"])
    directories = [
        bundle,
        bundle / ".state",
        bundle / "01-source",
        bundle / "02-pages",
        bundle / "03-converted",
        bundle / "03-converted" / "attempts",
        bundle / "04-review",
        bundle / "05-published",
    ]
    files = [
        bundle / "manifest.json",
        bundle / ".state" / "lock",
        bundle / ".state" / "private.json",
        bundle / ".state" / "history.ndjson",
        bundle / "01-source" / "source.pdf",
    ]

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history = [json.loads(line) for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()]
    persisted_text = "\n".join(path.read_text(errors="replace") for path in files)

    assert rc == 0
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert manifest["source"] == {
        "original_name": "source.pdf",
        "origin": {"kind": "local", "path": str(source)},
        "physical_path": "01-source/source.pdf",
        "sha256": PDF_SHA256,
        "size_bytes": len(PDF_BYTES),
    }
    assert manifest["settings_snapshot"] == {
        "schema_version": 1,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
        "sources": {
            "interaction_mode": "built_in_default",
            "publishing.mode": "built_in_default",
        },
        "invocation_cwd": {
            "path": str(tmp_path.resolve()),
            "device": tmp_path.stat().st_dev,
            "inode": tmp_path.stat().st_ino,
        },
        "settings_file": {
            "path": str(tmp_path / "config-home" / "settings.json"),
            "content_hash": None,
        },
    }
    assert private_state == {
        "schema_version": 1,
        "generation": 1,
        "source_uploads": [],
        "result_urls": [],
    }
    assert history == [
        {
            "schema_version": 1,
            "event": "bundle_started",
                "generation": 1,
                "at": "2024-01-02T03:04:05Z",
                "source_sha256": PDF_SHA256,
                "settings_snapshot": manifest["settings_snapshot"],
            }
        ]
    assert manifest["conversion_state"] == "preparing"
    assert manifest["publication_state"] == "not_requested"
    assert secret not in persisted_text


def test_start_rejects_invalid_unreadable_and_unsafe_sources_as_json(tmp_path, capsys):
    invalid = tmp_path / "not-a-pdf.bin"
    invalid.write_bytes(b"plain text")
    regular = tmp_path / "regular.pdf"
    regular.write_bytes(PDF_BYTES)
    symlink = tmp_path / "linked.pdf"
    symlink.symlink_to(regular)
    output_root = tmp_path / "bundles"

    cases = [
        (invalid, "invalid_pdf"),
        (tmp_path / "missing.pdf", "source_unreadable"),
        (symlink, "unsafe_source_type"),
    ]
    for source, error_code in cases:
        rc, result, stderr = invoke(
            capsys,
            ["start", "--source", str(source), "--output-dir", str(output_root)],
            cwd=tmp_path,
        )
        assert rc == 3
        assert result == {
            "schema_version": 1,
            "work_bundle": None,
            "generation": None,
            "conversion_state": None,
            "publication_state": None,
            "outcome": "error",
            "action_required": "provide_valid_local_pdf",
            "action_id": None,
            "evidence_hash": None,
            "artifacts": {},
            "errors": [{"code": error_code, "message": result["errors"][0]["message"]}],
        }
        assert error_code in stderr

    assert not list(output_root.glob(".pdf2markdown-*"))
    assert not [path for path in output_root.iterdir() if path.is_dir()]


def test_inspect_returns_current_state_without_writing_the_work_bundle(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    before = state_snapshot(bundle)

    rc, result, stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
    )

    assert rc == 0
    assert result == {**started, "outcome": "inspected"}
    assert "inspected" in stderr
    assert state_snapshot(bundle) == before


def test_resume_returns_the_same_state_without_rebuilding_the_bundle(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    before = state_snapshot(bundle)

    rc, result, stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(started["generation"]),
        ],
        cwd=tmp_path,
    )

    assert rc == 0
    assert result == {**started, "outcome": "no_progress"}
    assert "no_progress" in stderr
    assert state_snapshot(bundle) == before


def test_resume_rejects_stale_generation_and_a_concurrent_writer(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    before = state_snapshot(bundle)

    stale_rc, stale, _stderr = invoke(
        capsys,
        ["resume", "--work-bundle", str(bundle), "--expected-generation", "0"],
        cwd=tmp_path,
    )
    assert stale_rc == 5
    assert stale["work_bundle"] == str(bundle)
    assert stale["generation"] == 1
    assert stale["conversion_state"] == "preparing"
    assert stale["publication_state"] == "not_requested"
    assert stale["errors"][0]["code"] == "generation_conflict"
    assert stale["action_required"] == "inspect_current_generation"

    lock_descriptor = os.open(bundle / ".state" / "lock", os.O_RDWR)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked_rc, locked, _stderr = invoke(
            capsys,
            ["resume", "--work-bundle", str(bundle), "--expected-generation", "1"],
            cwd=tmp_path,
        )
        inspect_rc, inspected, _stderr = invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
        )
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    assert locked_rc == 5
    assert locked["work_bundle"] == str(bundle)
    assert locked["errors"][0]["code"] == "bundle_locked"
    assert locked["action_required"] == "retry_after_writer_finishes"
    assert inspect_rc == 0
    assert inspected["generation"] == 1
    assert state_snapshot(bundle) == before


def test_inspect_reports_integrity_and_schema_failures_without_completion(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    (bundle / "01-source" / "source.pdf").write_bytes(b"%PDF-tampered")

    integrity_rc, integrity, stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
    )

    assert integrity_rc == 4
    assert integrity["work_bundle"] == str(bundle)
    assert integrity["generation"] == 1
    assert integrity["conversion_state"] == "preparing"
    assert integrity["publication_state"] == "not_requested"
    assert integrity["outcome"] == "error"
    assert integrity["errors"][0]["code"] == "integrity_violation"
    assert integrity["conversion_state"] != "local_complete"
    assert "integrity_violation" in stderr

    second_source = tmp_path / "second.pdf"
    second_source.write_bytes(PDF_BYTES)
    _rc, second_started, _stderr = invoke(
        capsys,
        ["start", "--source", str(second_source)],
        cwd=tmp_path,
    )
    second_bundle = Path(second_started["work_bundle"])
    manifest_path = second_bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest))

    schema_rc, schema_error, stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(second_bundle)],
        cwd=tmp_path,
    )

    assert schema_rc == 4
    assert schema_error["work_bundle"] == str(second_bundle)
    assert schema_error["outcome"] == "error"
    assert schema_error["errors"][0]["code"] == "invalid_bundle"
    assert schema_error["conversion_state"] != "local_complete"
    assert "invalid_bundle" in stderr


def test_inspect_rejects_states_not_created_by_ticket_one(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)

    for field, fabricated_state in (
        ("conversion_state", "local_complete"),
        ("publication_state", "published"),
    ):
        _rc, started, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=tmp_path,
        )
        bundle = Path(started["work_bundle"])
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest[field] = fabricated_state
        manifest_path.write_text(json.dumps(manifest))

        rc, result, stderr = invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
        )

        assert rc == 4
        assert result["outcome"] == "error"
        assert result["errors"][0]["code"] == "invalid_bundle"
        assert result["conversion_state"] != "local_complete"
        assert result["publication_state"] != "published"
        assert "invalid_bundle" in stderr


def test_inspect_rejects_impossible_ticket_one_authoritative_state(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)

    for scenario in (
        "generation",
        "history",
        "private",
        "settings",
        "manifest_unknown",
        "source_unknown",
        "origin_unknown",
        "private_unknown",
        "settings_schema_type",
        "history_time",
    ):
        _rc, started, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=tmp_path,
        )
        bundle = Path(started["work_bundle"])
        manifest_path = bundle / "manifest.json"
        private_path = bundle / ".state" / "private.json"
        history_path = bundle / ".state" / "history.ndjson"

        if scenario == "generation":
            manifest = json.loads(manifest_path.read_text())
            manifest["generation"] = 7
            manifest_path.write_text(json.dumps(manifest))
            private_state = json.loads(private_path.read_text())
            private_state["generation"] = 7
            private_path.write_text(json.dumps(private_state))
        elif scenario == "history":
            with history_path.open("a") as history:
                history.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "event": "fabricated",
                            "generation": 999,
                        }
                    )
                    + "\n"
                )
        elif scenario == "private":
            private_state = json.loads(private_path.read_text())
            private_state["source_uploads"] = [{"state": "fabricated"}]
            private_path.write_text(json.dumps(private_state))
        elif scenario == "settings":
            manifest = json.loads(manifest_path.read_text())
            del manifest["settings_snapshot"]
            manifest_path.write_text(json.dumps(manifest))
        elif scenario == "manifest_unknown":
            manifest = json.loads(manifest_path.read_text())
            manifest["unexpected"] = "value"
            manifest_path.write_text(json.dumps(manifest))
        elif scenario == "source_unknown":
            manifest = json.loads(manifest_path.read_text())
            manifest["source"]["unexpected"] = "value"
            manifest_path.write_text(json.dumps(manifest))
        elif scenario == "origin_unknown":
            manifest = json.loads(manifest_path.read_text())
            manifest["source"]["origin"]["unexpected"] = "value"
            manifest_path.write_text(json.dumps(manifest))
        elif scenario == "private_unknown":
            private_state = json.loads(private_path.read_text())
            private_state["unexpected"] = "value"
            private_path.write_text(json.dumps(private_state))
        elif scenario == "settings_schema_type":
            manifest = json.loads(manifest_path.read_text())
            manifest["settings_snapshot"]["schema_version"] = True
            manifest_path.write_text(json.dumps(manifest))
        else:
            history_event = json.loads(history_path.read_text())
            history_event["at"] = "not-a-timestamp"
            history_path.write_text(json.dumps(history_event) + "\n")

        rc, result, stderr = invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
        )

        assert rc == 4
        assert result["outcome"] == "error"
        assert result["errors"][0]["code"] == "invalid_bundle"
        assert "invalid_bundle" in stderr


def test_inspect_rejects_duplicate_keys_in_every_authoritative_json_stream(
    tmp_path, capsys
):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)

    for location in ("manifest", "private", "history"):
        _rc, started, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=tmp_path,
        )
        bundle = Path(started["work_bundle"])
        if location == "manifest":
            path = bundle / "manifest.json"
            raw = path.read_text().replace(
                '"interaction_mode":"confirm"',
                '"interaction_mode":"auto","interaction_mode":"confirm"',
                1,
            )
        elif location == "private":
            path = bundle / ".state" / "private.json"
            raw = path.read_text().replace(
                '"generation":1', '"generation":2,"generation":1', 1
            )
        else:
            path = bundle / ".state" / "history.ndjson"
            raw = path.read_text().replace(
                '"interaction_mode":"confirm"',
                '"interaction_mode":"auto","interaction_mode":"confirm"',
                1,
            )
        path.write_text(raw, encoding="utf-8")

        rc, result, stderr = invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
        )

        assert rc == 4
        assert result["outcome"] == "error"
        assert result["errors"][0]["code"] == "invalid_bundle"
        assert "invalid_bundle" in stderr


def test_start_uses_output_priority_and_never_overwrites_a_name_collision(tmp_path, capsys):
    source = tmp_path / "Source.pdf"
    source.write_bytes(PDF_BYTES)
    environment_root = tmp_path / "environment-bundles"
    environment_root.mkdir()
    occupied_name = f"20240102-030405-source-{PDF_SHA256[:8]}"
    occupied = environment_root / occupied_name
    occupied.symlink_to(environment_root / "missing-target")

    env_rc, from_environment, _stderr = invoke(
        capsys,
        ["start", "--source", str(source), "--output-dir", ""],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_OUTPUT_DIR": str(environment_root)},
    )
    cli_rc, from_cli, _stderr = invoke(
        capsys,
        ["start", "--source", str(source), "--output-dir", "cli-bundles"],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_OUTPUT_DIR": str(environment_root)},
    )
    default_rc, from_default, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ={"PDF2MARKDOWN_OUTPUT_DIR": ""},
    )

    assert env_rc == cli_rc == default_rc == 0
    assert Path(from_environment["work_bundle"]) == environment_root / f"{occupied_name}-2"
    assert occupied.is_symlink()
    assert Path(from_cli["work_bundle"]).parent == tmp_path / "cli-bundles"
    assert Path(from_default["work_bundle"]).parent == tmp_path / "pdf2markdown-output"


def test_start_bounds_the_slug_for_a_maximum_length_source_name(tmp_path, capsys):
    source = tmp_path / f"{'a' * 240}.pdf"
    source.write_bytes(PDF_BYTES)

    rc, result, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )

    bundle = Path(result["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert rc == 0
    assert len(bundle.name.encode()) <= os.pathconf(bundle.parent, "PC_NAME_MAX")
    assert bundle.name.endswith(f"-{PDF_SHA256[:8]}")
    assert manifest["source"]["original_name"] == source.name


def test_inspect_canonicalizes_a_symlinked_bundle_ancestor(tmp_path, capsys):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    real_root = tmp_path / "real-root"
    _rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source), "--output-dir", str(real_root)],
        cwd=tmp_path,
    )
    bundle = Path(started["work_bundle"])
    alias_root = tmp_path / "alias-root"
    alias_root.symlink_to(real_root, target_is_directory=True)

    rc, result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(alias_root / bundle.name)],
        cwd=tmp_path,
    )

    assert rc == 0
    assert Path(result["work_bundle"]) == bundle

    dotdot_rc, dotdot_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle / "02-pages" / "..")],
        cwd=tmp_path,
    )
    assert dotdot_rc == 0
    assert Path(dotdot_result["work_bundle"]) == bundle


def test_inspect_preserves_posix_symlink_dotdot_resolution(tmp_path, capsys):
    left = tmp_path / "left"
    right = tmp_path / "right"
    child = right / "child"
    left.mkdir()
    child.mkdir(parents=True)
    first_source = tmp_path / "first.pdf"
    first_source.write_bytes(PDF_BYTES)
    second_source = tmp_path / "second.pdf"
    second_source.write_bytes(
        PDF_BYTES.replace(b"%%EOF", b"% right-side bundle\n%%EOF")
    )
    _rc, first, _stderr = invoke(
        capsys,
        ["start", "--source", str(first_source), "--output-dir", str(left)],
        cwd=tmp_path,
    )
    _rc, second, _stderr = invoke(
        capsys,
        ["start", "--source", str(second_source), "--output-dir", str(right)],
        cwd=tmp_path,
    )
    left_bundle = left / "bundle"
    right_bundle = right / "bundle"
    Path(first["work_bundle"]).rename(left_bundle)
    Path(second["work_bundle"]).rename(right_bundle)
    (left / "jump").symlink_to(child, target_is_directory=True)

    rc, result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(left / "jump" / ".." / "bundle")],
        cwd=tmp_path,
    )

    assert rc == 0
    assert Path(result["work_bundle"]) == right_bundle
    assert result["evidence_hash"] == second["evidence_hash"]


def test_resume_rejects_a_bundle_replaced_after_its_lock_is_acquired(
    tmp_path, capsys, monkeypatch
):
    first_source = tmp_path / "first.pdf"
    first_source.write_bytes(PDF_BYTES)
    second_source = tmp_path / "second.pdf"
    second_source.write_bytes(
        PDF_BYTES.replace(b"%%EOF", b"% replacement bundle\n%%EOF")
    )
    output_root = tmp_path / "bundles"
    _rc, first, _stderr = invoke(
        capsys,
        ["start", "--source", str(first_source), "--output-dir", str(output_root)],
        cwd=tmp_path,
    )
    _rc, second, _stderr = invoke(
        capsys,
        ["start", "--source", str(second_source), "--output-dir", str(output_root)],
        cwd=tmp_path,
    )
    bundle = Path(first["work_bundle"])
    replacement = Path(second["work_bundle"])
    moved_bundle = output_root / "moved-original"
    original_flock = workflow.fcntl.flock
    replacement_lock = None
    swapped = False

    def lock_then_replace(descriptor, operation):
        nonlocal replacement_lock, swapped
        result = original_flock(descriptor, operation)
        if not swapped and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            swapped = True
            bundle.rename(moved_bundle)
            replacement.rename(bundle)
            replacement_lock = os.open(bundle / ".state" / "lock", os.O_RDWR)
            original_flock(replacement_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return result

    monkeypatch.setattr(workflow.fcntl, "flock", lock_then_replace)
    try:
        rc, result, stderr = invoke(
            capsys,
            ["resume", "--work-bundle", str(bundle), "--expected-generation", "1"],
            cwd=tmp_path,
        )
    finally:
        if replacement_lock is not None:
            original_flock(replacement_lock, fcntl.LOCK_UN)
            os.close(replacement_lock)

    assert swapped
    assert rc == 4
    assert result["outcome"] == "error"
    assert result["errors"][0]["code"] == "invalid_bundle"
    assert "invalid_bundle" in stderr


def test_resume_reads_from_the_locked_bundle_during_an_aba_path_swap(
    tmp_path, capsys, monkeypatch
):
    first_source = tmp_path / "first.pdf"
    first_source.write_bytes(PDF_BYTES)
    second_source = tmp_path / "second.pdf"
    second_source.write_bytes(
        PDF_BYTES.replace(b"%%EOF", b"% temporary replacement\n%%EOF")
    )
    output_root = tmp_path / "bundles"
    _rc, first, _stderr = invoke(
        capsys,
        ["start", "--source", str(first_source), "--output-dir", str(output_root)],
        cwd=tmp_path,
    )
    _rc, second, _stderr = invoke(
        capsys,
        ["start", "--source", str(second_source), "--output-dir", str(output_root)],
        cwd=tmp_path,
    )
    bundle = Path(first["work_bundle"])
    replacement = Path(second["work_bundle"])
    moved_bundle = output_root / "moved-original"
    original_read_json = workflow._read_json
    original_source_digest = workflow._source_digest
    original_flock = workflow.fcntl.flock
    replacement_lock = None
    swapped = False

    def read_json_after_swap(*args, **kwargs):
        nonlocal replacement_lock, swapped
        if not swapped:
            swapped = True
            bundle.rename(moved_bundle)
            replacement.rename(bundle)
            replacement_lock = os.open(bundle / ".state" / "lock", os.O_RDWR)
            original_flock(replacement_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_read_json(*args, **kwargs)

    def digest_then_restore(*args, **kwargs):
        nonlocal replacement_lock
        try:
            return original_source_digest(*args, **kwargs)
        finally:
            bundle.rename(replacement)
            moved_bundle.rename(bundle)
            original_flock(replacement_lock, fcntl.LOCK_UN)
            os.close(replacement_lock)
            replacement_lock = None

    monkeypatch.setattr(workflow, "_read_json", read_json_after_swap)
    monkeypatch.setattr(workflow, "_source_digest", digest_then_restore)
    try:
        rc, result, _stderr = invoke(
            capsys,
            ["resume", "--work-bundle", str(bundle), "--expected-generation", "1"],
            cwd=tmp_path,
        )
    finally:
        if replacement_lock is not None:
            original_flock(replacement_lock, fcntl.LOCK_UN)
            os.close(replacement_lock)

    assert swapped
    assert rc == 0
    assert result["outcome"] == "no_progress"
    assert result["evidence_hash"] == first["evidence_hash"]
    assert result["evidence_hash"] != second["evidence_hash"]


def test_inspect_rejects_unsafe_bundle_paths_and_authoritative_state(tmp_path, capsys):
    def new_bundle(name):
        source = tmp_path / f"{name}.pdf"
        source.write_bytes(PDF_BYTES)
        _rc, started, _stderr = invoke(
            capsys,
            ["start", "--source", str(source)],
            cwd=tmp_path,
        )
        return Path(started["work_bundle"])

    aliased_bundle = new_bundle("aliased")
    alias = tmp_path / "bundle-alias"
    alias.symlink_to(aliased_bundle, target_is_directory=True)
    alias_rc, alias_error, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(alias)],
        cwd=tmp_path,
    )

    linked_manifest_bundle = new_bundle("linked-manifest")
    manifest_path = linked_manifest_bundle / "manifest.json"
    external_manifest = tmp_path / "external-manifest.json"
    manifest_path.replace(external_manifest)
    manifest_path.symlink_to(external_manifest)
    manifest_rc, manifest_error, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(linked_manifest_bundle)],
        cwd=tmp_path,
    )

    private_schema_bundle = new_bundle("private-schema")
    private_path = private_schema_bundle / ".state" / "private.json"
    private_state = json.loads(private_path.read_text())
    private_state["schema_version"] = 99
    private_path.write_text(json.dumps(private_state))
    private_rc, private_error, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(private_schema_bundle)],
        cwd=tmp_path,
    )

    malformed_bundle = new_bundle("malformed-generation")
    malformed_manifest_path = malformed_bundle / "manifest.json"
    malformed_manifest = json.loads(malformed_manifest_path.read_text())
    malformed_manifest["generation"] = "1"
    malformed_manifest_path.write_text(json.dumps(malformed_manifest))
    malformed_private_path = malformed_bundle / ".state" / "private.json"
    malformed_private = json.loads(malformed_private_path.read_text())
    malformed_private["generation"] = "1"
    malformed_private_path.write_text(json.dumps(malformed_private))
    malformed_rc, malformed_error, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(malformed_bundle)],
        cwd=tmp_path,
    )

    assert alias_rc == manifest_rc == private_rc == malformed_rc == 4
    assert alias_error["errors"][0]["code"] == "invalid_bundle"
    assert manifest_error["errors"][0]["code"] == "invalid_bundle"
    assert private_error["errors"][0]["code"] == "invalid_bundle"
    assert malformed_error["errors"][0]["code"] == "invalid_bundle"


def test_invalid_command_arguments_still_return_one_json_object(tmp_path, capsys):
    cases = [
        [],
        ["--help"],
        ["unknown-command"],
        ["start"],
        ["start", "--help"],
        ["resume", "--work-bundle", "missing", "--expected-generation", "not-an-int"],
        ["settings", "set-publish-mode", "publish"],
    ]

    for argv in cases:
        rc, result, stderr = invoke(capsys, argv, cwd=tmp_path)
        assert rc == 2
        assert result["schema_version"] == 1
        assert result["outcome"] == "error"
        assert result["action_required"] == "correct_command_arguments"
        assert result["errors"] == [
            {"code": "invalid_arguments", "message": "Command arguments are invalid."}
        ]
        assert "invalid_arguments" in stderr
        assert "usage:" not in stderr


def test_start_copies_and_hashes_the_same_open_source_descriptor(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF_BYTES)
    replacement = tmp_path / "replacement.pdf"
    replacement_bytes = b"%PDF-1.4\nreplacement bytes\n%%EOF\n"
    replacement.write_bytes(replacement_bytes)
    opened_source = tmp_path / "opened-source.pdf"
    original_open = workflow.os.open
    swapped = False

    def open_then_swap(path, flags, *args):
        nonlocal swapped
        descriptor = original_open(path, flags, *args)
        if not swapped and os.fspath(path) == str(source):
            source.replace(opened_source)
            replacement.replace(source)
            swapped = True
        return descriptor

    monkeypatch.setattr(workflow.os, "open", open_then_swap)
    rc, result, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
    )

    bundle = Path(result["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert rc == 0
    assert source.read_bytes() == replacement_bytes
    assert (bundle / "01-source" / "source.pdf").read_bytes() == PDF_BYTES
    assert result["evidence_hash"] == f"sha256:{PDF_SHA256}"
    assert manifest["source"]["sha256"] == PDF_SHA256


def test_atomic_write_json_and_append_history_use_shared_canonical_encoder(tmp_path):
    # workflow._atomic_write_json/_append_history (workflow.py:252-253,
    # 282-283) are a second writer for manifest.json/private.json/
    # history.ndjson (used at bundle-creation time, workflow.py:536/538/548):
    # they used to inline their own copy of
    # `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"`
    # rather than delegating to bundle.canonical_json_bytes -- the encoder
    # bundle.atomic_write_json/append_history actually use for every other
    # write path. This pins that both writers now produce byte-for-byte
    # identical output to bundle.canonical_json_bytes, including for a
    # payload with quotes, a backslash and non-ASCII content (where
    # ensure_ascii's value changes the byte count the most), proving the
    # switch to the shared encoder did not change what lands on disk.
    value = {
        "schema_version": 1,
        "note": '文件名包含中文与非 ASCII 字符 café "quoted" and a backslash \\',
        "nested": {"z": 1, "a": [1, 2, 3], "empty": {}},
    }
    expected_bytes = workflow.bundle_module.canonical_json_bytes(value)

    manifest_path = tmp_path / "manifest.json"
    workflow._atomic_write_json(manifest_path, value)
    assert manifest_path.read_bytes() == expected_bytes

    history_path = tmp_path / "history.ndjson"
    workflow._append_history(history_path, value)
    assert history_path.read_bytes() == expected_bytes

    second_event = {"schema_version": 1, "event": "second"}
    workflow._append_history(history_path, second_event)
    assert history_path.read_bytes() == expected_bytes + (
        workflow.bundle_module.canonical_json_bytes(second_event)
    )


def test_error_path_actions_are_a_closed_vocabulary():
    import workflow

    assert workflow.ERROR_PATH_ACTIONS == {
        "configure_aihub_api_key",
        "correct_command_arguments",
        "correct_correction_record",
        "correct_settings_override",
        "inspect_current_generation",
        "inspect_preflight_failure",
        "inspect_runtime_error",
        "preserve_work_bundle_and_stop",
        "provide_public_https_pdf",
        "provide_valid_local_pdf",
        "repair_or_restore_work_bundle",
        "repair_settings",
        "restore_preflight_dependencies",
        "resume_same_conversion_result",
        "correct_preflight_record",
        "correct_review_record",
        "restore_review_dependencies",
        "retry_after_writer_finishes",
        "retry_settings_write",
    }


def test_workflow_error_rejects_an_action_outside_the_error_path_vocabulary():
    import workflow

    try:
        workflow.WorkflowError(
            "invalid_bundle",
            "message",
            return_code=4,
            action_required="adopt_conversion_result",
        )
    except ValueError as exc:
        assert "adopt_conversion_result" in str(exc)
    else:
        raise AssertionError(
            "WorkflowError accepted a conversion-vocabulary action"
        )


def test_every_action_required_literal_in_workflow_is_in_the_error_vocabulary():
    import ast
    from pathlib import Path
    import workflow

    # 采集口径必须与运行时真正强制的面一致：WorkflowError.__init__ 只校验
    # 构造参数，因此这里只收 WorkflowError(...) 调用点的 action_required。
    # 必须用 AST 而非正则——action_required 有链式三元写法，值藏在 else
    # 兜底分支里（correct_preflight_record / correct_review_record /
    # restore_review_dependencies 三个值即如此），任何正则都抓不到。
    source = Path(workflow.__file__).read_text(encoding="utf-8")

    def _leaves(node):
        """逐分支产出 (是否静态字符串, 值)，穿透三元表达式的全部分支。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield True, node.value
        elif isinstance(node, ast.Constant) and node.value is None:
            # action_required=None 是合同明确允许的取值（见
            # test_workflow_error_accepts_a_null_action_required）。它既不属于
            # 词汇表、也不是动态传参，必须跳过——否则后续任务在某个构造点传
            # None 就会撞上 assert not dynamic，而 Global Constraints 禁止 4.3
            # 之外的任务改写既有断言。
            return
        elif isinstance(node, ast.IfExp):
            yield from _leaves(node.body)
            yield from _leaves(node.orelse)
        else:
            yield False, ast.dump(node)

    def _code_of(call):
        """构造点的 code（第一个位置参数）的静态值。

        取不到静态值时返回该节点的 AST 转储，让下面的等值断言变红，而不是把
        这个构造点静默漏掉——漏掉正好是本检查要堵的洞。
        """
        if not call.args:
            return "<no positional code>"
        node = call.args[0]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        # 唯一允许的间接写法：全大写的模块级字符串常量名。窄口那两条构造点读的
        # 都是 workflow.PENDING_BOUNDARY_HISTORY_CHECK_ERROR_CODE（任务 3.1d
        # 起是两条：history 检查和 private-state 检查，分别对应 pending 边界的
        # 三个崩溃窗口里的不同窗口），窄口表本身也直接用这个符号
        # 构造（三条审查遗留项之后不再靠一次单独的 import 时自校验比对
        # 两者，一致性由「同一个符号」这件事本身保证）。任务 3.1d 审查遗留项
        # 2：把这条间接写法收窄到全大写白名单，而不是任意 Name 都拿
        # getattr(workflow, node.id, None) 去解析——workflow.py 里以局部变量
        # （如 code）作第一位参数的构造点（当前 :1643、:1878、:2908、
        # :3067、:3511）不受任何全局约束保证「不会撞上某个模块级同名字符串
        # 常量」；若将来真出现一个模块级 `code = "..."`，未收窄的分支会把这
        # 些局部变量静默错解成那个常量的值，而不是照旧落到下面的 dump 分支
        # ——静默漏检正是这条测试要堵的洞。局部变量名不是全大写，照旧落到
        # dump 分支。
        if isinstance(node, ast.Name) and node.id.isupper():
            resolved = getattr(workflow, node.id, None)
            if isinstance(resolved, str):
                return resolved
        return ast.dump(node)

    # 任务 3.1b 修复轮 1（审查 M2）：窄口是按 (code, action) 成对匹配的，而上面
    # 的采集只看 action 一侧。于是把 resume_pending_conversion_operation 写到
    # 另一个 code 下（窄口并不放行的组合）时本测试照旧全绿，只有那条分支真被
    # 执行时才在运行时 ValueError——fail-loud 但 fail-late。这里把 code 一侧
    # 一并钉死：凡 action 落在 ERROR_PATH_ACTIONS 之外的构造点，其
    # (code, action) 必须恰好是窄口放行的那一组，不多不少。
    constructed, dynamic, outside_table = set(), set(), set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WorkflowError"
        ):
            for keyword in node.keywords:
                if keyword.arg == "action_required":
                    for is_static, value in _leaves(keyword.value):
                        (constructed if is_static else dynamic).add(value)
                        if is_static and value not in workflow.ERROR_PATH_ACTIONS:
                            outside_table.add((_code_of(node), value))

    # 动态传参必须显式暴露而不是被静默吞掉：静默返回空正是「表看起来闭合、
    # 运行时却在未覆盖分支抛 ValueError」的成因。
    assert not dynamic, f"dynamic action_required at a WorkflowError call site: {dynamic}"

    # ERROR_PATH_ACTIONS 中唯一不经 WorkflowError 构造、而是直接序列化输出的
    # 成员（workflow.py 里 runtime-error 结果字典的 "action_required" 键）。
    # 这里用显式白名单而不是扫描全部 dict 字面量：任务 2.4 会把 conversion
    # 词汇表的值写进同一个 action_required 键，扫全部 dict 会把它们误收进来，
    # 让本断言在 2.4 变红，而 Global Constraints 又禁止 2.4 改写既有断言。
    SERIALIZED_ONLY = {"inspect_runtime_error"}

    # 白名单必须自我校验：若它对应的产生点被改名或删除，该值就成了表内死值，
    # 而下面的等值断言依旧全绿——正是把 <= 升级成 == 想堵的洞，在这一个成员
    # 上会重新敞开。这里做**定向**扫描（只看 "action_required" 这个 dict 键的
    # 字符串字面量），并用子集方向断言，使任务 2.4 往同一键写入 conversion
    # 值时不会误伤。
    serialized = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "action_required"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    serialized.add(value.value)
    assert SERIALIZED_ONLY <= serialized, (
        f"whitelisted serialized-only action no longer produced: "
        f"{SERIALIZED_ONLY - serialized}"
    )

    # 任务 3.1b 等强翻译：WorkflowError 的闭合校验新增了一个**窄口**——
    # (invalid_bundle, resume_pending_conversion_operation) 这一对，是全计划
    # 唯一一处 WorkflowError 携带 conversion 词汇表 action 的情形，因此
    # workflow.py 里多了一个不属于 ERROR_PATH_ACTIONS 的静态字面量。
    #
    # 翻译方向不放宽：等值断言仍是 ==（不是 <=），只是右边把窄口值单列出来。
    # 于是任何**别的**表外字面量照旧让本断言变红，删掉某个表内值也照旧变红。
    # 白名单本身按 SERIALIZED_ONLY 的同一口径自校验：它必须恰好等于运行时真正
    # 放行的那一组 action，否则窄口和本测试就能各说各话。
    NARROW_GATE_ONLY = {
        action for _code, action in workflow.CONVERSION_ACTION_EXCEPTIONS
    }
    assert NARROW_GATE_ONLY == {"resume_pending_conversion_operation"}
    assert not NARROW_GATE_ONLY & set(workflow.ERROR_PATH_ACTIONS)

    assert constructed | SERIALIZED_ONLY == (
        set(workflow.ERROR_PATH_ACTIONS) | NARROW_GATE_ONLY
    )

    # code 一侧：表外 action 的每个构造点，其 (code, action) 必须正是运行时
    # 放行的那一对。把该 action 挪到别的 code 下、或让第二个 code 也产出它，
    # 都在这里立刻变红，而不是等那条分支被执行。
    assert outside_table == set(workflow.CONVERSION_ACTION_EXCEPTIONS)


def test_workflow_error_accepts_a_null_action_required():
    import workflow

    # 「action_required=None 仍合法」是本任务明写的接口契约，但今天 85 个
    # 构造点没有一处传 None，若无此断言，有人删掉 __init__ 里的 is not None
    # 短路全量回归依然全绿。
    #
    # ⚠️ 只断言「显式传 None 合法」，不要断言「该参数可省略」。
    # action_required 今天是**必传**关键字参数（`action_required: str,`，无默认
    # 值）。写成省略参数的调用会迫使实现者给它加 `= None` 默认值，那是未经
    # 授权的构造器 API 放宽，超出本任务范围（2026-07-26 订正：本测试初稿正是
    # 这么写的，导致修复轮次 1 引入了该默认值）。本任务只改类型标注，不改
    # 参数是否必传。
    explicit = workflow.WorkflowError(
        "invalid_bundle", "message", return_code=4, action_required=None
    )
    assert explicit.action_required is None


# --- Task 3.1b: a pending conversion boundary points at resume -------------
#
# Driving a bundle all the way to an unclosed conversion intent needs the real
# start -> advance -> record preflight -> stage -> create pipeline, which
# tests/unit/test_conversion_attempt.py already builds (`ready_staged_bundle`)
# together with the fake transports the pipeline is fed. Importing it beats
# copying ~120 lines of driver into this file.
#
# Be exact about the precedent (3.1b review, M1): one test module importing
# another is established here -- tests/unit/test_conversion_attempt.py:19 and
# tests/unit/test_review.py:17 both import test_raw_conversion -- but both of
# those are *same-directory* imports that need no sys.path change. Importing
# across test directories, and inserting a path in order to, is new with this
# helper, so it does not inherit those two as cover. pytest's prepend import
# mode only puts `tests/` on sys.path when this file is collected on its own,
# so tests/unit has to be added here for the standalone run to work. What makes
# the new part safe was checked rather than assumed: neither imported module
# runs anything at module level, and no name under tests/unit collides with
# scripts/ or the stdlib, so prepending that directory shadows nothing.
def _unit_test_module(name):
    unit = Path(__file__).parent / "unit"
    if str(unit) not in sys.path:
        sys.path.insert(0, str(unit))
    return __import__(name)


def _conversion_helpers():
    return _unit_test_module("test_conversion_attempt")


def _conversion_intents():
    """The closed set of conversion intent events, read off production.

    Used as a parametrisation domain, so a sixth intent added later widens the
    boundary matrix automatically instead of leaving a silent hole.
    """
    import conversion_attempt

    return conversion_attempt.CONVERSION_INTENTS


def _staging_helpers():
    return _unit_test_module("test_source_staging")


def park_on_conversion_intent(
    tmp_path,
    capsys,
    monkeypatch,
    intent_event,
    *,
    boundary="private",
    previous_private_out=None,
):
    """Park a real bundle on an unclosed `intent_event`, at one of the three
    crash windows that intent can leave behind.

    Every conversion operation writes the same durable four steps: append the
    intent, write private.json, write manifest.json, append the committed
    event. A crash can therefore land in three places, and
    `recover_interrupted_attempt` admits all three:

      * "private"  -- before the private write. Both authoritative files still
        hold the pre-intent state.
      * "manifest" -- after private, before manifest. private is post-intent,
        manifest is pre-intent.
      * "commit"   -- after both writes, before the committed event is
        appended. Both files are post-intent; only the journal is behind.

    Task 3.1b only ever drove the first one, which is why the predicate it
    landed only recognised that one; task 3.1d parameterises the window so the
    other two are driven too.

    Keying the crash on the last appended event name rather than on "the first
    private.json write" is what makes one helper serve all five intents: a
    single command can open two of these transactions in a row (a `resume`
    submits *and* records the submission result), so the first private write is
    the wrong one for the second intent. The "commit" window is keyed the same
    way -- "the next event appended while this intent is the tail" -- so it
    needs no table of committed event names to stay in step with.

    `previous_private_out`, when a list is passed, receives the exact bytes
    private.json holds at the instant the intent's own private write is about
    to replace them -- i.e. `previous_private`, read off disk rather than
    recomputed. The one test that needs it builds the write order's forbidden
    fourth state (`manifest` desired while `private` is previous), which no
    crash can produce, and taking the real bytes keeps that fixture from
    depending on the very prefix reduction the predicate under test performs.

    Returns (bundle, staged, environ).
    """
    helpers = _conversion_helpers()
    import conversion_attempt

    bundle, staged, dependencies, key, _url, _sha256 = helpers.ready_staged_bundle(
        tmp_path, capsys, monkeypatch
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    if intent_event == "conversion_authorize_initial_intent":
        # The credential gate: `advance` on a staged bundle with no API key in
        # the environment authorizes the first attempt before submitting it.
        argv = [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
            "--visual-capability",
            "available",
        ]
        crash_environ, crash_transport = dependencies, helpers.NeverNetwork()
    elif intent_event in {
        "conversion_submit_intent",
        "conversion_submit_result_intent",
    }:
        # One `resume` opens both: begin_attempt, then finish_submission once
        # the create call answers.
        argv = [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
        ]
        crash_environ = environ
        crash_transport = helpers.SuccessfulCreate("task-at-the-boundary")
    elif intent_event == "conversion_poll_result_intent":
        submit_rc, submitted, _stderr = helpers.invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=helpers.SuccessfulCreate("task-at-the-boundary"),
        )
        assert submit_rc == 0, json.dumps(submitted, sort_keys=True)
        argv = [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(submitted["generation"]),
        ]
        crash_environ = environ
        crash_transport = helpers.PollStatus(
            "task-at-the-boundary",
            "completed",
            results=[{"url": "https://results.aihubmax.com/b.zip?token=private"}],
        )
    elif intent_event == "conversion_retry_intent":
        unknown_rc, unknown, _stderr = helpers.invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=helpers.StatusCreate(500),
        )
        assert unknown_rc == 0, json.dumps(unknown, sort_keys=True)
        argv = [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unknown["generation"]),
            "--action-id",
            unknown["action_id"],
            "--evidence-hash",
            unknown["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "I accept the possible duplicate conversion charge.",
        ]
        crash_environ, crash_transport = dependencies, helpers.NeverNetwork()
    else:  # pragma: no cover - a new intent must bring its driver with it
        raise AssertionError(f"no driver parks a bundle on {intent_event!r}")

    original_atomic_write = conversion_attempt.bundle.atomic_write_json
    original_append_history = conversion_attempt.bundle.append_history
    last_appended = None

    def append_history(value, *, state_fd):
        nonlocal last_appended
        if boundary == "commit" and last_appended == intent_event:
            raise helpers.SimulatedProcessCrash
        last_appended = value.get("event")
        return original_append_history(value, state_fd=state_fd)

    def crash_after_the_intent(name, value, *, dir_fd):
        if (
            previous_private_out is not None
            and name == "private.json"
            and last_appended == intent_event
        ):
            # This call IS the intent's private write, so the file still holds
            # the pre-intent state right now.
            previous_private_out.append(
                (bundle / ".state" / "private.json").read_bytes()
            )
        if name == f"{boundary}.json" and last_appended == intent_event:
            raise helpers.SimulatedProcessCrash
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(conversion_attempt.bundle, "append_history", append_history)
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", crash_after_the_intent
    )
    with pytest.raises(helpers.SimulatedProcessCrash):
        workflow.main(
            argv,
            environ=crash_environ,
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=crash_transport,
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    if intent_event == "conversion_submit_intent":
        # The submission itself must not have gone out: begin_attempt's private
        # write is the crash point, and it runs before the create call.
        assert crash_transport.calls == []
    history = read_history_events(bundle)
    assert history[-1]["event"] == intent_event
    return bundle, staged, environ


def drive_to_pending_conversion_intent(tmp_path, capsys, monkeypatch):
    """Park a real bundle on an unclosed `conversion_submit_intent`."""
    return park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_submit_intent"
    )


# What a `resume` needs to answer once it has closed the boundary and carried
# on with the operation the intent opened. Closing itself is always zero
# network -- it is a journal replay -- but the command does not stop there,
# so a case whose next step is a create or a poll has to be given a transport
# for it. NeverNetwork marks the cases whose whole `resume` is offline.
_RESUME_TRANSPORT_BY_INTENT = {
    # The gate re-blocks before any network call: this bundle is parked with
    # no AIHUB_API_KEY in the environment.
    "conversion_authorize_initial_intent": lambda helpers: helpers.NeverNetwork(),
    # Closing the retry authorization leaves an `authorized` placeholder;
    # submitting it needs a credential this environment does not have.
    "conversion_retry_intent": lambda helpers: helpers.NeverNetwork(),
    # The create never went out (begin_attempt writes before it calls), so the
    # resumed command still has to send it.
    "conversion_submit_intent": lambda helpers: helpers.SuccessfulCreate(
        "task-at-the-boundary"
    ),
    # The create already answered; the resumed command polls next.
    "conversion_submit_result_intent": lambda helpers: helpers.PollStatus(
        "task-at-the-boundary", "processing"
    ),
    # The poll already answered with a result; nothing further goes out.
    "conversion_poll_result_intent": lambda helpers: helpers.NeverNetwork(),
}


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
@pytest.mark.parametrize(
    "intent_event", sorted(_conversion_intents()), ids=sorted(_conversion_intents())
)
def test_inspect_and_resume_agree_at_every_pending_conversion_boundary(
    tmp_path, capsys, monkeypatch, intent_event, boundary
):
    """Task 3.1d, the I1 root fix: `inspect`'s verdict must match what
    `resume` can actually do, for EVERY conversion intent at EVERY crash
    window -- fifteen combinations, not the one `conversion_submit_intent` /
    "private" cell task 3.1b covered.

    Before this task the predicate compared the history prefix's reduction to
    disk and nothing else, so it recognised only the pre-private window. The
    other two -- both of which `recover_interrupted_attempt` closes with rc 0
    -- were answered `repair_or_restore_work_bundle`: "go repair or restore
    this work bundle", about a bundle that is not damaged at all. Two thirds
    of the pending boundaries carried that instruction.

    The parametrisation is over `conversion_attempt.CONVERSION_INTENTS`
    itself, so a sixth intent added later fails here (no driver parks it)
    rather than quietly going uncovered.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, intent_event, boundary=boundary
    )

    before = state_snapshot(bundle)
    inspect_transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=inspect_transport,
    )

    # design.md Decision 8.1's contract is unchanged: fail closed, rc 4, zero
    # writes, zero network. Only the action changes.
    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "resume_pending_conversion_operation"
    assert inspect_transport.calls == []
    assert state_snapshot(bundle) == before

    # ...and the advice is true: resume really does close it.
    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=_RESUME_TRANSPORT_BY_INTENT[intent_event](helpers),
    )
    assert resume_rc == 0, json.dumps(resumed, sort_keys=True)
    assert read_history_events(bundle)[-1]["event"] not in _conversion_intents()


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
def test_inspect_names_resume_at_a_raw_bearing_pending_boundary(
    tmp_path, capsys, monkeypatch, boundary
):
    """Task 3.1d, the reachability question 3.1b left open and could not
    construct a driver for: a bundle carrying a raw conversion slice CAN park
    on a conversion intent.

    A retry authorized after a raw layout rejection is exactly that shape --
    tests/unit/test_conversion_attempt.py::
    test_layout_retry_journal_recovers_inside_a_raw_bearing_bundle already
    proves `record` closes all three of its crash windows with rc 0 and zero
    network. `inspect` used to answer `repair_or_restore_work_bundle` for all
    three anyway, and not because the bundle looked damaged: the boundary
    predicate reduced the prefix with conversion_attempt's own reducer, which
    does not know raw conversion events at all, so it returned None for every
    such bundle. The predicate now takes the reducer as an argument and
    `_inspect_open_bundle` hands it the same `_conversion_history_resolver`
    choice `_resume` hands recover_interrupted_attempt.
    """
    helpers = _conversion_helpers()
    import conversion_attempt

    bundle, ready, dependencies, key, _result_url = helpers.ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    layout_rc, layout_error, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=helpers.ArchiveTransport(
            helpers.make_zip([("a.md", b"a"), ("b.md", b"b")])
        ),
    )
    assert layout_rc == 0, json.dumps(layout_error, sort_keys=True)
    argv = [
        "record",
        "conversion",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(layout_error["generation"]),
        "--action-id",
        layout_error["action_id"],
        "--evidence-hash",
        layout_error["evidence_hash"],
        "--decision",
        "retry",
        "--basis",
        "The ambiguous result layout requires a new conversion charge.",
    ]
    original_atomic_write, original_append_history = (
        helpers._install_conversion_journal_crash(
            monkeypatch, event="conversion_retry_committed", boundary=boundary
        )
    )
    with pytest.raises(helpers.SimulatedProcessCrash):
        workflow.main(
            argv,
            environ=dependencies,
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=helpers.CountingNeverNetwork(),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )
    assert read_history_events(bundle)[-1]["event"] == "conversion_retry_intent"
    assert "raw_conversion" in json.loads((bundle / "manifest.json").read_text())

    before = state_snapshot(bundle)
    inspect_transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=inspect_transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "resume_pending_conversion_operation"
    assert inspect_transport.calls == []
    assert state_snapshot(bundle) == before

    close_transport = helpers.CountingNeverNetwork()
    close_rc, closed, _stderr = helpers.invoke(
        capsys, argv, cwd=tmp_path, environ=dependencies, transport=close_transport
    )
    assert close_rc == 0, json.dumps(closed, sort_keys=True)
    assert close_transport.calls == []
    assert read_history_events(bundle)[-1]["event"] not in _conversion_intents()


@pytest.mark.parametrize(
    "intent_event", ["conversion_retry_intent", "conversion_poll_result_intent"]
)
def test_a_closed_boundary_dispatches_through_the_conversion_attempt_layer(
    tmp_path, capsys, monkeypatch, intent_event
):
    """Plan step 3b's 3.1b-I2: the `has_conversion_attempt` dispatch, which
    every boundary test above stops short of.

    Those tests all end at rc 4 -- that is the point of them -- so
    `_inspect_open_bundle` raises before it ever chooses a
    `result_from_manifest` layer. The layer choice for the shape task 3.1b
    was about (a non-empty `conversion_attempts` list with a retry or poll
    operation in its history) therefore had no test at all: nothing proved
    that once the boundary is closed the bundle reports through
    conversion_attempt's wrapper rather than through source_staging's or
    preflight's, nor that the action it carries is the projector's.

    Both halves are here, on one bundle, in order: `resume` really closes the
    boundary (rc 0, the intent is no longer the tail), and the closed bundle
    then dispatches through the conversion-attempt layer -- proved by the
    three keys only that layer adds -- with an action that is exactly what
    the projector answers for the closed manifest, and specifically NOT
    `resume_pending_conversion_operation`, which would mean the flag had
    leaked onto a bundle with nothing left to resume.
    """
    helpers = _conversion_helpers()
    import conversion_actions

    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, intent_event
    )
    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=_RESUME_TRANSPORT_BY_INTENT[intent_event](helpers),
    )
    assert resume_rc == 0, json.dumps(resumed, sort_keys=True)
    assert read_history_events(bundle)[-1]["event"] not in _conversion_intents()

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"]
    assert "raw_conversion" not in manifest and "review" not in manifest

    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 0, json.dumps(result, sort_keys=True)
    assert transport.calls == []
    # The three keys conversion_attempt.result_from_manifest is the only layer
    # to add: their presence IS the dispatch having gone through it.
    active = manifest["conversion_attempts"][-1]
    assert result["conversion_attempt_state"] == active["state"]
    assert result["conversion_attempt_reason"] == active["reason"]
    assert result["conversion_attempt_reason_detail"] == active["reason_detail"]
    assert "raw_conversion_state" not in result

    projected = conversion_actions.project_conversion_action(manifest)
    assert result["action_required"] != "resume_pending_conversion_operation"
    assert result["action_required"] == (
        None if projected is None else projected["action_required"]
    )


def read_history_events(bundle):
    return [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]


def read_private_state(bundle):
    return json.loads((bundle / ".state" / "private.json").read_text())


def prefix_reduction(bundle):
    """Reduce the bundle's history WITHOUT its tail event, exactly the way
    `at_pending_conversion_boundary` does it.

    The predicate's first act after naming the tail event is to call the
    resolver `workflow._conversion_history_resolver` picked for this manifest,
    on `history[:-1]`, with the on-disk manifest and private state as the two
    templates -- and it returns False right there when the answer is None.
    Every gate past that point is therefore only reachable when this call
    answers something else.

    Two cells below reach their gate only because the untouched private state
    travels through as a template (see their docstrings). Asserting on this
    first is what stops those cells from silently degrading into "the prefix
    reduction rejected it" -- which would still be rc 4 /
    repair_or_restore_work_bundle, so every other assertion in them would go on
    passing while the branch they exist to cover went uncovered again. It calls
    the resolver the predicate calls with the arguments the predicate passes,
    so it needs no instrumentation and touches no production code.
    """
    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = read_private_state(bundle)
    history = read_history_events(bundle)
    resolve_history = workflow._conversion_history_resolver(manifest)
    return resolve_history(
        history[:-1], manifest_template=manifest, private_template=private_state
    )


def _rewrite_state_file(path, value):
    """Put `value` on disk the way the bundle writer would have."""
    import bundle as bundle_module

    path.write_bytes(bundle_module.canonical_json_bytes(value))


def rewrite_private_state(bundle, private_state):
    _rewrite_state_file(bundle / ".state" / "private.json", private_state)


def rewrite_manifest(bundle, manifest):
    _rewrite_state_file(bundle / "manifest.json", manifest)


def retamper_tail_intent(bundle, key, value):
    """Change one field of the dangling tail intent, keeping its key set.

    Nothing verifies a history event's own contents on the way in --
    bundle.read_history only checks that each event is a dict -- and the
    prefix reduction the boundary predicate performs covers history[:-1], so
    the tail intent is exactly the part of a bundle that arrives unchecked.
    """
    events = read_history_events(bundle)
    assert key in events[-1], key
    events[-1][key] = value
    import bundle as bundle_module

    (bundle / ".state" / "history.ndjson").write_bytes(
        b"".join(bundle_module.canonical_json_bytes(event) for event in events)
    )


# --- Task 3.1d fix round 1 (review I-1 + plan step 3b's 3.1b-I1): the
#     NEGATIVE side of the widened pending-boundary predicate.
#
# Task 3.1d widened conversion_attempt.at_pending_conversion_boundary from one
# crash window to three, which meant four new ways for it to answer False --
# a non-list `result_urls`, a replay that raises, a replay that returns None,
# and the three-conjunct admission itself. A full-suite instrumentation run
# over the 607 tests that shipped with it recorded ZERO executions of every
# one of them: the only test that ever reached a False answer with a
# conversion intent on the tail (test_a_genuinely_corrupt_bundle_still_says_
# repair) returns at the earliest gate of all, the prefix reduction.
#
# That is the wrong half of the pair to leave uncovered. Everything the task
# is worth rests on "the action inspect names is one resume can really
# perform"; the fifteen cells above pin the affirmative half, and without
# these, mis-writing any conjunct would hand `resume_pending_conversion_
# operation` -- an instruction to go on running a conversion operation -- to a
# genuinely damaged bundle, with nothing in the suite turning red.
#
# One test per branch, each constructed so that only its own branch can be the
# one that answers False, and each asserting the same four things the positive
# cells assert (rc 4, invalid_bundle, zero network, byte-identical bundle) so
# a regression cannot hide behind a differently-shaped failure.


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
def test_a_tampered_intent_payload_is_still_named_by_the_tail_event(
    tmp_path, capsys, monkeypatch, boundary
):
    """Plan step 3b's 3.1b-I1: the predicate judges legality BY NAME, and the
    advice it gives is honest anyway because `resume` owns the deep check.

    at_pending_conversion_boundary asks only whether the tail event's name is
    a CONVERSION_INTENTS member; it never validates the intent's payload. The
    obvious worry is that this makes `inspect` recommend `resume` for a bundle
    whose intent has been tampered with. It does -- at window 1, where the
    predicate answers True before looking at any payload -- and that
    recommendation is nonetheless true: `resume` is precisely the command that
    replays the payload, and it fails loud with `integrity_violation` rather
    than acting on it. Duplicating that validation in the predicate would
    rebuild the second implementation task 3.1d exists to delete;
    recover_interrupted_attempt is its single owner.

    At the other two windows the predicate must replay the intent to compute
    `desired`, so the tampering reaches it there, and the fail-closed `except`
    the task added (an untested branch until now) turns it into False -- the
    caller keeps `repair_or_restore_work_bundle`. Both answers are honest;
    they differ because the two windows genuinely know different amounts.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_retry_intent", boundary=boundary
    )
    # The intent's evidence_hash must equal the placeholder authorization's;
    # _authorize_state_from_intent refuses the pair outright when it does not.
    # The key set is untouched, so this is a payload lie, not a shape error.
    retamper_tail_intent(bundle, "evidence_hash", "sha256:" + "0" * 64)

    before = state_snapshot(bundle)
    inspect_transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=inspect_transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == (
        "resume_pending_conversion_operation"
        if boundary == "private"
        else "repair_or_restore_work_bundle"
    )
    assert inspect_transport.calls == []
    assert state_snapshot(bundle) == before

    # Whichever action was named, `resume` tells the truth about the payload
    # and changes nothing.
    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_transport = helpers.CountingNeverNetwork()
    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=resume_transport,
    )

    assert resume_rc == 4, json.dumps(resumed, sort_keys=True)
    assert [error["code"] for error in resumed["errors"]] == ["integrity_violation"]
    assert resume_transport.calls == []
    assert state_snapshot(bundle) == before


def test_a_boundary_whose_private_result_urls_are_not_a_list_says_repair(
    tmp_path, capsys, monkeypatch
):
    """The `template_results` type gate, which nothing reached before.

    Past window 1 the predicate has to hand the intent's replay the private
    result payloads it may need, and it reads them straight off the private
    state on disk -- which, at this point in `_inspect_open_bundle`, NOTHING
    has validated yet: the predicate now runs before the private-state
    consistency check. `result_urls` is whatever JSON the file happened to
    hold, so an externally damaged bundle can present a dict there, and
    iterating it inside the replay would be a TypeError escaping as rc 1 /
    runtime_error on the very population this branch exists to diagnose.

    Reaching this gate takes one specific shape, measured rather than assumed:
    the tail intent must be the FIRST conversion event in the bundle's
    history. Otherwise the prefix reduction gets there first --
    _reduce_history hands the untouched private state to
    apply_committed_operations as its template, and that function's own
    `not isinstance(template_results, list)` check answers None, so the
    predicate returns at `previous is None` instead. With no conversion
    operation in the prefix, _reduce_history returns the source-staging
    reduction directly (it forces `result_urls` to `[]` for that call), the
    damaged value survives to here, and this is the gate that catches it. A
    crash during the very first submission is exactly that shape.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_submit_intent",
        boundary="manifest",
    )
    assert not [
        event
        for event in read_history_events(bundle)[:-1]
        if event.get("event") in _conversion_intents()
    ], "the tail intent must be the first conversion event (see docstring)"
    private_state = read_private_state(bundle)
    assert isinstance(private_state["result_urls"], list)
    private_state["result_urls"] = {}
    rewrite_private_state(bundle, private_state)
    assert prefix_reduction(bundle) is not None, (
        "the prefix must still reduce, or the predicate answers at "
        "`previous is None` and never reaches the template_results gate"
    )

    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_a_poll_boundary_whose_result_payload_does_not_match_says_repair(
    tmp_path, capsys, monkeypatch
):
    """The `replayed is None` gate, which nothing reached before.

    A `result_ready` poll intent can only be replayed if the private payload
    it recorded is findable by (attempt_id, url_sha256); the replay answers
    None when it is not. Here it is present but its digest has been altered,
    so the lookup finds nothing and the bundle is not at a recoverable
    boundary -- and `resume` agrees, refusing the same bundle rather than
    closing it.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_poll_result_intent",
        boundary="manifest",
    )
    private_state = read_private_state(bundle)
    assert len(private_state["result_urls"]) == 1
    private_state["result_urls"][-1]["url_sha256"] = "sha256:" + "1" * 64
    rewrite_private_state(bundle, private_state)

    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before

    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_transport = helpers.CountingNeverNetwork()
    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=resume_transport,
    )
    assert resume_rc == 4, json.dumps(resumed, sort_keys=True)
    assert resume_transport.calls == []
    assert state_snapshot(bundle) == before


def test_a_boundary_whose_manifest_is_neither_state_says_repair(
    tmp_path, capsys, monkeypatch
):
    """The admission's FIRST conjunct: manifest must be `previous` or
    `desired`.

    The window-3 bundle below really is parked on a pending intent and really
    would be closed by `resume` -- the fifteen cells above prove it -- so the
    only thing standing between a tampered manifest and the instruction "carry
    on with the conversion operation" is this conjunct. The tamper is a
    changed attempt_id: still schema-shaped, so nothing before the predicate
    rejects it, and not equal to either admitted state.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_retry_intent", boundary="commit"
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["conversion_attempts"][-1]["attempt_id"] = "attempt-" + "9" * 24
    rewrite_manifest(bundle, manifest)

    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_a_boundary_whose_private_state_is_neither_state_says_repair(
    tmp_path, capsys, monkeypatch
):
    """The admission's SECOND conjunct: private.json must be `previous` or
    `desired`.

    Window 2 leaves the manifest at `previous`, so the first conjunct holds no
    matter what happens to private.json -- this cell is the second conjunct on
    its own. The damage is a recorded result payload no journal event ever
    produced, which belongs to neither admitted state.

    It has to be damage the prefix reduction itself tolerates, or the
    predicate returns at `previous is None` and this conjunct is never
    evaluated (that is what happens to a tampered `source_uploads` entry, for
    instance -- the staging reducer rejects it). `result_urls` is the one
    private field the reduction structurally ignores: with the tail intent as
    the first conversion event, _reduce_history reduces the staging prefix
    with `result_urls` forced to `[]` and returns that, so the disk value is
    never read on the way to here -- only compared against, which is exactly
    what this conjunct is for.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, "conversion_submit_intent", boundary="manifest"
    )
    assert not [
        event
        for event in read_history_events(bundle)[:-1]
        if event.get("event") in _conversion_intents()
    ], "the tail intent must be the first conversion event (see docstring)"
    private_state = read_private_state(bundle)
    assert private_state["result_urls"] == []
    private_state["result_urls"] = [
        {
            "attempt_id": "attempt-" + "8" * 24,
            "task_id": "task-that-was-never-polled",
            "url": "https://results.aihubmax.com/never-observed.zip",
            "url_sha256": "sha256:" + "2" * 64,
            "observed_at": "2024-01-01T00:00:00Z",
            "expires_at": None,
            "validity_window_hours": 24,
        }
    ]
    rewrite_private_state(bundle, private_state)
    assert prefix_reduction(bundle) is not None, (
        "the prefix must still reduce, or the predicate answers at "
        "`previous is None` and never evaluates the second conjunct"
    )

    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_the_write_orders_forbidden_fourth_state_says_repair(
    tmp_path, capsys, monkeypatch
):
    """The admission's THIRD conjunct: `manifest` desired while `private` is
    previous is refused even though both halves are individually admitted.

    No crash can produce this state -- private.json is written first -- so a
    bundle in it did not get there by being interrupted, and
    recover_interrupted_attempt refuses it for exactly that reason
    ("partially inconsistent"). Without this conjunct the predicate would call
    it a recoverable boundary and `inspect` would name an action `resume`
    then refuses to perform: the two sides of the contract this whole task
    exists to keep in step would disagree, on the one state the write order
    says cannot happen.

    It has to be built by hand, and `previous` is taken as the literal bytes
    private.json held at the instant the intent's own write replaced them --
    not recomputed from the journal, which would make the fixture depend on
    the same prefix reduction the predicate under test performs.
    """
    helpers = _conversion_helpers()
    captured = []
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path,
        capsys,
        monkeypatch,
        "conversion_retry_intent",
        boundary="commit",
        previous_private_out=captured,
    )
    assert len(captured) == 1
    desired_private = (bundle / ".state" / "private.json").read_bytes()
    assert captured[0] != desired_private, (
        "this intent must actually change private.json, or the state being "
        "built here would be window 3 rather than the forbidden one"
    )
    (bundle / ".state" / "private.json").write_bytes(captured[0])

    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before

    # ...and the refusal is not merely conservative: `resume` refuses this
    # same state too, which is what makes `repair_or_restore_work_bundle` the
    # honest answer rather than a missed recovery.
    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_transport = helpers.CountingNeverNetwork()
    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=resume_transport,
    )
    assert resume_rc == 4, json.dumps(resumed, sort_keys=True)
    assert [error["code"] for error in resumed["errors"]] == ["integrity_violation"]
    assert resume_transport.calls == []
    assert state_snapshot(bundle) == before


def corrupt_history_prefix(bundle):
    """Break the history *before* the dangling intent.

    The prefix reduce is what tells "parked on an intent" apart from "really
    corrupt", so the negative case has to damage the prefix and nothing else:
    one committed event's `manifest_hash` is replaced, which no reducer can
    reconcile, while every schema check that runs before the history check
    still passes.
    """
    import bundle as bundle_module

    events = read_history_events(bundle)
    for event in events:
        if event.get("event") == "source_upload_result_committed":
            event["manifest_hash"] = "sha256:" + "0" * 64
            break
    else:  # pragma: no cover - the driver always writes this event
        raise AssertionError("no source_upload_result_committed event to corrupt")
    (bundle / ".state" / "history.ndjson").write_bytes(
        b"".join(bundle_module.canonical_json_bytes(event) for event in events)
    )


def retype_tail_event_name(bundle, value):
    """Give the last history event a non-string `event` value.

    `bundle.read_history` only checks that each event is a dict -- the value
    under the "event" key is never type-checked -- so a bundle whose tail event
    name has been replaced by a container is read in without complaint. That is
    an externally damaged bundle, which is exactly the population `inspect`'s
    invalid_bundle / repair_or_restore_work_bundle branch exists to diagnose.
    """
    import bundle as bundle_module

    events = read_history_events(bundle)
    events[-1]["event"] = value
    (bundle / ".state" / "history.ndjson").write_bytes(
        b"".join(bundle_module.canonical_json_bytes(event) for event in events)
    )


@pytest.mark.parametrize("event_value", [{"a": 1}, ["x"]], ids=["dict", "list"])
def test_a_non_string_tail_event_name_still_says_repair(
    tmp_path, capsys, monkeypatch, event_value
):
    """An unhashable tail event name must not turn rc 4 into rc 1.

    The boundary test asks whether the tail event is a conversion intent. Asking
    that with `in` against a frozenset raises TypeError -- not returns False --
    when the value is unhashable, and neither _at_pending_conversion_boundary
    nor _inspect_open_bundle catches it, so it escapes to main's last-resort
    handler and is reported as rc 1 / runtime_error / inspect_runtime_error.
    That loses all three fields this branch is required to keep (rc 4,
    invalid_bundle, an action the caller can act on) for the one population the
    branch is for.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = drive_to_pending_conversion_intent(
        tmp_path, capsys, monkeypatch
    )
    retype_tail_event_name(bundle, event_value)
    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()

    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_inspect_at_a_pending_conversion_boundary_points_at_resume(
    tmp_path, capsys, monkeypatch
):
    helpers = _conversion_helpers()
    bundle, _staged, environ = drive_to_pending_conversion_intent(
        tmp_path, capsys, monkeypatch
    )
    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()

    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "resume_pending_conversion_operation"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_a_genuinely_corrupt_bundle_still_says_repair(tmp_path, capsys, monkeypatch):
    helpers = _conversion_helpers()
    bundle, _staged, environ = drive_to_pending_conversion_intent(
        tmp_path, capsys, monkeypatch
    )
    corrupt_history_prefix(bundle)
    before = state_snapshot(bundle)
    transport = helpers.CountingNeverNetwork()

    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_resume_still_closes_the_same_boundary_without_network(
    tmp_path, capsys, monkeypatch
):
    """The action `inspect` now names has to be one `resume` can actually do."""
    helpers = _conversion_helpers()
    bundle, staged, environ = drive_to_pending_conversion_intent(
        tmp_path, capsys, monkeypatch
    )
    transport = helpers.CountingNeverNetwork()

    rc, result, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )

    assert rc == 0
    assert transport.calls == []
    assert result["conversion_state"] == "submission_unknown"


@pytest.mark.parametrize(
    "intent_event",
    sorted(workflow.conversion_attempt_module.CONVERSION_INTENTS),
)
def test_every_conversion_intent_boundary_names_an_action_resume_performs(
    tmp_path, capsys, monkeypatch, intent_event
):
    """All five intents, not just the submission one.

    `_at_pending_conversion_boundary` admits any CONVERSION_INTENTS member, so
    "the action `inspect` names is one `resume` can really do" is a claim about
    five events. Only `conversion_submit_intent` was pinned; the other four
    rested on reading recover_interrupted_attempt's branches (3.1b review, M4).
    Parametrising off the runtime frozenset also means a sixth intent cannot be
    added without either a driver here or a deliberate, visible failure.

    Two halves per member, because either alone is satisfiable by a lie:
    `inspect` names the resume action (rc 4, invalid_bundle and zero writes all
    intact), and `resume` then really closes that intent -- rc 0, no network,
    and the dangling intent is no longer the last event.
    """
    helpers = _conversion_helpers()
    bundle, _staged, environ = park_on_conversion_intent(
        tmp_path, capsys, monkeypatch, intent_event
    )
    before = state_snapshot(bundle)
    inspect_transport = helpers.CountingNeverNetwork()

    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=environ,
        transport=inspect_transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "resume_pending_conversion_operation"
    assert inspect_transport.calls == []
    assert state_snapshot(bundle) == before

    # The generation is read off disk rather than remembered: some of these
    # intents are opened by a command that already committed an earlier
    # generation bump, so a stale --expected-generation would fail the resume
    # for a reason that has nothing to do with the boundary.
    generation = json.loads((bundle / "manifest.json").read_text())["generation"]
    resume_transport = helpers.CountingNeverNetwork()

    resume_rc, resumed, _stderr = helpers.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=resume_transport,
    )

    assert resume_rc == 0, json.dumps(resumed, sort_keys=True)
    assert resume_transport.calls == []
    assert read_history_events(bundle)[-1]["event"] != intent_event


def test_a_pending_source_staging_intent_is_not_called_a_conversion_boundary(
    tmp_path, capsys, monkeypatch
):
    """The boundary test discriminates on the intent, not just on the prefix.

    A source upload parked on its own dangling `source_upload_result_intent`
    reduces its prefix to exactly what is on disk too -- measured, not
    assumed -- so the prefix reduce alone cannot tell it apart from a
    conversion boundary. Only the tail event can, and
    `resume_pending_conversion_operation` is a conversion-vocabulary action
    that must not be handed out for a staging operation (nor smuggled through
    the WorkflowError narrow gate on its behalf).

    Pinned to the same standard as the conversion boundary cells (plan step
    3b, 3.1b-I3): a COUNTING transport, so "no network" is an assertion about
    a recorded call list rather than about an exception that happens not to
    have been raised, and a byte-and-mtime snapshot of the whole bundle, so
    "no writes" covers every file rather than the two the assertions happen
    to read back.
    """
    helpers = _staging_helpers()
    conversion_helpers = _conversion_helpers()
    bundle, ready, dependencies, _source_bytes = helpers.ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    key = "test-aihub-key-123456"
    upload = helpers.SuccessfulUpload(
        "https://files.aihubmax.com/source.pdf?token=private-bearer"
    )
    original_atomic_write = helpers.source_staging.bundle.atomic_write_json
    private_writes = 0

    def crash_on_the_result_commit(name, value, *, dir_fd):
        nonlocal private_writes
        if name == "private.json":
            private_writes += 1
            if private_writes == 2:
                raise helpers.SimulatedProcessCrash
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(
        helpers.source_staging.bundle, "atomic_write_json", crash_on_the_result_commit
    )
    with pytest.raises(helpers.SimulatedProcessCrash):
        workflow.main(
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
                "--visual-capability",
                "available",
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=upload,
            now=helpers.NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        helpers.source_staging.bundle, "atomic_write_json", original_atomic_write
    )
    history = read_history_events(bundle)
    assert history[-1]["event"] == "source_upload_result_intent"

    before = state_snapshot(bundle)
    transport = conversion_helpers.CountingNeverNetwork()
    rc, result, _stderr = helpers.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    assert rc == 4
    assert [error["code"] for error in result["errors"]] == ["invalid_bundle"]
    assert result["action_required"] == "repair_or_restore_work_bundle"
    assert transport.calls == []
    assert state_snapshot(bundle) == before


def test_the_workflow_error_conversion_gate_admits_exactly_one_pair():
    """Pin the narrow gate: one action, one code, nothing else.

    `resume_pending_conversion_operation` is a CONVERSION_ACTIONS member, so
    the closed ERROR_PATH_ACTIONS check in WorkflowError.__init__ has to admit
    it -- but only paired with `invalid_bundle`. Widening the check to the
    whole conversion table (or to any code) must fail here.
    """
    import conversion_attempt

    assert workflow.CONVERSION_ACTION_EXCEPTIONS == frozenset(
        {("invalid_bundle", "resume_pending_conversion_operation")}
    )
    # Not a dead literal: the admitted action is really a conversion-vocabulary
    # member, and is still disjoint from the error-path vocabulary.
    for _code, action in workflow.CONVERSION_ACTION_EXCEPTIONS:
        assert action in conversion_attempt.CONVERSION_ACTIONS
        assert action not in workflow.ERROR_PATH_ACTIONS

    admitted = workflow.WorkflowError(
        "invalid_bundle",
        "message",
        return_code=4,
        action_required="resume_pending_conversion_operation",
    )
    assert admitted.action_required == "resume_pending_conversion_operation"

    # Same action, a different code -- still refused.
    for code in ("integrity_violation", "invalid_arguments", "runtime_error"):
        with pytest.raises(ValueError) as excinfo:
            workflow.WorkflowError(
                code,
                "message",
                return_code=4,
                action_required="resume_pending_conversion_operation",
            )
        assert "resume_pending_conversion_operation" in str(excinfo.value)

    # Same code, every other conversion action -- still refused.
    for action in sorted(
        conversion_attempt.CONVERSION_ACTIONS - {"resume_pending_conversion_operation"}
    ):
        with pytest.raises(ValueError) as excinfo:
            workflow.WorkflowError(
                "invalid_bundle", "message", return_code=4, action_required=action
            )
        assert action in str(excinfo.value)
