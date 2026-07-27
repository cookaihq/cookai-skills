import subprocess
import sys

from frpc_launch import (pid_paths, write_pid_record, read_pid_record,
                         pid_alive, pid_identity_ok, config_digest_official,
                         config_digest_sakura, read_log_tail)


def test_pid_record_roundtrip(tmp_path):
    pid_file, log_file = pid_paths(tmp_path, "official")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {"pid": 1234, "exe": "/x/frpc", "mode": "official",
           "config_digest": "d", "started_at": "t"}
    write_pid_record(pid_file, rec)
    assert read_pid_record(pid_file) == rec
    assert read_pid_record(tmp_path / "missing.pid") == {}


def test_pid_alive_and_identity(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid) is True
        good = {"pid": proc.pid, "exe": sys.executable}
        assert pid_identity_ok(good) is True
        # pid 存活但 exe 不匹配 → 身份核对不通过（模拟 pid 被无关进程复用）
        bad = {"pid": proc.pid, "exe": "/managed/bin/frpc"}
        assert pid_identity_ok(bad) is False
    finally:
        proc.kill()
        proc.wait()
    assert pid_alive(proc.pid) is False


def test_config_digest_changes_with_content(tmp_path):
    toml = tmp_path / "frpc.toml"
    toml.write_text('serverAddr = "a"\n')
    d1 = config_digest_official(toml, tmp_path / "frpc")
    toml.write_text('serverAddr = "b"\n')
    d2 = config_digest_official(toml, tmp_path / "frpc")
    assert d1 != d2
    assert config_digest_sakura("k1", "1,2", tmp_path / "s") != \
           config_digest_sakura("k1", "1,3", tmp_path / "s")


def test_read_log_tail(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("\n".join("line%d" % i for i in range(100)) + "\n")
    tail = read_log_tail(log, 3)
    assert tail.splitlines() == ["line97", "line98", "line99"]
    assert read_log_tail(tmp_path / "missing.log") == ""
    # review NIT：lines<=0 不得退化为输出整个文件
    assert read_log_tail(log, 0).splitlines() == ["line99"]
