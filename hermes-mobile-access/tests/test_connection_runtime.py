import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import connection_runtime as runtime
from access_security import generate_key, binding_digest
from configuration import ConfigStore, ConfigError, draft, new_id
from native_control import ControlError
from skill_ui.storage import atomic_json


@pytest.fixture
def connected(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path, home=tmp_path / 'home', use_local_key=True, environ={})
    cid, row = new_id(), draft()
    row.update(auth={'type': 'mobile_key_v1', 'key': generate_key()},
               hermes={'home': str(tmp_path / 'hermes'), 'preferred_profile': 'work', 'profile_home': str(tmp_path / 'work')},
               frp={'project': str(tmp_path), 'home': str(tmp_path / 'frp'), 'source_binding': 'source'})
    store.save(store.snapshot().revision, {'schema_version': 1, 'connections': {cid: row}}, default_id=cid)
    manager = runtime.Manager(store, tmp_path / 'frp-skill')
    events = []
    monkeypatch.setattr(manager, 'status', lambda _: {'controller_running': True, 'result': 'awaiting_mobile'})
    monkeypatch.setattr(manager, 'prepare', lambda *a, **kw: (events.append('precheck') or ({}, 'https://example.test/', {})))
    monkeypatch.setattr(runtime.NativeBackend, 'discover', lambda *a, **k: events.append('identity'))
    monkeypatch.setattr(manager, '_disconnect', lambda _: events.append('stop'))
    def start(*a, **kw):
        prefix = store.snapshot().select(cid)[1]['domain_prefix']
        events.append('start:' + prefix)
        return {'result': 'awaiting_mobile'}
    monkeypatch.setattr(manager, '_connect', start)
    return manager, store, cid, row, events


def modified(store, cid, **updates):
    snap = store.snapshot()
    collection = copy.deepcopy(snap.collection)
    collection['connections'][cid].update(updates)
    return snap, collection


def test_occupied_domain_keeps_old_config_and_live_connection(connected, monkeypatch):
    manager, store, cid, row, events = connected
    def busy(*a, **kw):
        raise ConfigError('domain_occupied', 'occupied')
    monkeypatch.setattr(manager, 'prepare', busy)
    original = (store.home / '.env').read_bytes()
    with pytest.raises(ConfigError, match='occupied'):
        manager.apply(cid, *modified(store, cid, domain_prefix='busy'))
    assert not events
    assert (store.home / '.env').read_bytes() == original


def test_switch_validates_before_stopping_and_starts_saved_target(connected):
    manager, store, cid, row, events = connected
    manager.apply(cid, *modified(store, cid, domain_prefix='new'))
    assert events == ['precheck', 'identity', 'stop', 'start:new']
    assert store.snapshot().select(cid)[1]['domain_prefix'] == 'new'


def test_failed_new_connection_restores_old_and_reports_interruption(connected, monkeypatch):
    manager, store, cid, row, events = connected
    def start(*a, **kw):
        prefix = store.snapshot().select(cid)[1]['domain_prefix']
        events.append('start:' + prefix)
        return {'result': 'failed' if prefix == 'new' else 'awaiting_mobile'}
    monkeypatch.setattr(manager, '_connect', start)
    with pytest.raises(ConfigError) as caught:
        manager.apply(cid, *modified(store, cid, domain_prefix='new'))
    assert caught.value.code == 'apply_failed_restored'
    assert events == ['precheck', 'identity', 'stop', 'start:new', 'stop', 'start:hermes-mobile']
    assert store.snapshot().select(cid)[1] == row


def test_rollback_never_overwrites_a_concurrent_edit(connected, monkeypatch):
    manager, store, cid, row, events = connected
    def changed(*a, **kw):
        snapshot, collection = modified(store, cid, domain_prefix='external-change')
        store.save(snapshot.revision, collection)
        return {'result': 'failed'}
    monkeypatch.setattr(manager, '_connect', changed)
    with pytest.raises(ConfigError) as caught:
        manager.apply(cid, *modified(store, cid, domain_prefix='new'))
    assert caught.value.code == 'apply_and_restore_failed'
    assert 'rollback_conflict' in str(caught.value)
    assert store.snapshot().select(cid)[1]['domain_prefix'] == 'external-change'


def test_failed_restore_does_not_claim_old_connection_is_available(connected, monkeypatch):
    manager, store, cid, row, events = connected
    monkeypatch.setattr(manager, '_connect', lambda *a, **kw: {'result': 'failed'})
    with pytest.raises(ConfigError) as caught:
        manager.apply(cid, *modified(store, cid, domain_prefix='new'))
    assert caught.value.code == 'apply_and_restore_failed'
    assert 'rollback_unavailable' in str(caught.value)


def test_rename_and_recovery_toggle_do_not_restart(connected):
    manager, store, cid, row, events = connected
    manager.apply(cid, *modified(store, cid, name='renamed', auto_recover=False))
    assert not events
    assert store.snapshot().select(cid)[1]['auto_recover'] is False


def test_clearing_key_stops_before_save_and_does_not_restore(connected):
    manager, store, cid, row, events = connected
    manager.apply(cid, *modified(store, cid, auth=None))
    assert events == ['stop']
    assert store.snapshot().select(cid)[1]['auth'] is None


def test_same_pid_with_wrong_start_is_never_accepted():
    identity = runtime.process_identity()
    identity['created'] -= 1
    with pytest.raises(ConfigError) as caught:
        runtime.owned_process({'process': identity})
    assert caught.value.code == 'runtime_owner_mismatch'


def test_stale_runtime_evidence_cannot_supply_current_url(tmp_path):
    store = ConfigStore(tmp_path, home=tmp_path / 'home')
    manager, cid = runtime.Manager(store, tmp_path), new_id()
    directory = manager.directory(cid, True)
    atomic_json(directory / 'state.json', {'process': runtime.process_identity(),
        'result': 'awaiting_mobile', 'heartbeat': time.time() - 60, 'active_url': 'https://old.test/'})
    result = manager.status(cid)
    assert result['result'] == 'status_stale' and result['active_url'] is None


def test_frp_mutation_finishes_before_cancellation_can_start_cleanup():
    async def run():
        import threading
        started, finish = threading.Event(), threading.Event()
        events = []
        def mutate():
            started.set()
            finish.wait(2)
            events.append('mutation_finished')
        task = asyncio.create_task(runtime.blocking(mutate))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events.append('cleanup')
        assert events == ['mutation_finished', 'cleanup']
    asyncio.run(run())


def test_safety_cleanup_runs_when_recovery_is_disabled(tmp_path, monkeypatch):
    cid = new_id()
    store = ConfigStore(tmp_path, home=tmp_path / 'home', use_local_key=True, environ={})
    row = draft()
    row['hermes'] = {'home': str(tmp_path / 'hermes')}
    row['auto_recover'] = False
    store.save(store.snapshot().revision, {'schema_version': 1, 'connections': {cid: row}}, default_id=cid)
    launch = {'project': str(tmp_path), 'home': str(store.home), 'connection': cid, 'use_local_key': True,
        'frp_skill': str(tmp_path), 'frp': {'project': str(tmp_path), 'home': str(tmp_path)},
        'generation': 'fixture', 'binding': binding_digest(store.snapshot().source, cid, row)}
    directory = runtime.Manager(store, tmp_path).directory(cid, True)
    supervisor = runtime.Supervisor(directory, launch)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(runtime, 'CHECK_INTERVAL', .001)
    events = []
    async def opened(*a, **k):
        supervisor.bridge = type('Revoked', (), {'reason': 'backend_exited'})()
        events.append('opened')
    async def cleaned():
        events.append('closed')
        supervisor.bridge = None
    monkeypatch.setattr(supervisor, 'open', opened)
    monkeypatch.setattr(supervisor, 'cleanup', cleaned)
    asyncio.run(supervisor.run())
    assert events[0] == 'opened' and 'closed' in events
    assert supervisor.state['result'] == 'failed'
    assert supervisor.state['active_url'] is None


def test_publication_gate_never_reaches_gateway_before_verification():
    from test_access_bridge import setup_bridge, headers, url
    async def run():
        async with setup_bridge() as (bridge, backend, client, _):
            bridge.gated = True
            async with client.get(url(bridge), headers=headers(bridge)) as reply:
                assert reply.status == 200 and bridge.marker in await reply.text()
            for path in ('/api/profiles', '/api/ws?token=' + bridge.marker, '/api/files/download'):
                async with client.get(url(bridge, path), headers=headers(bridge)) as reply:
                    assert reply.status == 503
            assert not backend.requests and not backend.grants
            bridge.gated = False
            async with client.get(url(bridge, '/api/profiles'), headers=headers(bridge)) as reply:
                assert reply.status == 200
            assert len(backend.requests) == 1
    asyncio.run(run())


@pytest.mark.parametrize('cancelled', [False, True])
def test_desktop_stays_exclusive_until_cleanup_finishes(tmp_path, monkeypatch, cancelled):
    store = ConfigStore(tmp_path, home=tmp_path / 'home', use_local_key=True, environ={})
    cid, row = new_id(), draft()
    row['hermes'] = {'home': str(tmp_path / 'hermes')}
    store.save(store.snapshot().revision, {'schema_version': 1, 'connections': {cid: row}}, default_id=cid)
    launch = {'project': str(tmp_path), 'home': str(store.home), 'connection': cid, 'use_local_key': True,
        'frp_skill': str(tmp_path), 'frp': {'project': str(tmp_path), 'home': str(tmp_path)},
        'generation': 'fixture', 'binding': binding_digest(store.snapshot().source, cid, row)}
    supervisor = runtime.Supervisor(runtime.Manager(store, tmp_path).directory(cid, True), launch)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    async def stopped():
        if cancelled:
            raise asyncio.CancelledError()
        raise ControlError('fixture_failure')
    cleanup_checks = []
    async def cleaned():
        lock = next((tmp_path / '.config/hermes-mobile-access/runtime/desktops').glob('*.lock'))
        with pytest.raises(runtime.UIError) as caught:
            with runtime.file_lock(lock):
                pass
        assert caught.value.code == 'busy'
        cleanup_checks.append(lock)
    monkeypatch.setattr(supervisor, 'lifecycle', stopped)
    monkeypatch.setattr(supervisor, 'cleanup', cleaned)
    asyncio.run(supervisor.run())
    assert len(cleanup_checks) == 1
    assert supervisor.state['result'] == ('stopped' if cancelled else 'failed')
    with runtime.file_lock(cleanup_checks[0]):
        pass
