from frpc_launch import mask_secret, mask_text, parse_env_file


def test_mask_secret_head4_tail4():
    assert mask_secret("abcdefghijklmnop") == "abcd****mnop"


def test_mask_secret_short_and_empty():
    assert mask_secret("12345678") == "****"
    assert mask_secret("") == "(未设置)"


def test_mask_text_replaces_all_occurrences():
    s = "tok_secret_value_1"
    text = "login token=tok_secret_value_1 retry token=tok_secret_value_1"
    out = mask_text(text, [s])
    assert "tok_secret_value_1" not in out
    assert mask_secret(s) in out


def test_parse_env_file_rules(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n"
        "\n"
        "FRPC_LAUNCH_MODE = official\n"
        "FRPC_LAUNCH_SAKURA_KEY=\"quoted value\"\n"
        "FRPC_LAUNCH_SAKURA_KEY='last wins'\n"
        "NOT_EXPANDED=$HOME/x\n",
        encoding="utf-8",
    )
    env = parse_env_file(f)
    assert env["FRPC_LAUNCH_MODE"] == "official"
    assert env["FRPC_LAUNCH_SAKURA_KEY"] == "last wins"
    assert env["NOT_EXPANDED"] == "$HOME/x"   # 不做展开


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope") == {}
