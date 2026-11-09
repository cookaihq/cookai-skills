"""Verify policy effects at transport boundaries, not only catalog lookups."""
import asyncio
import copy
import json

import aiohttp
import pytest

from test_access_bridge import setup_bridge, headers, url, connect_ws
from access_policy import AccessPolicy, WebSocketPolicy, GROUP_IDS, normalize_access
from access_security import binding_digest
from configuration import draft, validate_record, ConfigError
from endpoint_checks import check_endpoint, check_chat_methods
from access_probe import exercise


@pytest.mark.parametrize('access', [None, {'http': {'cron': 'false'}}, {'websocket': {'unknown': True}},
    {'websocket_enabled': 0}, {'http': None}, {'http': {'cron': False, 'models': 1}}])
def test_invalid_persisted_policy_is_rejected(access):
    row = draft()
    row['access'] = access
    with pytest.raises(ConfigError, match='布尔开关'):
        validate_record(row)


def test_existing_connections_default_all_enabled_and_false_is_effective():
    row = draft()
    del row['access']
    validated = validate_record(row)
    assert all(normalize_access(validated['access'])['http'].values())
    before = binding_digest('source', 'connection', validated)
    changed = copy.deepcopy(validated)
    changed['access'] = {'http': {'cron': False}, 'websocket': {'cron': False}}
    assert not normalize_access(changed['access'])['http']['cron']
    assert before != binding_digest('source', 'connection', changed)
    changed['access'] = {'http': {key: True for key in GROUP_IDS}}
    assert before == binding_digest('source', 'connection', changed)


def test_default_http_serves_mobile_management_and_unknown_business_routes():
    async def run():
        async with setup_bridge() as (bridge, upstream, client, _):
            for method, route in [('GET','/api/config/schema'), ('POST','/api/cron/jobs/fixture/trigger'),
                    ('DELETE','/api/mcp/servers/fixture'), ('PATCH','/api/plugins/kanban/tasks/fixture'),
                    ('PUT','/api/model/moa'), ('GET','/api/future-mobile-page')]:
                async with client.request(method, url(bridge, route), headers=headers(bridge), json={'fixture': True}) as response:
                    assert response.status == 200
                assert upstream.requests[-1][:2] == (method, route)
            assert upstream.verified == 6
    asyncio.run(run())


def test_http_denial_is_before_body_and_native_grant_with_segment_boundaries():
    async def run():
        async with setup_bridge({'http': {'configuration': False, 'other': False}}) as (bridge, upstream, client, _):
            for route in ['/api/config', '/api/config/raw', '/api/env/reveal', '/api/credentials/pool',
                          '/api/configuration', '/api/config-extra', '/api/auth/ws-ticket', '/not-api']:
                async with client.post(url(bridge, route), headers=headers(bridge), data=b'not-json') as response:
                    assert response.status == 403
            assert not upstream.requests and not upstream.grants
            async with client.get(url(bridge, '/api/cron/jobs'), headers=headers(bridge)) as response:
                assert response.status == 200
    asyncio.run(run())


def test_websocket_master_refuses_before_native_ticket_and_http_still_works():
    async def run():
        async with setup_bridge({'websocket_enabled': False}) as (bridge, upstream, client, _):
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await connect_ws(bridge, client)
            assert caught.value.status == 403
            assert not upstream.requests and not upstream.grants
            async with client.get(url(bridge, '/api/cron/jobs'), headers=headers(bridge)) as response:
                assert response.status == 200
    asyncio.run(run())


@pytest.mark.parametrize('method,group,params', [
    ('cron.manage','cron',{}), ('cron.submit','cron',{}), ('config.set','configuration',{}),
    ('session.usage','sessions',{}), ('prompt.submit','sessions',{}), ('plugins.manage','skills_plugins',{}),
    ('mcp.servers.test','tools_mcp',{}), ('image.attach_bytes','files_audio',{}),
    ('groups.send','messaging',{}), ('learning.edit','memory',{}), ('process.kill','maintenance',{}),
    ('model.save_key','configuration',{}), ('shell.exec','other',{}), ('future.execute','other',{}),
    ('session.mutate','models',{'operation':'model'}), ('session.automation','cron',{'kind':'loop'}),
    ('session.mutate','tools_mcp',{'operation':'edit_policy','payload':{'policy':'session'}}),
])
def test_disabled_rpc_and_notification_never_reach_native_backend(method, group, params):
    async def run():
        async with setup_bridge({'websocket': {group: False}}) as (bridge, upstream, client, _):
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                await ws.send_json({'id':'blocked','method':method,'params':params})
                refused = await ws.receive_json()
                assert refused['id'] == 'blocked'
                assert refused['error']['data']['reason'] == 'access_policy_denied'
                assert refused['error']['data']['group'] == group
                await ws.send_json({'method':method,'params':params})
                await ws.send_json({'id':'barrier','method':'gateway.ping','params':{}})
                assert (await ws.receive_json())['id'] == 'barrier'
                assert [frame['method'] for frame in upstream.frames] == ['gateway.ping']
    asyncio.run(run())


def test_live_events_replay_and_open_requests_obey_same_ws_policy():
    async def run():
        async with setup_bridge({'websocket': {'cron': False, 'configuration': False}}) as (bridge, upstream, client, _):
            denied_event = {'type':'cron.changed','payload':{'private':'not-for-mobile'}}
            allowed_event = {'type':'message.delta','session_id':'fixture','payload':{'text':'fixture'}}
            denied_request = {'id':'srq-secret','method':'secret','params':{'session_id':'fixture'}}
            allowed_request = {'id':'srq-clarify','method':'clarify','params':{'session_id':'fixture'}}
            async def handle(ws, frame):
                if frame.get('method') == 'session.events.since':
                    for event in (denied_event, allowed_event):
                        await ws.send_json({'method':'event','params':event})
                    await ws.send_json({'jsonrpc':'2.0','id':frame['id'],'result':{
                        'events':[denied_event, allowed_event], 'count':2, 'latest_seq':9,
                        'open_requests':[denied_request, allowed_request]}})
                elif frame.get('method') == 'gateway.ping':
                    await ws.send_json({'jsonrpc':'2.0','id':frame['id'],'result':{'ok':True}})
            upstream.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                await ws.send_json({'id':'replay','method':'session.events.since','params':{'session_id':'fixture'}})
                assert (await ws.receive_json())['params'] == allowed_event
                replay = await ws.receive_json()
                assert replay['result'] == {'events':[allowed_event],'count':1,'latest_seq':9,'open_requests':[allowed_request]}
                await ws.send_json({'jsonrpc':'2.0','id':'srq-clarify','result':{'answer':'fixture'}})
                await ws.send_json({'id':'barrier','method':'gateway.ping','params':{}})
                assert (await ws.receive_json())['id'] == 'barrier'
                assert any(f.get('id')=='srq-secret' and f.get('error',{}).get('data',{}).get('reason')=='access_policy_denied' for f in upstream.frames)
                assert any(f.get('id')=='srq-clarify' and 'result' in f for f in upstream.frames)
    asyncio.run(run())


def test_unknown_server_reply_and_unclassified_answer_cannot_bypass_restrictions():
    policy = WebSocketPolicy(AccessPolicy({'websocket': {'configuration': False}}))
    with pytest.raises(ValueError, match='unsolicited_rpc_reply'):
        policy.upload({'id':'unseen','result':{'value':'fixture'}})
    forwarded, refused = policy.upload({'id':1,'method':'request.answer','params':{'id':'unseen','result':{}}})
    assert forwarded is None and refused['error']['data']['reason'] == 'access_policy_denied'


@pytest.mark.parametrize('websocket', [True, False])
def test_endpoint_and_local_probes_treat_disabled_services_as_policy(websocket):
    async def run():
        access = {'http': {'profiles': False}, 'websocket_enabled': websocket,
                  'websocket': {'sessions': False}}
        async with setup_bridge(access) as (bridge, upstream, client, _):
            from aiohttp import web
            # Public probe uses the fixed HTTPS Host, even in this loopback test.
            async def forwarded(request):
                async with client.request(request.method, url(bridge, request.path_qs),
                        headers={**dict(request.headers), 'Host':bridge.host}) as response:
                    return web.Response(status=response.status, body=await response.read(), content_type=response.content_type)
            if websocket:
                # Exercise the RPC probe directly; proxy fixture only serves HTTP.
                async with await connect_ws(bridge, client) as ws:
                    await ws.receive_json()
                    result = await check_chat_methods(ws, 'work', bridge.policy)
                    assert result['verification'] == 'disabled_by_policy'
                    assert not upstream.frames
            else:
                app=web.Application();app.router.add_route('*','/{path:.*}',forwarded)
                runner=web.AppRunner(app,access_log=None);await runner.setup()
                site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
                try:
                    result=await check_endpoint(bridge,'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/',protocol=True)
                    assert result['rest_profile']=='disabled_by_policy' and result['websocket']=='disabled_by_policy'
                finally:
                    await runner.cleanup()
        async with setup_bridge(access, start=False) as (bridge, upstream, client, _):
            result = await exercise(bridge)
            assert result['profiles'] == 'disabled_by_policy'
            assert result['test_socket_closed'] and result['revoked_test_instance']
            if websocket:
                assert result['chat_methods']['verification'] == 'disabled_by_policy'
            else:
                assert result['websocket'] == 'disabled_by_policy'
    asyncio.run(run())
