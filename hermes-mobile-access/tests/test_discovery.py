import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from discovery import (Listener, Observation, ObservationFailure, Process, backend_argv, candidates,
                       discover, health_summary, probe_live)


HEALTH = {'status': 200, 'body': {'ok': True, 'version': '0.12.0',
                                'commit': 'a' * 40, 'auth_required': False}}


def snapshot(*, birth=20., port=43127, unrelated=False, opaque=False):
    return Observation((Process(10, 1, 10., '/Applications/Hermes.app/Hermes', desktop=True),
                        Process(11, 1 if unrelated else 10, birth, '/python', backend_hint=not opaque)),
                       (Listener(11, '127.0.0.1', port),))


class DiscoveryTests(unittest.TestCase):
    def test_desktop_child_opaque_launcher_requires_protocol(self):
        report = discover(lambda: snapshot(opaque=True), lambda *args: HEALTH)
        self.assertEqual(report['state'], 'backend_discovered')
        self.assertEqual(report['android'], 'not_checked')
        self.assertEqual(report['publication'], 'not_performed')

    def test_other_python_service_is_not_probed(self):
        def forbidden(*args):
            self.fail('Unrelated listener was probed')
        result = discover(lambda: snapshot(unrelated=True), forbidden)
        self.assertEqual(result['state'], 'desktop_backend_not_found')

    def test_pid_reuse_during_request_invalidates_result(self):
        observations = iter([snapshot(), snapshot(birth=30.)])
        result = discover(lambda: next(observations), lambda *args: HEALTH)
        self.assertIsNone(result['selected'])
        self.assertEqual(result['candidates'][0]['reason'], 'instance_changed_during_probe')

    def test_old_port_replaced_by_other_program_is_not_recovered(self):
        observations = iter([snapshot(), snapshot(unrelated=True)])
        result = discover(lambda: next(observations), lambda *args: HEALTH)
        self.assertEqual(result['state'], 'backend_unproven')

    def test_http_200_is_not_hermes_evidence(self):
        result = discover(lambda: snapshot(), lambda *args: {'status': 200, 'body': {'ok': True}})
        self.assertIsNone(result['selected'])
        self.assertEqual(result['state'], 'backend_unproven')

    def test_explicit_selection_cannot_reuse_stale_fingerprint(self):
        old = candidates(snapshot())[0]['id']
        result = discover(lambda: snapshot(birth=21.), lambda *args: HEALTH, selected=old)
        self.assertEqual(result['state'], 'selection_stale_or_unverified')

    def test_multiple_backends_require_selection(self):
        before = snapshot()
        many = Observation(before.processes + (Process(12, 10, 22., '/python'),),
                           before.listeners + (Listener(12, '127.0.0.1', 43128),))
        report = discover(lambda: many, lambda *args: HEALTH)
        self.assertEqual(report['state'], 'selection_required')
        selected = report['candidates'][1]['id']
        self.assertEqual(discover(lambda: many, lambda *args: HEALTH, selected=selected)['selected'], selected)

    def test_no_auto_selection_if_other_candidate_could_not_be_inspected(self):
        obs = snapshot()
        restricted = Observation(obs.processes, obs.listeners, issues=('candidate_sockets_access_denied',))
        self.assertEqual(discover(lambda: restricted, lambda *args: HEALTH)['state'], 'selection_required')

    def test_health_drops_secrets_and_validates_types(self):
        body = dict(HEALTH['body'], session_token='synthetic-secret')
        self.assertNotIn('synthetic-secret', json.dumps(health_summary(body)))
        self.assertIsNone(health_summary(dict(body, auth_required='false')))

    def test_network_probe_refuses_non_loopback(self):
        self.assertEqual(probe_live('192.0.2.1', 80, .1)['error'], 'non_loopback_probe_refused')

    def test_cli_hint_does_not_match_inline_code_or_generic_python(self):
        self.assertTrue(backend_argv(['python', '-m', 'hermes_cli.main', 'serve']))
        self.assertFalse(backend_argv(['python', '-c', 'print("hermes serve")']))
        self.assertFalse(backend_argv(['python', 'server.py', '--name', 'hermes']))

    def test_expired_budget_does_not_probe(self):
        def forbidden(*args):
            self.fail('Budget exhausted but network used')
        report = discover(lambda: snapshot(), forbidden, budget=0)
        self.assertIn('discovery_budget_exhausted', report['issues'])

    def test_ipv4_and_ipv6_same_port_are_not_same_desktop_target(self):
        obs = snapshot(unrelated=True)
        ipv6 = Observation(obs.processes, obs.listeners, ((10, '::1', 43127),))
        self.assertEqual(candidates(ipv6), [])
        ipv4 = Observation(obs.processes, obs.listeners, ((10, '127.0.0.1', 43127),))
        self.assertEqual(len(candidates(ipv4)), 1)

    def test_desktop_attached_shared_runtime_with_opaque_argv(self):
        obs = snapshot(unrelated=True, opaque=True)
        linked = Observation(obs.processes, obs.listeners, ((10, '127.0.0.1', 43127),))
        self.assertEqual(discover(lambda: linked, lambda *args: HEALTH)['state'], 'backend_discovered')

    def test_os_scan_timeout_cannot_return_old_success(self):
        calls = 0
        def observe():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ObservationFailure('os_observation_timeout')
            return snapshot()
        report = discover(observe, lambda *args: HEALTH)
        self.assertEqual(report['state'], 'discovery_blocked')
        self.assertIsNone(report['selected'])

    def test_real_probe_preserves_health_contract_and_discards_secrets(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(dict(HEALTH['body'], token='synthetic-secret')).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = probe_live('127.0.0.1', server.server_port, 3)
            self.assertIsNotNone(health_summary(response.get('body')))
            self.assertNotIn('synthetic-secret', json.dumps(response))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_probe_does_not_follow_http_redirects(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/private')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = probe_live('127.0.0.1', server.server_port, 3)
            self.assertEqual(response['status'], 302)
            self.assertEqual(paths, ['/api/health'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
