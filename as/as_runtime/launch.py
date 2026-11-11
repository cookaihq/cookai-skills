"""Launch once, persist receipts, replay only Orca's original operation."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from copy import deepcopy

from skill_ui.contracts import UIError
from skill_ui.storage import atomic_json, file_lock, private_directory
from .config import check_choice, parse_invocation, resolve_choice
from .recommendations import DEFAULTS
from .context import capture, prompt as context_prompt
from .process import json_command, orca_executable, run


def workspace(cwd, orca):
    result = json_command(orca + ["worktree", "current", "--environment", "local", "--json"], cwd)
    row = result.get("worktree", result.get("workspace", {}))
    address = row.get("address") if isinstance(row, dict) else None
    if not isinstance(address, dict) or not address.get("environment") or not address.get("worktree"):
        raise UIError("workspace_unavailable", "无法取得当前 Orca 工作区的精确地址；未创建会话。")
    if Path(row.get("path", "")).resolve() != cwd:
        raise UIError("cwd_mismatch", "当前目录与 Orca 工作区根目录不同。请在 Orca 中打开该目录，再调用 AS。")
    branch_result = run(["git", "branch", "--show-current"], cwd)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    return {"path": str(cwd), "branch": branch, "address": address}


def operation_id(value):
    if isinstance(value, dict):
        if isinstance(value.get("operationId"), str):
            return value["operationId"]
        for child in value.values():
            found = operation_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = operation_id(child)
            if found:
                return found
    return None


def receipt_summary(record):
    return {key: record.get(key) for key in ("request_id", "status", "operation_id", "target", "choice", "context", "receipt", "error", "configuration")}


def configuration_offer(agent, store, catalog):
    recommendation = deepcopy(DEFAULTS.get(agent))
    problem = None
    if recommendation is not None:
        try:
            check_choice(agent, recommendation, catalog)
        except UIError as exc:
            problem = str(exc)
    return {"agent": agent, "source": str(store.path), "recommendation": recommendation,
            "available": recommendation is not None and problem is None, "problem": problem,
            "actions": (["adopt_recommended", "open_configuration"] if recommendation else ["choose_explicit_preset"])}


class Launcher:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def path(self, request_id):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{7,79}", request_id):
            raise UIError("request_id", "request-id 必须是本次调用固定的 8–80 位唯一标识。")
        return self.directory / (request_id + ".json")

    def inspect(self, request_id):
        path = self.path(request_id)
        if not path.is_file():
            raise UIError("request_missing", "未找到此 AS 调用记录。")
        return receipt_summary(json.loads(path.read_text(encoding="utf-8")))

    def launch(self, request_id, invocation, current_agent, cwd, store, catalog, *, source_session_id=None, source_file=None, adopt_recommended=False):
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise UIError("cwd_missing", "调用目录不存在。")
        path = self.path(request_id)
        private_directory(self.directory, owner_path=cwd)
        inputs = [invocation, current_agent, str(cwd), str(store.path)]
        if source_session_id is not None or source_file is not None:
            inputs.append([source_session_id, str(source_file) if source_file is not None else None])
        fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()
        with file_lock(path.with_suffix(".lock")):
            previous = None
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("fingerprint") != fingerprint:
                    raise UIError("request_conflict", "同一调用标识已用于不同输入；未创建新会话。")
                if previous.get("status") != "configuration_required":
                    return receipt_summary(previous)
            if adopt_recommended:
                if previous is None:
                    raise UIError("confirmation_required", "请先执行原调用并展示推荐组合与保存范围，用户明确选择后才能快捷采用。")
                settings, revision = store.adopt_default(current_agent, previous["configuration"]["recommendation"], previous["config_revision"], catalog)
                logging.info("recommended_default_saved request=%s agent=%s", request_id, current_agent)
            else:
                settings, revision = store.read()
            parsed = previous["parsed"] if previous else parse_invocation(invocation, settings)
            if previous:
                source_session_id = previous.get("source_session_id")
            elif parsed["carry_context"] and current_agent == "codex":
                source_session_id = source_session_id or os.environ.get("CODEX_THREAD_ID")
            try:
                choice = resolve_choice(parsed, current_agent, settings)
            except UIError as exc:
                if exc.code != "configuration_required":
                    raise
                pending = {"request_id": request_id, "fingerprint": fingerprint, "created": time.time(),
                           "status": "configuration_required", "config_revision": revision,
                           "parsed": parsed, "source_session_id": source_session_id,
                           "configuration": configuration_offer(current_agent, store, catalog), "error": str(exc)}
                atomic_json(path, pending, owner_path=cwd)
                logging.info("configuration_required request=%s agent=%s", request_id, current_agent)
                return receipt_summary(pending)
            check_choice(choice["agent"], choice, catalog)
            context = None
            message = choice["message"]
            if choice["carry_context"]:
                context = capture(current_agent, cwd, source_session_id, source_file)
                message = context_prompt(context, message, path)
            elif source_session_id is not None or source_file is not None:
                raise UIError("context_not_requested", "只有原始调用带 -r 时才接受来源会话参数。")
            orca = orca_executable()
            target = workspace(cwd, orca)
            argv = orca + ["agent", "launch", "--agent", choice["agent"],
                           "--environment", target["address"]["environment"],
                           "--worktree", target["address"]["worktree"], "--model", choice["model"], "--json"]
            if choice["effort"] is not None:
                argv += ["--effort", choice["effort"]]
            if message:
                argv += ["--prompt", message]
            record = {"request_id": request_id, "fingerprint": fingerprint, "created": time.time(),
                      "status": "prepared", "target": target, "config_revision": revision,
                      "choice": {key: value for key, value in choice.items() if key != "message"},
                      "context": context, "argv": argv}
            atomic_json(path, record, owner_path=cwd)
            return self._execute(path, record)

    def resume(self, request_id):
        path = self.path(request_id)
        with file_lock(path.with_suffix(".lock")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] == "complete":
                return receipt_summary(record)
            if record["status"] == "configuration_required":
                raise UIError("configuration_required", "原调用尚未提交。配置完成后请使用原输入、宿主、目录和 request-id 接续 launch。")
            if record["status"] != "prepared" and not record.get("operation_id"):
                raise UIError("outcome_unknown", "启动结果尚不明确且没有 Orca operationId。请只读核对工作区；禁止重新启动或补发正文。")
            cwd = Path(record["target"]["path"])
            current = workspace(cwd, [record["argv"][0]])
            if current != record["target"]:
                raise UIError("target_changed", "原工作区地址或分支已变化；停止恢复，先核对原会话。")
            return self._execute(path, record, retry=record.get("operation_id"))

    def _execute(self, path, record, retry=None):
        argv = record["argv"] + (["--retry-launch", retry] if retry else [])
        record["status"] = "inflight"
        atomic_json(path, record, owner_path=Path(record["target"]["path"]))
        logging.info("launch_started request=%s retry=%s", record["request_id"], bool(retry))
        try:
            result = run(argv, record["target"]["path"], timeout=90)
            raw = result.stdout
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            record["operation_id"] = operation_id(data)
            if not isinstance(data, dict) or not data:
                record.update(status="unknown", error="Orca 回执不是有效 JSON；请只读检查工作区。")
            elif result.returncode or data.get("ok") is False:
                # Native error may include an accepted launch. Never infer that retrying fresh is safe.
                record.update(status="unknown", receipt=data, error="Orca 未确认启动完成；请核对回执并恢复同一操作。")
            else:
                payload = data.get("result", data)
                payload = payload if isinstance(payload, dict) else {}
                launch = payload.get("launch", payload)
                launch = launch if isinstance(launch, dict) else {}
                outcome = launch.get("outcome") or {}
                prompt = launch.get("prompt") or {}
                outcome = outcome if isinstance(outcome, dict) else {}
                prompt = prompt if isinstance(prompt, dict) else {}
                delivery_ok = "--prompt" not in record["argv"] or prompt.get("outcome") in ("journaled", "handed-to-terminal")
                complete = outcome.get("kind") in ("structured", "terminal") and bool(outcome.get("handle")) and delivery_ok
                record.update(status="complete" if complete else "unconfirmed", receipt=data)
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or b""
            try:
                record["operation_id"] = operation_id(json.loads(raw))
            except (ValueError, TypeError):
                pass
            record.update(status="unknown", error="Orca 启动超时，可能已创建会话。请查询该调用，不能重新执行新启动。")
        except (OSError, UIError):
            record.update(status="unknown", error="启动程序调用失败，请核对进程及原操作状态。")
        atomic_json(path, record, owner_path=Path(record["target"]["path"]))
        logging.info("launch_finished request=%s status=%s", record["request_id"], record["status"])
        return receipt_summary(record)
