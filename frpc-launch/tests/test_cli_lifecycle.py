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

# 真实 frp v0.70.1 在 token 错误时输出 [W] connect to server error: ... 并在重试窗口内保持存活
# （E2E 实测；它从不打印 "login to server failed"）
FAKE_FRPC_LOGIN_FAIL = """#!/bin/sh
if [ "$1" = "verify" ]; then echo "syntax is ok"; exit 0; fi
echo "connect to server error: token in login doesn't match token from configuration"
sleep 60
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
    assert out["result"] == "failed"
    assert "connect to server error" in out["log_tail"]
    # 启动失败后不留下孤儿进程（实现应清理已拉起的进程）
    time.sleep(0.5)
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(out["pid"], 0)
    assert not (home / "run" / "official.pid").exists()


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


def test_logs_masks_single_quoted_token(tmp_path):
    # review M1：TOML 字面量字符串（单引号）里的 token 同样必须掩码
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    (home / "frpc.toml").write_text(
        "serverAddr = \"example.com\"\nserverPort = 7000\nauth.token = 'sqsupersecret99x'\n")
    (home / "run").mkdir(exist_ok=True)
    (home / "run" / "official.log").write_text("auth with sqsupersecret99x done\n")
    r = run_cli(home, cwd, "logs", "--mode", "official")
    assert "sqsupersecret99x" not in r.stdout


def test_unconfigured_start_exits_4(tmp_path):
    home = tmp_path / "empty_home"
    cwd = tmp_path / "proj2"
    cwd.mkdir()
    r = run_cli(home, cwd, "start")
    assert r.returncode == 4
    assert json.loads(r.stdout)["result"] == "unconfigured"


# review M2：不打印任何 marker 的 fake（模拟超时后进程仍存活）
FAKE_FRPC_SILENT = """#!/bin/sh
if [ "$1" = "verify" ]; then echo "syntax is ok"; exit 0; fi
echo "starting..."
sleep 60
"""


def test_timeout_then_second_start_not_reported_as_success(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_SILENT)
    try:
        r1 = run_cli(home, cwd, "start", "--wait", "2")
        assert r1.returncode == 1
        assert json.loads(r1.stdout)["result"] == "timeout"
        r2 = run_cli(home, cwd, "start", "--wait", "2")
        assert r2.returncode != 0
        out2 = json.loads(r2.stdout)
        assert out2["result"] == "running_unverified"
        st = json.loads(run_cli(home, cwd, "status").stdout)
        assert st["modes"]["official"]["verified"] is False
    finally:
        run_cli(home, cwd, "stop")


def test_explicit_mode_without_config_enters_guide(tmp_path):
    home = tmp_path / "empty_home"
    cwd = tmp_path / "proj3"
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRPC_LAUNCH_")}
    env["FRPC_LAUNCH_MODE"] = "official"
    r = subprocess.run([sys.executable, SCRIPT, "--home", str(home), "--json", "start"],
                       capture_output=True, text=True, cwd=str(cwd), env=env)
    assert r.returncode == 4
    assert json.loads(r.stdout)["result"] == "unconfigured"


def test_logs_json_output(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    (home / "run").mkdir(exist_ok=True)
    (home / "run" / "official.log").write_text("hello log\n")
    r = run_cli(home, cwd, "logs", "--mode", "official")
    out = json.loads(r.stdout)
    assert "hello log" in out["modes"]["official"]


def test_already_running_payload_schema(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    try:
        assert run_cli(home, cwd, "start", "--wait", "10").returncode == 0
        out = json.loads(run_cli(home, cwd, "start", "--wait", "10").stdout)
        assert out["result"] == "already_running"
        assert "log_tail" in out and "binary_version" in out
    finally:
        run_cli(home, cwd, "stop")


def test_explicit_binary_path_missing_is_friendly_error(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRPC_LAUNCH_")}
    env["FRPC_LAUNCH_FRPC"] = "/does/not/exist/frpc"
    r = subprocess.run([sys.executable, SCRIPT, "--home", str(home), "--json",
                        "start", "--wait", "5"],
                       capture_output=True, text=True, cwd=str(cwd), env=env)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "错误:" in r.stderr


def test_stop_dead_pid_message_says_exited(tmp_path):
    home, cwd = _setup(tmp_path, FAKE_FRPC_OK)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (home / "run").mkdir(exist_ok=True)
    (home / "run" / "official.pid").write_text(json.dumps({
        "pid": dead.pid, "exe": "/managed/bin/frpc",
        "mode": "official", "config_digest": "x", "started_at": "t"}))
    out = json.loads(run_cli(home, cwd, "stop").stdout)
    assert out["modes"]["official"]["result"] == "stale_pid_cleaned"
    assert "已退出" in out["modes"]["official"]["detail"]
