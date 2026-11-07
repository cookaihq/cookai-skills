"""Python 3.9-compatible entry guard; only the skill's locked environment runs work."""
import os
from pathlib import Path
import re
import subprocess
import sys


def ensure_runtime():
    root = Path(__file__).resolve().parents[1]
    target = (root / (os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")).resolve()
    executable = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = "HMA_BOOTSTRAP_REEXEC"
    attempted = os.environ.get(marker) == str(target)
    valid = executable.is_file() and (target / "pyvenv.cfg").is_file()
    in_environment = Path(sys.prefix).resolve() == target
    failure = ""
    if valid and in_environment:
        try:
            import psutil
            import aiohttp
            from aiohttp import web
            psutil.Process().create_time()
            if not callable(aiohttp.ClientSession) or not callable(web.Application):
                raise RuntimeError("http dependency probe failed")
            os.environ.pop(marker, None)
            return
        except Exception as exc:
            failure = type(exc).__name__ + ": dependency import/probe failed"
    if attempted:
        raise RuntimeError("固定运行环境仍不可用：" + (failure or str(target)))
    if not valid or failure:
        try:
            probe = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=10)
            match = re.search(r"uv (\d+)\.(\d+)", probe.stdout)
            if probe.returncode or not match or tuple(map(int, match.groups())) < (0, 8):
                raise RuntimeError("需要 uv >= 0.8。")
            print("[bootstrap] 正在按 uv.lock 恢复本 Skill 运行环境。", file=sys.stderr)
            command = ["uv", "sync", "--frozen", "--project", str(root), "--no-dev"]
            if failure:
                command += ["--reinstall-package", "psutil", "--reinstall-package", "aiohttp"]
            result = subprocess.run(command, capture_output=True, timeout=180)
            if result.returncode or not executable.is_file() or not (target / "pyvenv.cfg").is_file():
                raise RuntimeError("冻结环境恢复失败；请检查 uv 的缓存、下载连接与文件权限。")
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("运行环境恢复失败：" + type(exc).__name__) from None
    os.environ[marker] = str(target)
    os.execv(str(executable), [str(executable)] + sys.argv)
