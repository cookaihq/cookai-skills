"""AS owns one global, atomic configuration; other Skills' settings are not read."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from skill_ui.contracts import UIError
from skill_ui.storage import atomic_json, file_lock

EMPTY = {"defaults": {}, "presets": {}}
CODE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}\Z")


def default_path():
    return Path.home() / ".config/as/config.json"


def check_choice(agent, value, catalog):
    entry = catalog.get(agent)
    if not entry or not entry.get("available"):
        raise UIError("agent_unavailable", f"{agent} 当前没有可用模型目录，请修复安装或选择可用预设。")
    model = next((row for row in entry["models"] if row["id"] == value.get("model")), None)
    if not model:
        raise UIError("model_unavailable", f"{agent} 的模型不在当前目录中，请重新选择。")
    if value.get("effort") not in (model["efforts"] or [None]):
        raise UIError("invalid_effort", "思考强度与所选模型不匹配。")


def validate(value):
    if not isinstance(value, dict) or set(value) != {"defaults", "presets"}:
        raise UIError("invalid_config", "AS 配置结构无效。")
    if not isinstance(value["defaults"], dict) or not isinstance(value["presets"], dict):
        raise UIError("invalid_config", "默认配置和预设须为记录。")
    if len(value["presets"]) > 50:
        raise UIError("invalid_config", "最多保存 50 个预设。")
    for agent, choice in value["defaults"].items():
        if agent not in ("codex", "claude") or not isinstance(choice, dict) or set(choice) != {"model", "effort"}:
            raise UIError("invalid_config", "默认配置字段无效。")
        validate_fields(choice)
    for code, preset in value["presets"].items():
        if not CODE.fullmatch(code) or code in ("__proto__", "constructor", "prototype"):
            raise UIError("invalid_code", "预设代码须为 1–16 位字母、数字、下划线或连字符。")
        if not isinstance(preset, dict) or set(preset) != {"name", "agent", "model", "effort"}:
            raise UIError("invalid_config", "预设须指定名称、Agent、模型和思考强度。")
        if not isinstance(preset["name"], str) or not preset["name"].strip() or len(preset["name"]) > 40:
            raise UIError("invalid_config", "预设名称须为 1–40 字。")
        if preset["agent"] not in ("codex", "claude"):
            raise UIError("invalid_config", "预设的目标 Agent 无效。")
        validate_fields(preset)
    return deepcopy(value)


def validate_fields(value):
    if not isinstance(value.get("model"), str) or not value["model"].strip() or len(value["model"]) > 200:
        raise UIError("invalid_model", "请明确选择模型。")
    if value.get("effort") is not None and (not isinstance(value["effort"], str) or len(value["effort"]) > 30):
        raise UIError("invalid_effort", "思考强度无效。")


class Store:
    def __init__(self, path=None):
        self.path = Path(path or default_path()).resolve()

    def initialization(self, action, revision=None):
        """Page-owned prompt state; reading business configuration stays read-only."""
        if action not in ("inspect", "add", "dismiss"):
            raise UIError("invalid_action", "未知初始化操作。")
        state_path = self.path.with_name(self.path.name + ".initialization.json")
        with file_lock(self.path.with_suffix(".lock"), protect_git=True, owner_path=self.path.parent):
            value, current = self.read()
            try:
                state = json.loads(state_path.read_bytes())
            except FileNotFoundError:
                state = {"handled": False}
            except (ValueError, UnicodeError, OSError):
                raise UIError("initialization_unreadable", f"无法读取初始化提示状态：{state_path}") from None
            if not isinstance(state, dict) or set(state) != {"handled"} or type(state["handled"]) is not bool:
                raise UIError("initialization_invalid", f"初始化提示状态无效：{state_path}")
            populated = bool(value["defaults"] or value["presets"])
            if action == "add" and (current != revision or populated or state["handled"]):
                raise UIError("conflict", "初始化条件已变化。草稿已保留，请重新载入核对。", 409)
            if (populated or action in ("add", "dismiss")) and not state["handled"]:
                atomic_json(state_path, {"handled": True}, protect_git=True, owner_path=self.path.parent)
                state["handled"] = True
            return {"show": not state["handled"], "revision": current, "settings": value}

    def read(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return deepcopy(EMPTY), "missing"
        except OSError:
            raise UIError("config_unreadable", f"无法读取配置：{self.path}") from None
        try:
            value = validate(json.loads(raw))
        except (ValueError, UnicodeError):
            raise UIError("invalid_config", f"配置不是有效 JSON：{self.path}") from None
        return value, hashlib.sha256(raw).hexdigest()

    def save(self, value, revision, catalog, *, initializing=False):
        value = validate(value)
        if type(initializing) is not bool:
            raise UIError("invalid_config", "初始化标记须为布尔值。")
        with file_lock(self.path.with_suffix(".lock"), protect_git=True, owner_path=self.path.parent):
            old, current = self.read()
            if current != revision or (initializing and (old["defaults"] or old["presets"])):
                raise UIError("conflict", "配置已被其他页面或程序修改。草稿已保留，请重新载入核对。", 409)
            # Preserve unchanged choices whose Agent is temporarily offline; check every changed choice.
            for agent, choice in value["defaults"].items():
                if old["defaults"].get(agent) != choice:
                    check_choice(agent, choice, catalog)
            for code, preset in value["presets"].items():
                if old["presets"].get(code) != preset:
                    check_choice(preset["agent"], preset, catalog)
            atomic_json(self.path, value, protect_git=True, owner_path=self.path.parent)
            result = self.read()
            if result[0] != value:
                raise UIError("readback_conflict", "保存后的配置与本次提交不一致，请核对文件。", 409)
            return result

    def adopt_default(self, agent, choice, revision, catalog):
        if agent not in ("codex", "claude") or choice is None:
            raise UIError("unsupported_default", "此调用宿主没有可启动的默认 Agent，请选择明确目标的预设。")
        choice = validate({"defaults": {agent: choice}, "presets": {}})["defaults"][agent]
        with file_lock(self.path.with_suffix(".lock"), protect_git=True, owner_path=self.path.parent):
            value, current = self.read()
            if current != revision or agent in value["defaults"]:
                raise UIError("conflict", "配置已变化，未覆盖默认值。请回读配置后继续原调用。", 409)
            check_choice(agent, choice, catalog)
            value["defaults"][agent] = choice
            atomic_json(self.path, value, protect_git=True, owner_path=self.path.parent)
            result = self.read()
            if result[0] != value:
                raise UIError("readback_conflict", "推荐配置保存后回读不一致，已停止启动。", 409)
            return result


def strip_separator(value):
    """Remove the command separator, retaining the body's indentation and blank lines."""
    return re.sub(r"^(?:[^\S\r\n]*\r?\n|[^\S\r\n]+)", "", value, count=1)


def parse_invocation(invocation, settings):
    match = re.match(r"^\s*(?:\$as|/as)(?=\s|$)", invocation)
    if not match:
        raise UIError("invalid_invocation", "请提供原始 $as 或 /as 调用内容。")
    remainder = strip_separator(invocation[match.end():])
    code = None
    carry_context = False
    while True:
        tokens = remainder.lstrip()
        first = re.match(r"\S+", tokens)
        first = first[0] if first else None
        if first == "--":
            message = strip_separator(tokens[2:])
            break
        if first == "-r":
            if carry_context:
                raise UIError("duplicate_context_flag", "一次调用只需指定一个 -r。")
            carry_context = True
            remainder = strip_separator(tokens[2:])
        elif first == "--preset":
            if code is not None:
                raise UIError("duplicate_preset", "一次调用只能选择一个预设。")
            tokens = tokens[len(first):].lstrip()
            selected = re.match(r"\S+", tokens)
            if not selected:
                raise UIError("missing_preset", "--preset 后须提供已保存的预设代码。")
            code = selected[0]
            if code not in settings["presets"]:
                raise UIError("unknown_preset", f"未找到预设「{code}」。请打开配置页检查代码。")
            remainder = strip_separator(tokens[len(code):])
        elif code is None and first in settings["presets"]:
            code = first
            remainder = strip_separator(tokens[len(code):])
        else:
            following = re.match(r"\S+\s+(\S+)", tokens)
            if code is None and following and following[1] == "-r":
                raise UIError("unknown_preset", f"未找到预设「{first}」。请打开配置页检查代码；字面正文请放在 -- 后。")
            message = remainder
            break
    return {"code": code, "message": message if message.strip() else "", "carry_context": carry_context}


def resolve_choice(parsed, current_agent, settings):
    code = parsed["code"]
    if code is not None:
        choice = deepcopy(settings["presets"][code])
    else:
        if current_agent not in settings["defaults"]:
            raise UIError("configuration_required", f"尚未保存 {current_agent} 默认配置，请打开 AS 配置页。")
        choice = {"agent": current_agent, **settings["defaults"][current_agent]}
    return {"agent": choice["agent"], "model": choice["model"], "effort": choice["effort"],
            "code": code, "source": f"presets.{code}" if code else f"defaults.{current_agent}",
            "message": parsed["message"], "carry_context": parsed["carry_context"]}


def resolve(invocation, current_agent, settings):
    return resolve_choice(parse_invocation(invocation, settings), current_agent, settings)
