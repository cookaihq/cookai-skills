"""Exercise the actual detached process boundary, including immediate restart."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(os.environ.get("SKILL_UI_TEST_TMP", Path.cwd() / "tmp" / "skill-ui-tests")).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class ProcessTests(unittest.TestCase):
    def test_each_opening_has_its_own_context_on_the_same_project_service(self):
        with tempfile.TemporaryDirectory(dir=TMP) as temporary:
            project = Path(temporary)
            command = [sys.executable, str(ROOT / "scripts/ui.py")]
            arguments = ["--adapter", str(ROOT / "examples/studio_adapter.py"),
                         "--project", str(project), "--page", "configuration", "--runtime-dir", str(project / "runtime")]
            def cli(action, *context):
                result = subprocess.run(command + [action] + arguments + list(context), capture_output=True,
                                        text=True, encoding="utf-8", timeout=25)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            try:
                first = cli("start", "--fields", "api_key", "--target", "project")
                second = cli("start", "--fields", "format,retain")
                self.assertEqual(first["instance"], second["instance"])
                self.assertEqual(Path(second["project"]), project)
                self.assertEqual(parse_qs(urlsplit(first["url"]).query), {"fields": ["api_key"], "target": ["project"]})
                self.assertEqual(parse_qs(urlsplit(second["url"]).query), {"fields": ["format,retain"]})
                self.assertEqual(urlsplit(first["url"]).fragment, urlsplit(second["url"]).fragment)
                self.assertFalse((project / "demo-state/settings.json").exists())
            finally:
                cli("stop")

    def test_stop_finishes_cleanup_before_immediate_restart(self):
        with tempfile.TemporaryDirectory(dir=TMP) as temporary:
            project = Path(temporary)
            command = [sys.executable, str(ROOT / "scripts/ui.py")]
            arguments = ["--adapter", str(ROOT / "examples/studio_adapter.py"),
                         "--project", str(project), "--page", "artifacts",
                         "--runtime-dir", str(project / "runtime")]

            def cli(action):
                completed = subprocess.run(command + [action] + arguments, capture_output=True,
                                           text=True, encoding="utf-8", timeout=25)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                return json.loads(completed.stdout)

            instances = set()
            try:
                for _ in range(6):
                    started = cli("start")
                    self.assertEqual(started["status"], "running")
                    self.assertNotIn(started["instance"], instances)
                    instances.add(started["instance"])
                    directory = Path(started["runtime"])
                    self.assertEqual(cli("stop")["status"], "stopped")
                    self.assertFalse((directory / "server.json").exists())
                    log = (directory / "service.log").read_text(encoding="utf-8")
                    self.assertIn("service_stopped page=artifacts instance=" + started["instance"], log)
                self.assertEqual(cli("status")["status"], "stopped")
                self.assertEqual((directory / "launcher.log").read_text(encoding="utf-8"), "")
            finally:
                cli("stop")


if __name__ == "__main__":
    unittest.main()
