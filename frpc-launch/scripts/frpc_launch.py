#!/usr/bin/env python3
"""frpc-launch: 本地一键启动/管理 frpc（official / sakura 双模式）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_HOME = Path.home() / ".config" / "frpc-launch"
VAR_NAMES = [
    "FRPC_LAUNCH_MODE", "FRPC_LAUNCH_CONFIG", "FRPC_LAUNCH_FRPC",
    "FRPC_LAUNCH_SAKURA_KEY", "FRPC_LAUNCH_SAKURA_TUNNELS", "FRPC_LAUNCH_SAKURA_FRPC",
]


class FrpcLaunchError(Exception):
    """用户可读的失败，message 已掩码，退出码 1。"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="frpc_launch", description="本地一键启动/管理 frpc")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME,
                   help="受管目录（默认 ~/.config/frpc-launch，测试/诊断用）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果（供 Agent 解析）")
    sub = p.add_subparsers(dest="command")
    for name, desc in [
        ("start", "后台启动 frpc"), ("stop", "停止受管 frpc"),
        ("status", "查看运行状态"), ("logs", "查看受管日志"),
        ("install", "安装受管二进制"), ("update", "显式升级受管二进制"),
        ("guide-init", "引导流程写配置（由 SKILL.md 编排调用）"),
    ]:
        sub.add_parser(name, help=desc)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    print("NOT_IMPLEMENTED: %s" % args.command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
