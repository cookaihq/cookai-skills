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

401 does not consume credits, so the fallback is safe. Any other status (402 /
422 / 429 / 5xx / network) stops immediately and is reported to the caller.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

from client import call_with_key_fallback, http_request
from config import KEY_NAME, legacy_key_notice, mask_key, resolve_api_key_candidates
from upload_helper import UploadHelperError, upload_local_file

MODEL = "doc2x-v3"
DEFAULT_BASE_URL = "https://api.aihubmax.com"
CONFIG_DIR = os.path.expanduser("~/.config/pdf2md_docx")

CONVERT_MODES = ("md", "tex", "docx")
FORMULA_MODES = ("normal", "dollar")

_HINTS = {
    400: "请求格式错误",
    401: "鉴权失败（key 无效/过期/权限不足）",
    402: "账户余额不足，请充值后再试",
    422: "参数校验失败",
    429: "请求频率超限（不自动重试）",
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
    """Count pages with pypdf, then PyMuPDF (fitz). Raises if neither is available."""
    try:
        from pypdf import PdfReader  # type: ignore
        return len(PdfReader(path).pages)
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
        with fitz.open(path) as doc:
            return doc.page_count
    except ImportError:
        pass
    raise RuntimeError(
        "无法自动统计 PDF 页数（缺少 pypdf / PyMuPDF）。请用 --page-count 显式传入页数。"
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
def create_task(body: dict, keys: list, base_url: str, transport=None):
    """POST /v1/run/generations. Returns (task_id, used_key). Raises on non-200."""
    if transport is None:
        transport = http_request
    url = base_url + "/v1/run/generations"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def attempt(key):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        return transport("POST", url, headers, payload)

    resp, used = call_with_key_fallback(keys, attempt)
    if resp.status == 200 and isinstance(resp.json, dict) and resp.json.get("id"):
        return resp.json["id"], used
    raise RuntimeError(doc2x_error_message(resp))


def poll_task(task_id: str, key: str, base_url: str, *, interval: int, max_attempts: int,
              transport=None, sleep=None) -> dict:
    """Poll until status is completed (with non-empty results) or failed.
    A completed status with empty results is an upstream race — keep polling."""
    if transport is None:
        transport = http_request
    if sleep is None:
        sleep = time.sleep
    url = base_url + "/v1/tasks/" + task_id + "?sync_upstream=true"
    headers = {"Authorization": "Bearer " + key}
    for attempt in range(1, max_attempts + 1):
        resp = transport("GET", url, headers, None)
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
    raise RuntimeError(
        "轮询超时（任务可能仍在运行）。task_id=%s，可稍后手动查询 "
        "GET %s" % (task_id, url)
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
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
        zip_bytes = urllib.request.urlopen(zip_url, timeout=120).read()
    except Exception as e:  # noqa: BLE001 - report any download failure to the user
        log("Error: 下载结果 ZIP 失败: %s" % e)
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


if __name__ == "__main__":
    sys.exit(main())
