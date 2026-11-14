"""The UI edits the real dotenv protocol, with one atomic connection group."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from pathlib import Path

from skill_ui.contracts import Adapter, UIError
from skill_ui.storage import file_lock, prepare_private_write

from . import config, private_files, vision

SCALARS = {"asr": config.PROVIDER_KEY, "output": "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR",
           "source": "VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR", "ffmpeg": "FFMPEG_BIN", "ffprobe": "FFPROBE_BIN",
           "feishu-profile": config.IDENTITY_KEYS[0], "feishu-app": config.IDENTITY_KEYS[1], "feishu-user": config.IDENTITY_KEYS[2]}
LABELS = {"asr": "语音转写", "output": "任务输出目录", "source": "源视频保存目录", "ffmpeg": "FFmpeg 路径", "ffprobe": "FFprobe 路径",
          "feishu-profile": "飞书 Profile", "feishu-app": "飞书应用 ID", "feishu-user": "飞书用户 Open ID",
          "method": "图像识别方式", "model": "看图模型 ID", "base_url": "Base URL", "api_key": "API Key",
          "runtime": "独立 Agent 运行器", "protocol": "接口协议", "accept_unverified_model": "接受实际模型无法核验",
          "max_images": "每次请求最多图片数", "max_request_bytes": "每次请求最多字节", "max_context_chars": "上下文字符上限",
          "ask": "每次新任务询问", "call_limit": "模型请求次数上限"}


class VideoAdapter(Adapter):
    title = "video-to-markdown 配置"

    def __init__(self, project: Path, *, host="codex", use_home=False, home_config_dir=None, caps=None, environ=None):
        self.project = project.resolve()
        self.host, self.use_home = host, use_home
        self.home_dir = home_config_dir
        self.env = dict(os.environ if environ is None else environ)
        self.caps = caps or vision.capabilities(host=host)
        self.secret = secrets.token_bytes(32)
        home = (Path(home_config_dir) if home_config_dir else Path.home() / ".config" / "video-to-markdown") / ".env"
        self.targets = {"project": self.project / ".env.video-to-markdown", "local": self.project / ".env.local",
                        "env": self.project / ".env", "global": home}

    def layers(self):
        return [{"id": "process", "name": "进程环境", "scope": "process", "path": "process environment", "readonly": True, "enabled": True}] + [
            {"id": k, "name": p.name if k != "global" else "个人全局", "scope": "global" if k == "global" else "project",
             "path": str(p), "readonly": k == "global" and not self.use_home, "enabled": k != "global" or self.use_home,
             "message": "全局未启用；未读取。使用 --use-local-key 重开可编辑。" if k == "global" and not self.use_home else ""}
            for k, p in self.targets.items()]

    def inputs(self):
        result: dict = {"process": {k: self.env[k] for k in config.KNOWN_NAMES if k in self.env}}
        for key, path in self.targets.items():
            result[key] = None if key == "global" and not self.use_home else (path.read_bytes() if path.exists() else b"")
        return result

    def revision(self):
        inputs = self.inputs()
        raw = json.dumps({k: v.hex() if isinstance(v, bytes) else v for k, v in inputs.items()}, sort_keys=True).encode()
        return hmac.new(self.secret, raw, hashlib.sha256).hexdigest()

    def values(self, override=None):
        values = {"process": self.env}
        for key, path in self.targets.items():
            values[key] = {} if key == "global" and not self.use_home else config.parse_dotenv(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if override:
            key, value = override
            values[key] = value
        return values

    def field_value(self, field, values):
        if field in SCALARS:
            return values.get(SCALARS[field])
        group = config.vision_group(values.get(config.VISION_KEY))
        if group is None:
            return None
        if field == "ask":
            return group.get("ask_each_task", True)
        if field == "call_limit":
            return group.get("call_limit")
        route = group.get("routes", {}).get(self.host) or group.get("routes", {}).get("any", {})
        return route.get(field)

    def snapshot(self, override=None):
        values = self.values(override)
        overrides = {str(p): values[k] for k, p in self.targets.items() if k != "global" or self.use_home}
        resolved = config.resolve(self.env, self.project, use_home=self.use_home, home_config_dir=self.home_dir, overrides=overrides)
        layers = self.layers()
        fields = []
        vision_source = resolved.sources.get("vision")
        effective_layer = next((l["id"] for l in layers if l["path"] == vision_source), None)
        resolved_scalars = {"asr": (resolved.asr_provider, "asr_provider"), "output": (resolved.output_base, "output_base"),
                            "source": (resolved.source_video_dir, "source_video_dir"), "ffmpeg": (resolved.ffmpeg_bin, "ffmpeg_bin"),
                            "ffprobe": (resolved.ffprobe_bin, "ffprobe_bin")}
        for field in ["ask", "method", "model", "base_url", "api_key", "runtime", "protocol", "accept_unverified_model",
                      "max_images", "max_request_bytes", "max_context_chars", "call_limit", *SCALARS]:
            rows = [{"layer": l["id"], "present": self.field_value(field, values[l["id"]]) is not None,
                     "value": self.field_value(field, values[l["id"]]),
                     "status": "configured" if self.field_value(field, values[l["id"]]) is not None else "absent"} for l in layers]
            source = effective_layer
            current = self.field_value(field, values[source]) if source else None
            if field in resolved_scalars:
                current, source_key = resolved_scalars[field]
                source = next((l["id"] for l in layers if l["path"] == resolved.sources.get(source_key)), None)
            elif field.startswith("feishu-"):
                source = next((l["id"] for l in layers if l["path"] == resolved.sources.get("feishu_identity")), None)
                current = self.field_value(field, values[source]) if source else None
            if field == "ask" and current is None:
                current = True
            key = SCALARS.get(field, config.VISION_KEY + (".ask_each_task" if field == "ask" else ".call_limit" if field == "call_limit" else ".routes." + self.host + "." + field))
            fields.append({"id": field, "config_key": key, "label": LABELS[field], "type": "boolean" if field in {"ask", "accept_unverified_model"} else "number" if field in {"call_limit", "max_images", "max_request_bytes", "max_context_chars"} else "text",
                           "sensitive": field == "api_key", "group": "vision" if field not in SCALARS else "advanced",
                           "targets": [l["id"] for l in layers if not l["readonly"]],
                           "effective": {"source": source or "default", "status": "configured" if source else "default", "value": current}, "rows": rows})
        fields.append({"id": "capabilities", "config_key": "current-session capability evidence", "label": "宿主能力", "type": "json", "readonly": True,
                       "effective": {"source": self.caps["source"], "status": "reported", "value": vision.options(self.caps)}, "rows": []})
        return {"revision": self.revision(), "layers": layers, "fields": fields, "default_targets": ["project", "global"],
                "note": "全局已显式启用" if self.use_home else "全局未启用；本次任务不会读取全局配置",
                "validation_errors": [], "validation": "真实 dotenv 加载器校验及保存后回读"}

    def config(self):
        try:
            return self.snapshot()
        except (config.ConfigError, ValueError, OSError):
            raise UIError("configuration", "配置无法解析；请先修复对应 dotenv 文件。不会回退到其他凭证来源。") from None

    def candidate(self, changes, revision, target):
        if target not in self.targets or (target == "global" and not self.use_home):
            raise UIError("target", "请选择已启用的项目或全局保存文件。")
        if revision != self.revision():
            raise UIError("conflict", "配置已被其他进程修改；草稿保留，请重新载入核对。", 409)
        if not isinstance(changes, dict) or set(changes) - (set(LABELS) | {"inherit"}):
            raise UIError("field", "包含未知配置字段。")
        values = self.values()[target]
        group = config.vision_group(values.get(config.VISION_KEY)) or {"ask_each_task": True, "routes": {}}
        # Copy only this file's group. Never copy a higher layer's connection/Key.
        group = json.loads(json.dumps(group))
        route = group.setdefault("routes", {}).setdefault(self.host, {})
        updates: dict[str, str | None] = {}
        for field, operation in changes.items():
            if not isinstance(operation, dict) or operation.get("op") not in {"set", "unset"} or set(operation) - {"op", "value"}:
                raise UIError("operation", "配置修改操作无效。")
            unset = operation["op"] == "unset"
            value = None if unset else operation.get("value")
            if field == "inherit":
                if value is not True:
                    raise UIError("operation", "继承操作无效。")
                updates[config.VISION_KEY] = None
                continue
            if field in {"ask", "accept_unverified_model"} and not unset and type(value) is not bool:
                raise UIError("type", "开关必须是布尔值。")
            if field in {"call_limit", "max_images", "max_request_bytes", "max_context_chars"} and not unset and (type(value) is not int or value < 1):
                raise UIError("type", "上限必须是正整数，或移除以使用默认值。")
            if field not in {"ask", "accept_unverified_model", "call_limit", "max_images", "max_request_bytes", "max_context_chars"} and not unset and (not isinstance(value, str) or any(c in value for c in "\r\n\0") or "••" in value):
                raise UIError("type", "字段类型或内容无效；遮盖值不能提交为密钥。")
            if field in SCALARS:
                updates[SCALARS[field]] = value
            elif field in {"ask", "call_limit"}:
                name = "ask_each_task" if field == "ask" else field
                if unset:
                    group.pop(name, None)
                else:
                    group[name] = value
            elif unset:
                route.pop(field, None)
            else:
                route[field] = value
        if any(f not in SCALARS and f != "inherit" for f in changes) and "inherit" not in changes:
            # A new method must not retain a stale model/provider from the prior method.
            unused = {"main-agent": {"model", "base_url", "api_key", "runtime", "protocol", "accept_unverified_model"},
                      "host-agent": {"base_url", "api_key", "runtime", "protocol"},
                      "api": {"runtime"}}.get(route.get("method"), set())
            for name in unused:
                route.pop(name, None)
            if route:
                try:
                    vision.validate_route(route, complete=not group.get("ask_each_task", True))
                except vision.VisionError:
                    raise UIError("dependency", "识别方式或连接不完整：检查模型、Base URL、Key、协议与运行器。") from None
            else:
                group["routes"].pop(self.host, None)
                if not group.get("ask_each_task", True):
                    raise UIError("dependency", "固定方式需要先选择识别方式。")
            updates[config.VISION_KEY] = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
        for name in config.IDENTITY_KEYS:
            if name in updates:
                identity = {k: updates.get(k, values.get(k)) for k in config.IDENTITY_KEYS}
                if any(identity.values()) and not all(identity.values()):
                    raise UIError("dependency", "飞书 Profile、应用 ID、用户 Open ID 必须在同一文件中完整保存。")
        merged = {**values, **{k: v for k, v in updates.items() if v is not None}}
        for k, v in updates.items():
            if v is None:
                merged.pop(k, None)
        try:
            snapshot = self.snapshot((target, merged))
        except (ValueError, OSError):
            raise UIError("validation", "候选配置未通过实际加载器校验，未保存。") from None
        return updates, snapshot

    def preview(self, changes, revision, target):
        return self.candidate(changes, revision, target)[1]

    def save(self, changes, revision, target):
        updates, _ = self.candidate(changes, revision, target)
        path = self.targets[target]
        prepare_private_write(path, owner_path=self.project)
        with file_lock(path.with_name(path.name + ".lock"), protect_git=True, owner_path=self.project):
            updates, _ = self.candidate(changes, revision, target)
            original = path.read_bytes() if path.exists() else b""
            text = config.render_dotenv(original.decode("utf-8-sig"), updates)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            prepare_private_write(temporary, temporary_for=path, owner_path=self.project)
            try:
                # Create and protect the empty file before opening the data stream.
                # Windows may deny WRITE_OWNER while another CRT handle is open.
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                private_files.protect(temporary, 0o600, assign_owner=False)
                with temporary.open("wb") as stream:
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if revision != self.revision():
                    raise UIError("conflict", "保存前配置发生变化，未覆盖；草稿保留。", 409)
                private_files.replace_file(temporary, path)
                private_files.protect(path, 0o600, assign_owner=False)
            except OSError as exc:
                code = getattr(exc.__cause__, "winerror", getattr(exc, "winerror", None))
                raise UIError("permission", "保存文件权限或原子替换失败；草稿保留。" + (" Windows 错误 " + str(code) if code else "")) from None
            finally:
                temporary.unlink(missing_ok=True)
        if config.parse_dotenv(path.read_text(encoding="utf-8")) != config.parse_dotenv(text):
            raise UIError("readback", "已写入但回读不一致，请重新载入检查。")
        return self.config()


def build(project: Path):
    caps_path = os.environ.get("VIDEO_VISION_UI_CAPABILITIES")
    host = os.environ.get("VIDEO_VISION_UI_HOST", "codex")
    return VideoAdapter(project, host=host, use_home=os.environ.get("VIDEO_VISION_UI_HOME") == "1",
                        caps=vision.capabilities(caps_path, host=host))
