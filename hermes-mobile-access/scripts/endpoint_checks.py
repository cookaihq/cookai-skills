"""Bounded checks of this exact bridge over the public HTTPS entry, without chat."""
import asyncio
from datetime import datetime, timezone
import secrets

import aiohttp

from access_security import KEY_HEADER
from native_control import ControlError


async def check_chat_methods(ws, profile):
    """Check dispatch with a random nonexistent session, without reading chats.

    A precise not_found proves the method reached session authorization. A
    missing method, permission error or fabricated success must fail preflight.
    This is capability evidence, not a successful Android conversation.
    """
    session_id = 'hma-probe-' + secrets.token_hex(24)
    pending = {'hma-usage-check': 'session.usage',
               'hma-context-check': 'session.context_breakdown'}
    async with asyncio.timeout(8):
        for rid, method in pending.items():
            # Use the actual Mobile serializer shape (default jsonrpc omitted),
            # not a hand-written explicit-version frame that masks bypasses.
            await ws.send_json({'id': rid, 'method': method,
                               'params': {'session_id': session_id, 'profile': profile}})
        while pending:
            reply = await ws.receive_json()
            if 'method' in reply or reply.get('id') not in pending:
                continue
            method = pending.pop(reply['id'])
            error = reply.get('error', {})
            if (error.get('code') != 4001 or error.get('data', {}).get('reason') != 'not_found'
                    or 'result' in reply):
                raise ControlError('chat_usage_method_unavailable' if method == 'session.usage'
                                   else 'chat_context_method_unavailable')
    return {'usage_method': 'session.stats', 'context_method': 'session.context_breakdown',
            'verification': 'nonexistent_session_refused', 'session_created': False}


async def check_endpoint(bridge, url, *, protocol=False):
    timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)
    trace = aiohttp.TraceConfig()

    async def no_redirect(session, context, params):
        params.response.close()
        raise ControlError('public_redirect_refused')

    trace.on_request_redirect.append(no_redirect)
    headers = {KEY_HEADER: bridge.key}
    results = {}
    async with asyncio.timeout(45):
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(), trace_configs=[trace]) as client:
            client._retry_connection = False
            if not protocol:
                for label, path, extra, expected in (
                    ('anonymous_root', '', {}, 401),
                    ('anonymous_api', 'api/profiles', {}, 401),
                    ('anonymous_ws', 'api/ws', {}, 401),
                    ('anonymous_file', 'api/files/download', {}, 401),
                    ('wrong_key', '', {KEY_HEADER: secrets.token_urlsafe(32)}, 401),
                    ('browser_origin', '', {**headers, 'Origin': url.rstrip('/')}, 403)):
                    async with client.get(url + path, headers=extra, allow_redirects=False) as response:
                        results[label] = response.status
                        if response.status != expected:
                            raise ControlError('public_access_gate_failed')
            async with client.get(url, headers=headers, allow_redirects=False) as response:
                raw = await response.content.read(4096)
                if response.status != 200 or ('"' + bridge.marker + '"').encode() not in raw:
                    raise ControlError('public_endpoint_identity_failed')
                results['https_bridge_identity'] = True
            if protocol:
                async with client.get(url + 'api/profiles', headers=headers,
                        params={'profile': bridge.backend.preferred}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ControlError('public_rest_failed')
                    raw = await response.content.read(65537)
                    if len(raw) > 65536:
                        raise ControlError('public_rest_too_large')
                    import json
                    roster = json.loads(raw)
                    if bridge.backend.preferred not in [row['name'] for row in roster['profiles']]:
                        raise ControlError('public_profile_missing')
                    results['rest_profile'] = bridge.backend.preferred
                async with client.ws_connect(url.replace('https:', 'wss:', 1) + 'api/ws?token=' + bridge.marker,
                        headers=headers, timeout=aiohttp.ClientWSTimeout(ws_receive=8, ws_close=1),
                        max_msg_size=1024 * 1024) as ws:
                    ready = await ws.receive_json()
                    if ready.get('params', {}).get('type') != 'gateway.ready':
                        raise ControlError('public_ws_ready_failed')
                    await ws.send_json({'jsonrpc': '2.0', 'id': 'hma-check',
                                        'method': 'gateway.ping', 'params': {}})
                    async with asyncio.timeout(8):
                        while True:
                            reply = await ws.receive_json()
                            if reply.get('id') == 'hma-check':
                                break
                    if reply.get('result', {}).get('ok') is not True:
                        raise ControlError('public_ws_ping_failed')
                    results['websocket_ping'] = True
                    results['chat_methods'] = await check_chat_methods(ws, bridge.backend.preferred)
    return {**results, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'source': 'desktop_via_public_https', 'chat': 'not_tested', 'android': 'not_tested'}
