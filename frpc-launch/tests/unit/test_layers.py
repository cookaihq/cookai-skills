from frpc_launch import (resolve_layered, official_config_path,
                         official_config_valid, sakura_config)


def _mkhome(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_first_found_wins_per_variable(tmp_path):
    home = _mkhome(tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".env.local").write_text("FRPC_LAUNCH_MODE=official\n")
    (cwd / ".env").write_text("FRPC_LAUNCH_MODE=sakura\nFRPC_LAUNCH_SAKURA_KEY=k_from_dotenv_x\n")
    (home / ".env").write_text("FRPC_LAUNCH_SAKURA_TUNNELS=1,2\n")
    layered = resolve_layered({"FRPC_LAUNCH_CONFIG": "/x/frpc.toml"}, cwd, home)
    assert layered["FRPC_LAUNCH_CONFIG"] == ("/x/frpc.toml", "env")
    assert layered["FRPC_LAUNCH_MODE"] == ("official", ".env.local")
    assert layered["FRPC_LAUNCH_SAKURA_KEY"] == ("k_from_dotenv_x", ".env")
    assert layered["FRPC_LAUNCH_SAKURA_TUNNELS"] == ("1,2", "global")


def test_official_config_prefers_workspace_over_global(tmp_path):
    home = _mkhome(tmp_path)
    (home / "frpc.toml").write_text('serverAddr = "g"\n')
    proj_toml = tmp_path / "proj.toml"
    proj_toml.write_text('serverAddr = "p"\n')
    layered = {"FRPC_LAUNCH_CONFIG": (str(proj_toml), ".env.local")}
    path, source = official_config_path(layered, home)
    assert path == proj_toml and source == ".env.local"
    path, source = official_config_path({}, home)
    assert path == home / "frpc.toml" and source == "global"


def test_official_config_valid_requires_serveraddr(tmp_path):
    good = tmp_path / "a.toml"
    good.write_text('serverAddr = "example.com"\nserverPort = 7000\n')
    bad = tmp_path / "b.toml"
    bad.write_text("# empty\n")
    assert official_config_valid(good)[0] is True
    ok, reason = official_config_valid(bad)
    assert ok is False and "serverAddr" in reason


def test_sakura_config_requires_key_and_tunnels():
    only_key = {"FRPC_LAUNCH_SAKURA_KEY": ("k1234567890", ".env")}
    assert sakura_config(only_key) == (None, "")
    both = dict(only_key, FRPC_LAUNCH_SAKURA_TUNNELS=("11,22", "global"))
    cfg, source = sakura_config(both)
    assert cfg == {"key": "k1234567890", "tunnels": "11,22", "frpc_path": None}
    assert source == ".env"


def test_skill_specific_file_precedence_and_isolation(tmp_path):
    home = _mkhome(tmp_path)
    (tmp_path / ".env.frpc-launch").write_text("FRPC_LAUNCH_MODE=official\nFRPC_LAUNCH_CONFIG=''\n")
    (tmp_path / ".env.local").write_text("FRPC_LAUNCH_MODE=sakura\nFRPC_LAUNCH_CONFIG=/project/frpc.toml\n")
    (tmp_path / ".env.other-skill").write_text("FRPC_LAUNCH_SAKURA_KEY=wrong\n")
    result = resolve_layered({}, tmp_path, home)
    assert result["FRPC_LAUNCH_MODE"] == ("official", ".env.frpc-launch")
    assert result["FRPC_LAUNCH_CONFIG"] == ("/project/frpc.toml", ".env.local")
    assert "FRPC_LAUNCH_SAKURA_KEY" not in result
    assert resolve_layered({"FRPC_LAUNCH_MODE": "sakura"}, tmp_path, home)["FRPC_LAUNCH_MODE"] == ("sakura", "env")
    child = tmp_path / "child"
    child.mkdir()
    assert resolve_layered({}, child, home) == {}
