from frpc_launch import (mask_secret, mask_text, parse_env_file,
                         _extract_official_secrets)


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


def test_mask_text_skips_too_short_secrets():
    # review W5：极短值做全文替换会把端口号/时间戳打花，len<6 不做全文替换
    assert mask_text("port 1 at 10:00", ["1"]) == "port 1 at 10:00"


def test_extract_secrets_all_quote_styles():
    # review M1/W7：双引号、单引号、三引号（basic/literal multiline）都要提取
    toml = (
        'auth.token = "dq_secret_123"\n'
        "webServer.password = 'sq_secret_456'\n"
        'auth.oidc.clientSecret = """tq_secret_789"""\n'
    )
    secrets = _extract_official_secrets(toml)
    assert set(secrets) == {"dq_secret_123", "sq_secret_456", "tq_secret_789"}


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
