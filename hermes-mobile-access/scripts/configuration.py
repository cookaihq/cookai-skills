"""Atomic named connections inside layered dotenv files; no business I/O."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from skill_ui.storage import file_lock, private_directory, _private_windows
from access_security import AUTH_TYPE, valid_key
from access_policy import normalize_access

SELECTOR = "HMA_CONNECTION"
COLLECTION = "HMA_CONNECTIONS_JSON"
UPDATE = "AUTO_UPDATE_CHECK"
ID = re.compile(r"hma_[0-9a-f]{32}\Z")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class ConfigError(Exception):
    def __init__(self, code, message, source=""):
        super().__init__(message)
        self.code, self.source = code, str(source)

    def report(self):
        return {"result": self.code, "detail": str(self), "source": self.source}


def new_id():
    return "hma_" + uuid.uuid4().hex


def draft(name="我的 Hermes"):
    return {"name": name, "domain_prefix": "hermes-mobile", "hermes": {},
            "frp": {}, "auth": None, "auto_recover": True, "access": {}}


def _object(value, allowed, field):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError("invalid_collection", field + " 的对象字段无效。")


def validate_record(value):
    _object(value, ("name", "domain_prefix", "hermes", "frp", "auth", "auto_recover", "access"), "连接")
    value = copy.deepcopy(value)
    name = value.get("name")
    if not isinstance(name, str):
        raise ConfigError("invalid_collection", "连接名称必须为文本。")
    name = unicodedata.normalize("NFC", name.strip())
    if not 1 <= len(name) <= 64 or any(unicodedata.category(c) == "Cc" for c in name):
        raise ConfigError("invalid_collection", "连接名称须为 1–64 个字符，且不含控制字符。")
    value["name"] = name
    prefix = value.setdefault("domain_prefix", "hermes-mobile")
    if not isinstance(prefix, str) or not LABEL.fullmatch(prefix.lower()):
        raise ConfigError("invalid_collection", "服务子域名须为一个合法 DNS 标签。")
    value["domain_prefix"] = prefix.lower()
    for key, fields in (("hermes", ("home", "preferred_profile", "profile_home")),
                        ("frp", ("project", "home", "source_binding"))):
        obj = value.setdefault(key, {})
        _object(obj, fields, key)
        if any(not isinstance(item, str) or any(c in item for c in "\r\n\0") for item in obj.values()):
            raise ConfigError("invalid_collection", key + " 的字段必须为单行文本。")
    auth = value.setdefault("auth", None)
    if auth is not None:
        if not isinstance(auth, dict) or set(auth) != {"type", "key"} or auth.get("type") != AUTH_TYPE:
            raise ConfigError("unsupported_auth", "不支持的入口认证类型或字段。")
        if not valid_key(auth.get("key")):
            raise ConfigError("invalid_access_key", "请生成新的 32 字节接入密钥，或填写其 43 字符 Base64URL 值；不要填写掩码。")
    if type(value.setdefault("auto_recover", True)) is not bool:
        raise ConfigError("invalid_collection", "auto_recover 必须为布尔值。")
    try:
        access = value.setdefault('access', {})
        if access is None:
            raise ValueError()
        normalize_access(access)
    except ValueError:
        raise ConfigError('invalid_access_policy', '访问范围只允许已声明的 HTTP / WebSocket 布尔开关。') from None
    return value


def validate_collection(value):
    _object(value, ("schema_version", "connections"), "配置集")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ConfigError("unsupported_schema", "不支持的配置集 schema_version。")
    connections = value.get("connections")
    if not isinstance(connections, dict):
        raise ConfigError("invalid_collection", "connections 必须为对象。")
    result, names = {}, set()
    for cid, record in connections.items():
        if not ID.fullmatch(cid):
            raise ConfigError("invalid_collection", "连接 ID 格式无效。")
        record = validate_record(record)
        key = record["name"].casefold()
        if key in names:
            raise ConfigError("duplicate_name", "连接名称重复。")
        names.add(key)
        result[cid] = record
    return {"schema_version": 1, "connections": result}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("duplicate_key", "JSON 中有重复字段或连接 ID。")
        result[key] = value
    return result


def parse_collection(raw, source):
    try:
        return validate_collection(json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except ConfigError as exc:
        exc.source = str(source)
        raise
    except (ValueError, TypeError, RecursionError):
        raise ConfigError("invalid_collection", "配置集 JSON 无效；请修复实际来源。", source) from None


def read_file(path):
    try:
        if path.is_symlink():
            raise ConfigError("unsafe_path", "配置来源不能是符号链接。", path)
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError:
        raise ConfigError("unreadable", "配置来源不可读。", path) from None


def parse_env(raw, source):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ConfigError("unreadable", "配置文件须为 UTF-8。", source) from None
    result = {}
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if not match or match[1] not in (SELECTOR, COLLECTION):
            continue
        key, value = match.groups()
        if key in result:
            raise ConfigError("duplicate_key", "dotenv 中有重复的 Hermes 配置字段。", source)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def update_env(raw, updates):
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    output, written = [], set()
    for line in text.splitlines(keepends=True):
        match = ASSIGNMENT.match(line.rstrip("\r\n"))
        key = match[1] if match else None
        if key in updates:
            if key not in written:
                output.append(key + "=" + updates[key] + newline)
                written.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in written:
            if output and not output[-1].endswith(("\r", "\n")):
                output.append(newline)
            output.append(key + "=" + value + newline)
    return "".join(output).encode("utf-8")


def atomic_bytes(path, content, owner):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ConfigError("unsafe_path", "保存目标必须是独立普通文件。", path)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "nt":
                _private_windows(temporary, owner)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclasses.dataclass(repr=False)
class Snapshot:
    collection: dict
    source: str
    selector: str
    selector_source: str
    revision: str
    target: Path | None

    def select(self, explicit=None):
        cid = self.selector if explicit is None else explicit
        if not cid or cid not in self.collection["connections"]:
            raise ConfigError("selection_required", "请选择有效连接；不会自动取第一条。", self.selector_source)
        return cid, copy.deepcopy(self.collection["connections"][cid])


class ConfigStore:
    def __init__(self, project, *, home=None, use_local_key=False, scope="personal", environ=None):
        self.project = Path(project).resolve()
        self.home = Path(home or Path.home() / ".config/hermes-mobile-access").expanduser().resolve()
        self.use_local_key, self.scope = use_local_key, scope
        self.environ = os.environ if environ is None else environ
        self.key = secrets.token_bytes(32)

    def layers(self):
        yield "env", {k: self.environ.get(k, "") for k in (SELECTOR, COLLECTION)}, None
        for path in [self.project / name for name in (".env.hermes-mobile-access", ".env.local", ".env")] + (
                [self.home / ".env"] if self.use_local_key else []):
            raw = read_file(path)
            yield str(path), parse_env(raw, path), raw

    def snapshot(self):
        chosen, observations = {}, []
        for source, values, raw in self.layers():
            observations.append([source, raw.hex() if raw is not None else values])
            for key in (SELECTOR, COLLECTION):
                if key not in chosen and values.get(key) not in (None, ""):
                    chosen[key] = (values[key], source)
            if len(chosen) == 2:
                break
        raw, source = chosen.get(COLLECTION, ("", ""))
        collection = parse_collection(raw, source) if raw else {"schema_version": 1, "connections": {}}
        selector, selector_source = chosen.get(SELECTOR, ("", ""))
        if selector and (not isinstance(selector, str) or not ID.fullmatch(selector)):
            raise ConfigError("invalid_selector", "默认连接 ID 格式无效。", selector_source)
        target = Path(source) if source and source != "env" else None
        if not source:
            target = self.home / ".env" if self.scope == "personal" else self.project / ".env.local"
            if self.scope == "personal" and not self.use_local_key:
                target = None
        if target:
            observations.append([str(target), read_file(target).hex()])
        revision = hmac.new(self.key, json.dumps(observations, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return Snapshot(collection, source, selector, selector_source, revision, target)

    def _check_target(self, path):
        # Existing project sources can be repaired in place; all must be ignored
        # and untracked. A new project configuration always uses .env.local.
        if path.parent == self.home:
            return
        if path.parent != self.project:
            raise ConfigError("unsafe_path", "保存目标不属于本次来源上下文。", path)
        options = dict(capture_output=True, text=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            root = subprocess.run(["git", "-C", str(self.project), "rev-parse", "--show-toplevel"], **options)
            if root.returncode:
                if "not a git repository" in root.stderr.lower():
                    return
                raise OSError()
            tracked = subprocess.run(["git", "-C", str(self.project), "ls-files", "--error-unmatch", "--", str(path)], **options)
            ignored = subprocess.run(["git", "-C", str(self.project), "check-ignore", "-q", "--", str(path)], **options)
            if tracked.returncode != 1 or ignored.returncode != 0:
                raise ConfigError("git_protection", "保存目标须未被 Git 跟踪且已忽略。", path)
        except (OSError, subprocess.SubprocessError):
            raise ConfigError("git_protection", "无法完成 Git 保存检查。", path) from None

    def update_preference(self):
        raw = read_file(self.home / ".env")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            raise ConfigError("unreadable", "个人更新设置无法读取。", self.home / ".env") from None
        value = ""
        for line in text.splitlines():
            match = ASSIGNMENT.match(line)
            if match and match[1] == UPDATE:
                value = match[2]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
        return value != "0", hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    def save_update_preference(self, revision, enabled):
        if type(enabled) is not bool:
            raise ConfigError("invalid_update", "自动检查更新须为布尔值。")
        path = self.home / ".env"
        if not self.home.exists():
            private_directory(self.home, owner_path=self.project)
        with file_lock(path.with_name(path.name + ".lock")):
            if self.update_preference()[1] != revision:
                raise ConfigError("conflict", "个人更新设置已被其他操作修改。", path)
            atomic_bytes(path, update_env(read_file(path), {UPDATE: "1" if enabled else "0"}), self.project)

    def save(self, revision, collection, *, default_id=None, active_ids=(), auto_update=None):
        collection = validate_collection(collection)
        current = self.snapshot()
        if current.revision != revision:
            raise ConfigError("conflict", "配置来源已变化；请重新载入后核对。", current.source)
        if current.target is None:
            raise ConfigError("source_readonly", "当前环境来源不可写，或个人配置未显式启用。", current.source)
        path = current.target
        self._check_target(path)
        if not path.parent.exists():
            private_directory(path.parent, owner_path=self.project)
        with file_lock(path.with_name(path.name + ".lock")):
            current = self.snapshot()
            if current.revision != revision or current.target != path:
                raise ConfigError("conflict", "配置被其他操作修改；草稿保留。", current.source)
            removed = set(current.collection["connections"]) - set(collection["connections"])
            if removed.intersection(active_ids):
                raise ConfigError("active_connection", "删除活动连接前须先断开并核对资源停止。", current.source)
            updates = {COLLECTION: json.dumps(collection, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
            if auto_update is not None:
                if path != self.home / ".env" or type(auto_update) is not bool:
                    raise ConfigError("separate_save", "更新开关固定写个人配置，请与项目连接配置分开保存。")
                updates[UPDATE] = "1" if auto_update else "0"
            local = parse_env(read_file(path), path)
            if default_id is not None:
                if default_id and default_id not in collection["connections"]:
                    raise ConfigError("invalid_selector", "默认连接不在本次配置集中。", path)
                if current.selector_source and current.selector_source != str(path):
                    raise ConfigError("selector_source", "默认项来自另一个来源，不能写低层值掩盖它。", current.selector_source)
                updates[SELECTOR] = default_id
            elif local.get(SELECTOR) in removed:
                updates[SELECTOR] = ""
            atomic_bytes(path, update_env(read_file(path), updates), self.project)
        return self.snapshot()


def missing_binding(record):
    missing = [key + "." + field for key, fields in (("hermes", ("home", "preferred_profile", "profile_home")),
               ("frp", ("project", "home", "source_binding"))) for field in fields if not record[key].get(field)]
    if record["auth"] is None:
        missing.append("auth")
    return missing
