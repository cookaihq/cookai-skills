"""Owned background connection, guarded FRP lifetime and conditional UI rollback.

The launch record contains context/fingerprints only. Credentials are reloaded
from their original store. OS locks coordinate cooperating local processes.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import psutil

from access_bridge import AccessBridge
from access_security import binding_digest
from configuration import ConfigError, ConfigStore, ID, missing_binding, validate_record
from domain_probe import probe_domain, server_url
from endpoint_checks import check_endpoint
from frp_client import FrpClient
from native_backend import NativeBackend
from native_control import ControlError
from skill_ui.contracts import UIError
from skill_ui.storage import atomic_json, file_lock, private_directory

START_BUDGET = 150
STOP_BUDGET = 65
CHECK_INTERVAL = 2
FRP_CHECK_INTERVAL = 10
PUBLIC_CHECK_INTERVAL = 60
ROOT = Path(__file__).resolve().parent
log = logging.getLogger('hermes_mobile_access.controller')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_json(path):
    try:
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise ConfigError('runtime_record_invalid', '运行记录不可读；未猜测或停止其他进程。', path) from None


def process_identity():
    process = psutil.Process()
    return {'pid': process.pid, 'created': process.create_time(),
            'exe': process.exe(), 'argv': process.cmdline()}


def owned_process(state):
    identity = state.get('process', {})
    if not identity:
        return None
    try:
        process = psutil.Process(identity['pid'])
        if (process.create_time() == identity['created'] and process.exe() == identity['exe']
                and process.cmdline() == identity['argv'] and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE):
            return process
        raise ConfigError('runtime_owner_mismatch', '运行记录指向不同进程，未向它发送信号。')
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, KeyError):
        raise ConfigError('runtime_owner_unverified', '无法核对接入进程归属。') from None


def error_code(exc):
    if isinstance(exc, (ConfigError, UIError)):
        return exc.code
    if isinstance(exc, ControlError):
        return str(exc)
    return type(exc).__name__


class Manager:
    def __init__(self, store, frp_skill, selected=None):
        self.store, self.frp_skill, self.selected = store, Path(frp_skill).resolve(), selected

    def directory(self, cid, create=False):
        if not isinstance(cid, str) or not ID.fullmatch(cid):
            raise ConfigError('selection_required', '需要有效的稳定连接 ID。')
        path = self.store.home / 'runtime/connections' / cid
        if create:
            private_directory(path, owner_path=self.store.project)
        return path

    def client(self, cid, record):
        return FrpClient(self.frp_skill, record['frp']['project'], record['frp']['home'], cid)

    def status(self, cid):
        state = read_json(self.directory(cid) / 'state.json')
        if not state:
            return {'result': 'stopped', 'connection': cid, 'controller_running': False,
                    'mobile': 'not_verified', 'active_url': None}
        live = owned_process(state) is not None
        state['controller_running'] = live
        if not live:
            state['active_url'] = None
            if state.get('result') not in ('stopped', 'failed', 'cleanup_failed'):
                state['result'] = 'controller_exited'
        # Old successful evidence remains timestamped; never becomes current
        # merely because the PID still exists (e.g. after sleep or a hang).
        if live and time.time() - state.get('heartbeat', 0) > 30:
            state['result'], state['active_url'] = 'status_stale', None
        return state

    def prepare(self, cid, record, *, check_domain=True):
        if missing_binding(record):
            raise ConfigError('configuration_incomplete', '请先在配置页保存实例、profile、FRP 来源和接入密钥。')
        context = self.client(cid, record).context()
        if (context.get('connection', {}).get('status') != 'configured'
                or context.get('domain_suffix', {}).get('status') != 'configured'):
            raise ConfigError('frp_context_incomplete', 'FRP 来源或父域不可用。')
        if record['frp']['source_binding'] != context.get('source_binding'):
            raise ConfigError('frp_source_changed', 'FRP 来源已变化，请重新载入并核对连接绑定。')
        if not {'owner_watch', 'require_tls'} <= set(context.get('capabilities', [])):
            raise ConfigError('frp_update_required', '需要带进程退出保护与 TLS 要求的 FRP Launch 消费者接口。')
        url = server_url(record['domain_prefix'], context['domain_suffix']['value'])
        check = probe_domain(url) if check_domain else None
        if check and check['occupancy'] == 'occupied':
            raise ConfigError('domain_occupied', '新域名正常响应，按已选规则视为占用；原配置与连接保留。')
        if check and check.get('reason') in ('tls_error', 'dns_error', 'connection_error', 'timeout'):
            raise ConfigError('https_not_ready', '域名按访问规则未占用，但 HTTPS 访问条件尚未满足；原连接保留。')
        return context, url, check

    def connect(self, cid, *, wait=True):
        directory = self.directory(cid, True)
        with file_lock(directory / 'operation.lock'):
            return self._connect(cid, wait=wait)

    def _connect(self, cid, *, wait=True, prepared=None):
        snapshot = self.store.snapshot()
        _, record = snapshot.select(cid)
        fingerprint = binding_digest(snapshot.source, cid, record)
        current = self.status(cid)
        if current.get('controller_running'):
            if current.get('binding') == fingerprint:
                return current
            raise ConfigError('connection_changed', '运行配置与当前保存值不同，请先断开或使用配置页应用变更。')
        context, url, check = prepared or self.prepare(cid, record)
        # A previous owner may have crashed. Verify cleanup before reusing its
        # route. This uses the original persisted context, even after a rename.
        previous = read_json(self.directory(cid) / 'launch.json')
        if previous:
            self._cleanup_frp(cid, previous)
        generation = secrets.token_hex(16)
        payload = {'schema_version': 1, 'generation': generation, 'connection': cid,
            'project': str(self.store.project), 'home': str(self.store.home),
            'use_local_key': self.store.use_local_key, 'frp_skill': str(self.frp_skill),
            'frp': record['frp'], 'binding': fingerprint, 'source': snapshot.source,
            'context_revision': context['revision'], 'server_url': url,
            'selected': self.selected, 'domain_check': check}
        directory = self.directory(cid, True)
        atomic_json(directory / 'launch.json', payload, owner_path=self.store.project)
        state = {'result': 'starting', 'connection': cid, 'generation': generation,
                 'binding': fingerprint, 'server_url': url, 'active_url': None,
                 'heartbeat': time.time(), 'checked_at': now(), 'mobile': 'not_verified'}
        atomic_json(directory / 'state.json', state, owner_path=self.store.project)
        argv = [sys.executable, '-B', str(ROOT / 'hermes_mobile_access.py'),
                '_serve', str(directory), generation]
        options = {'stdin': subprocess.PIPE, 'stdout': subprocess.DEVNULL,
                   'stderr': subprocess.DEVNULL, 'cwd': str(self.store.project)}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        child = subprocess.Popen(argv, **options)
        try:
            child.stdin.write(b'GO\n')
            child.stdin.close()
        except BaseException:
            child.terminate()
            raise
        deadline = time.monotonic() + (START_BUDGET + 10 if wait else 5)
        while time.monotonic() < deadline:
            current = self.status(cid)
            if current.get('generation') != generation:
                raise ConfigError('runtime_conflict', '运行记录已被其他操作替换。')
            if current.get('process') and (not wait or current['result'] in ('awaiting_mobile', 'failed', 'cleanup_failed')):
                return current
            if child.poll() is not None and not current.get('controller_running'):
                return {**current, 'result': 'failed', 'reason': current.get('reason', 'worker_exited')}
            time.sleep(.2)
        return {**current, 'result': 'startup_pending', 'detail': '启动仍待核对，请查询 status；不要重复启动。'}

    def _cleanup_frp(self, cid, launch):
        binding = launch['frp']
        return FrpClient(launch['frp_skill'], binding['project'], binding['home'], cid).stop()

    def disconnect(self, cid):
        with file_lock(self.directory(cid, True) / 'operation.lock'):
            return self._disconnect(cid)

    def _disconnect(self, cid):
        directory = self.directory(cid, True)
        state, launch = self.status(cid), read_json(directory / 'launch.json')
        process = owned_process(state)
        if process:
            atomic_json(directory / 'stop.json', {'generation': state['generation']}, owner_path=self.store.project)
            deadline = time.monotonic() + STOP_BUDGET
            while time.monotonic() < deadline and owned_process(state):
                time.sleep(.2)
            process = owned_process(state)
            if process:
                # psutil retains the creation time and rechecks PID reuse.
                process.terminate()
                try:
                    process.wait(10)
                except psutil.TimeoutExpired:
                    raise ConfigError('cleanup_failed', '接入进程未停止；请通过 doctor 核对。') from None
        if launch:
            frp = self._cleanup_frp(cid, launch)
        else:
            # Migration from a previously created standalone consumer route.
            # No saved runtime exists yet; use the explicitly selected record.
            _, record = self.store.snapshot().select(cid)
            frp = self.client(cid, record).stop() if all(record['frp'].get(k) for k in ('project', 'home')) else None
        result = {**read_json(directory / 'state.json'), 'result': 'stopped',
                  'connection': cid, 'controller_running': False, 'active_url': None,
                  'frp': frp, 'heartbeat': time.time(), 'checked_at': now(),
                  'public_endpoint': 'not_checked', 'mobile': 'not_verified'}
        atomic_json(directory / 'state.json', result, owner_path=self.store.project)
        return result

    def apply(self, cid, snapshot, collection, *, default_id=None, auto_update=None):
        """Check-before-stop; restore only our own unchanged saved generation."""
        with file_lock(self.directory(cid, True) / 'operation.lock'):
            if self.store.snapshot().revision != snapshot.revision:
                raise ConfigError('conflict', '配置已变化；草稿保留。')
            values = validate_record(collection['connections'][cid])
            old = snapshot.collection['connections'].get(cid)
            current = self.status(cid)
            running = current.get('controller_running') is True
            changed = not old or binding_digest(snapshot.source, cid, old) != binding_digest(snapshot.source, cid, values)
            if not running or not changed:
                saved = self.store.save(snapshot.revision, collection, default_id=default_id, auto_update=auto_update)
                return saved
            prepared = None
            if not missing_binding(values):
                prepared = self.prepare(cid, values, check_domain=old['domain_prefix'] != values['domain_prefix'])
                # Verify the new Desktop binding while the old connection is
                # still intact. No Gateway restart or business mutation.
                NativeBackend.discover(values['hermes'], self.selected)
            self._disconnect(cid)
            saved = None
            try:
                saved = self.store.save(snapshot.revision, collection, default_id=default_id, auto_update=auto_update)
                if missing_binding(values):  # clearing auth is an intentional revocation
                    return saved
                result = self._connect(cid, prepared=prepared)
                if result['result'] != 'awaiting_mobile':
                    raise ConfigError('connection_apply_failed', '新连接未通过验证。')
                return self.store.snapshot()
            except Exception as exc:
                original = error_code(exc)
                try:
                    self._disconnect(cid)
                    latest = self.store.snapshot()
                    if saved:
                        if latest.revision != saved.revision:
                            raise ConfigError('rollback_conflict', '配置已被其他操作修改，不覆盖。')
                        # Restore only this connection; retain other connections
                        # and unrelated dotenv fields, including update settings.
                        restored = copy.deepcopy(latest.collection)
                        restored['connections'][cid] = old
                        self.store.save(latest.revision, restored)
                    elif latest.revision != snapshot.revision:
                        raise ConfigError('rollback_conflict', '保存失败后来源已变化，不覆盖。')
                    result = self._connect(cid)
                    if result['result'] != 'awaiting_mobile':
                        raise ConfigError('rollback_unavailable', '旧连接未通过恢复验证。')
                except Exception as rollback:
                    raise ConfigError('apply_and_restore_failed', '应用失败（%s），旧连接恢复未完成（%s）；请核对状态。' %
                                      (original, error_code(rollback))) from None
                raise ConfigError('apply_failed_restored', '应用失败（%s）；已验证旧连接恢复，页面草稿保留。' % original) from None


async def blocking(function, *args, **kwargs):
    """Cancellation must not race a still-running FRP mutation with cleanup."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


class Supervisor:
    def __init__(self, directory, launch):
        self.directory, self.launch = directory, launch
        self.store = ConfigStore(launch['project'], home=launch['home'], use_local_key=launch['use_local_key'])
        self.cid = launch['connection']
        self.state = {**read_json(directory / 'state.json'), 'process': process_identity()}
        self.client = FrpClient(launch['frp_skill'], launch['frp']['project'], launch['frp']['home'], self.cid)
        self.bridge = None

    def record(self):
        snapshot = self.store.snapshot()
        _, record = snapshot.select(self.cid)
        if binding_digest(snapshot.source, self.cid, record) != self.launch['binding']:
            raise ControlError('configuration_changed')
        return record

    def unchanged(self):
        self.record()
        return True

    def write(self, result=None, **fields):
        if result:
            self.state['result'] = result
            log.info('connection_state connection=%s state=%s reason=%s', self.cid, result, fields.get('reason', 'none'))
        self.state.update(fields, heartbeat=time.time(), checked_at=now())
        atomic_json(self.directory / 'state.json', self.state, owner_path=self.store.project)

    async def cleanup(self):
        # Close admission and existing streams before waiting on FRP shutdown.
        if self.bridge:
            await self.bridge.close()
            self.bridge = None
        state = await blocking(self.client.stop)
        self.write(active_url=None, adapter_port=None, frp=state)

    async def open(self, record, recovery=False):
        self.write('recovering' if recovery else 'starting', active_url=None)
        context = await blocking(self.client.context)
        if context.get('revision') != self.launch['context_revision']:
            raise ControlError('frp_context_changed')
        check = await blocking(probe_domain, self.launch['server_url'])
        if check['occupancy'] != 'unoccupied':
            raise ControlError('domain_occupied')
        self.write(domain_check=check)
        backend = await blocking(NativeBackend.discover, record['hermes'], None if recovery else self.launch.get('selected'))
        self.bridge = AccessBridge(self.launch['server_url'], record['auth']['key'], backend, self.unchanged, gated=True)
        await self.bridge.start()
        self.write(adapter_port=self.bridge.port, backend={'candidate': backend.candidate['id'],
            'pid': backend.candidate['pid'], 'host': backend.candidate['host'], 'port': backend.candidate['port'],
            'instance_id': backend.identity['instance_id'], 'preferred_profile': backend.preferred})
        await blocking(self.client.configure, context['revision'], record['domain_prefix'], self.bridge.port)
        self.write('verifying', frp=await blocking(self.client.start))
        # Only the authenticated random bootstrap marker is available here;
        # Gateway REST/WS remain gated until the public checks all pass.
        protection = await check_endpoint(self.bridge, self.launch['server_url'])
        await self.bridge._check()
        self.bridge.gated = False
        protocol = await check_endpoint(self.bridge, self.launch['server_url'], protocol=True)
        self.write('awaiting_mobile', active_url=self.launch['server_url'],
                   evidence={'protection': protection, 'protocol': protocol},
                   mobile='not_verified', reason=None, recovery=recovery)

    async def lifecycle(self):
        record = self.record()
        await self.monitor(record)

    async def run(self):
        try:
            record = self.record()
            await self.run_exclusive(record)
        except Exception as exc:
            self.write('failed', reason=error_code(exc), active_url=None)

    async def run_exclusive(self, record):
        # One logical Desktop per OS user, including callers with a different
        # --home. Retain the lock through stream and FRP cleanup, including
        # cancellation; a successor must not publish before this owner stops.
        key = hashlib.sha256(os.path.normcase(str(Path(record['hermes']['home']).resolve())).encode()).hexdigest()
        locks = private_directory(Path.home() / '.config/hermes-mobile-access/runtime/desktops', owner_path=self.store.project)
        with file_lock(locks / (key + '.lock')):
            await self.run_owned()

    async def monitor(self, record):
            await asyncio.wait_for(self.open(record), START_BUDGET)
            attempts = 0
            frp_check = public_check = time.monotonic()
            while True:
                await asyncio.sleep(CHECK_INTERVAL)
                record = self.record()
                try:
                    if self.bridge is None or self.bridge.reason:
                        raise ControlError('backend_unavailable')
                    tick = time.monotonic()
                    if tick - frp_check >= FRP_CHECK_INTERVAL:
                        context = await blocking(self.client.context)
                        if context.get('revision') != self.launch['context_revision']:
                            raise ControlError('frp_context_changed')
                        status = await blocking(self.client.status)
                        if not status.get('modes', {}).get('official', {}).get('running'):
                            raise ControlError('frp_stopped')
                        frp_check = tick
                    if tick - public_check >= PUBLIC_CHECK_INTERVAL:
                        protection = await check_endpoint(self.bridge, self.launch['server_url'])
                        self.state['evidence']['protection'] = protection
                        public_check = tick
                    self.write()
                    attempts = 0
                except Exception as exc:
                    await self.cleanup()
                    reason = error_code(exc)
                    self.write('degraded', reason=reason)
                    if not record['auto_recover'] or reason in ('frp_context_changed', 'configuration_changed'):
                        raise
                    # Discovery/reconnect only, never replay a chat or a ticket.
                    while True:
                        await asyncio.sleep(min(2 ** min(attempts + 1, 5), 30))
                        record = self.record()
                        if not record['auto_recover']:
                            raise ControlError('recovery_disabled')
                        attempts += 1
                        try:
                            await asyncio.wait_for(self.open(record, recovery=True), START_BUDGET)
                            frp_check = public_check = time.monotonic()
                            break
                        except Exception as retry:
                            await self.cleanup()
                            self.write('degraded', reason=error_code(retry), recovery_attempts=attempts)
                            if error_code(retry) in ('frp_context_changed', 'configuration_changed', 'domain_occupied'):
                                raise

    async def run_owned(self):
        self.write('starting')
        task = asyncio.create_task(self.lifecycle())
        try:
            while not task.done():
                await asyncio.sleep(.25)
                if read_json(self.directory / 'stop.json').get('generation') == self.launch['generation']:
                    task.cancel()
                    break
            await task
        except asyncio.CancelledError:
            self.write('stopping', active_url=None)
        except Exception as exc:
            self.write('failed', reason=error_code(exc), active_url=None)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                await self.cleanup()
                if self.state['result'] != 'failed':
                    self.write('stopped', active_url=None)
            except Exception as exc:
                self.write('cleanup_failed', reason=error_code(exc), active_url=None)


def serve(directory, generation):
    directory = Path(directory).resolve()
    launch = read_json(directory / 'launch.json')
    if launch.get('generation') != generation or sys.stdin.buffer.read(3) != b'GO\n':
        raise ConfigError('launch_changed', '启动记录已变化或调用方未完成交接。')
    expected = Path(launch['home']).resolve() / 'runtime/connections' / launch['connection']
    if directory != expected:
        raise ConfigError('launch_changed', '启动目录与连接身份不符。')
    handler = RotatingFileHandler(directory / 'service.log', maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger = logging.getLogger('hermes_mobile_access')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        with file_lock(directory / 'run.lock'):
            asyncio.run(Supervisor(directory, launch).run())
    finally:
        handler.close()
