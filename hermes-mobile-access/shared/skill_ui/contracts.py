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

    Config fields: id, label, type, value, sensitive, required, advanced, readonly,
    group, help, choices, depends_on, explicit, default, scope, path, effect, clear_help.
    Types: text, number, boolean, select, list, json. depends_on maps field IDs to values.
    Artifacts need only id and title; optional kind, task, summary, preview, resource,
    url, status, metadata. preview: {type, text|columns+rows|resource|renderer+data}.
    All snapshots must be JSON serializable. Artifacts must already be project-filtered.
    """

    title = "Skill"

    def config(self):
        raise UIError("unsupported", "此目标没有配置页面。", 404)

    def save(self, changes, revision):
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
    result = {k: snapshot[k] for k in ("revision", "task", "validation", "note") if k in snapshot}
    result["fields"] = []
    allowed = ("id", "label", "type", "sensitive", "required", "advanced", "readonly", "group", "help",
               "choices", "depends_on", "explicit", "scope", "path", "effect", "clear_help")
    for field in snapshot["fields"]:
        item = {k: field[k] for k in allowed if k in field}
        if field.get("sensitive"):
            item.update(mask=masked(field.get("value")), configured=field.get("value") not in (None, ""))
        else:
            item["value"] = field.get("value")
            if "default" in field:
                item["default"] = field["default"]
        result["fields"].append(item)
    return result
