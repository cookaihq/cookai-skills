from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from strict_json import MAX_SAFE_INTEGER, canonicalize
from v2_schema import NAME_RE, SchemaError, parse_reference, parse_target


SETUP_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
GENERIC_PROVIDER = "custom"
GENERIC_CONTRACT_ID = "generic.console.v1"
GENERIC_SURFACE_VERSION = "synthetic.v1"
GENERIC_REGISTRY_REVISION = "generic.v1"
GENERIC_REGION_POLICY_CLASS = "synthetic"
ACTION_TYPES = (
    "create-bucket",
    "apply-prefix-public-read",
    "write-lifecycle",
    "write-cors",
    "issue-long-lived-access-key",
)


class SetupContractError(ValueError):
    pass


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SetupContractError(f"{label} must be an object")
    return value


def _exact(value: Mapping[str, Any], keys: Sequence[str], label: str) -> None:
    expected = set(keys)
    actual = set(value)
    if actual - expected:
        raise SetupContractError(f"{label} has unknown fields: {', '.join(sorted(actual - expected))}")
    if expected - actual:
        raise SetupContractError(f"{label} is missing fields: {', '.join(sorted(expected - actual))}")


def _setup_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SETUP_ID_RE.fullmatch(value):
        raise SetupContractError(f"invalid {label}")
    return value


def validate_setup_identifier(value: Any, label: str = "Setup Identifier") -> str:
    return _setup_id(value, label)


def _single_line(value: Any, label: str, *, nullable: bool = False) -> Optional[str]:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise SetupContractError(f"invalid {label}")
    return value


def schema_ref(identifier: str) -> Dict[str, Any]:
    return {"id": identifier, "version": 1}


def validate_schema_ref(value: Any, label: str = "Schema Reference") -> Dict[str, Any]:
    item = _object(value, label)
    _exact(item, ("id", "version"), label)
    _setup_id(item["id"], label + " id")
    if not isinstance(item["version"], int) or isinstance(item["version"], bool) or not 1 <= item["version"] <= MAX_SAFE_INTEGER:
        raise SetupContractError(f"invalid {label} version")
    return item


def envelope(schema_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"schema": schema_ref(schema_id), "payload": payload}


def _action_contract(action_type: str) -> Dict[str, Any]:
    return {
        "action_type": action_type,
        "state": "test-only",
        "evidence_id": "generic.synthetic.v1",
        "observation_schema": schema_ref("generic.observation"),
        "resource_scope_schema": schema_ref("generic.resource-scope"),
        "mutation_schema": schema_ref("generic.mutation"),
        "diff_schema": schema_ref("generic.diff"),
        "success_schema": schema_ref("generic.success"),
        "recovery_schema": schema_ref("generic.recovery"),
    }


GENERIC_ACTIONS = {action: _action_contract(action) for action in ACTION_TYPES}


class GenericSetupContract:
    provider = GENERIC_PROVIDER
    region_policy_class = GENERIC_REGION_POLICY_CLASS
    contract_id = GENERIC_CONTRACT_ID
    surface_version = GENERIC_SURFACE_VERSION
    registry_revision = GENERIC_REGISTRY_REVISION
    observation_schema = schema_ref("generic.observation")
    actions = GENERIC_ACTIONS

    def registry_contract(
        self, action_types: Sequence[str], *, mode: str = "test-only",
    ) -> Dict[str, Any]:
        if mode != "test-only":
            raise SetupContractError("normal assisted setup is unavailable")
        return generic_registry_contract(action_types)

    def validate_payload_envelope(
        self, value: Any, expected_schema: Mapping[str, Any], purpose: str,
    ) -> Dict[str, Any]:
        return validate_payload_envelope(value, expected_schema, purpose)

    def observation_digest(self, value: Mapping[str, Any]) -> str:
        return observation_digest(value)

    def canonical_scope(self, observation_payload: Any) -> Dict[str, str]:
        payload = validate_observation_payload(observation_payload)
        return {
            "account": payload["account"],
            "region": payload["region"],
            "bucket": payload["bucket"],
            "prefix": payload["prefix"],
        }

    def build_action(
        self, action_type: str, observation_payload: Any,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        payload = validate_observation_payload(observation_payload)
        if action_type not in GENERIC_ACTIONS:
            raise SetupContractError(f"unknown or disabled setup action: {action_type}")
        return {
            "resource_scope": envelope("generic.resource-scope", {
                "account": payload["account"], "region": payload["region"],
                "bucket": payload["bucket"], "prefix": payload["prefix"],
            }),
            "mutation": envelope("generic.mutation", {
                "operation": action_type, "parameters": {},
            }),
            "diff": envelope("generic.diff", {
                "before_state": payload["state"], "after_state": "present",
                "summary": action_type,
            }),
            "expected_success": envelope("generic.success", {"state": "present"}),
            "recovery_limits": envelope("generic.recovery", {
                "retry": "never", "rollback": "manual",
            }),
        }


GENERIC_SETUP_CONTRACT = GenericSetupContract()


def lookup_registered_setup_contract(
    *, provider: str, contract_id: str, surface_version: str,
    registry_revision: str,
):
    identity = (
        provider, contract_id, surface_version, registry_revision,
    )
    if identity == (
        GENERIC_PROVIDER, GENERIC_CONTRACT_ID, GENERIC_SURFACE_VERSION,
        GENERIC_REGISTRY_REVISION,
    ):
        return GENERIC_SETUP_CONTRACT
    try:
        from provider_setup_candidates import lookup_setup_contract
    except ImportError:
        return None
    return lookup_setup_contract(
        provider=provider,
        contract_id=contract_id,
        surface_version=surface_version,
        registry_revision=registry_revision,
    )


def setup_contract_for_plan(plan: Mapping[str, Any]):
    identity = plan["setup_contract"]
    contract = lookup_registered_setup_contract(
        provider=identity["provider"],
        contract_id=identity["contract_id"],
        surface_version=identity["surface_version"],
        registry_revision=identity["registry_revision"],
    )
    if contract is None:
        raise SetupContractError("Setup Plan registry identity is unavailable")
    return contract


def generic_registry_contract(action_types: Iterable[str]) -> Dict[str, Any]:
    rows = []
    for action_type in action_types:
        if action_type not in GENERIC_ACTIONS:
            raise SetupContractError(f"unknown or disabled setup action: {action_type}")
        rows.append(dict(GENERIC_ACTIONS[action_type]))
    return {
        "provider": GENERIC_PROVIDER,
        "region_policy_class": GENERIC_REGION_POLICY_CLASS,
        "surface_version": GENERIC_SURFACE_VERSION,
        "registry_revision": GENERIC_REGISTRY_REVISION,
        "contract_id": GENERIC_CONTRACT_ID,
        "action_contracts": rows,
    }


def normalize_target(value: Any, *, expected_scope: str) -> Dict[str, Any]:
    try:
        target = parse_target(
            value, expected_scope=expected_scope, allow_candidates=True,
        )
    except SchemaError as exc:
        raise SetupContractError(str(exc)) from exc
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
            "cors": target.setup.cors,
        },
    }


def normalize_proposed_target(value: Any, *, expected_scope: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("access") is not None:
        return normalize_target(value, expected_scope=expected_scope)
    candidate = dict(value)
    candidate["access"] = {
        "mode": "public",
        "public_base_url": "https://setup.invalid",
        "presign_expires_seconds": None,
    }
    normalized = normalize_target(candidate, expected_scope=expected_scope)
    normalized["access"] = None
    return normalized


def validate_selector(value: Any, *, target_ref: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    item = _object(value, "selector_change")
    _exact(item, ("kind", "caller_skill", "before", "after"), "selector_change")
    if item["kind"] not in {"project-default", "skill-target"}:
        raise SetupContractError("invalid selector_change.kind")
    if item["kind"] == "project-default" and item["caller_skill"] is not None:
        raise SetupContractError("project-default caller_skill must be null")
    if item["kind"] == "skill-target" and (
        not isinstance(item["caller_skill"], str) or not NAME_RE.fullmatch(item["caller_skill"])
    ):
        raise SetupContractError("skill-target requires a valid caller_skill")
    if item["before"] is not None:
        try:
            parse_reference(item["before"], "selector before")
        except SchemaError as exc:
            raise SetupContractError(str(exc)) from exc
    if item["after"] != target_ref:
        raise SetupContractError("selector_change.after must equal target_ref")
    return dict(item)


def validate_setup_request(value: Any) -> Dict[str, Any]:
    item = _object(value, "Setup Request")
    keys = (
        "schema_version", "artifact_type", "mode", "provider", "account_hint",
        "target_ref", "credential_ref", "credential_source_category",
        "credential_persistence", "proposed_target", "requested_action_types",
        "selector_change",
    )
    _exact(item, keys, "Setup Request")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool) or item["artifact_type"] != "s3-upload-setup-request":
        raise SetupContractError("invalid Setup Request identity")
    if item["mode"] != "test-only":
        raise SetupContractError("normal assisted setup is unavailable")
    provider = _setup_id(item["provider"], "provider")
    _single_line(item["account_hint"], "account_hint", nullable=True)
    try:
        target_ref = parse_reference(item["target_ref"], "target_ref")
        credential_ref = parse_reference(item["credential_ref"], "credential_ref")
    except SchemaError as exc:
        raise SetupContractError(str(exc)) from exc
    if target_ref.scope != credential_ref.scope:
        raise SetupContractError("Target and Credential references must have the same scope")
    category = item["credential_source_category"]
    persistence = item["credential_persistence"]
    if category not in {"persistent-existing", "process-memory", "planned-issuance"}:
        raise SetupContractError("invalid credential_source_category")
    if persistence not in {"project", "global", "this-run"}:
        raise SetupContractError("invalid credential_persistence")
    if (category == "process-memory") != (persistence == "this-run"):
        raise SetupContractError("process-memory and this-run must be selected together")
    if category != "process-memory" and persistence != target_ref.scope:
        raise SetupContractError("persistent credential scope must equal Target scope")
    target = normalize_proposed_target(
        item["proposed_target"], expected_scope=target_ref.scope,
    )
    if target["credential"] != credential_ref.text or target["provider"] != provider:
        raise SetupContractError("proposed Target does not match request references/provider")
    actions = item["requested_action_types"]
    if not isinstance(actions, list) or not actions:
        raise SetupContractError("requested_action_types must be a non-empty array")
    normalized_actions = []
    for action in actions:
        action = _setup_id(action, "requested action type")
        if action in normalized_actions:
            raise SetupContractError("requested_action_types must be unique")
        normalized_actions.append(action)
    if "issue-long-lived-access-key" in normalized_actions:
        if category != "planned-issuance" or normalized_actions[-1] != "issue-long-lived-access-key":
            raise SetupContractError("credential issuance must be the final action of a planned issuance request")
    elif category == "planned-issuance":
        raise SetupContractError("planned issuance requires a final credential issuance action")
    selector = validate_selector(item["selector_change"], target_ref=target_ref.text)
    return {
        **item,
        "target_ref": target_ref.text,
        "credential_ref": credential_ref.text,
        "proposed_target": target,
        "requested_action_types": normalized_actions,
        "selector_change": selector,
    }


def validate_observation_payload(value: Any) -> Dict[str, Any]:
    item = _object(value, "generic observation payload")
    keys = (
        "account", "region", "bucket", "prefix", "surface_marker", "dedicated",
        "new_bucket", "prefix_empty", "prefix_overlap", "public_base_url", "state",
    )
    _exact(item, keys, "generic observation payload")
    for key in ("account", "region", "bucket", "surface_marker"):
        _single_line(item[key], key)
    if not isinstance(item["prefix"], str):
        raise SetupContractError("prefix must be a string")
    for key in ("dedicated", "new_bucket", "prefix_empty", "prefix_overlap"):
        if not isinstance(item[key], bool):
            raise SetupContractError(f"{key} must be boolean")
    _single_line(item["public_base_url"], "public_base_url", nullable=True)
    if item["state"] not in {"absent", "present"}:
        raise SetupContractError("invalid generic observation state")
    return dict(item)


def validate_payload_envelope(value: Any, expected_schema: Mapping[str, Any], purpose: str) -> Dict[str, Any]:
    item = _object(value, purpose)
    _exact(item, ("schema", "payload"), purpose)
    schema = validate_schema_ref(item["schema"], purpose + " schema")
    if schema != expected_schema:
        raise SetupContractError(f"wrong-purpose or unregistered schema for {purpose}")
    schema_id = schema["id"]
    payload = item["payload"]
    if schema_id == "generic.observation":
        payload = validate_observation_payload(payload)
    elif schema_id == "generic.resource-scope":
        payload = _object(payload, purpose + " payload")
        _exact(payload, ("account", "region", "bucket", "prefix"), purpose + " payload")
        for key in ("account", "region", "bucket"):
            _single_line(payload[key], key)
        if not isinstance(payload["prefix"], str):
            raise SetupContractError("resource prefix must be a string")
    elif schema_id == "generic.mutation":
        payload = _object(payload, purpose + " payload")
        _exact(payload, ("operation", "parameters"), purpose + " payload")
        _setup_id(payload["operation"], "mutation operation")
        if payload["parameters"] != {}:
            raise SetupContractError("generic mutation parameters must be empty")
    elif schema_id == "generic.diff":
        payload = _object(payload, purpose + " payload")
        _exact(payload, ("before_state", "after_state", "summary"), purpose + " payload")
        if payload["before_state"] not in {"absent", "present"} or payload["after_state"] not in {"absent", "present"}:
            raise SetupContractError("invalid generic diff state")
        _setup_id(payload["summary"], "diff summary")
    elif schema_id == "generic.success":
        payload = _object(payload, purpose + " payload")
        _exact(payload, ("state",), purpose + " payload")
        if payload["state"] != "present":
            raise SetupContractError("generic success state must be present")
    elif schema_id == "generic.recovery":
        payload = _object(payload, purpose + " payload")
        _exact(payload, ("retry", "rollback"), purpose + " payload")
        if payload["retry"] != "never" or payload["rollback"] != "manual":
            raise SetupContractError("invalid generic recovery boundary")
    elif schema_id == "s3-upload.local-install":
        payload = validate_local_install_payload(payload)
    elif schema_id == "s3-upload.setup-recovery":
        payload = validate_setup_recovery_payload(payload)
    else:
        raise SetupContractError(f"unregistered schema: {schema_id}")
    return {"schema": schema, "payload": payload}


def validate_setup_observation(value: Any) -> Dict[str, Any]:
    item = _object(value, "Setup Observation")
    keys = (
        "schema_version", "artifact_type", "provider", "contract_id",
        "surface_version", "registry_revision", "observation",
    )
    _exact(item, keys, "Setup Observation")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool) or item["artifact_type"] != "s3-upload-setup-observation":
        raise SetupContractError("invalid Setup Observation identity")
    for key in ("provider", "contract_id", "surface_version", "registry_revision"):
        _setup_id(item[key], key)
    contract = lookup_registered_setup_contract(
        provider=item["provider"],
        contract_id=item["contract_id"],
        surface_version=item["surface_version"],
        registry_revision=item["registry_revision"],
    )
    if contract is None:
        raise SetupContractError("unregistered or stale setup observation identity")
    try:
        observation = contract.validate_payload_envelope(
            item["observation"], contract.observation_schema,
            "initial observation",
        )
    except ValueError as exc:
        raise SetupContractError("invalid registered setup observation") from exc
    return {**item, "observation": observation}


def validate_local_install_payload(value: Any) -> Dict[str, Any]:
    item = _object(value, "local_install payload")
    keys = (
        "project_root", "config_home", "target_ref", "credential_ref",
        "credential_source_category", "credential_persistence", "credential_handle_id",
        "proposed_target", "selector_change", "credential_slot", "file_snapshots",
        "git_verdicts",
    )
    _exact(item, keys, "local_install payload")
    for key in ("project_root", "config_home"):
        _single_line(item[key], key)
        if not os.path.isabs(item[key]) or os.path.abspath(item[key]) != item[key]:
            raise SetupContractError(f"{key} must be a normalized absolute path")
    try:
        target_ref = parse_reference(item["target_ref"], "target_ref")
        credential_ref = parse_reference(item["credential_ref"], "credential_ref")
    except SchemaError as exc:
        raise SetupContractError(str(exc)) from exc
    if target_ref.scope != credential_ref.scope:
        raise SetupContractError("local Target and Credential scopes must match")
    normalized_target = normalize_target(
        item["proposed_target"], expected_scope=target_ref.scope,
    )
    if normalized_target != item["proposed_target"]:
        raise SetupContractError("local proposed Target must be normalized")
    if normalized_target["credential"] != credential_ref.text:
        raise SetupContractError("local proposed Target credential mismatch")
    validate_selector(item["selector_change"], target_ref=target_ref.text)
    category = item["credential_source_category"]
    persistence = item["credential_persistence"]
    if category not in {"persistent-existing", "process-memory", "planned-issuance"}:
        raise SetupContractError("invalid local credential source")
    if persistence not in {"project", "global", "this-run"}:
        raise SetupContractError("invalid local credential persistence")
    if (category == "process-memory") != (persistence == "this-run"):
        raise SetupContractError("local process-memory and this-run must match")
    if category != "process-memory" and persistence != target_ref.scope:
        raise SetupContractError("local persistent credential scope mismatch")
    if item["credential_handle_id"] is not None:
        _setup_id(item["credential_handle_id"], "credential handle id")
    slot = _object(item["credential_slot"], "credential_slot")
    _exact(slot, ("name", "state", "secret_file_role", "version_token"), "credential_slot")
    if not NAME_RE.fullmatch(slot["name"]):
        raise SetupContractError("invalid credential slot name")
    if slot["state"] not in {"existing", "absent", "process-memory"}:
        raise SetupContractError("invalid credential slot state")
    if slot["secret_file_role"] not in {"project-env-local", "global-env", "process-map"}:
        raise SetupContractError("invalid credential secret_file_role")
    expected_role = "project-env-local" if target_ref.scope == "project" else "global-env"
    if slot["name"] != credential_ref.name:
        raise SetupContractError("credential slot name mismatch")
    if category == "process-memory":
        if (
            item["credential_handle_id"] is None
            or slot["state"] != "process-memory"
            or slot["secret_file_role"] != "process-map"
            or slot["version_token"] is not None
        ):
            raise SetupContractError("invalid process-memory Credential slot")
    elif category == "persistent-existing":
        if (
            item["credential_handle_id"] is not None
            or slot["state"] != "existing"
            or slot["secret_file_role"] != expected_role
            or slot["version_token"] is None
        ):
            raise SetupContractError("invalid existing Credential slot")
    elif (
        item["credential_handle_id"] is not None
        or slot["state"] != "absent"
        or slot["secret_file_role"] != expected_role
    ):
        raise SetupContractError("invalid planned-issuance Credential slot")
    if slot["version_token"] is not None:
        token = _object(slot["version_token"], "Secret File Version Token")
        _exact(token, (
            "owned", "type", "device", "inode", "owner", "mode", "size",
            "mtime_ns", "ctime_ns",
        ), "Secret File Version Token")
        if token["owned"] is not True or token["type"] != "regular":
            raise SetupContractError("invalid Secret File Version Token verdict")
        for key in ("device", "inode", "owner", "size", "mtime_ns", "ctime_ns"):
            if not isinstance(token[key], str) or not re.fullmatch(r"0|[1-9][0-9]*", token[key]):
                raise SetupContractError("invalid Secret File Version Token identity")
        if not isinstance(token["mode"], str) or not re.fullmatch(r"[0-7]{4}", token["mode"]):
            raise SetupContractError("invalid Secret File Version Token mode")
    snapshots = item["file_snapshots"]
    if not isinstance(snapshots, list):
        raise SetupContractError("file_snapshots must be an array")
    def validate_identity(identity_value: Any, label: str) -> Dict[str, Any]:
        identity = _object(identity_value, label)
        _exact(identity, (
            "device", "inode", "owner", "mode", "size", "mtime_ns", "ctime_ns",
        ), label)
        for identity_key in (
            "device", "inode", "owner", "size", "mtime_ns", "ctime_ns",
        ):
            if (
                not isinstance(identity[identity_key], str)
                or not re.fullmatch(r"0|[1-9][0-9]*", identity[identity_key])
            ):
                raise SetupContractError(f"invalid {label}")
        if (
            not isinstance(identity["mode"], str)
            or not re.fullmatch(r"[0-7]{4}", identity["mode"])
        ):
            raise SetupContractError(f"invalid {label} mode")
        return identity

    roles = []
    rows_by_role = {}
    for snapshot in snapshots:
        row = _object(snapshot, "file snapshot")
        _exact(row, ("role", "path", "secret", "snapshot"), "file snapshot")
        _setup_id(row["role"], "file role")
        if row["role"] in roles:
            raise SetupContractError("duplicate file snapshot role")
        roles.append(row["role"])
        _single_line(row["path"], "file path")
        if not os.path.isabs(row["path"]) or os.path.abspath(row["path"]) != row["path"]:
            raise SetupContractError("file snapshot path must be normalized and absolute")
        if not isinstance(row["secret"], bool) or not isinstance(row["snapshot"], dict):
            raise SetupContractError("invalid file snapshot")
        state = row["snapshot"].get("state")
        if state == "absent":
            _exact(row["snapshot"], ("state",), "absent file snapshot")
        elif state == "present" and row["secret"]:
            _exact(
                row["snapshot"], ("state", "version_token"),
                "Secret file snapshot",
            )
            if row["snapshot"]["version_token"] != slot["version_token"]:
                raise SetupContractError("Secret snapshot/version token mismatch")
        elif state == "present" and not row["secret"]:
            _exact(
                row["snapshot"], ("state", "identity", "sha256"),
                "non-Secret file snapshot",
            )
            validate_identity(row["snapshot"]["identity"], "file identity")
            digest = row["snapshot"]["sha256"]
            if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
                raise SetupContractError("invalid non-Secret file snapshot digest")
        else:
            raise SetupContractError("invalid file snapshot state")
        if row["secret"] != (row["role"] == "credential"):
            raise SetupContractError("file snapshot Secret role mismatch")
        rows_by_role[row["role"]] = row
    required_roles = ["target"]
    if category != "process-memory":
        required_roles.insert(0, "credential")
    if item["selector_change"] is not None:
        required_roles.append("selector")
    if roles[:len(required_roles)] != required_roles or any(
        role not in {*required_roles, "git-exclude"} for role in roles
    ):
        raise SetupContractError("invalid ordered local file snapshots")
    if category == "process-memory" and any(row["secret"] for row in snapshots):
        raise SetupContractError("process-memory plan must not snapshot a Secret file")
    credential_snapshot = rows_by_role.get("credential")
    if credential_snapshot is not None:
        file_present = credential_snapshot["snapshot"]["state"] == "present"
        if file_present != (slot["version_token"] is not None):
            raise SetupContractError("Credential slot/file version projection mismatch")
    expected_paths = {
        "credential": os.path.join(
            item["project_root"], ".env.local",
        ) if target_ref.scope == "project" else os.path.join(
            item["config_home"], ".env",
        ),
        "target": os.path.join(
            item["project_root"], ".s3-upload", "targets",
            target_ref.name + ".json",
        ) if target_ref.scope == "project" else os.path.join(
            item["config_home"], "targets", target_ref.name + ".json",
        ),
        "selector": os.path.join(
            item["project_root"], ".s3-upload", "config.json",
        ),
    }
    for role in required_roles:
        if rows_by_role[role]["path"] != expected_paths[role]:
            raise SetupContractError("local file snapshot path does not match planned role")
    verdicts = item["git_verdicts"]
    if not isinstance(verdicts, list) or len(verdicts) > 1:
        raise SetupContractError("git_verdicts must be an array")
    for verdict_value in verdicts:
        verdict = _object(verdict_value, "Git verdict")
        _exact(verdict, (
            "path", "status", "repository_root", "relative_path", "tracked",
            "ignored", "exclude_path", "anchored_rule",
        ), "Git verdict")
        if verdict["path"] != expected_paths["credential"]:
            raise SetupContractError("Git verdict path mismatch")
        for key in ("repository_root", "exclude_path"):
            _single_line(verdict[key], "Git verdict " + key)
            if not os.path.isabs(verdict[key]):
                raise SetupContractError("Git verdict path must be absolute")
        _single_line(verdict["relative_path"], "Git verdict relative_path")
        if os.path.isabs(verdict["relative_path"]):
            raise SetupContractError("Git verdict relative_path must be relative")
        if verdict["tracked"] is not False or not isinstance(verdict["ignored"], bool):
            raise SetupContractError("invalid Git tracked/ignored verdict")
        expected_status = "passed" if verdict["ignored"] else "install-local-exclude"
        if verdict["status"] != expected_status:
            raise SetupContractError("Git verdict status mismatch")
        if (
            not isinstance(verdict["anchored_rule"], str)
            or not verdict["anchored_rule"].startswith("/")
            or "\n" in verdict["anchored_rule"]
        ):
            raise SetupContractError("invalid anchored Git exclude rule")
        exclude = rows_by_role.get("git-exclude")
        if verdict["status"] == "install-local-exclude":
            if exclude is None or exclude["path"] != verdict["exclude_path"]:
                raise SetupContractError("Git exclude snapshot is missing")
        elif exclude is not None:
            raise SetupContractError("unexpected Git exclude snapshot")
    if "git-exclude" in rows_by_role and not verdicts:
        raise SetupContractError("Git exclude snapshot requires a Git verdict")
    return dict(item)


def validate_setup_recovery_payload(value: Any) -> Dict[str, Any]:
    item = _object(value, "setup recovery payload")
    keys = (
        "cloud_rollback", "unknown_mutation_retry", "local_install_repair",
        "credential_issuance_recovery",
    )
    _exact(item, keys, "setup recovery payload")
    expected = {
        "cloud_rollback": "never-automatic",
        "unknown_mutation_retry": "never",
        "local_install_repair": "staged-idempotent",
        "credential_issuance_recovery": "manual-revoke-and-reissue",
    }
    if item != expected:
        raise SetupContractError("invalid setup recovery policy")
    return dict(item)


def observation_digest(value: Mapping[str, Any]) -> str:
    material = {
        "contract_id": GENERIC_CONTRACT_ID,
        "surface_version": GENERIC_SURFACE_VERSION,
        "registry_revision": GENERIC_REGISTRY_REVISION,
        "value": value,
    }
    return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()


def _uuid4(value: Any) -> str:
    if not isinstance(value, str):
        raise SetupContractError("invalid plan_id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SetupContractError("invalid plan_id") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise SetupContractError("invalid plan_id")
    return value


def validate_setup_plan(value: Any) -> Dict[str, Any]:
    item = _object(value, "Setup Plan")
    keys = (
        "schema_version", "artifact_type", "mode", "plan_id", "plan_hash",
        "setup_contract", "authorization_scope", "observations", "actions",
        "local_install", "recovery_limits",
    )
    _exact(item, keys, "Setup Plan")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool) or item["artifact_type"] != "s3-upload-setup-plan":
        raise SetupContractError("invalid Setup Plan identity")
    if item["mode"] != "test-only":
        raise SetupContractError("normal assisted setup is unavailable")
    _uuid4(item["plan_id"])
    if not isinstance(item["plan_hash"], str) or not HASH_RE.fullmatch(item["plan_hash"]):
        raise SetupContractError("invalid plan_hash")
    unhashed = {key: value for key, value in item.items() if key != "plan_hash"}
    expected_hash = "sha256:" + hashlib.sha256(canonicalize(unhashed)).hexdigest()
    if item["plan_hash"] != expected_hash:
        raise SetupContractError("Setup Plan hash mismatch")
    contract = _object(item["setup_contract"], "setup_contract")
    _exact(contract, (
        "provider", "region_policy_class", "surface_version", "registry_revision",
        "contract_id", "action_contracts",
    ), "setup_contract")
    for key in (
        "provider", "region_policy_class", "surface_version",
        "registry_revision", "contract_id",
    ):
        _setup_id(contract[key], "setup_contract " + key)
    registry = lookup_registered_setup_contract(
        provider=contract["provider"],
        contract_id=contract["contract_id"],
        surface_version=contract["surface_version"],
        registry_revision=contract["registry_revision"],
    )
    if registry is None or contract["region_policy_class"] != registry.region_policy_class:
        raise SetupContractError("Setup Contract registry identity is stale")
    actions = item["actions"]
    if not isinstance(actions, list) or not actions:
        raise SetupContractError("actions must be a non-empty array")
    action_types = []
    for action in actions:
        row = _object(action, "action")
        _exact(row, (
            "action_id", "action_type", "resource_scope", "before_observation_id",
            "before_digest", "mutation", "diff", "expected_success",
            "recovery_limits", "credential_delivery",
        ), "action")
        _setup_id(row["action_id"], "action_id")
        action_type = _setup_id(row["action_type"], "action_type")
        if action_type in action_types:
            raise SetupContractError("setup action types must be unique")
        action_types.append(action_type)
    try:
        expected_contract = registry.registry_contract(
            action_types, mode=item["mode"],
        )
    except ValueError as exc:
        raise SetupContractError("Setup Contract contains unavailable actions") from exc
    if contract != expected_contract:
        raise SetupContractError("Setup Contract is stale or self-asserted")
    observations = item["observations"]
    if not isinstance(observations, list) or len(observations) != 1:
        raise SetupContractError("plan requires exactly one initial observation")
    observation_ids = {}
    for index, observation in enumerate(observations):
        row = _object(observation, "plan observation")
        _exact(row, ("observation_id", "action_id", "value", "digest"), "plan observation")
        observation_id = _setup_id(row["observation_id"], "observation_id")
        if observation_id in observation_ids:
            raise SetupContractError("duplicate observation_id")
        if row["action_id"] is not None:
            raise SetupContractError("initial observation action_id must be null")
        try:
            validated_value = registry.validate_payload_envelope(
                row["value"], registry.observation_schema, "plan observation",
            )
            expected_observation_digest = registry.observation_digest(
                validated_value,
            )
        except ValueError as exc:
            raise SetupContractError("invalid registered plan observation") from exc
        if row["digest"] != expected_observation_digest:
            raise SetupContractError("observation digest mismatch")
        observation_ids[observation_id] = row
    action_ids = []
    for index, action in enumerate(actions):
        action_id = action["action_id"]
        if action_id in action_ids:
            raise SetupContractError("duplicate action_id")
        action_ids.append(action_id)
        contract_row = registry.actions[action["action_type"]]
        before = observation_ids.get(action["before_observation_id"])
        if before is None or before["digest"] != action["before_digest"]:
            raise SetupContractError("action before observation is unresolved")
        for field_name, contract_field in (
            ("resource_scope", "resource_scope_schema"),
            ("mutation", "mutation_schema"),
            ("diff", "diff_schema"),
            ("expected_success", "success_schema"),
            ("recovery_limits", "recovery_schema"),
        ):
            try:
                registry.validate_payload_envelope(
                    action[field_name], contract_row[contract_field], field_name,
                )
            except ValueError as exc:
                raise SetupContractError(
                    "invalid registered action payload",
                ) from exc
        delivery = action["credential_delivery"]
        if action["action_type"] == "issue-long-lived-access-key":
            if index != len(actions) - 1 or delivery != {
                "fields": ["access_key_id", "secret_access_key"],
                "one_time": True,
                "destination": item["authorization_scope"]["credential_persistence"],
                "requires_memory_sink": True,
            }:
                raise SetupContractError("invalid credential_delivery")
        elif delivery is not None:
            raise SetupContractError("credential_delivery is only valid for final issuance")
    authorization = _object(item["authorization_scope"], "authorization_scope")
    _exact(authorization, (
        "account", "region", "bucket", "prefix", "target_ref", "credential_ref",
        "credential_source_category", "credential_persistence", "action_ids",
        "selector_change",
    ), "authorization_scope")
    for key in ("account", "region", "bucket"):
        _single_line(authorization[key], "authorization " + key)
    if not isinstance(authorization["prefix"], str):
        raise SetupContractError("authorization prefix must be a string")
    try:
        authorization_target = parse_reference(
            authorization["target_ref"], "authorization target_ref",
        )
        authorization_credential = parse_reference(
            authorization["credential_ref"], "authorization credential_ref",
        )
    except SchemaError as exc:
        raise SetupContractError(str(exc)) from exc
    if authorization_target.scope != authorization_credential.scope:
        raise SetupContractError("authorization reference scopes must match")
    category = authorization["credential_source_category"]
    persistence = authorization["credential_persistence"]
    if category not in {"persistent-existing", "process-memory", "planned-issuance"}:
        raise SetupContractError("invalid authorization credential source")
    if persistence not in {"project", "global", "this-run"}:
        raise SetupContractError("invalid authorization credential persistence")
    if (category == "process-memory") != (persistence == "this-run"):
        raise SetupContractError("authorization process-memory and this-run must match")
    if category != "process-memory" and persistence != authorization_target.scope:
        raise SetupContractError("authorization persistent scope mismatch")
    validate_selector(
        authorization["selector_change"], target_ref=authorization_target.text,
    )
    if authorization["action_ids"] != action_ids:
        raise SetupContractError("authorization action_ids mismatch")
    local_install = validate_payload_envelope(
        item["local_install"], schema_ref("s3-upload.local-install"), "local_install",
    )
    validate_payload_envelope(
        item["recovery_limits"], schema_ref("s3-upload.setup-recovery"), "recovery_limits",
    )
    local = local_install["payload"]
    for key in (
        "target_ref", "credential_ref", "credential_source_category",
        "credential_persistence", "selector_change",
    ):
        if authorization[key] != local[key]:
            raise SetupContractError(f"authorization/local_install {key} mismatch")
    initial_payload = observations[0]["value"]["payload"]
    target = local["proposed_target"]
    if contract["provider"] != target["provider"]:
        raise SetupContractError("Setup Contract provider does not own proposed Target")
    try:
        registered_scope = registry.canonical_scope(initial_payload)
    except ValueError as exc:
        raise SetupContractError("invalid registered observation scope") from exc
    expected_scope = {
        "account": registered_scope["account"], "region": target["region"],
        "bucket": target["bucket"], "prefix": target["prefix"],
    }
    if any(
        registered_scope[key] != expected_scope[key]
        for key in ("region", "bucket", "prefix")
    ):
        raise SetupContractError("registered observation scope does not match Target")
    if any(authorization[key] != expected_scope[key] for key in expected_scope):
        raise SetupContractError("authorization scope does not match initial observation/Target")
    for action in actions:
        if action["before_observation_id"] != observations[0]["observation_id"]:
            raise SetupContractError("action must use the initial observation")
        scope = action["resource_scope"]["payload"]
        if any(scope[key] != authorization[key] for key in ("account", "region", "bucket", "prefix")):
            raise SetupContractError("action resource scope exceeds authorization")
        if action["mutation"]["payload"]["operation"] != action["action_type"]:
            raise SetupContractError("action mutation purpose mismatch")
        if action["diff"]["payload"]["summary"] != action["action_type"]:
            raise SetupContractError("action diff purpose mismatch")
        if registry is GENERIC_SETUP_CONTRACT:
            if action["diff"]["payload"]["before_state"] != initial_payload["state"]:
                raise SetupContractError("action diff does not match initial observation")
            if (
                action["diff"]["payload"]["after_state"]
                != action["expected_success"]["payload"]["state"]
            ):
                raise SetupContractError("action diff/success projection mismatch")
    has_issuance = action_types[-1] == "issue-long-lived-access-key"
    if has_issuance != (category == "planned-issuance"):
        raise SetupContractError("credential source/action projection mismatch")
    return item


def _recovery_instructions(value: Any, label: str) -> List[str]:
    if not isinstance(value, list):
        raise SetupContractError(f"{label} must be an array")
    for instruction in value:
        if (
            not isinstance(instruction, str)
            or not instruction
            or len(instruction.encode("utf-8")) > 1024
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in instruction)
        ):
            raise SetupContractError(f"invalid {label}")
    return value


def validate_setup_result(
    value: Any, *, plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    item = _object(value, "Setup Result")
    _exact(item, (
        "schema_version", "artifact_type", "plan_id", "plan_hash", "status",
        "action_results", "created_resources", "local_install_result",
        "recovery_instructions",
    ), "Setup Result")
    if (
        item["schema_version"] != 1
        or isinstance(item["schema_version"], bool)
        or item["artifact_type"] != "s3-upload-setup-result"
    ):
        raise SetupContractError("invalid Setup Result identity")
    _uuid4(item["plan_id"])
    if not isinstance(item["plan_hash"], str) or not HASH_RE.fullmatch(item["plan_hash"]):
        raise SetupContractError("invalid Setup Result plan_hash")
    if item["status"] not in {
        "completed", "partial", "plan_stale", "blocked", "failed", "unknown",
    }:
        raise SetupContractError("invalid Setup Result status")
    if item["local_install_result"] not in {
        "not_started", "installed", "idempotent", "configuration_changed", "failed",
    }:
        raise SetupContractError("invalid local install result")
    _recovery_instructions(item["recovery_instructions"], "recovery_instructions")
    action_results = item["action_results"]
    if not isinstance(action_results, list):
        raise SetupContractError("action_results must be an array")
    action_ids = []
    action_statuses = {}
    for row_value in action_results:
        row = _object(row_value, "action result")
        _exact(row, (
            "action_id", "status", "before_digest", "after_digest",
            "recovery_instructions",
        ), "action result")
        action_id = _setup_id(row["action_id"], "result action_id")
        if action_id in action_ids:
            raise SetupContractError("duplicate result action_id")
        action_ids.append(action_id)
        if row["status"] not in {
            "not_started", "succeeded", "definite_failure", "unknown", "skipped",
        }:
            raise SetupContractError("invalid action result status")
        for key in ("before_digest", "after_digest"):
            digest = row[key]
            if digest is not None and (not isinstance(digest, str) or not HASH_RE.fullmatch(digest)):
                raise SetupContractError("invalid action result digest")
        if row["status"] == "succeeded" and (
            row["before_digest"] is None or row["after_digest"] is None
        ):
            raise SetupContractError("succeeded action requires both observation digests")
        if row["status"] != "succeeded" and row["after_digest"] is not None:
            raise SetupContractError("only succeeded action may have an after digest")
        _recovery_instructions(
            row["recovery_instructions"], "action recovery_instructions",
        )
        action_statuses[action_id] = row["status"]
    resources = item["created_resources"]
    if not isinstance(resources, list):
        raise SetupContractError("created_resources must be an array")
    for resource_value in resources:
        resource = _object(resource_value, "created resource")
        _exact(resource, ("action_id", "resource_type", "resource_id"), "created resource")
        action_id = _setup_id(resource["action_id"], "created resource action_id")
        if action_id not in action_statuses:
            raise SetupContractError("created resource action is unresolved")
        _setup_id(resource["resource_type"], "created resource type")
        _single_line(resource["resource_id"], "created resource id")
        if len(resource["resource_id"].encode("utf-8")) > 4096:
            raise SetupContractError("created resource id is too long")
    if plan is not None:
        if item["plan_id"] != plan["plan_id"] or item["plan_hash"] != plan["plan_hash"]:
            raise SetupContractError("Setup Result plan binding mismatch")
        expected_ids = [action["action_id"] for action in plan["actions"]]
        if action_ids != expected_ids:
            raise SetupContractError("Setup Result action order mismatch")
    statuses = list(action_statuses.values())
    succeeded = statuses.count("succeeded")
    if item["status"] == "completed":
        if (
            not statuses
            or succeeded != len(statuses)
            or item["local_install_result"] not in {"installed", "idempotent"}
        ):
            raise SetupContractError("completed Setup Result is inconsistent")
    elif item["status"] == "unknown":
        if "unknown" not in statuses or item["local_install_result"] != "not_started":
            raise SetupContractError("unknown Setup Result is inconsistent")
    elif item["status"] == "partial":
        action_incomplete = any(status != "succeeded" for status in statuses)
        local_incomplete = item["local_install_result"] in {
            "configuration_changed", "failed",
        }
        if succeeded == 0 or not (action_incomplete or local_incomplete):
            raise SetupContractError("partial Setup Result is inconsistent")
    elif item["status"] == "blocked":
        if succeeded or item["local_install_result"] != "not_started":
            raise SetupContractError("blocked Setup Result is inconsistent")
    elif item["status"] == "plan_stale":
        if succeeded or item["local_install_result"] not in {
            "not_started", "configuration_changed",
        }:
            raise SetupContractError("stale Setup Result is inconsistent")
    elif item["status"] == "failed":
        if succeeded or item["local_install_result"] not in {
            "not_started", "failed",
        }:
            raise SetupContractError("failed Setup Result is inconsistent")
    return item
