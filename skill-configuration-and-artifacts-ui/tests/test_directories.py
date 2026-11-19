"""Observable two-stage load/save behavior with only project-local synthetic data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'assets/runtime'), str(ROOT / 'examples')]
from directories_adapter import DirectoryDemo
from skill_ui.contracts import UIError, public_config
from skill_ui.transactions import JsonTransaction

TMP = Path(os.environ.get('SKILL_UI_TEST_TMP', Path.cwd() / 'tmp' / 'skill-ui-tests')).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class Directories(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temporary.cleanup)
        self.store = DirectoryDemo(Path(self.temporary.name))
        self.old = self.store.anchors['global']
        self.old.parent.mkdir(parents=True)
        self.old.write_text(json.dumps({'api_key': 'old-synthetic', 'model': 'old-model'}), encoding='utf-8')
        self.new = self.store.directory / 'new global'

    def request(self):
        return {'files': {self.store.layer_id(self.old): {
            'global_directory': {'op': 'set', 'value': str(self.new)},
            'api_key': {'op': 'set', 'value': 'new-synthetic'}}}}

    def field(self, snapshot, name):
        return next(f for f in snapshot['fields'] if f['id'] == name)

    def approve(self, request, revision):
        request.pop('directory_revision', None)
        preview = self.store.preview(request, revision)
        request['directory_revision'] = preview['directory_revision']
        return preview

    def test_mixed_save_writes_new_root_without_copying_and_resets_to_default(self):
        initial = self.store.config()
        request = self.request()
        preview = self.approve(request, initial['revision'])
        self.assertFalse(self.new.exists())
        self.assertNotIn('new-synthetic', json.dumps(public_config(preview)))
        self.assertEqual(public_config(preview)['directory_targets']['api_key'], str(self.new / 'config.json'))
        saved = self.store.save(request, initial['revision'])
        self.assertEqual(json.loads((self.new / 'config.json').read_text(encoding='utf-8')), {'api_key': 'new-synthetic'})
        self.assertEqual(self.field(saved, 'model')['effective']['value'], 'demo-model')
        self.assertEqual(saved['revision'], self.store.config()['revision'])
        reset = {'files': {self.store.layer_id(self.old): {'global_directory': {'op': 'unset'}}}}
        self.approve(reset, saved['revision'])
        restored = self.store.save(reset, saved['revision'])
        self.assertEqual(self.field(restored, 'api_key')['effective']['value'], 'old-synthetic')
        self.assertTrue((self.new / 'config.json').exists())

    def test_setting_file_follows_root_and_unchanged_fields_are_not_copied(self):
        state = self.store.config()
        settings = self.old.parent / 'settings.json'
        request = self.request()
        request['files'][self.store.layer_id(settings)] = {'favorite': {'op': 'set', 'value': 'one'}}
        request['files'][self.store.layer_id(self.old)]['model'] = {'op': 'set', 'value': 'old-model'}
        self.approve(request, state['revision'])
        saved = self.store.save(request, state['revision'])
        self.assertNotIn('model', json.loads((self.new / 'config.json').read_text(encoding='utf-8')))
        self.assertEqual(self.field(saved, 'favorite')['effective']['value'], 'one')
        self.assertEqual(json.loads((self.new / 'settings.json').read_text(encoding='utf-8')), {'favorite': 'one'})
        self.assertFalse(settings.exists())

    def test_anchor_conflict_and_failed_transaction_preserve_old_files(self):
        state = self.store.config()
        before = self.old.read_bytes()
        self.old.write_text('{}', encoding='utf-8')
        with self.assertRaises(UIError):
            self.store.save(self.request(), state['revision'])
        self.old.write_bytes(before)
        original_write = JsonTransaction.write
        def fail_new(instance, path, raw):
            if Path(path) == self.new / 'config.json' and raw is not None:
                raise OSError('synthetic write failure')
            return original_write(instance, path, raw)
        request = self.request()
        self.approve(request, self.store.config()['revision'])
        with patch.object(JsonTransaction, 'write', fail_new), self.assertRaises(UIError):
            self.store.save(request, self.store.config()['revision'])
        self.assertEqual(self.old.read_bytes(), before)
        self.assertFalse((self.new / 'config.json').exists())

    def test_new_root_changes_after_preview_require_a_new_preview(self):
        state = self.store.config()
        request = self.request()
        before = self.old.read_bytes()
        with self.assertRaises(UIError) as missing:
            self.store.save(request, state['revision'])
        self.assertEqual(missing.exception.code, 'conflict')
        self.approve(request, state['revision'])
        self.new.mkdir()
        (self.new / 'settings.json').write_text('{"favorite":"external"}', encoding='utf-8')
        self.assertEqual(state['revision'], self.store.config()['revision'])
        with self.assertRaises(UIError) as changed:
            self.store.save(request, state['revision'])
        self.assertEqual(changed.exception.code, 'conflict')
        self.assertEqual(self.old.read_bytes(), before)
        self.assertFalse((self.new / 'config.json').exists())
        self.approve(request, state['revision'])
        saved = self.store.save(request, state['revision'])
        self.assertEqual(self.field(saved, 'favorite')['effective']['value'], 'external')

    def test_directory_and_unedited_switch_keep_the_original_input_baseline(self):
        self.new.mkdir()
        target = self.new / 'config.json'
        target.write_text('{"api_key":"candidate-synthetic"}', encoding='utf-8')
        for mode, expected in [('existing', 'candidate-synthetic'), ('input', 'old-synthetic')]:
            with self.subTest(mode=mode):
                state = self.store.config()
                request = {'files': {self.store.layer_id(self.old): {
                    'global_directory': {'op': 'set', 'value': str(self.new)}}},
                    'switches': {'api_key': {'target': self.store.layer_id(self.old), 'mode': mode}}}
                preview = self.approve(request, state['revision'])
                self.assertEqual(preview['validation_errors'], [])
                self.assertEqual(self.field(preview, 'api_key')['effective']['value'], expected)
                self.assertEqual(preview['scope_plans']['api_key']['input_changed'], False)

        state = self.store.config()
        request['switches']['api_key']['mode'] = 'input'
        request['files'][self.store.layer_id(self.old)]['api_key'] = {'op': 'set', 'value': 'edited-synthetic'}
        preview = self.approve(request, state['revision'])
        plan = preview['scope_plans']['api_key']
        self.assertEqual(plan['state'], 'choice')
        request['switches']['api_key'].update(choice='input', choice_token=plan['choice_token'])
        saved = self.store.save(request, state['revision'])
        self.assertEqual(self.field(saved, 'api_key')['effective']['value'], 'edited-synthetic')

    def test_candidate_source_change_immediately_before_commit_preserves_external_data(self):
        state, request = self.store.config(), self.request()
        self.approve(request, state['revision'])
        before = self.old.read_bytes()
        commit = JsonTransaction.commit
        def external_edit(instance, updates, verify, **options):
            self.new.mkdir(exist_ok=True)
            (self.new / 'config.json').write_text('{"model":"external-model"}', encoding='utf-8')
            return commit(instance, updates, verify, **options)
        with patch.object(JsonTransaction, 'commit', external_edit), self.assertRaises(UIError) as error:
            self.store.save(request, state['revision'])
        self.assertEqual(error.exception.code, 'conflict')
        self.assertEqual(self.old.read_bytes(), before)
        self.assertEqual(json.loads((self.new / 'config.json').read_text(encoding='utf-8')), {'model': 'external-model'})

    def test_traversal_and_linked_paths_are_rejected_before_any_file_read(self):
        state = self.store.config()
        outside = self.store.project / 'outside'
        outside.mkdir()
        (outside / 'config.json').write_text('{"api_key":"outside-synthetic"}', encoding='utf-8')
        read_text = Path.read_text
        def no_external_read(path, *args, **options):
            if path.resolve().is_relative_to(outside):
                raise AssertionError('demo attempted an external file read')
            return read_text(path, *args, **options)
        request = self.request()
        change = request['files'][self.store.layer_id(self.old)]['global_directory']
        change['value'] = str(self.store.directory / '..' / 'outside')
        with patch.object(Path, 'read_text', no_external_read):
            self.assertTrue(self.store.preview(request, state['revision'])['validation_errors'])
        link = self.store.directory / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('directory symlinks unavailable on this host')
        change['value'] = str(link / 'nested')
        with patch.object(Path, 'read_text', no_external_read):
            self.assertTrue(self.store.preview(request, state['revision'])['validation_errors'])
        self.old.unlink()
        self.old.symlink_to(outside / 'config.json')
        with patch.object(Path, 'read_text', no_external_read):
            self.assertTrue(self.store.config()['validation_errors'])

    def test_invalid_directory_and_malformed_envelope_never_write(self):
        state = self.store.config()
        request = self.request()
        request['files'][self.store.layer_id(self.old)]['global_directory']['value'] = '../outside'
        with self.assertRaises(UIError):
            self.store.save(request, state['revision'])
        for value in ([], {'files': {}, 'switches': []}, {'files': {}, 'switches': {'api_key': None}}):
            with self.assertRaises(UIError):
                self.store.preview(value, self.store.config()['revision'])
        self.assertFalse(self.new.exists())


if __name__ == '__main__':
    unittest.main()
