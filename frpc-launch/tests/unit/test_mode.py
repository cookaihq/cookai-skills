import pytest

from frpc_launch import decide_mode, resolve_layered, FrpcLaunchError


def _layered(tmp_path, official=False, sakura=False, mode=""):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if official:
        (home / "frpc.toml").write_text('serverAddr = "example.com"\n')
    env = {}
    if sakura:
        env["FRPC_LAUNCH_SAKURA_KEY"] = "k1234567890"
        env["FRPC_LAUNCH_SAKURA_TUNNELS"] = "11"
    if mode:
        env["FRPC_LAUNCH_MODE"] = mode
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    return resolve_layered(env, cwd, home), home


def test_explicit_mode_wins(tmp_path):
    layered, home = _layered(tmp_path, official=True, sakura=True, mode="sakura")
    d = decide_mode(layered, home)
    assert d.decision == "explicit" and d.modes == ["sakura"]


def test_single_mode(tmp_path):
    layered, home = _layered(tmp_path, official=True)
    d = decide_mode(layered, home)
    assert d.decision == "single" and d.modes == ["official"]


def test_ambiguous_when_both(tmp_path):
    layered, home = _layered(tmp_path, official=True, sakura=True)
    d = decide_mode(layered, home)
    assert d.decision == "ambiguous" and sorted(d.modes) == ["official", "sakura"]


def test_none_when_unconfigured(tmp_path):
    layered, home = _layered(tmp_path)
    assert decide_mode(layered, home).decision == "none"


def test_invalid_mode_value_raises(tmp_path):
    layered, home = _layered(tmp_path, official=True, mode="bogus")
    with pytest.raises(FrpcLaunchError):
        decide_mode(layered, home)


def test_cli_mode_flag_overrides(tmp_path):
    layered, home = _layered(tmp_path, official=True, sakura=True)
    d = decide_mode(layered, home, requested_mode="official")
    assert d.decision == "explicit" and d.modes == ["official"]
