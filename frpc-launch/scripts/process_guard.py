"""Stdlib process-incarnation check shared by the launcher and its runner."""
import os
from pathlib import Path
import subprocess


def process_started(pid):
    if not isinstance(pid, int) or pid <= 0:
        return ""
    if os.name == "nt":
        from windows_runtime import snapshot
        return snapshot(pid, command=False).get("process_start", "")
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return "" if fields[0] == "Z" else "proc:" + fields[19]
    except (OSError, IndexError):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, timeout=5)
        return "ps:" + result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else ""


def owner_matches(pid, started):
    try:
        return bool(started) and process_started(pid) == started
    except (OSError, subprocess.SubprocessError):
        return False
