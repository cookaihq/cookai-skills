"""Thin HTTP/CLI continuation check; business cases live at Maintenance.action."""
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

SKILL = Path(__file__).resolve().parents[1]


class UITests(unittest.TestCase):
    def test_browser_and_agent_continue_one_task_after_service_restart(self):
        root = Path(os.environ['AGENT_RULES_TEST_TMP'])
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            base = Path(directory)
            project = base/'project'
            project.mkdir()
            a, b = project/'A.md', project/'B.md'
            a.write_bytes(b'A\n')
            b.write_bytes(b'B\n')
            command = [sys.executable, str(SKILL/'scripts/agent_rules.py'), '--project',str(project),
                       '--personal-dir',str(base/'personal')]
            def cli(*args, data=None):
                result = subprocess.run(command + list(args), input=json.dumps(data or {}),
                                        text=True, encoding='utf-8', capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                return json.loads(result.stdout)
            start = cli('ui','start')
            self.assertEqual(start['status'], 'running')
            try:
                def connect(started):
                    parsed = urlsplit(started['url'])
                    origin = parsed.scheme+'://'+parsed.netloc
                    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
                    def post(path, data, csrf=''):
                        request = Request(origin+path, data=json.dumps(data).encode(),
                            headers={'Content-Type':'application/json','Origin':origin,'X-CSRF-Token':csrf})
                        with opener.open(request, timeout=20) as response:
                            return json.load(response)
                    session = post('/api/session', {'token':parsed.fragment})
                    return post, session['csrf']
                post, csrf = connect(start)
                with self.assertRaises(HTTPError) as caught:
                    post('/api/rules', {'action':'task.create','data':{'kind':'sync','authority':str(a),'targets':[]}})
                self.assertEqual(caught.exception.code, 403)
                task = post('/api/rules', {'action':'task.create','data':{'kind':'sync','authority':str(a),
                                'targets':[{'path':str(b),'strategy':'merge'}]}}, csrf)['result']
                self.assertEqual(task['status'], 'waiting_agent')
                prompt = post('/api/rules', {'action':'task.prompt','data':{'task_id':task['id']}}, csrf)['result']
                self.assertIn(task['id'], prompt['prompt'])
                cli('call','task.result', data={'task_id':task['id'],'input_revision':task['input_revision'],'content':'A and B\n'})
                plan = post('/api/rules', {'action':'task.get','data':{'task_id':task['id']}}, csrf)['result']
                self.assertEqual(plan['status'], 'pending_confirmation')
                self.assertEqual(a.read_bytes(), b'A\n')
                post('/api/rules', {'action':'task.confirm','data':{'task_id':task['id'],'plan_revision':plan['plan_revision']}}, csrf)
                result = post('/api/rules', {'action':'task.apply','data':{'task_id':task['id']}}, csrf)['result']
                self.assertEqual(result['status'], 'completed')
                cli('ui','stop')
                restarted = cli('ui','start')
                self.assertNotEqual(start['instance'], restarted['instance'])
                post, csrf = connect(restarted)
                continued = post('/api/rules', {'action':'task.get','data':{'task_id':task['id']}}, csrf)['result']
                self.assertEqual(continued['status'], 'completed')
                self.assertEqual([a.read_bytes(), b.read_bytes()], [b'A and B\n']*2)
            finally:
                cli('ui','stop')


if __name__ == '__main__':
    unittest.main()
