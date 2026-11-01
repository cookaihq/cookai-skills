"""Real Win32 lifecycle/ACL tests using a network-free, locally compiled EXE."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

import pytest

import frpc_launch as f
from project_config import runtime_home
from test_project_lifecycle import setup  # shared project fixture and CLI

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows API acceptance")


def test_windows_zip_install_probes_binary_and_records_hashes(tmp_path, native_fake):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("frp_0.70.1_windows_amd64/frpc.exe", native_fake.read_bytes())
        z.writestr("../unrelated.txt", "must not extract")
    body = archive.getvalue()
    name = "frp_0.70.1_windows_amd64.zip"
    release = {"tag_name": "v0.70.1", "assets": [
        {"name": name, "browser_download_url": "https://example.test/" + name},
        {"name": f.CHECKSUMS_NAME, "browser_download_url": "https://example.test/checksums.txt"}]}
    meta = f.install_official(tmp_path / "home", "0.70.1", fetch_json=lambda u: release,
        fetch_bytes=lambda u: (hashlib.sha256(body).hexdigest() + "  " + name).encode() if u.endswith(".txt") else body)
    installed = tmp_path / "home/bin/windows/frpc.exe"
    assert installed.read_bytes() == native_fake.read_bytes()
    assert meta["archive_sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["sha256"] == f.sha256_file(installed)
    assert meta["reported_version"] == "synthetic-1.0"
    assert not (tmp_path / "home/run/unrelated.txt").exists()


def _pids(run, project):
    log = run(project, "logs")[1]["modes"]["official"]
    return [int(value) for value in re.findall(r"(?:client|descendant)_pid=(\d+)", log)]


def _wait_dead(pids):
    deadline = time.monotonic() + 5
    while any(f.pid_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert all(not f.pid_alive(pid) for pid in pids)


def test_job_owns_descendants_and_has_no_console(setup):
    defaults, (a, _), _, _, run = setup
    (a / ".env.local").write_text("FRPC_LAUNCH_SERVER_ADDR=descendant.example.test\n"
        "FRPC_LAUNCH_SERVER_PORT=7000\nFRPC_LAUNCH_TOKEN=job-synthetic-token\n")
    assert run(a, "start", "--wait", "5")[0] == 0
    pids = _pids(run, a)
    assert len(pids) == 2
    assert "[fixture] console=0" in run(a, "logs")[1]["modes"]["official"]
    assert run(a, "stop")[0] == 0
    _wait_dead(pids)


def test_crashed_supervisor_cannot_leave_a_client(setup):
    defaults, (a, _), _, _, run = setup
    assert run(a, "start", "--wait", "5")[0] == 0
    pids = _pids(run, a)
    record = f.read_pid_record(runtime_home(a, defaults) / "run/official.pid")
    f.windows.terminate(record)
    _wait_dead(pids)
    assert run(a, "status")[1]["modes"]["official"]["running"] is False
    assert run(a, "start", "--wait", "5")[0] == 0


def test_launch_gate_eof_starts_no_client(tmp_path, native_fake):
    log = tmp_path / "gate.log"
    argv = [str(Path(sys._base_executable).resolve()), str(f.Path(f.__file__).with_name("project_runner.py")),
            "--instance", str(tmp_path), "--mode", "sakura", "--binary", str(native_fake), "--log", str(log)]
    proc = f.spawn_daemon(argv, dict(os.environ), log, supervised=True)
    proc.stdin.close()
    assert proc.wait(timeout=5) == 1
    assert not log.exists() or "client_pid=" not in log.read_text()


def test_verify_does_not_start_and_bad_exe_is_actionable(setup, tmp_path):
    defaults, (a, _), env, command, run = setup
    code, result = run(a, "verify")
    assert code == 0 and result["verification_scope"] == "configuration"
    assert not (runtime_home(a, defaults) / "run/official.pid").exists()
    invalid = tmp_path / "invalid.exe"
    invalid.write_bytes(b"not a PE executable")
    call = subprocess.run(command(a, "start"), env=dict(env, FRPC_LAUNCH_FRPC=str(invalid)),
                          capture_output=True, text=True, timeout=10)
    assert call.returncode == 1 and "Traceback" not in call.stderr
    assert not (runtime_home(a, defaults) / "run/official.pid").exists()


def test_sakura_native_lifecycle_uses_its_own_executable(setup, native_fake):
    defaults, (a, _), env, command, run = setup
    env.update(FRPC_LAUNCH_SAKURA_FRPC=str(native_fake), FRPC_LAUNCH_SAKURA_KEY="sakura-synthetic-key",
               FRPC_LAUNCH_SAKURA_TUNNELS="1234")
    try:
        code, result = run(a, "start", "--mode", "sakura", "--wait", "5")
        assert code == 0 and result["mode"] == "sakura"
        assert "sakura-synthetic-key" not in json.dumps(run(a, "logs")[1])
        assert run(a, "status")[1]["modes"]["sakura"]["running"] is True
    finally:
        assert run(a, "stop", "--mode", "sakura")[0] == 0


def test_private_windows_acl_is_protected_and_limited(setup):
    defaults, (a, _), _, _, run = setup
    assert run(a, "start", "--wait", "5")[0] == 0
    target = runtime_home(a, defaults) / "run/official.pid"
    # Query the actual security descriptor. PowerShell 5's Get-Acl cannot read
    # this deliberately long Unicode/space-containing test path reliably.
    import ctypes as c
    from ctypes import wintypes as w
    read = f.windows._api(f.windows.advapi, "GetFileSecurityW",
                         [w.LPCWSTR, w.DWORD, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)])
    stringify = f.windows._api(f.windows.advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                              [w.LPVOID, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), w.LPVOID])
    size = w.DWORD()
    read(str(target), 4, None, 0, c.byref(size))
    buffer = c.create_string_buffer(size.value)
    assert read(str(target), 4, buffer, len(buffer), c.byref(size))
    result = w.LPWSTR()
    assert stringify(buffer, 1, 4, c.byref(result), None)
    try:
        sddl = result.value
        assert sddl.startswith("D:P")
        assert set(re.findall(r";;;([^)]*)\)", sddl)) == {f.windows.user_sid(), "SY", "BA"}
    finally:
        f.windows.LocalFree(c.cast(result, w.LPVOID))


def test_wrong_birth_cannot_authorize_termination(setup):
    defaults, (a, _), _, _, run = setup
    started = run(a, "start", "--wait", "5")[1]
    record = f.read_pid_record(runtime_home(a, defaults) / "run/official.pid")
    with pytest.raises(OSError, match="身份"):
        f.windows.terminate(dict(record, process_start="win:wrong"))
    assert f.pid_alive(started["pid"])


def test_sakura_status_does_not_require_official_connection(setup, native_fake):
    defaults, (a, _), env, _, run = setup
    env.update(FRPC_LAUNCH_SAKURA_FRPC=str(native_fake), FRPC_LAUNCH_SAKURA_KEY="sakura-synthetic-key",
               FRPC_LAUNCH_SAKURA_TUNNELS="1234")
    (defaults / ".env").write_text("")
    assert run(a, "start", "--mode", "sakura", "--wait", "5")[0] == 0
    code, status = run(a, "status", "--mode", "sakura")
    assert code == 0 and "configuration_error" not in status
    assert status["modes"]["sakura"]["config_changed"] is False
    env["FRPC_LAUNCH_SAKURA_TUNNELS"] = "5678"
    assert run(a, "status", "--mode", "sakura")[1]["modes"]["sakura"]["config_changed"] is True


def test_log_rotation_redacts_both_files(tmp_path, monkeypatch):
    import project_runner
    monkeypatch.setattr(project_runner, "LOG_LIMIT", 1)
    path = tmp_path / "client.log"
    log = project_runner.PrivateLog(path, ["synthetic-private-token"])
    try:
        log.write("first synthetic-private-token\n")
        log.write("second synthetic-private-token\n")
    finally:
        log.close()
    for item in (path, path.with_suffix(".log.1")):
        text = item.read_text()
        assert "synthetic-private-token" not in text and "synt****oken" in text


def test_acl_denial_names_the_affected_path(tmp_path, monkeypatch):
    from runtime_files import protect
    def denied(_):
        raise PermissionError(13, "Access is denied", None, 5)
    monkeypatch.setattr(f.windows, "private_acl", denied)
    with pytest.raises(OSError, match="私有 ACL") as exc:
        protect(tmp_path)
    assert exc.value.filename == str(tmp_path) and exc.value.winerror == 5
