"""JSON entry for the shared maintenance-task boundary."""
from _runtime_bootstrap import ensure
ensure()

import argparse
import json
from pathlib import Path
import sys
from rules import Maintenance, RuleError


def main():
    parser = argparse.ArgumentParser(description='Agent 规则维护：检测、审阅任务、具体计划、确认及恢复')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--personal-dir', type=Path, default=Path.home()/'.config/agent-instructions-update')
    parser.add_argument('--no-personal', action='store_true', help='本次关闭个人业务配置；历史目录不变')
    commands = parser.add_subparsers(dest='command', required=True)
    call = commands.add_parser('call')
    call.add_argument('action')
    call.add_argument('--input', type=Path, help='JSON 请求文件；省略则从 stdin 读取')
    ui = commands.add_parser('ui', help='启动、查询或结束当前项目的规则工作台')
    ui.add_argument('operation', choices=['start', 'status', 'stop', 'serve'])
    ui.add_argument('--runtime-dir', type=Path)
    ui.add_argument('--idle-seconds', type=int, default=3600)
    ui.add_argument('--public-origin')
    args = parser.parse_args()
    if not args.project.is_dir():
        raise RuleError('project', '所选项目目录不存在。')
    if args.command == 'ui':
        from rules.ui_server import run
        return run(args)
    app = Maintenance(args.personal_dir, args.project, use_personal=not args.no_personal)
    content = args.input.read_text(encoding='utf-8-sig') if args.input else sys.stdin.read()
    try:
        data = json.loads(content) if content.strip() else {}
        result = app.action(args.action, data)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RuleError('request', '请求字段或任务记录格式无效：' + type(exc).__name__) from None
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except RuleError as exc:
        print(json.dumps({'error': exc.code, 'message': exc.message}, ensure_ascii=False))
        raise SystemExit(1)
    except OSError as exc:
        print(json.dumps({'error': 'filesystem', 'type': type(exc).__name__, 'message': '文件操作未完成，核对权限、路径和任务状态。'}, ensure_ascii=False))
        raise SystemExit(1)
