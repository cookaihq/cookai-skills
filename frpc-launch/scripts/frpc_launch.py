#!/usr/bin/env python3
"""frpc-launch: 本地一键启动/管理 frpc（official / sakura 双模式）。"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
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
# 平台探测、HTTP 辅助、meta 与原子安装原语
# ---------------------------------------------------------------------------


def detect_platform():
    sysname = platform.system()
    machine = platform.machine().lower()
    os_map = {"Darwin": ("darwin", "macos"), "Linux": ("linux", "linux")}
    arch_map = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    if sysname not in os_map or machine not in arch_map:
        raise FrpcLaunchError(
            "暂不支持的平台: %s/%s（v1 支持 macOS/Linux 的 amd64/arm64；"
            "bin/windows 目录为预留）" % (sysname, machine))
    os_name, os_subdir = os_map[sysname]
    return os_name, os_subdir, arch_map[machine]


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise FrpcLaunchError(
            "网络请求失败: %s (%s)。请检查网络或自行配置代理；"
            "本工具不自动切换第三方镜像。" % (url, e))


def http_get_json(url: str, timeout: int = 30) -> dict:
    try:
        return json.loads(http_get(url, timeout).decode("utf-8"))
    except ValueError as e:
        raise FrpcLaunchError("响应不是合法 JSON: %s (%s)" % (url, e))


def ensure_home_layout(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    for sub in ("bin/macos", "bin/linux", "bin/windows", "run"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def write_meta(meta_path: Path, meta: dict) -> None:
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, meta_path)


def read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clear_quarantine(path: Path) -> None:
    if platform.system() != "Darwin":
        return
    r = subprocess.run(["xattr", "-d", "com.apple.quarantine", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0 and "No such xattr" not in (r.stderr or ""):
        print("警告: 清除 quarantine 失败（不中断）: %s" % r.stderr.strip(), file=sys.stderr)


def install_binary(tmp_path: Path, final_path: Path) -> None:
    os.chmod(tmp_path, 0o755)
    clear_quarantine(tmp_path)
    os.replace(tmp_path, final_path)


def _hash_file(path: Path, algo) -> str:
    h = algo()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    return _hash_file(path, hashlib.sha256)


def md5_file(path: Path) -> str:
    return _hash_file(path, hashlib.md5)


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
