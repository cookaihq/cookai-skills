"""Query native Agent model directories, as used by Orca model discovery."""
from concurrent.futures import ThreadPoolExecutor
from skill_ui.contracts import UIError
from .process import JsonLines, executable


def codex_models(cwd):
    connection = JsonLines(executable("codex") + ["app-server"], cwd)
    try:
        connection.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "as-model-discovery", "version": "1"}}})
        initialized = connection.receive(lambda item: item.get("id") == 1)
        if "error" in initialized:
            raise UIError("catalog_protocol", "Codex 不支持模型目录初始化。")
        connection.send({"method": "initialized"})
        models, cursor = [], None
        for number in range(2, 12):
            connection.send({"id": number, "method": "model/list", "params": {
                "limit": 100, "includeHidden": False, **({"cursor": cursor} if cursor else {})}})
            response = connection.receive(lambda item: item.get("id") == number)
            if "error" in response:
                raise UIError("catalog_protocol", "Codex 未提供模型目录。")
            payload = response.get("result", {})
            for row in payload.get("data", []):
                identifier = row.get("model") or row.get("id")
                levels = row.get("supportedReasoningEfforts")
                if row.get("hidden") or not identifier or not isinstance(levels, list):
                    continue
                models.append({"id": identifier, "label": row.get("displayName") or identifier,
                               "efforts": [level["reasoningEffort"] for level in levels],
                               "defaultEffort": row.get("defaultReasoningEffort")})
            cursor = payload.get("nextCursor")
            if not cursor:
                return models
        raise UIError("catalog_incomplete", "模型目录未完整返回，请更新 Codex 后重试。")
    finally:
        connection.close()


def claude_models(cwd):
    connection = JsonLines(executable("claude") + ["-p", "--input-format", "stream-json",
                           "--output-format", "stream-json", "--verbose", "--strict-mcp-config"], cwd)
    try:
        connection.send({"type": "control_request", "request_id": "as-model-discovery",
                         "request": {"subtype": "list_models"}})
        data = connection.receive(lambda item: item.get("type") == "control_response" and
                                  item.get("response", {}).get("request_id") == "as-model-discovery")
        response = data.get("response", {})
        if response.get("subtype") != "success":
            raise UIError("catalog_protocol", "Claude Code 不支持 list_models；请更新后重试。")
        models, seen = [], set()
        for row in response.get("response", {}).get("models", []):
            identifier = row.get("resolvedModel") or row.get("value")
            if row.get("disabled") or identifier in (None, "default") or identifier in seen:
                continue
            seen.add(identifier)
            models.append({"id": identifier, "label": identifier,
                           "efforts": row.get("supportedEffortLevels", []) if row.get("supportsEffort") else [],
                           "alias": row.get("value")})
        return models
    finally:
        connection.close()


def discover(cwd):
    def get(agent, label, loader):
        try:
            models = loader(cwd)
            if not models:
                raise UIError("catalog_empty", "Agent 未返回可选模型。")
            return agent, {"label": label, "models": models, "available": True,
                           "source": "本机 Agent 原生模型目录；实际启动仍由 Orca 校验"}
        except (UIError, OSError, ValueError, KeyError) as exc:
            message = str(exc) if isinstance(exc, UIError) else "模型目录读取失败，请检查 Agent 安装。"
            return agent, {"label": label, "models": [], "available": False, "error": message}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(get, "codex", "Codex", codex_models),
                pool.submit(get, "claude", "Claude Code", claude_models)]
        return dict(job.result() for job in jobs)
