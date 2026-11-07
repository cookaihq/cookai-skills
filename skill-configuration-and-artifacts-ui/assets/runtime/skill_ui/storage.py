"""Local-file helpers, for adapters that explicitly choose this storage protocol."""
import contextlib
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import subprocess

from .contracts import UIError


def _windows_owner(path):
    """Read the selected project's owner SID without depending on a shell/user-profile name."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                            ctypes.POINTER(pointer), pointer, pointer, pointer, ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    owner, descriptor, text = pointer(), pointer(), wintypes.LPWSTR()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    if result:
        raise OSError(result, "Cannot read selected path owner")
    try:
        if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, pointer))
        kernel.LocalFree(descriptor)


def _private_windows(path, owner_path):
    current = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip().split(",")[-1].strip('"')
    owners = {current, _windows_owner(owner_path)}
    permissions = "(OI)(CI)F" if path.is_dir() else "F"
    command = ["icacls", str(path), "/inheritance:r"]
    for sid in sorted(owners):
        command.extend(["/grant:r", f"*{sid}:{permissions}"])
    subprocess.run(command, check=True, capture_output=True, timeout=10)


def private_directory(path, owner_path=None):
    path = Path(path)
    if path.is_symlink():
        raise UIError("path", "运行目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        _private_windows(path, owner_path or path.parent)
    else:
        path.chmod(0o700)
    return path


@contextlib.contextmanager
def file_lock(path):
    """OS lock, released on process exit; coordinates only cooperating writers."""
    path = Path(path)
    if path.is_symlink():
        raise UIError("path", "锁路径不能是符号链接。")
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UIError("busy", "另一个实例正在操作，请稍后重试。", 409) from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, data, owner_path=None):
    path = Path(path)
    if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
        raise UIError("path", "保存目标必须是独立普通文件。")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            _private_windows(temporary, owner_path or path.parent)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def active(field, values):
    return all(values.get(key) == value for key, value in field.get("depends_on", {}).items())


def apply_changes(values, fields, changes):
    """Validate explicit edits. Loading/fallback semantics remain the store's job."""
    if not isinstance(changes, dict):
        raise UIError("changes", "变更格式无效。")
    definitions = {f["id"]: f for f in fields}
    result = dict(values)
    for name, change in changes.items():
        field = definitions.get(name)
        if not field or field.get("readonly"):
            raise UIError("readonly", "变更包含未知或只读字段。")
        if not isinstance(change, dict) or change.get("op") not in ("set", "unset"):
            raise UIError("changes", "变更格式无效。")
        if change["op"] == "unset":
            result.pop(name, None)
            continue
        value, kind = change.get("value"), field.get("type", "text")
        valid = {"text": isinstance(value, str), "select": value in field.get("choices", []),
                 "number": type(value) in (int, float) and math.isfinite(value), "boolean": type(value) is bool,
                 "list": isinstance(value, list) and all(isinstance(v, str) for v in value), "json": True}.get(kind, False)
        if not valid or (isinstance(value, str) and "••••••" in value):
            raise UIError("validation", field["label"] + "的值无效。", 422)
        result[name] = value
    return result


def validation_errors(fields, effective, changes):
    errors = []
    for field in fields:
        name = field["id"]
        if active(field, effective) and field.get("required") and effective[name] in (None, "", []):
            errors.append("请补齐：" + field["label"])
        if name in changes and not active(field, effective) and changes[name]["op"] == "set":
            errors.append(field["label"] + "当前不适用，请先核对关联设置。")
    return errors


class JsonConfig:
    """Single JSON-file opt-in reference store; never a replacement for an existing loader.

    Preserves unknown keys. HMAC revision detects changes without exposing low-entropy secrets.
    Dependencies and required checks run on candidate effective values before atomic replace.
    """

    def __init__(self, path, fields, owner_path=None):
        self.path, self.fields, self.key = Path(path), fields, secrets.token_bytes(32)
        self.owner_path = Path(owner_path) if owner_path else self.path.parent

    def _read(self):
        if self.path.is_symlink():
            raise UIError("path", "配置目标不能是符号链接。")
        raw = self.path.read_bytes() if self.path.exists() else b""
        try:
            values = json.loads(raw) if raw else {}
            if not isinstance(values, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise UIError("config", "配置文件格式无效，请先修复文件。", 422) from None
        return values, hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    def _snapshot(self, values, revision, changes=None):
        effective = {f["id"]: values.get(f["id"], f.get("default")) for f in self.fields}
        fields = []
        for f in self.fields:
            present, value = f["id"] in values, effective[f["id"]]
            status = "set" if present else "default" if "default" in f else "missing" if f.get("required") else "optional"
            fields.append(dict(f, active=active(f, effective), targets=[] if f.get("readonly") else ["project"],
                               effective={"value": value, "source": "project" if present else "default" if "default" in f else None, "status": status},
                               rows=[{"layer": "project", "present": present, "value": values.get(f["id"]), "status": "effective" if present else "missing"}]))
        return {"revision": revision, "validation": "本地格式与必需项校验",
                "default_targets": {"project": "project"},
                "layers": [{"id": "project", "name": self.path.name, "path": str(self.path), "scope": "project",
                            "scope_label": "仅此项目", "readonly": False, "enabled": True}],
                "fields": fields, "validation_errors": validation_errors(self.fields, effective, changes or {})}

    def snapshot(self):
        return self._snapshot(*self._read())

    def _candidate(self, changes, revision, target):
        if target != "project":
            raise UIError("target", "请选择有效的保存文件。")
        values, current = self._read()
        if current != revision:
            raise UIError("conflict", "配置已被其他操作修改。草稿仍保留，请重新载入后核对。", 409)
        values = apply_changes(values, self.fields, changes)
        return values, self._snapshot(values, current, changes)

    def preview(self, changes, revision, target):
        return self._candidate(changes, revision, target)[1]

    def save(self, changes, revision, target):
        if not isinstance(changes, dict) or not changes:
            raise UIError("changes", "没有待保存的修改。")
        # Existing business directories retain their ACL; only new directories/files are tightened.
        if not self.path.parent.exists():
            private_directory(self.path.parent, owner_path=self.owner_path if self.owner_path.exists() else None)
        with file_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            values, candidate = self._candidate(changes, revision, target)
            if candidate["validation_errors"]:
                raise UIError("validation", "；".join(candidate["validation_errors"]), 422)
            atomic_json(self.path, values, owner_path=self.owner_path)
        return self.snapshot()
