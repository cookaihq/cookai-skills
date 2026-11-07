from dataclasses import dataclass
from pathlib import Path


class UIError(Exception):
    """Only safe, user-facing messages belong here. Never include a credential."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Resource:
    content: Path | bytes
    mime: str = "application/octet-stream"
    name: str = "download"


class Adapter:
    """Implement only the page(s) selected for the target. Methods run under a lock.

    Config: revision, layers (in loader order), fields, default_targets, validation_errors.
    Fields: id, label, type, sensitive, required, advanced, readonly, active, group,
    help, choices, depends_on, targets, effective {source, status, value, message},
    rows [{layer, present, status, value, message}]. The adapter owns resolution.
    Types: text, number, boolean, select, list, json. depends_on maps field IDs to values.
    Artifacts need only id and title; optional kind, task, summary, preview, resource,
    url, status, metadata. preview: {type, text|columns+rows|resource|renderer+data}.
    All snapshots must be JSON serializable. Artifacts must already be project-filtered.
    """

    title = "Skill"

    def config(self):
        raise UIError("unsupported", "此目标没有配置页面。", 404)

    def preview(self, changes, revision, target):
        """Resolve a candidate through the same loader without writing any files."""
        raise UIError("unsupported", "此目标不支持配置预览。", 404)

    def save(self, changes, revision, target):
        raise UIError("unsupported", "此目标不支持修改配置。", 404)

    def artifacts(self):
        return {"items": [], "note": "此目标尚未提供产物记录。"}

    def resource(self, resource_id):
        raise UIError("missing", "此文件不存在或已不可用。", 404)

    def extensions(self):
        """Optional trusted packaged JS/CSS: {URL basename: Path}; no user files."""
        return {}


def masked(value):
    value = str(value or "")
    return (value[:3] + "••••••" + value[-3:]) if len(value) > 8 else ("••••••" if value else "未填写")


def public_config(snapshot):
    if "layers" not in snapshot:
        raise UIError("contract", "配置适配器需要迁移到分层协议；请同时升级适配器和共用核心。", 422)
    result = {k: snapshot[k] for k in ("revision", "task", "validation", "note", "default_targets", "validation_errors") if k in snapshot}
    layer_keys = ("id", "name", "scope", "scope_label", "path", "readonly", "enabled", "message")
    result["layers"] = [{k: layer[k] for k in layer_keys if k in layer} for layer in snapshot["layers"]]
    result["fields"] = []
    allowed = ("id", "label", "type", "sensitive", "required", "advanced", "readonly", "active", "group", "help",
               "choices", "depends_on", "targets", "effect")
    for field in snapshot["fields"]:
        item = {k: field[k] for k in allowed if k in field}
        def public_value(record, keys):
            public = {k: record[k] for k in keys if k in record}
            if field.get("sensitive"):
                public.update(mask=masked(record.get("value")), configured=record.get("value") not in (None, ""))
            else:
                public["value"] = record.get("value")
            return public
        item["effective"] = public_value(field["effective"], ("source", "status", "message"))
        item["rows"] = [public_value(row, ("layer", "present", "status", "message")) for row in field["rows"]]
        result["fields"].append(item)
    return result
