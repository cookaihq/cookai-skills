import config


def test_parse_dotenv_quotes_comments_last_wins():
    out = config.parse_dotenv("# c\n\nX_API_KEY=sk-a\nX_API_KEY='sk-b'\nLIT=${X}\n")
    assert out["X_API_KEY"] == "sk-b"
    assert out["LIT"] == "${X}"


def test_resolve_precedence_and_dedup(tmp_path):
    (tmp_path / ".env.local").write_text("X_API_KEY=sk-local\n", encoding="utf-8")
    (tmp_path / ".env").write_text("X_API_KEY=sk-dotenv\n", encoding="utf-8")
    keys = config.resolve_api_keys({"X_API_KEY": "sk-env"}, str(tmp_path), False, "/unused")
    assert keys == ["sk-env", "sk-local", "sk-dotenv"]


def test_config_dir_only_with_flag(tmp_path):
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / ".env").write_text("X_API_KEY=sk-persist\n", encoding="utf-8")
    assert config.resolve_api_keys({}, str(tmp_path), False, str(cfg)) == []
    assert config.resolve_api_keys({}, str(tmp_path), True, str(cfg)) == ["sk-persist"]


def test_mask_key():
    assert config.mask_key("sk-1234567890abcd") == "sk-1****abcd"
    assert config.mask_key("short") == "****"
    assert config.mask_key("") == "(empty)"
