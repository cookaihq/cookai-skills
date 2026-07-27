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


def resolve_official_binary(layered: dict, home: Path):
    explicit = layered.get("FRPC_LAUNCH_FRPC")
    if explicit:
        return Path(explicit[0]).expanduser(), "explicit"
    _, os_subdir, _ = detect_platform()
    managed = home / "bin" / os_subdir / "frpc"
    if managed.is_file() and os.access(managed, os.X_OK):
        return managed, "managed"
    found = shutil.which("frpc")
    if found:
        r = subprocess.run([found, "--version"], capture_output=True, text=True)
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
        return Path(explicit[0]).expanduser(), "explicit"
    _, os_subdir, _ = detect_platform()
    managed = home / "bin" / os_subdir / "frpc-sakura"
    if managed.is_file() and os.access(managed, os.X_OK):
        return managed, "managed"
    return None, ""


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
    return p


def _emit(args, payload: dict, human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(human)


def cmd_install(args) -> int:
    mode = args.mode or "official"
    if mode == "official":
        layered = resolve_layered(dict(os.environ), Path.cwd(), args.home)
        binary, origin = resolve_official_binary(layered, args.home)
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
    binary, origin = resolve_sakura_binary(layered, args.home)
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
        "install": cmd_install,
        "update": cmd_update,
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


if __name__ == "__main__":
    sys.exit(main())
