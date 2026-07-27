import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = str(Path(__file__).parents[1] / "scripts" / "frpc_launch.py")

FAKE_FRPC_OK = """#!/bin/sh
if [ "$1" = "verify" ]; then echo "syntax is ok"; exit 0; fi
echo "login to server success, get run id abc"
sleep 60
"""

FAKE_FRPC_LOGIN_FAIL = """#!/bin/sh
if [ "$1" = "verify" ]; then echo "syntax is ok"; exit 0; fi
echo "login to server failed: token mismatch"
exit 1
"""


def _setup(tmp_path, fake_script):
    home = tmp_path / "home"
    for sub in ("bin/macos", "bin/linux", "bin/windows", "run"):
        (home / sub).mkdir(parents=True)
    import platform
    os_subdir = "macos" if platform.system() == "Darwin" else "linux"
    frpc = home / "bin" / os_subdir / "frpc"
    frpc.write_text(fake_script)
    frpc.chmod(0o755)
    (home / "frpc.toml").write_text('serverAddr = "example.com"\nserverPort = 7000\n')
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return home, cwd


def run_cli(home, cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRPC_LAUNCH_")}
    return subprocess.run([sys.executable, SCRIPT, "--home", str(home), "--json", *args],
                          capture_output=True, text=True, cwd=str(cwd), env=env)


def test_start_success_reports_global_source(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    r = run_cli(home, cwd, "start", "--wait", "10")
    out = json.loads(r.stdout)
    try:
        assert r.returncode == 0
        assert out["result"] == "ok" and out["config_source"] == "global"
        assert "login to server success" in out["log_tail"]
    finally:
        run_cli(home, cwd, "stop")


def test_start_login_failure_not_reported_as_success(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_LOGIN_FAIL)
    r = run_cli(home, cwd, "start", "--wait", "10")
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["result"] in ("failed", "exited")
    assert "login to server failed" in out["log_tail"]


def test_duplicate_start_protected(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    try:
        assert run_cli(home, cwd, "start", "--wait", "10").returncode == 0
        r2 = run_cli(home, cwd, "start", "--wait", "10")
        assert r2.returncode == 0
        assert json.loads(r2.stdout)["result"] == "already_running"
    finally:
        run_cli(home, cwd, "stop")


def test_config_change_requires_confirmation(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    try:
        assert run_cli(home, cwd, "start", "--wait", "10").returncode == 0
        (home / "frpc.toml").write_text('serverAddr = "changed.com"\nserverPort = 7000\n')
        r2 = run_cli(home, cwd, "start", "--wait", "10")
        assert r2.returncode == 5
        assert json.loads(r2.stdout)["result"] == "config_changed"
    finally:
        run_cli(home, cwd, "stop")


def test_stop_terminates_and_cleans(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    assert run_cli(home, cwd, "start", "--wait", "10").returncode == 0
    pid = json.loads(run_cli(home, cwd, "status").stdout)["modes"]["official"]["pid"]
    r = run_cli(home, cwd, "stop")
    assert r.returncode == 0
    assert not (home / "run" / "official.pid").exists()
    time.sleep(0.3)
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stop_stale_pid_does_not_kill_unrelated(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (home / "run").mkdir(exist_ok=True)
        (home / "run" / "official.pid").write_text(json.dumps({
            "pid": bystander.pid, "exe": "/managed/bin/frpc",
            "mode": "official", "config_digest": "x", "started_at": "t"}))
        r = run_cli(home, cwd, "stop")
        assert r.returncode == 0
        assert not (home / "run" / "official.pid").exists()
        assert bystander.poll() is None   # 无关进程仍存活，未被误杀
    finally:
        bystander.kill()
        bystander.wait()


def test_status_reports_not_running(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    out = json.loads(run_cli(home, cwd, "status").stdout)
    assert out["modes"]["official"]["running"] is False


def test_logs_masks_secrets(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    (home / "frpc.toml").write_text(
        'serverAddr = "example.com"\nserverPort = 7000\nauth.token = "supersecrettoken99"\n')
    (home / "run").mkdir(exist_ok=True)
    (home / "run" / "official.log").write_text("auth with supersecrettoken99 done\n")
    r = run_cli(home, cwd, "logs", "--mode", "official")
    assert "supersecrettoken99" not in r.stdout
    assert "supe****en99" in r.stdout


def test_unconfigured_start_exits_4(tmp_path):
    home = tmp_path / "empty_home"
    cwd = tmp_path / "proj2"
    cwd.mkdir()
    r = run_cli(home, cwd, "start")
    assert r.returncode == 4
    assert json.loads(r.stdout)["result"] == "unconfigured"
