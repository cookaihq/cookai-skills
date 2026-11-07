"""Enter the pinned Skill environment before any business imports (Python 3.9+)."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
MARKER = 'AGENT_INSTRUCTIONS_UPDATE_REEXEC'


def safe_reason(value):
    # Keep the final diagnostic, not traceback source lines or command output.
    text = str(value).strip().splitlines()[-1] if str(value).strip() else 'no diagnostic output'
    for key, secret in os.environ.items():
        if len(secret) >= 4 and re.search(r'token|secret|password|credential|api.?key', key, re.I):
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:token|secret|password|credential|api[_-]?key)\s*[:=]\s*)\S+', r'\1[redacted]', text)
    text = re.sub(r'https?://\S+', '[url redacted]', text)
    return text[:800]


def run(stage, arguments, timeout):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except Exception as exc:
        raise RuntimeError(stage + ': ' + type(exc).__name__ + ': ' + safe_reason(exc)) from None
    if result.returncode:
        raise RuntimeError(stage + ': exit=' + str(result.returncode) + ': ' + safe_reason(result.stderr or result.stdout)) from None
    return result


def ensure():
    environment = (ROOT / Path(os.environ.get('UV_PROJECT_ENVIRONMENT') or '.venv')).resolve()
    interpreter = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    expected = (ROOT / '.python-version').read_text(encoding='utf-8').strip()
    probe = ('import sys,ssl,json,http.server,hashlib,tomllib; '
             "assert '.'.join(map(str,sys.version_info[:3])) == " + repr(expected))

    def ready():
        if not interpreter.is_file() or not (environment/'pyvenv.cfg').is_file():
            return 'preflight: environment interpreter or pyvenv.cfg is missing'
        try:
            run('preflight', [str(interpreter), '-c', probe], 15)
            return None
        except Exception as exc:
            return safe_reason(exc)

    failure = ready()
    if Path(sys.prefix).resolve() == environment and failure is None:
        os.environ.pop(MARKER, None)
        return
    if os.environ.get(MARKER) == str(environment):
        raise RuntimeError('Skill environment remains unusable after one repair: ' + (failure or 'interpreter did not enter the expected environment')) from None
    if failure:
        version = run('uv version check', ['uv', '--version'], 15)
        match = re.search(r'uv (\d+)\.(\d+)', version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < (0, 8):
            raise RuntimeError('uv >= 0.8 is required; ask the Agent to prepare it')
        print('[runtime] Preparing the Skill environment from uv.lock; ' + failure, file=sys.stderr)
        run('runtime repair', ['uv', 'sync', '--frozen', '--project', str(ROOT), '--no-dev'], 180)
        failure = ready()
        if failure:
            raise RuntimeError('Runtime repair failed: ' + failure) from None
    os.environ[MARKER] = str(environment)
    if os.name == 'nt':
        raise SystemExit(subprocess.run([str(interpreter)] + sys.argv).returncode)
    os.execv(str(interpreter), [str(interpreter)] + sys.argv)
