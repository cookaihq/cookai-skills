import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(os.environ.get("SKILL_UI_TEST_TMP", Path.cwd()/"tmp/skill-ui-tests")).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class BootstrapTests(unittest.TestCase):
    def test_missing_environment_rebuild_preserves_stdout_and_exit_code(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            environment = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(Path(directory)/"env"))
            first = subprocess.run([sys.executable,str(ROOT/"scripts/ui.py"),"--help"],env=environment,
                                   capture_output=True,text=True,timeout=45)
            self.assertEqual(first.returncode,0,first.stderr)
            self.assertIn("--adapter",first.stdout)
            self.assertTrue((Path(directory)/"env/pyvenv.cfg").exists())
            failure = subprocess.run([sys.executable,str(ROOT/"scripts/ui.py"),"unknown-command"],env=environment,
                                     capture_output=True,text=True,timeout=15)
            self.assertEqual(failure.returncode,2)
            self.assertIn("invalid choice",failure.stderr)

    def test_failed_reentry_does_not_loop_or_rebuild_again(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            expected=str((Path(directory)/"broken").resolve())
            environment=dict(os.environ,UV_PROJECT_ENVIRONMENT=expected,SKILL_CONFIGURATION_ARTIFACTS_UI_REEXEC=expected)
            failure=subprocess.run([sys.executable,str(ROOT/"scripts/ui.py"),"--help"],env=environment,
                                   capture_output=True,text=True,timeout=15)
            self.assertNotEqual(failure.returncode,0)
            self.assertIn("remains unusable after one repair",failure.stderr)
            self.assertFalse(Path(expected).exists())


if __name__ == '__main__':unittest.main()
