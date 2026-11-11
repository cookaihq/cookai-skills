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

    def save(self, value, revision, catalog):
        value = validate(value)
        with file_lock(self.path.with_suffix(".lock"), protect_git=True, owner_path=self.path.parent):
            old, current = self.read()
            if current != revision:
                raise UIError("conflict", "配置已被其他页面或程序修改。草稿已保留，请重新载入核对。", 409)
            # Preserve unchanged choices whose Agent is temporarily offline; check every changed choice.
            for agent, choice in value["defaults"].items():
                if old["defaults"].get(agent) != choice:
                    check_choice(agent, choice, catalog)
            for code, preset in value["presets"].items():
                if old["presets"].get(code) != preset:
                    check_choice(preset["agent"], preset, catalog)
            atomic_json(self.path, value, protect_git=True, owner_path=self.path.parent)
        return self.read()


def resolve(invocation, current_agent, settings):
    match = re.match(r"^\s*(?:\$as|/as)(?=\s|$)", invocation)
    if not match:
        raise UIError("invalid_invocation", "请提供原始 $as 或 /as 调用内容。")
    remainder = invocation[match.end():].lstrip()
    message = ""
    code = None
    if remainder:
        code = re.match(r"\S+", remainder)[0]
        if code not in settings["presets"]:
            raise UIError("unknown_preset", f"未找到预设「{code}」。请打开配置页检查代码。")
        choice = deepcopy(settings["presets"][code])
        message = re.sub(r"^(?:[ \t]*\r?\n|[ \t]+)", "", remainder[len(code):], count=1)
        if not message.strip():
            message = ""
    else:
        if current_agent not in settings["defaults"]:
            raise UIError("configuration_required", f"尚未保存 {current_agent} 默认配置，请打开 AS 配置页。")
        choice = {"agent": current_agent, **settings["defaults"][current_agent]}
    return {"agent": choice["agent"], "model": choice["model"], "effort": choice["effort"],
            "code": code, "source": f"presets.{code}" if code else f"defaults.{current_agent}",
            "message": message}
