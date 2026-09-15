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


def test_skill_settings_layer_survives_snapshot_validation(tmp_path):
    (tmp_path / ".env.pdf2markdown").write_text("PDF2MARKDOWN_INTERACTION_MODE=auto\nPDF2MARKDOWN_PUBLISH_MODE=''\nPDF2MARKDOWN_OUTPUT_DIR=skill-output\n")
    (tmp_path / ".env.local").write_text("PDF2MARKDOWN_INTERACTION_MODE=confirm\nPDF2MARKDOWN_PUBLISH_MODE=skip\n")
    kwargs = dict(environ={}, cwd=tmp_path, config_home=tmp_path / "home", use_local_key=False)
    result = settings.resolve(None, cli={}, **kwargs)
    assert result["interaction_mode"] == "auto"
    assert result["sources"]["interaction_mode"] == "cwd_dotenv_skill"
    assert result["sources"]["publishing.mode"] == "cwd_dotenv_local"
    assert settings.resolve_variable("PDF2MARKDOWN_OUTPUT_DIR", **kwargs) == "skill-output"
    status = settings.status(tmp_path / "home/settings.json", cli={}, environ={}, cwd=tmp_path, config_home=tmp_path / "home", home_config_authorized=False)
    snapshot = settings.snapshot(status, cwd=tmp_path)
    settings.validate_snapshot(snapshot)
