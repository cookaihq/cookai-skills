"""Behavior at the shared maintenance-task boundary, using real isolated files."""
import os
import json
from pathlib import Path
import sys
import tempfile
import stat
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
from rules import Maintenance, RuleError


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(os.environ['AGENT_RULES_TEST_TMP']).resolve()
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.app = Maintenance(self.base / 'personal', self.project)

    def file(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8'))
        return str(path)

    def call(self, action, **data):
        return self.app.action(action, data)

    def test_workspace_from_subdirectory_finds_project_and_named_nested_scenarios(self):
        main = self.file('AGENTS.md', '# Project\n\nRead [Commands](docs/agents/dev/commands.md) before commands.\n')
        scenario = self.file('docs/agents/dev/commands.md', '```sh\n# example\n```\n\n# Command execution\n\nCheck results.\n')
        self.file('docs/agents/collaboration/handoff.md', '# Handoff\n')
        self.file('notes.md', '# Not a scenario\n')
        child = self.project / 'src'
        child.mkdir()
        app = Maintenance(self.base/'personal', child, environment={'HOME':str(self.base/'home'), 'USERPROFILE':str(self.base/'home'), 'PATH':''})
        view = app.action('workspace.read', {'scope':'project'})
        self.assertEqual(view['project']['path'], str(self.project))
        self.assertEqual(view['project']['availability'], 'ready')
        self.assertEqual({f['relative_path']:f['title'] for f in view['scenarios']},
                         {'dev/commands.md':'Command execution', 'collaboration/handoff.md':'Handoff'})
        self.assertEqual(next(f for f in view['scenarios'] if f['path']==scenario)['content'], self.call('file.read', path=scenario)['content'])
        self.assertIn(main, [f['path'] for f in view['main_files']])

    def test_global_workspace_keeps_project_scenarios_separate_and_does_not_create_rules(self):
        home = self.base/'home'
        library = home/'.agents'
        (library/'scenarios-agent-instructions/tools').mkdir(parents=True)
        (library/'AGENTS.md').write_text('# Global\n', encoding='utf-8')
        rule = library/'scenarios-agent-instructions/tools/commands.md'
        rule.write_text('# Global commands\n', encoding='utf-8')
        self.file('AGENTS.md', '# Project\n')
        self.file('docs/agents/commands.md', '# Project commands\n')
        app = Maintenance(self.base/'personal', self.project, environment={'HOME':str(home), 'USERPROFILE':str(home), 'PATH':''})
        view = app.action('workspace.read', {'scope':'global'})
        self.assertEqual(view['scope'], 'global')
        self.assertEqual({x['title'] for x in view['scenarios']}, {'Global commands'})
        self.assertEqual(view['scenarios'][0]['relative_path'], 'tools/commands.md')
        self.assertFalse((self.base/'personal').exists())

    def test_project_without_scenario_directory_can_be_empty_then_ready(self):
        (self.project/'.git').mkdir()
        empty = self.call('workspace.read', scope='project')
        self.assertEqual(empty['project']['availability'], 'empty')
        self.assertEqual(empty['errors'], [])
        self.file('AGENTS.md', '# Project\n')
        ready = self.call('workspace.read', scope='project')
        self.assertEqual(ready['project']['availability'], 'ready')
        self.assertEqual(ready['scenarios'], [])
        self.assertFalse((self.project/'docs/agents').exists())

    def test_workspace_honors_project_binding_to_a_custom_authority(self):
        authority = self.file('custom/main-rules.md', '# Custom project rules\n')
        binding = {'scope':'project', 'project':str(self.project), 'product':'codex',
                   'installation':'fixture installation', 'profile':'work', 'paths':[authority],
                   'authority':authority, 'layout':'existing'}
        config = self.call('config.read')
        self.call('config.save', revision=config['revision'], save_scope='personal', values={'binding':binding})
        view = self.call('workspace.read', scope='project')
        self.assertEqual(view['project']['path'], str(self.project))
        self.assertEqual(view['project']['availability'], 'ready')
        self.assertIn(authority, [item['path'] for item in view['main_files']])

    def test_workspace_finds_scoped_scenarios_from_a_table_reading_gate(self):
        scenario = self.file('docs/deliverables/widget/agent-rules/release.md', '# Release checks\n')
        for edge, delimiter in (('|', '---'), ('', '---'), ('|', '-'), ('', '--')):
            with self.subTest(table_edge=edge, delimiter=delimiter):
                self.file('AGENTS.md', '# Project\n\nFor matching work, read and follow the table below.\n\n'
                          f'{edge} Trigger | Instructions {edge}\n{edge} {delimiter} | {delimiter} {edge}\n'
                          f'{edge} Release | [Release](docs/deliverables/widget/agent-rules/release.md) {edge}\n')
                view = self.call('workspace.read', scope='project')
                self.assertEqual([item['path'] for item in view['scenarios']], [scenario])

    def test_workspace_only_promotes_scenarios_from_active_rule_reading_chains(self):
        main = self.file('AGENTS.md', '# Project\nRead [Gate](policy/gate.md); context only: [Old](archive/old.md).\n'
                         'Background: [Notes](notes.md).\nRead [Tool](tools/example/SKILL.md) before using the helper.\n')
        gate = self.file('policy/gate.md', '# Gate\nRead [Nested](nested.md) before commands.\n'
                         '历史记录包含读取动作；背景见 [Old](../archive/old.md)。\n')
        nested = self.file('policy/nested.md', '# Nested\n')
        direct = self.file('docs/agents/direct.md', '# Direct scenario\n')
        self.file('notes.md', '# Historical notes\nRead [Old](archive/old.md).\nRead [Outside](../outside/AGENTS.md).\n')
        self.file('archive/old.md', '# Historical example\n')
        outside = self.base/'outside'
        outside.mkdir()
        (outside/'AGENTS.md').write_text('# Other project\nRead [Other](other.md).\n', encoding='utf-8')
        (outside/'other.md').write_text('# Other scenario\n', encoding='utf-8')
        self.file('tools/example/SKILL.md', '---\nname: example\ndescription: Tool instructions\n---\n# Example tool\nRead [Wrong](../../wrong.md).\n')
        self.file('wrong.md', '# Tool reference\n')
        view = self.call('workspace.read', scope='project')
        self.assertEqual({item['path'] for item in view['scenarios']}, {gate, nested, direct})
        self.assertEqual([item['path'] for item in view['main_files']], [main])

    def test_mixed_batch_waits_then_writes_authority_and_every_selected_target(self):
        a = self.file('A.md', 'Keep preferences.\n')
        b = self.file('B.md', 'Keep project boundaries.\n')
        c = self.file('C.md', '')
        d = self.file('D.md', 'Obsolete content must not enter merge.\n')
        e = self.file('E.md', 'Independent unselected rule.\n')
        task = self.call('task.create', kind='sync', authority=a, targets=[
            {'path': b, 'strategy': 'merge'}, {'path': c, 'strategy': 'empty'},
            {'path': d, 'strategy': 'overwrite'}])
        self.assertEqual(task['status'], 'waiting_agent')
        self.assertEqual([x['path'] for x in task['merge_inputs']], [a, b])
        with self.assertRaises(RuleError):
            self.call('task.apply', task_id=task['id'])
        self.assertEqual(Path(c).read_text(), '')
        candidate = 'Keep preferences.\nKeep project boundaries.\n'
        task = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], content=candidate)
        self.assertEqual(Path(a).read_text(), 'Keep preferences.\n')
        task = self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        receipt = self.call('task.apply', task_id=task['id'])
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual([Path(p).read_text() for p in [a, b, c, d]], [candidate] * 4)
        self.assertEqual(Path(e).read_text(), 'Independent unselected rule.\n')
        self.assertEqual(len(receipt['receipt']['written']), 4)

    def test_scenario_parent_segments_cannot_escape_selected_rule_root(self):
        main = self.file('AGENTS.md', '# Project\n')
        source = self.file('docs/agents/commands.md', '# Commands\n')
        escaped = str(self.project / '..' / 'outside.md')
        for arguments in (
            {'operation':'add', 'path':escaped, 'entry':main, 'trigger':'Before commands', 'content':'# Outside\n'},
            {'operation':'edit', 'path':escaped, 'content':'# Outside\n'},
            {'operation':'rename', 'path':source, 'destination':escaped},
        ):
            with self.subTest(operation=arguments['operation']):
                with self.assertRaises(RuleError):
                    self.call('scenario.plan', **arguments)
        self.assertFalse((self.base/'outside.md').exists())
        self.assertEqual(Path(source).read_text(), '# Commands\n')

    def test_external_edit_invalidates_confirmation_and_keeps_candidate(self):
        a = self.file('A.md', 'Authority.\n')
        b = self.file('B.md', 'Old target.\n')
        task = self.call('task.create', kind='sync', authority=a,
                         targets=[{'path': b, 'strategy': 'overwrite'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        Path(b).write_text('Another editor owns this change.\n', encoding='utf-8')
        with self.assertRaises(RuleError) as caught:
            self.call('task.apply', task_id=task['id'])
        self.assertEqual(caught.exception.code, 'external_change')
        self.assertEqual(Path(b).read_text(), 'Another editor owns this change.\n')
        current = self.call('task.get', task_id=task['id'])
        self.assertEqual(current['status'], 'stale')
        self.assertEqual(current['candidate'], 'Authority.\n')
        self.assertIsNone(current['confirmation'])

    def test_empty_target_filled_externally_requires_a_new_strategy_after_refresh(self):
        a = self.file('A.md', 'Authority\n')
        b = self.file('B.md', '')
        task = self.call('task.create', kind='sync', authority=a, targets=[{'path':b,'strategy':'empty'}])
        Path(b).write_bytes(b'External content\n')
        self.assertEqual(self.call('task.get', task_id=task['id'])['status'], 'stale')
        with self.assertRaises(RuleError) as caught:
            self.call('task.refresh', task_id=task['id'])
        self.assertEqual(caught.exception.code, 'strategy_changed')
        retained = self.call('task.get', task_id=task['id'])
        self.assertEqual(retained['candidate'], 'Authority\n')
        self.assertEqual(Path(b).read_bytes(), b'External content\n')
        replacement = self.call('task.create', kind='sync', authority=a, targets=[{'path':b,'strategy':'merge'}])
        self.assertEqual(replacement['status'], 'waiting_agent')
        self.assertEqual([x['content'] for x in replacement['merge_inputs']], ['Authority\n', 'External content\n'])

    def test_review_candidates_include_differences_before_selection(self):
        target = self.file('review.md', '# Rules\n\nOriginal requirement\n')
        task = self.call('task.create', kind='review', paths=[target])
        result = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'],
                           suggestions=[{'id':'s1', 'path':target, 'line':3, 'evidence':['Original text'],
                                         'impact':'Trigger is unclear', 'recommendation':'Clarify the trigger',
                                         'verification':'text', 'content':'# Rules\n\nClear requirement\n'}])
        self.assertEqual(result['status'], 'candidate_review')
        self.assertIn('-Original requirement\n', result['suggestions'][0]['diff'])
        self.assertIn('+Clear requirement\n', result['suggestions'][0]['diff'])
        self.assertEqual(Path(target).read_text(), '# Rules\n\nOriginal requirement\n')

    def test_reading_a_review_after_external_edit_reports_stale_and_preserves_suggestions(self):
        target = self.file('review.md', 'Original\n')
        task = self.call('task.create', kind='review', paths=[target])
        suggestion = {'id':'s1', 'path':target, 'line':1, 'evidence':['Original rule text'],
                      'impact':'Unclear trigger', 'recommendation':'Add a trigger',
                      'verification':'pending', 'content':'Candidate\n'}
        self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], suggestions=[suggestion])
        Path(target).write_bytes(b'Another editor\n')
        current = self.call('task.get', task_id=task['id'])
        self.assertEqual(current['status'], 'stale')
        self.assertEqual(current['suggestions'][0]['content'], 'Candidate\n')
        self.assertTrue(current['suggestions_expired'])
        self.assertEqual(current['changed'], [target])
        self.assertEqual(Path(target).read_bytes(), b'Another editor\n')

    def test_shared_entity_reports_unselected_alias_and_writes_entity_once(self):
        a = self.file('A.md', 'A\n')
        b = self.file('B.md', 'B\n')
        alias = self.project / 'Alias.md'
        os.link(a, alias)
        task = self.call('task.create', kind='sync', authority=a,
                         known_paths=[str(alias)], targets=[{'path': b, 'strategy': 'merge'}])
        task = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], content='A and B\n')
        self.assertIn(str(alias), [p for x in task['shared_impacts'] for p in x['entrances']])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        receipt = self.call('task.apply', task_id=task['id'])
        self.assertEqual(Path(a).read_bytes(), b'A and B\n')
        self.assertEqual(alias.read_bytes(), b'A and B\n')
        self.assertEqual(len(receipt['receipt']['written']), 2)

    def test_sync_respects_verified_roles_across_aliases_without_classifying_by_name_alone(self):
        identity = self.file('SOUL.md', 'Identity content\n')
        entrance = self.project/'AGENTS.md'
        entrance.symlink_to(identity)
        target = self.file('empty.md', '')
        with self.assertRaises(RuleError) as caught:
            self.call('task.create', kind='sync', authority=str(entrance), targets=[{'path':target,'strategy':'empty'}])
        self.assertEqual(caught.exception.code, 'identity_sync')
        work = self.file('USER.md', 'A confirmed work rule despite its name\n')
        task = self.call('task.create', kind='sync', authority=work, targets=[{'path':target,'strategy':'empty'}],
                         classifications=[{'path':work,'role':'work','evidence':'User confirmed this is a work rule, not a persona file.'}])
        self.assertEqual(task['status'], 'pending_confirmation')
        self.assertEqual(task['classifications'][0]['role'], 'work')
        self.assertEqual(Path(target).read_bytes(), b'')

    def test_write_failure_stops_and_safely_restores_completed_writes(self):
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('Permission boundary requires a non-root execution user')
        a = self.file('A.md', 'Original A\n')
        b = self.file('B.md', 'Original B\n')
        Path(b).chmod(stat.S_IREAD)
        self.addCleanup(Path(b).chmod, stat.S_IWRITE | stat.S_IREAD)
        task = self.call('task.create', kind='sync', authority=a, targets=[{'path': b, 'strategy': 'merge'}])
        task = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], content='Combined\n')
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(Path(a).read_bytes(), b'Original A\n')
        self.assertEqual(Path(b).read_bytes(), b'Original B\n')
        self.assertEqual(result['receipt']['restored'], [a])
        self.assertEqual(result['receipt']['unwritten'], [b])
        self.assertEqual(result['candidate'], 'Combined\n')

    def test_scenario_rename_updates_root_and_subdirectory_references_as_one_plan(self):
        rule = self.file('scenarios/build.md', 'Build guidance.\n')
        root = self.file('AGENTS.md', 'For builds read [build](scenarios/build.md#checks).\n')
        nested = self.file('src/AGENTS.md', 'Read [build](../scenarios/build.md).\n')
        destination = str(self.project / 'scenarios' / 'compile.md')
        task = self.call('scenario.plan', operation='rename', path=rule, destination=destination)
        self.assertTrue(Path(rule).exists())
        self.assertFalse(Path(destination).exists())
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(Path(rule).exists())
        self.assertEqual(Path(destination).read_bytes(), b'Build guidance.\n')
        self.assertEqual(Path(root).read_text(), 'For builds read [build](scenarios/compile.md#checks).\n')
        self.assertEqual(Path(nested).read_text(), 'Read [build](../scenarios/compile.md).\n')

    def test_scenario_rename_reconnects_known_link_entrances_and_delete_lists_them(self):
        rule = self.file('scenarios/old.md', 'Keep this scenario\n')
        direct = self.project/'CLAUDE.md'
        indirect = self.project/'AGENTS.md'
        direct.symlink_to(rule)
        indirect.symlink_to(direct)
        destination = self.project/'scenarios/new.md'
        task = self.call('scenario.plan', operation='rename', path=rule, destination=str(destination))
        self.assertEqual({x['path'] for x in task['writes'] if x.get('op')=='link'}, {str(direct), str(indirect)})
        self.assertEqual(set(task['shared_impacts'][0]['entrances']), {rule, str(direct), str(indirect)})
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(direct.resolve(), destination)
        self.assertEqual(indirect.resolve(), destination)
        self.assertEqual(indirect.read_bytes(), b'Keep this scenario\n')
        restore = self.call('history.restore', task_id=task['id'])
        self.assertEqual([x['content'] for x in restore['writes'] if x.get('target')], ['Keep this scenario\n'] * 2)
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        restored = self.call('task.apply', task_id=restore['id'])
        self.assertEqual(restored['status'], 'completed', restored.get('receipt'))
        self.assertEqual(direct.resolve(), Path(rule))
        self.assertEqual(indirect.resolve(), Path(rule))
        self.assertEqual(indirect.read_bytes(), b'Keep this scenario\n')
        repeated = self.call('scenario.plan', operation='rename', path=rule, destination=str(destination))
        self.call('task.confirm', task_id=repeated['id'], plan_revision=repeated['plan_revision'])
        self.assertEqual(self.call('task.apply', task_id=repeated['id'])['status'], 'completed')
        removal = self.call('scenario.plan', operation='delete', path=str(destination))
        self.assertEqual({x['path'] for x in removal['writes']}, {str(destination), str(direct), str(indirect)})
        self.call('task.confirm', task_id=removal['id'], plan_revision=removal['plan_revision'])
        removed = self.call('task.apply', task_id=removal['id'])
        self.assertEqual(removed['status'], 'completed')
        self.assertFalse(os.path.lexists(direct))
        self.assertFalse(os.path.lexists(indirect))
        self.assertFalse(destination.exists())

    def test_global_scenario_creation_includes_triggered_reading_entrance(self):
        global_root = self.base / 'my rules'
        global_root.mkdir()
        main = global_root / 'AGENTS.md'
        main.write_bytes(b'Personal rules.\n')
        scenario = global_root / 'scenarios-agent-instructions' / 'commands.md'
        task = self.call('scenario.plan', scope='global', rules_root=str(global_root), operation='add',
                         path=str(scenario), entry=str(main), title='命令执行', trigger='编写或执行命令前', content='核对实际 Shell。\n')
        self.assertFalse(scenario.exists())
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(scenario.read_text(encoding='utf-8'), '# 命令执行\n\n核对实际 Shell。\n')
        self.assertIn('编写或执行命令前：读取并遵守 [命令执行](scenarios-agent-instructions/commands.md)', main.read_text(encoding='utf-8'))

    def test_scenario_name_and_new_reading_entry_use_the_body_title(self):
        main = self.file('AGENTS.md', '# Project\n')
        path = str(self.project/'docs/agents/release.md')
        task = self.call('scenario.plan', operation='add', path=path, entry=main,
                         trigger='Before release', content='# Release checks\n\nValidate output.\n')
        self.assertIn('[Release checks]', next(x['content'] for x in task['writes'] if x['path']==main))
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        view = self.call('workspace.read', scope='project')
        self.assertEqual(view['scenarios'][0]['title'], 'Release checks')

    def test_scenario_edit_reports_known_shared_entrance_impact(self):
        target = self.file('scenarios/shared.md', 'Before\n')
        alias = self.project/'alias.md'
        alias.symlink_to(target)
        task = self.call('scenario.plan', operation='edit', path=target, content='After\n', entrances=[str(alias)])
        self.assertIn(str(alias), [path for group in task['shared_impacts'] for path in group['entrances']])
        self.assertEqual(alias.read_bytes(), b'Before\n')

    def test_scenario_deletion_requires_resolving_every_known_reference(self):
        rule = self.file('scenarios/old.md', 'Old scenario.\n')
        main = self.file('AGENTS.md', 'Read [old](scenarios/old.md).\nKeep other requirements.\n')
        with self.assertRaises(RuleError) as caught:
            self.call('scenario.plan', operation='delete', path=rule)
        self.assertEqual(caught.exception.code, 'references_required')
        task = self.call('scenario.plan', operation='delete', path=rule,
                         reference_changes=[{'path': main, 'content': 'Keep other requirements.\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        self.assertFalse(Path(rule).exists())
        self.assertEqual(Path(main).read_text(), 'Keep other requirements.\n')

    def test_scenario_deletion_updates_a_shared_reference_once_and_ignores_deleted_self_reference(self):
        rule = self.file('scenarios/old.md', 'Read [self](old.md)\n')
        main = self.file('AGENTS.md', 'Read [old](scenarios/old.md)\nKeep this\n')
        alias = self.project/'CLAUDE.md'
        alias.symlink_to(main)
        with self.assertRaises(RuleError) as caught:
            self.call('scenario.plan', operation='delete', path=rule, reference_changes=[
                {'path':main,'content':'Keep this\n'}, {'path':str(alias),'content':'Conflicting\n'}])
        self.assertEqual(caught.exception.code, 'reference_conflict')
        task = self.call('scenario.plan', operation='delete', path=rule, reference_changes=[
            {'path':str(alias),'content':'Keep this\n'}])
        self.assertEqual(len(task['writes']), 2)
        self.assertEqual(set(task['shared_impacts'][0]['entrances']), {main, str(alias)})
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(Path(rule).exists())
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.read_bytes(), b'Keep this\n')

    def test_scenario_deletion_includes_explicit_entrances_and_new_references_expire_plan(self):
        rule = self.file('scenarios/old.md', 'Old scenario\n')
        external = self.base/'custom-entry.md'
        external.write_text('[Old](project/scenarios/old.md)\n', encoding='utf-8')
        with self.assertRaises(RuleError) as caught:
            self.call('scenario.plan', operation='delete', path=rule, entrances=[str(external)])
        self.assertEqual(caught.exception.code, 'references_required')
        task = self.call('scenario.plan', operation='delete', path=rule, entrances=[str(external)],
                         reference_changes=[{'path':str(external),'content':'Other requirements\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        added = self.file('new/AGENTS.md', '[Old](../scenarios/old.md)\n')
        with self.assertRaises(RuleError) as caught:
            self.call('task.apply', task_id=task['id'])
        self.assertEqual(caught.exception.code, 'external_change')
        self.assertTrue(Path(rule).exists())
        self.assertIn('old.md', Path(added).read_text())
        self.assertIn('old.md', external.read_text())

    def test_concurrent_apply_is_serialized_or_reports_busy_without_corrupting_task(self):
        a = self.file('A.md', 'Authority\n')
        b = self.file('B.md', 'Target\n')
        task = self.call('task.create', kind='sync', authority=a, targets=[{'path': b, 'strategy': 'overwrite'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        barrier = threading.Barrier(2)
        def invoke():
            barrier.wait(timeout=5)
            try:
                return self.call('task.apply', task_id=task['id'])['status']
            except RuleError as error:
                return error.code
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: invoke(), range(2)))
        self.assertIn('completed', outcomes)
        self.assertTrue(set(outcomes) <= {'completed', 'busy'}, outcomes)
        current = self.call('task.get', task_id=task['id'])
        self.assertEqual(current['status'], 'completed')
        self.assertEqual(Path(b).read_bytes(), b'Authority\n')

    def test_history_restore_is_a_new_plan_against_current_files(self):
        target = self.file('AGENTS.md', 'Original\n')
        task = self.call('task.create', kind='edit', changes=[{'path': target, 'content': 'Version two\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        Path(target).write_bytes(b'Later manual edit\n')
        restored = self.call('history.restore', task_id=task['id'])
        self.assertNotEqual(restored['id'], task['id'])
        self.assertIn('Later manual edit', restored['writes'][0]['diff'])
        self.assertEqual(Path(target).read_bytes(), b'Later manual edit\n')
        self.call('task.confirm', task_id=restored['id'], plan_revision=restored['plan_revision'])
        self.call('task.apply', task_id=restored['id'])
        self.assertEqual(Path(target).read_bytes(), b'Original\n')
        records = self.call('history.list')['tasks']
        self.assertEqual({x['id'] for x in records}, {task['id'], restored['id']})

    def test_link_plan_and_history_restore_preserve_entry_type_and_original_body(self):
        entrance = self.file('host/AGENTS.md', 'Original entrance\n')
        physical = self.file('rules/AGENTS.md', 'Shared entity\n')
        task = self.call('task.create', kind='edit', changes=[{'path': entrance, 'op': 'link', 'target': physical}])
        self.assertFalse(Path(entrance).is_symlink())
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(Path(entrance).is_symlink())
        self.assertEqual(Path(entrance).read_bytes(), b'Shared entity\n')
        restore = self.call('history.restore', task_id=task['id'])
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        result = self.call('task.apply', task_id=restore['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(Path(entrance).is_symlink())
        self.assertEqual(Path(entrance).read_bytes(), b'Original entrance\n')
        self.assertEqual(Path(physical).read_bytes(), b'Shared entity\n')

    def test_restoring_a_link_previews_current_target_and_binds_its_freshness(self):
        original = self.file('original.md', 'Historical body\n')
        replacement = self.file('replacement.md', 'Replacement\n')
        entrance = self.project/'entrance.md'
        entrance.symlink_to(original)
        task = self.call('task.create', kind='edit', changes=[{'path':str(entrance),'op':'link','target':replacement}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        Path(original).write_bytes(b'Current original target\n')
        restore = self.call('history.restore', task_id=task['id'])
        self.assertEqual(restore['writes'][0]['content'], 'Current original target\n')
        self.assertIn('+Current original target', restore['writes'][0]['diff'])
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        Path(original).write_bytes(b'Later target edit\n')
        with self.assertRaises(RuleError):
            self.call('task.apply', task_id=restore['id'])
        refreshed = self.call('task.refresh', task_id=restore['id'])
        self.assertEqual(refreshed['writes'][0]['content'], 'Later target edit\n')
        self.call('task.confirm', task_id=refreshed['id'], plan_revision=refreshed['plan_revision'])
        result = self.call('task.apply', task_id=refreshed['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(entrance.resolve(), Path(original))
        self.assertEqual(entrance.read_bytes(), b'Later target edit\n')

    def test_restoring_content_through_a_retargeted_link_does_not_overwrite_later_target_edit(self):
        target = self.file('target.md', 'Original\n')
        other = self.file('other.md', 'Other\n')
        entrance = self.project/'entrance.md'
        entrance.symlink_to(target)
        task = self.call('task.create', kind='edit', changes=[{'path':str(entrance),'content':'Version two\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        entrance.unlink()
        entrance.symlink_to(other)
        restore = self.call('history.restore', task_id=task['id'])
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        Path(target).write_bytes(b'Another editor after confirmation\n')
        with self.assertRaises(RuleError) as caught:
            self.call('task.apply', task_id=restore['id'])
        self.assertEqual(caught.exception.code, 'external_change')
        self.assertEqual(Path(target).read_bytes(), b'Another editor after confirmation\n')
        self.assertEqual(entrance.resolve(), Path(other))
        refreshed = self.call('task.refresh', task_id=restore['id'])
        self.assertEqual([write['content'] for write in refreshed['writes']], ['Original\n', 'Original\n'])
        self.assertIsNone(refreshed['confirmation'])
        self.call('task.confirm', task_id=refreshed['id'], plan_revision=refreshed['plan_revision'])
        result = self.call('task.apply', task_id=refreshed['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(entrance.resolve(), Path(target))
        self.assertEqual(entrance.read_bytes(), b'Original\n')
        self.assertEqual(Path(other).read_bytes(), b'Other\n')

    def test_process_interruption_is_reconciled_without_replaying_old_confirmation(self):
        target = self.file('interrupted.md', 'Before\n')
        task = self.call('task.create', kind='edit', changes=[{'path': target, 'content': 'After\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        original_sync = os.fsync
        identity = os.stat(target).st_ino
        def interrupted_sync(fd):
            original_sync(fd)
            if os.fstat(fd).st_ino == identity:
                raise SystemExit('simulated process death after filesystem flush')
        with patch('os.fsync', side_effect=interrupted_sync):
            with self.assertRaises(SystemExit):
                self.call('task.apply', task_id=task['id'])
        restarted = Maintenance(self.base / 'personal', self.project)
        current = restarted.action('task.get', {'task_id': task['id']})
        self.assertEqual(current['status'], 'recovery_required')
        self.assertIn(target, current['receipt']['recovery_required'])
        self.assertIsNone(current['confirmation'])
        with self.assertRaises(RuleError):
            restarted.action('task.apply', {'task_id': task['id']})
        self.assertEqual(Path(target).read_bytes(), b'After\n')
        restore = restarted.action('history.restore', {'task_id': task['id']})
        restarted.action('task.confirm', {'task_id': restore['id'], 'plan_revision': restore['plan_revision']})
        restarted.action('task.apply', {'task_id': restore['id']})
        self.assertEqual(Path(target).read_bytes(), b'Before\n')

    def test_corrupted_persisted_identity_cannot_redirect_task_access(self):
        a = self.file('A.md', 'A\n')
        b = self.file('B.md', 'B\n')
        one = self.call('task.create', kind='edit', changes=[{'path':a,'content':'First\n'}])
        two = self.call('task.create', kind='edit', changes=[{'path':b,'content':'Second\n'}])
        record = self.base/'personal/history'/one['id']/'task.json'
        corrupted = json.loads(record.read_text(encoding='utf-8'))
        corrupted['id'] = two['id']
        record.write_text(json.dumps(corrupted), encoding='utf-8')
        with self.assertRaises(RuleError) as caught:
            self.call('task.get', task_id=one['id'])
        self.assertEqual(caught.exception.code, 'history_invalid')
        self.assertEqual(self.call('task.get', task_id=two['id'])['writes'][0]['content'], 'Second\n')
        self.assertEqual(Path(a).read_bytes(), b'A\n')
        self.assertEqual(Path(b).read_bytes(), b'B\n')

    def test_review_result_is_bound_to_task_and_selected_suggestions_form_one_plan(self):
        target = self.file('AGENTS.md', 'Vague requirement\n')
        foreign = self.file('unselected.md', 'Keep\n')
        task = self.call('task.create', kind='review', paths=[target], models={
            'configured': {'id': 'default-model', 'source': 'config'},
            'current': {'id': None, 'source': 'unknown'}, 'targets': ['model-a', 'model-b']})
        other = self.call('task.create', kind='review', paths=[target])
        self.assertNotEqual(task['input_revision'], other['input_revision'])
        prompt = self.call('task.prompt', task_id=task['id'])['prompt']
        self.assertIn(task['input_revision'], prompt)
        self.assertIn(task['id'], prompt)
        suggestion = {'id': 's1', 'path': target, 'line': 1, 'evidence': ['Rule text'],
                      'impact': 'Unclear scope', 'recommendation': 'Specify the trigger',
                      'verification': 'pending', 'content': 'Before shell use, check the shell.\n'}
        with self.assertRaises(RuleError):
            self.call('task.result', task_id=task['id'], input_revision=other['input_revision'], suggestions=[suggestion])
        with self.assertRaises(RuleError):
            self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], suggestions=[dict(suggestion, path=foreign)])
        result = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], suggestions=[suggestion])
        self.assertEqual(result['status'], 'candidate_review')
        self.assertEqual(result['models']['current']['id'], None)
        self.assertEqual(result['suggestions'][0]['verification'], 'pending')
        plan = self.call('task.select', task_id=task['id'], suggestions=['s1'])
        self.assertEqual(plan['id'], task['id'])
        self.assertEqual(Path(target).read_bytes(), b'Vague requirement\n')
        self.call('task.confirm', task_id=plan['id'], plan_revision=plan['plan_revision'])
        self.call('task.apply', task_id=plan['id'])
        self.assertEqual(Path(target).read_bytes(), suggestion['content'].encode())
        self.assertEqual(Path(foreign).read_bytes(), b'Keep\n')

    def test_review_reuses_scoped_preferences_without_saving_task_overrides(self):
        target = self.file('AGENTS.md', 'Keep user preference\n')
        config = self.call('config.read')
        binding = {'scope':'project', 'project':str(self.project), 'product':'codex',
                   'installation':'test', 'profile':'default', 'paths':[target],
                   'authority':target, 'layout':'keep'}
        saved = self.call('config.save', revision=config['revision'],
                          values={'review_models':['saved-model'], 'binding':binding})
        task = self.call('task.create', kind='review', paths=[target])
        self.assertEqual(task['models']['targets'], ['saved-model'])
        self.assertEqual(task['binding'], binding)
        self.assertIsNone(task['models']['configured']['id'])
        self.assertIsNone(task['models']['current']['id'])
        explicit = self.call('task.create', kind='review', paths=[target], models={'targets':['one-off']}, binding=None)
        self.assertEqual(explicit['models']['targets'], ['one-off'])
        self.assertIsNone(explicit['models']['current']['id'])
        self.assertIsNone(explicit['binding']['product'])
        self.assertEqual(explicit['binding']['paths'], [target])
        self.assertEqual(self.call('config.read')['revision'], saved['revision'])

    def test_every_task_keeps_intent_and_a_complete_unsplit_target_binding(self):
        target = self.file('AGENTS.md', 'Keep user choice\n')
        second = self.file('target.md', '')
        binding = {'scope':'project', 'project':str(self.project), 'product':'codex',
                   'installation':'known-installation', 'profile':'work', 'paths':[target, second],
                   'authority':target, 'layout':'existing'}
        initial = self.call('config.read')
        self.call('config.save', revision=initial['revision'], values={'binding':binding})
        requests = [{'kind':'edit', 'changes':[{'path':target, 'content':'Candidate\n'}]},
                    {'kind':'sync', 'authority':target, 'targets':[{'path':second, 'strategy':'empty'}]},
                    {'kind':'review', 'paths':[target]}]
        for request in requests:
            with self.subTest(kind=request['kind']):
                task = self.call('task.create', **request, intent='Clarify when shell checks apply')
                resumed = Maintenance(self.base/'personal', self.project).action('task.get', {'task_id':task['id']})
                self.assertEqual(resumed['intent'], 'Clarify when shell checks apply')
                self.assertEqual(resumed['binding'], binding)
                self.assertEqual(resumed['binding_source'], str(self.base/'personal/.env'))
                with self.assertRaises(RuleError) as caught:
                    self.call('task.create', **request, binding={'product':'hermes'})
                self.assertEqual(caught.exception.code, 'binding_incomplete')
                with self.assertRaises(RuleError) as caught:
                    self.call('task.create', **request, binding=dict(binding, scope='global', project=None))
                self.assertEqual(caught.exception.code, 'binding_scope')
        unbound = self.call('task.create', **requests[0], binding=None)
        self.assertEqual(set(unbound['binding']), set(binding))
        self.assertEqual(unbound['binding']['paths'], [target])
        self.assertIsNone(unbound['binding']['product'])
        self.assertIsNone(unbound['binding']['installation'])
        self.assertEqual(Path(target).read_bytes(), b'Keep user choice\n')

    def test_behavior_verified_suggestion_requires_evidence(self):
        target = self.file('AGENTS.md', 'Keep this\n')
        task = self.call('task.create', kind='review', paths=[target])
        suggestion = {'id':'s1','path':target,'line':1,'evidence':[], 'impact':'Unclear trigger',
                      'recommendation':'Specify when to read','verification':'behavior','content':'Candidate\n'}
        with self.assertRaises(RuleError) as caught:
            self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], suggestions=[suggestion])
        self.assertEqual(caught.exception.code, 'evidence')
        pending = dict(suggestion, verification='pending')
        result = self.call('task.result', task_id=task['id'], input_revision=task['input_revision'], suggestions=[pending])
        self.assertEqual(result['suggestions'][0]['verification'], 'pending')
        self.assertEqual(Path(target).read_bytes(), b'Keep this\n')

    def test_history_retention_is_per_scope_and_preserves_unfinished_tasks(self):
        target = self.file('AGENTS.md', 'Original\n')
        pending = self.call('task.create', kind='review', paths=[target])
        global_task = self.call('task.create', kind='edit', scope='global', changes=[{'path': target, 'content': 'Global\n'}])
        self.call('task.confirm', task_id=global_task['id'], plan_revision=global_task['plan_revision'])
        self.call('task.apply', task_id=global_task['id'])
        completed = []
        for index in range(31):
            task = self.call('task.create', kind='edit', changes=[{'path': target, 'content': str(index)}])
            self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
            self.call('task.apply', task_id=task['id'])
            completed.append(task['id'])
        records = self.call('history.list')['tasks']
        self.assertEqual(len(records), 32)
        self.assertNotIn(completed[0], {x['id'] for x in records})
        self.assertIn(pending['id'], {x['id'] for x in records})
        self.call('history.prune', keep=2)
        remaining = self.call('history.list')['tasks']
        self.assertEqual({x['id'] for x in remaining}, {pending['id'], global_task['id'], *completed[-2:]})

    def test_retention_error_does_not_hide_a_completed_write(self):
        target = self.file('AGENTS.md', 'Before\n')
        task = self.call('task.create', kind='edit', changes=[{'path':target,'content':'After\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.file('.env.agent-instructions-update', 'AGENT_RULES_HISTORY_KEEP=invalid\n')
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['receipt']['written'], [target])
        self.assertIn('history_warning', result)
        self.assertEqual(self.call('task.get', task_id=task['id'])['status'], 'completed')
        self.assertEqual(Path(target).read_bytes(), b'After\n')

    def test_pruning_layout_history_cleans_only_unchanged_owned_directory_backup(self):
        entrance = self.project/'old-scenarios'
        self.file('old-scenarios/nested/command.md', 'Original\n')
        target = self.project/'new-scenarios'
        self.file('new-scenarios/command.md', 'Shared\n')
        task = self.call('task.create', kind='edit', changes=[{'path':str(entrance),'op':'link','target':str(target)}])
        backup = Path(task['writes'][0]['backup'])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        self.assertTrue(backup.is_dir())
        current = self.call('task.create', kind='edit', changes=[{'path':str(target/'command.md'),'content':'Latest\n'}])
        self.call('task.confirm', task_id=current['id'], plan_revision=current['plan_revision'])
        self.call('task.apply', task_id=current['id'])
        result = self.call('history.prune', keep=1)
        self.assertIn(task['id'], result['removed'])
        self.assertFalse(backup.exists())
        self.assertTrue(entrance.is_symlink())
        self.assertEqual((entrance/'command.md').read_bytes(), b'Latest\n')

    def test_directory_link_retarget_invalidates_plan_and_refresh_retains_candidate(self):
        first = self.project / 'rules-one'
        second = self.project / 'rules-two'
        first.mkdir()
        second.mkdir()
        (first / 'actions.md').write_bytes(b'One\n')
        (second / 'actions.md').write_bytes(b'Two\n')
        entrance = self.project / 'scenarios'
        entrance.symlink_to(first, target_is_directory=True)
        task = self.call('task.create', kind='edit', changes=[{'path': str(entrance/'actions.md'), 'content': 'Candidate\n'}])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        entrance.unlink()
        entrance.symlink_to(second, target_is_directory=True)
        with self.assertRaises(RuleError):
            self.call('task.apply', task_id=task['id'])
        refreshed = self.call('task.refresh', task_id=task['id'])
        self.assertNotEqual(refreshed['plan_revision'], task['plan_revision'])
        self.assertIsNone(refreshed['confirmation'])
        self.assertEqual(refreshed['writes'][0]['content'], 'Candidate\n')
        self.assertIn('Two', refreshed['writes'][0]['diff'])
        self.assertEqual((first/'actions.md').read_bytes(), b'One\n')
        self.assertEqual((second/'actions.md').read_bytes(), b'Two\n')

    def test_scenario_relocation_preserves_outbound_links_and_updates_code_path_references(self):
        main = self.file('AGENTS.md', 'Before running commands read `rules/commands.md`.\n')
        other = self.file('rules/common.md', 'Common rule\n')
        source = self.file('rules/commands.md', '[Common](common.md#details) and [self](commands.md#intro)\n')
        destination = str(self.project/'rules/nested/renamed.md')
        task = self.call('scenario.plan', operation='rename', path=source, destination=destination)
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('`rules/nested/renamed.md`', Path(main).read_text())
        self.assertIn('[Common](../common.md#details)', Path(destination).read_text())
        self.assertIn('[self](renamed.md#intro)', Path(destination).read_text())
        self.assertFalse(Path(source).exists())

    def test_linked_main_uses_entity_directory_for_index_and_renamed_reference(self):
        physical = self.base/'shared'
        (physical/'scenarios').mkdir(parents=True)
        main = physical/'AGENTS.md'
        rule = physical/'scenarios/old.md'
        main.write_bytes(b'Read [scenario](scenarios/old.md)\n')
        rule.write_bytes(b'Rule\n')
        entrance = self.project/'AGENTS.md'
        entrance.symlink_to(main)
        index = self.call('index.read')
        self.assertIn(str(rule), {x['path'] for x in index['files'] if x['status']=='found'})
        workspace = self.call('workspace.read', scope='project')
        self.assertEqual([item['path'] for item in workspace['scenarios']], [str(rule)])
        self.assertEqual([item['path'] for item in workspace['main_files']], [str(entrance)])
        destination = physical/'scenarios/new.md'
        task = self.call('scenario.plan', scope='global', rules_root=str(physical), operation='rename',
                         path=str(rule), destination=str(destination), entrances=[str(entrance)])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed', result.get('receipt'))
        self.assertEqual(main.read_bytes(), b'Read [scenario](scenarios/new.md)\n')
        self.assertTrue(entrance.is_symlink())
        self.assertTrue(destination.exists())

    def test_declared_home_path_basis_is_used_without_guessing_from_the_source_location(self):
        home = self.base/'fixture-home'
        scenarios = self.base/'physical-global/scenarios-agent-instructions'
        scenarios.mkdir(parents=True)
        (home/'.agents').mkdir(parents=True)
        (home/'.agents/scenarios-agent-instructions').symlink_to(scenarios, target_is_directory=True)
        rule = scenarios/'old.md'
        rule.write_bytes(b'Rule\n')
        main = self.file('AGENTS.md', 'Relative to the current user home, read `.agents/scenarios-agent-instructions/old.md`.\n')
        unknown = self.call('index.read')
        entry = next(x for x in unknown['files'] if x['path']==main)
        self.assertIsNone(entry['references'][0]['target'])
        self.assertEqual(entry['references'][0]['basis'], 'unresolved')
        bases = [{'path':main,'prefix':'.agents/','base':str(home)}]
        indexed = self.call('index.read', reference_bases=bases)
        self.assertIn(str(rule), {x['path'] for x in indexed['files'] if x['status']=='found'})
        destination = scenarios/'new.md'
        task = self.call('scenario.plan', scope='global', rules_root=str(scenarios.parent), operation='rename',
                         path=str(rule), destination=str(destination), entrances=[main], reference_bases=bases)
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('`.agents/scenarios-agent-instructions/new.md`', Path(main).read_text())
        self.assertEqual(destination.read_bytes(), b'Rule\n')
        self.assertFalse(rule.exists())

    def test_layout_plan_moves_to_selected_physical_root_with_recoverable_directory_entrance(self):
        main = self.file('original/AGENTS.md', 'Read [command](scenarios-agent-instructions/command.md)\n')
        scenario = self.file('original/scenarios-agent-instructions/command.md', 'Check the shell\n')
        root = self.project / 'physical rules'
        task = self.call('layout.plan', root=str(root), source_main=main,
                         source_scenarios=str(Path(scenario).parent), entrances=[main])
        self.assertFalse(root.exists())
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        result = self.call('task.apply', task_id=task['id'])
        self.assertEqual(result['status'], 'completed', result.get('receipt'))
        self.assertTrue(Path(main).is_symlink())
        self.assertTrue(Path(scenario).parent.is_symlink())
        self.assertEqual((root/'scenarios-agent-instructions/command.md').read_bytes(), b'Check the shell\n')
        restore = self.call('history.restore', task_id=task['id'])
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        result = self.call('task.apply', task_id=restore['id'])
        self.assertEqual(result['status'], 'completed', result.get('receipt'))
        self.assertFalse(Path(main).is_symlink())
        self.assertFalse(Path(scenario).parent.is_symlink())
        self.assertEqual(Path(scenario).read_bytes(), b'Check the shell\n')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_layout_junction_restore_and_history_prune_preserve_original_target(self):
        import _winapi
        main = self.file('AGENTS.md', 'Read [rules](legacy/command.md)\n')
        original = Path(self.file('real-rules/command.md', 'Keep the original\n'))
        entrance = self.project/'legacy'
        _winapi.CreateJunction(str(original.parent), str(entrance))
        self.assertEqual(self.call('file.read', path=str(entrance))['kind'], 'junction')
        task = self.call('layout.plan', root=str(self.project/'new-root'), source_main=main,
                         source_scenarios=str(entrance), entrances=[main])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.assertEqual(self.call('task.apply', task_id=task['id'])['status'], 'completed')
        restore = self.call('history.restore', task_id=task['id'])
        self.call('task.confirm', task_id=restore['id'], plan_revision=restore['plan_revision'])
        result = self.call('task.apply', task_id=restore['id'])
        self.assertEqual(result['status'], 'completed', result.get('receipt'))
        self.assertTrue(entrance.is_junction())
        self.call('history.prune', keep=1)
        self.assertEqual(original.read_bytes(), b'Keep the original\n')

    def test_restoring_directory_replaced_by_external_directory_refuses_false_completion(self):
        main = self.file('AGENTS.md', 'Main\n')
        original = self.file('old/command.md', 'Original\n')
        entrance = Path(original).parent
        task = self.call('layout.plan', root=str(self.project/'physical'), source_main=main,
                         source_scenarios=str(entrance), entrances=[main])
        self.call('task.confirm', task_id=task['id'], plan_revision=task['plan_revision'])
        self.call('task.apply', task_id=task['id'])
        entrance.unlink()
        entrance.mkdir()
        external = entrance/'external.md'
        external.write_bytes(b'Another user directory\n')
        with self.assertRaises(RuleError) as caught:
            self.call('history.restore', task_id=task['id'])
        self.assertEqual(caught.exception.code, 'directory_conflict')
        self.assertEqual(external.read_bytes(), b'Another user directory\n')


if __name__ == '__main__':
    unittest.main()
