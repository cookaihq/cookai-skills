"""Independent saves, dependency boundaries and scope confirmation through real stores."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager

from test_layers import LayeredDemo, TMP
from directories_adapter import DirectoryDemo
from skill_ui.batches import run
from skill_ui.storage import JsonConfig, file_lock


class Batches(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(temporary.cleanup)
        self.store = LayeredDemo(Path(temporary.name))
        self.store.config = self.store.snapshot

    def request(self, edits, target='project_local', store=None):
        store = store or self.store
        state = store.config()
        return state['revision'], {'files': {target: {key: {'op': 'set', 'value': value}
            for key, value in edits.items()}}, 'field_revisions': {
            f['id']: f['edit_revision'] for f in state['fields']}}

    def test_bad_independent_value_does_not_block_key_and_retains_disk_bytes(self):
        self.store.path('project_local').write_text('{"binding":{"identity":"incomplete"}}')
        revision, request = self.request({'api_key': 'new-synthetic', 'model': 'not-a-model'})
        result = run(self.store, request, revision, saving=True)
        self.assertEqual(result['field_results']['api_key']['status'], 'saved')
        self.assertEqual(result['field_results']['model']['status'], 'failed')
        self.assertEqual(result['save_status'], 'partial')
        self.assertEqual(json.loads(self.store.path('project_local').read_text()),
                         {'binding': {'identity': 'incomplete'}, 'api_key': 'new-synthetic'})

    def test_dependency_group_fails_together_but_independent_field_saves(self):
        revision, request = self.request({'days': 'bad-number', 'retain': False, 'format': 'webp'})
        result = run(self.store, request, revision, saving=True)
        self.assertEqual({k: v['status'] for k, v in result['field_results'].items()},
                         {'days': 'failed', 'retain': 'failed', 'format': 'saved'})
        self.assertEqual(json.loads(self.store.path('project_local').read_text()),
                         {'api_key': 'demo-project-key-0002', 'format': 'webp'})

    def test_only_conflicting_field_fails_and_unrelated_external_changes_are_preserved(self):
        revision, request = self.request({'format': 'webp', 'model': 'detail-v2'})
        self.store.path('project_local').write_text('{"format":"jpeg","retain":false,"unknown":99}')
        result = run(self.store, request, revision, saving=True)
        self.assertEqual(result['field_results']['format']['code'], 'conflict')
        self.assertEqual(result['field_results']['model']['status'], 'saved')
        self.assertEqual(json.loads(self.store.path('project_local').read_text()),
                         {'format': 'jpeg', 'retain': False, 'unknown': 99, 'model': 'detail-v2'})

    def test_scope_preview_and_confirmation_do_not_write_and_cleanup_is_one_group(self):
        revision, request = self.request({})
        request['switches'] = {'api_key': {'target': 'plugin_env', 'mode': 'input', 'choose_content': True}}
        before = {path: path.read_bytes() for path in self.store.directory.glob('*.json')}
        preview = run(self.store, request, revision)
        plan = preview['scope_plans']['api_key']
        self.assertTrue(plan['conflict'])
        self.assertEqual(plan['cleanup'], ['project_local'])
        self.assertEqual(run(self.store, request, revision, saving=True)['field_results']['api_key']['status'], 'failed')
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        request['switches']['api_key'].update(choice='input', choice_token=plan['choice_token'], cleanup_token=plan['cleanup_token'])
        self.assertEqual(run(self.store, request, revision)['field_results']['api_key']['status'], 'ready')
        self.assertEqual(self.store.path('project_local').read_bytes(), before[self.store.path('project_local')])
        saved = run(self.store, request, revision, saving=True)
        self.assertEqual(saved['field_results']['api_key']['status'], 'saved')
        self.assertNotIn('api_key', json.loads(self.store.path('project_local').read_text()))

    def test_lock_time_changes_merge_only_unrelated_fields(self):
        for kind in ('json', 'layers'):
            for related in (False, True):
                with self.subTest(kind=kind, related=related):
                    if kind == 'json':
                        path = self.store.directory / 'single.json'
                        store = JsonConfig(path, [{'id': 'format', 'type': 'text'}, {'id': 'model', 'type': 'text'}], owner_path=self.store.project)
                        store.config = store.snapshot
                        target, module = 'project', 'skill_ui.storage.file_lock'
                    else:
                        store, target, module = self.store, 'project_local', 'layers_adapter.file_lock'
                        path = store.path(target)
                    path.write_text('{"format":"png","model":"fast-v1","unknown":7}')
                    revision, request = self.request({'format': 'webp'}, target, store)
                    @contextmanager
                    def concurrent_write(*args, **kwargs):
                        with file_lock(*args, **kwargs):
                            data = json.loads(path.read_text())
                            data['format' if related else 'model'] = 'jpeg' if related else 'detail-v2'
                            path.write_text(json.dumps(data))
                            yield
                    with patch(module, concurrent_write):
                        result = run(store, request, revision, saving=True)
                    actual = json.loads(path.read_text())
                    self.assertEqual(result['field_results']['format']['status'], 'failed' if related else 'saved')
                    self.assertEqual(actual, {'format': 'jpeg' if related else 'webp', 'model': 'fast-v1' if related else 'detail-v2', 'unknown': 7})

    def test_preflight_exception_is_failed_but_unknown_write_stops_remaining_groups(self):
        revision, request = self.request({'format': 'webp', 'model': 'detail-v2'})
        with patch.object(self.store, 'preview', side_effect=OSError('secret omitted')):
            result = run(self.store, request, revision, saving=True)
        self.assertEqual(result['save_status'], 'failed')
        with patch.object(self.store, 'save', side_effect=OSError('secret omitted')) as save:
            result = run(self.store, request, revision, saving=True)
        self.assertEqual(result['save_status'], 'uncertain')
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result['field_results']['model']['code'], 'not_attempted')
        self.assertNotIn('secret omitted', json.dumps(result))

    def test_new_directory_preview_binds_unedited_effective_values(self):
        store = DirectoryDemo(self.store.project)
        old = store.anchors['global']
        old.parent.mkdir(parents=True)
        old.write_text('{"model":"original"}')
        new = store.directory / 'new'
        revision, request = self.request({'global_directory': str(new), 'api_key': 'new-synthetic'},
                                         store.layer_id(old), store)
        preview = run(store, request, revision)
        request['preview_tokens'] = preview['preview_tokens']
        new.mkdir()
        (new / 'config.json').write_text('{"model":"external"}')
        result = run(store, request, revision, saving=True)
        self.assertEqual(result['field_results']['api_key']['code'], 'conflict')
        self.assertEqual(old.read_text(), '{"model":"original"}')
        request['preview_tokens'] = run(store, request, revision)['preview_tokens']
        result = run(store, request, revision, saving=True)
        self.assertEqual(result['save_status'], 'saved')
        self.assertEqual(json.loads((new / 'config.json').read_text()), {'model': 'external', 'api_key': 'new-synthetic'})

    def test_directory_switch_never_rebuilds_corrupt_target(self):
        store = DirectoryDemo(self.store.project)
        new = store.directory / 'corrupt-new'
        new.mkdir(parents=True)
        broken = new / 'config.json'
        broken.write_text('{bad')
        anchor = store.anchors['global']
        for values in ({'global_directory': str(new)}, {'global_directory': str(new), 'api_key': 'synthetic'}):
            revision, request = self.request(values, store.layer_id(anchor), store)
            preview = run(store, request, revision)
            self.assertEqual(preview['save_status'], 'failed')
            result = run(store, request, revision, saving=True)
            self.assertTrue(all(row['status'] == 'failed' for row in result['field_results'].values()))
            self.assertEqual(broken.read_text(), '{bad')
            self.assertFalse(anchor.exists())
