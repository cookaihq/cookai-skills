"""One bounded repair of this Skill's frozen environment, before dependency imports."""
import os
from pathlib import Path
import subprocess
import sys

MARKER = "PDF_VISION_BOOTSTRAP_TARGET"


def ensure():
    root = Path(__file__).resolve().parent.parent
    target = Path(os.environ.get("UV_PROJECT_ENVIRONMENT") or root / ".venv")
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = "import pypdfium2, PIL.Image, filelock, markdown_it, httpx; import sys; assert sys.version_info[:2] == (3, 13)"

    def valid():
        if not python.is_file() or not (target / "pyvenv.cfg").is_file():
            return False
        try:
            return subprocess.run([str(python), "-c", probe], capture_output=True, timeout=30).returncode == 0
        except Exception:
            return False

    ready = valid()
    if Path(sys.prefix).resolve() == target and ready:
        os.environ.pop(MARKER, None)
        return
    if os.environ.get(MARKER) == str(target):
        raise RuntimeError("Frozen runtime is unusable after one repair: " + str(target))
    if not ready:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=15)
        version = tuple(int(p) for p in result.stdout.split()[1].split(".")[:2])
        if result.returncode or version < (0, 8):
            raise RuntimeError("uv >= 0.8 is required")
        print("[bootstrap] Restoring the Skill environment from uv.lock", file=sys.stderr)
        subprocess.run(["uv", "sync", "--frozen", "--project", str(root), "--no-dev", "--reinstall"],
                       check=True, timeout=300, stdout=sys.stderr)
        if not valid():
            raise RuntimeError("Dependency import failed after frozen repair: " + str(target))
    os.environ[MARKER] = str(target)
    os.execv(str(python), [str(python), *sys.argv])
