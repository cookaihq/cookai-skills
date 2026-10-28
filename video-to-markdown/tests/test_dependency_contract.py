"""Real process boundary checks. Provider fixtures follow the fixed upstream contracts."""
import json
import os
from pathlib import Path
import sys
import time

from video_to_markdown import external, feishu


def test_external_argv_and_utf8_round_trip_without_shell_expansion(tmp_path):
    script = tmp_path / "arguments.py"
    script.write_text("import sys,json; print(json.dumps(sys.argv[1:], ensure_ascii=False))", encoding="utf-8")
    args = ['中文 path with spaces', '{"need_timestamp":true}', '$(never-run)', '`never-run`']
    result = external.execute([sys.executable, str(script), *args], cwd=tmp_path, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == args


def test_timeout_ends_dependency_children_before_they_write(tmp_path):
    marker = tmp_path / "should-not-exist"
    child = tmp_path / "child.py"
    child.write_text("import time; from pathlib import Path; time.sleep(1); Path(" + repr(str(marker)) + ").touch()", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text("import subprocess,sys,time; subprocess.Popen([sys.executable," + repr(str(child)) + "]); time.sleep(20)", encoding="utf-8")
    result = external.execute([sys.executable, str(parent)], cwd=tmp_path, timeout=.2)
    assert result.timed_out
    time.sleep(1.1)
    assert not marker.exists()


def test_lark_retry_after_contract_is_observed_for_safe_reads(tmp_path, monkeypatch):
    calls = []
    def execute(argv, **kwargs):
        calls.append(time.monotonic())
        if len(calls) < 3:
            return external.Result(1, "", json.dumps({"error": {"type": "api", "subtype": "rate_limit", "retryable": True, "retry_after_seconds": .02}}))
        return external.Result(0, '{"ok":true}')
    monkeypatch.setattr(external, "execute", execute)
    result = feishu.read(["lark-cli", "--profile", "work", "minutes", "+detail"], cwd=tmp_path, deadline=time.monotonic() + .5)
    assert result.returncode == 0 and len(calls) == 3
    assert calls[1] - calls[0] >= .02 and calls[2] - calls[1] >= .02


def test_read_budget_and_cooldown_survive_a_new_invocation(tmp_path, monkeypatch):
    calls = []
    def execute(argv, **kwargs):
        calls.append(kwargs["timeout"])
        return external.Result(1, "", '{"error":{"type":"api","subtype":"rate_limit","retryable":true,"retry_after_seconds":2}}')
    monkeypatch.setattr(external, "execute", execute)
    journal = {}
    argv = ["lark-cli", "--profile", "work", "minutes", "+detail"]
    began = time.monotonic()
    feishu.read(argv, cwd=tmp_path, deadline=began + .1, journal=journal)
    restored = json.loads(json.dumps(journal))
    feishu.read(argv, cwd=tmp_path, deadline=time.monotonic() + .1, journal=restored)
    assert len(calls) == 1 and 0 < calls[0] <= .1
    assert time.monotonic() - began < .5
    assert restored["retry_history"][0]["delay_seconds"] == 2
