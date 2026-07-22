from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import stat
import subprocess
import errno
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from dotenv_parser import DotenvError, parse_dotenv
from resolver import (
    GLOBAL_CREDENTIALS,
    PROJECT_CREDENTIALS,
    ResolutionError,
    resolve_target,
)
from safe_io import FileSecurityError, open_directory, read_regular_file
from strict_json import canonicalize, loads
from v2_schema import (
    NAME_RE,
    CredentialProfile,
    SchemaError,
    ScopedReference,
    UploadTarget,
    parse_credential,
    parse_credential_map,
    parse_project_config,
    parse_reference,
    parse_target,
)


class InstallError(ValueError):
    pass


class ConfigurationConflict(InstallError):
    pass


class ConfigurationChanged(InstallError):
    pass


class NewCredentialNameRequired(ConfigurationConflict):
    pass


class SimulatedCrash(BaseException):
    pass


@dataclass(frozen=True)
class SelectorChange:
    kind: str
    caller_skill: Optional[str]
    before: Optional[str]
    after: str


@dataclass(frozen=True)
class InstallSpec:
    project_root: str
    config_home: str
    target_ref: str
    target: Mapping[str, Any]
    credential_ref: str
    credential: Mapping[str, Any] = field(repr=False)
    selector_change: Optional[SelectorChange] = None
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    credential_mode: str = "persistent"
    approved_replacements: frozenset = field(default_factory=frozenset)
    acknowledge_global_target_impact: bool = False
    install_local_exclude: bool = False
    process_shadow: str = "reject"
    allow_candidates: bool = False
    git_binary: str = "git"


@dataclass(frozen=True)
class RecordAnalysis:
    disposition: str
    dependencies: Tuple[str, ...] = ()
    replacement_allowed: bool = True
    warning: Optional[str] = None


@dataclass(frozen=True)
class InstallAnalysis:
    credential: RecordAnalysis
    target: RecordAnalysis
    selector: RecordAnalysis


@dataclass(frozen=True)
class GitSecretVerdict:
    applicable: bool
    repository_root: Optional[str]
    relative_path: Optional[str]
    tracked: Optional[bool]
    ignored: Optional[bool]
    exclude_path: Optional[str]
    anchored_rule: Optional[str]
    install_rule: bool = False


@dataclass(frozen=True)
class FileSnapshot:
    state: str
    secret: bool
    device: Optional[int] = None
    inode: Optional[int] = None
    owner: Optional[int] = None
    mode: Optional[int] = None
    size: Optional[int] = None
    mtime_ns: Optional[int] = None
    ctime_ns: Optional[int] = None
    digest: Optional[str] = None
    content: Optional[bytes] = field(default=None, repr=False, compare=False)

    def public_record(self) -> Dict[str, Any]:
        if self.state == "absent":
            return {"state": "absent"}
        identity = {
            "device": str(self.device),
            "inode": str(self.inode),
            "owner": str(self.owner),
            "mode": f"{self.mode:04o}",
            "size": str(self.size),
            "mtime_ns": str(self.mtime_ns),
            "ctime_ns": str(self.ctime_ns),
        }
        if self.secret:
            return {
                "state": "present",
                "version_token": {"owned": True, "type": "regular", **identity},
            }
        return {"state": "present", "identity": identity, "sha256": "sha256:" + str(self.digest)}

    def matches(self, other: "FileSnapshot") -> bool:
        if self.state != other.state or self.secret != other.secret:
            return False
        if self.state == "absent":
            return True
        fields = (
            "device", "inode", "owner", "mode", "size", "mtime_ns", "ctime_ns",
        )
        if any(getattr(self, name) != getattr(other, name) for name in fields):
            return False
        return True if self.secret else self.digest == other.digest


@dataclass(frozen=True)
class PlannedFile:
    role: str
    path: str
    secret: bool
    exact_mode: Optional[int]
    desired_mode: int
    snapshot: FileSnapshot


@dataclass(frozen=True)
class InstallPlan:
    spec: InstallSpec
    analysis: InstallAnalysis
    target_ref: ScopedReference
    credential_ref: ScopedReference
    target_record: Dict[str, Any]
    credential_record: Dict[str, Any] = field(repr=False)
    git_verdict: GitSecretVerdict
    files: Tuple[PlannedFile, ...]
    lock_paths: Tuple[str, ...]


@dataclass(frozen=True)
class InstallResult:
    status: str
    stages: Tuple[str, ...]
    environ: Dict[str, str] = field(repr=False)
    resolver_verified: bool = True
    recovery: Tuple[str, ...] = ()


class LockedInstallSession:
    def __init__(self, plan: InstallPlan) -> None:
        self._plan = plan
        self._active = True
        self._applied = False

    def __repr__(self) -> str:
        return (
            "LockedInstallSession("
            f"active={self._active}, applied={self._applied})"
        )

    def close(self) -> None:
        self._active = False

    def apply(
        self, *, credential: Optional[Mapping[str, Any]] = None, fault=None,
    ) -> InstallResult:
        if not self._active or self._applied:
            raise InstallError("locked installation session is unavailable")
        plan = self._plan
        if credential is not None:
            plan = bind_install_plan_credential(plan, credential)
        self._applied = True
        return _apply_install_plan_locked(plan, fault=fault)


def _target_record(target: UploadTarget) -> Dict[str, Any]:
    cors = None if target.setup.cors is None else dict(target.setup.cors)
    return {
        "schema_version": 1,
        "credential": target.credential.text,
        "provider": target.provider,
        "region": target.region,
        "endpoint": target.endpoint,
        "addressing": target.addressing,
        "bucket": target.bucket,
        "prefix": target.prefix,
        "access": {
            "mode": target.access.mode,
            "public_base_url": target.access.public_base_url,
            "presign_expires_seconds": target.access.presign_expires_seconds,
        },
        "retention": {"mode": target.retention.mode, "days": target.retention.days},
        "collision": target.collision,
        "object_headers": {
            "cache_control": target.object_headers.cache_control,
            "content_disposition": target.object_headers.content_disposition,
        },
        "limits": {
            "soft_max_bytes": target.limits.soft_max_bytes,
            "multipart_threshold_bytes": target.limits.multipart_threshold_bytes,
            "part_size_bytes": target.limits.part_size_bytes,
        },
        "retry": {
            "part_max_attempts": target.retry.part_max_attempts,
            "collision_max_attempts": target.retry.collision_max_attempts,
        },
        "setup": {
            "exclusive_prefix": target.setup.exclusive_prefix,
            "integration_test": target.setup.integration_test,
            "cors": cors,
        },
    }


def _credential_record(value: Mapping[str, Any], parsed: CredentialProfile) -> Dict[str, Any]:
    return {
        "access_key_id": parsed.access_key_id,
        "secret_access_key": parsed.secret_access_key,
        "session_token": parsed.session_token,
        "expires_at": value["expires_at"],
    }


def _validate_spec(spec: InstallSpec):
    try:
        target_ref = parse_reference(spec.target_ref, "Target reference")
        credential_ref = parse_reference(spec.credential_ref, "Credential reference")
        if target_ref.scope != credential_ref.scope:
            raise InstallError("Target and Credential Profile must use the same scope")
        target = parse_target(
            dict(spec.target), expected_scope=target_ref.scope,
            allow_candidates=spec.allow_candidates,
        )
        if target.credential != credential_ref:
            raise InstallError("proposed Target must reference the selected Credential Profile")
        credential = parse_credential(dict(spec.credential))
    except SchemaError as exc:
        raise InstallError(str(exc)) from exc
    remaining = credential.remaining_seconds(spec.now)
    if remaining is not None and remaining <= 60:
        raise InstallError("temporary Credential Profile must have more than 60 whole seconds remaining")
    if spec.credential_mode not in {"persistent", "process-memory"}:
        raise InstallError("credential_mode must be persistent or process-memory")
    if spec.process_shadow not in {"reject", "clear"}:
        raise InstallError("process_shadow must be reject or clear")
    selector = spec.selector_change
    if selector is not None:
        if selector.kind not in {"project-default", "skill-target"}:
            raise InstallError("invalid selector kind")
        if selector.kind == "project-default" and selector.caller_skill is not None:
            raise InstallError("project-default selector must not have a caller Skill")
        if selector.kind == "skill-target" and (
            selector.caller_skill is None or not NAME_RE.fullmatch(selector.caller_skill)
        ):
            raise InstallError("skill-target selector requires a valid caller Skill")
        if selector.after != target_ref.text:
            raise InstallError("selector after value must equal the proposed Target reference")
        if selector.before is not None:
            parse_reference(selector.before, "selector before reference")
    return target_ref, credential_ref, target, credential


def _read_optional(path: Path, *, secret: bool) -> Optional[str]:
    try:
        return read_regular_file(str(path), max_bytes=1048576, secret=secret, missing_ok=True)
    except (FileSecurityError, OSError) as exc:
        raise InstallError(f"unsafe configuration path: {path.name}") from exc


def _capture_snapshot(path: Path, *, secret: bool, exact_mode: Optional[int] = None) -> FileSnapshot:
    absolute = path.expanduser().absolute()
    if not absolute.parent.exists():
        return FileSnapshot("absent", secret)
    try:
        parent_fd = open_directory(str(absolute.parent))
    except FileNotFoundError:
        return FileSnapshot("absent", secret)
    except (FileSecurityError, OSError) as exc:
        raise InstallError(f"unsafe configuration parent: {absolute.parent.name}") from exc
    descriptor = None
    try:
        try:
            descriptor = os.open(
                absolute.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return FileSnapshot("absent", secret)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise InstallError("configuration file is a symlink or unsafe") from exc
            raise
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise InstallError("configuration file must be an owned regular file")
        if secret and mode != 0o600:
            raise InstallError("Secret configuration permissions must be 0600")
        if exact_mode is not None and mode != exact_mode:
            raise InstallError(f"configuration permissions must be {exact_mode:04o}")
        if not secret and mode & 0o022:
            raise InstallError("configuration file must not be group/world-writable")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 1048576:
                raise InstallError("configuration file is too large")
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(before, name) != getattr(after, name) for name in identity_fields):
            raise ConfigurationChanged("configuration changed while being snapshotted")
        content = b"".join(chunks)
        return FileSnapshot(
            "present", secret, after.st_dev, after.st_ino, after.st_uid, mode,
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
            None if secret else hashlib.sha256(content).hexdigest(), content,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _dotenv(text: Optional[str], variable: str) -> Dict[str, str]:
    try:
        return parse_dotenv(
            text, allowed_keys={variable}, label="credential dotenv",
        )
    except DotenvError as exc:
        raise InstallError(str(exc)) from exc


def _existing_credential(plan_scope: str, name: str, spec: InstallSpec):
    variable = PROJECT_CREDENTIALS if plan_scope == "project" else GLOBAL_CREDENTIALS
    if spec.credential_mode == "process-memory":
        source = spec.environ.get(variable, "")
    else:
        path = Path(spec.project_root) / ".env.local" if plan_scope == "project" else Path(spec.config_home) / ".env"
        values = _dotenv(_read_optional(path, secret=True), variable)
        source = values.get(variable, "")
    if not source:
        return None
    try:
        raw = loads(source)
        parsed = parse_credential_map(raw)
    except (ValueError, SchemaError) as exc:
        raise InstallError("selected Credential map is malformed") from exc
    if name not in parsed:
        return None
    value = raw[name]
    return _credential_record(value, parsed[name])


def _existing_target(ref: ScopedReference, spec: InstallSpec) -> Optional[Dict[str, Any]]:
    if ref.scope == "project":
        path = Path(spec.project_root) / ".s3-upload" / "targets" / (ref.name + ".json")
        secret = False
    else:
        path = Path(spec.config_home) / "targets" / (ref.name + ".json")
        secret = True
    text = _read_optional(path, secret=secret)
    if text is None:
        return None
    try:
        value = loads(text)
        parsed = parse_target(value, expected_scope=ref.scope, allow_candidates=spec.allow_candidates)
    except (ValueError, SchemaError) as exc:
        raise InstallError("selected Upload Target is malformed") from exc
    return _target_record(parsed)


def _existing_selector(spec: InstallSpec) -> Optional[str]:
    selector = spec.selector_change
    if selector is None:
        return None
    path = Path(spec.project_root) / ".s3-upload" / "config.json"
    text = _read_optional(path, secret=False)
    if text is None:
        return None
    try:
        default, mappings = parse_project_config(loads(text))
    except (ValueError, SchemaError) as exc:
        raise InstallError("project selector configuration is malformed") from exc
    if selector.kind == "project-default":
        return None if default is None else default.text
    selected = mappings.get(selector.caller_skill or "")
    return None if selected is None else selected.text


def _credential_dependents(scope: str, credential_name: str, spec: InstallSpec) -> Tuple[Tuple[str, ...], bool]:
    if scope == "project":
        directory = Path(spec.project_root) / ".s3-upload" / "targets"
        secret = False
    else:
        directory = Path(spec.config_home) / "targets"
        secret = True
    if not directory.exists():
        return (), True
    if directory.is_symlink() or not directory.is_dir():
        return (), False
    dependents = []
    try:
        entries = sorted(os.scandir(str(directory)), key=lambda entry: entry.name)
    except OSError:
        return (), False
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            return tuple(dependents), False
        try:
            text = _read_optional(Path(entry.path), secret=secret)
            parsed = parse_target(
                loads(text or ""), expected_scope=scope,
                allow_candidates=spec.allow_candidates,
            )
        except (InstallError, ValueError, SchemaError):
            return tuple(dependents), False
        if parsed.credential.name == credential_name:
            dependents.append(f"{scope}:{entry.name[:-5]}")
    return tuple(dependents), True


def _target_dependents(ref: ScopedReference, spec: InstallSpec) -> Tuple[Tuple[str, ...], bool, Optional[str]]:
    if ref.scope == "global":
        return (), True, "Global Target consumers in other projects cannot be enumerated"
    path = Path(spec.project_root) / ".s3-upload" / "config.json"
    text = _read_optional(path, secret=False)
    if text is None:
        return (), True, None
    try:
        default, mappings = parse_project_config(loads(text))
    except (ValueError, SchemaError):
        return (), False, "project selector dependents cannot be enumerated safely"
    dependents = []
    if default == ref:
        dependents.append("project-default")
    for caller, selected in sorted(mappings.items()):
        if selected == ref:
            dependents.append("skill-target:" + caller)
    return tuple(dependents), True, None


def analyze_configuration(spec: InstallSpec) -> InstallAnalysis:
    target_ref, credential_ref, target, credential = _validate_spec(spec)
    normalized_target = _target_record(target)
    normalized_credential = _credential_record(spec.credential, credential)
    current_credential = _existing_credential(credential_ref.scope, credential_ref.name, spec)
    current_target = _existing_target(target_ref, spec)
    current_selector = _existing_selector(spec)
    credential_disposition = (
        "create" if current_credential is None
        else "idempotent" if current_credential == normalized_credential
        else "conflict"
    )
    target_disposition = (
        "create" if current_target is None
        else "idempotent" if current_target == normalized_target
        else "conflict"
    )
    selector_disposition = "idempotent"
    if spec.selector_change is not None:
        selector_disposition = (
            "idempotent" if current_selector == spec.selector_change.after
            else "create" if current_selector is None
            else "conflict"
        )
    credential_dependencies: Tuple[str, ...] = ()
    credential_replace_allowed = True
    credential_warning = None
    if credential_disposition == "conflict":
        credential_dependencies, credential_replace_allowed = _credential_dependents(
            credential_ref.scope, credential_ref.name, spec,
        )
        if not credential_replace_allowed:
            credential_warning = "Credential dependents cannot be enumerated safely; use a new name"
    target_dependencies: Tuple[str, ...] = ()
    target_replace_allowed = True
    target_warning = None
    if target_disposition == "conflict":
        target_dependencies, target_replace_allowed, target_warning = _target_dependents(target_ref, spec)
    return InstallAnalysis(
        credential=RecordAnalysis(
            credential_disposition, credential_dependencies,
            credential_replace_allowed, credential_warning,
        ),
        target=RecordAnalysis(
            target_disposition, target_dependencies,
            target_replace_allowed, target_warning,
        ),
        selector=RecordAnalysis(selector_disposition),
    )


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            raise InstallError("no existing parent for configuration path")
        current = current.parent
    if not current.is_dir() or current.is_symlink():
        raise InstallError("configuration parent is unsafe")
    return current


def _git(spec: InstallSpec, cwd: Path, arguments, *, allow_failure: bool = False):
    try:
        result = subprocess.run(
            [spec.git_binary, "-C", str(cwd), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise InstallError("Git is unavailable for Secret persistence preflight") from exc
    if not allow_failure and result.returncode != 0:
        raise InstallError("Git Secret persistence check was inconclusive")
    return result


def _literal_gitignore_rule(relative_path: str) -> str:
    escaped = []
    for character in relative_path.replace(os.sep, "/"):
        if character in {"\\", "*", "?", "[", "]", " "}:
            escaped.append("\\")
        escaped.append(character)
    return "/" + "".join(escaped)


def _git_secret_verdict(path: Path, spec: InstallSpec, *, permit_rule: bool) -> GitSecretVerdict:
    cwd = _nearest_existing_directory(path.parent)
    probe = _git(spec, cwd, ["rev-parse", "--show-toplevel"], allow_failure=True)
    if probe.returncode != 0:
        return GitSecretVerdict(False, None, None, None, None, None, None)
    root = Path(probe.stdout.strip()).absolute()
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise InstallError("Git worktree did not contain the Secret destination") from exc
    tracked_result = _git(
        spec, root, ["ls-files", "--error-unmatch", "--", relative],
        allow_failure=True,
    )
    if tracked_result.returncode not in {0, 1}:
        raise InstallError("Git tracked-state check was inconclusive")
    tracked = tracked_result.returncode == 0
    ignore_result = _git(
        spec, root, ["check-ignore", "--no-index", "-q", "--", relative],
        allow_failure=True,
    )
    if ignore_result.returncode not in {0, 1}:
        raise InstallError("Git ignore-state check was inconclusive")
    ignored = ignore_result.returncode == 0
    if tracked:
        raise InstallError("Secret destination is tracked by Git")
    exclude_result = _git(spec, root, ["rev-parse", "--git-path", "info/exclude"])
    exclude = Path(exclude_result.stdout.strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    rule = _literal_gitignore_rule(relative)
    if not ignored and not permit_rule:
        raise InstallError("Secret destination must be effectively ignored by Git")
    return GitSecretVerdict(
        True, str(root), relative, False, ignored, str(exclude.absolute()),
        rule, install_rule=not ignored,
    )


def _planned_files(
    spec: InstallSpec, target_ref: ScopedReference, credential_ref: ScopedReference,
) -> Tuple[PlannedFile, ...]:
    files = []
    if spec.credential_mode == "persistent":
        credential_path = (
            Path(spec.project_root) / ".env.local"
            if credential_ref.scope == "project"
            else Path(spec.config_home) / ".env"
        )
        files.append(PlannedFile(
            "credential", str(credential_path.absolute()), True, 0o600, 0o600,
            _capture_snapshot(credential_path, secret=True, exact_mode=0o600),
        ))
    target_path = (
        Path(spec.project_root) / ".s3-upload" / "targets" / (target_ref.name + ".json")
        if target_ref.scope == "project"
        else Path(spec.config_home) / "targets" / (target_ref.name + ".json")
    )
    exact_target_mode = 0o600 if target_ref.scope == "global" else None
    files.append(PlannedFile(
        "target", str(target_path.absolute()), False, exact_target_mode, 0o600,
        _capture_snapshot(target_path, secret=False, exact_mode=exact_target_mode),
    ))
    if spec.selector_change is not None:
        selector_path = Path(spec.project_root) / ".s3-upload" / "config.json"
        files.append(PlannedFile(
            "selector", str(selector_path.absolute()), False, None, 0o600,
            _capture_snapshot(selector_path, secret=False),
        ))
    return tuple(files)


def _configuration_paths(
    spec: InstallSpec, target_ref: ScopedReference, credential_ref: ScopedReference,
) -> Tuple[Path, ...]:
    paths = []
    if spec.credential_mode == "persistent":
        paths.append(
            Path(spec.project_root) / ".env.local"
            if credential_ref.scope == "project"
            else Path(spec.config_home) / ".env"
        )
    paths.append(
        Path(spec.project_root) / ".s3-upload" / "targets" / (target_ref.name + ".json")
        if target_ref.scope == "project"
        else Path(spec.config_home) / "targets" / (target_ref.name + ".json")
    )
    if spec.selector_change is not None:
        paths.append(Path(spec.project_root) / ".s3-upload" / "config.json")
    return tuple(paths)


def _preflight_lock_paths(paths: Tuple[Path, ...]) -> Tuple[str, ...]:
    return tuple(sorted({
        str(_nearest_existing_directory(path.parent).absolute()) for path in paths
    }))


def _lock_paths(files: Tuple[PlannedFile, ...]) -> Tuple[str, ...]:
    return tuple(sorted({
        str(_nearest_existing_directory(Path(planned.path).parent).absolute())
        for planned in files
    }))


@contextmanager
def _hold_locks(paths: Tuple[str, ...]):
    descriptors = []
    try:
        for path in paths:
            try:
                descriptor = open_directory(path)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (FileSecurityError, OSError) as exc:
                raise InstallError("configuration lock path is unsafe or unavailable") from exc
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _assert_plan_cas(plan: InstallPlan) -> None:
    for planned in plan.files:
        current = _capture_snapshot(
            Path(planned.path), secret=planned.secret, exact_mode=planned.exact_mode,
        )
        if not planned.snapshot.matches(current):
            raise ConfigurationChanged(f"{planned.role} configuration changed after preflight")


def _fault(fault, boundary: str) -> None:
    if fault is not None:
        fault(boundary)


def _fsync_parent(path: Path) -> None:
    descriptor = open_directory(str(path.parent))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_staged(plan: InstallPlan, intended: Dict[str, bytes]) -> bool:
    planned_by_role = {entry.role: entry for entry in plan.files}
    complete = True
    try:
        for role in reversed(tuple(intended)):
            planned = planned_by_role.get(role)
            if planned is None:
                continue
            path = Path(planned.path)
            current = _capture_snapshot(
                path, secret=planned.secret, exact_mode=planned.exact_mode,
            )
            if current.state != "present" or current.content != intended[role]:
                complete = False
                continue
            if planned.snapshot.state == "absent":
                path.unlink()
                _fsync_parent(path)
            else:
                if planned.snapshot.content is None or planned.snapshot.mode is None:
                    complete = False
                    continue
                _atomic_write(path, planned.snapshot.content, planned.snapshot.mode)
        return complete
    except (InstallError, OSError):
        return False


def preflight_installation(spec: InstallSpec) -> InstallPlan:
    target_ref, credential_ref, target, credential = _validate_spec(spec)
    project = Path(spec.project_root).expanduser().absolute()
    if not project.is_dir() or project.is_symlink():
        raise InstallError("project root must be an existing real directory")
    normalized_target = _target_record(target)
    normalized_credential = _credential_record(spec.credential, credential)
    process_variable = PROJECT_CREDENTIALS if credential_ref.scope == "project" else GLOBAL_CREDENTIALS
    if (
        spec.credential_mode == "persistent"
        and spec.environ.get(process_variable, "")
        and spec.process_shadow == "reject"
    ):
        raise InstallError("process Credential map would shadow the persistent destination")
    preflight_locks = _preflight_lock_paths(
        _configuration_paths(spec, target_ref, credential_ref),
    )
    with _hold_locks(preflight_locks):
        analysis = analyze_configuration(spec)
        if (
            analysis.target.disposition == "conflict"
            and analysis.credential.disposition == "conflict"
        ):
            raise NewCredentialNameRequired(
                "a changed Target and linked Credential Profile require a new credential name"
            )
        if (
            spec.selector_change is not None
            and analysis.selector.disposition != "idempotent"
            and _existing_selector(spec) != spec.selector_change.before
        ):
            raise ConfigurationChanged("selector value changed from its confirmed before value")
        for record_name, record in (
            ("credential", analysis.credential),
            ("target", analysis.target),
            ("selector", analysis.selector),
        ):
            if record.disposition != "conflict":
                continue
            if not record.replacement_allowed:
                raise ConfigurationConflict(f"{record_name} replacement is unsafe; choose a new name")
            if record_name not in spec.approved_replacements:
                raise ConfigurationConflict(f"{record_name} configuration conflict requires approval")
            if (
                record_name == "target" and target_ref.scope == "global"
                and not spec.acknowledge_global_target_impact
            ):
                raise ConfigurationConflict("global Target replacement requires unknown-impact acknowledgement")
        git_verdict = GitSecretVerdict(False, None, None, None, None, None, None)
        if spec.credential_mode == "persistent" and analysis.credential.disposition != "idempotent":
            credential_path = (
                Path(spec.project_root) / ".env.local"
                if credential_ref.scope == "project"
                else Path(spec.config_home) / ".env"
            )
            git_verdict = _git_secret_verdict(
                credential_path, spec, permit_rule=spec.install_local_exclude,
            )
        file_list = list(_planned_files(spec, target_ref, credential_ref))
        if git_verdict.install_rule and git_verdict.exclude_path is not None:
            exclude_path = Path(git_verdict.exclude_path)
            exclude_snapshot = _capture_snapshot(exclude_path, secret=False)
            file_list.append(PlannedFile(
                "git-exclude", str(exclude_path.absolute()), False, None,
                exclude_snapshot.mode or 0o644, exclude_snapshot,
            ))
        files = tuple(file_list)
        if analyze_configuration(spec) != analysis:
            raise ConfigurationChanged("configuration changed while preflight snapshots were captured")
    return InstallPlan(
        spec=spec,
        analysis=analysis,
        target_ref=target_ref,
        credential_ref=credential_ref,
        target_record=normalized_target,
        credential_record=normalized_credential,
        git_verdict=git_verdict,
        files=files,
        lock_paths=_lock_paths(files),
    )


def _ensure_directory(path: Path) -> None:
    absolute = path.expanduser().absolute()
    components = [part for part in absolute.parts if part != os.sep]
    descriptor = os.open(
        os.sep,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for index, component in enumerate(components):
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise InstallError("configuration parent contains a non-directory component")
            if index == len(components) - 1:
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                    os.close(child)
                    raise InstallError("configuration parent must be owned and not group/world-writable")
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise InstallError("configuration parent contains a symlink or unsafe component") from exc
        raise
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, mode: int, *, no_replace: bool = False) -> None:
    _ensure_directory(path.parent)
    parent_fd = open_directory(str(path.parent))
    temporary = "." + path.name + "." + secrets.token_hex(8)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
        try:
            if no_replace:
                try:
                    os.link(
                        temporary, path.name,
                        src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise ConfigurationChanged(f"{path.name} appeared after preflight") from exc
                os.unlink(temporary, dir_fd=parent_fd)
            else:
                os.replace(
                    temporary, path.name,
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                )
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)


def _planned_file(plan: InstallPlan, role: str) -> Optional[PlannedFile]:
    return next((entry for entry in plan.files if entry.role == role), None)


def _credential_path(plan: InstallPlan) -> Path:
    if plan.credential_ref.scope == "project":
        return Path(plan.spec.project_root) / ".env.local"
    return Path(plan.spec.config_home) / ".env"


def _target_path(plan: InstallPlan) -> Path:
    if plan.target_ref.scope == "project":
        return Path(plan.spec.project_root) / ".s3-upload" / "targets" / (plan.target_ref.name + ".json")
    return Path(plan.spec.config_home) / "targets" / (plan.target_ref.name + ".json")


def _selector_path(plan: InstallPlan) -> Path:
    return Path(plan.spec.project_root) / ".s3-upload" / "config.json"


def _credential_dotenv(plan: InstallPlan) -> bytes:
    variable = PROJECT_CREDENTIALS if plan.credential_ref.scope == "project" else GLOBAL_CREDENTIALS
    path = _credential_path(plan)
    original = _read_optional(path, secret=True) or ""
    values = _dotenv(original, variable)
    source = values.get(variable, "")
    if source:
        try:
            raw_mapping = loads(source)
            parse_credential_map(raw_mapping)
        except (ValueError, SchemaError) as exc:
            raise InstallError("selected Credential map is malformed") from exc
        mapping = dict(raw_mapping)
    else:
        mapping = {}
    mapping[plan.credential_ref.name] = plan.credential_record
    retained = []
    for raw in original.splitlines(keepends=True):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == variable:
                continue
        retained.append(raw)
    text = "".join(retained)
    if text and not text.endswith("\n"):
        text += "\n"
    text += variable + "=" + canonicalize(mapping).decode("utf-8") + "\n"
    return text.encode("utf-8")


def _install_git_exclude(plan: InstallPlan) -> None:
    verdict = plan.git_verdict
    if not verdict.install_rule:
        return
    if verdict.exclude_path is None or verdict.anchored_rule is None:
        raise ConfigurationChanged("missing planned Git exclude rule")
    path = Path(verdict.exclude_path)
    current = _read_optional(path, secret=False) or ""
    lines = current.splitlines()
    if verdict.anchored_rule not in lines:
        if current and not current.endswith("\n"):
            current += "\n"
        current += verdict.anchored_rule + "\n"
        planned = _planned_file(plan, "git-exclude")
        _atomic_write(
            path, current.encode("utf-8"), planned.desired_mode if planned else 0o644,
            no_replace=bool(planned and planned.snapshot.state == "absent"),
        )
    final = _git_secret_verdict(_credential_path(plan), plan.spec, permit_rule=False)
    if final.tracked or not final.ignored:
        raise ConfigurationChanged("Git Secret persistence precondition changed")


def _selector_record(plan: InstallPlan) -> Dict[str, Any]:
    selector = plan.spec.selector_change
    path = _selector_path(plan)
    current = _read_optional(path, secret=False)
    if current is None:
        default = None
        mappings: Dict[str, ScopedReference] = {}
    else:
        try:
            default, mappings = parse_project_config(loads(current))
        except (ValueError, SchemaError) as exc:
            raise ConfigurationChanged("project selector configuration changed") from exc
    default_text = None if default is None else default.text
    mapping_text = {caller: reference.text for caller, reference in mappings.items()}
    if selector is None:
        return {"schema_version": 1, "default_target": default_text, "skill_targets": mapping_text}
    if selector.kind == "project-default":
        default_text = selector.after
    else:
        mapping_text[selector.caller_skill or ""] = selector.after
    return {
        "schema_version": 1,
        "default_target": default_text,
        "skill_targets": mapping_text,
    }


def bind_install_plan_credential(
    plan: InstallPlan, credential: Mapping[str, Any],
) -> InstallPlan:
    if plan.analysis.credential.disposition != "create":
        raise InstallError("one-time Credential can only bind an absent slot")
    spec = replace(plan.spec, credential=dict(credential))
    _, credential_ref, _, parsed = _validate_spec(spec)
    if credential_ref != plan.credential_ref:
        raise InstallError("bound Credential reference changed")
    return replace(
        plan,
        spec=spec,
        credential_record=_credential_record(dict(credential), parsed),
    )


@contextmanager
def locked_install_session(plan: InstallPlan):
    with _hold_locks(plan.lock_paths):
        _assert_plan_cas(plan)
        _install_git_exclude(plan)
        if (
            plan.git_verdict.applicable
            and plan.spec.credential_mode == "persistent"
            and plan.analysis.credential.disposition != "idempotent"
        ):
            final_git = _git_secret_verdict(
                _credential_path(plan), plan.spec, permit_rule=False,
            )
            if final_git.tracked or not final_git.ignored:
                raise ConfigurationChanged(
                    "Git Secret persistence precondition changed",
                )
        session = LockedInstallSession(plan)
        try:
            yield session
        finally:
            session.close()


def _apply_install_plan_locked(plan: InstallPlan, *, fault=None) -> InstallResult:
    stages = []
    intended: Dict[str, bytes] = {}
    environment = dict(plan.spec.environ)
    if plan.spec.credential_mode == "persistent" and plan.spec.process_shadow == "clear":
        shadow_variable = PROJECT_CREDENTIALS if plan.credential_ref.scope == "project" else GLOBAL_CREDENTIALS
        environment.pop(shadow_variable, None)
    try:
        if plan.analysis.credential.disposition != "idempotent" and plan.spec.credential_mode == "persistent":
            credential_bytes = _credential_dotenv(plan)
            intended["credential"] = credential_bytes
            _fault(fault, "before-credential")
            credential_file = _planned_file(plan, "credential")
            _atomic_write(
                _credential_path(plan), credential_bytes, 0o600,
                no_replace=bool(credential_file and credential_file.snapshot.state == "absent"),
            )
            stages.append("credential")
            _fault(fault, "after-credential")
        elif plan.analysis.credential.disposition != "idempotent":
            variable = PROJECT_CREDENTIALS if plan.credential_ref.scope == "project" else GLOBAL_CREDENTIALS
            source = environment.get(variable, "")
            if source:
                try:
                    process_mapping = loads(source)
                    parse_credential_map(process_mapping)
                except (ValueError, SchemaError) as exc:
                    raise ConfigurationChanged("process Credential map changed or became invalid") from exc
                merged_process_mapping = dict(process_mapping)
            else:
                merged_process_mapping = {}
            merged_process_mapping[plan.credential_ref.name] = plan.credential_record
            environment[variable] = canonicalize(merged_process_mapping).decode("utf-8")
            stages.append("credential")
            _fault(fault, "after-credential")
        if plan.analysis.target.disposition != "idempotent":
            target_bytes = canonicalize(plan.target_record)
            intended["target"] = target_bytes
            target_mode = 0o600 if plan.target_ref.scope == "global" else 0o600
            _fault(fault, "before-target")
            target_file = _planned_file(plan, "target")
            _atomic_write(
                _target_path(plan), target_bytes, target_mode,
                no_replace=bool(target_file and target_file.snapshot.state == "absent"),
            )
            stages.append("target")
            _fault(fault, "after-target")
        if plan.spec.selector_change is not None and plan.analysis.selector.disposition != "idempotent":
            selector_bytes = canonicalize(_selector_record(plan))
            intended["selector"] = selector_bytes
            _fault(fault, "before-selector")
            selector_file = _planned_file(plan, "selector")
            _atomic_write(
                _selector_path(plan), selector_bytes, 0o600,
                no_replace=bool(selector_file and selector_file.snapshot.state == "absent"),
            )
            stages.append("selector")
            _fault(fault, "after-selector")
        resolved = resolve_target(
            cwd=plan.spec.project_root,
            config_home=plan.spec.config_home,
            environ=environment,
            cli_target=None if plan.spec.selector_change is not None else plan.target_ref.text,
            cli_caller=(
                plan.spec.selector_change.caller_skill
                if plan.spec.selector_change is not None and plan.spec.selector_change.kind == "skill-target"
                else None
            ),
            use_local_key=True,
            now=plan.spec.now,
            allow_candidates=plan.spec.allow_candidates,
        )
        if resolved.ref != plan.target_ref or resolved.credential is None:
            raise InstallError("offline resolver did not select the proposed configuration graph")
    except Exception as exc:
        if _rollback_staged(plan, intended):
            raise InstallError("installation failed; staged records were rolled back") from exc
        raise InstallError("installation failed and rollback was incomplete") from exc
    status = "installed" if stages else "idempotent"
    return InstallResult(status, tuple(stages), environment)


def apply_install_plan(plan: InstallPlan, *, fault=None) -> InstallResult:
    with locked_install_session(plan) as session:
        return session.apply(fault=fault)


def repair_installation(spec: InstallSpec, *, fault=None) -> InstallResult:
    return apply_install_plan(preflight_installation(spec), fault=fault)
