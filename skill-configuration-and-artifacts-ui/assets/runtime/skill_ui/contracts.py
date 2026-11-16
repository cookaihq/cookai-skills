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

    Config: configuration_protocol='field-scopes-v1', revision, layers (in loader order),
    fields, default_targets, validation_errors. annotate_snapshot attaches per-field
    edit_revision and loader-defined effective.empty. Save/preview receive one
    {files, switches, file_edits} envelope and revision; legacy target is rejected.
    readiness {ready, missing: field IDs} describes business prerequisites, not save errors.
    Optional presentation {title, description, focus: field IDs, initial_target} supplies
    opening defaults. URL fields/target may select another view, never grant write access.
    Fields: id (UI identity), config_key (actual key/path, no values), label, type, sensitive, required, advanced, readonly, active, group,
    help, choices, depends_on, targets, effective {source, status, value, message},
    optional directory_scope ('global'/'project'), default (absolute root), anchor
    (fixed selector file). directory_targets maps edited field IDs to projected
    absolute destination files; the business adapter owns root resolution/policy.
    rows [{layer, present, status, value, message}]. The adapter owns resolution.
    Types: text, number, boolean, select, list, json. depends_on maps field IDs to values.
    Artifacts need only id and title; optional kind, task, summary, preview, resource,
    url, status, metadata. preview: {type, text|columns+rows|resource|renderer+data}.
    All snapshots must be JSON serializable. Artifacts must already be project-filtered.
    """

    title = "Skill"

    def config(self):
        raise UIError("unsupported", "此目标没有配置页面。", 404)

    def preview(self, changes, revision):
        """Resolve a candidate through the same loader without writing any files."""
        raise UIError("unsupported", "此目标不支持配置预览。", 404)

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
    if "layers" not in snapshot or snapshot.get("configuration_protocol") != "field-scopes-v1":
        raise UIError("contract", "配置适配器需要迁移到逐项范围协议；请同时升级适配器和共用核心。", 422)
    result = {k: snapshot[k] for k in ("configuration_protocol", "revision", "directory_revision", "task", "validation", "note", "default_targets", "validation_errors") if k in snapshot}
    presentation = snapshot.get("presentation", {})
    result["presentation"] = {k: presentation[k] for k in ("title", "description", "focus", "initial_target") if k in presentation}
    # Compute before masking; adapters may additionally report business-specific missing conditions.
    missing = [f["id"] for f in snapshot["fields"] if f.get("active", True) and f.get("required")
               and (f["effective"].get("empty") is True or f["effective"].get("status") in ("missing", "error", "invalid"))]
    readiness = snapshot.get("readiness", {})
    missing = list(dict.fromkeys([*missing, *readiness.get("missing", [])]))
    known = {f["id"] for f in snapshot["fields"]}
    result["readiness"] = {"missing": [name for name in missing if name in known],
                           "ready": not missing and readiness.get("ready", True)}
    layer_keys = ("id", "name", "scope", "scope_label", "path", "readonly", "enabled", "exists", "message")
    result["layers"] = [{k: layer[k] for k in layer_keys if k in layer} for layer in snapshot["layers"]]
    result["fields"] = []
    allowed = ("id", "config_key", "label", "type", "sensitive", "required", "advanced", "readonly", "active", "group", "help",
               "choices", "choice_labels", "depends_on", "targets", "effect", "edit_revision",
               "directory_scope", "default", "anchor")
    for field in snapshot["fields"]:
        item = {k: field[k] for k in allowed if k in field}
        def public_value(record, keys):
            public = {k: record[k] for k in keys if k in record}
            if field.get("sensitive"):
                public.update(mask=masked(record.get("value")), configured=record.get("value") not in (None, ""))
            else:
                public["value"] = record.get("value")
            return public
        item["effective"] = public_value(field["effective"], ("source", "status", "message", "empty"))
        item["rows"] = [public_value(row, ("layer", "present", "status", "message")) for row in field["rows"]]
        result["fields"].append(item)
    plan_keys = ("target", "state", "cleanup", "writes_target", "conflict", "choice_token", "input_changed", "uses_target", "message")
    if "scope_plans" in snapshot:
        result["scope_plans"] = {key: {name: plan[name] for name in plan_keys if name in plan}
                                 for key, plan in snapshot["scope_plans"].items() if key in known}
    result["unchanged_fields"] = [key for key in snapshot.get("unchanged_fields", []) if key in known]
    if "directory_targets" in snapshot:
        result["directory_targets"] = {key: path for key, path in snapshot["directory_targets"].items()
                                       if key in known and isinstance(path, str)}
    return result
