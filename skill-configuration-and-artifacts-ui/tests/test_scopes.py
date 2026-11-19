"""Behavioral cases for switching sources and preserving transaction boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_layers import LayeredDemo, TMP, UIError
from scope_requests import file_edit
from skill_ui.contracts import public_config


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.project = Path(self.tmp.name)
        self.store = LayeredDemo(self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def field(self, snapshot, name):
        return next(f for f in snapshot['fields'] if f['id'] == name)

    def write(self, target, values):
        self.store.path(target).write_text(json.dumps(values), encoding='utf-8')

    def request(self, name, target, value=None, mode='input'):
        files = {} if value is None else {'project_local': {name: {'op': 'set', 'value': value}}}
        return {'files': files, 'switches': {name: {'target': target, 'mode': mode}}}

    def preview(self, changes):
        return self.store.preview(changes, self.store.snapshot()['revision'])

    def save(self, changes):
        return self.store.save(changes, self.store.snapshot()['revision'])

    def test_unedited_secret_copy_and_existing_value_are_distinct(self):
        request = self.request('api_key', 'plugin_env')
        preview = self.preview(request)
        self.assertFalse(preview['scope_plans']['api_key']['conflict'])
        self.assertTrue(preview['scope_plans']['api_key']['writes_target'])
        self.assertNotIn('demo-project-key-0002', json.dumps(public_config(preview)))
        saved = self.save(request)
        self.assertEqual(self.field(saved, 'api_key')['effective']['value'], 'demo-project-key-0002')
        self.assertEqual(self.field(saved, 'api_key')['effective']['source'], 'plugin_env')
        self.assertNotIn('api_key', json.loads(self.store.path('project_local').read_text()))
        self.write('project_local', {'api_key': 'new-project'})
        saved = self.save(self.request('api_key', 'plugin_env', mode='existing'))
        self.assertEqual(self.field(saved, 'api_key')['effective']['value'], 'demo-project-key-0002')

    def test_same_value_switch_cleans_all_higher_keys_and_never_restores(self):
        self.write('project_skill', {'model': 'balanced-v1', 'unrelated': 'keep'})
        self.write('project_env', {'model': 'balanced-v1'})
        lower = self.store.path('skill_home')
        lower.write_text('{"model":"speed-v1"}', encoding='utf-8')
        saved = self.save(self.request('model', 'plugin_env'))
        self.assertEqual(self.field(saved, 'model')['effective']['source'], 'plugin_env')
        self.assertEqual(json.loads(self.store.path('project_skill').read_text()), {'unrelated': 'keep'})
        self.assertEqual(json.loads(self.store.path('project_env').read_text()), {})
        self.assertEqual(lower.read_text(), '{"model":"speed-v1"}')
        saved = self.save(self.request('model', 'project_skill', 'detail-v2'))
        self.assertEqual(self.field(saved, 'model')['effective']['value'], 'detail-v2')
        self.assertEqual(json.loads(self.store.path('plugin_env').read_text())['model'], 'balanced-v1')

    def test_choice_tokens_bind_input_target_and_revision(self):
        self.store.fields = deepcopy(self.store.fields)
        next(f for f in self.store.fields if f['id']=='model')['choices'].append('other-input')
        request = self.request('model', 'plugin_env', 'detail-v2')
        preview = self.preview(request)
        self.assertEqual(preview['scope_plans']['model']['state'], 'ready')  # already the current source
        self.write('project_local', {'model': 'speed-v1'})
        preview = self.preview(request)
        self.assertEqual(preview['scope_plans']['model']['state'], 'choice')
        intent = request['switches']['model']
        intent.update(choice='target', choice_token=preview['scope_plans']['model']['choice_token'])
        self.assertEqual(self.preview(request)['scope_plans']['model']['state'], 'ready')
        changed = deepcopy(request)
        changed['files']['project_local']['model']['value'] = 'other-input'
        self.assertEqual(self.preview(changed)['scope_plans']['model']['state'], 'choice')
        self.write('plugin_local', {'model': 'balanced-v1'})
        changed = deepcopy(request)
        changed['switches']['model']['target'] = 'plugin_local'
        self.assertEqual(self.preview(changed)['scope_plans']['model']['state'], 'choice')
        self.assertEqual(self.preview(request)['scope_plans']['model']['state'], 'choice')

    def test_process_readonly_unreadable_and_disabled_block_whole_switch(self):
        before = self.store.path('project_local').read_bytes()
        request = self.request('api_key', 'plugin_env')
        self.store.environment = {'api_key': 'process-key'}
        with self.assertRaises(UIError): self.save(request)
        self.assertEqual(before, self.store.path('project_local').read_bytes())
        self.store.environment = {}
        original = self.store.layers
        with patch.object(self.store, 'layers', side_effect=lambda: [dict(l, readonly=True) if l['id']=='project_local' else l for l in original()]):
            with self.assertRaises(UIError): self.save(request)
        self.store.path('project_skill').write_text('invalid', encoding='utf-8')
        with self.assertRaises(UIError): self.save(request)
        self.assertEqual(before, self.store.path('project_local').read_bytes())
        self.store.path('project_skill').unlink()
        self.store.global_enabled = False
        with self.assertRaises(UIError): self.save(request)

    def test_tracked_cleanup_target_blocks_before_any_config_write(self):
        subprocess.run(['git','init','-q',str(self.project)], check=True)
        relative = self.store.path('project_local').relative_to(self.project).as_posix()
        subprocess.run(['git','-C',str(self.project),'add','--',relative], check=True)
        before = {path: path.read_bytes() for path in self.store.directory.glob('*.json')}
        with self.assertRaises(UIError): self.save(self.request('api_key', 'plugin_env'))
        for path, raw in before.items(): self.assertEqual(raw, path.read_bytes())

    def test_false_zero_and_empty_json_array_survive_but_blank_falls_through(self):
        changes = {'files': {'plugin_env': {'retain': {'op':'set','value':False}, 'retries': {'op':'set','value':0}, 'priority': {'op':'set','value':[]}}}}
        saved = self.save(changes)
        for key, value in [('retain',False), ('retries',0), ('priority',[])]:
            field = self.field(saved, key)
            self.assertEqual(field['effective']['value'], value)
            self.assertFalse(field['effective']['empty'])
        preview = self.preview(self.request('timeout','project_local','   '))
        self.assertEqual(preview['scope_plans']['timeout']['state'], 'blocked')
        self.assertEqual(self.field(preview,'timeout')['effective']['source'], 'plugin_env')

    def test_subset_cannot_borrow_unsaved_dependency_and_batch_can(self):
        self.write('project_local', {'retain': False})
        subset = file_edit('project_local', {'days': {'op':'set','value':5}})
        with self.assertRaises(UIError): self.save(subset)
        together = {'files': {'project_local': {'days': {'op':'set','value':5}}, 'project_plugin_local': {'retain': {'op':'set','value':True}}}}
        saved = self.save(together)
        self.assertEqual(self.field(saved,'days')['effective']['value'], 5)
        self.assertTrue(self.field(saved,'days')['active'])

    def test_write_failure_and_interrupted_commit_restore_exact_bytes(self):
        request = self.request('api_key','plugin_env')
        before = {path: path.read_bytes() for path in self.store.directory.glob('*.json')}
        original = self.store.transaction.write
        for failure in (OSError('synthetic failure'), KeyboardInterrupt()):
            calls = 0
            def fail_second(path, raw):
                nonlocal calls
                calls += 1
                if calls == 2: raise failure
                original(path, raw)
            with patch.object(self.store.transaction, 'write', side_effect=fail_second):
                with self.assertRaises(UIError if isinstance(failure, Exception) else type(failure)): self.save(request)
            self.store.snapshot()  # recover an interrupted transaction before retry
            for path, raw in before.items(): self.assertEqual(raw, path.read_bytes())
            self.assertFalse(self.store.transaction.journal.exists())

    def test_legacy_request_rejected_and_normal_unchanged_edit_has_no_write(self):
        with self.assertRaises(UIError): self.preview({'model': {'op':'set','value':'detail-v2'}})
        before = self.store.path('plugin_env').read_bytes()
        saved = self.save({'files': {'project_local': {'model': {'op':'set','value':'balanced-v1'}}}})
        self.assertEqual(self.field(saved,'model')['effective']['source'], 'plugin_env')
        self.assertEqual(before, self.store.path('plugin_env').read_bytes())
