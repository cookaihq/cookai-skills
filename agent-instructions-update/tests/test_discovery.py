import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
from rules import Maintenance, RuleError


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ['AGENT_RULES_TEST_TMP'])
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()

    def put(self, path, content='fixture'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return str(path)

    def test_five_products_profiles_custom_desktop_and_fallback_have_separate_evidence(self):
        home = self.base / 'home'
        binaries = self.base / 'bin'
        for name in ('codex', 'claude', 'hermes', 'opencode'):
            self.put(binaries / (name + ('.exe' if os.name == 'nt' else '')))
        second = self.put(self.base / 'standalone' / 'codex.exe')
        desktop = self.put(self.base / 'desktop' / 'WorkBuddy.exe')
        self.put(home / '.codex/AGENTS.md', 'Codex rules')
        self.put(home / '.codex/config.toml', 'model="base-model"\n[profiles.fast]\nmodel="fast-model"\n')
        claude = self.put(home / '.claude/CLAUDE.md', 'Claude fallback')
        self.put(home / '.workbuddy/CODEBUDDY.md', 'Work rules')
        soul = self.put(home / '.workbuddy/SOUL.md', 'Persona')
        hermes = self.base / 'hermes-home'
        self.put(hermes / 'SOUL.md', 'Identity')
        self.put(hermes / 'profiles/rs/SOUL.md', 'Another identity')
        self.put(hermes / 'active_profile', 'rs')
        self.put(self.project / 'AGENTS.md', '[Scenario](rules/actions.md)')
        self.put(self.project / 'sub/AGENTS.md', 'Only the subdirectory')
        self.put(self.project / 'rules/actions.md', 'Scenario')
        env = {'PATH': str(binaries), 'HERMES_HOME': str(hermes), 'LOCALAPPDATA': str(home / 'AppData/Local')}
        app = Maintenance(self.base / 'personal', self.project, environment=env)
        report = app.action('discover.scan', {'home': str(home), 'probe_system': False,
                            'custom': [{'product': 'codex', 'installation': second},
                                       {'product': 'workbuddy', 'installation': desktop}]})
        products = {x['id']: x for x in report['products']}
        self.assertEqual({p for p,x in products.items() if x['supported']}, {'codex','claude-code','workbuddy','hermes','opencode'})
        for product in ('codex','claude-code','workbuddy','hermes','opencode'):
            self.assertEqual(products[product]['installation_status'], 'found', product)
            self.assertEqual(products[product]['loading'], 'unverified')
        self.assertEqual(len(products['codex']['installations']), 2)
        self.assertIn('fast', {p['name'] for p in products['codex']['profiles']})
        self.assertIn('rs', {p['name'] for p in products['hermes']['profiles']})
        fallback = next(x for x in products['opencode']['rules'] if x['path'] == claude)
        self.assertEqual(fallback['selection'], 'fallback_candidate')
        persona = next(x for x in products['workbuddy']['rules'] if x['path'] == soul)
        self.assertEqual(persona['role'], 'identity')
        with self.assertRaises(RuleError) as caught:
            app.action('task.create', {'kind': 'sync', 'authority': soul, 'targets': []})
        self.assertEqual(caught.exception.code, 'identity_sync')
        unsupported = [p for p in products.values() if not p['supported']]
        self.assertEqual(len(unsupported), 6)
        self.assertTrue(all(p['installation_status'] == 'unsupported' for p in unsupported))
        child = next(x for x in report['project_rules']['files'] if x['path'] == str(self.project/'sub/AGENTS.md'))
        self.assertEqual(child['scope'], str(self.project/'sub'))

    def test_current_codex_named_profile_files_are_reported_without_assuming_active_session(self):
        home = self.base/'home'
        profile = self.put(home/'.codex/work.config.toml', 'model="review-model"\n')
        app = Maintenance(self.base/'personal', self.project, environment={})
        report = app.action('discover.scan', {'home':str(home),'probe_system':False})
        codex = next(x for x in report['products'] if x['id']=='codex')
        self.assertIn('work', {x['name'] for x in codex['profiles']})
        self.assertIn({'profile':'work','id':'review-model','source':profile}, codex['models']['configured'])
        self.assertIsNone(codex['models']['current']['id'])

    def test_unreadable_installation_does_not_abort_other_products(self):
        home = self.base/'home'
        binaries = self.base/'bin'
        denied = self.put(binaries/('codex.exe' if os.name == 'nt' else 'codex'))
        self.put(binaries/('claude.exe' if os.name == 'nt' else 'claude'))
        actual_stat = os.stat
        def guarded_stat(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(denied):
                raise PermissionError('isolated unreadable installation')
            return actual_stat(path, *args, **kwargs)
        app = Maintenance(self.base/'personal', self.project, environment={'PATH':str(binaries)})
        with patch('os.stat', side_effect=guarded_stat):
            report = app.action('discover.scan', {'home':str(home),'probe_system':False})
        products = {x['id']: x for x in report['products']}
        self.assertEqual(products['codex']['installation_status'], 'unreadable')
        self.assertEqual(products['claude-code']['installation_status'], 'found')
        self.assertTrue(report['errors'])
        self.assertEqual(len([x for x in report['products'] if x['supported']]), 5)

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher format')
    def test_simple_command_launcher_and_executable_are_one_installation_with_two_entrances(self):
        home = self.base/'home'
        local = home/'AppData/Local'
        executable = self.put(local/'Programs/OpenAI/Codex/bin/codex.exe')
        launcher = self.put(self.base/'bin/codex.cmd', '@echo off\n"%LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe" %*\n')
        app = Maintenance(self.base/'personal', self.project, environment={'PATH':str(self.base/'bin'), 'LOCALAPPDATA':str(local)})
        report = app.action('discover.scan', {'home':str(home),'probe_system':False})
        codex = next(x for x in report['products'] if x['id']=='codex')
        self.assertEqual(len(codex['installations']), 1)
        installation = codex['installations'][0]
        self.assertEqual({x['path'] for x in installation['entrances']}, {launcher,executable})
        self.assertEqual(installation['entity'], executable)
        self.assertEqual(installation['runnable'], 'unverified')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_bounded_discovery_does_not_follow_nested_rule_or_profile_junctions(self):
        import _winapi
        home = self.base/'home'
        outside = self.base/'outside'
        self.put(outside/'extra.md', 'Outside the detected rules root')
        self.put(outside/'SOUL.md', 'Outside the detected profile root')
        rules = home/'.claude/rules'
        profiles = home/'.hermes/profiles'
        rules.mkdir(parents=True)
        profiles.mkdir(parents=True)
        _winapi.CreateJunction(str(outside), str(rules/'redirect'))
        _winapi.CreateJunction(str(outside), str(profiles/'redirect'))
        app = Maintenance(self.base/'personal', self.project, environment={'HERMES_HOME':str(home/'.hermes')})
        report = app.action('discover.scan', {'home':str(home),'probe_system':False})
        products = {x['id']:x for x in report['products']}
        self.assertNotIn('redirect', {p['name'] for p in products['hermes']['profiles']})
        self.assertFalse(any('redirect' in r['path'] for r in products['claude-code']['rules']))


if __name__ == '__main__':
    unittest.main()
