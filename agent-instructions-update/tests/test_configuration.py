import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
from rules import Maintenance, RuleError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ['AGENT_RULES_TEST_TMP'])
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.personal = self.base / 'personal'

    def test_layered_settings_are_readonly_and_binding_does_not_fill_from_another_layer(self):
        self.personal.mkdir()
        scope = str(self.project.resolve())
        binding = {'scope': 'project', 'project': scope, 'product': 'codex', 'installation': None,
                   'profile': 'default', 'paths': ['AGENTS.md'], 'authority': 'AGENTS.md', 'layout': 'existing'}
        (self.personal / '.env').write_text('AGENT_RULES_HISTORY_KEEP=7\nAGENT_RULES_BINDINGS=' + json.dumps({scope: binding}), encoding='utf-8')
        (self.project / '.env.local').write_text('AGENT_RULES_HISTORY_KEEP=9\nUNRELATED=unchanged\n', encoding='utf-8')
        (self.project / '.env.agent-instructions-update').write_text('AGENT_RULES_HISTORY_KEEP=11\n', encoding='utf-8')
        app = Maintenance(self.personal, self.project, environment={'AGENT_RULES_HISTORY_KEEP': '13'})
        settings = app.action('config.read', {})
        self.assertEqual(settings['values']['history_keep'], 13)
        self.assertEqual(settings['sources']['history_keep'], 'process')
        self.assertEqual(settings['values']['binding']['profile'], 'default')
        self.assertFalse((self.personal / 'settings.json').exists())
        partial = {scope: {'product': 'hermes'}}
        (self.project / '.env.agent-instructions-update').write_text('AGENT_RULES_BINDINGS=' + json.dumps(partial), encoding='utf-8')
        with self.assertRaises(RuleError) as caught:
            app.action('config.read', {})
        self.assertEqual(caught.exception.code, 'binding_incomplete')
        (self.project / '.env.agent-instructions-update').unlink()
        clean = Maintenance(self.personal, self.project, environment={})
        before = clean.action('config.read', {})
        saved = clean.action('config.save', {'save_scope': 'project', 'revision': before['revision'], 'values': {'history_keep': 2}})
        self.assertEqual(saved['values']['history_keep'], 2)
        self.assertIn('UNRELATED=unchanged', (self.project / '.env.local').read_text())
        self.assertIn('AGENT_RULES_HISTORY_KEEP=7', (self.personal / '.env').read_text())
        other_project = self.base / 'other'
        other_project.mkdir()
        other = Maintenance(self.personal, other_project, environment={}).action('config.read', {})
        self.assertIsNone(other['values']['binding'])
        self.assertEqual(other['values']['history_keep'], 7)
        disabled = Maintenance(self.personal, other_project, environment={}, use_personal=False).action('config.read', {})
        self.assertEqual(disabled['values']['history_keep'], 30)
        self.assertIsNone(disabled['values']['binding'])

    def test_one_personal_save_updates_preferences_and_update_protocol_without_project_leakage(self):
        app = Maintenance(self.personal, self.project, environment={})
        before = app.action('config.read', {'scope':'global'})
        saved = app.action('config.save', {'scope':'global', 'revision':before['revision'], 'save_scope':'personal',
                                         'values':{'history_keep':12, 'auto_update':False}})
        self.assertEqual(saved['values']['history_keep'], 12)
        self.assertFalse(saved['values']['auto_update'])
        self.assertIn('AUTO_UPDATE_CHECK=0', (self.personal/'.env').read_text())
        self.assertFalse((self.project/'.env.local').exists())
        with self.assertRaises(RuleError):
            app.action('config.save', {'scope':'global', 'revision':saved['revision'], 'save_scope':'project', 'values':{'auto_update':True}})


if __name__ == '__main__':
    unittest.main()
