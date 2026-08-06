import os

import config


def test_parse_dotenv_basic_and_quotes_and_comments():
    text = (
        "# comment line\n"
        "\n"
        "AIHUB_API_KEY=sk-plain\n"
        'OTHER="quoted value"\n'
        "SPACED =  trimmed \n"
    )
    out = config.parse_dotenv(text)
    assert out["AIHUB_API_KEY"] == "sk-plain"
    assert out["OTHER"] == "quoted value"
    assert out["SPACED"] == "trimmed"
    assert "# comment line" not in out


def test_parse_dotenv_last_wins_and_no_shell_expansion():
    text = "AIHUB_API_KEY=first\nAIHUB_API_KEY=second\nLIT=${OTHER}\n"
    out = config.parse_dotenv(text)
    assert out["AIHUB_API_KEY"] == "second"
    assert out["LIT"] == "${OTHER}"  # literal, no expansion


def test_read_key_from_dotenv_missing_file_returns_none():
    assert config.read_key_from_dotenv("/nonexistent/path/.env") is None


def test_read_key_from_dotenv_reads_x_api_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AIHUB_API_KEY='sk-fromfile'\n", encoding="utf-8")
    assert config.read_key_from_dotenv(str(p)) == "sk-fromfile"


def test_resolve_api_keys_precedence_env_then_dotenvlocal_then_dotenv(tmp_path):
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=sk-local\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AIHUB_API_KEY=sk-dotenv\n", encoding="utf-8")
    keys = config.resolve_api_keys(
        environ={"AIHUB_API_KEY": "sk-env"},
        cwd=str(tmp_path),
        use_local_key=False,
        config_dir="/unused",
    )
    assert keys == ["sk-env", "sk-local", "sk-dotenv"]


def test_resolve_api_keys_dedup_preserves_order(tmp_path):
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=sk-env\n", encoding="utf-8")
    keys = config.resolve_api_keys(
        environ={"AIHUB_API_KEY": "sk-env"},
        cwd=str(tmp_path),
        use_local_key=False,
        config_dir="/unused",
    )
    assert keys == ["sk-env"]  # duplicate value collapsed


def test_resolve_api_keys_config_dir_only_with_flag(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".env").write_text("AIHUB_API_KEY=sk-persist\n", encoding="utf-8")
    without = config.resolve_api_keys({}, str(tmp_path), use_local_key=False, config_dir=str(cfg))
    assert without == []
    with_flag = config.resolve_api_keys({}, str(tmp_path), use_local_key=True, config_dir=str(cfg))
    assert with_flag == ["sk-persist"]


def test_legacy_x_api_key_is_still_accepted_everywhere(tmp_path):
    (tmp_path / ".env.local").write_text("X_API_KEY=sk-legacy-local\n", encoding="utf-8")
    cands = config.resolve_api_key_candidates(
        environ={"X_API_KEY": "sk-legacy-env"},
        cwd=str(tmp_path),
        use_local_key=False,
        config_dir="/unused",
    )
    assert [c.value for c in cands] == ["sk-legacy-env", "sk-legacy-local"]
    assert all(c.is_legacy for c in cands)
    notice = config.legacy_key_notice(cands)
    assert notice is not None and "AIHUB_API_KEY" in notice


def test_canonical_name_wins_over_legacy_within_one_source(tmp_path):
    (tmp_path / ".env.local").write_text(
        "X_API_KEY=sk-legacy\nAIHUB_API_KEY=sk-canonical\n", encoding="utf-8"
    )
    cands = config.resolve_api_key_candidates(
        environ={"X_API_KEY": "sk-legacy-env", "AIHUB_API_KEY": "sk-canonical-env"},
        cwd=str(tmp_path),
        use_local_key=False,
        config_dir="/unused",
    )
    assert [c.value for c in cands] == ["sk-canonical-env", "sk-canonical"]
    assert not any(c.is_legacy for c in cands)
    assert config.legacy_key_notice(cands) is None


def test_source_precedence_outranks_key_name(tmp_path):
    # A legacy name in a higher-priority source still beats the canonical name
    # in a lower-priority one — layer order is the outer rule.
    (tmp_path / ".env").write_text("AIHUB_API_KEY=sk-canonical-dotenv\n", encoding="utf-8")
    cands = config.resolve_api_key_candidates(
        environ={"X_API_KEY": "sk-legacy-env"},
        cwd=str(tmp_path),
        use_local_key=False,
        config_dir="/unused",
    )
    assert [c.value for c in cands] == ["sk-legacy-env", "sk-canonical-dotenv"]


def test_mask_key():
    assert config.mask_key("sk-1234567890abcd") == "sk-1****abcd"
    assert config.mask_key("short") == "****"
    assert config.mask_key("") == "(empty)"
    assert config.mask_key("123456789") == "1234****6789"  # 9-char: first of long branch
    assert config.mask_key("12345678") == "****"            # 8-char: last of short branch
