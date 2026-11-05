"""Enter this developer Skill's pinned environment before importing implementation."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
MARKER = "SKILL_CONFIGURATION_ARTIFACTS_UI_REEXEC"


def ensure():
    environment = (ROOT / Path(os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")).resolve()
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    expected = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    probe = "import sys,ssl,json,http.server,hashlib; assert '.'.join(map(str,sys.version_info[:2])) == " + repr(expected)

    def ready():
        if not interpreter.is_file() or not (environment / "pyvenv.cfg").is_file():
            return False
        try:
            return subprocess.run([str(interpreter), "-c", probe], capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    valid = ready()
    if Path(sys.prefix).resolve() == environment and valid:
        os.environ.pop(MARKER, None)
        return
    if os.environ.get(MARKER) == str(environment):
        raise RuntimeError("Skill environment remains unusable after one repair")
    if not valid:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=15)
        match = re.search(r"uv (\d+)\.(\d+)", result.stdout)
        if result.returncode or not match or tuple(map(int, match.groups())) < (0, 8):
            raise RuntimeError("uv >= 0.8 is required; ask the Agent to prepare it")
        print("[runtime] Preparing the Skill environment from uv.lock", file=sys.stderr)
        subprocess.run(["uv", "sync", "--frozen", "--project", str(ROOT), "--no-dev"], check=True, timeout=180)
        if not ready():
            raise RuntimeError("Runtime repair failed: " + str(environment))
    os.environ[MARKER] = str(environment)
    if os.name == "nt":
        # Windows execv detaches from some host capture pipes; wait and propagate the real result.
        raise SystemExit(subprocess.run([str(interpreter)] + sys.argv).returncode)
    os.execv(str(interpreter), [str(interpreter)] + sys.argv)
