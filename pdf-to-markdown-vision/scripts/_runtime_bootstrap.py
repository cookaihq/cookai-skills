"""One bounded repair of this Skill's frozen environment, before dependency imports."""
import os
from pathlib import Path
import re
import subprocess
import sys

MARKER = "PDF_VISION_BOOTSTRAP_TARGET"


def diagnostic(text):
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", text)
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text).strip()[-4000:]


def ensure():
    root = Path(__file__).resolve().parent.parent
    target = Path(os.environ.get("UV_PROJECT_ENVIRONMENT") or root / ".venv")
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = "import pypdfium2, PIL.Image, filelock, markdown_it, httpx; import sys; assert sys.version_info[:2] == (3, 13)"

    def probe_environment():
        if not python.is_file() or not (target / "pyvenv.cfg").is_file():
            return False, "Interpreter or pyvenv.cfg is missing at " + str(target)
        try:
            result = subprocess.run([str(python), "-c", probe], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
            return result.returncode == 0, diagnostic(result.stderr or result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, diagnostic(type(exc).__name__ + ": " + str(exc))

    ready, cause = probe_environment()
    if Path(sys.prefix).resolve() == target and ready:
        os.environ.pop(MARKER, None)
        return
    if os.environ.get(MARKER) == str(target):
        raise RuntimeError("Frozen runtime is unusable after one repair: " + str(target) + "\n" + cause)
    if not ready:
        try:
            result = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("uv >= 0.8 is required to repair " + str(target) + ": " + diagnostic(str(exc)) + "\n" + cause) from None
        match = re.search(r"\buv (\d+)\.(\d+)", result.stdout)
        if result.returncode or not match or tuple(map(int, match.groups())) < (0, 8):
            raise RuntimeError("uv >= 0.8 is required: " + diagnostic(result.stderr or result.stdout))
        print("[bootstrap] Restoring the Skill environment from uv.lock", file=sys.stderr)
        result = subprocess.run(["uv", "sync", "--frozen", "--project", str(root), "--no-dev", "--reinstall"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode:
            raise RuntimeError("Frozen runtime repair failed at " + str(target) + "\n" + diagnostic(result.stderr or result.stdout) + "\nInitial probe: " + cause)
        ready, cause = probe_environment()
        if not ready:
            raise RuntimeError("Dependency import failed after frozen repair: " + str(target) + "\n" + cause)
    os.environ[MARKER] = str(target)
    os.execv(str(python), [str(python), *sys.argv])
