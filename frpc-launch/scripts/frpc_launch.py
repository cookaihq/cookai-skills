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
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
        # review W5：极短值做全文替换会把端口号/时间戳等无关内容打花，跳过 len<6
        if s and len(s) >= 6:
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
    for sub in ("bin", "bin/macos", "bin/linux", "bin/windows", "run"):
        (home / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(home / sub, 0o700)


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
# official 二进制自动安装（GitHub API + sha256 + 原子就位）
# ---------------------------------------------------------------------------

GITHUB_RELEASES = "https://api.github.com/repos/fatedier/frp/releases"
CHECKSUMS_NAME = "frp_sha256_checksums.txt"


def resolve_official_release(release: dict, os_name: str, arch: str):
    version = str(release.get("tag_name", "")).lstrip("v")
    asset_name = "frp_%s_%s_%s.tar.gz" % (version, os_name, arch)
    asset_url = csum_url = ""
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            asset_url = asset.get("browser_download_url", "")
        elif asset.get("name") == CHECKSUMS_NAME:
            csum_url = asset.get("browser_download_url", "")
    if not version or not asset_url or not csum_url:
        raise FrpcLaunchError(
            "release 中找不到预期资产 %s 或 %s；官方命名可能已变化，停止自动安装，"
            "请手动下载后用 FRPC_LAUNCH_FRPC 指定路径" % (asset_name, CHECKSUMS_NAME))
    return version, asset_name, asset_url, csum_url


def parse_checksums(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[1]] = parts[0]
    return result


def extract_frpc_member(tar_path: Path, version, os_name, arch, dest: Path) -> None:
    member_name = "frp_%s_%s_%s/frpc" % (version, os_name, arch)
    with tarfile.open(tar_path, "r:gz") as tf:
        try:
            member = tf.getmember(member_name)
        except KeyError:
            raise FrpcLaunchError("归档中找不到预期成员 %s，停止安装" % member_name)
        if not member.isreg():
            raise FrpcLaunchError("归档成员 %s 不是常规文件，拒绝提取" % member_name)
        src = tf.extractfile(member)
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def install_official(home: Path, version: str = "",
                     fetch_json=None, fetch_bytes=None) -> dict:
    fetch_json = fetch_json or http_get_json
    fetch_bytes = fetch_bytes or http_get
    os_name, os_subdir, arch = detect_platform()
    ensure_home_layout(home)
    if version.startswith("v"):
        version = version[1:]
    url = (GITHUB_RELEASES + "/latest") if not version else \
          (GITHUB_RELEASES + "/tags/v" + version)
    release = fetch_json(url)
    ver, asset_name, asset_url, csum_url = resolve_official_release(release, os_name, arch)
    checksums = parse_checksums(fetch_bytes(csum_url).decode("utf-8"))
    expected = checksums.get(asset_name)
    if not expected:
        raise FrpcLaunchError("校验文件中找不到 %s 的 SHA-256，停止安装" % asset_name)
    with tempfile.TemporaryDirectory(dir=str(home / "run")) as td:
        tar_path = Path(td) / asset_name
        tar_path.write_bytes(fetch_bytes(asset_url))
        actual = sha256_file(tar_path)
        if actual != expected:
            raise FrpcLaunchError(
                "SHA-256 校验不一致（期望 %s… 实际 %s…），已删除临时文件；"
                "不重试第三方源" % (expected[:12], actual[:12]))
        tmp_bin = Path(td) / "frpc.new"
        extract_frpc_member(tar_path, ver, os_name, arch, tmp_bin)
        final = home / "bin" / os_subdir / "frpc"
        install_binary(tmp_bin, final)
    meta = {"version": ver, "source_url": asset_url,
            "sha256": sha256_file(final),
            "installed_at": datetime.now(timezone.utc).isoformat()}
    write_meta(home / "bin" / os_subdir / "frpc.meta.json", meta)
    return meta


def _validated_explicit_binary(value: str, var_name: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_file() or not os.access(p, os.X_OK):
        raise FrpcLaunchError("%s 指向的二进制不存在或不可执行: %s" % (var_name, p))
    return p


def resolve_official_binary(layered: dict, home: Path):
    explicit = layered.get("FRPC_LAUNCH_FRPC")
    if explicit:
        return _validated_explicit_binary(explicit[0], "FRPC_LAUNCH_FRPC"), "explicit"
    _, os_subdir, _ = detect_platform()
    managed = home / "bin" / os_subdir / "frpc"
    if managed.is_file() and os.access(managed, os.X_OK):
        return managed, "managed"
    found = shutil.which("frpc")
    if found:
        try:
            r = subprocess.run([found, "--version"], capture_output=True,
                               text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return None, ""
        if r.returncode == 0:
            return Path(found), "path"
    return None, ""


# ---------------------------------------------------------------------------
# sakura 定制二进制自动安装（natfrp API + size/hash 校验 + 保守回退）
# ---------------------------------------------------------------------------

SAKURA_CLIENTS_URL = "https://api.natfrp.com/v4/system/clients"
SAKURA_HOST_SUFFIXES = (".natfrp.com", ".globalslb.net")
_HASH32_RE = re.compile(r"^[0-9a-f]{32}$")
SAKURA_FALLBACK_HINT = ("已停止自动安装。请从樱花面板『软件下载』手动下载定制 frpc，"
                        "并通过 FRPC_LAUNCH_SAKURA_FRPC 提供其路径")


class SakuraApiError(FrpcLaunchError):
    pass


def parse_sakura_clients(payload: dict, plat_key: str) -> dict:
    # 取值路径以 tests/fixtures/sakura_clients.json（真实响应）为准：
    # payload["frpc"]["ver"] + payload["frpc"]["archs"][plat_key]{url,hash,size}
    frpc_cat = payload.get("frpc") if isinstance(payload, dict) else None
    if not isinstance(frpc_cat, dict):
        raise SakuraApiError("客户端清单缺少 frpc 分类。" + SAKURA_FALLBACK_HINT)
    version = frpc_cat.get("ver")
    archs = frpc_cat.get("archs")
    entry = archs.get(plat_key) if isinstance(archs, dict) else None
    if not isinstance(entry, dict):
        raise SakuraApiError("客户端清单中找不到平台 %s。%s"
                             % (plat_key, SAKURA_FALLBACK_HINT))
    url, size, hash_ = entry.get("url"), entry.get("size"), entry.get("hash")
    if not version or not url or not isinstance(size, int) or size <= 0:
        raise SakuraApiError("客户端清单字段缺失或类型异常。" + SAKURA_FALLBACK_HINT)
    parsed = urllib.parse.urlparse(str(url))
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(SAKURA_HOST_SUFFIXES):
        raise SakuraApiError("下载 URL 非 HTTPS 或 host 不可识别: %s。%s"
                             % (url, SAKURA_FALLBACK_HINT))
    if not _HASH32_RE.match(str(hash_ or "")):
        raise SakuraApiError("hash 格式无法识别（预期 32 位十六进制）。" + SAKURA_FALLBACK_HINT)
    return {"version": str(version), "url": str(url), "size": size, "hash": str(hash_)}


def install_sakura(home: Path, fetch_json=None, fetch_bytes=None) -> dict:
    fetch_json = fetch_json or http_get_json
    fetch_bytes = fetch_bytes or http_get
    os_name, os_subdir, arch = detect_platform()
    ensure_home_layout(home)
    info = parse_sakura_clients(fetch_json(SAKURA_CLIENTS_URL), "%s_%s" % (os_name, arch))
    with tempfile.TemporaryDirectory(dir=str(home / "run")) as td:
        tmp_bin = Path(td) / "frpc-sakura.new"
        body = fetch_bytes(info["url"])
        if len(body) != info["size"]:
            raise SakuraApiError("下载大小不一致（期望 %d 实际 %d）。%s"
                                 % (info["size"], len(body), SAKURA_FALLBACK_HINT))
        tmp_bin.write_bytes(body)
        if md5_file(tmp_bin) != info["hash"]:
            raise SakuraApiError("hash 校验不一致，已删除临时文件。" + SAKURA_FALLBACK_HINT)
        final = home / "bin" / os_subdir / "frpc-sakura"
        install_binary(tmp_bin, final)
    meta = {"version": info["version"], "source_url": info["url"],
            "sha256": sha256_file(final), "upstream_hash": info["hash"],
            "installed_at": datetime.now(timezone.utc).isoformat()}
    write_meta(home / "bin" / os_subdir / "frpc-sakura.meta.json", meta)
    return meta


def resolve_sakura_binary(layered: dict, home: Path):
    explicit = layered.get("FRPC_LAUNCH_SAKURA_FRPC")
    if explicit:
        return _validated_explicit_binary(explicit[0], "FRPC_LAUNCH_SAKURA_FRPC"), "explicit"
    _, os_subdir, _ = detect_platform()
    managed = home / "bin" / os_subdir / "frpc-sakura"
    if managed.is_file() and os.access(managed, os.X_OK):
        return managed, "managed"
    return None, ""


# ---------------------------------------------------------------------------
# pid 记录、进程身份核对与配置摘要
# ---------------------------------------------------------------------------


def pid_paths(home: Path, mode: str):
    return home / "run" / (mode + ".pid"), home / "run" / (mode + ".log")


def write_pid_record(path: Path, record: dict) -> None:
    write_meta(path, record)


def read_pid_record(path: Path) -> dict:
    return read_meta(path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command(pid: int) -> str:
    # -ww：禁止按终端宽度截断（macOS 与 procps 均支持），防止长路径身份核对恒失败
    r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def pid_identity_ok(record: dict) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    exe = record.get("exe", "")
    return bool(exe) and exe in process_command(pid)


def config_digest_official(config_path: Path, binary: Path) -> str:
    try:
        body = config_path.read_bytes()
    except OSError:
        body = b""
    return hashlib.sha256(body + b"\0" + str(binary).encode("utf-8")).hexdigest()


def config_digest_sakura(key: str, tunnels: str, binary: Path) -> str:
    return hashlib.sha256(
        (key + "\n" + tunnels + "\n" + str(binary)).encode("utf-8")).hexdigest()


def read_log_tail(log_path: Path, lines: int = 50) -> str:
    lines = max(1, lines)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


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
        sp = sub.add_parser(name, help=desc)
        if name in ("start", "stop", "status", "logs", "install", "update"):
            sp.add_argument("--mode", choices=["official", "sakura"], default="",
                            help="限定操作的模式")
        if name in ("install", "update"):
            sp.add_argument("--version", default="",
                            help="official 模式指定版本（缺省为最新稳定版）")
        if name == "start":
            sp.add_argument("--wait", type=float, default=15.0,
                            help="启动验证的有界等待秒数（默认 15）")
        if name == "logs":
            sp.add_argument("-n", type=int, default=50, dest="lines",
                            help="输出日志尾部行数（默认 50）")
        if name == "guide-init":
            sp.add_argument("--scope", choices=["global", "project"], required=True)
            sp.add_argument("--source", choices=["frps", "baota", "sakura"], required=True)
            sp.add_argument("--server-addr", default="", dest="server_addr")
            sp.add_argument("--server-port", type=int, default=0, dest="server_port")
            sp.add_argument("--proxy", action="append", default=[], dest="proxies",
                            help="代理规格，如 name=web;type=http;localPort=8080;"
                                 "customDomains=a.com（可重复）")
            sp.add_argument("--allow-tracked", action="store_true",
                            help="显式允许写入未被 git 忽略的 Secret 文件")
    return p


# ---------------------------------------------------------------------------
# guide-init：引导流程落盘（作用域 + 三来源 + git 安全检查）
# ---------------------------------------------------------------------------


def parse_proxy_spec(spec: str) -> dict:
    fields = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise FrpcLaunchError("代理规格片段缺少 '=': %r（完整规格: %s）" % (part, spec))
        k, v = part.split("=", 1)
        fields[k.strip()] = v.strip()
    for required in ("name", "type", "localPort"):
        if not fields.get(required):
            raise FrpcLaunchError("代理规格缺少必填字段 %s: %s" % (required, spec))
    if fields["type"] not in ("tcp", "udp", "http", "https"):
        raise FrpcLaunchError("代理 type 取值无效: %s（只接受 tcp/udp/http/https）"
                              % fields["type"])
    if fields["type"] in ("tcp", "udp") and not fields.get("remotePort"):
        raise FrpcLaunchError("tcp/udp 代理必须提供 remotePort: %s" % spec)
    if fields["type"] in ("http", "https") and not fields.get("customDomains"):
        raise FrpcLaunchError("http/https 代理必须提供 customDomains: %s" % spec)
    for port_key in ("localPort", "remotePort"):
        if port_key in fields:
            fields[port_key] = _validated_port(fields[port_key], port_key)
    return fields


def _validated_port(value, label: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise FrpcLaunchError("%s 必须是整数: %r" % (label, value))
    if not 1 <= port <= 65535:
        raise FrpcLaunchError("%s 超出 1–65535 范围: %d" % (label, port))
    return port


def render_frpc_toml(server_addr: str, server_port: int, token: str, proxies: list) -> str:
    lines = ["serverAddr = %s" % json.dumps(server_addr),
             "serverPort = %d" % server_port]
    if token:
        lines.append("auth.token = %s" % json.dumps(token))
    for p in proxies:
        lines += ["", "[[proxies]]",
                  "name = %s" % json.dumps(p["name"]),
                  "type = %s" % json.dumps(p["type"]),
                  "localPort = %d" % int(p["localPort"])]
        if "remotePort" in p:
            lines.append("remotePort = %d" % int(p["remotePort"]))
        if "customDomains" in p:
            domains = [d.strip() for d in p["customDomains"].split(",") if d.strip()]
            lines.append("customDomains = [%s]" % ", ".join(json.dumps(d) for d in domains))
    return "\n".join(lines) + "\n"


def update_env_file(path: Path, updates: dict) -> None:
    # review N7：只有文件不存在才从空开始；读取失败必须中止，不得静默清空原文件
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise FrpcLaunchError(
                "读取 %s 失败（%s），中止写入以免丢失原有内容" % (path, e))
    else:
        lines = []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) in remaining:
            out.append("%s=%s" % (m.group(1), remaining.pop(m.group(1))))
        else:
            out.append(line)
    for k, v in remaining.items():
        out.append("%s=%s" % (k, v))
    tmp = path.with_name(path.name + ".tmp")
    _write_private(tmp, "\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _write_private(path: Path, text: str) -> None:
    """以 0600 权限创建并写入，避免默认 umask 下的 world-readable 窗口。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _git_secret_check(cwd: Path, target: Path, allow_tracked: bool):
    """project 作用域写 Secret 前的 git 安全检查；返回 (ok, reason)。"""
    if allow_tracked:
        return True, ""
    r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, cwd=str(cwd))
    if r.returncode != 0 or r.stdout.strip() != "true":
        return True, ""
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(target)],
                             capture_output=True, text=True, cwd=str(cwd))
    if tracked.returncode == 0:
        return False, "%s 已被 git 跟踪，拒绝写入 Secret（可用 --allow-tracked 显式覆盖）" % target
    ignored = subprocess.run(["git", "check-ignore", "-q", str(target)],
                             capture_output=True, cwd=str(cwd))
    if ignored.returncode != 0:
        return False, ("%s 未被 .gitignore 忽略，拒绝写入 Secret"
                       "（先把它加入 .gitignore，或用 --allow-tracked 显式覆盖）" % target)
    return True, ""


def cmd_guide_init(args) -> int:
    home = args.home
    cwd = Path.cwd()
    written = []
    token_masked = ""
    if args.source in ("frps", "baota"):
        token = os.environ.get("FRPC_LAUNCH_INIT_TOKEN", "")
        token_masked = mask_secret(token)
        if not args.server_addr or not args.server_port:
            raise FrpcLaunchError("official 引导必须提供 --server-addr 与 --server-port")
        _validated_port(args.server_port, "--server-port")
        proxies = [parse_proxy_spec(s) for s in args.proxies]
        toml_text = render_frpc_toml(args.server_addr, args.server_port, token, proxies)
        if args.scope == "global":
            ensure_home_layout(home)
            target = home / "frpc.toml"
        else:
            # review I1/N6：git 安全检查必须是写盘前 preflight——任一目标不通过，一个字节都不写
            target = cwd / "frpc.toml"
            preflight = ([target] if token else []) + [cwd / ".env.local"]
            for check_target in preflight:
                ok, reason = _git_secret_check(cwd, check_target, args.allow_tracked)
                if not ok:
                    print("错误: %s" % reason, file=sys.stderr)
                    return 6
        tmp = target.with_name(target.name + ".tmp")
        _write_private(tmp, toml_text)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        written.append(str(target))
        if not token:
            print("提示: 未设置 FRPC_LAUNCH_INIT_TOKEN，生成的配置不含 auth.token 行；"
                  "若服务端启用了 token 认证请补充", file=sys.stderr)
        if args.scope == "project":
            env_target = cwd / ".env.local"
            update_env_file(env_target, {"FRPC_LAUNCH_CONFIG": str(target.resolve())})
            written.append(str(env_target))
    else:
        key = os.environ.get("FRPC_LAUNCH_SAKURA_KEY", "")
        tunnels = os.environ.get("FRPC_LAUNCH_SAKURA_TUNNELS", "")
        token_masked = mask_secret(key)
        if not key or not tunnels:
            raise FrpcLaunchError(
                "sakura 引导必须经环境变量提供 FRPC_LAUNCH_SAKURA_KEY 与 "
                "FRPC_LAUNCH_SAKURA_TUNNELS（不接受命令行参数，防泄露）")
        updates = {"FRPC_LAUNCH_SAKURA_KEY": key, "FRPC_LAUNCH_SAKURA_TUNNELS": tunnels}
        if args.scope == "global":
            ensure_home_layout(home)
            target = home / ".env"
        else:
            target = cwd / ".env.local"
            ok, reason = _git_secret_check(cwd, target, args.allow_tracked)
            if not ok:
                print("错误: %s" % reason, file=sys.stderr)
                return 6
        update_env_file(target, updates)
        written.append(str(target))
    _emit(args, {"written": written, "scope": args.scope, "source": args.source,
                 "token": token_masked},
          "已写入: %s（token/key 回显掩码: %s）" % (", ".join(written), token_masked))
    return 0


# ---------------------------------------------------------------------------
# stop / status / logs
# ---------------------------------------------------------------------------


def _collect_secrets(layered: dict, home: Path) -> list:
    secrets = []
    o_path, _ = official_config_path(layered, home)
    if o_path is not None and o_path.is_file():
        try:
            secrets += _extract_official_secrets(o_path.read_text(encoding="utf-8"))
        except OSError:
            pass
    s_cfg, _ = sakura_config(layered)
    if s_cfg:
        secrets.append(s_cfg["key"])
    return secrets


def cmd_stop(args) -> int:
    import signal
    import time as _time
    home = args.home
    modes = [args.mode] if args.mode else ["official", "sakura"]
    results = {}
    for mode in modes:
        pid_file, _ = pid_paths(home, mode)
        record = read_pid_record(pid_file)
        if not record:
            results[mode] = {"result": "not_running"}
            continue
        if not pid_identity_ok(record):
            pid_file.unlink(missing_ok=True)
            if isinstance(record.get("pid"), int) and not pid_alive(record["pid"]):
                detail = "进程已退出，仅清理残留 pid 文件"
            else:
                detail = ("pid 记录身份核对不通过（进程号可能被复用），"
                          "只清理 pid 文件，未发送任何信号")
            results[mode] = {"result": "stale_pid_cleaned", "detail": detail}
            continue
        pid = record["pid"]
        os.kill(pid, signal.SIGTERM)
        deadline = _time.monotonic() + 10
        while pid_alive(pid) and _time.monotonic() < deadline:
            _time.sleep(0.2)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
        results[mode] = {"result": "stopped", "pid": pid}
    human = "; ".join("%s: %s" % (m, r["result"]) for m, r in results.items())
    _emit(args, {"modes": results}, human)
    return 0


def cmd_status(args) -> int:
    home = args.home
    layered = resolve_layered(dict(os.environ), Path.cwd(), home)
    secrets = _collect_secrets(layered, home)
    o_path, o_source = official_config_path(layered, home)
    _, s_source = sakura_config(layered)
    source_map = {"official": o_source, "sakura": s_source}
    try:
        _, os_subdir, _ = detect_platform()
    except FrpcLaunchError:
        os_subdir = ""
    modes = {}
    for mode in ([args.mode] if args.mode else ["official", "sakura"]):
        pid_file, log_file = pid_paths(home, mode)
        record = read_pid_record(pid_file)
        running = bool(record) and pid_identity_ok(record)
        binary_name = "frpc" if mode == "official" else "frpc-sakura"
        meta = read_meta(home / "bin" / os_subdir / (binary_name + ".meta.json")) \
            if os_subdir else {}
        modes[mode] = {
            "running": running,
            "verified": record.get("verified", True) if running else None,
            "pid": record.get("pid") if running else None,
            "config_source": source_map.get(mode, ""),
            "binary_version": meta.get("version", ""),
            "log_tail": mask_text(read_log_tail(log_file, 5), secrets),
        }
    human = "\n".join(
        "%s: %s%s%s" % (
            m,
            "运行中 (pid %s)" % v["pid"] if v["running"] else "未运行",
            "（未见登录成功证据）" if v["running"] and v["verified"] is False else "",
            "，配置来源: %s" % v["config_source"] if v["config_source"] else "")
        for m, v in modes.items())
    _emit(args, {"modes": modes}, human)
    return 0


def cmd_logs(args) -> int:
    home = args.home
    layered = resolve_layered(dict(os.environ), Path.cwd(), home)
    secrets = _collect_secrets(layered, home)
    modes = [args.mode] if args.mode else ["official", "sakura"]
    payload = {}
    chunks = []
    for mode in modes:
        _, log_file = pid_paths(home, mode)
        tail = mask_text(read_log_tail(log_file, args.lines), secrets)
        payload[mode] = tail
        if not tail and not args.mode:
            continue
        header = "===== %s =====" % mode if not args.mode else ""
        chunks.append((header + "\n" if header else "") + tail)
    _emit(args, {"modes": payload}, "\n".join(chunks))
    return 0


# ---------------------------------------------------------------------------
# start：后台拉起、有界启动验证、重复保护、配置变更不静默替换
# ---------------------------------------------------------------------------

# 失败文案经 E2E 实测核对：frp v0.70.1 在 token 错误/服务端不可达时输出
# "[W] connect to server error: ..." 并在重试窗口内保持存活，从不打印 "login to server failed"
OFFICIAL_SUCCESS_MARKERS = ("login to server success",)
OFFICIAL_FAILURE_MARKERS = ("connect to server error:", "login to server failed")
SAKURA_SUCCESS_MARKERS = ("login to server success", "start proxy success")
SAKURA_FAILURE_MARKERS = ("connect to server error:", "login to server failed")


def spawn_daemon(argv: list, env: dict, log_path: Path) -> int:
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(argv, env=env, stdout=log_f,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
    finally:
        log_f.close()
    return proc.pid


def wait_for_ready(pid: int, log_path: Path, success_markers, failure_markers,
                   timeout: float, poll: float = 0.5):
    import time as _time
    deadline = _time.monotonic() + timeout
    while True:
        tail = read_log_tail(log_path, 100)
        if any(m in tail for m in failure_markers):
            return "failed", tail
        if any(m in tail for m in success_markers) and pid_alive(pid):
            return "ok", tail
        if not pid_alive(pid):
            return "exited", tail
        if _time.monotonic() >= deadline:
            return "timeout", tail
        _time.sleep(poll)


# review M1/W7：匹配双引号、单引号与三引号（TOML basic/literal/multiline 字符串），
# 并覆盖 token 之外的凭证键
_SECRET_KEY_RE = re.compile(
    r'(?:token|clientSecret|password)\s*=\s*'
    r'(?:"""(.*?)"""|\'\'\'(.*?)\'\'\'|"([^"]*)"|\'([^\']*)\')')


def _extract_official_secrets(toml_text: str) -> list:
    result = []
    for groups in _SECRET_KEY_RE.findall(toml_text):
        value = next((g for g in groups if g), "")
        if value:
            result.append(value)
    return result


def _start_one_mode(args, mode: str, decision: ModeDecision, layered: dict) -> dict:
    home = args.home
    ensure_home_layout(home)
    pid_file, log_file = pid_paths(home, mode)
    secrets = []
    if mode == "official":
        config_path, config_source = decision.official
        if config_path is None:
            raise FrpcLaunchError("official 模式配置无效或缺失")
        try:
            secrets = _extract_official_secrets(config_path.read_text(encoding="utf-8"))
        except OSError:
            pass
        binary, origin = resolve_official_binary(layered, home)
        if binary is None:
            meta = install_official(home)
            print("已自动安装官方 frpc v%s（sha256 校验通过）" % meta["version"],
                  file=sys.stderr)
            binary, origin = resolve_official_binary(layered, home)
        try:
            verify = subprocess.run([str(binary), "verify", "-c", str(config_path)],
                                    capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise FrpcLaunchError("frpc verify 超时（30s），二进制可能异常: %s" % binary)
        if verify.returncode != 0:
            raise FrpcLaunchError(
                "frpc verify 未通过:\n%s" % mask_text(
                    (verify.stdout or "") + (verify.stderr or ""), secrets))
        digest = config_digest_official(config_path, binary)
        argv = [str(binary), "-c", str(config_path)]
        env = dict(os.environ)
        success_markers, failure_markers = OFFICIAL_SUCCESS_MARKERS, OFFICIAL_FAILURE_MARKERS
    else:
        s_cfg, config_source = decision.sakura
        if not s_cfg:
            raise FrpcLaunchError("sakura 模式配置无效或缺失")
        secrets = [s_cfg["key"]]
        binary, origin = resolve_sakura_binary(layered, home)
        if binary is None:
            meta = install_sakura(home)
            print("已自动安装樱花定制 frpc %s（size/hash 校验通过）" % meta["version"],
                  file=sys.stderr)
            binary, origin = resolve_sakura_binary(layered, home)
        digest = config_digest_sakura(s_cfg["key"], s_cfg["tunnels"], binary)
        argv = [str(binary)]
        env = dict(os.environ)
        env["NATFRP_TOKEN"] = s_cfg["key"]
        env["NATFRP_TARGET"] = s_cfg["tunnels"]
        success_markers, failure_markers = SAKURA_SUCCESS_MARKERS, SAKURA_FAILURE_MARKERS

    meta_now = read_meta(binary.with_name(binary.name + ".meta.json"))
    tail_now = mask_text(read_log_tail(log_file, 20), secrets)
    record = read_pid_record(pid_file)
    if record:
        if pid_identity_ok(record):
            if record.get("config_digest") == digest:
                # review M2：timeout 写入的记录标记 verified=false；
                # 二次 start 必须复核成功证据，不得把未登录成功的进程报成 0
                if not record.get("verified", True):
                    # review I2：复核成功证据必须扫全量日志（尾部窗口会被后续日志顶掉）；
                    # review W2：先掩码再截断，避免 secret 跨切片边界残留明文
                    try:
                        full_text = log_file.read_text(encoding="utf-8",
                                                       errors="replace")
                    except OSError:
                        full_text = ""
                    if any(m in full_text for m in success_markers):
                        record["verified"] = True
                        write_pid_record(pid_file, record)
                    else:
                        return {"mode": mode, "result": "running_unverified",
                                "pid": record["pid"], "config_source": config_source,
                                "binary_version": meta_now.get("version", ""),
                                "log_tail": mask_text(full_text, secrets)[-2000:],
                                "detail": "进程存活但仍未见登录成功证据；"
                                          "请检查日志或 stop 后重试", "exit_code": 1}
                return {"mode": mode, "result": "already_running",
                        "pid": record["pid"], "config_source": config_source,
                        "binary_version": meta_now.get("version", ""),
                        "log_tail": tail_now, "exit_code": 0}
            return {"mode": mode, "result": "config_changed",
                    "pid": record["pid"], "config_source": config_source,
                    "binary_version": meta_now.get("version", ""),
                    "log_tail": tail_now,
                    "detail": "运行中进程使用的配置与当前解析结果不一致；"
                              "请确认后先 stop 再 start，不会静默替换", "exit_code": 5}
        pid_file.unlink(missing_ok=True)
        print("发现 stale pid 记录（进程身份核对不通过），已清理 pid 文件", file=sys.stderr)

    log_file.write_text("")
    pid = spawn_daemon(argv, env, log_file)
    state, tail = wait_for_ready(pid, log_file, success_markers, failure_markers,
                                 timeout=args.wait)
    meta = read_meta(binary.with_name(binary.name + ".meta.json"))
    result = {"mode": mode, "result": state, "pid": pid,
              "config_source": config_source,
              "binary_version": meta.get("version", ""),
              "log_tail": mask_text(tail, secrets)}
    if state == "ok":
        write_pid_record(pid_file, {
            "pid": pid, "exe": str(binary), "mode": mode,
            "config_digest": digest, "verified": True,
            "started_at": datetime.now(timezone.utc).isoformat()})
        result["exit_code"] = 0
    else:
        result["exit_code"] = 1
        if state == "failed" and pid_alive(pid):
            # 启动验证已判失败：清理刚拉起的进程，不留孤儿（该 pid 是本次 spawn 的，可安全终止）
            import signal
            import time as _time
            os.kill(pid, signal.SIGTERM)
            deadline = _time.monotonic() + 5
            while pid_alive(pid) and _time.monotonic() < deadline:
                _time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            result["detail"] = "启动验证失败，已终止本次拉起的进程"
        if state == "timeout" and pid_alive(pid):
            write_pid_record(pid_file, {
                "pid": pid, "exe": str(binary), "mode": mode,
                "config_digest": digest, "verified": False,
                "started_at": datetime.now(timezone.utc).isoformat()})
            result["detail"] = "进程存活但未见成功证据（超时）；已保留进程与 pid 记录"
    return result


def cmd_start(args) -> int:
    layered = resolve_layered(dict(os.environ), Path.cwd(), args.home)
    decision = decide_mode(layered, args.home, args.mode)
    if decision.decision == "none":
        _emit(args, {"result": "unconfigured", "exit_code": 4},
              "未检测到任何配置（official frpc.toml 或 sakura 变量均无）。"
              "请先完成配置；可由 Agent 走引导流程（guide-init）。")
        return 4
    if decision.decision == "ambiguous":
        _emit(args, {"result": "ambiguous", "modes": decision.modes, "exit_code": 3},
              "official 与 sakura 配置同时存在且未指定模式，请用 --mode 指定"
              "（或由 Agent 询问用户选择）。")
        return 3
    mode = decision.modes[0]
    # review N1：显式指定的模式若无有效配置，同样进入引导流程（exit 4），并带上具体原因
    if (mode == "official" and decision.official[0] is None) or \
            (mode == "sakura" and decision.sakura[0] is None):
        reason = ""
        if mode == "official":
            o_path, _ = official_config_path(layered, args.home)
            reason = official_config_valid(o_path)[1] if o_path is not None \
                else "未找到 frpc.toml（工作区 FRPC_LAUNCH_CONFIG 与全局均缺失）"
        else:
            reason = "sakura 变量不完整（需 FRPC_LAUNCH_SAKURA_KEY 与 FRPC_LAUNCH_SAKURA_TUNNELS）"
        _emit(args, {"result": "unconfigured", "mode": mode, "reason": reason,
                     "exit_code": 4},
              "%s 模式无有效配置：%s。请先完成配置；可由 Agent 走引导流程（guide-init）。"
              % (mode, reason))
        return 4
    result = _start_one_mode(args, mode, decision, layered)
    exit_code = result.pop("exit_code")
    human_map = {
        "ok": "frpc（%s 模式）启动成功，配置来源: %s%s" % (
            mode, result.get("config_source", ""),
            "（使用了全局配置）" if result.get("config_source") == "global" else ""),
        "already_running": "frpc（%s 模式）已在运行（pid %s），未重复拉起" % (
            mode, result.get("pid")),
        "config_changed": result.get("detail", "配置已变更，需确认"),
    }
    human = human_map.get(result["result"],
                          "frpc（%s 模式）启动失败: %s\n%s" % (
                              mode, result["result"], result.get("log_tail", "")))
    _emit(args, result, human)
    return exit_code


def _emit(args, payload: dict, human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(human)


def cmd_install(args) -> int:
    mode = args.mode or "official"
    if mode == "sakura" and args.version:
        # review W4：与 update 一致，按用法错误处理
        print("用法错误: sakura 模式版本由上游 API 决定，不支持 --version", file=sys.stderr)
        return 2
    if mode == "official":
        layered = resolve_layered(dict(os.environ), Path.cwd(), args.home)
        try:
            binary, origin = resolve_official_binary(layered, args.home)
        except FrpcLaunchError as e:
            # review W1：install 是修复"没有可用二进制"的入口，显式路径失效降级为警告后继续安装
            print("警告: %s；忽略该显式路径，继续安装受管二进制" % e, file=sys.stderr)
            binary, origin = None, ""
        if origin == "managed":
            _, os_subdir, _ = detect_platform()
            meta = read_meta(args.home / "bin" / os_subdir / "frpc.meta.json")
            _emit(args, {"result": "already_installed", "meta": meta},
                  "已安装受管 frpc v%s，不重复下载；如需升级请用 update"
                  % meta.get("version", "?"))
            return 0
        meta = install_official(args.home, args.version)
        _emit(args, {"result": "installed", "meta": meta},
              "已安装官方 frpc v%s（sha256 校验通过）" % meta["version"])
        return 0
    layered = resolve_layered(dict(os.environ), Path.cwd(), args.home)
    try:
        binary, origin = resolve_sakura_binary(layered, args.home)
    except FrpcLaunchError as e:
        print("警告: %s；忽略该显式路径，继续安装受管二进制" % e, file=sys.stderr)
        binary, origin = None, ""
    if origin == "managed":
        _, os_subdir, _ = detect_platform()
        meta = read_meta(args.home / "bin" / os_subdir / "frpc-sakura.meta.json")
        _emit(args, {"result": "already_installed", "meta": meta},
              "已安装受管 frpc-sakura %s，不重复下载；如需升级请用 update"
              % meta.get("version", "?"))
        return 0
    meta = install_sakura(args.home)
    _emit(args, {"result": "installed", "meta": meta},
          "已安装樱花定制 frpc %s（size/hash 校验通过）" % meta["version"])
    return 0


def cmd_update(args) -> int:
    mode = args.mode or "official"
    if mode == "official":
        meta = install_official(args.home, args.version)
    else:
        if args.version:
            print("用法错误: sakura 模式版本由上游 API 决定，不支持 --version",
                  file=sys.stderr)
            return 2
        meta = install_sakura(args.home)
    _emit(args, {"result": "updated", "meta": meta},
          "已更新 %s 模式受管二进制至 %s" % (mode, meta["version"]))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "logs": cmd_logs,
        "install": cmd_install,
        "update": cmd_update,
        "guide-init": cmd_guide_init,
    }
    handler = handlers.get(args.command)
    if handler is None:
        print("NOT_IMPLEMENTED: %s" % args.command, file=sys.stderr)
        return 2
    try:
        return handler(args)
    except FrpcLaunchError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        # 兜底：外部命令缺失（xattr/ps/git）等环境性错误不吐裸堆栈
        print("错误: 环境调用失败: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
