"""Pin entrypoints to the Skill's uv environment (ADR 0007)."""

import os
import shlex
import subprocess
import sys


REEXEC_ENV = "VIDEO_TO_MARKDOWN_BOOTSTRAP_REEXEC"
UV_INSTALL_HINT = (
    "winget install --exact --id astral-sh.uv" if os.name == "nt"
    else "curl -LsSf https://astral.sh/uv/install.sh | sh"
)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(
    SKILL_DIR, os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv"
)
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV_DIR, "bin", "python")


def _fail(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def _manual_rebuild_hint():
    quoted = "'" + SKILL_DIR.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(SKILL_DIR)
    return "uv sync --frozen --project %s --no-dev --reinstall" % quoted


def _venv_is_valid():
    return os.path.exists(VENV_PY) and os.path.exists(
        os.path.join(VENV_DIR, "pyvenv.cfg")
    )


def _dependencies_are_valid(*, report_error=False):
    modules = "from PIL import Image, ImageDraw, ImageOps"
    if os.name == "nt":
        modules += "; import win32api, win32con, win32file, win32security, ntsecuritycon, pywintypes, win32event, win32job, win32pipe, win32process"
    try:
        result = subprocess.run(
            [VENV_PY, "-c", modules], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if report_error:
            sys.stderr.write("Dependency import probe failed: %s\n" % exc)
        return False
    if result.returncode != 0 and report_error:
        sys.stderr.write(result.stdout.decode("utf-8", "replace"))
    return result.returncode == 0


def _require_uv():
    try:
        probe = subprocess.run(
            ["uv", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("uv is not installed. Run: " + UV_INSTALL_HINT)
    parts = probe.stdout.decode("utf-8", "replace").split()
    found = parts[1] if len(parts) > 1 else "0"
    try:
        numeric = tuple(int(x) for x in (found.split(".") + ["0", "0"])[:2])
    except ValueError:
        numeric = (0, 0)
    if probe.returncode != 0 or numeric < (0, 8):
        _fail("uv >= 0.8 is required (found %s). Run: uv self update" % found)


def _sync():
    sys.stderr.write(
        "[bootstrap] Runtime environment is missing or damaged; rebuilding from uv.lock at %s ...\n"
        % VENV_DIR
    )
    try:
        sync = subprocess.run(
            ["uv", "sync", "--frozen", "--project", SKILL_DIR, "--no-dev", "--reinstall"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        _fail("uv sync exceeded 600 seconds. Run: " + _manual_rebuild_hint())
    if sync.returncode != 0 or not _venv_is_valid():
        _fail(
            "uv sync failed (run %s):\n%s"
            % (_manual_rebuild_hint(), sync.stdout.decode("utf-8", "replace"))
        )


def ensure():
    target = os.path.realpath(VENV_DIR)
    runtime_ready = _venv_is_valid() and _dependencies_are_valid()
    if os.path.realpath(sys.prefix) == target and runtime_ready:
        if os.environ.get(REEXEC_ENV) == target:
            os.environ.pop(REEXEC_ENV, None)
        return
    if os.environ.get(REEXEC_ENV) == target:
        _fail(
            "Runtime is still unusable at %s after re-exec. Rebuild it with: %s"
            % (VENV_DIR, _manual_rebuild_hint())
        )
    if not runtime_ready:
        _require_uv()
        _sync()
        if not _dependencies_are_valid(report_error=True):
            _fail("Runtime dependencies still fail to import after repair. Run: " + _manual_rebuild_hint())
    os.environ[REEXEC_ENV] = target
    os.execv(VENV_PY, [VENV_PY] + sys.argv)
