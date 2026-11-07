import asyncio
from collections import namedtuple
from pathlib import Path
import sys

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import native_backend
import native_control
from native_backend import verify_connected_peer, NativeBackend
from native_control import ControlError


def test_established_socket_tuple_must_belong_to_verified_process(monkeypatch):
    row = namedtuple('Row', 'laddr raddr status')
    candidate = {'host': '127.0.0.1', 'port': 5000}
    process = type('Process', (), {})()
    checks = []
    monkeypatch.setattr(native_control, 'check_process', lambda value: checks.append(value) or process)
    process.net_connections = lambda **_: [row(('127.0.0.1', 5000), ('127.0.0.1', 5010), psutil.CONN_ESTABLISHED)]
    verify_connected_peer(candidate, ('127.0.0.1', 5010), ('127.0.0.1', 5000))
    assert len(checks) == 2
    for remote in (('127.0.0.1', 5011), ('127.0.0.2', 5010)):
        with pytest.raises(ControlError, match='upstream_peer_owner_unverified'):
            verify_connected_peer(candidate, remote, ('127.0.0.1', 5000))
    with pytest.raises(ControlError, match='upstream_peer_mismatch'):
        verify_connected_peer(candidate, ('127.0.0.1', 5010), ('127.0.0.1', 5001))
    process.net_connections = lambda **_: [row(('127.0.0.1', 5000), (), psutil.CONN_LISTEN)]
    with pytest.raises(ControlError, match='upstream_peer_owner_unverified'):
        verify_connected_peer(candidate, ('127.0.0.1', 5010), ('127.0.0.1', 5000))


def test_peer_check_rechecks_process_identity_after_os_query(monkeypatch):
    class Process:
        def net_connections(self, **_):
            monkeypatch.setattr(native_control, 'check_process', changed)
            return []
    def changed(_):
        raise ControlError('backend_identity_changed')
    monkeypatch.setattr(native_control, 'check_process', lambda _: Process())
    with pytest.raises(ControlError, match='backend_identity_changed'):
        verify_connected_peer({'host': '127.0.0.1', 'port': 5000}, ('127.0.0.1', 5010), ('127.0.0.1', 5000))


def test_live_same_process_socket_is_checked_before_sending():
    async def run():
        admitted = asyncio.Event()
        got_data = asyncio.Event()
        handlers = set()
        async def accept(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                admitted.set()
                if await reader.read(1):
                    got_data.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(accept, '127.0.0.1', 0)
        process = psutil.Process()
        candidate = {'pid': process.pid, 'born': process.create_time(), 'host': '127.0.0.1',
                     'port': server.sockets[0].getsockname()[1]}
        async with server:
            reader, writer = await asyncio.open_connection('127.0.0.1', candidate['port'])
            await admitted.wait()
            try:
                await asyncio.to_thread(verify_connected_peer, candidate,
                    writer.get_extra_info('sockname'), writer.get_extra_info('peername'))
                assert not got_data.is_set()
                writer.write(b'x')
                await writer.drain()
                await asyncio.wait_for(got_data.wait(), 2)
            finally:
                writer.close()
                await writer.wait_closed()
                await asyncio.gather(*handlers)
    asyncio.run(run())
