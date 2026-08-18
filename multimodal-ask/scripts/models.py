from __future__ import annotations

from client import (METADATA_TIMEOUT, call_with_key_fallback, http_request,
                    request_with_retry)


class ModelsQueryError(Exception):
    """模型清单查询没能拿到 200——**不等于「无可用模型」**。

    历史缺陷：旧实现不看 resp.status，5xx 的错误响应里没有 data 字段，于是被当成
    「该 token 一个模型都没有」，把服务端故障误报成用户配置问题（ADR 0006 规则 2）。
    """

    def __init__(self, status, detail=""):
        super().__init__("模型清单查询失败 [HTTP %s]%s" % (status, (" " + detail) if detail else ""))
        self.status = status


def fetch_models(keys: list, *, base_url: str, transport=None, sleep=None, log=None) -> tuple:
    """GET the token's available llm-custom models. Returns (data_list, used_key).
    This config query does not consume credits.

    幂等 GET：429/5xx 与网络异常按 ADR 0006 重试 3 次 + 指数退避；重试耗尽后
    非 200 抛 ModelsQueryError（而不是伪装成空清单）。"""
    if transport is None:
        transport = http_request
    url = base_url + "/v1/configs/llm_generations_models"

    def attempt(key):
        return request_with_retry(
            lambda: transport("GET", url, {"Authorization": "Bearer " + key}, None,
                              timeout=METADATA_TIMEOUT),
            op="fetch_models", sleep=sleep, log=log)

    resp, used = call_with_key_fallback(keys, attempt)
    if resp.status != 200:
        raise ModelsQueryError(resp.status, (resp.text or "")[:200])
    data = resp.json.get("data", []) if isinstance(resp.json, dict) else []
    return data, used


def check_capabilities(models: list, model_id: str, needed_caps: list) -> tuple:
    """Return (ok, reason, suggestions). Advisory only — the API is the final authority.
    capabilities is the union across visible channels."""
    found = next((m for m in models if m.get("id") == model_id), None)
    if found is None:
        return False, "模型 '%s' 不在当前 token 可用清单" % model_id, [m.get("id") for m in models]
    caps = set(found.get("capabilities") or [])
    missing = [c for c in needed_caps if c not in caps]
    if missing:
        suggestions = [m.get("id") for m in models if set(needed_caps) <= set(m.get("capabilities") or [])]
        return False, "模型 '%s' 不支持所需能力 %s" % (model_id, missing), suggestions
    return True, "", []
