"""Phone/native protocol regression over the bridge's actual WS transport."""
import asyncio

import pytest

from test_access_bridge import setup_bridge, connect_ws
from mobile_rpc import MAX_PENDING_RPC, MobileRpc, RpcRefusal, usage_snapshot
from endpoint_checks import check_chat_methods
from native_control import ControlError


STATS = {'input_tokens': 120, 'output_tokens': 30, 'reasoning_tokens': 5,
         'prompt_tokens': 200, 'completion_tokens': 35, 'total_tokens': 235,
         'api_calls': 2, 'cache_read_tokens': 80, 'cache_write_tokens': 10,
         'compression_count': 1, 'last_prompt_tokens': 200, 'context_length': 1000,
         'context_source': 'provider_usage', 'avg_latency_s': 1.24,
         'tokens_per_s': 24.36, 'active_subagents': 0, 'model': 'fixture-model'}
EXPECTED = {'input': 120, 'output': 30, 'reasoning': 5, 'prompt': 200,
            'completion': 35, 'total': 235, 'calls': 2, 'cache_read': 80,
            'cache_write': 10, 'compressions': 1, 'model': 'fixture-model',
            'context_used': 200, 'context_max': 1000, 'context_percent': 20,
            'context_source': 'provider_usage', 'context_estimated': False,
            'cache_hit_pct': 40, 'avg_latency_s': 1.2, 'avg_tps': 24.4,
            'active_subagents': 0}


def test_projection_preserves_known_figures_and_omits_unknown_cost_or_context():
    assert usage_snapshot(STATS) == EXPECTED
    assert usage_snapshot({'input_tokens': 0, 'output_tokens': 0,
                           'context_length': None, 'avg_latency_s': None}) == {'input': 0, 'output': 0}
    assert usage_snapshot({'prompt_tokens': 15, 'input_tokens': 0}) == {'prompt': 15, 'input': 15}
    assert usage_snapshot({'input_tokens': 1, 'active_subagents': True,
                           'tokens_per_s': float('nan')}) == {'input': 1}
    clamped = usage_snapshot({**STATS, 'last_prompt_tokens': 2000, 'context_source': 'estimate'})
    assert clamped['context_used'] == 1000 and clamped['context_percent'] == 100
    assert clamped['context_estimated'] is True


@pytest.mark.parametrize('explicit_version', [True, False])
def test_usage_rpc_correlates_out_of_order_replies_without_touching_events_or_other_calls(explicit_version):
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'session.usage',
                 'params': {'session_id': 'synthetic/a', 'profile': 'personal'}},
                {'jsonrpc': '2.0', 'id': '1', 'method': 'session.usage',
                 'params': {'session_id': 'synthetic/b', 'profile': 'work'}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'session.stats',
                 'params': {'session_id': 'synthetic/a', 'profile': 'personal'}},
            ]
            if not explicit_version:
                requests = [{key: value for key, value in row.items() if key != 'jsonrpc'} for row in requests]
            push = {'jsonrpc': '2.0', 'method': 'gateway.event', 'params': {
                'type': 'session.usage', 'payload': {'usage': {'input': 999}}}}
            server_call = {'jsonrpc': '2.0', 'id': 1, 'method': 'fixture.prompt', 'params': {}}

            async def handle(ws, frame):
                if len(backend.frames) == 3:
                    await ws.send_json(push)
                    await ws.send_json(server_call)
                    await ws.send_json({'jsonrpc': '2.0', 'id': 3, 'result': STATS})
                    await ws.send_json({'jsonrpc': '2.0', 'id': '1', 'result': {'input_tokens': 42}})
                    await ws.send_json({'jsonrpc': '2.0', 'id': 1, 'result': STATS})

            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                for frame in requests:
                    await ws.send_json(frame)
                assert await ws.receive_json() == push
                assert await ws.receive_json() == server_call
                assert await ws.receive_json() == {'jsonrpc': '2.0', 'id': 3, 'result': STATS}
                assert await ws.receive_json() == {'jsonrpc': '2.0', 'id': '1', 'result': {'input': 42}}
                assert await ws.receive_json() == {'jsonrpc': '2.0', 'id': 1, 'result': EXPECTED}
                assert backend.frames == [{**row, 'method': 'session.stats'} for row in requests]
    asyncio.run(run())


@pytest.mark.parametrize('reason,code', [('not_found', 4001), ('permission_denied', 4001),
                                      ('unknown_method', -32601)])
def test_usage_errors_are_preserved_and_request_id_can_be_reused(reason, code):
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            reply = {'jsonrpc': '2.0', 'id': 'usage', 'error': {
                'code': code, 'message': 'backend message', 'data': {'reason': reason}}}

            async def handle(ws, frame):
                await ws.send_json(reply)

            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                for _ in range(2):
                    await ws.send_json({'jsonrpc': '2.0', 'id': 'usage', 'method': 'session.usage',
                                       'params': {'session_id': 'synthetic', 'profile': 'personal'}})
                    assert await ws.receive_json() == reply
                assert len(backend.frames) == 2
    asyncio.run(run())


@pytest.mark.parametrize('invalid', [None, [], {}, {'input_tokens': 'unknown'}])
def test_malformed_stats_are_not_reported_as_empty_success(invalid):
    rpc = MobileRpc()
    rpc.upload({'jsonrpc': '2.0', 'id': 'a', 'method': 'session.usage'})
    reply = rpc.download({'jsonrpc': '2.0', 'id': 'a', 'result': invalid})
    assert reply['id'] == 'a' and 'result' not in reply
    assert reply['error']['data']['reason'] == 'usage_stats_invalid'
    assert not rpc.pending


def test_notifications_client_replies_and_other_connections_do_not_get_transformed():
    rpc = MobileRpc()
    notification = {'jsonrpc': '2.0', 'method': 'session.usage', 'params': {'profile': 'work'}}
    client_reply = {'jsonrpc': '2.0', 'id': 1, 'result': {'accepted': True}}
    assert rpc.upload(notification) is notification
    assert rpc.upload(client_reply) is client_reply
    rpc.upload({'jsonrpc': '2.0', 'id': 1, 'method': 'session.usage'})
    original = {'jsonrpc': '2.0', 'id': 1, 'result': STATS}
    assert MobileRpc().download(original) is original
    assert rpc.download(original)['result'] == EXPECTED


def test_diagnostics_correlate_failures_without_logging_ids_params_or_backend_messages(caplog):
    rpc = MobileRpc()
    with caplog.at_level('INFO', logger='hermes_mobile_access.rpc'):
        rpc.upload({'id': 'private-request', 'method': 'session.resume',
                    'params': {'session_id': 'private-stored', 'profile': 'private-profile'}})
        rpc.download({'jsonrpc': '2.0', 'id': 'private-request',
                      'result': {'session_id': 'private-runtime', 'messages': ['private-chat']}})
        rpc.upload({'id': 'private-request2', 'method': 'session.usage',
                    'params': {'session_id': 'private-runtime', 'profile': 'private-profile'}})
        error = {'jsonrpc': '2.0', 'id': 'private-request2', 'error': {
            'code': 4001, 'message': 'private-backend-message', 'data': {'reason': 'not_found'}}}
        assert rpc.download(error) is error
    assert 'method=session.usage adapted=True version=omitted code=4001 reason=not_found' in caplog.text
    assert 'runtime_session=' + rpc._tag('private-runtime') in caplog.text
    assert 'session=' + rpc._tag('private-runtime') in caplog.text
    assert 'private-' not in caplog.text


def test_mobile_session_reads_keep_their_confirmed_profile_after_other_sessions_open():
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            owners = {'personal-runtime': 'personal', 'work-runtime': 'work'}
            async def handle(ws, frame):
                params = frame['params']
                if frame['method'] == 'session.resume':
                    result = {'session_id': params['profile'] + '-runtime',
                              'stored_session_id': params['session_id']}
                else:
                    assert params['profile'] == owners[params['session_id']]
                    result = STATS if frame['method'] == 'session.stats' else {'ok': True}
                await ws.send_json({'jsonrpc': '2.0', 'id': frame['id'], 'result': result})
            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                for profile in ('personal', 'work'):
                    await ws.send_json({'id': profile, 'method': 'session.resume',
                        'params': {'session_id': profile + '-stored', 'profile': profile}})
                    assert 'result' in await ws.receive_json()
                # Mobile omits jsonrpc AND profile for these live-session reads.
                for profile in ('personal', 'work', 'personal'):
                    for method in ('session.usage', 'session.context_breakdown', 'process.list'):
                        await ws.send_json({'id': method, 'method': method,
                                            'params': {'session_id': profile + '-runtime'}})
                        reply = await ws.receive_json()
                        assert reply['result'] == (EXPECTED if method == 'session.usage' else {'ok': True})
    asyncio.run(run())


def test_profile_learning_uses_successful_owner_responses_and_preserves_explicit_selectors():
    rpc = MobileRpc(preferred_profile='work')
    def resume(rid, sid, profile):
        frame = {'id': rid, 'method': 'session.resume', 'params': {'session_id': sid}}
        if profile is not None:
            frame['params']['profile'] = profile
        rpc.upload(frame)
    # Two pending resumes resolve out of order; each keeps its own profile.
    resume('a', 'stored-a', 'personal')
    resume('b', 'stored-b', 'work')
    for rid in ('b', 'a'):
        rpc.download({'jsonrpc': '2.0', 'id': rid, 'result': {'session_id': 'runtime-' + rid}})
    for sid, owner in (('stored-a', 'personal'), ('runtime-a', 'personal'), ('runtime-b', 'work')):
        translated = rpc.upload({'id': sid, 'method': 'session.usage', 'params': {'session_id': sid}})
        assert translated['params']['profile'] == owner
    explicit = rpc.upload({'id': 'explicit', 'method': 'session.context_breakdown',
                          'params': {'session_id': 'runtime-a', 'profile': 'work'}})
    assert explicit['params']['profile'] == 'work'
    resume('failed', 'not-owned', 'personal')
    rpc.download({'jsonrpc': '2.0', 'id': 'failed', 'error': {'code': 4001}})
    unbound = rpc.upload({'id': 'unbound', 'method': 'session.usage', 'params': {'session_id': 'not-owned'}})
    assert 'profile' not in unbound['params']
    resume('default', 'default-stored', None)
    rpc.download({'jsonrpc': '2.0', 'id': 'default', 'result': {'session_id': 'default-runtime'}})
    assert rpc.session_profiles['default-runtime'] == 'work'
    # A new socket must learn ownership again, never inherit another phone's session state.
    assert not MobileRpc(preferred_profile='work').session_profiles


def test_session_ids_shared_by_profiles_require_an_explicit_profile():
    rpc = MobileRpc()
    for profile in ('work', 'personal'):
        rpc.upload({'id': profile, 'method': 'session.resume',
                    'params': {'session_id': 'shared-id', 'profile': profile}})
        rpc.download({'jsonrpc': '2.0', 'id': profile, 'result': {'session_id': 'shared-id'}})
    with pytest.raises(RpcRefusal) as exc:
        rpc.upload({'id': 'ambiguous', 'method': 'session.usage', 'params': {'session_id': 'shared-id'}})
    assert exc.value.frame['error']['data']['reason'] == 'ambiguous_profile'
    explicit = rpc.upload({'id': 'explicit', 'method': 'session.usage',
                          'params': {'session_id': 'shared-id', 'profile': 'personal'}})
    assert explicit['params']['profile'] == 'personal'


def test_correlation_refuses_duplicate_ids_and_bounds_unanswered_requests():
    rpc = MobileRpc()
    rpc.upload({'jsonrpc': '2.0', 'id': 1, 'method': 'session.usage'})
    with pytest.raises(ValueError, match='rpc_request_correlation_invalid'):
        rpc.upload({'jsonrpc': '2.0', 'id': 1, 'method': 'gateway.ping'})
    for rid in range(2, MAX_PENDING_RPC + 1):
        rpc.upload({'jsonrpc': '2.0', 'id': rid, 'method': 'gateway.ping'})
    with pytest.raises(ValueError, match='rpc_request_correlation_invalid'):
        rpc.upload({'jsonrpc': '2.0', 'id': MAX_PENDING_RPC + 1, 'method': 'session.usage'})


@pytest.mark.parametrize('failure', [None, 'session.stats', 'session.context_breakdown', 'false_success'])
def test_chat_preflight_checks_real_dispatch_and_requires_exact_missing_session_refusal(failure):
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            async def handle(ws, frame):
                if failure == 'false_success':
                    reply = {'result': STATS}
                else:
                    missing = frame['method'] == failure
                    reply = {'error': {'code': -32601 if missing else 4001,
                                      'data': {'reason': 'unknown_method' if missing else 'not_found'}}}
                await ws.send_json({'jsonrpc': '2.0', 'id': frame['id'], **reply})

            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                if failure:
                    with pytest.raises(ControlError, match='chat_.*_method_unavailable'):
                        await check_chat_methods(ws, 'personal')
                else:
                    evidence = await check_chat_methods(ws, 'personal')
                    assert evidence['verification'] == 'nonexistent_session_refused'
                    assert evidence['session_created'] is False
                assert {row['method'] for row in backend.frames} <= {'session.stats', 'session.context_breakdown'}
                assert all(row['params']['profile'] == 'personal' for row in backend.frames)
                assert all(row['params']['session_id'].startswith('hma-probe-') for row in backend.frames)
    asyncio.run(run())
