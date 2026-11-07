import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_unusable_runtime_reports_sanitized_probe_cause_without_repair_loop(self):
        root = Path(os.environ['AGENT_RULES_TEST_TMP'])
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            base = Path(directory)
            (base/'ssl.py').write_text("raise OSError(5, 'dependency fixture unavailable; token=fixture-secret')\n", encoding='utf-8')
            environment = dict(os.environ, PYTHONPATH=str(base),
                               UV_PROJECT_ENVIRONMENT=str(Path(sys.prefix).resolve()),
                               AGENT_INSTRUCTIONS_UPDATE_REEXEC=str(Path(sys.prefix).resolve()))
            result = subprocess.run([sys.executable, str(SKILL/'scripts/agent_rules.py'), '--help'],
                                    env=environment, capture_output=True, encoding='utf-8', timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('preflight', result.stderr)
            self.assertIn('exit=1', result.stderr)
            self.assertIn('OSError', result.stderr)
            self.assertIn('dependency fixture unavailable', result.stderr)
            self.assertNotIn('fixture-secret', result.stderr)
            self.assertNotIn('Preparing', result.stderr)

    def test_installed_entry_reads_and_resumes_the_same_task_across_processes(self):
        root = Path(os.environ['AGENT_RULES_TEST_TMP'])
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            base = Path(directory)
            project = base / 'project with spaces'
            project.mkdir()
            path = project / 'AGENTS.md'
            path.write_bytes(b'Before\n')
            def invoke(action, data):
                result = subprocess.run([sys.executable, str(SKILL/'scripts/agent_rules.py'),
                    '--project', str(project), '--personal-dir', str(base/'personal'), 'call', action],
                    input=json.dumps(data), capture_output=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout)
            task = invoke('task.create', {'kind':'edit','changes':[{'path':str(path),'content':'After\n'}]})
            invoke('task.confirm', {'task_id':task['id'],'plan_revision':task['plan_revision']})
            self.assertEqual(invoke('task.apply', {'task_id':task['id']})['status'], 'completed')
            self.assertEqual(invoke('task.get', {'task_id':task['id']})['status'], 'completed')
            self.assertEqual(path.read_bytes(), b'After\n')


if __name__ == '__main__':
    unittest.main()
