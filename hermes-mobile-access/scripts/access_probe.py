"""Bounded real local adapter validation, without FRP or chat mutations."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

import aiohttp
from aiohttp import WSMsgType

from access_bridge import AccessBridge
from access_security import AUTH_TYPE, KEY_HEADER, binding_digest
from configuration import ConfigStore, ConfigError
from frp_client import FrpClient
from native_backend import NativeBackend
from native_control import ControlError
from endpoint_checks import check_chat_methods


async def exercise(bridge):
    """Use the Mobile wire format, but only profile reads and non-chat RPCs."""
    observed = {}
    await bridge.start()
    base = f'http://127.0.0.1:{bridge.port}'
    headers = {'Host': bridge.host, KEY_HEADER: bridge.key}
    timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
            for label, extra, expected in [
                ('anonymous', {'Host': bridge.host}, 401),
                ('wrong_key', {'Host': bridge.host, KEY_HEADER: 'invalid'}, 401),
                ('foreign_host', {**headers, 'Host': 'wrong.invalid'}, 403),
                ('browser_origin', {**headers, 'Origin': 'https://' + bridge.host}, 403)]:
                async with client.get(base + '/', headers=extra, allow_redirects=False) as response:
                    observed[label] = response.status
                    if response.status != expected:
                        raise ControlError('access_gate_probe_failed')
            async with client.get(base + '/', headers=headers, allow_redirects=False) as response:
                raw = await response.content.read(4096)
                match = re.search(rb'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', raw)
                if response.status != 200 or match is None:
                    raise ControlError('mobile_bootstrap_probe_failed')
                marker = match[1].decode('ascii')
            async with client.get(base + '/api/profiles', headers=headers, allow_redirects=False) as response:
                if not bridge.policy.http_allowed('GET', '/api/profiles'):
                    if response.status != 403 or (await response.json()).get('error') != 'access_policy_denied':
                        raise ControlError('profile_policy_probe_failed')
                    observed['profiles'] = 'disabled_by_policy'
                elif response.status != 200:
                    raise ControlError('profile_rest_probe_failed')
                else:
                    data = await response.json()
                    observed['profile_count'] = len(data['profiles'])
                    if bridge.backend.preferred not in [row['name'] for row in data['profiles']]:
                        raise ControlError('preferred_profile_not_returned')
            ws_url = base.replace('http:', 'ws:', 1) + '/api/ws?token=' + marker
            try:
                async with client.ws_connect(ws_url, headers={'Host': bridge.host}):
                    raise ControlError('anonymous_ws_accepted')
            except aiohttp.WSServerHandshakeError as exc:
                observed['anonymous_ws'] = exc.status
                if exc.status != 401:
                    raise ControlError('ws_gate_probe_failed')
            if not bridge.policy.websocket_enabled:
                async with client.get(ws_url.replace('ws:', 'http:', 1), headers=headers) as response:
                    if response.status != 403 or (await response.json()).get('error') != 'access_policy_denied':
                        raise ControlError('ws_policy_probe_failed')
                    observed['websocket'] = 'disabled_by_policy'
                await bridge.revoke('probe_finished')
                observed['test_socket_closed'] = True
            else:
                await exercise_websocket(client, ws_url, headers, bridge, observed)
            async with client.get(base + '/api/profiles', headers=headers, allow_redirects=False) as response:
                observed['revoked_test_instance'] = response.status == 401
        required = ['test_socket_closed', 'revoked_test_instance']
        if bridge.policy.websocket_enabled:
            required += ['gateway_ready', 'heartbeat']
        if not all(observed.get(name) for name in required):
            raise ControlError('access_protocol_probe_failed')
        return observed
    finally:
        await bridge.close()


async def exercise_websocket(client, ws_url, headers, bridge, observed):
    async with client.ws_connect(ws_url, headers=headers,
            timeout=aiohttp.ClientWSTimeout(ws_receive=8, ws_close=1)) as ws:
        ready = await ws.receive_json()
        observed['gateway_ready'] = ready.get('params', {}).get('type') == 'gateway.ready'
        await ws.send_json({'jsonrpc': '2.0', 'id': 'hma-probe', 'method': 'gateway.ping', 'params': {}})
        async with asyncio.timeout(8):
            while True:
                response = await ws.receive_json()
                if response.get('id') == 'hma-probe':
                    break
        observed['heartbeat'] = response.get('result', {}).get('ok') is True
        observed['chat_methods'] = await check_chat_methods(ws, bridge.backend.preferred, bridge.policy)
        await bridge.revoke('probe_finished')
        end = await ws.receive()
        observed['test_socket_closed'] = end.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


def worker(options):
    store = ConfigStore(options['project'], home=options['home'], use_local_key=options['use_local_key'])
    snapshot = store.snapshot()
    cid, record = snapshot.select(options.get('connection'))
    if not record['auth'] or record['auth']['type'] != AUTH_TYPE:
        raise ControlError('access_key_required')
    expected = binding_digest(snapshot.source, cid, record)

    def unchanged():
        latest = store.snapshot()
        _, actual = latest.select(cid)
        return binding_digest(latest.source, cid, actual) == expected

    frp = record['frp']
    context = FrpClient(options['frp_skill'], frp.get('project') or options['project'],
                        frp.get('home') or options['frp_home']).context()
    suffix = context['domain_suffix']
    if suffix.get('status') != 'configured':
        raise ControlError('frp_parent_required')
    if frp.get('source_binding') != context.get('source_binding'):
        raise ControlError('frp_source_changed')
    url = 'https://' + record['domain_prefix'] + '.' + suffix['value'] + '/'
    backend = NativeBackend.discover(record['hermes'], options.get('selected'))
    bridge = AccessBridge(url, record['auth']['key'], backend, unchanged, access=record.get('access'))
    observed = asyncio.run(asyncio.wait_for(exercise(bridge), 45))
    return {'result': 'access_probe_passed', 'connection': cid, 'server_url': url,
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'candidate': backend.candidate['id'], 'version': backend.candidate['version'],
            'commit': backend.candidate['commit'], 'preferred_profile': backend.preferred,
            'observed': observed, 'scope': 'loopback_mobile_auth_translation',
            'listener': 'stopped', 'publication': 'not_performed', 'chat': 'not_performed',
            'media': 'not_checked', 'android': 'not_checked', 'persistent_key_change': 'not_performed'}


def probe_access(options):
    try:
        child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
            input=json.dumps(options).encode(), capture_output=True, timeout=100,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = json.loads(child.stdout)
        if not isinstance(result, dict) or (child.returncode and result.get('result') == 'access_probe_passed'):
            raise ValueError()
        return result
    except subprocess.TimeoutExpired:
        return {'result': 'access_probe_unavailable', 'reason': 'probe_timeout', 'publication': 'not_performed'}
    except (OSError, ValueError):
        return {'result': 'access_probe_unavailable', 'reason': 'probe_worker_failed', 'publication': 'not_performed'}


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = worker(json.loads(sys.stdin.buffer.read(65537)))
    except (ControlError, ConfigError) as exc:
        result = {'result': 'access_probe_unavailable', 'reason': exc.code if isinstance(exc, ConfigError) else str(exc),
                  'publication': 'not_performed'}
    except Exception as exc:
        result = {'result': 'access_probe_unavailable', 'reason': 'local_probe_failed',
                  'error_type': type(exc).__name__, 'publication': 'not_performed'}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get('result') == 'access_probe_passed' else 4)
