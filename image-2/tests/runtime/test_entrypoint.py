import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_task.sh"


class EntrypointTests(unittest.TestCase):
    def run_script(self, *arguments):
        environment = dict(os.environ)
        environment.pop("AIHUB_API_KEY", None)
        environment.pop("AIHUBMAX_BASE_URL", None)
        with tempfile.TemporaryDirectory() as cwd:
            return subprocess.run(
                [str(SCRIPT), *arguments],
                cwd=cwd,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_launcher_is_executable_and_shell_syntax_is_valid(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_json_argument_failure_is_exactly_one_handoff_document(self):
        for arguments in (
            ("--json",),
            ("--json", "--prompt", "cover", "--resolution", "1x1"),
            ("--json", "--prompt", "cover", "--unknown-option"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 1)
                document = json.loads(result.stdout)
                self.assertEqual(
                    set(document),
                    {"schema_version", "task_id", "status", "outputs", "error"},
                )
                self.assertEqual(document["error"]["code"], "invalid_arguments")
                self.assertEqual(result.stdout.count("\n"), 1)

    def test_help_remains_available_on_the_existing_entrypoint(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("create_task.sh", result.stdout)
        self.assertIn("--json", result.stdout)


if __name__ == "__main__":
    unittest.main()
