"""Real dotenv configuration UI. Unverified connection conditions stay visible."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from configuration import ConfigStore, ConfigError, draft, new_id, missing_binding, LABEL
from frp_client import FrpClient
from desktop_profiles import read_profiles, path_key
from native_control import ControlError
from access_security import AUTH_TYPE, KEY_HEADER
from connection_runtime import Manager
from skill_ui import Adapter
from skill_ui.contracts import UIError
sys.path.insert(0, str(ROOT / 'ui'))
from access_fields import access_fields
from access_policy import GROUP_IDS


class Assets:
    """Compose fixed vendored assets and business controls without editing core."""
    def __init__(self, name=None):
        self.name = name

    def __truediv__(self, name):
        return Assets(name)

    def read_bytes(self):
        raw = (ROOT / "shared/skill_ui/static" / self.name).read_bytes()
        extra = {"app.js": "configuration.js", "style.css": "configuration.css"}.get(self.name)
        if extra:
            raw += b"\n" + (ROOT / "ui" / extra).read_bytes()
            raw += b"\n" + (ROOT / 'ui' / ('access.js' if self.name == 'app.js' else 'access.css')).read_bytes()
        return raw


class Configuration(Adapter):
    title = "Hermes 手机接入"

    def __init__(self, project, options):
        import skill_ui.server
        skill_ui.server.STATIC = Assets()
        self.project, self.options = Path(project), options
        self.store = ConfigStore(project, home=options["home"], use_local_key=options["use_local_key"],
                                 scope=options.get("scope", "personal"))
        self.explicit = options.get("connection")
        self.draft_id = new_id()
        self._profile_cache = None
        self.manager = Manager(self.store, options['frp_skill'], options.get('candidate'))

    def _profiles(self, force=False):
        if force or self._profile_cache is None or time.monotonic() - self._profile_cache[0] > 5:
            self._profile_cache = (time.monotonic(), read_profiles(selected=self.options.get("candidate")))
        return self._profile_cache[1]

    def _matching_home(self, record, profiles):
        if profiles.get("result") != "profiles_verified":
            return False
        saved = record["hermes"].get("home")
        try:
            return not saved or path_key(saved) == path_key(profiles["home"])
        except ControlError:
            return False

    def _snapshot(self, force_profiles=False):
        snap = self.store.snapshot()
        if snap.collection["connections"]:
            cid, record = snap.select(self.explicit)
        else:
            if self.explicit:
                raise ConfigError("selection_required", "指定连接不存在。", snap.source)
            cid, record = self.draft_id, draft()
        frp = record["frp"]
        client = FrpClient(self.options["frp_skill"], frp.get("project") or self.options["frp_project"],
                           frp.get("home") or self.options["frp_home"], cid)
        try:
            context = client.context()
        except ConfigError as exc:
            context = {"revision": exc.code, "domain_suffix": {"value": "", "status": "unavailable"}, "error": exc.report()}
        try:
            status = client.status()
        except ConfigError as exc:
            status = {"error": exc.report(), "modes": {"official": {"running": None}}}
        context["_update_revision"] = self.store.update_preference()[1]
        profiles = self._profiles(force_profiles)
        profile_revision = profiles.get("revision") or json.dumps(profiles, sort_keys=True)
        revision = hashlib.sha256((snap.revision + "|" + context["revision"] + "|" + context["_update_revision"]
                                   + "|" + profile_revision).encode()).hexdigest()
        return snap, cid, record, context, status, revision, profiles

    def config(self):
        try:
            snap, cid, record, context, status, revision, profiles = self._snapshot()
        except ConfigError as exc:
            return {"revision": "blocked", "fields": [], "note": str(exc),
                    "validation": "配置来源待修复：" + exc.source}
        suffix = context["domain_suffix"].get("value", "")
        running = status.get("modes", {}).get("official", {}).get("running")
        route = status.get("route", {})
        # A live frpc process alone cannot establish the effective HTTPS URL.
        # The connection controller supplies timestamped endpoint evidence.
        current = "入口尚未验证" if running else "未连接" if running is False else "状态无法核对"
        try:
            runtime = self.manager.status(cid)
        except ConfigError as exc:
            runtime = {'result': exc.code, 'reason': str(exc)}
        if runtime.get('active_url') and runtime.get('result') == 'awaiting_mobile':
            current = runtime['active_url']
        target = str(snap.target) if snap.target else "当前环境来源（只读）"
        fields = []
        def field(id, label, value, *, group="域名设置", readonly=False, **extra):
            fields.append(dict(id=id, label=label, value=value, type="text", readonly=readonly,
                explicit=not readonly and cid in snap.collection["connections"], group=group,
                scope="只读状态" if readonly else "个人配置" if snap.target and snap.target.parent == self.store.home else "当前来源",
                path="" if readonly else target, effect="" if readonly else "保存当前连接配置；运行状态单独核对。", **extra))
        field("subdomain", "Server URL（服务器地址）", record["domain_prefix"], required=True,
              group="手机连接信息",
              default="hermes-mobile", help="只编辑中间的服务子域名；复制得到完整 Server URL。")
        field("access_key", "手机接入密钥", (record["auth"] or {}).get("key", ""),
              group="手机连接信息", sensitive=True, required=True,
              help="生成后保存，在 Hermes Mobile 的 Custom Headers 中填写 " + KEY_HEADER + "。秘密仅在本页查看或复制。",
              clear_help="清除此连接的接入密钥，不回退其他密钥；已启动的适配层检测到变更后关闭连接。")
        fields[-1]["explicit"] = record["auth"] is not None
        fields[-1]["effect"] = "保存接入密钥；替换或清除会使旧认证失效。公网连接与手机验证单独执行。"
        field("auth", "入口认证", "独立接入密钥 · 手机 Token 模式", group="手机连接信息", readonly=True)
        permissions, permission_metadata = access_fields(record, cid, target, readonly=snap.target is None)
        fields.extend(permissions)
        field("name", "连接名称", record["name"], required=True)
        field("suffix", "FRP 配置域名", suffix or "未配置", readonly=True, advanced=True,
              help="从 FRP Launch 的原项目和个人配置上下文读取。")
        frp_note = "父域：" + suffix if suffix else "父域缺失或接口不可用，请先修复 FRP Launch 配置。"
        if context.get("connection", {}).get("status") != "configured":
            frp_note += " 服务器连接仍有缺项，请通过 FRP Launch 补齐。"
        field("frp_config", "FRP 配置", frp_note, readonly=True)
        field("domain_policy", "域名占用判定", "正常访问为占用，否则为未占用；切换前检查。", readonly=True, advanced=True)
        field("active_url", "当前生效的 Server URL", current, group="运行绑定", readonly=True)
        field("frp_domain", "FRP 路由域名", route.get("domain", "尚未配置"), group="运行绑定", readonly=True)
        field("proxy", "本连接 FRP 代理", route.get("proxy", {}).get("name", "尚未创建"), group="运行绑定", readonly=True)
        field("process", "FRP 客户端", {True: "运行中", False: "未运行", None: "无法核对"}[running], group="运行绑定", readonly=True)
        field("registration", "域名注册", "FRP 启动及公网入口已验证；手机待验证" if runtime.get('active_url') else
              "尚未取得当前公网入口证据", group="运行绑定", readonly=True)
        proxy = route.get("proxy", {})
        field("local_target", "FRP 本地转发目标", str(proxy.get("localIP")) + ":" + str(proxy.get("localPort"))
              if running and proxy.get("localPort") else "未绑定", group="运行绑定", readonly=True)
        backend = runtime.get('backend', {})
        field("binding", "Desktop 端口绑定", (str(backend.get('host')) + ':' + str(backend.get('port'))
              if runtime.get('active_url') and backend else "当前未绑定已验证后端"), group="运行绑定", readonly=True)
        field("last_check", "最近检查", runtime.get('checked_at', '尚未连接'), group="运行绑定", readonly=True)
        labels = {"hermes.home": "Desktop 实例", "hermes.preferred_profile": "首选 profile", "hermes.profile_home": "profile 归属",
                  "frp.project": "FRP 项目", "frp.home": "FRP 配置位置", "frp.source_binding": "FRP 来源绑定", "auth": "入口认证"}
        missing = missing_binding(record)
        field("last_result", "接入条件", "待配置：" + "、".join(labels[k] for k in missing) if missing else
              ("公网 REST/WS 已验证；等待手机聊天确认。" if runtime.get('active_url') else
               "连接状态：" + runtime.get('result', 'stopped') + "；" + str(runtime.get('reason') or '等待连接')),
              group="运行绑定", readonly=True)
        matches = self._matching_home(record, profiles)
        instance = profiles.get("home") if matches else record["hermes"].get("home")
        field("instance", "Hermes 逻辑实例", instance or "待目标电脑上核验", group="Hermes", readonly=True)
        diagnosis = ("已验证本机身份和 profile 列表；手机使用情况仍待核对。" if matches else
                     "当前发现实例与已保存连接不一致，请先核对目标实例。" if profiles.get("result") == "profiles_verified" else
                     "尚未核实真实列表：" + profiles.get("detail", profiles.get("reason", "未知")))
        field("profile_status", "profile 检查", diagnosis, group="Hermes", readonly=True)
        if matches:
            field("profile_checked", "profile 检查时间", profiles["checked_at"], group="Hermes", readonly=True, advanced=True)
        preferred = record["hermes"].get("preferred_profile", "")
        choices = [row["name"] for row in profiles.get("profiles", []) if row["served"]] if matches else []
        fields.append(dict(id="profile", label="手机聊天首选的 profile", type="select", value=preferred,
            choices=choices if matches else [preferred] if preferred else [], readonly=not matches,
            explicit=bool(preferred), required=True, group="Hermes", path=target, scope="当前连接",
            help="选择并保存手机聊天首选目标；不修改 Desktop 当前 profile。接入时在手机核对，之后可在手机切换。"))
        fields.append(dict(id="monitor", label="连接期间自动恢复", type="boolean", value=record["auto_recover"],
            default=True, explicit=True, group="恢复", scope="当前连接", path=target,
            help="连接期间生效；断开时停止，不默认登录自启。关闭恢复仍检测身份失效并停止旧转发。"))
        fields.append(dict(id="update", label="自动检查 Skill 更新", type="boolean", value=self.store.update_preference()[0],
            default=True, explicit=True, group="高级", advanced=True, scope="个人设置", path=str(self.store.home / ".env"),
            help="只检查与提醒；应用更新仍逐次确认。此开关固定从个人配置读取。"))
        return {"revision": revision, "fields": fields,
                "note": "使用真实配置加载器保存连接。已有连接的变更先检查再应用；首次配置保存后由 Agent 连接。独立密钥用于手机 Token 模式。",
                "validation": "本机配置格式、来源与修订检查；公网和手机未验证",
                "task": {"id": cid, "binding": str(self.project), **permission_metadata,
                         "continuation": "继续配置 Hermes 手机接入，核验目标电脑、FRP 和入口认证。"}}

    def save(self, changes, revision):
        try:
            snap, cid, record, context, status, current, profiles = self._snapshot(force_profiles=True)
            if revision != current:
                raise ConfigError("conflict", "FRP 或连接配置已变化，请重新载入；草稿保留。")
            permission_ids = {protocol + '_' + group for protocol in ('http', 'websocket') for group in GROUP_IDS}
            permission_ids.add('access_websocket_enabled')
            if (not isinstance(changes, dict) or not changes or
                    set(changes) - ({"name", "subdomain", "profile", "access_key", "monitor", "update"} | permission_ids)):
                raise ConfigError("readonly", "变更包含只读或未验证字段。")
            auto_update = None
            if "update" in changes:
                change = changes["update"]
                if not isinstance(change, dict) or change.get("op") not in ("set", "unset"):
                    raise ConfigError("changes", "变更格式无效。")
                auto_update = True if change["op"] == "unset" else change.get("value")
                if type(auto_update) is not bool:
                    raise ConfigError("invalid_update", "自动检查更新须为布尔值。")
                if len(changes) == 1:
                    self.store.save_update_preference(context["_update_revision"], auto_update)
                    return self.config()
            values = copy.deepcopy(record)
            for key, change in changes.items():
                if key == "update":
                    continue
                if not isinstance(change, dict) or change.get("op") not in ("set", "unset"):
                    raise ConfigError("changes", "变更格式无效。")
                if key in permission_ids:
                    access = values.setdefault('access', {})
                    if key == 'access_websocket_enabled':
                        target, leaf = access, 'websocket_enabled'
                    else:
                        protocol, leaf = key.split('_', 1)
                        target = access.setdefault(protocol, {})
                    if change['op'] == 'unset':
                        target.pop(leaf, None)
                    elif type(change.get('value')) is not bool:
                        raise ConfigError('invalid_access_policy', '访问开关必须为开启或关闭。')
                    else:
                        target[leaf] = change['value']
                    continue
                if key == "access_key":
                    values["auth"] = None if change["op"] == "unset" else {"type": AUTH_TYPE, "key": change.get("value")}
                    continue
                if key == "profile":
                    if not self._matching_home(record, profiles):
                        raise ConfigError("profile_unverified", "真实后端或 profile 列表尚未验证，不能保存绑定。")
                    if change["op"] == "unset":
                        values["hermes"] = {}
                        continue
                    selected = next((row for row in profiles["profiles"]
                                     if row["name"] == change.get("value") and row["served"]), None)
                    if selected is None:
                        raise ConfigError("profile_unavailable", "该 profile 当前不可用，请重新载入核对。")
                    values["hermes"] = {"home": profiles["home"], "preferred_profile": selected["name"],
                                        "profile_home": selected["path"]}
                    continue
                dest = {"name": "name", "subdomain": "domain_prefix", "monitor": "auto_recover"}[key]
                values[dest] = draft()[dest] if change["op"] == "unset" else change.get("value")
            if "subdomain" in changes:
                prefix = values["domain_prefix"]
                if not isinstance(prefix, str) or not LABEL.fullmatch(prefix.lower()):
                    raise ConfigError("subdomain", "只填写单个服务子域名。")
                if context["domain_suffix"].get("value") and len(prefix + "." + context["domain_suffix"]["value"]) > 253:
                    raise ConfigError("subdomain", "完整主机名超过长度限制。")
            running = status.get("modes", {}).get("official", {}).get("running")
            if (running is not False and not self.manager.status(cid).get('controller_running')
                    and any(values[k] != record[k] for k in ("domain_prefix", "auto_recover", "hermes", "access"))):
                raise ConfigError("preflight_unknown", "FRP 不属于当前可核验的接入进程；先断开并核对，原配置保留。")
            collection = copy.deepcopy(snap.collection)
            first = not collection["connections"]
            if cid not in collection["connections"] and not values["frp"]:
                values["frp"] = {"project": self.options["frp_project"], "home": self.options["frp_home"]}
                if context.get("connection", {}).get("status") == "configured" and context.get("source_binding"):
                    values["frp"]["source_binding"] = context["source_binding"]
            collection["connections"][cid] = values
            self.manager.apply(cid, snap, collection, default_id=cid if first and not snap.selector else None,
                               auto_update=auto_update)
            self.explicit = cid
            return self.config()
        except ConfigError as exc:
            raise UIError(exc.code, str(exc), 409 if exc.code in ("conflict", "preflight_unknown") else 422) from None


def build(project):
    path = Path(os.environ["HMA_UI_CONTEXT"])
    options = json.loads(path.read_text(encoding="utf-8"))
    if Path(options["project"]).resolve() != project.resolve():
        raise UIError("context", "配置页面项目绑定不匹配。")
    return Configuration(project, options)
