"""Business HTTP extension and launcher around the unchanged, pinned UI core."""
from __future__ import annotations
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from skill_ui.__main__ import running, request, wait_for_exit, remove_record
from skill_ui.contracts import Adapter, UIError
from skill_ui.server import Handler, Server
from skill_ui.storage import atomic_json, file_lock, private_directory
from .maintenance import Maintenance, RuleError, digest

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = {'file.read', 'index.read', 'task.create', 'task.get', 'task.prompt',
           'task.confirm', 'task.apply', 'task.refresh', 'scenario.plan', 'layout.plan'}


class RulesAdapter(Adapter):
    title = 'Agent 规则维护'

    def __init__(self, app: Maintenance):
        self.app = app

    def artifacts(self) -> dict[str, Any]:
        return {'items': [{'id': 'workbench', 'title': '规则维护工作台', 'kind': 'document',
                          'summary': '主规则同步 · 场景规则 · 存储与入口',
                          'preview': {'type': 'custom', 'renderer': 'rules-workbench',
                                      'data': {'project': str(self.app.project)}}}]}

    def extensions(self) -> dict[str, Path]:
        return {name: ROOT/'ui'/name for name in ('workbench.js', 'workbench.css')}


class RulesHandler(Handler):
    def post(self) -> None:
        if urlsplit(self.path).path != '/api/rules':
            return super().post()
        self.check_origin(post=True)
        self.server.lifetime()
        self.authorized()
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf):
            raise UIError('csrf', '请求校验失败，请重新打开页面。', 403)
        payload = self.body()
        action, data = payload.get('action'), payload.get('data', {})
        if action not in ACTIONS or not isinstance(data, dict):
            raise UIError('action', '此工作台操作不可用。')
        if action == 'task.create' and data.get('kind') not in ('sync', 'edit'):
            raise UIError('action', '该任务类型尚未接入此页面。')
        try:
            with self.server.lock:
                result = self.server.adapter.app.action(action, data)
        except RuleError as exc:
            raise UIError(exc.code, exc.message, 409 if exc.code in ('external_change', 'stale_plan', 'busy') else 400) from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UIError('request', '请求字段或维护记录无效。') from None
        return self.send(200, {'result': result})


class RulesServer(Server):
    def __init__(self, app: Maintenance, directory: Path, idle_seconds: int, public_origin: str | None):
        super().__init__(RulesAdapter(app), app.project, 'artifacts', directory, idle_seconds, public_origin)
        self.RequestHandlerClass = RulesHandler


def serve(args: Any, directory: Path) -> None:
    with file_lock(directory/'service.lock'):
        handler = RotatingFileHandler(directory/'service.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')
        logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
        app = Maintenance(args.personal_dir, args.project, use_personal=not args.no_personal)
        server = RulesServer(app, directory, args.idle_seconds, args.public_origin)
        record = {'pid': os.getpid(), 'origin': server.origin, 'public_origin': server.public_origin,
                  'instance': server.instance, 'token': server.token, 'page': 'artifacts',
                  'project': str(args.project), 'personal': str(args.personal_dir), 'adapter': str(Path(__file__).resolve())}
        atomic_json(directory/'server.json', record, owner_path=args.project)
        logging.info('service_started instance=%s', server.instance)
        threading.Thread(target=server.watcher, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.closed.set()
            server.server_close()
            remove_record(directory, server.instance)
            logging.info('service_stopped instance=%s', server.instance)


def run(args: Any) -> None:
    args.project, args.personal_dir = args.project.resolve(), args.personal_dir.resolve()
    if args.idle_seconds <= 0:
        raise RuleError('lifetime', '空闲有效期须大于零。')
    binding = [str(Path(__file__).resolve()), str(args.project), str(args.personal_dir), args.no_personal]
    runtime = (args.runtime_dir or args.personal_dir/'ui').resolve()
    directory = runtime/digest(binding)[:20]
    if args.operation in ('status', 'stop') and not directory.exists():
        print(json.dumps({'status': 'stopped', 'runtime': str(directory)}))
        return
    try:
        private_directory(directory, owner_path=args.project)
        if args.operation == 'serve':
            return serve(args, directory)
        with file_lock(directory/'launcher.lock'):
            record = running(directory)
            if record and any(record.get(key) != expected for key, expected in
                              {'adapter':binding[0], 'project':str(args.project), 'personal':str(args.personal_dir), 'page':'artifacts'}.items()):
                raise UIError('ownership', '运行记录不属于当前工作台，不能复用或停止。')
            if args.operation == 'start' and record and args.public_origin and args.public_origin != record['public_origin']:
                raise UIError('origin', '已有服务使用不同地址，请先结束当前工作台再重开。')
            if args.operation == 'start' and not record:
                wait_for_exit(directory)
                command = [sys.executable, str(ROOT/'scripts/agent_rules.py'), '--project', str(args.project),
                           '--personal-dir', str(args.personal_dir)]
                if args.no_personal:
                    command.append('--no-personal')
                command += ['ui', 'serve', '--runtime-dir', str(runtime), '--idle-seconds', str(args.idle_seconds)]
                if args.public_origin:
                    command += ['--public-origin', args.public_origin]
                options: dict[str, Any] = {'stdin':subprocess.DEVNULL, 'cwd':str(args.project)}
                if os.name == 'nt':
                    options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    options['start_new_session'] = True
                with (directory/'launcher.log').open('ab') as stream:
                    process = subprocess.Popen(command, stdout=stream, stderr=stream, **options)
                deadline = time.monotonic()+12
                while time.monotonic() < deadline:
                    record = running(directory)
                    if record:
                        break
                    if process.poll() is not None:
                        raise UIError('startup', '工作台启动失败，请核对运行目录中的 launcher.log。')
                    time.sleep(0.1)
                if not record:
                    raise UIError('startup', '启动未完成，请先查询状态，不要重复创建进程。')
            if args.operation == 'stop':
                if record:
                    request(record['origin'], '/api/control/stop', {'token':record['token']})
                wait_for_exit(directory)
                record = None
            elif args.operation == 'status' and not record:
                wait_for_exit(directory)
            output = {'status':'running' if record else 'stopped', 'runtime':str(directory)}
            if record:
                output.update(instance=record['instance'], project=record['project'], lifetime=record['lifetime'])
                if args.operation == 'start':
                    output['url'] = record['public_origin']+'/#'+record['token']
            print(json.dumps(output, ensure_ascii=False))
    except UIError as exc:
        raise RuleError(exc.code, exc.message) from None
