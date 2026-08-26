from pathlib import Path

import pytest

from video_to_markdown import config


def test_config_layering_and_per_variable_resolution(tmp_path: Path):
    (tmp_path / ".env.local").write_text(
        "AIHUB_API_KEY=local-key\nVIDEO_TO_MARKDOWN_ASR_MODEL=scribe-v2\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "AIHUB_API_KEY=env-file-key\nVIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR=from-env-file\n",
        encoding="utf-8",
    )
    result = config.resolve(
        {
            "AIHUB_API_KEY": "process-key",
            "VIDEO_TO_MARKDOWN_ASR_MODEL": "",
        },
        tmp_path,
    )
    assert result.api_key == "process-key"
    assert result.asr_model == "scribe-v2"
    assert result.output_base == "from-env-file"
    assert result.sources["api_key"] == "process environment"


def test_config_does_not_search_parent_and_home_requires_authorization(tmp_path: Path):
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=parent-key\n", encoding="utf-8")
    home = tmp_path / "home-config"
    home.mkdir()
    (home / ".env").write_text("AIHUB_API_KEY=home-key\n", encoding="utf-8")

    unauthorized = config.resolve({}, child, use_home=False, home_config_dir=home)
    assert unauthorized.api_key is None
    authorized = config.resolve({}, child, use_home=True, home_config_dir=home)
    assert authorized.api_key == "home-key"


def test_dotenv_is_literal_and_last_value_wins():
    parsed = config.parse_dotenv(
        "AIHUB_API_KEY=$(whoami)\nAIHUB_API_KEY='${SECRET}'\nUNKNOWN=value\n"
    )
    assert parsed == {"AIHUB_API_KEY": "${SECRET}"}


def test_unknown_model_fails_before_use(tmp_path: Path):
    with pytest.raises(config.ConfigError):
        config.resolve(
            {"VIDEO_TO_MARKDOWN_ASR_MODEL": "not-a-model"},
            tmp_path,
        )


def test_legacy_key_is_supported_and_identified(tmp_path: Path):
    result = config.resolve({"X_API_KEY": "abcdefghij"}, tmp_path)
    assert result.api_key == "abcdefghij"
    assert result.api_key_name == "X_API_KEY"
    assert config.mask_key(result.api_key) == "abcd****ghij"
