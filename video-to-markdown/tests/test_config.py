from pathlib import Path

import pytest

from video_to_markdown import config


def test_config_layering_and_per_variable_resolution(tmp_path: Path):
    (tmp_path / ".env.local").write_text(
        "VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR=from-env-file\n",
        encoding="utf-8",
    )
    result = config.resolve(
        {
            "VIDEO_TO_MARKDOWN_ASR_PROVIDER": "aihub-audio",
        },
        tmp_path,
    )
    assert result.asr_provider == "aihub-audio"
    assert result.output_base == "from-env-file"
    assert result.sources["asr_provider"] == "process environment"


def test_config_does_not_search_parent_and_home_requires_authorization(tmp_path: Path):
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n", encoding="utf-8")
    home = tmp_path / "home-config"
    home.mkdir()
    (home / ".env").write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\n", encoding="utf-8")

    unauthorized = config.resolve({}, child, use_home=False, home_config_dir=home)
    assert unauthorized.asr_provider == "aihub-audio"
    authorized = config.resolve({}, child, use_home=True, home_config_dir=home)
    assert authorized.asr_provider == "feishu-minutes"


def test_dotenv_is_literal_and_last_value_wins():
    parsed = config.parse_dotenv(
        "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR=$(whoami)\nVIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR='${LITERAL}'\nUNKNOWN=value\n"
    )
    assert parsed == {"VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR": "${LITERAL}"}


def test_old_model_intent_cannot_silently_be_discarded(tmp_path: Path):
    with pytest.raises(config.ConfigError):
        config.resolve(
            {"VIDEO_TO_MARKDOWN_ASR_MODEL": "not-a-model"},
            tmp_path,
        )


def test_legacy_credentials_are_not_consumed_or_copied(tmp_path: Path):
    result = config.resolve({"X_API_KEY": "abcdefghij"}, tmp_path)
    assert not hasattr(result, "api_key")
    assert not result.sources
    assert list(tmp_path.iterdir()) == []


def test_skill_specific_layer_is_per_field_and_does_not_read_other_skills(tmp_path):
    dedicated = tmp_path / ".env.video-to-markdown"
    dedicated.write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=feishu-minutes\nVIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR=''\n")
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=aihub-audio\nVIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR=task\n")
    (tmp_path / ".env.other-skill").write_text("VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR=wrong\n")
    result = config.resolve({}, tmp_path)
    assert result.asr_provider == "feishu-minutes"
    assert result.sources["asr_provider"] == str(dedicated)
    assert result.source_video_dir == "task"
    assert result.output_base is None
    assert config.resolve({"VIDEO_TO_MARKDOWN_ASR_PROVIDER": "aihub-audio"}, tmp_path).asr_provider == "aihub-audio"
    dedicated.write_text("VIDEO_TO_MARKDOWN_ASR_PROVIDER=''\n")
    assert config.resolve({}, tmp_path).asr_provider == "aihub-audio"


def test_identity_group_does_not_fill_missing_fields_from_lower_layers(tmp_path):
    (tmp_path / ".env.local").write_text("VIDEO_TO_MARKDOWN_FEISHU_PROFILE=local\n")
    (tmp_path / ".env").write_text("VIDEO_TO_MARKDOWN_FEISHU_APP_ID=wrong-app\nVIDEO_TO_MARKDOWN_FEISHU_USER_OPEN_ID=wrong-user\n")
    resolved = config.resolve({}, tmp_path)
    assert resolved.feishu_identity == {"profile": "local", "app_id": "", "user_open_id": ""}


def test_invalid_provider_is_an_error(tmp_path):
    with pytest.raises(config.ConfigError, match="must be"):
        config.resolve({config.PROVIDER_KEY: "typo"}, tmp_path)
