import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from frpc_launch import parse_proxy_spec, render_frpc_toml, update_env_file, FrpcLaunchError

SCRIPT = str(Path(__file__).parents[2] / "scripts" / "frpc_launch.py")


def test_parse_proxy_spec_http():
    p = parse_proxy_spec("name=web;type=http;localPort=8080;customDomains=demo.example.com")
    assert p["name"] == "web" and p["type"] == "http"


def test_parse_proxy_spec_tcp_requires_remoteport():
    with pytest.raises(FrpcLaunchError):
        parse_proxy_spec("name=ssh;type=tcp;localPort=22")


def test_render_frpc_toml():
    text = render_frpc_toml("devfrp.zhidalab.cn", 7005, "tok_1234567890", [
        parse_proxy_spec("name=web;type=http;localPort=8080;customDomains=demo.example.com")])
    assert 'serverAddr = "devfrp.zhidalab.cn"' in text
    assert "serverPort = 7005" in text
    assert 'auth.token = "tok_1234567890"' in text
    assert "[[proxies]]" in text and 'customDomains = ["demo.example.com"]' in text


def test_update_env_file_preserves_other_lines(tmp_path):
    f = tmp_path / ".env.local"
    f.write_text("OTHER_VAR=keep\nFRPC_LAUNCH_SAKURA_KEY=old\n")
    update_env_file(f, {"FRPC_LAUNCH_SAKURA_KEY": "newkey12345",
                        "FRPC_LAUNCH_SAKURA_TUNNELS": "7,8"})
    text = f.read_text()
    assert "OTHER_VAR=keep" in text
    assert "FRPC_LAUNCH_SAKURA_KEY=newkey12345" in text and "old" not in text
    assert "FRPC_LAUNCH_SAKURA_TUNNELS=7,8" in text
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def _run_guide(home, cwd, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRPC_LAUNCH_")}
    env.update(env_extra)
    return subprocess.run([sys.executable, SCRIPT, "--home", str(home), "--json",
                           "guide-init", *args],
                          capture_output=True, text=True, cwd=str(cwd), env=env)


def test_guide_init_global_official_writes_toml_0600(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    r = _run_guide(home, cwd, {"FRPC_LAUNCH_INIT_TOKEN": "tok_abcdefgh1234"},
                   "--scope", "global", "--source", "frps",
                   "--server-addr", "devfrp.zhidalab.cn", "--server-port", "7005",
                   "--proxy", "name=web;type=http;localPort=8080;customDomains=d.example.com")
    assert r.returncode == 0, r.stderr
    toml = home / "frpc.toml"
    assert 'auth.token = "tok_abcdefgh1234"' in toml.read_text()
    assert stat.S_IMODE(toml.stat().st_mode) == 0o600
    assert "tok_abcdefgh1234" not in r.stdout          # 回显掩码
    assert json.loads(r.stdout)["token"] == "tok_****1234"


def test_guide_init_project_refuses_unignored_secret_in_git(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
    r = _run_guide(home, cwd, {"FRPC_LAUNCH_SAKURA_KEY": "k_abcdefgh1234",
                               "FRPC_LAUNCH_SAKURA_TUNNELS": "7"},
                   "--scope", "project", "--source", "sakura")
    assert r.returncode == 6
    assert not (cwd / ".env.local").exists()
    (cwd / ".gitignore").write_text(".env.local\nfrpc.toml\n")
    r2 = _run_guide(home, cwd, {"FRPC_LAUNCH_SAKURA_KEY": "k_abcdefgh1234",
                                "FRPC_LAUNCH_SAKURA_TUNNELS": "7"},
                    "--scope", "project", "--source", "sakura")
    assert r2.returncode == 0, r2.stderr
    assert "FRPC_LAUNCH_SAKURA_KEY=k_abcdefgh1234" in (cwd / ".env.local").read_text()
