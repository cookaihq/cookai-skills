from pathlib import Path

import config
import settings


def test_skill_credential_locator_can_be_resumed_without_reselecting(tmp_path):
    dedicated = tmp_path / ".env.pdf2markdown"
    dedicated.write_text("AIHUB_API_KEY=skill-key\n")
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=local-key\n")
    (tmp_path / ".env.other-skill").write_text("AIHUB_API_KEY=wrong-key\n")
    kwargs = dict(cwd=tmp_path, config_home=tmp_path / "home", use_local_key=False)
    selected = config.resolve_api_key(environ={}, **kwargs)
    assert selected.value == "skill-key"
    assert selected.locator["path"] == str(dedicated)
    identity = {"source_id": selected.source_id, "fingerprint": selected.fingerprint, "locator": selected.locator}
    resumed = config.read_exact_api_key(identity, environ={"AIHUB_API_KEY": "changed-process-key"}, config_home=kwargs["config_home"], use_local_key=False)
    assert resumed.value == "skill-key"
    assert config.resolve_api_key(environ={"AIHUB_API_KEY": "process-key"}, **kwargs).value == "process-key"
    dedicated.write_text("AIHUB_API_KEY=''\n")
    assert config.resolve_api_key(environ={}, **kwargs).value == "local-key"


def test_output_directory_follows_the_same_layers(tmp_path):
    (tmp_path / ".env.pdf2markdown").write_text("PDF2MARKDOWN_OUTPUT_DIR=skill-output\n")
    (tmp_path / ".env.local").write_text("PDF2MARKDOWN_OUTPUT_DIR=local-output\n")
    kwargs = dict(environ={}, cwd=tmp_path, config_home=tmp_path / "home", use_local_key=False)
    assert settings.resolve_variable("PDF2MARKDOWN_OUTPUT_DIR", **kwargs) == "skill-output"
    (tmp_path / ".env.pdf2markdown").write_text("PDF2MARKDOWN_OUTPUT_DIR=''\n")
    assert settings.resolve_variable("PDF2MARKDOWN_OUTPUT_DIR", **kwargs) == "local-output"
    kwargs["environ"] = {"PDF2MARKDOWN_OUTPUT_DIR": "process-output"}
    assert settings.resolve_variable("PDF2MARKDOWN_OUTPUT_DIR", **kwargs) == "process-output"


def test_parent_and_other_skill_configs_are_not_read_and_home_requires_flag(tmp_path):
    import pytest
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".env").write_text("AIHUB_API_KEY=parent-key\n")
    (project / ".env.other").write_text("AIHUB_API_KEY=other-key\n")
    (home / ".env").write_text("AIHUB_API_KEY=home-key\n")
    kwargs = dict(environ={}, cwd=project, config_home=home)
    with pytest.raises(config.ConfigError):
        config.resolve_api_key(use_local_key=False, **kwargs)
    key = config.resolve_api_key(use_local_key=True, **kwargs)
    assert key.value == "home-key"
    with pytest.raises(config.ConfigError, match="not authorized"):
        config.read_exact_api_key(key.public_identity, environ={}, config_home=home, use_local_key=False)


def test_config_report_never_contains_secret(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("AIHUB_API_KEY", "fake-secret-do-not-print")
    report = config.configuration_report(cwd=tmp_path)
    assert report["AIHUB_API_KEY"]["configured"] is True
    assert report["AIHUB_API_KEY"]["source"] == "process_environment:AIHUB_API_KEY"
    assert "fake-secret" not in json.dumps(report)
    assert report["online_auth_checked"] is False
