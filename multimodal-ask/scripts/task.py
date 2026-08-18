from __future__ import annotations

import json
import time

from client import (BILLING_WRITE, METADATA_TIMEOUT, NETWORK_ERRORS,
                    call_with_key_fallback, http_request, request_with_retry)
from client import _default_log as _log_stderr


def family_of(model: str) -> "str | None":
    """Prefix-based heuristic; only affects the max_tokens default — the user can always override."""
    m = (model or "").lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gpt"):
        return "gpt"
    if m.startswith("gemini"):
        return "gemini"
    return None


def apply_max_tokens(model: str, max_tokens) -> "int | None":
    """Family rule: claude-* requires max_tokens (default 1024 when missing);
    gpt-*/gemini-*/others optional (omit when missing). User-given values pass through."""
    if max_tokens is not None:
        return max_tokens
    if family_of(model) == "claude":
        return 1024
    return None


def build_submit_body(model, messages, *, max_tokens=None, temperature=None,
                      top_p=None, stop=None, reasoning=False) -> dict:
    body = {"model": model, "messages": messages, "stream": False}
    mt = apply_max_tokens(model, max_tokens)
    if mt is not None:
        body["max_tokens"] = mt
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if stop is not None:
        body["stop"] = stop
    if reasoning:
        # opt-in passthrough; llm-custom schema doesn't list it but additionalProperties:true.
        body["reasoning"] = True
    return body


_LLM_HINTS = {
    "no_available_model": "模型未配置或不可用",
    "model_not_support_capability": "该模型不支持本次内容类型组合",
    "model_rule_violation": "违反模型规则（如视频过大 / 多视频等）",
    "invalid_param": "参数非法（含 max_tokens 家族约束）",
}


class LLMError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class PollTimeout(Exception):
    pass


class TaskFailed(Exception):
    pass


def _llm_error_message(resp) -> str:
    code = ""
    server = ""
    if isinstance(resp.json, dict) and isinstance(resp.json.get("error"), dict):
        err = resp.json["error"]
        code = err.get("code") or err.get("type") or ""
        server = err.get("message") or ""
    hint = _LLM_HINTS.get(code, "")
    parts = ["[HTTP %s]" % resp.status]
    if code:
        parts.append(code)
    if hint:
        parts.append(hint)
    if server:
        parts.append("| 上游: " + server)
    return " ".join(parts)


# 提交是**计费写操作**，请求本身没有幂等键，因此重试面必须收窄（ADR 0006 规则 4）：
#   - 429：服务端明确表示被限流、本次未受理，重试不会重复扣费 → 可安全重试，
#          带 Retry-After 时按该值等待。
#   - 连接阶段失败（DNS 解析失败 / 连接被拒）：请求确定没发出去 → 可安全重试。
#   - 其余网络失败（含全部超时、发送 body 途中断开）：任务可能已创建并已计费 →
#          抛 client.AmbiguousRequest，由 ask.py 如实告知用户，绝不盲重试。
#   - 5xx：无法排除「任务已创建、只是响应失败」，同样不重试，直接按 LLMError 报错。
SUBMIT_RETRYABLE_STATUSES = frozenset([429])


def submit_llm(body: dict, keys: list, *, base_url: str, transport=None,
               sleep=None, log=None) -> tuple:
    """POST the llm-custom task. Returns (submit_json, used_key). Raises LLMError on non-200,
    or client.AmbiguousRequest when the request went out but no response came back."""
    if transport is None:
        transport = http_request
    url = base_url + "/v1/llm/generations"
    payload = json.dumps(body).encode()

    def attempt(key):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        return request_with_retry(
            lambda: transport("POST", url, headers, payload, timeout=METADATA_TIMEOUT),
            op="submit_llm", write_safety=BILLING_WRITE,
            retryable_statuses=SUBMIT_RETRYABLE_STATUSES, sleep=sleep, log=log)

    resp, used = call_with_key_fallback(keys, attempt)
    if resp.status == 200 and isinstance(resp.json, dict) and resp.json.get("id"):
        return resp.json, used
    raise LLMError(resp.status, _llm_error_message(resp))


def poll_task(task_id: str, key: str, *, base_url: str, transport=None,
              interval: int = 5, timeout: int = 300, sleep=None, log=None,
              monotonic=None) -> dict:
    """Poll GET /v1/tasks/{id}?sync_upstream=true until status is completed/failed.
    Returns the terminal task json. Raises PollTimeout if it never reaches terminal.
    On a `failed` terminal this still returns the task dict; callers pass it to
    `extract_text`, which raises `TaskFailed`.

    轮询是幂等 GET：单次瞬时失败（429/5xx/网络异常）先消耗内层重试（3 次 + 指数
    退避 + 日志）。**内层重试穷尽也不终止整个轮询**：本轮作废、消耗一格预算、
    继续下一轮（ADR 0006 规则 5，与 pdf2md_docx 同口径）。

    `timeout` 是**真墙钟预算**，不是「次数 × 间隔」的估算值：进入每一轮前先查
    deadline，超了立即抛 PollTimeout。内层重试的退避、上游变慢的响应时间都算在
    这份预算里，所以实际轮询轮数可能少于 timeout//interval。计数预算同时保留，
    两者任一耗尽即终态。`monotonic` 仅供测试注入。"""
    if transport is None:
        transport = http_request
    if sleep is None:
        sleep = time.sleep
    if monotonic is None:
        monotonic = time.monotonic
    if log is None:
        log = _log_stderr
    url = base_url + "/v1/tasks/" + task_id + "?sync_upstream=true"
    headers = {"Authorization": "Bearer " + key}
    max_polls = max(1, timeout // interval)
    deadline = monotonic() + timeout
    for i in range(1, max_polls + 1):
        if monotonic() >= deadline:
            break
        try:
            resp = request_with_retry(
                lambda: transport("GET", url, headers, None, timeout=METADATA_TIMEOUT),
                op="poll_task(%s)" % task_id, sleep=sleep, log=log)
        except NETWORK_ERRORS as exc:
            # 内层 3 次尝试全失败：作废本轮（等同「本轮没拿到状态」），继续下一轮，
            # 不让一次抖动击穿整个轮询。
            log("[poll] 第 %d/%d 轮查询重试耗尽（%s），本轮按未拿到状态处理，继续轮询"
                % (i, max_polls, exc))
            resp = None
        if resp is not None and isinstance(resp.json, dict) \
                and resp.json.get("status") in ("completed", "failed"):
            return resp.json
        if i < max_polls:
            sleep(interval)
    raise PollTimeout("任务 %s 轮询超时（可能仍在运行），可凭 task_id 稍后手动查询" % task_id)


def extract_text(task_json: dict) -> str:
    """Return the assistant text from a terminal task. Raises TaskFailed on failure or
    malformed result. Note: thinking models may legitimately return empty content."""
    if task_json.get("status") == "failed":
        err = task_json.get("error") or {}
        raise TaskFailed("任务失败: [%s] %s"
                         % (err.get("code") or err.get("type") or "?", err.get("message") or ""))
    results = task_json.get("results") or []
    if not results or not isinstance(results[0], dict):
        raise TaskFailed("任务已终态但无 results")
    choices = results[0].get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise TaskFailed("结果无 choices")
    message = choices[0].get("message") or {}
    return message.get("content") or ""
