"""Synthetic JSON loader with ten layers. Never reads real env files or credentials.

This example owns its resolution protocol; the UI core only displays its reports.
All files, including simulated global files, live under the selected demo project.
"""
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from skill_ui import UIError
from skill_ui.storage import active, apply_changes, atomic_json, file_lock, private_directory, validation_errors
from studio_adapter import Studio


FIELDS = [
    dict(id="model", label="生成模型", type="select", choices=["balanced-v1", "detail-v2", "speed-v1"], default="balanced-v1", group="基本配置"),
    dict(id="endpoint", label="服务地址", type="text", required=True, group="基本配置"),
    dict(id="api_key", label="API Key", type="text", sensitive=True, required=True, group="基本配置", help="仅填写合成凭证。新增项目密钥写入项目本地文件。"),
    dict(id="timeout", label="请求超时", type="number", default=60, group="基本配置", help="单位为秒；空值继续向下读取。"),
    dict(id="retries", label="重试次数", type="number", default=2, group="基本配置", help="0 是有效值。"),
    dict(id="format", label="输出格式", type="select", choices=["png", "webp", "jpeg"], default="png", group="基本配置"),
    dict(id="retain", label="保留任务记录", type="boolean", default=True, advanced=True, group="高级配置"),
    dict(id="days", label="保留天数", type="number", default=30, required=True, depends_on={"retain": True}, advanced=True, group="高级配置"),
    dict(id="priority", label="语言优先级", type="list", default=["中文", "English"], advanced=True, group="高级配置"),
    dict(id="binding", label="发布目标绑定", type="json", advanced=True, group="高级配置", help="identity、workspace、resource 整组设置；不能跨层补齐。"),
]
LAYER_DEFS = [
    ("process", "进程环境变量", "process", "仅本次调用"),
    ("project_skill", "项目专用文件", "project", "仅此项目 · 此 Skill"),
    ("project_local", "项目本地文件", "project", "仅此项目 · 读取同名字段的 Skill"),
    ("project_env", "项目共享文件", "project", "仅此项目 · 读取同名字段的 Skill"),
    ("plugin_skill_local", "全局 Skill 本地文件", "global", "所有项目 · 此 Plugin 的此 Skill"),
    ("plugin_skill_env", "全局 Skill 文件", "global", "所有项目 · 此 Plugin 的此 Skill"),
    ("plugin_skill_root", "全局 Plugin 专用文件", "global", "所有项目 · 此 Plugin 的此 Skill"),
    ("plugin_local", "全局 Plugin 本地文件", "global", "所有项目 · 此 Plugin"),
    ("plugin_env", "全局 Plugin 共享文件", "global", "所有项目 · 此 Plugin"),
    ("skill_home", "全局 Skill 回退文件", "global", "所有项目 · 同名 Skill"),
]


class LayeredDemo:
    def __init__(self, project):
        self.directory = Path(project) / "demo-layers"
        self.project, self.key = Path(project), secrets.token_bytes(32)
        self.environment, self.global_enabled = {}, True
        self.fields = FIELDS
        private_directory(self.directory, owner_path=self.project)
        with file_lock(self.directory / "layers.lock"):
            if not (self.directory / "initialized.json").exists():
                for name, values in {
                    "plugin_env": dict(model="balanced-v1", endpoint="https://api.demo.invalid/v1", api_key="demo-global-key-0001", timeout=90, retries=2, format="png"),
                    "project_local": dict(api_key="demo-project-key-0002"),
                }.items():
                    if not self.path(name).exists():
                        atomic_json(self.path(name), values, owner_path=self.project)
                atomic_json(self.directory / "initialized.json", {"synthetic": True}, owner_path=self.project)

    def path(self, name):
        return self.directory / (name + ".json")

    def layers(self):
        return [dict(id=name, name=label, scope=scope, scope_label=impact,
                     path="演示进程（只读，不读取真实环境）" if name == "process" else str(self.path(name)),
                     readonly=name == "process", enabled=scope != "global" or self.global_enabled)
                for name, label, scope, impact in LAYER_DEFS]

    def _read(self):
        data, errors = {"process": deepcopy(self.environment)}, {}
        fingerprint = [json.dumps([self.environment, self.global_enabled], sort_keys=True).encode()]
        for layer in self.layers()[1:]:
            name, path = layer["id"], self.path(layer["id"])
            try:
                if path.is_symlink():
                    raise ValueError()
                raw = path.read_bytes() if path.exists() else b""
                fingerprint.append(name.encode() + b"\0" + raw)
                data[name] = json.loads(raw) if raw else {}
                if not isinstance(data[name], dict):
                    raise ValueError()
            except (OSError, ValueError, UnicodeError):
                data[name], errors[name] = {}, "文件无法读取或 JSON 格式无效。"
                fingerprint.append(name.encode() + b"\0error")
        revision = hmac.new(self.key, b"\0".join(fingerprint), hashlib.sha256).hexdigest()
        return data, revision, errors

    def _resolve(self, data, errors, field):
        for layer in self.layers():
            if layer["enabled"] and layer["id"] in errors:
                return dict(value=None, source=layer["id"], status="error", message=errors[layer["id"]])
        for layer in self.layers():
            value = data[layer["id"]].get(field["id"])
            if layer["enabled"] and value is not None and value != "":
                if field["id"] == "binding" and (not isinstance(value, dict) or set(value) != {"identity", "workspace", "resource"} or not all(isinstance(v, str) and v.strip() for v in value.values())):
                    return dict(value=value, source=layer["id"], status="invalid", message="绑定记录不完整；不会从其他层补齐。")
                return dict(value=value, source=layer["id"], status="set")
        return dict(value=field.get("default"), source="default" if "default" in field else None,
                    status="default" if "default" in field else "missing" if field.get("required") else "optional")

    def _snapshot(self, data, revision, errors, changes=None):
        layers, fields = self.layers(), []
        resolved = {f["id"]: self._resolve(data, errors, f) for f in self.fields}
        values = {name: record["value"] for name, record in resolved.items()}
        for field in self.fields:
            name, result = field["id"], resolved[field["id"]]
            rows, targets = [], []
            for layer in layers:
                lid, present = layer["id"], name in data[layer["id"]]
                value = data[lid].get(name)
                if not layer["readonly"] and (not field.get("sensitive") or layer["scope"] != "project" or lid == "project_local" or present):
                    targets.append(lid)
                status = "skipped" if not layer["enabled"] else "error" if lid in errors else "missing" if not present else "empty" if value is None or value == "" else "effective" if result["source"] == lid else "shadowed"
                rows.append(dict(layer=lid, present=present, value=value, status=status, message=errors.get(lid, "")))
            fields.append(dict(field, active=active(field, values), targets=targets, effective=result, rows=rows))
        issues = validation_errors(self.fields, values, changes or {})
        issues.extend(f["label"] + "：" + f["effective"].get("message", "配置无效") for f in fields if f["effective"]["status"] in ("error", "invalid"))
        return dict(revision=revision, layers=layers, fields=fields, default_targets={"project": "project_local", "global": "plugin_env"},
                    validation="本地类型、依赖与必需项校验（未验证 API 连通性）", validation_errors=list(dict.fromkeys(issues)),
                    note="合成分层示例：所有项目／全局文件都位于当前演示目录，不读取真实配置。")

    def snapshot(self):
        return self._snapshot(*self._read())

    def _candidate(self, changes, revision, target):
        data, current, errors = self._read()
        if current != revision:
            raise UIError("conflict", "配置已变化，草稿已保留；请重新载入并核对。", 409)
        snapshot = self._snapshot(data, current, errors)
        layer = next((l for l in snapshot["layers"] if l["id"] == target), None)
        if layer is None or layer["readonly"] or target in errors:
            raise UIError("target", "所选配置层不可写。")
        if not isinstance(changes, dict):
            raise UIError("changes", "变更格式无效。")
        for name in changes:
            field = next((f for f in snapshot["fields"] if f["id"] == name), None)
            if field is None or target not in field["targets"]:
                raise UIError("target", "此字段不能保存到所选文件。")
        # This loader treats explicit empty strings as fallthrough for every ordinary field.
        nonempty_changes = {k: v for k, v in changes.items() if not isinstance(v, dict) or v.get("op") != "set" or v.get("value") != ""}
        candidate = apply_changes(data[target], self.fields, nonempty_changes)
        for name in changes.keys() - nonempty_changes.keys():
            candidate[name] = ""
        data[target] = candidate
        return data, self._snapshot(data, current, errors, changes)

    def preview(self, changes, revision, target):
        return self._candidate(changes, revision, target)[1]

    def save(self, changes, revision, target):
        if not changes:
            raise UIError("changes", "没有待保存的修改。")
        with file_lock(self.directory / "layers.lock"):
            data, candidate = self._candidate(changes, revision, target)
            if candidate["validation_errors"]:
                raise UIError("validation", "；".join(candidate["validation_errors"]), 422)
            atomic_json(self.path(target), data[target], owner_path=self.project)
        return self.snapshot()


class LayeredStudio(Studio):
    title = "内容工作室 · 分层配置示例"

    def __init__(self, project):
        super().__init__(project)
        self.store = LayeredDemo(project)

    def config(self):
        return dict(self.store.snapshot(), task={"id": "layers-demo", "binding": str(self.project),
                    "continuation": "继续当前演示项目的 layers-demo 任务，先核对配置和原任务授权。"})


def build(project):
    return LayeredStudio(project)
