from __future__ import annotations

import argparse
import json
import os
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。放在
# `__main__` 守卫里，好让 tests 把本模块 import 进来时不触发 execv。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

from client import (BILLING_WRITE, UPLOAD_TIMEOUT, AmbiguousRequest,  # noqa: E402
                    call_with_key_fallback, encode_multipart, http_request,
                    request_with_retry)
from config import (KEY_NAME, legacy_key_notice, mask_key,  # noqa: E402
                    resolve_api_key_candidates)


BASE_URL = "https://api.aihubmax.com"
CONFIG_DIR = os.path.expanduser("~/.config/memoji-sticker-pack")


def build_request(
    mode,
    *,
    base_url,
    file_bytes=None,
    filename=None,
    file_data=None,
    url=None,
    file_name=None,
    auto_cleanup=True,
) -> tuple:
    """Return the URL, headers, and body for one upload mode."""
    if mode == "stream":
        fields = {"auto_cleanup": "true" if auto_cleanup else "false"}
        if file_name:
            fields["file_name"] = file_name
        content_type, body = encode_multipart(
            fields, "file", filename, file_bytes
        )
        return (
            base_url + "/v1/files/upload/stream",
            {"Content-Type": content_type},
            body,
        )
    if mode == "base64":
        payload = {"file_data": file_data, "auto_cleanup": auto_cleanup}
        if file_name:
            payload["file_name"] = file_name
        return (
            base_url + "/v1/files/upload/base64",
            {"Content-Type": "application/json"},
            json.dumps(payload).encode(),
        )
    if mode == "url":
        payload = {"url": url, "auto_cleanup": auto_cleanup}
        if file_name:
            payload["file_name"] = file_name
        return (
            base_url + "/v1/files/upload/url",
            {"Content-Type": "application/json"},
            json.dumps(payload).encode(),
        )
    raise ValueError("unknown upload mode: %r" % mode)


ERROR_HINTS = {
    400: "请求格式错误",
    401: "鉴权失败（key 无效 / 缺失 / 权限不足）",
    403: "存储空间不足",
    413: "文件过大，请压缩或更换更小的文件",
    429: "请求频率超限（已按 Retry-After / 指数退避自动重试 3 次仍未通过），请稍后再试",
    500: "服务器内部错误（结果不明：文件可能已存入但没拿到 URL，未自动重试）",
}

# 上传是写操作、无幂等键（ADR 0006 规则 4）：
#   - 429 明确表示本次未受理 → 可安全重试，带 Retry-After 时按该值等待。
#   - 连接建立/发送阶段失败（URLError）→ 请求没发出去，可安全重试。
#   - 5xx 与「已发出但响应丢失」→ 无法确认服务端是否已存下这个文件，按结果不明
#     处理：5xx 直接报 UploadError（消息里写明结果不明），响应丢失由
#     request_with_retry 抛 client.AmbiguousRequest，都不盲重试。
UPLOAD_RETRYABLE_STATUSES = frozenset([429])


class UploadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def run_upload(full_url, headers, body, keys, transport=None, sleep=None, log=None) -> tuple:
    if transport is None:
        transport = http_request

    def attempt(key):
        request_headers = dict(headers)
        request_headers["Authorization"] = "Bearer " + key
        return request_with_retry(
            lambda: transport("POST", full_url, request_headers, body,
                              timeout=UPLOAD_TIMEOUT),
            op="upload", write_safety=BILLING_WRITE,
            retryable_statuses=UPLOAD_RETRYABLE_STATUSES, sleep=sleep, log=log)

    return call_with_key_fallback(keys, attempt)


def interpret_upload(response) -> dict:
    if (
        response.status == 200
        and isinstance(response.json, dict)
        and response.json.get("url")
    ):
        return response.json
    if response.status == 403 and "error code: 1010" in (
        response.text or ""
    ).lower():
        hint = "请求被 Cloudflare 拒绝（error code: 1010，请检查 User-Agent）"
    elif 500 <= response.status <= 599:
        hint = ERROR_HINTS.get(
            response.status,
            "服务器错误（结果不明：文件可能已存入但没拿到 URL，未自动重试）",
        )
    else:
        hint = ERROR_HINTS.get(response.status, "未预期的响应")
    server_message = ""
    if isinstance(response.json, dict) and isinstance(
        response.json.get("error"), dict
    ):
        server_message = response.json["error"].get("message", "")
    message = "[HTTP %s] %s" % (response.status, hint)
    if server_message:
        message += " | 上游: " + server_message
    raise UploadError(response.status, message)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Upload a Memoji reference image to a 72h public URL"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="local file path")
    source.add_argument("--base64", dest="base64_data", help="raw base64 or data URL")
    source.add_argument("--url", help="remote URL to fetch and re-host")
    parser.add_argument("--file-name", help="override stored file name")
    parser.add_argument("--no-auto-cleanup", action="store_true")
    parser.add_argument(
        "--use-local-key",
        action="store_true",
        help="also read ~/.config/memoji-sticker-pack/.env",
    )
    parser.add_argument("--base-url", default=BASE_URL)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    candidates = resolve_api_key_candidates(os.environ, os.getcwd(), args.use_local_key, CONFIG_DIR)
    if not candidates:
        print(
            "未找到 %s（检查进程 env / $PWD/.env.local / $PWD/.env / --use-local-key）" % KEY_NAME,
            file=sys.stderr,
        )
        return 2
    notice = legacy_key_notice(candidates)
    if notice:
        print(notice, file=sys.stderr)
    keys = [c.value for c in candidates]

    auto_cleanup = not args.no_auto_cleanup
    if args.file is not None:
        try:
            with open(args.file, "rb") as file_handle:
                file_bytes = file_handle.read()
        except OSError as error:
            print(
                "无法读取文件 %s: %s"
                % (args.file, error.strerror or error),
                file=sys.stderr,
            )
            return 1
        url, headers, body = build_request(
            "stream",
            base_url=args.base_url,
            file_bytes=file_bytes,
            filename=args.file_name or os.path.basename(args.file),
            file_name=args.file_name,
            auto_cleanup=auto_cleanup,
        )
    elif args.base64_data is not None:
        url, headers, body = build_request(
            "base64",
            base_url=args.base_url,
            file_data=args.base64_data,
            file_name=args.file_name,
            auto_cleanup=auto_cleanup,
        )
    else:
        url, headers, body = build_request(
            "url",
            base_url=args.base_url,
            url=args.url,
            file_name=args.file_name,
            auto_cleanup=auto_cleanup,
        )

    try:
        response, used_key = run_upload(url, headers, body, keys)
        result = interpret_upload(response)
    except UploadError as error:
        print(error.message, file=sys.stderr)
        return 1
    except AmbiguousRequest as error:
        # 写操作的结果不明态（ADR 0006 规则 4）：请求已发出，文件可能已存入服务端
        # 也可能没有。不自动重传（避免产生重复对象），把不确定性如实交给调用方。
        print(
            "上传结果不明：请求已发出但未收到响应（%s）；未自动重试。请稍后重跑本次上传。"
            % error.cause,
            file=sys.stderr,
        )
        return 1
    except OSError as error:  # URLError / socket.timeout 均为 OSError 子类
        print("网络错误: %s（请求未发出，已重试 3 次仍失败）" % error, file=sys.stderr)
        return 1

    print(result["url"])
    print(
        "上传成功 id=%s size=%s bytes | key=%s | 该 URL 72 小时后过期"
        % (result.get("id"), result.get("size"), mask_key(used_key)),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
