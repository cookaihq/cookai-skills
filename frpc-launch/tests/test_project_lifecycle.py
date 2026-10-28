import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from project_config import project_id, runtime_home

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Native Windows process management is not implemented.")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "frpc_launch.py"

FAKE = '''#!{python}
import os, sys, time, tomllib
if 'verify' in sys.argv:
    print('syntax is ok')
    raise SystemExit(0)
config = tomllib.loads(os.environ['FRPC_LAUNCH_RUNTIME_CONFIG'])
print('token=' + config['auth']['token'], flush=True)
if config['serverAddr'] == 'failure.example.test':
    print('connect to server error: synthetic rejection', flush=True)
elif config['serverAddr'] != 'silent.example.test':
    if config['serverAddr'] == 'delayed.example.test':
        time.sleep(2)
    print('login to server success', flush=True)
    for proxy in config['proxies']:
        print('[' + proxy['name'] + '] start proxy success', flush=True)
time.sleep(90)
'''


@pytest.fixture
def setup(tmp_path):
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    binary = tmp_path / "fake frpc"
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(0o755)
    (defaults / ".env").write_text('FRPC_LAUNCH_SERVER_ADDR=127.0.0.1\nFRPC_LAUNCH_SERVER_PORT=7000\n'
                                    'FRPC_LAUNCH_TOKEN=synthetic-shared-token\nFRPC_LAUNCH_DOMAIN_SUFFIX=dev.example.test\n')
    projects = [tmp_path / "a" / "My App", tmp_path / "b" / "My App"]
    for project in projects:
        project.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRPC_LAUNCH_")}
    env["FRPC_LAUNCH_FRPC"] = str(binary)
    def command(project, *args):
        return [sys.executable, str(SCRIPT), "--project", str(project), "--home", str(defaults), "--json", *args]
    def run(project, *args):
        result = subprocess.run(command(project, *args), env=env, capture_output=True, text=True, timeout=25)
        assert result.stdout, result.stderr
        return result.returncode, json.loads(result.stdout)
    for project in projects:
        assert run(project, "project-config", "--local-port", "3000", "--route-mode", "customDomains")[0] == 0
    yield defaults, projects, env, command, run
    for project in projects:
        run(project, "stop")


def test_two_projects_start_concurrently_and_stop_independently(setup):
    defaults, (a, b), env, command, run = setup
    calls = [subprocess.Popen(command(p, "start", "--wait", "5"), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for p in (a, b)]
    outputs = []
    for call in calls:
        stdout, stderr = call.communicate(timeout=15)
        assert call.returncode == 0, stderr + stdout
        outputs.append(json.loads(stdout))
    assert outputs[0]["pid"] != outputs[1]["pid"]
    assert outputs[0]["domain"] != outputs[1]["domain"]
    assert outputs[0]["proxies"][0]["name"] != outputs[1]["proxies"][0]["name"]
    for project in (a, b):
        runtime = runtime_home(project, defaults)
        log = (runtime / "run/official.log").read_text()
        assert "synthetic-shared-token" not in log
        assert "synt****oken" in log
        assert "synthetic-shared-token" not in (runtime / "run/official.toml").read_text()
    assert run(a, "stop")[0] == 0
    assert run(a, "status")[1]["modes"]["official"]["running"] is False
    assert run(b, "status")[1]["modes"]["official"]["running"] is True


def test_concurrent_start_of_same_project_creates_one_process(setup):
    _, (a, _), env, command, run = setup
    calls = [subprocess.Popen(command(a, "start", "--wait", "5"), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for call in calls:
        stdout, stderr = call.communicate(timeout=15)
        assert call.returncode == 0, stderr + stdout
        results.append(json.loads(stdout))
    assert {r["result"] for r in results} == {"ok", "already_running"}
    assert len({r["pid"] for r in results}) == 1


def test_changed_config_preserves_live_process_until_explicit_stop(setup):
    _, (a, b), _, _, run = setup
    first = run(a, "start", "--wait", "5")[1]
    assert run(b, "start", "--wait", "5")[0] == 0
    assert run(a, "project-config", "--local-port", "4000")[0] == 0
    code, result = run(a, "start", "--wait", "5")
    assert code == 5 and result["pid"] == first["pid"]
    assert run(a, "status")[1]["modes"]["official"]["config_changed"] is True
    assert run(a, "stop")[0] == 0
    replacement = run(a, "start", "--wait", "5")[1]
    assert replacement["result"] == "ok" and replacement["pid"] != first["pid"]
    assert run(b, "status")[1]["modes"]["official"]["running"] is True


def test_other_project_pid_record_cannot_authorize_a_signal(setup):
    defaults, (a, b), _, _, run = setup
    assert run(a, "start", "--wait", "5")[0] == 0
    assert run(b, "start", "--wait", "5")[0] == 0
    a_file = runtime_home(a, defaults) / "run/official.pid"
    b_file = runtime_home(b, defaults) / "run/official.pid"
    original = a_file.read_bytes()
    try:
        a_file.write_bytes(b_file.read_bytes())
        result = run(a, "stop")[1]
        assert result["modes"]["official"]["result"] == "stale_pid_cleaned"
        assert run(b, "status")[1]["modes"]["official"]["running"] is True
    finally:
        a_file.write_bytes(original)


def test_start_failure_cleans_its_own_process_and_preserves_other_project(setup):
    defaults, (a, b), _, _, run = setup
    assert run(b, "start", "--wait", "5")[0] == 0
    (a / ".env.local").write_text('FRPC_LAUNCH_SERVER_ADDR=failure.example.test\nFRPC_LAUNCH_SERVER_PORT=7000\n'
                                  'FRPC_LAUNCH_TOKEN=failure-synthetic-token\n')
    code, result = run(a, "start", "--wait", "5")
    assert code == 1 and result["result"] == "failed"
    assert not (runtime_home(a, defaults) / "run/official.pid").exists()
    assert run(b, "status")[1]["modes"]["official"]["running"] is True


def test_interrupted_start_can_be_checked_and_stopped_next_invocation(setup):
    defaults, (a, _), env, command, run = setup
    (a / ".env.local").write_text('FRPC_LAUNCH_SERVER_ADDR=delayed.example.test\nFRPC_LAUNCH_SERVER_PORT=7000\n'
                                  'FRPC_LAUNCH_TOKEN=delayed-synthetic-token\n')
    call = subprocess.Popen(command(a, "start", "--wait", "10"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    record = runtime_home(a, defaults) / "run/official.pid"
    deadline = time.monotonic() + 5
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert record.exists()
    call.terminate()
    call.communicate(timeout=5)
    assert run(a, "status")[1]["modes"]["official"]["running"] is True
    time.sleep(2.2)
    code, resumed = run(a, "start", "--wait", "5")
    assert code == 0 and resumed["result"] == "already_running"
    assert run(a, "stop")[0] == 0


def test_timeout_and_broken_current_config_do_not_prevent_stop(setup):
    defaults, (a, _), _, _, run = setup
    (a / ".env.local").write_text('FRPC_LAUNCH_SERVER_ADDR=silent.example.test\nFRPC_LAUNCH_SERVER_PORT=7000\n'
                                  'FRPC_LAUNCH_TOKEN=silent-synthetic-token\n')
    assert run(a, "start", "--wait", "0.2")[0] == 1
    status = run(a, "status")[1]["modes"]["official"]
    assert status["running"] is True and status["verified"] is False
    (a / ".env.local").write_text('FRPC_LAUNCH_SERVER_ADDR=partial.example.test\n')
    status = run(a, "status")[1]
    assert status["configuration_error"]["result"] == "incomplete_connection"
    assert status["modes"]["official"]["running"] is True
    assert "silent-synthetic-token" not in json.dumps(run(a, "logs")[1])
    assert run(a, "stop")[0] == 0


def test_wrong_process_birth_is_not_trusted(setup):
    defaults, (a, _), _, _, run = setup
    assert run(a, "start", "--wait", "5")[0] == 0
    file = runtime_home(a, defaults) / "run/official.pid"
    original = file.read_bytes()
    record = json.loads(original)
    record["process_start"] = "proc:wrong"
    try:
        file.write_text(json.dumps(record))
        assert run(a, "status")[1]["modes"]["official"]["running"] is False
    finally:
        file.write_bytes(original)


def test_unreadable_configuration_still_reports_and_stops_owned_process(setup):
    _, (a, _), _, _, run = setup
    assert run(a, "start", "--wait", "5")[0] == 0
    (a / ".env.local").write_bytes(b"\xffinvalid-utf8")
    status = run(a, "status")[1]
    assert status["configuration_error"]["result"] == "invalid_config"
    assert status["modes"]["official"]["running"] is True
    assert run(a, "stop")[0] == 0


def test_corrupt_record_prevents_duplicate_start_and_does_not_kill(setup):
    defaults, (a, _), env, command, run = setup
    first = run(a, "start", "--wait", "5")[1]
    file = runtime_home(a, defaults) / "run/official.pid"
    original = file.read_bytes()
    try:
        file.write_text("{invalid")
        call = subprocess.run(command(a, "start", "--wait", "1"), env=env,
                              capture_output=True, text=True, timeout=10)
        assert call.returncode == 1
        os.kill(first["pid"], 0)
        assert file.read_text() == "{invalid"
    finally:
        file.write_bytes(original)
