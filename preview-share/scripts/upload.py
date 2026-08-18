#!/usr/bin/env python3
"""preview-share: 把本地文件（及其关联资源）通过 FTP 上传到预览服务器，返回预览 URL。

核心行为：给一个入口文件（通常是 HTML），自动扫描它引用的本地资源
（src/href/<link>/<script>/url(...)/srcset 等），递归收集，保留相对目录结构
整体上传到唯一子目录下，使得在线打开的页面里所有相对引用都能正确解析。

配置读取优先级（见仓库 CLAUDE.md「Skills 配置读取优先级」通用约定）：
  1. 进程环境变量（本轮显式注入或已 export）
  2. $PWD/.env.local      （自动读，不向上递归）
  3. $PWD/.env            （自动读，不向上递归）
  4. ~/.config/preview-share/.env  （仅 --use-local-key 时读）
每个变量独立按此顺序取「首个非空来源」。
"""
import argparse
import datetime
import ftplib
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse

# ---------- 运行时 bootstrap（ADR 0007 §1.4 入口脚本侧兜底） ----------
# 作用：把「用哪个解释器跑」从调用方每轮的记忆变成结构性事实——不在目标 venv
# 就 os.execv 拉回去，venv 缺失就按 uv.lock 自动重建。
# **本段及本文件只用 Python 3.9 兼容语法**：它可能先被系统 python3 执行，用了
# 新语法会在 SyntaxError 阶段就死掉，兜底反而成了故障点。

# 一次性再入护栏。标记值存**本轮目标 venv 的 realpath**而非布尔：外部环境
# export 了同名变量时，值不匹配就不算本轮的再入，仍照常自动重建。
_REEXEC_ENV = "PREVIEW_SHARE_BOOTSTRAP_REEXEC"
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 重定位只认 uv 原生的 UV_PROJECT_ENVIRONMENT，且相对值**按项目根解析**——uv
# 就是这么解析的；按 CWD 解析会在用户项目目录里设了相对值时 exec 进错的 venv。
_VENV_DIR = os.path.join(_SKILL_DIR, os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")


def _bootstrap_fail(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(1)


def _bootstrap_manual_hint():
    # 必须 shell 引用：路径含空格时，未引用的 `rm -rf /tmp/sp ace/.venv` 被照抄
    # 执行会删掉两个无关路径。
    return "rm -rf %s && uv sync --project %s --no-dev" % (
        shlex.quote(_VENV_DIR), shlex.quote(_SKILL_DIR))


def _venv_is_valid():
    """有效 venv = 解释器在 + pyvenv.cfg 在。

    只判 bin/python 存在不够：sync 中断、手工建的同名目录、残留软链都会让解释器
    存在而目录不是 venv，此时跳过修复直接 execv 会陷入无限重启。
    """
    return (os.path.exists(_VENV_PY)
            and os.path.exists(os.path.join(_VENV_DIR, "pyvenv.cfg")))


def _require_uv():
    """uv 是系统级程序，缺失/版本过低只报错给命令，不擅自安装（ADR 0007 §4.2）。"""
    try:
        probe = subprocess.run(["uv", "--version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        _bootstrap_fail("uv 未安装。请执行：curl -LsSf https://astral.sh/uv/install.sh | sh")
    parts = probe.stdout.decode("utf-8", "replace").split()  # "uv 0.8.11 (...)"
    found = parts[1] if len(parts) > 1 else "0"
    try:
        numeric = tuple(int(x) for x in (found.split(".") + ["0", "0"])[:2])
    except ValueError:
        # uv 出错时 stdout 是 "error: ..."，硬 int() 会抛未捕获的 ValueError。
        numeric = (0, 0)
    if numeric < (0, 8):
        _bootstrap_fail("uv 版本过低（需 >= 0.8，当前 %s）。请执行：uv self update" % found)


def ensure_runtime():
    """确保当前进程运行在目标 venv 里；不是则 exec 拉回去，缺失则先重建。"""
    target = os.path.realpath(_VENV_DIR)
    if os.path.realpath(sys.prefix) == target:
        if os.environ.get(_REEXEC_ENV) == target:
            os.environ.pop(_REEXEC_ENV, None)  # 别让子进程继承标记后误判
        return
    if os.environ.get(_REEXEC_ENV) == target:
        _bootstrap_fail("运行环境异常：已重启到 %s 但解释器仍不在该 venv 内，目录疑似损坏。\n"
                        "请手工重建：%s" % (_VENV_DIR, _bootstrap_manual_hint()))
    if not _venv_is_valid():
        _require_uv()
        sys.stderr.write("[bootstrap] 运行环境缺失，正在按 uv.lock 重建 %s ...\n" % _VENV_DIR)
        try:
            # `--no-dev`：这里重建的是**运行时**环境。不加的话 uv 会把 dev 组
            # （pytest 等）一并装进 .venv，让运行时环境带上只有跑测试才需要的包。
            # 跑测试时显式用 `uv sync --project <dir> --group dev`。
            sync = subprocess.run(["uv", "sync", "--project", _SKILL_DIR, "--no-dev"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=600)  # 网络调用必设总预算（ADR 0006）
        except subprocess.TimeoutExpired:
            _bootstrap_fail("uv sync 超过 600 秒未完成，疑似网络异常。请手工执行："
                            + _bootstrap_manual_hint())
        if sync.returncode != 0 or not _venv_is_valid():
            _bootstrap_fail("uv sync 失败，无法重建运行环境（请手工执行：%s）：\n%s"
                            % (_bootstrap_manual_hint(),
                               sync.stdout.decode("utf-8", "replace")))
    os.environ[_REEXEC_ENV] = target  # putenv，execv 后的进程读得到
    os.execv(_VENV_PY, [_VENV_PY] + sys.argv)  # 拉回目标解释器重启自身


if __name__ == "__main__":
    ensure_runtime()

VARS = ("PREVIEW_SHARE_FTP", "PREVIEW_SHARE_BASEURL")

# 文本类入口会被递归扫描依赖；其它一律按二进制原样上传
SCANNABLE_EXT = {".html", ".htm", ".css", ".svg"}

# 跳过的非本地引用前缀
SKIP_PREFIXES = ("http://", "https://", "//", "data:", "mailto:",
                 "tel:", "javascript:", "#", "blob:")

# HTML/CSS 中提取引用的正则
RE_ATTR = re.compile(r"""(?:src|href|poster|data-src|background)\s*=\s*["']([^"']+)["']""", re.I)
RE_SRCSET = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.I)
RE_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.I)


def log(msg):
    print(msg, file=sys.stderr)


def mask_ftp(url):
    """ftp://user:pass@host -> ftp://user:***@host，日志安全。"""
    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        user = f"{u.username}:***@" if u.username else ""
        return f"{u.scheme}://{user}{host}{port}{u.path}"
    except Exception:
        return "ftp://***"


# ---------- 配置读取（分层优先级） ----------

def parse_env_file(path):
    """极简 .env 解析：KEY=value / KEY="value" / KEY='value'，# 注释，空行。
    不做 shell 展开 / 命令替换 / 续行。同名取最后一次。"""
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                vals[k] = v
    except FileNotFoundError:
        pass
    return vals


def resolve_config(use_local_key):
    """返回 (config dict, sources dict)。每个变量独立取首个非空来源。"""
    layers = []  # (label, dict)
    layers.append(("env", {k: os.environ[k] for k in VARS if os.environ.get(k)}))
    layers.append(("$PWD/.env.local", parse_env_file(os.path.join(os.getcwd(), ".env.local"))))
    layers.append(("$PWD/.env", parse_env_file(os.path.join(os.getcwd(), ".env"))))
    if use_local_key:
        home_env = os.path.expanduser("~/.config/preview-share/.env")
        layers.append(("~/.config/preview-share/.env", parse_env_file(home_env)))

    cfg, src = {}, {}
    for var in VARS:
        for label, d in layers:
            if d.get(var):
                cfg[var] = d[var]
                src[var] = label
                break
    return cfg, src


# ---------- 依赖扫描 ----------

def clean_ref(ref):
    """归一化一个引用：去 query/fragment；非本地返回 None。"""
    ref = ref.strip()
    if not ref:
        return None
    low = ref.lower()
    if any(low.startswith(p) for p in SKIP_PREFIXES):
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    if not ref or ref.startswith("/"):  # 绝对文件系统路径不处理
        return None
    return ref


def extract_refs(text):
    refs = set()
    for m in RE_ATTR.finditer(text):
        r = clean_ref(m.group(1))
        if r:
            refs.add(r)
    for m in RE_SRCSET.finditer(text):
        for cand in m.group(1).split(","):
            r = clean_ref(cand.strip().split()[0]) if cand.strip() else None
            if r:
                refs.add(r)
    for m in RE_CSS_URL.finditer(text):
        r = clean_ref(m.group(1))
        if r:
            refs.add(r)
    return refs


def scan_deps(entry):
    """从 entry 出发递归收集本地依赖文件，返回去重后的绝对路径集合（含 entry）。"""
    entry = os.path.realpath(entry)
    collected = {entry}
    queue = [entry]
    missing = []
    while queue:
        cur = queue.pop()
        if os.path.splitext(cur)[1].lower() not in SCANNABLE_EXT:
            continue
        try:
            with open(cur, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        base = os.path.dirname(cur)
        for ref in extract_refs(text):
            target = os.path.realpath(os.path.join(base, ref))
            if target in collected:
                continue
            if os.path.isfile(target):
                collected.add(target)
                queue.append(target)
            else:
                missing.append((cur, ref))
    return collected, missing


# ---------- FTP 上传（网络抖动处理见 ADR 0006） ----------

# ADR 0006 规则 3 默认参数：总尝试 3 次（首次 + 2 次重试），第 n 次重试前等 2^(n-1) 秒。
NET_MAX_ATTEMPTS = 3


def backoff_seconds(attempt):
    """attempt 为刚失败的那次尝试序号（1 起）；返回本次重试前的等待秒数。"""
    return 2 ** (attempt - 1)


def is_transient_ftp_error(exc):
    """区分瞬时故障与确定性错误（ADR 0006 规则 2）。

    - `error_perm`：FTP 5xx 永久否定应答（530 鉴权失败、550 无权限/不存在）。
      重试必然同样失败，禁止重试。
    - `error_temp`：FTP 4xx 临时否定应答（421 服务不可用、425 数据连接建立失败、
      450 资源暂时不可用）——协议层明确标为「临时」，属瞬时。
    - 三类 socket 层故障：`socket.timeout` / `TimeoutError`（读写超时）、
      `ConnectionError`（connection reset、broken pipe、连接被拒）、
      `socket.gaierror`（DNS 解析失败）。

    **这里刻意不写成裸 `OSError`**：`OSError` 把 `FileNotFoundError`、
    `PermissionError`、`IsADirectoryError`、`NotADirectoryError` 这些**本地文件系统
    错误**也一并收进来了。它们是确定性的本地问题，重试必然同样失败，而被当成瞬时
    网络故障后，用户看到的会是「传输中断 / 无法查询远端状态」，被引去排查 FTP 服务
    器，真正的原因（本地文件不存在或没权限）反而被盖住。

    其余（error_reply / error_proto 等协议异常）同样不当瞬时，避免在语义不明时盲
    重试。
    """
    if isinstance(exc, ftplib.error_perm):
        return False
    if isinstance(exc, ftplib.error_temp):
        return True
    # socket.timeout 在 3.10+ 就是 TimeoutError；3.9 上两者是不同的 OSError 子类，
    # 都代表超时，所以两个都列。
    return isinstance(
        exc, (socket.timeout, TimeoutError, ConnectionError, socket.gaierror)
    )


def retry_log(op, attempt, delay, exc):
    log("[retry] %s 第 %d/%d 次尝试失败（%s），%ds 后重试"
        % (op, attempt, NET_MAX_ATTEMPTS, exc, delay))


class AmbiguousUpload(Exception):
    """写操作结果不明，且无法先查后写确认——按 ADR 0006 规则 4 不盲重试。"""


class LocalSourceError(Exception):
    """本地待上传文件读不了（不存在 / 没权限 / 是目录）。

    单独立一个类型而不是混进 FTP 错误：这是确定性的**本地**问题，与服务器无关，
    报错必须直说「检查本地路径」，否则用户会照着 FTP 报错去查服务器和网络。
    """


class FtpSession:
    """持有连接参数的 FTP 会话。

    单独封装的理由：瞬时故障后既要能重建控制连接，又要能在重试前用 SIZE 查远端
    状态（先查后写），两者都需要保留原始连接参数，裸 `ftplib.FTP` 对象做不到。
    """

    def __init__(self, parts, timeout):
        self.parts = parts
        self.timeout = timeout
        self.ftp = None
        self.size_supported = True  # 服务器是否支持 SIZE（决定能否先查后写）

    # -- 连接 --

    def _open_once(self):
        ftp = ftplib.FTP()
        ftp.connect(self.parts.hostname, self.parts.port or 21, timeout=self.timeout)
        ftp.login(self.parts.username or "",
                  urllib.parse.unquote(self.parts.password or ""))
        ftp.set_pasv(True)
        return ftp

    def open(self):
        """建立连接并登录。

        connect/login 是只读握手，重连不产生任何远端副作用，天然幂等——ADR 0006
        规则 4 对写操作的先查后写要求在此不适用，瞬时失败可直接重试。
        """
        for attempt in range(1, NET_MAX_ATTEMPTS + 1):
            try:
                self.ftp = self._open_once()
                return
            except Exception as exc:
                if not is_transient_ftp_error(exc) or attempt == NET_MAX_ATTEMPTS:
                    raise
                delay = backoff_seconds(attempt)
                retry_log("FTP 连接/登录", attempt, delay, exc)
                time.sleep(delay)

    def _reopen(self):
        """瞬时故障常把控制连接一并打断；重连后才能继续 SIZE 查询与重传。"""
        try:
            self.ftp.close()
        except Exception:
            pass
        try:
            self.ftp = self._open_once()
            return True
        except Exception:
            return False

    def close(self):
        try:
            self.ftp.quit()
        except Exception:
            pass

    # -- 先查后写用的远端状态查询 --

    def remote_size(self, remote):
        """返回远端文件字节数；None = 不存在；-1 = 无法查询（服务器不支持 SIZE）。"""
        if not self.size_supported:
            # 本次会话已确认服务器不认 SIZE，不必每个文件再问一遍。
            return -1
        for attempt in (1, 2):
            try:
                self.ftp.voidcmd("TYPE I")  # SIZE 在 ASCII 模式下多数服务器拒绝
                return self.ftp.size(remote)
            except ftplib.error_perm as exc:
                code = str(exc)[:3]
                if code == "550":
                    return None  # 文件不存在（或不可读），视为「上次写入未生效」
                # 500/502 = 未实现该命令；其余永久否定同样意味着这台服务器查不到
                # 状态。这是服务器能力问题，对整个会话成立，记下来别再逐个文件问。
                self.size_supported = False
                return -1
            except Exception:
                # 控制连接断了：这是**本次**的瞬时故障，不是服务器不支持 SIZE，
                # 所以不能把 size_supported 置 False（否则后续文件全都退化成
                # ambiguous）。重连一次再问，仍失败才认这一次查不到。
                if attempt == 1 and self._reopen():
                    continue
                return -1

    # -- 写操作 --

    def ensure_remote_dir(self, path):
        """逐级 MKD，已存在则忽略。

        MKD 是幂等性可保证的写：本函数已把「目录已存在」的 550 当成功容忍，等价于
        先查后写（ADR 0006 规则 4 第二种情形）——重试要么真正建成，要么撞上已存在
        被容忍，不会产生重复副作用。因此瞬时失败可安全重试。
        """
        parts = [p for p in path.split("/") if p]
        built = "/" if path.startswith("/") else ""
        for p in parts:
            built = built.rstrip("/") + "/" + p
            for attempt in range(1, NET_MAX_ATTEMPTS + 1):
                try:
                    self.ftp.mkd(built)
                    break
                except ftplib.error_perm as exc:
                    # 550 通常是「已存在」，其它权限错误才需要关注
                    if not str(exc).startswith("550"):
                        raise
                    break
                except Exception as exc:
                    if not is_transient_ftp_error(exc) or attempt == NET_MAX_ATTEMPTS:
                        raise
                    delay = backoff_seconds(attempt)
                    retry_log("建远程目录 %s" % built, attempt, delay, exc)
                    time.sleep(delay)
                    self._reopen()  # 瞬时故障可能已打断控制连接

    def upload_file(self, local, remote, local_size):
        """上传单个文件，返回 "uploaded" 或 "already-complete"。

        STOR 对同一路径是覆盖写，但瞬时中断会在远端留下**半截文件**，直接盲重传
        无法判断上一轮是否已写完，属 ADR 0006 规则 4 说的结果不明。这里实现先查
        后写使重试安全：重试前用 SIZE 查远端该文件字节数——
          - 与本地等长：上一轮实际已写完，跳过，不重传；
          - 不等长或不存在：STOR 覆盖重传（覆盖写本身幂等）。
        服务器不支持 SIZE 时先查后写语义不成立，按 ambiguous 报给调用方，
        **不盲重试**。

        打开本地文件在重试循环**之外**：本地文件打不开是确定性的本地错误，放在循环
        内会被下面的 `except` 一路当成传输失败——重试三轮、SIZE 探测、最后报成
        「网络中断且无法查询远端状态」，把用户引去排查 FTP 服务器。这里直接抛
        `LocalSourceError`，一次就说清是本地路径的问题。
        """
        try:
            handle = open(local, "rb")
        except OSError as exc:
            raise LocalSourceError(
                "读取本地文件失败 %s: %s" % (local, exc)) from exc
        with handle as fh:
            for attempt in range(1, NET_MAX_ATTEMPTS + 1):
                try:
                    # 重传必须从文件头开始：上一轮 storbinary 已经把文件指针读到了
                    # 中断的位置，不 seek 会把残缺的后半段当成整个文件传上去。
                    fh.seek(0)
                    self.ftp.storbinary("STOR " + remote, fh)
                    return "uploaded"
                except Exception as exc:
                    if not is_transient_ftp_error(exc):
                        raise
                    if attempt == NET_MAX_ATTEMPTS:
                        raise
                    probed = self.remote_size(remote)
                    if probed == -1:
                        raise AmbiguousUpload(
                            "上传 %s 时网络中断（%s），且无法查询远端状态"
                            "（服务器不支持 SIZE，或控制连接已不可用），"
                            "无法确认远端是否已写完——不盲重试。请确认服务器状态后手工重跑本次上传。"
                            % (remote, exc))
                    if probed == local_size:
                        log("[retry] %s 传输中断，但远端已是完整文件（%dB），跳过重传"
                            % (remote, local_size))
                        return "already-complete"
                    delay = backoff_seconds(attempt)
                    retry_log("上传 %s" % remote, attempt, delay, exc)
                    time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(description="FTP 上传文件及其关联资源，返回预览 URL")
    ap.add_argument("entry", help="入口文件（其 URL 会被返回；通常是 preview.html）")
    ap.add_argument("--include", action="append", default=[],
                    help="额外强制包含的文件或目录（相对/绝对皆可），可重复")
    ap.add_argument("--label", help="子目录标签，最终子目录 = {时间戳}-{标签}")
    ap.add_argument("--subdir", help="直接指定远程子目录名（覆盖 时间戳-标签）")
    ap.add_argument("--no-scan", action="store_true",
                    help="不扫描依赖，只上传 entry 与 --include 指定项")
    ap.add_argument("--use-local-key", action="store_true",
                    help="允许读取 ~/.config/preview-share/.env")
    ap.add_argument("--timeout", type=int, default=300,
                    help="FTP 连接/传输超时秒数（默认 300，大文件可调大）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析并打印文件清单与预览 URL，不真正上传")
    args = ap.parse_args()

    if not os.path.isfile(args.entry):
        log(f"[error] 入口文件不存在: {args.entry}")
        return 2

    cfg, src = resolve_config(args.use_local_key)
    missing_cfg = [v for v in VARS if not cfg.get(v)]
    if missing_cfg:
        log(f"[error] 缺少配置: {', '.join(missing_cfg)}")
        log("  按优先级设置：进程环境变量 > $PWD/.env.local > $PWD/.env > ~/.config/preview-share/.env(--use-local-key)")
        return 2
    log(f"[config] PREVIEW_SHARE_FTP <- {src['PREVIEW_SHARE_FTP']}  ({mask_ftp(cfg['PREVIEW_SHARE_FTP'])})")
    log(f"[config] PREVIEW_SHARE_BASEURL <- {src['PREVIEW_SHARE_BASEURL']}  ({cfg['PREVIEW_SHARE_BASEURL']})")

    u = urllib.parse.urlparse(cfg["PREVIEW_SHARE_FTP"])
    if u.scheme != "ftp":
        log(f"[error] PREVIEW_SHARE_FTP scheme 必须是 ftp，实际: {u.scheme}（如需 sftp/ftps 请扩展脚本）")
        return 2
    remote_base = u.path.rstrip("/") or ""
    baseurl = cfg["PREVIEW_SHARE_BASEURL"].rstrip("/")

    # 收集文件
    files = set()
    missing_refs = []
    if args.no_scan:
        files.add(os.path.realpath(args.entry))
    else:
        files, missing_refs = scan_deps(args.entry)
    for inc in args.include:
        p = os.path.realpath(inc)
        if os.path.isdir(p):
            for root, _, fns in os.walk(p):
                for fn in fns:
                    files.add(os.path.join(root, fn))
        elif os.path.isfile(p):
            files.add(p)
        else:
            log(f"[warn] --include 未找到: {inc}")

    files = sorted(files)
    root = os.path.dirname(os.path.realpath(args.entry)) if len(files) == 1 \
        else os.path.commonpath(files)
    if os.path.isfile(root):
        root = os.path.dirname(root)

    # 子目录命名
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.subdir:
        subdir = args.subdir
    elif args.label:
        subdir = f"{ts}-{args.label}"
    else:
        subdir = ts
    subdir = subdir.strip("/")

    entry_rel = os.path.relpath(os.path.realpath(args.entry), root).replace(os.sep, "/")
    preview_url = f"{baseurl}/{subdir}/{entry_rel}"

    # 打印清单
    log(f"\n[plan] 本地根目录: {root}")
    log(f"[plan] 远程子目录: {remote_base}/{subdir}/")
    log(f"[plan] 待上传 {len(files)} 个文件:")
    plan = []
    for f in files:
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        remote = f"{remote_base}/{subdir}/{rel}"
        plan.append((f, remote, rel))
        size = os.path.getsize(f)
        mark = "  <== 入口" if rel == entry_rel else ""
        log(f"   {rel}  ({size:,}B) -> {remote}{mark}")
    if missing_refs:
        log(f"\n[warn] 以下引用在本地未找到（不会上传，线上可能裂图）:")
        for parent, ref in missing_refs:
            log(f"   {os.path.relpath(parent, root)} -> {ref}")

    log(f"\n[preview] {preview_url}")

    if args.dry_run:
        log("\n[dry-run] 未上传。")
        print(preview_url)
        return 0

    # 上传
    log(f"\n[ftp] 连接 {u.hostname}:{u.port or 21} 用户 {u.username} …")
    session = FtpSession(u, args.timeout)
    try:
        session.open()
    except ftplib.error_perm as e:
        log(f"[error] FTP 登录失败（认证/权限）: {e}")
        return 3
    except Exception as e:
        log(f"[error] FTP 连接失败（已按 ADR 0006 重试 {NET_MAX_ATTEMPTS} 次）: {e}")
        return 3

    made_dirs = set()
    try:
        for local, remote, rel in plan:
            rdir = os.path.dirname(remote)
            if rdir not in made_dirs:
                session.ensure_remote_dir(rdir)
                made_dirs.add(rdir)
            outcome = session.upload_file(local, remote, os.path.getsize(local))
            log(f"   ✓ {rel}" + ("  (远端已完整，跳过重传)" if outcome == "already-complete" else ""))
    except LocalSourceError as e:
        # 与 FTP 无关：直说是本地文件的问题，不要让用户去查服务器和网络。
        log(f"[error] {e}")
        log("  这是本地文件错误，与 FTP 服务器无关：请检查该路径是否存在、是否可读。")
        session.close()
        return 2
    except AmbiguousUpload as e:
        log(f"[ambiguous] {e}")
        session.close()
        return 3
    except Exception as e:
        log(f"[error] 上传中断: {e}")
        session.close()
        return 3
    session.close()

    log(f"\n[done] 已上传 {len(plan)} 个文件。")
    print(preview_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
