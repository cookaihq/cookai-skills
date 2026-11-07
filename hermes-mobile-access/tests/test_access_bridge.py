"""Exercise the published protocol over real loopback TCP, without Hermes/FRP."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys

import aiohttp
from aiohttp import web, WSMsgType
from multidict import CIMultiDict
import pytest
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from access_bridge import AccessBridge
from access_security import KEY_HEADER, generate_key
from native_control import ControlError


class Gateway:
    """Local fixture redeems grants and exposes what reached the transport."""
    def __init__(self):
        self.preferred = 'work'
        self.grants = {}
        self.requests = []
        self.frames = []
        self.checked = self.verified = 0
        self.peer_failure = self.identity_failure = False
        self.redirect = self.stream = False
        self.stream_abort = False
        self.disconnect_after_grant = False
        self.file_status = 206
        self.pending_verify = None
        self.stream_release = asyncio.Event()
        self.ws_closed = asyncio.Event()
        self.rpc_handler = None

    def profile_home(self, name):
        name = self.preferred if name is None else name
        if not isinstance(name, str) or name not in ('work', 'personal'):
            raise ControlError('profile_not_served')
        return '/fixture/' + name

    async def check(self):
        self.checked += 1
        if self.identity_failure:
            raise ControlError('backend_identity_changed')

    async def verify_socket(self, local, peer):
        if self.pending_verify:
            await self.pending_verify.wait()
        if self.peer_failure:
            raise ControlError('upstream_peer_owner_unverified')
        self.verified += 1

    async def mint(self, name, purpose):
        grant = generate_key()
        self.grants[grant] = (self.profile_home(name), purpose)
        return grant

    async def handle(self, request):
        self.requests.append((request.method, request.path_qs, dict(request.headers), await request.read()))
        if self.redirect:
            return web.Response(status=307, headers={'Location': '/fixture-redirect-target'})
        if request.path == '/api/ws':
            raw = request.headers.get('Sec-WebSocket-Protocol', '')
            grant = raw.split('hermes-gateway-ticket.', 1)[-1]
            assert self.grants.pop(grant)[1] == 'interactive'
            ws = web.WebSocketResponse(protocols=['hermes-gateway-v1'])
            await ws.prepare(request)
            await ws.send_json({'method': 'gateway.event', 'params': {'type': 'gateway.ready'}})
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    frame = json.loads(msg.data)
                    self.frames.append(frame)
                    if self.rpc_handler:
                        await self.rpc_handler(ws, frame)
                    else:
                        await ws.send_json({'jsonrpc': '2.0', 'id': frame.get('id'), 'result': {'ok': True}})
            finally:
                self.ws_closed.set()
            return ws
        grant = request.headers.get('X-Hermes-Gateway-Ticket')
        home, purpose = self.grants.pop(grant)
        assert purpose == 'native-http'
        if self.disconnect_after_grant:
            request.transport.abort()
            return web.Response()
        if request.path in ('/api/files/download', '/api/files/stream'):
            if self.file_status == 403:
                return web.json_response({'detail': 'File is not readable'}, status=403)
            return web.Response(body=b'file-bytes', status=self.file_status,
                headers={'Content-Range': 'bytes 0-9/100', 'Accept-Ranges': 'bytes',
                         'Content-Disposition': 'inline; filename="test.wav"'})
        if self.stream:
            response = web.StreamResponse(headers={'Content-Length': '100'} if self.stream_abort else {})
            await response.prepare(request)
            await response.write(b'begin\n')
            await self.stream_release.wait()
            if self.stream_abort:
                request.transport.close()
            return response
        return web.json_response({'home': home, 'ok': True}, headers={'Set-Cookie': 'not-for-phone=1'})


@asynccontextmanager
async def setup_bridge():
    upstream = Gateway()
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', upstream.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.2)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    upstream.origin = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    active = {'current': True}
    bridge = AccessBridge('https://mobile.example.invalid/', generate_key(), upstream, lambda: active['current'])
    try:
        await bridge.start()
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=4)) as client:
            yield bridge, upstream, client, active
    finally:
        upstream.stream_release.set()
        await asyncio.wait_for(bridge.close(), 5)
        await runner.cleanup()


def headers(bridge):
    return {'Host': bridge.host, KEY_HEADER: bridge.key}


def url(bridge, path='/'):
    return URL(f'http://127.0.0.1:{bridge.port}' + path, encoded=True)


async def connect_ws(bridge, client, extra=None):
    return await client.ws_connect(url(bridge, '/api/ws?token=' + bridge.marker),
        headers=extra or headers(bridge), timeout=aiohttp.ClientWSTimeout(ws_receive=2, ws_close=0.2))


def test_authentication_and_path_gates_never_reach_gateway():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            expected = [
                ('/', {'Host': bridge.host}, 401),
                ('/', {**headers(bridge), KEY_HEADER: generate_key()}, 401),
                ('/', {**headers(bridge), 'Host': 'foreign.invalid'}, 403),
                ('/', {**headers(bridge), 'Origin': ''}, 403),
                ('/', CIMultiDict([*headers(bridge).items(), (KEY_HEADER, bridge.key)]), 401),
                ('/api/config', headers(bridge), 403),
                ('/api/%2e%2e/config', headers(bridge), 400),
                ('/api/files%2Fread', headers(bridge), 400),
                ('/api/profiles?token=marker', headers(bridge), 400),
                ('/api/profiles?profile=work&profile=personal', headers(bridge), 422),
                ('/api/profiles?profile=unknown', headers(bridge), 422)]
            for path, sent, status in expected:
                async with client.get(url(bridge, path), headers=sent, allow_redirects=False) as response:
                    assert response.status == status
                    assert bridge.key not in await response.text()
            assert not upstream.requests and not upstream.grants
    asyncio.run(run())


def test_rest_translates_one_use_grants_keeps_profile_and_strips_untrusted_headers():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            sent = {**headers(bridge), 'Authorization': 'Bearer untrusted', 'Cookie': 'cookie=untrusted',
                    'X-Hermes-Gateway-Ticket': 'forged', 'X-Forwarded-Host': 'elsewhere.invalid',
                    'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1', 'Range': 'bytes=0-10'}
            for query, expected in [('', '/fixture/work'), ('?profile=personal', '/fixture/personal')]:
                async with client.get(url(bridge, '/api/profiles' + query), headers=sent) as response:
                    assert response.status == 200
                    assert (await response.json())['home'] == expected
                    assert 'Set-Cookie' not in response.headers
            assert not upstream.grants and upstream.verified == 2
            for _, _, received, _ in upstream.requests:
                assert received['Range'] == 'bytes=0-10'
                assert received['Host'] == upstream.origin.removeprefix('http://')
                assert received['X-Hermes-Gateway-Ticket'] != 'forged'
                assert not any(name in received for name in (KEY_HEADER, 'Authorization', 'Cookie',
                    'X-Forwarded-Host', 'X-Forwarded-For', 'Forwarded', 'Origin'))
    asyncio.run(run())


@pytest.mark.parametrize('websocket', [False, True])
def test_redirects_are_not_followed(websocket):
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.redirect = True
            if websocket:
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await connect_ws(bridge, client)
            else:
                async with client.get(url(bridge, '/api/profiles'), headers=headers(bridge)) as response:
                    assert response.status == 502
            assert len(upstream.requests) == 1
    asyncio.run(run())


def test_peer_ownership_failure_sends_no_http_credentials_or_data():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.peer_failure = True
            async with client.post(url(bridge, '/api/files/upload'), headers=headers(bridge), data=b'private-file') as response:
                assert response.status == 503
            assert not upstream.requests
            assert bridge.reason is not None
    asyncio.run(run())


def test_websocket_requires_both_key_and_marker_and_preserves_rpc():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            for path, sent in [('/api/ws?token=' + bridge.marker, {'Host': bridge.host}),
                               ('/api/ws?token=wrong', headers(bridge)),
                               ('/api/ws?token=' + bridge.marker, {**headers(bridge), 'Sec-WebSocket-Protocol': 'forged'})]:
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect(url(bridge, path), headers=sent)
                assert error.value.status == 401
            assert not upstream.requests
            async with await connect_ws(bridge, client) as ws:
                assert (await ws.receive_json())['params']['type'] == 'gateway.ready'
                frame = {'jsonrpc': '2.0', 'id': 'read', 'method': 'gateway.ping', 'params': {'profile': 'personal'}}
                await ws.send_json(frame)
                assert (await ws.receive_json())['result'] == {'ok': True}
                assert upstream.frames == [frame]
            await asyncio.wait_for(upstream.ws_closed.wait(), 2)
    asyncio.run(run())


@pytest.mark.parametrize('frame', [
    {'method': 'client.capabilities', 'params': {'desktop_local': True}},
    {'method': 'gateway.ping', 'params': {'profile': 'unserved'}},
    [],
])
def test_ws_invalid_capability_or_profile_never_reaches_gateway(frame):
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                await ws.send_json(frame)
                assert (await ws.receive()).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
            assert not upstream.frames
    asyncio.run(run())


def test_revocation_closes_existing_ws_and_admission_even_when_callers_cancel():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, active):
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                callers = [asyncio.create_task(bridge.revoke('rotation')) for _ in range(4)]
                await asyncio.sleep(0)
                callers[0].cancel()
                await asyncio.wait_for(asyncio.gather(*callers, return_exceptions=True), 3)
                assert (await ws.receive()).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                await asyncio.wait_for(upstream.ws_closed.wait(), 2)
            async with client.get(url(bridge, '/api/profiles'), headers=headers(bridge)) as response:
                assert response.status == 401
            assert not bridge.sockets and not bridge.upstream_sockets
    asyncio.run(run())


def test_configuration_change_closes_idle_ws_without_another_request(monkeypatch):
    import access_bridge
    monkeypatch.setattr(access_bridge, 'MONITOR_INTERVAL', 0.02)
    async def run():
        async with setup_bridge() as (bridge, upstream, client, active):
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                active['current'] = False
                assert (await ws.receive()).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
            assert bridge.reason == 'binding_unavailable'
    asyncio.run(run())


def test_revoke_interrupts_inflight_http_stream():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.stream = True
            async with client.get(url(bridge, '/api/files/read'), headers=headers(bridge)) as response:
                assert await response.content.readline() == b'begin\n'
                await asyncio.wait_for(bridge.revoke(), 3)
                with pytest.raises((aiohttp.ClientError, ConnectionError)):
                    await asyncio.wait_for(response.read(), 2)
    asyncio.run(run())


def test_disconnected_http_client_does_not_revoke_other_mobile_connections():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.stream = True
            response = await client.get(url(bridge, '/api/files/read'), headers=headers(bridge))
            assert await response.content.readline() == b'begin\n'
            response.close()
            upstream.stream_release.set()
            await asyncio.sleep(0.05)
            assert bridge.reason is None
    asyncio.run(run())


def test_mobile_media_marker_removed_range_preserved_and_policy_refusal_is_local():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            path = '/api/files/stream?path=C%3A%2Ftest%20file.wav&token=' + bridge.marker
            async with client.get(url(bridge, path), headers={**headers(bridge), 'Range': 'bytes=0-9'}) as response:
                assert response.status == 206 and await response.read() == b'file-bytes'
                assert response.headers['Content-Range'] == 'bytes 0-9/100'
                assert response.content_length == 10
            _, received_path, received_headers, _ = upstream.requests[-1]
            assert 'token' not in URL(received_path).query
            assert URL(received_path).query['path'] == 'C:/test file.wav'
            assert received_headers['Range'] == 'bytes=0-9'
            for extra in ('&token=duplicate', '&ticket=native-ticket'):
                async with client.get(url(bridge, path + extra), headers=headers(bridge)) as response:
                    assert response.status == 400
            # A URL alone confers no authority, including in an external browser.
            async with client.get(url(bridge, path), headers={'Host': bridge.host}) as response:
                assert response.status == 401
            upstream.file_status = 403
            async with client.get(url(bridge, path), headers=headers(bridge)) as response:
                assert response.status == 403
            assert bridge.reason is None
    asyncio.run(run())


def test_session_ids_with_slashes_and_valid_json_upload_keep_scope():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            async with client.get(url(bridge, '/api/sessions/mobile/fixture-123/messages?profile=personal'),
                                  headers=headers(bridge)) as response:
                assert response.status == 200
            async with client.post(url(bridge, '/api/files/upload?profile=personal'),
                    headers=headers(bridge), json={'profile': 'work', 'data': 'fixture'}) as response:
                assert response.status == 422
            assert len(upstream.requests) == 1
            body = {'profile': 'personal', 'path': 'fixture', 'data': 'aGVsbG8='}
            async with client.post(url(bridge, '/api/files/upload'), headers=headers(bridge), json=body) as response:
                assert response.status == 200
                assert (await response.json())['home'] == '/fixture/personal'
            assert json.loads(upstream.requests[-1][3]) == body
    asyncio.run(run())


def test_ws_admission_limit_includes_handshakes_in_progress(monkeypatch):
    import access_bridge
    monkeypatch.setattr(access_bridge, 'MAX_SOCKETS', 1)
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.pending_verify = asyncio.Event()
            connecting = asyncio.create_task(connect_ws(bridge, client))
            async with asyncio.timeout(2):
                while not bridge.ws_admitted:
                    await asyncio.sleep(0.005)
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await connect_ws(bridge, client)
            assert caught.value.status == 503
            upstream.pending_verify.set()
            ws = await connecting
            await ws.receive_json()
            await ws.close()
            async with asyncio.timeout(2):
                while bridge.ws_admitted:
                    await asyncio.sleep(0.005)
            assert len(upstream.requests) == 1
    asyncio.run(run())


def test_failed_upstream_stream_terminates_body_instead_of_appending_error():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.stream = upstream.stream_abort = True
            async with client.get(url(bridge, '/api/files/read'), headers=headers(bridge)) as response:
                assert await response.content.readline() == b'begin\n'
                upstream.stream_release.set()
                with pytest.raises(aiohttp.ClientPayloadError):
                    await response.read()
            assert bridge.reason == 'upstream_stream_failed'
    asyncio.run(run())


def test_connection_loss_after_grant_redemption_does_not_replay_http():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            upstream.disconnect_after_grant = True
            async with client.get(url(bridge, '/api/profiles'), headers=headers(bridge)) as response:
                assert response.status == 503
                assert (await response.json())['error'] == 'upstream_unavailable'
            assert len(upstream.requests) == 1
            assert not upstream.grants
    asyncio.run(run())
