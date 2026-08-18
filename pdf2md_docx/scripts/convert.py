#!/usr/bin/env python3
"""Doc2X V3 PDF conversion (md / tex / docx) via aihubmax.com.

Pipeline: local PDF -> (count pages) -> (upload for a public URL) ->
POST /v1/run/generations -> poll /v1/tasks/{id} -> download result ZIP ->
extract into a date-time-prefixed folder.

Key resolution chain (high -> low), value-deduped, 401 -> fall back to next.
Each source accepts AIHUB_API_KEY first, then the deprecated X_API_KEY:
  1. env AIHUB_API_KEY
  2. $PWD/.env.local         (auto, no flag)
  3. $PWD/.env               (auto, no flag)
  4. ~/.config/pdf2md_docx/.env  (only with --use-local-key)

401 does not consume credits, so the fallback is safe. It is the only status that
advances to the next key: every other status stops the key chain right there.

What happens to those other statuses depends on the call's semantics, not on the
key chain (ADR 0006):
  - 429 and 5xx are transient. On an idempotent GET (polling, downloading the
    result ZIP) they are retried in place — 3 attempts, backing off 1s then 2s,
    honouring `Retry-After` on a 429 (capped at 60s). On the billed write
    (creating the task) only 429 is retried, because a 429 means the server
    refused the request and no task was created; a 5xx leaves it unknown.
  - Deterministic statuses (402 credit exhausted, 422 invalid argument, 403,
    404, …) stop immediately and are reported to the caller as-is.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

# --------------------------------------------------------------------------- #
# 运行时 bootstrap（ADR 0007 §1.4 入口脚本侧兜底）
# 把「用哪个解释器跑」从调用方每轮的记忆变成结构性事实：不在目标 venv 就 os.execv
# 拉回去，venv 缺失就按 uv.lock 自动重建。
# **本段只用 Python 3.9 兼容语法**：它可能先被系统 python3 执行，用了新语法会在
# SyntaxError 阶段就死掉，兜底反而成了故障点。
# --------------------------------------------------------------------------- #

# 一次性再入护栏。标记值存**本轮目标 venv 的 realpath**而非布尔：外部环境 export
# 了同名变量时，值不匹配就不算本轮的再入，仍照常自动重建。
_REEXEC_ENV = "PDF2MD_DOCX_BOOTSTRAP_REEXEC"
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 重定位只认 uv 原生的 UV_PROJECT_ENVIRONMENT，且相对值**按项目根解析**——uv 就是
# 这么解析的；按 CWD 解析会在用户项目目录里设了相对值时 exec 进错的 venv。
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

# 同目录模块入 sys.path：直接执行本文件时 Python 会自动加入 scripts/，被当模块
# import 时不会。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import (  # noqa: E402
    NET_MAX_ATTEMPTS, RETRYABLE_STATUSES, AmbiguousWrite, backoff_seconds,
    call_with_key_fallback, is_transient_network_error, request_with_retry,
)
from config import KEY_NAME, legacy_key_notice, mask_key, resolve_api_key_candidates
from upload_helper import UploadHelperError, upload_local_file

MODEL = "doc2x-v3"
DEFAULT_BASE_URL = "https://api.aihubmax.com"
CONFIG_DIR = os.path.expanduser("~/.config/pdf2md_docx")

CONVERT_MODES = ("md", "tex", "docx")
FORMULA_MODES = ("normal", "dollar")

# 轮询的墙钟总预算（ADR 0006 规则 5）。值由默认档位推出：--max-attempts 90 ×
# --poll-interval 8s = 720s，即「按默认参数轮询走完全程」应该花的时间。次数预算
# 本身不约束时间——每一轮内部最坏要走 3 次物理请求 × 60s 超时 + 退避——所以需要
# 这条独立的墙钟闸。调用方把 interval / max_attempts 调大时，实际预算取两者较大
# 值（见 poll_task），不会被这个默认值砍短。
POLL_DEADLINE_SECONDS = 90 * 8

_HINTS = {
    400: "请求格式错误",
    401: "鉴权失败（key 无效/过期/权限不足）",
    402: "账户余额不足，请充值后再试",
    422: "参数校验失败",
    429: "请求频率超限（已按 ADR 0006 自动重试 3 次仍被限流，请稍后再试）",
    500: "服务器内部错误",
    503: "服务暂不可用，请稍后重试",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sanitize_label(raw: str, limit: int = 40) -> str:
    """Keep CJK/Unicode letters, drop filesystem-unsafe chars, collapse
    whitespace to '_', take the first `limit` code points."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", raw or "")
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:limit]


def count_pdf_pages(path: str) -> int:
    """Count pages with pypdf, then PyMuPDF (fitz). Raises if neither is available.

    pypdf / PyMuPDF 都是**可选**依赖（见 pyproject 的 optional-dependencies）：两者
    都没装时本函数报错，用户改用 `--page-count` 显式传页数即可完成转换，所以它们
    不进必需依赖。

    捕获面按 ADR 0007 §1.5 放宽到 `Exception`：真实 import 会执行包的顶层代码，
    半残环境（包目录在但传递依赖缺失、二进制与架构不符）里那里抛的可能是
    OSError / RuntimeError 等任意异常，只接 ImportError 会让它们穿过这里裸抛。
    底层异常原文一并透出，便于定位是「没装」还是「装坏了」。
    """
    reasons = []
    # import 与实际读取分开写：把 except 只罩在 import 上，读 PDF 本身出错
    # （文件损坏、加密）照常抛出，不会被误报成「依赖不可用」。
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 依赖不可用的统一映射，见 docstring
        reasons.append("pypdf: %s: %s" % (type(exc).__name__, exc))
    else:
        return len(PdfReader(path).pages)
    try:
        import fitz  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 同上
        reasons.append("PyMuPDF(fitz): %s: %s" % (type(exc).__name__, exc))
    else:
        with fitz.open(path) as doc:
            return doc.page_count
    raise RuntimeError(
        "无法自动统计 PDF 页数（pypdf / PyMuPDF 均不可用）。请用 --page-count 显式传入页数。\n"
        "  底层原因：\n    " + "\n    ".join(reasons)
    )


def doc2x_error_message(resp) -> str:
    server = ""
    if isinstance(resp.json, dict) and isinstance(resp.json.get("error"), dict):
        server = resp.json["error"].get("message", "")
    hint = _HINTS.get(resp.status, "未预期的响应")
    msg = "[HTTP %s] %s" % (resp.status, hint)
    if server:
        msg += " | 上游: " + server
    return msg


def unique_dir(parent: str, name: str) -> str:
    """Return parent/name, appending -2, -3, ... if it already exists."""
    candidate = os.path.join(parent, name)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, "%s-%d" % (name, i))
        i += 1
    return candidate


def safe_extract_zip(zip_path: str, dest_dir: str) -> list:
    """Extract a ZIP into dest_dir, refusing entries that escape dest_dir
    (zip-slip guard). Returns the list of extracted relative paths."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_abs = os.path.realpath(dest_dir)
    extracted = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            target = os.path.realpath(os.path.join(dest_dir, member))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise RuntimeError("ZIP 含越界路径，已拒绝解压: %r" % member)
            zf.extract(member, dest_dir)
            extracted.append(member)
    return extracted


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def create_task(body: dict, keys: list, base_url: str, transport=None, sleep=None):
    """POST /v1/run/generations. Returns (task_id, used_key). Raises on non-200.

    这是**计费写操作**：一次成功的调用会在上游创建任务并扣费。因此按 ADR 0006
    规则 4 走 `idempotent=False` 的重试策略——只重试 429（服务端限流拒绝，任务
    没建）与连接阶段失败（DNS/连接被拒，请求体没到服务端）；请求发出之后的超时
    抛 `AmbiguousWrite` 交给调用方提示用户去查任务状态，绝不盲重试（盲重试 =
    可能重复建任务、重复扣费）。
    """
    url = base_url + "/v1/run/generations"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def attempt(key):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        return request_with_retry("POST", url, headers, payload, idempotent=False,
                                  op="创建 Doc2X 任务", transport=transport, sleep=sleep)

    resp, used = call_with_key_fallback(keys, attempt)
    if resp.status == 200 and isinstance(resp.json, dict) and resp.json.get("id"):
        return resp.json["id"], used
    raise RuntimeError(doc2x_error_message(resp))


def poll_task(task_id: str, key: str, base_url: str, *, interval: int, max_attempts: int,
              transport=None, sleep=None, monotonic=None) -> dict:
    """Poll until status is completed (with non-empty results) or failed.
    A completed status with empty results is an upstream race — keep polling.

    单次轮询是幂等 GET：它的瞬时网络故障先在物理请求层按 ADR 0006 重试 3 次
    （退避 1s、2s）；内层重试全部失败也只让这一轮轮询作废、继续下一轮，而不是
    终止整个轮询。这样一次网络抖动不会击穿 `max_attempts` 的总预算语义（规则 5）；
    预算耗尽时统一报「轮询超时」终态并带上最后一次网络错误。

    **只有网络故障才作废本轮**：非网络异常（`TypeError`、`AttributeError` 这类
    编程错误，或注入 transport 抛出的断言）会原样抛出。否则它们会被这段循环反复
    吞掉 `max_attempts` 次，最后洗成一句「轮询超时（任务可能仍在运行）」——把一个
    确定性缺陷伪装成上游慢，用户与开发者都拿不到真实的失败原因。

    除了次数预算，还有一条**墙钟预算**（ADR 0006 规则 5）：次数不等于时间，每一轮
    内部最坏要走满 3 次物理请求 × 60s 超时 + 退避，90 轮理论上能挂到几个小时，与
    「90 轮 × 8s ≈ 12 分钟」的直觉相差两个数量级。到期即以同一句「轮询超时」终态
    返回，见 `POLL_DEADLINE_SECONDS`。
    """
    if sleep is None:
        sleep = time.sleep
    if monotonic is None:
        monotonic = time.monotonic
    url = base_url + "/v1/tasks/" + task_id + "?sync_upstream=true"
    headers = {"Authorization": "Bearer " + key}
    # 墙钟预算取「默认档位推出的 720s」与「本次调用参数的名义总时长」中的较大者：
    # 用户显式调大 interval / max_attempts 时不会被默认档位反过来砍短。
    deadline_budget = max(POLL_DEADLINE_SECONDS, interval * max_attempts)
    started = monotonic()
    last_net_error = None
    for attempt in range(1, max_attempts + 1):
        if monotonic() - started >= deadline_budget:
            log("[poll %d/%d] 墙钟预算 %ds 已耗尽，停止轮询"
                % (attempt, max_attempts, deadline_budget))
            break
        try:
            resp = request_with_retry("GET", url, headers, None, idempotent=True,
                                      op="轮询任务 %s" % task_id,
                                      transport=transport, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 - 分类见下：只作废网络故障那一轮
            if not is_transient_network_error(exc) and not isinstance(exc, AmbiguousWrite):
                raise
            last_net_error = exc
            log("[poll %d/%d] 网络故障（内层重试已穷尽）: %s；本轮作废，继续轮询"
                % (attempt, max_attempts, exc))
            if attempt < max_attempts:
                sleep(interval)
            continue
        last_net_error = None
        data = resp.json if isinstance(resp.json, dict) else {}
        status = str(data.get("status") or "unknown").lower()
        results = data.get("results") or []
        progress = data.get("progress")
        log("[poll %d/%d] status=%s progress=%s results=%d"
            % (attempt, max_attempts, status,
               progress if progress is not None else "-", len(results)))
        if status == "failed":
            return data
        if status == "completed":
            if results:
                return data
            log("[poll] status=completed 但 results 为空（上游竞态），继续轮询")
        if attempt < max_attempts:
            sleep(interval)
    msg = ("轮询超时（任务可能仍在运行）。task_id=%s，可稍后手动查询 "
           "GET %s" % (task_id, url))
    if last_net_error is not None:
        msg += "\n  最后一次轮询以网络故障告终：%s" % last_net_error
    raise RuntimeError(msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def download_zip(zip_url: str, *, timeout: int = 120, opener=None, sleep=None) -> bytes:
    """下载结果 ZIP。

    幂等 GET（同一 URL 反复取同一份内容，不产生副作用），所以瞬时失败按 ADR 0006
    规则 2/3 重试 3 次 + 指数退避。瞬时的判据有两条：

      1. 网络故障（超时、连接重置、DNS 失败）——`is_transient_network_error`；
      2. **HTTP 状态码 5xx / 429**（`RETRYABLE_STATUSES`）——对象存储在结果刚生成
         时返回 503，或限流返回 429，都是等一下就好的临时状态。

    确定性 4xx（404 链接过期、403 签名失效等）立即抛出：重试必然同样失败，只会让
    用户多等三轮才看到真正的错误。
    """
    if opener is None:
        opener = urllib.request.urlopen
    if sleep is None:
        sleep = time.sleep
    for attempt in range(1, NET_MAX_ATTEMPTS + 1):
        try:
            return opener(zip_url, timeout=timeout).read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUSES or attempt == NET_MAX_ATTEMPTS:
                raise  # 确定性 HTTP 错误：重试必然同样失败
            delay = backoff_seconds(attempt)
            log("[retry] 下载结果 ZIP 第 %d/%d 次尝试失败（HTTP %s），%ss 后重试"
                % (attempt, NET_MAX_ATTEMPTS, exc.code, delay))
            sleep(delay)
        except Exception as exc:  # noqa: BLE001 - 分类交给 is_transient_network_error
            if not is_transient_network_error(exc) or attempt == NET_MAX_ATTEMPTS:
                raise
            delay = backoff_seconds(attempt)
            log("[retry] 下载结果 ZIP 第 %d/%d 次尝试失败（网络瞬时故障: %s），%ss 后重试"
                % (attempt, NET_MAX_ATTEMPTS, exc, delay))
            sleep(delay)
    raise AssertionError("unreachable")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Doc2X V3：将 PDF 转为 md / tex / docx，结果 ZIP 自动解压到带日期时间的文件夹。")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="本地 PDF 路径（自动统计页数并上传换 URL）")
    src.add_argument("--pdf-url", help="已公开可下载的 PDF URL（需配合 --page-count）")

    p.add_argument("--convert-mode", choices=CONVERT_MODES, default="md",
                   help="输出格式，每次调用一种（默认 md）")
    p.add_argument("--formula-mode", choices=FORMULA_MODES, default="normal",
                   help="公式处理模式（默认 normal）")
    p.add_argument("--merge-cross-page-forms", action="store_true",
                   help="合并跨页表格（默认关）")
    p.add_argument("--page-count", type=int,
                   help="PDF 页数；本地 PDF 可省略（自动统计），--pdf-url 时必填")
    p.add_argument("--filename",
                   help="ZIP 内输出文档的文件名（不含扩展名，上游超 50 字截断）")

    p.add_argument("--output-dir",
                   help="解压输出根目录（默认 env PDF2MD_DOCX_OUTPUT_DIR 或 $PWD）")
    p.add_argument("--label", help="文件夹标签段（默认取 PDF 文件名前 40 字）")
    p.add_argument("--keep-zip", action="store_true", help="在输出文件夹内保留原始 ZIP")
    p.add_argument("--no-extract", action="store_true",
                   help="只下载 ZIP，不解压（ZIP 存到输出根目录）")

    p.add_argument("--poll-interval", type=int, default=8, help="轮询间隔秒（默认 8）")
    p.add_argument("--max-attempts", type=int, default=90, help="最大轮询次数（默认 90）")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="覆盖 API base URL")
    p.add_argument("--use-local-key", action="store_true",
                   help="允许读取 ~/.config/pdf2md_docx/.env")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    # --- keys ---
    candidates = resolve_api_key_candidates(os.environ, os.getcwd(), args.use_local_key, CONFIG_DIR)
    if not candidates:
        log("Error: 未找到 %s（env / $PWD/.env.local / $PWD/.env" % KEY_NAME
            + ("" if args.use_local_key else " ；如需读取 ~/.config 请加 --use-local-key") + "）")
        return 2
    notice = legacy_key_notice(candidates)
    if notice:
        log(notice)
    keys = [c.value for c in candidates]

    # --- resolve page_count and pdf_url ---
    label_source = args.label
    if args.pdf:
        if not os.path.isfile(args.pdf):
            log("Error: 本地 PDF 不存在: %s" % args.pdf)
            return 1
        page_count = args.page_count if args.page_count else count_pdf_pages(args.pdf)
        if label_source is None:
            label_source = os.path.splitext(os.path.basename(args.pdf))[0]
        log("[pdf] %s | page_count=%d" % (args.pdf, page_count))
        log("[upload] 上传本地 PDF 换取公网 URL …")
        try:
            pdf_url = upload_local_file(args.pdf, keys, base_url=args.base_url)
        except UploadHelperError as e:
            log("Error: 上传失败 %s" % e.message)
            return 1
        except AmbiguousWrite as e:
            log("Error: 上传结果不明 %s" % e)
            log("  上传请求已发出但没拿到应答，无法确认文件是否已落到上游存储。"
                "按 ADR 0006 规则 4 不自动重试——请稍后重跑本命令（上游文件 72 小时"
                "自动清理，重复上传只是多一份临时文件，不产生转换扣费）。")
            return 4
        except urllib.error.URLError as e:
            log("Error: 网络错误（upload）: %s" % e)
            return 1
        log("[upload] 完成（URL 72 小时后过期）: %s" % pdf_url)
    else:
        if not args.page_count:
            log("Error: 使用 --pdf-url 时必须显式提供 --page-count（无法自动统计远程 PDF 页数）")
            return 1
        pdf_url = args.pdf_url
        page_count = args.page_count
        if label_source is None:
            tail = pdf_url.split("?")[0].rstrip("/").split("/")[-1]
            label_source = os.path.splitext(tail)[0] or "doc2x"

    if page_count < 1:
        log("Error: page_count 必须 >= 1")
        return 1

    label = sanitize_label(label_source) or "doc2x"

    # --- build request body ---
    body = {
        "model": MODEL,
        "pdf_url": pdf_url,
        "page_count": page_count,
        "convert_mode": args.convert_mode,
        "formula_mode": args.formula_mode,
        "merge_cross_page_forms": bool(args.merge_cross_page_forms),
    }
    if args.filename:
        body["filename"] = args.filename

    log("Request summary:")
    log("- endpoint: %s/v1/run/generations" % args.base_url)
    log("- model: %s" % MODEL)
    log("- convert_mode: %s | formula_mode: %s | merge_cross_page_forms: %s"
        % (args.convert_mode, args.formula_mode, body["merge_cross_page_forms"]))
    log("- page_count: %d" % page_count)
    log("- key chain (high → low): %s" % ", ".join(mask_key(k) for k in keys))

    # --- create task ---
    try:
        task_id, used_key = create_task(body, keys, args.base_url)
    except AmbiguousWrite as e:
        log("Error: 创建任务结果不明 %s" % e)
        log("  请求已发出但没拿到应答，任务**可能已经创建并计费**。按 ADR 0006 规则 4"
            " 不自动重试，以免重复扣费。")
        log("  请先查上游任务列表确认是否已有本次任务：GET %s/v1/tasks（带 "
            "Authorization: Bearer <key>）；确认没有再重跑本命令。" % args.base_url)
        return 4
    except urllib.error.URLError as e:
        log("Error: 网络错误（create）: %s" % e)
        return 1
    except RuntimeError as e:
        log("Error: 创建任务失败 %s" % e)
        return 1
    log("[create] task_id=%s（使用 key %s）" % (task_id, mask_key(used_key)))

    # --- poll ---
    try:
        task = poll_task(task_id, used_key, args.base_url,
                         interval=args.poll_interval, max_attempts=args.max_attempts)
    except urllib.error.URLError as e:
        log("Error: 网络错误（poll）: %s" % e)
        log("  任务已创建（task_id=%s），未扣费重跑请直接查询上游任务状态。" % task_id)
        return 1
    except RuntimeError as e:
        log("Error: %s" % e)
        return 3

    if task.get("status") == "failed":
        err = task.get("error") or {}
        log("任务失败: [%s] %s" % (err.get("code") or err.get("type") or "?",
                                   err.get("message") or ""))
        return 2

    results = task.get("results") or []
    zip_url = (results[0] or {}).get("url") if results and isinstance(results[0], dict) else None
    if not zip_url:
        log("Error: 任务已完成但未找到结果 ZIP URL。原始响应: %s" % json.dumps(task, ensure_ascii=False)[:400])
        return 2
    log("[result] ZIP URL（24 小时后过期）: %s" % zip_url)

    # --- output dir ---
    output_root = args.output_dir or os.environ.get("PDF2MD_DOCX_OUTPUT_DIR") or os.getcwd()
    os.makedirs(output_root, exist_ok=True)

    # --- download ZIP ---
    try:
        zip_bytes = download_zip(zip_url)
    except Exception as e:  # noqa: BLE001 - report any download failure to the user
        log("Error: 下载结果 ZIP 失败（已重试 %d 次）: %s" % (NET_MAX_ATTEMPTS, e))
        log("  ZIP URL 24 小时内有效，可直接重跑下载或手工 curl 该地址。")
        return 1

    if args.no_extract:
        zip_path = os.path.join(output_root, "%s-%s.zip" % (timestamp, label))
        with open(zip_path, "wb") as fh:
            fh.write(zip_bytes)
        log("[save] 已下载 ZIP（未解压）: %s" % zip_path)
        print(zip_path)
        return 0

    # --- extract into a dated folder ---
    folder = unique_dir(output_root, "%s-%s" % (timestamp, label))
    os.makedirs(folder, exist_ok=True)
    tmp_zip = os.path.join(folder, "_doc2x_result.zip")
    with open(tmp_zip, "wb") as fh:
        fh.write(zip_bytes)
    try:
        members = safe_extract_zip(tmp_zip, folder)
    except (zipfile.BadZipFile, RuntimeError) as e:
        log("Error: 解压失败: %s" % e)
        return 1

    if args.keep_zip:
        kept = os.path.join(folder, "%s-%s.zip" % (timestamp, label))
        os.replace(tmp_zip, kept)
        log("[save] 保留 ZIP: %s" % kept)
    else:
        os.remove(tmp_zip)

    log("[save] 解压完成，共 %d 个文件 → %s" % (len(members), folder))
    for m in members:
        log("    %s" % m)
    print(folder)
    return 0


def cli() -> int:
    """main() 的兜底外壳：把未预期的异常收成一行可读报错，不给用户裸 traceback。"""
    try:
        return main()
    except KeyboardInterrupt:
        log("已中断。")
        return 130
    except AmbiguousWrite as e:
        log("Error: 写操作结果不明 %s" % e)
        log("  按 ADR 0006 规则 4 不自动重试；请先确认上游状态再决定是否重跑。")
        return 4
    except Exception as e:  # noqa: BLE001 - 顶层兜底
        log("Error: 未预期的错误 %s: %s" % (type(e).__name__, e))
        log("  完整堆栈（供排障）:")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(cli())
