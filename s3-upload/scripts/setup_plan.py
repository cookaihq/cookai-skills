from __future__ import annotations

import hashlib
import fcntl
import os
import secrets
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import unquote_to_bytes

from config_install import (
    InstallPlan, InstallSpec, SelectorChange, apply_install_plan,
    preflight_installation,
)
from resolver import GLOBAL_CREDENTIALS, PROJECT_CREDENTIALS
from safe_io import (
    FileSecurityError, lexical_absolute, open_directory, read_regular_file,
)
from strict_json import StrictJSONError, canonicalize, loads
from v2_schema import SchemaError, parse_credential_map, parse_reference

from setup_contracts import (
    SetupContractError, envelope, lookup_registered_setup_contract,
    normalize_target,
    validate_setup_observation, validate_setup_plan, validate_setup_request,
)


class SetupPlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlanningContext:
    project_root: str
    config_home: str
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)
    use_local_key: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class SetupInput:
    path: str
    value: Dict[str, Any]
    identity: tuple


@dataclass(frozen=True)
class PlanSinkSnapshot:
    path: str
    parent_identity: tuple
    state: str
    file_identity: Optional[tuple]
    digest: Optional[str]


def _dotenv(text: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if text is None:
        return result
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SetupPlanError(f"invalid dotenv line {number}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0:
                raise SetupPlanError(f"invalid dotenv line {number}")
            value = value[1:end]
        result[key] = value
    return result


def _read(path: Path, *, secret: bool) -> Optional[str]:
    try:
        return read_regular_file(str(path), max_bytes=1048576, secret=secret, missing_ok=True)
    except (FileSecurityError, OSError) as exc:
        raise SetupPlanError("local configuration path is unsafe") from exc


def read_setup_input(path: str) -> SetupInput:
    absolute = lexical_absolute(path)
    parent_fd = None
    descriptor = None
    try:
        parent_fd = open_directory(os.path.dirname(absolute))
        descriptor = os.open(
            os.path.basename(absolute),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or mode & 0o022
            or before.st_nlink != 1
            or before.st_size > 1048576
        ):
            raise SetupPlanError("setup input must be an owned safe regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, 1048577 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 1048576:
                raise SetupPlanError("setup input is too large")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev", "st_ino", "st_uid", "st_mode", "st_nlink", "st_size",
            "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(before, key) != getattr(after, key) for key in stable_fields):
            raise SetupPlanError("setup input changed while it was read")
        identity = (after.st_dev, after.st_ino)
        text = b"".join(chunks).decode("utf-8")
        value = loads(text or "")
    except (OSError, UnicodeDecodeError, FileSecurityError, StrictJSONError) as exc:
        raise SetupPlanError("setup input is unsafe or invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)
    if not isinstance(value, dict):
        raise SetupPlanError("setup input must be a JSON object")
    return SetupInput(absolute, value, identity)


def _protected_plan_path(path: str, context: PlanningContext) -> bool:
    project = lexical_absolute(context.project_root)
    home = lexical_absolute(context.config_home)
    exact = {
        os.path.join(project, ".env"),
        os.path.join(project, ".env.local"),
        os.path.join(project, ".s3-upload", "config.json"),
    }
    project_tree = os.path.join(project, ".s3-upload")
    return path in exact or path == project_tree or path.startswith(project_tree + os.sep) or path == home or path.startswith(home + os.sep)


def _opened_plan(path: str):
    parent_fd = open_directory(os.path.dirname(path))
    try:
        return _opened_plan_at(parent_fd, os.path.basename(path))
    finally:
        os.close(parent_fd)


def _opened_plan_at(parent_fd: int, name: str):
    descriptor = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > 1048576
        ):
            raise SetupPlanError("existing Setup Plan sink is unsafe")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 1048576:
                raise SetupPlanError("existing Setup Plan is too large")
        data = b"".join(chunks)
        try:
            validate_setup_plan(loads(data.decode("utf-8")))
        except (UnicodeDecodeError, StrictJSONError, SetupContractError) as exc:
            raise SetupPlanError("existing output is not a valid Setup Plan") from exc
        identity = (
            info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode),
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        )
        return identity, data
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SetupPlanError("Setup Plan sink is unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def preflight_plan_sink(
    path: str, *, context: PlanningContext, inputs: tuple,
) -> PlanSinkSnapshot:
    absolute = lexical_absolute(path)
    if _protected_plan_path(absolute, context):
        raise SetupPlanError("Setup Plan sink is in a protected namespace")
    if any(absolute == item.path for item in inputs):
        raise SetupPlanError("Setup Plan sink aliases an input")
    try:
        parent_fd = open_directory(os.path.dirname(absolute))
        parent = os.fstat(parent_fd)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise SetupPlanError("Setup Plan parent must be owned and not group/world-writable")
        parent_identity = (
            parent.st_dev, parent.st_ino, parent.st_uid, stat.S_IMODE(parent.st_mode),
        )
        os.close(parent_fd)
    except (OSError, FileSecurityError) as exc:
        raise SetupPlanError("Setup Plan parent is unsafe") from exc
    try:
        identity, data = _opened_plan(absolute)
    except FileNotFoundError:
        return PlanSinkSnapshot(absolute, parent_identity, "absent", None, None)
    if any((identity[0], identity[1]) == item.identity for item in inputs):
        raise SetupPlanError("Setup Plan sink is a hardlink alias of an input")
    return PlanSinkSnapshot(
        absolute, parent_identity, "existing-plan", identity,
        hashlib.sha256(data).hexdigest(),
    )


def publish_setup_plan(snapshot: PlanSinkSnapshot, plan: Dict[str, Any]) -> bytes:
    intended = canonicalize(validate_setup_plan(plan))
    parent_fd = open_directory(os.path.dirname(snapshot.path))
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        info = os.fstat(parent_fd)
        current_parent = (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
        if current_parent != snapshot.parent_identity:
            raise SetupPlanError("Setup Plan parent changed after preflight")
        name = os.path.basename(snapshot.path)
        if snapshot.state == "existing-plan":
            try:
                identity, data = _opened_plan_at(parent_fd, name)
            except FileNotFoundError as exc:
                raise SetupPlanError("Setup Plan sink changed after preflight") from exc
            if (
                identity != snapshot.file_identity
                or hashlib.sha256(data).hexdigest() != snapshot.digest
            ):
                raise SetupPlanError("Setup Plan sink changed after preflight")
        temporary = "." + name + "." + secrets.token_hex(8) + ".tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(intended)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short Setup Plan write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if snapshot.state == "absent":
                try:
                    os.link(
                        temporary, name,
                        src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise SetupPlanError(
                        "Setup Plan sink changed after preflight",
                    ) from exc
                os.unlink(temporary, dir_fd=parent_fd)
            else:
                os.replace(
                    temporary, name,
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                )
            published = True
            os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        return intended
    finally:
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        finally:
            os.close(parent_fd)


def _selected_persistent_credential(request: Dict[str, Any], context: PlanningContext) -> Dict[str, Any]:
    reference = parse_reference(request["credential_ref"], "credential_ref")
    variable = PROJECT_CREDENTIALS if reference.scope == "project" else GLOBAL_CREDENTIALS
    process_value = context.environ.get(variable, "")
    if process_value:
        raise SetupPlanError("standalone plan cannot capture a process Credential map")
    if reference.scope == "global" and not context.use_local_key:
        raise SetupPlanError("Global credential planning requires --use-local-key")
    path = (
        Path(context.project_root) / ".env.local"
        if reference.scope == "project"
        else Path(context.config_home) / ".env"
    )
    source = _dotenv(_read(path, secret=True)).get(variable, "")
    if not source:
        raise SetupPlanError("persistent Credential Profile is unavailable")
    try:
        raw = loads(source)
        parsed = parse_credential_map(raw)
    except (StrictJSONError, SchemaError) as exc:
        raise SetupPlanError("persistent Credential map is invalid") from exc
    if reference.name not in parsed:
        raise SetupPlanError("persistent Credential Profile is unavailable")
    return dict(raw[reference.name])


def _selected_process_credential(request: Dict[str, Any], context: PlanningContext) -> Dict[str, Any]:
    reference = parse_reference(request["credential_ref"], "credential_ref")
    variable = PROJECT_CREDENTIALS if reference.scope == "project" else GLOBAL_CREDENTIALS
    source = context.environ.get(variable, "")
    if not source:
        raise SetupPlanError("process Credential Profile is unavailable")
    try:
        raw = loads(source)
        parsed = parse_credential_map(raw)
    except (StrictJSONError, SchemaError) as exc:
        raise SetupPlanError("process Credential map is invalid") from exc
    if reference.name not in parsed:
        raise SetupPlanError("process Credential Profile is unavailable")
    return dict(raw[reference.name])


def capture_process_credential(
    request: Dict[str, Any], context: PlanningContext,
) -> Dict[str, Any]:
    if request["credential_source_category"] != "process-memory":
        raise SetupPlanError("request does not select a process Credential")
    return _selected_process_credential(request, context)


def _persistent_credential_slot_is_absent(
    request: Dict[str, Any], context: PlanningContext,
) -> bool:
    reference = parse_reference(request["credential_ref"], "credential_ref")
    variable = PROJECT_CREDENTIALS if reference.scope == "project" else GLOBAL_CREDENTIALS
    if context.environ.get(variable, ""):
        raise SetupPlanError("process Credential map would shadow planned issuance")
    if reference.scope == "global" and not context.use_local_key:
        raise SetupPlanError("Global credential planning requires --use-local-key")
    path = (
        Path(context.project_root) / ".env.local"
        if reference.scope == "project"
        else Path(context.config_home) / ".env"
    )
    source = _dotenv(_read(path, secret=True)).get(variable, "")
    if not source:
        return True
    try:
        raw = loads(source)
        parsed = parse_credential_map(raw)
    except (StrictJSONError, SchemaError) as exc:
        raise SetupPlanError("persistent Credential map is invalid") from exc
    return reference.name not in parsed


def _selector(value: Optional[Dict[str, Any]]) -> Optional[SelectorChange]:
    if value is None:
        return None
    return SelectorChange(
        value["kind"], value["caller_skill"], value["before"], value["after"],
    )


def _local_install(
    request: Dict[str, Any], context: PlanningContext, *,
    credential_handle_id: Optional[str] = None,
    credential_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    category = request["credential_source_category"]
    if category == "persistent-existing":
        if credential_override is not None:
            raise SetupPlanError("persistent planning must not inject a Credential")
        if credential_handle_id is not None:
            raise SetupPlanError("persistent credentials must not use a memory handle")
        credential = _selected_persistent_credential(request, context)
        credential_mode = "persistent"
        slot_state = "existing"
        secret_file_role = (
            "project-env-local"
            if parse_reference(request["credential_ref"], "credential_ref").scope == "project"
            else "global-env"
        )
    elif category == "process-memory":
        if credential_handle_id is None:
            raise SetupPlanError("continuous planning requires a Credential handle")
        credential = (
            credential_override
            if credential_override is not None
            else _selected_process_credential(request, context)
        )
        credential_mode = "process-memory"
        slot_state = "process-memory"
        secret_file_role = "process-map"
    else:
        if credential_override is not None:
            raise SetupPlanError("planned issuance must not inject a Credential")
        if credential_handle_id is not None:
            raise SetupPlanError("planned issuance must not use a pre-supplied handle")
        if not _persistent_credential_slot_is_absent(request, context):
            raise SetupPlanError("planned issuance requires an absent Credential slot")
        credential = {
            "access_key_id": "SETUP-PLANNING-ONLY",
            "secret_access_key": "SETUP-PLANNING-ONLY-SECRET",
            "session_token": "",
            "expires_at": None,
        }
        credential_mode = "persistent"
        slot_state = "absent"
        secret_file_role = (
            "project-env-local"
            if parse_reference(request["credential_ref"], "credential_ref").scope == "project"
            else "global-env"
        )
    spec = InstallSpec(
        project_root=context.project_root,
        config_home=context.config_home,
        target_ref=request["target_ref"],
        target=request["proposed_target"],
        credential_ref=request["credential_ref"],
        credential=credential,
        selector_change=_selector(request["selector_change"]),
        environ=context.environ,
        now=context.now,
        credential_mode=credential_mode,
        allow_candidates=request["provider"] in {"aliyun-oss", "tencent-cos"},
    )
    try:
        install_plan = preflight_installation(spec)
    except ValueError as exc:
        raise SetupPlanError("local installation preflight failed") from exc
    projection = _local_install_projection(
        request,
        install_plan,
        credential_handle_id=credential_handle_id,
        slot_state=slot_state,
        secret_file_role=secret_file_role,
    )
    return projection, None if category == "planned-issuance" else credential


def _reject_plan_credential_reflection(
    value: Any, credential: Optional[Mapping[str, Any]],
) -> None:
    if credential is None:
        return
    protected = tuple(
        field for field in credential.values()
        if isinstance(field, str) and field
    )
    if not protected:
        return
    if isinstance(value, str):
        try:
            decoded = unquote_to_bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SetupPlanError("planned text contains invalid percent-encoded UTF-8") from exc
        if any(field in value or field in decoded for field in protected):
            raise SetupPlanError("Setup Plan input reflected Credential material")
    elif isinstance(value, list):
        for item in value:
            _reject_plan_credential_reflection(item, credential)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_plan_credential_reflection(item, credential)


def _local_install_projection(
    request: Dict[str, Any],
    install_plan: InstallPlan,
    *,
    credential_handle_id: Optional[str],
    slot_state: str,
    secret_file_role: str,
) -> Dict[str, Any]:
    category = request["credential_source_category"]
    snapshots = [
        {
            "role": planned.role,
            "path": planned.path,
            "secret": planned.secret,
            "snapshot": planned.snapshot.public_record(),
        }
        for planned in install_plan.files
    ]
    credential_snapshot = next(
        (row["snapshot"] for row in snapshots if row["role"] == "credential"),
        {"state": "absent"},
    )
    version_token = credential_snapshot.get("version_token")
    git_verdicts = []
    if install_plan.git_verdict.applicable:
        verdict = install_plan.git_verdict
        git_verdicts.append({
            "path": next(row["path"] for row in snapshots if row["role"] == "credential"),
            "status": "passed" if verdict.ignored else "install-local-exclude",
            "repository_root": verdict.repository_root,
            "relative_path": verdict.relative_path,
            "tracked": verdict.tracked,
            "ignored": verdict.ignored,
            "exclude_path": verdict.exclude_path,
            "anchored_rule": verdict.anchored_rule,
        })
    credential_ref = parse_reference(request["credential_ref"], "credential_ref")
    return {
        "project_root": str(Path(install_plan.spec.project_root).absolute()),
        "config_home": str(Path(install_plan.spec.config_home).absolute()),
        "target_ref": request["target_ref"],
        "credential_ref": request["credential_ref"],
        "credential_source_category": category,
        "credential_persistence": request["credential_persistence"],
        "credential_handle_id": credential_handle_id,
        "proposed_target": install_plan.target_record,
        "selector_change": request["selector_change"],
        "credential_slot": {
            "name": credential_ref.name,
            "state": slot_state,
            "secret_file_role": secret_file_role,
            "version_token": (
                version_token
                if install_plan.spec.credential_mode == "persistent"
                else None
            ),
        },
        "file_snapshots": snapshots,
        "git_verdicts": git_verdicts,
    }


def _process_environment_with_credential(
    environ: Mapping[str, str], credential_ref: str, credential: Dict[str, Any],
) -> Dict[str, str]:
    reference = parse_reference(credential_ref, "credential_ref")
    variable = PROJECT_CREDENTIALS if reference.scope == "project" else GLOBAL_CREDENTIALS
    source = environ.get(variable, "")
    if source:
        try:
            raw = loads(source)
            parse_credential_map(raw)
            mapping = dict(raw)
        except (StrictJSONError, SchemaError) as exc:
            raise SetupPlanError("process Credential map is invalid") from exc
    else:
        mapping = {}
    mapping[reference.name] = credential
    result = dict(environ)
    result[variable] = canonicalize(mapping).decode("utf-8")
    return result


def prepare_setup_install(
    plan: Dict[str, Any],
    *,
    context: PlanningContext,
    credential: Optional[Dict[str, Any]] = None,
) -> tuple[InstallPlan, Dict[str, Any]]:
    local = plan["local_install"]["payload"]
    if (
        str(Path(context.project_root).absolute()) != local["project_root"]
        or str(Path(context.config_home).absolute()) != local["config_home"]
    ):
        raise SetupPlanError("execution context does not match planned configuration roots")
    category = local["credential_source_category"]
    if category == "persistent-existing":
        if credential is not None:
            raise SetupPlanError("persistent execution must re-read the planned Credential slot")
        credential = _selected_persistent_credential(local, context)
        credential_mode = "persistent"
        slot_state = "existing"
    elif category == "process-memory":
        if credential is None:
            raise SetupPlanError("process-memory execution requires a bound Credential")
        credential_mode = "process-memory"
        slot_state = "process-memory"
    else:
        if credential is None:
            credential = {
                "access_key_id": "SETUP-PLANNING-ONLY",
                "secret_access_key": "SETUP-PLANNING-ONLY-SECRET",
                "session_token": "",
                "expires_at": None,
            }
        credential_mode = "persistent"
        slot_state = "absent"
    environ = dict(context.environ)
    if credential_mode == "process-memory":
        environ = _process_environment_with_credential(
            environ, local["credential_ref"], credential,
        )
    spec = InstallSpec(
        project_root=local["project_root"],
        config_home=local["config_home"],
        target_ref=local["target_ref"],
        target=local["proposed_target"],
        credential_ref=local["credential_ref"],
        credential=credential,
        selector_change=_selector(local["selector_change"]),
        environ=environ,
        now=context.now,
        credential_mode=credential_mode,
        allow_candidates=local["proposed_target"]["provider"] in {
            "aliyun-oss", "tencent-cos",
        },
    )
    try:
        install_plan = preflight_installation(spec)
    except ValueError as exc:
        raise SetupPlanError("local installation preflight changed") from exc
    expected = _local_install_projection(
        local,
        install_plan,
        credential_handle_id=local["credential_handle_id"],
        slot_state=slot_state,
        secret_file_role=local["credential_slot"]["secret_file_role"],
    )
    if expected != local:
        raise SetupPlanError("local installation preflight changed")
    return install_plan, credential


def build_setup_plan(
    request_value: Any,
    observation_value: Any,
    *,
    context: PlanningContext,
    plan_id_factory: Callable[[], Any] = uuid.uuid4,
    credential_handle_id: Optional[str] = None,
    credential_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        request = validate_setup_request(request_value)
        observation = validate_setup_observation(observation_value)
    except SetupContractError as exc:
        raise SetupPlanError(str(exc)) from exc
    payload = observation["observation"]["payload"]
    if request["provider"] != observation["provider"]:
        raise SetupPlanError("Request and Observation providers do not match")
    registry = lookup_registered_setup_contract(
        provider=observation["provider"],
        contract_id=observation["contract_id"],
        surface_version=observation["surface_version"],
        registry_revision=observation["registry_revision"],
    )
    if registry is None:
        raise SetupPlanError("setup registry identity is unavailable")
    try:
        observed_scope = registry.canonical_scope(payload)
    except ValueError as exc:
        raise SetupPlanError("registered Observation scope is invalid") from exc
    target = request["proposed_target"]
    if target["access"] is None:
        existing_prefix_is_safe = (
            payload["dedicated"]
            and not payload["new_bucket"]
            and payload["prefix_empty"]
            and not payload["prefix_overlap"]
        )
        new_bucket_is_safe = (
            payload["dedicated"]
            and payload["new_bucket"]
            and payload["state"] == "absent"
        )
        if not (new_bucket_is_safe or existing_prefix_is_safe):
            raise SetupPlanError(
                "public default requires verified dedicated storage",
            )
        if payload["public_base_url"] is None:
            raise SetupPlanError("public default requires a verified Public Base URL")
        candidate = dict(target)
        candidate["access"] = {
            "mode": "public",
            "public_base_url": payload["public_base_url"],
            "presign_expires_seconds": None,
        }
        try:
            target = normalize_target(
                candidate,
                expected_scope=parse_reference(
                    request["target_ref"], "target_ref",
                ).scope,
            )
        except SetupContractError as exc:
            raise SetupPlanError(str(exc)) from exc
        request = {**request, "proposed_target": target}
    if (
        request["account_hint"] is not None
        and request["account_hint"] != observed_scope["account"]
    ):
        raise SetupPlanError("account_hint does not match observed account")
    if (
        target["region"] != observed_scope["region"]
        or target["bucket"] != observed_scope["bucket"]
        or target["prefix"] != observed_scope["prefix"]
    ):
        raise SetupPlanError("Observation scope does not match proposed Target")
    local_payload, planning_credential = _local_install(
        request,
        context,
        credential_handle_id=credential_handle_id,
        credential_override=credential_override,
    )
    plan_id_value = str(plan_id_factory())
    initial_value = observation["observation"]
    try:
        initial_digest = registry.observation_digest(initial_value)
    except ValueError as exc:
        raise SetupPlanError("registered Observation digest failed") from exc
    action_rows = []
    action_ids = []
    for index, action_type in enumerate(request["requested_action_types"], 1):
        action_id = f"action-{index}"
        action_ids.append(action_id)
        delivery = None
        if action_type == "issue-long-lived-access-key":
            delivery = {
                "fields": ["access_key_id", "secret_access_key"],
                "one_time": True,
                "destination": request["credential_persistence"],
                "requires_memory_sink": True,
            }
        try:
            built_action = registry.build_action(action_type, payload, request)
        except ValueError as exc:
            raise SetupPlanError("registered setup action is unavailable") from exc
        action_rows.append({
            "action_id": action_id,
            "action_type": action_type,
            "resource_scope": built_action["resource_scope"],
            "before_observation_id": "initial-observation",
            "before_digest": initial_digest,
            "mutation": built_action["mutation"],
            "diff": built_action["diff"],
            "expected_success": built_action["expected_success"],
            "recovery_limits": built_action["recovery_limits"],
            "credential_delivery": delivery,
        })
    plan = {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-plan",
        "mode": request["mode"],
        "plan_id": plan_id_value,
        "plan_hash": "",
        "setup_contract": registry.registry_contract(
            request["requested_action_types"], mode=request["mode"],
        ),
        "authorization_scope": {
            "account": observed_scope["account"],
            "region": observed_scope["region"],
            "bucket": observed_scope["bucket"],
            "prefix": observed_scope["prefix"],
            "target_ref": request["target_ref"],
            "credential_ref": request["credential_ref"],
            "credential_source_category": request["credential_source_category"],
            "credential_persistence": request["credential_persistence"],
            "action_ids": action_ids,
            "selector_change": request["selector_change"],
        },
        "observations": [{
            "observation_id": "initial-observation",
            "action_id": None,
            "value": initial_value,
            "digest": initial_digest,
        }],
        "actions": action_rows,
        "local_install": envelope("s3-upload.local-install", local_payload),
        "recovery_limits": envelope("s3-upload.setup-recovery", {
            "cloud_rollback": "never-automatic",
            "unknown_mutation_retry": "never",
            "local_install_repair": "staged-idempotent",
            "credential_issuance_recovery": "manual-revoke-and-reissue",
        }),
    }
    _reject_plan_credential_reflection(plan, planning_credential)
    unhashed = {key: value for key, value in plan.items() if key != "plan_hash"}
    plan["plan_hash"] = "sha256:" + hashlib.sha256(canonicalize(unhashed)).hexdigest()
    try:
        return validate_setup_plan(plan)
    except SetupContractError as exc:
        raise SetupPlanError(str(exc)) from exc


def install_setup_plan_local(
    plan: Dict[str, Any], *, context: PlanningContext,
    credential: Optional[Dict[str, Any]] = None,
    credential_mode: Optional[str] = None,
):
    try:
        install_plan, _ = prepare_setup_install(
            plan, context=context, credential=credential,
        )
        return apply_install_plan(install_plan)
    except ValueError as exc:
        raise SetupPlanError("local installation failed") from exc
