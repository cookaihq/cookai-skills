from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

from client import call_with_key_fallback, encode_multipart, http_request
from config import KEY_NAME, legacy_key_notice, mask_key, resolve_api_key_candidates


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
    429: "请求频率超限，请稍后再试（不自动重试）",
    500: "服务器内部错误",
}


class UploadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def run_upload(full_url, headers, body, keys, transport=None) -> tuple:
    if transport is None:
        transport = http_request

    def attempt(key):
        request_headers = dict(headers)
        request_headers["Authorization"] = "Bearer " + key
        return transport("POST", full_url, request_headers, body)

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
    except urllib.error.URLError as error:
        print("网络错误: %s" % error, file=sys.stderr)
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
