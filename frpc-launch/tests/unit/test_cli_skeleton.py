import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).parents[2] / "scripts" / "frpc_launch.py")


def run_cli(*args):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True)


def test_help_lists_subcommands():
    r = run_cli("--help")
    assert r.returncode == 0
    for sub in ["start", "stop", "status", "logs", "install", "update", "guide-init"]:
        assert sub in r.stdout


def test_unimplemented_subcommand_exits_2():
    r = run_cli("status")
    assert r.returncode == 2
