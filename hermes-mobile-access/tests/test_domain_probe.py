import asyncio
import json
from pathlib import Path
import subprocess
import sys

from aiohttp import web
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import domain_probe
import hermes_mobile_access as cli
from configuration import ConfigError, ConfigStore, draft, new_id


@pytest.mark.parametrize('status,occupied', [
    (200, True), (204, True), (301, True), (302, True),
    (401, False), (403, False), (404, False), (429, False), (500, False), (503, False)])
def test_user_policy_classifies_real_http_responses_without_reading_body(status, occupied, monkeypatch):
    # Environment credentials/proxies must not reach an arbitrary candidate.
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HMA_TEST_SECRET', 'synthetic-secret')
    async def run():
        requests = []
        async def respond(request):
            requests.append(dict(request.headers))
            return web.Response(status=status, text=None if status == 204 else 'unretained body',
                                headers={'Location': 'http://127.0.0.1:1/', 'Set-Cookie': 'synthetic=secret'})
        app = web.Application()
        app.router.add_get('/', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            result = await domain_probe.observe(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/')
            assert result['occupancy'] == ('occupied' if occupied else 'unoccupied')
            assert result['http_status'] == status
            assert result['registration'] == 'not_checked'
            assert len(requests) == 1  # no redirect or Retry-After replay
            assert not {'Authorization', 'Cookie', 'X-Hermes-Mobile-Key'} & requests[0].keys()
            assert 'secret' not in json.dumps(result) and 'unretained body' not in json.dumps(result)
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_dropped_connection_is_unoccupied_and_is_not_retried():
    async def run():
        requests = []
        handlers = set()
        async def drop(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                requests.append(await reader.readuntil(b'\r\n\r\n'))
                writer.transport.abort()
            finally:
                writer.close()
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(drop, '127.0.0.1', 0)
        async with server:
            result = await domain_probe.observe(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/', budget=1)
        await asyncio.gather(*handlers)
        assert result['occupancy'] == 'unoccupied'
        assert result['http_status'] is None
        assert result['reason'] == 'connection_error'
        assert len(requests) == 1
    asyncio.run(run())


def test_timeout_is_unoccupied_and_does_not_retry():
    async def run():
        count = 0
        async def respond(request):
            nonlocal count
            count += 1
            await asyncio.sleep(0.12)
            return web.Response()
        app = web.Application()
        app.router.add_get('/', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            result = await domain_probe.observe(f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/', budget=0.04)
            assert result['occupancy'] == 'unoccupied' and result['reason'] == 'timeout'
            assert count == 1
        finally:
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('url', ['http://mobile.frp.invalid/', 'https://a:b@mobile.frp.invalid/',
    'https://mobile.frp.invalid:7005/', 'https://mobile.frp.invalid/api/ws',
    'https://mobile.frp.invalid/?token=secret', 'https://mobile.frp.invalid/#secret'])
def test_invalid_target_stops_before_worker_launch(url, monkeypatch):
    monkeypatch.setattr(domain_probe.subprocess, 'run', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(ConfigError, match='HTTPS'):
        domain_probe.probe_domain(url)


def test_worker_timeout_is_bounded_but_broken_runtime_is_an_error(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == domain_probe.WORKER_BUDGET
        raise subprocess.TimeoutExpired('worker', kwargs['timeout'])
    monkeypatch.setattr(domain_probe.subprocess, 'run', timeout)
    assert domain_probe.probe_domain('https://mobile.frp.invalid/')['occupancy'] == 'unoccupied'
    monkeypatch.setattr(domain_probe.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 2, b'invalid', b'secret'))
    with pytest.raises(ConfigError) as exc:
        domain_probe.probe_domain('https://mobile.frp.invalid/')
    assert exc.value.code == 'domain_probe_failed'
    assert 'secret' not in str(exc.value)


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    for name in ('HMA_CONNECTION', 'HMA_CONNECTIONS_JSON'):
        monkeypatch.delenv(name, raising=False)
    project, home = tmp_path / 'project', tmp_path / 'home'
    project.mkdir()
    monkeypatch.setattr(cli.FrpClient, 'context', lambda _: {'revision': 'synthetic',
        'domain_suffix': {'value': 'frp.example.invalid', 'status': 'configured'},
        'connection': {'status': 'configured'}})
    monkeypatch.setattr(cli.FrpClient, 'stop', lambda _: pytest.fail('read-only preflight must not stop'))
    prefix = ['--project', str(project), '--home', str(home), '--use-local-key']
    return project, home, prefix


def test_probe_domain_works_before_creating_connection_and_does_not_save(invocation, monkeypatch):
    project, home, prefix = invocation
    calls = []
    def probe(url):
        calls.append(url)
        return domain_probe.observation(url, 404, 'abnormal_http_response', 0)
    monkeypatch.setattr(cli, 'probe_domain', probe)
    result = cli.execute(cli.parser().parse_args(prefix + ['probe-domain', '--domain-prefix', 'Mobile']))
    assert result['connection'] is None
    assert calls == ['https://mobile.frp.example.invalid/']
    assert result['occupancy'] == 'unoccupied'
    assert not (home / '.env').exists()


@pytest.mark.parametrize('status,blocked', [(200, True), (404, False), (None, False)])
def test_plan_uses_http_policy_without_requiring_frps_admin(invocation, monkeypatch, status, blocked):
    project, home, prefix = invocation
    store = ConfigStore(project, home=home, use_local_key=True)
    snap = store.snapshot()
    cid = new_id()
    store.save(snap.revision, {'schema_version': 1, 'connections': {cid: draft()}}, default_id=cid)
    before = (home / '.env').read_bytes()
    monkeypatch.setattr(cli, 'probe_domain', lambda url: domain_probe.observation(url, status, 'fixture', 0))
    result = cli.execute(cli.parser().parse_args(prefix + ['plan']))
    assert ('domain_occupied' in result['missing']) is blocked
    assert 'remote_domain_occupancy' not in result['missing']
    assert result['domain_check']['occupancy'] == ('occupied' if blocked else 'unoccupied')
    assert (home / '.env').read_bytes() == before
    assert result['publication'] == 'not_performed'
