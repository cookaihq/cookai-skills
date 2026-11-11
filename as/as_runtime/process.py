"""Execute argv directly; Windows npm shims must not interpret user text."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from skill_ui.contracts import UIError

OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def executable(name):
    path = shutil.which(name)
    if not path:
        raise UIError("missing_dependency", f"未找到 {name}。请先安装或修复 PATH。")
    path = Path(path).resolve()
    if os.name != "nt" or path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
        return [str(path)]
    # Resolve the installed package's real executable; never invoke a shell shim.
    package = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}.get(name)
    if package:
        root = path.parent / "node_modules" / package
        metadata = json.loads((root / "package.json").read_text(encoding="utf-8"))
        bin_path = metadata["bin"]
        entry = (root / (bin_path[name] if isinstance(bin_path, dict) else bin_path)).resolve()
        if entry.is_file() and entry.suffix.lower() == ".exe":
            return [str(entry)]
        if name == "codex":
            candidates = list((root / "node_modules/@openai").glob("codex-win32-*/vendor/*/bin/codex.exe"))
            if len(candidates) == 1:
                return [str(candidates[0])]
        if entry.is_file() and entry.suffix.lower() in (".js", ".mjs"):
            node = shutil.which("node")
            if node:
                return [node, str(entry)]
    raise UIError("unsupported_launcher", f"{name} 只发现 Shell 包装入口，无法安全直接执行；请提供原生可执行程序。")


def orca_executable():
    selected = os.environ.get("ORCA_CLI_COMMAND")
    if not selected:
        selected = "orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else (
            "orca-ide" if sys.platform.startswith("linux") and not os.environ.get("ORCA_TERMINAL_ID") else "orca")
    return executable(selected)


def run(argv, cwd, *, timeout=30, input=None):
    try:
        return subprocess.run(argv, cwd=cwd, input=input, text=True, encoding="utf-8",
                              errors="replace", capture_output=True, timeout=timeout, **OPTIONS)
    except FileNotFoundError:
        raise UIError("missing_dependency", "程序入口已失效，请重新检查安装。") from None


def json_command(argv, cwd, timeout=30):
    try:
        result = run(argv, cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UIError("discovery_timeout", "只读能力查询超时；未创建会话。") from None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise UIError("invalid_response", "程序未返回有效 JSON，请检查程序版本与运行状态。") from None
    if not isinstance(data, dict):
        raise UIError("invalid_response", "程序返回的 JSON 结构无效，请检查程序版本。")
    if result.returncode or data.get("ok") is False:
        error = data.get("error", {})
        message = error.get("message") if isinstance(error, dict) else None
        raise UIError("orca_unavailable", str(message or "Orca 查询失败，请确认 Orca 正在运行。"))
    payload = data.get("result", data)
    if not isinstance(payload, dict):
        raise UIError("invalid_response", "Orca 未返回有效对象。")
    return payload


class JsonLines:
    """Bounded read-only discovery protocol; never submits a user turn."""
    def __init__(self, argv, cwd, timeout=25):
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        errors="replace", **OPTIONS)
        self.lines = queue.Queue()
        def reader():
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.lines.put(value)
                except ValueError:
                    pass
            self.lines.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def receive(self, predicate):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise UIError("catalog_timeout", "模型目录查询超时。")
            try:
                value = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise UIError("catalog_timeout", "模型目录查询超时。") from None
            if value is None:
                raise UIError("catalog_unavailable", "Agent 未返回模型目录，请检查其登录与版本。")
            if predicate(value):
                return value

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.process.stdout.close()
