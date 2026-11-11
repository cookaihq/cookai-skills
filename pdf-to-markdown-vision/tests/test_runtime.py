"""A host receives useful diagnostics even before business imports succeed."""
import os
import subprocess
import sys


def test_unusable_runtime_reports_dependency_cause_without_reexecution_loop(tmp_path, command):
    target = tmp_path / "incomplete-environment"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(target)], check=True, timeout=30)
    result = command("status", "--task", tmp_path, expect=2,
                     env=dict(os.environ, UV_PROJECT_ENVIRONMENT=str(target),
                              PDF_VISION_BOOTSTRAP_TARGET=str(target)))
    assert result["kind"] == "RuntimeError"
    assert "pypdfium2" in result["error"]
    assert "ModuleNotFoundError" in result["error"]
    assert str(target) in result["error"]
