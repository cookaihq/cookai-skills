#!/usr/bin/env python3
"""frpc-launch: 本地一键启动/管理 frpc（official / sakura 双模式）。"""
from __future__ import annotations

import argparse
import dataclasses
import re
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


# ---------------------------------------------------------------------------
# 凭证掩码与 .env 极简解析
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def mask_secret(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def mask_text(text: str, secrets: list) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, mask_secret(s))
    return text


def parse_env_file(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    result = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# 配置分层解析与来源标签
# ---------------------------------------------------------------------------


def resolve_layered(environ: dict, cwd: Path, home: Path) -> dict:
    layers = [
        ("env", {k: environ.get(k, "") for k in VAR_NAMES}),
        (".env.local", parse_env_file(cwd / ".env.local")),
        (".env", parse_env_file(cwd / ".env")),
        ("global", parse_env_file(home / ".env")),
    ]
    resolved = {}
    for name in VAR_NAMES:
        for source, values in layers:
            v = values.get(name, "")
            if v:
                resolved[name] = (v, source)
                break
    return resolved


def official_config_path(layered: dict, home: Path):
    if "FRPC_LAUNCH_CONFIG" in layered:
        value, source = layered["FRPC_LAUNCH_CONFIG"]
        return Path(value).expanduser(), source
    global_toml = home / "frpc.toml"
    if global_toml.is_file():
        return global_toml, "global"
    return None, ""


def official_config_valid(path: Path):
    if path is None or not path.is_file():
        return False, "配置文件不存在: %s" % path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return False, "配置文件不可读: %s (%s)" % (path, e)
    if not text.strip():
        return False, "配置文件为空: %s" % path
    if "serverAddr" not in text:
        return False, ("配置缺少 serverAddr 字段: %s"
                       "（本脚本只做朴素检查，完整语义校验由 frpc verify 执行）" % path)
    return True, ""


def sakura_config(layered: dict):
    key = layered.get("FRPC_LAUNCH_SAKURA_KEY")
    tunnels = layered.get("FRPC_LAUNCH_SAKURA_TUNNELS")
    if not key or not tunnels:
        return None, ""
    frpc_path = layered.get("FRPC_LAUNCH_SAKURA_FRPC")
    return {"key": key[0], "tunnels": tunnels[0],
            "frpc_path": frpc_path[0] if frpc_path else None}, key[1]


# ---------------------------------------------------------------------------
# 模式判定
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ModeDecision:
    decision: str
    modes: list
    official: tuple
    sakura: tuple


def decide_mode(layered: dict, home: Path, requested_mode: str = "") -> ModeDecision:
    o_path, o_source = official_config_path(layered, home)
    o_ok = o_path is not None and official_config_valid(o_path)[0]
    s_cfg, s_source = sakura_config(layered)
    official = (o_path if o_ok else None, o_source if o_ok else "")
    sakura = (s_cfg, s_source)
    explicit = requested_mode or (layered.get("FRPC_LAUNCH_MODE") or ("",))[0]
    if explicit:
        if explicit not in ("official", "sakura"):
            raise FrpcLaunchError(
                "FRPC_LAUNCH_MODE/--mode 取值无效: %r（只接受 official / sakura）" % explicit)
        return ModeDecision("explicit", [explicit], official, sakura)
    available = [m for m, ok in [("official", o_ok), ("sakura", s_cfg is not None)] if ok]
    if len(available) == 2:
        return ModeDecision("ambiguous", available, official, sakura)
    if len(available) == 1:
        return ModeDecision("single", available, official, sakura)
    return ModeDecision("none", [], official, sakura)


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
