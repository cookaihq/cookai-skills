"""Mobile's send shape, native admission identity and refusal propagation."""
import asyncio
from copy import deepcopy

import pytest

from test_access_bridge import setup_bridge, connect_ws
from mobile_rpc import MobileRpc


def prompt(rid='17', **extra):
    return {'id': rid, 'method': 'prompt.submit', 'params': {
        'session_id': 'synthetic-runtime', 'text': 'Synthetic text', **extra}}


def test_mobile_send_reaches_native_admission_with_owner_and_identity_once(caplog):
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            receipts = {}
            admissions = []

            async def handle(ws, frame):
                if frame['method'] == 'session.resume':
                    result = {'session_id': 'synthetic-runtime'}
                else:
                    params = frame['params']
                    assert params['profile'] == 'personal'
                    identity = params.get('submission_id') or params.get('input_id')
                    if not isinstance(identity, str) or not identity:
                        await ws.send_json({'jsonrpc': '2.0', 'id': frame['id'], 'error': {
                            'code': 4001, 'message': 'invalid_params', 'data': {'reason': 'invalid_params'}}})
                        return
                    assert params['text'] == 'Synthetic text'
                    assert params['queued'] is True and params['clarify_batch'] is True
                    if identity not in receipts:
                        admissions.append(identity)
                        receipts[identity] = {'status': 'queued', 'admission_id': 'synthetic-admission'}
                    result = receipts[identity]
                await ws.send_json({'jsonrpc': '2.0', 'id': frame['id'], 'result': result})

            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                await ws.send_json({'id': 'resume', 'method': 'session.resume',
                    'params': {'session_id': 'synthetic-stored', 'profile': 'personal'}})
                await ws.receive_json()
                for _ in range(2):
                    await ws.send_json(prompt(queued=True, clarify_batch=True))
                    reply = await ws.receive_json()
                    assert reply == {'jsonrpc': '2.0', 'id': '17', 'result': {
                        'status': 'queued', 'admission_id': 'synthetic-admission'}}
                assert len(admissions) == 1
                assert len(backend.frames) == 3
    with caplog.at_level('INFO', logger='hermes_mobile_access.rpc'):
        asyncio.run(run())
    assert 'rpc_prompt_receipt' in caplog.text and 'status=queued' in caplog.text
    assert not any(value in caplog.text for value in ('Synthetic text', 'synthetic-runtime',
                   'synthetic-stored', 'synthetic-admission', 'personal'))


@pytest.mark.parametrize('explicit', [
    {'submission_id': 'phone-owned'}, {'input_id': 'phone-owned'},
    {'submission_id': 'one', 'input_id': 'two'},
    {'submission_id': ''}, {'input_id': None},
])
def test_client_owned_identity_including_invalid_values_is_not_replaced(explicit):
    frame = prompt(**explicit)
    assert MobileRpc().upload(frame) == frame


def test_generated_identity_is_scoped_and_does_not_depend_on_message_text():
    rpc = MobileRpc()
    frame = prompt()
    original = deepcopy(frame)
    first = rpc.upload(frame)
    sid = first['params']['submission_id']
    assert isinstance(sid, str) and sid
    assert frame == original
    rpc.download({'jsonrpc': '2.0', 'id': '17', 'result': {'status': 'queued'}})
    # Reusing a request ID with changed text reaches native conflict handling,
    # not a newly minted identity that could create a duplicate charge.
    changed = prompt()
    changed['params']['text'] = 'Different text'
    assert rpc.upload(changed)['params']['submission_id'] == sid
    assert rpc.upload(prompt('18'))['params']['submission_id'] != sid
    assert MobileRpc().upload(prompt())['params']['submission_id'] != sid


def test_typed_rpc_ids_stay_distinct_and_equal_numeric_ids_stay_equal():
    rpc = MobileRpc()
    number = rpc.upload(prompt(17))['params']['submission_id']
    assert rpc.upload(prompt('17'))['params']['submission_id'] != number
    rpc.download({'jsonrpc': '2.0', 'id': 17, 'result': {'status': 'queued'}})
    assert rpc.upload(prompt(17.0))['params']['submission_id'] == number


def test_notifications_null_ids_and_other_methods_are_not_given_admission_identity():
    for frame in ({'method': 'prompt.submit', 'params': prompt()['params']},
                  prompt(None), {**prompt(), 'method': 'session.steer'},
                  {**prompt(), 'jsonrpc': 'invalid'}):
        assert MobileRpc().upload(frame) == frame


def test_refused_submit_is_forwarded_once_without_replay():
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            error = {'jsonrpc': '2.0', 'id': '17', 'error': {'code': 4001,
                'message': 'backend refusal', 'data': {'reason': 'permission_denied'}}}
            async def handle(ws, frame):
                await ws.send_json(error)
            backend.rpc_handler = handle
            async with await connect_ws(bridge, client) as ws:
                await ws.receive_json()
                await ws.send_json(prompt(profile='personal'))
                assert await ws.receive_json() == error
                assert len(backend.frames) == 1
    asyncio.run(run())
