"""Resolve the Codex command once, without invoking a Windows command shell."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    source: str


class LaunchError(ValueError):
    pass


def _executable(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _candidates(name: str, cwd: Path, env: dict[str, str]):
    suffixes = (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if os.name == "nt" else ("",)
    for entry in env.get("PATH", "").split(os.pathsep):
        entry = entry.strip('"')
        if not entry:
            continue
        for suffix in suffixes:
            candidate = cwd / entry / (name + suffix)
            if _executable(candidate):
                yield candidate


def _argv(path: Path, cwd: Path, env: dict[str, str]) -> tuple[str, ...] | None:
    if not _executable(path):
        return None
    if os.name != "nt" or path.suffix.lower() in {".exe", ".com"}:
        return (str(path),)
    if path.name.lower() not in {"codex.cmd", "codex.bat", "codex.ps1"}:
        return None
    launcher = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if _executable(launcher):
        adjacent = path.with_name("node.exe")
        node = adjacent if _executable(adjacent) else next(
            (item for item in _candidates("node", cwd, env) if item.suffix.lower() == ".exe"), None)
        if node:
            return (str(node), str(launcher))
    return None


_WINDOWS_ANCESTORS = r'''
$ErrorActionPreference = 'Stop'
$cursor = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
for ($depth = -1; $depth -lt 12 -and $null -ne $cursor; $depth++) {
  if ($depth -ge 0) {
    $line = @{ path = $cursor.ExecutablePath; name = $cursor.Name; command = $cursor.CommandLine } | ConvertTo-Json -Compress
    [Console]::WriteLine($line)
  }
  if ($cursor.ParentProcessId -le 0) { break }
  $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($cursor.ParentProcessId)"
  if ($null -eq $parent -or $parent.CreationDate -gt $cursor.CreationDate) { break }
  $cursor = $parent
}
'''
_WRAPPER = re.compile(r"(node|pwsh|powershell|cmd|conhost|bash|sh|zsh|dash|fish|env|uvx?|python[0-9.]*)(?:\.exe)?$", re.I)
_OTHER_AGENT = re.compile(r"(?:^|[\s\x00/\\'\"])(?:claude(?:-code)?|workbuddy|opencode|gemini|copilot)(?:[\s\x00/\\.'\"]|$)", re.I)


def _ancestors(env: dict[str, str]):
    if os.name == "nt":
        root = Path(env.get("SYSTEMROOT") or env.get("SystemRoot") or "")
        if not root.is_absolute():
            return
        powershell = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        output = subprocess.check_output([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                                         "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + _WINDOWS_ANCESTORS],
                                         timeout=4, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        for line in output.decode("utf-8").splitlines()[1:]:
            yield json.loads(line)
        return
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return
        executable: str
        command: str
        if sys.platform == "linux":
            proc = Path("/proc") / str(pid)
            executable = os.readlink(proc / "exe")
            parent = re.search(r"^PPid:\s+(\d+)", (proc / "status").read_text(), re.M)
            command = (proc / "cmdline").read_bytes().decode(errors="replace")
            if not parent:
                return
            pid = int(parent[1])
        elif sys.platform == "darwin":
            output = subprocess.check_output(["/bin/ps", "-ww", "-p", str(pid), "-o", "ppid=", "-o", "comm="],
                                             timeout=0.5, stderr=subprocess.DEVNULL, text=True)
            match = re.fullmatch(r"\s*(\d+)\s+(.+?)\s*", output)
            if not match:
                return
            pid, executable, command = int(match[1]), match[2], ""
        else:
            return
        yield {"path": executable, "name": Path(executable).name, "command": command}


def probe(launch: Launch) -> None:
    try:
        result = subprocess.run([*launch.argv, "exec", "--help"], timeout=8,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError("the selected Codex executable did not pass its local CLI probe") from exc
    required = (b"--ignore-user-config", b"--ignore-rules", b"--output-last-message", b"--config", b"--json")
    if result.returncode or any(flag not in result.stdout for flag in required):
        raise LaunchError("the selected Codex CLI does not expose the required exec options")


def current_codex(env: dict[str, str], ancestors=_ancestors) -> Path | None:
    if not env.get("CODEX_THREAD_ID") or env.get("CLAUDECODE") == "1" or env.get("WORKBUDDY_SESSION_ID"):
        return None
    try:
        for ancestor in ancestors(env):
            name = (ancestor.get("name") or "").lower()
            path = Path(ancestor.get("path") or "")
            if not path.is_absolute():
                break
            if name == ("codex.exe" if os.name == "nt" else "codex"):
                return path if path.name.lower() == name and _executable(path) else None
            if not _WRAPPER.fullmatch(name) or _OTHER_AGENT.search(ancestor.get("command") or ""):
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def resolve(cwd: Path, *, explicit: str | None = None, explicit_source: str | None = None,
            host: str | None = None, environ: dict[str, str] | None = None, find_current=current_codex) -> Launch:
    env = dict(os.environ) if environ is None else environ
    if explicit:
        command = _argv(cwd / explicit, cwd, env)
        if command:
            return Launch(command, explicit_source or "explicit configuration")
        raise LaunchError("VIDEO_TO_MARKDOWN_CODEX_EXECUTABLE is unavailable or is an unsupported launcher")
    if host in {None, "codex"}:
        current = find_current(env)
        command = _argv(current, cwd, env) if current else None
        if command:
            return Launch(command, "current_codex_session")
    for candidate in _candidates("codex", cwd, env):
        command = _argv(candidate, cwd, env)
        if command:
            return Launch(command, "PATH")
    raise LaunchError("Codex CLI is unavailable; install a native executable or the official npm launcher")
