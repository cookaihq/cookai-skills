from __future__ import annotations

import argparse
import os
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。放在
# `__main__` 守卫里，好让 tests 把本模块 import 进来时不触发 execv。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

from client import AmbiguousRequest  # noqa: E402  (must come after the bootstrap)
from config import KEY_NAME, legacy_key_notice, mask_key, resolve_api_key_candidates
from dedup import dedup_key  # noqa: F401  (exposed for callers/tests; same-round guard is Agent-side)
from media import (CAPABILITY_BY_KIND, classify_source, normalize_youtube, size_warning)
from messages import build_messages
from models import ModelsQueryError, check_capabilities, fetch_models
from task import (LLMError, PollTimeout, TaskFailed, build_submit_body, extract_text,
                  poll_task, submit_llm)
from upload_helper import UploadHelperError, upload_local_file

BASE_URL = "https://api.aihubmax.com"
CONFIG_DIR = os.path.expanduser("~/.config/multimodal-ask")
def _parse_warn_bytes() -> int:
    raw = os.environ.get("MULTIMODAL_ASK_WARN_BYTES") or ""
    if raw:
        try:
            return int(raw)
        except ValueError:
            print("⚠ MULTIMODAL_ASK_WARN_BYTES=%r 非整数，已忽略，使用默认值 20 MB" % raw, file=sys.stderr)
    return 20 * 1024 * 1024


WARN_BYTES = _parse_warn_bytes()


def parse_args(argv):
    p = argparse.ArgumentParser(description="Ask a aihubmax llm-custom model over text/media")
    p.add_argument("--model", required=True, help="model id (must be in the token's available list)")
    p.add_argument("--prompt", help="text prompt")
    p.add_argument("--system", help="system instruction")
    p.add_argument("--image", action="append", default=[], help="image path or URL (repeatable)")
    p.add_argument("--video", action="append", default=[], help="video path/URL/YouTube (repeatable)")
    p.add_argument("--audio", action="append", default=[], help="audio path or URL (repeatable)")
    p.add_argument("--file", action="append", default=[], help="document path or URL (repeatable)")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--temperature", type=float)
    p.add_argument("--top-p", type=float)
    p.add_argument("--stop", action="append", help="stop sequence (repeatable)")
    p.add_argument("--reasoning", action="store_true",
                   help="opt-in: pass reasoning=true (effect on llm-custom unverified)")
    p.add_argument("--poll-interval", type=int, default=5)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--use-local-key", action="store_true")
    return p.parse_args(argv)


def _collect_media(args):
    """Return list of (kind, source) preserving CLI order within each kind."""
    items = []
    for src in args.image:
        items.append(("image", src))
    for src in args.video:
        items.append(("video", src))
    for src in args.audio:
        items.append(("audio", src))
    for src in args.file:
        items.append(("file", src))
    return items


def main(argv=None) -> int:
    args = parse_args(argv)
    raw_media = _collect_media(args)
    if not args.prompt and not raw_media:
        print("需要至少提供 --prompt 或一个媒体（--image/--video/--audio/--file）", file=sys.stderr)
        return 2

    candidates = resolve_api_key_candidates(os.environ, os.getcwd(), args.use_local_key, CONFIG_DIR)
    if not candidates:
        print("未找到 %s（检查进程 env / $PWD/.env.local / $PWD/.env / --use-local-key）" % KEY_NAME,
              file=sys.stderr)
        return 2
    notice = legacy_key_notice(candidates)
    if notice:
        print(notice, file=sys.stderr)
    keys = [c.value for c in candidates]

    needed_caps = sorted({CAPABILITY_BY_KIND[kind] for kind, _ in raw_media})

    # Capability pre-check (advisory; the config query does not consume credits).
    try:
        models, _ = fetch_models(keys, base_url=args.base_url)
        ok, reason, suggestions = check_capabilities(models, args.model, needed_caps)
        if not ok:
            print(reason, file=sys.stderr)
            if suggestions:
                print("可用/支持该能力的模型: %s" % ", ".join(suggestions), file=sys.stderr)
            return 3
    except ModelsQueryError as e:
        # 服务端故障 ≠ 「该 token 无可用模型」：预校验只是建议性的，跳过即可。
        print("⚠ 能力预校验跳过（%s，已重试仍失败）；将直接提交，由 API 裁决" % e,
              file=sys.stderr)
    except OSError as e:  # URLError / socket.timeout 均为 OSError 子类
        print("⚠ 能力预校验跳过（网络错误: %s）；将直接提交，由 API 裁决" % e, file=sys.stderr)

    # Resolve each media source to a URL (upload locals; rewrite YouTube; pass through URLs).
    resolved = []
    for kind, src in raw_media:
        cls = classify_source(src)
        if cls == "local":
            warn = size_warning(src, WARN_BYTES)
            if warn:
                print("⚠ " + warn, file=sys.stderr)
            try:
                url = upload_local_file(src, keys, base_url=args.base_url)
            except UploadHelperError as e:
                print("上传失败（%s）: %s" % (src, e.message), file=sys.stderr)
                return 1
            except AmbiguousRequest as e:
                print("上传结果不明（%s）：请求已发出但未收到响应（%s），"
                      "文件可能已存入服务端也可能没有；未自动重试以免产生重复对象。"
                      "请稍后重试本次调用。" % (src, e.cause), file=sys.stderr)
                return 1
            except OSError as e:  # URLError / socket.timeout 均为 OSError 子类
                print("上传网络错误（%s）: %s（已按 ADR 0006 重试 3 次仍失败）"
                      % (src, e), file=sys.stderr)
                return 1
        elif cls == "youtube":
            url = normalize_youtube(src)
        else:
            url = src
        resolved.append((kind, url))

    msgs = build_messages(args.prompt, args.system, resolved)
    body = build_submit_body(args.model, msgs, max_tokens=args.max_tokens,
                             temperature=args.temperature, top_p=args.top_p,
                             stop=args.stop, reasoning=args.reasoning)

    try:
        submit_json, used = submit_llm(body, keys, base_url=args.base_url)
    except LLMError as e:
        print(e.message, file=sys.stderr)
        return 1
    except AmbiguousRequest as e:
        # 计费写操作的结果不明态（ADR 0006 规则 4）：请求已经发出去了，任务可能
        # 已经创建并计费，也可能没有。禁止自动重发，把不确定性如实交给用户。
        print("提交结果不明：请求已发出但未收到响应（%s）。"
              "任务可能已创建并已计费，也可能没有；本次未自动重试以免重复扣费。"
              "请先到 aihubmax 控制台确认任务列表，再决定是否重新提交。" % e.cause,
              file=sys.stderr)
        return 1
    except OSError as e:  # URLError / socket.timeout 均为 OSError 子类
        print("提交网络错误: %s（请求未发出，已重试 3 次仍失败；任务未创建）" % e,
              file=sys.stderr)
        return 1

    task_id = submit_json.get("id", "")
    try:
        final = poll_task(task_id, used, base_url=args.base_url,
                          interval=args.poll_interval, timeout=args.timeout)
    except PollTimeout as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:  # URLError / socket.timeout 均为 OSError 子类
        print("轮询网络错误: %s（单次轮询已重试 3 次仍失败；task_id=%s，"
              "任务可能仍在运行，可凭 task_id 稍后手动查询）" % (e, task_id),
              file=sys.stderr)
        return 1

    try:
        text = extract_text(final)
    except TaskFailed as e:
        print(str(e), file=sys.stderr)
        return 1

    print(text)  # stdout: the model's text answer
    if not text:
        print("（注：内容为空——思考类模型的推理内容不累积进结果，仅在流式可见）", file=sys.stderr)
    print("✓ 完成 model=%s task=%s key=%s" % (args.model, task_id, mask_key(used)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
