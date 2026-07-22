from __future__ import annotations

import copy
import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import unquote_to_bytes

from artifacts import IdentifierRejected, validate_provider_identifier
from config_install import (
    ConfigurationChanged, InstallError, _capture_snapshot, apply_install_plan,
    locked_install_session,
)
from setup_adapters import SetupAdapter, validate_mutation_outcome
from setup_contracts import (
    HASH_RE, SetupContractError, setup_contract_for_plan,
    validate_setup_identifier, validate_setup_plan,
)
from setup_plan import PlanningContext, SetupPlanError, prepare_setup_install
from v2_schema import SchemaError, parse_credential


@dataclass(frozen=True)
class ExecutionContext:
    project_root: str
    config_home: str
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)
    persisted: bool = True
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    authorized_action_types: Tuple[str, ...] = ()
    install_fault: Optional[Any] = field(default=None, repr=False, compare=False)


class CredentialHandleRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, Dict[str, Any]] = {}

    def __repr__(self) -> str:
        return f"CredentialHandleRegistry(live={len(self._entries)})"

    @property
    def live_count(self) -> int:
        return len(self._entries)

    def capture(self, credential: Mapping[str, Any]) -> str:
        try:
            parsed = parse_credential(dict(credential))
        except SchemaError as exc:
            raise ValueError("invalid process Credential Profile") from exc
        handle_id = "handle-" + uuid.uuid4().hex
        self._entries[handle_id] = {
            "credential": {
                "access_key_id": parsed.access_key_id,
                "secret_access_key": parsed.secret_access_key,
                "session_token": parsed.session_token,
                "expires_at": credential["expires_at"],
            },
            "binding": None,
        }
        return handle_id

    def bind(self, handle_id: str, plan_id: str, plan_hash: str) -> None:
        entry = self._entries.get(handle_id)
        if entry is None or entry["binding"] is not None:
            raise ValueError("Credential handle cannot be bound")
        entry["binding"] = (plan_id, plan_hash)

    def consume(self, handle_id: str, plan_id: str, plan_hash: str) -> Dict[str, Any]:
        entry = self._entries.pop(handle_id, None)
        if entry is None or entry["binding"] != (plan_id, plan_hash):
            raise ValueError("Credential handle is missing or stale")
        return copy.deepcopy(entry["credential"])

    def clear(self) -> None:
        self._entries.clear()


class CredentialSink:
    def __init__(self, plan_id: str, plan_hash: str, action_id: str) -> None:
        self._binding = (plan_id, plan_hash, action_id)
        self._profile: Optional[Dict[str, Any]] = None
        self._live = True

    def __repr__(self) -> str:
        return (
            "CredentialSink("
            f"live={self._live}, delivered={self._profile is not None})"
        )

    @property
    def live(self) -> bool:
        return self._live

    @property
    def delivered(self) -> bool:
        return self._profile is not None

    def deliver(self, value: Any) -> None:
        if not self._live or self._profile is not None:
            raise ValueError("Credential sink is unavailable or already consumed")
        if not isinstance(value, dict) or set(value) != {
            "access_key_id", "secret_access_key",
        }:
            raise ValueError("Credential sink accepts only an Access Key pair")
        profile = {
            "access_key_id": value["access_key_id"],
            "secret_access_key": value["secret_access_key"],
            "session_token": "",
            "expires_at": None,
        }
        if any(
            not isinstance(profile[key], str)
            or len(profile[key].encode("utf-8")) > 4096
            for key in ("access_key_id", "secret_access_key")
        ):
            raise ValueError("Credential sink value is invalid")
        try:
            parse_credential(profile)
        except SchemaError as exc:
            raise ValueError("Credential sink value is invalid") from exc
        self._profile = profile

    def _protected_values(self) -> Tuple[str, ...]:
        if self._profile is None:
            return ()
        return tuple(
            value for value in self._profile.values()
            if isinstance(value, str) and value
        )

    def _consume(self, binding: Tuple[str, str, str]) -> Dict[str, Any]:
        if not self._live or self._binding != binding or self._profile is None:
            raise ValueError("Credential sink delivery is missing or stale")
        profile = self._profile
        self._profile = None
        self._live = False
        return profile

    def clear(self) -> None:
        self._profile = None
        self._live = False


def validate_confirmation(value: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "artifact_type", "plan_id", "plan_hash", "decision",
    }:
        raise ValueError("invalid Setup Confirmation")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool) or value["artifact_type"] != "s3-upload-setup-confirmation":
        raise ValueError("invalid Setup Confirmation")
    if value["decision"] not in {"confirm", "reject"}:
        raise ValueError("invalid Setup Confirmation")
    if not isinstance(value["plan_id"], str):
        raise ValueError("invalid Setup Confirmation")
    try:
        parsed = uuid.UUID(value["plan_id"])
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid Setup Confirmation") from exc
    if parsed.version != 4 or str(parsed) != value["plan_id"]:
        raise ValueError("invalid Setup Confirmation")
    if not isinstance(value["plan_hash"], str) or not HASH_RE.fullmatch(value["plan_hash"]):
        raise ValueError("invalid Setup Confirmation")
    return value


def _result(plan: Dict[str, Any], status: str, action_results, created_resources,
            local_install_result: str, recovery=()):
    return {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-result",
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "status": status,
        "action_results": action_results,
        "created_resources": created_resources,
        "local_install_result": local_install_result,
        "recovery_instructions": list(recovery),
    }


def _not_started(plan: Dict[str, Any]):
    return [{
        "action_id": action["action_id"],
        "status": "not_started",
        "before_digest": None,
        "after_digest": None,
        "recovery_instructions": [],
    } for action in plan["actions"]]


def _validate_local_snapshots(plan: Dict[str, Any]) -> None:
    for row in plan["local_install"]["payload"]["file_snapshots"]:
        snapshot = _capture_snapshot(
            __import__("pathlib").Path(row["path"]), secret=row["secret"],
            exact_mode=0o600 if row["secret"] else None,
        ).public_record()
        if snapshot != row["snapshot"]:
            raise SetupPlanError("local installation snapshot changed")


def _forbidden_values(
    credential: Mapping[str, Any], adapter: SetupAdapter,
) -> Tuple[str, ...]:
    values = [value for value in credential.values() if isinstance(value, str) and value]
    values.extend(
        value for value in getattr(adapter, "redaction_sentinels", ())
        if isinstance(value, str) and value
    )
    return tuple(dict.fromkeys(values))


def _reject_reflection(value: Any, forbidden: Tuple[str, ...]) -> None:
    if not forbidden:
        return
    if isinstance(value, str):
        try:
            decoded = unquote_to_bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("adapter value contains invalid percent-encoded UTF-8") from exc
        if any(secret in value or secret in decoded for secret in forbidden):
            raise ValueError("adapter value reflected protected authentication material")
    elif isinstance(value, list):
        for item in value:
            _reject_reflection(item, forbidden)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_reflection(item, forbidden)


def guarded_mutation_call(
    plan: Dict[str, Any], action: Dict[str, Any], before_digest: str,
) -> Dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "before_digest": before_digest,
        "resource_scope": action["resource_scope"],
        "mutation": action["mutation"],
        "diff": action["diff"],
        "expected_success": action["expected_success"],
        "recovery_limits": action["recovery_limits"],
    }


def live_gate_satisfied(
    plan: Dict[str, Any], adapter: SetupAdapter, context: ExecutionContext,
) -> bool:
    if getattr(adapter, "synthetic", False):
        return True
    target = plan["local_install"]["payload"]["proposed_target"]
    required_actions = tuple(action["action_type"] for action in plan["actions"])
    return (
        context.environ.get("S3_UPLOAD_LIVE_TEST") == "1"
        and context.environ.get("S3_UPLOAD_LIVE_TEST_TARGET")
        == plan["authorization_scope"]["target_ref"]
        and target["setup"]["integration_test"] is True
        and all(action in context.authorized_action_types for action in required_actions)
    )


def _adapter_supports_credential_sink(adapter: SetupAdapter) -> bool:
    try:
        signature = inspect.signature(adapter.guarded_mutate)
    except (TypeError, ValueError, AttributeError):
        return False
    parameters = tuple(signature.parameters.values())
    return len(parameters) >= 2 or any(
        parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }
        for parameter in parameters
    )


def _created_resource(
    action: Dict[str, Any], resource: Any, forbidden: Tuple[str, ...],
) -> Optional[Dict[str, Any]]:
    if resource is None:
        return None
    try:
        resource_type = validate_setup_identifier(
            resource["resource_type"], "created resource type",
        )
        resource_id = validate_provider_identifier(
            resource["resource_id"], forbidden,
        )
    except (KeyError, TypeError, IdentifierRejected, SetupContractError) as exc:
        raise ValueError("adapter created resource is unsafe") from exc
    return {
        "action_id": action["action_id"],
        "resource_type": resource_type,
        "resource_id": resource_id,
    }


def _issuance_recovery(sink: CredentialSink) -> list[str]:
    instructions = ["manual_revoke_and_reissue"]
    values = sink._protected_values()
    if values:
        access_key_id = values[0]
        if len(access_key_id) >= 9:
            instructions.append(
                "access_key_id="
                + access_key_id[:4]
                + "****"
                + access_key_id[-4:],
            )
    return instructions


def _execute_credential_issuance(
    *,
    plan: Dict[str, Any],
    action: Dict[str, Any],
    action_index: int,
    before_digest: str,
    adapter: SetupAdapter,
    context: ExecutionContext,
    install_plan,
    forbidden: Tuple[str, ...],
    action_results: list,
    created_resources: list,
    registry,
) -> Tuple[Dict[str, Any], int]:
    sink = CredentialSink(plan["plan_id"], plan["plan_hash"], action["action_id"])
    try:
        try:
            with locked_install_session(install_plan) as install_session:
                try:
                    outcome = validate_mutation_outcome(
                        adapter.guarded_mutate(
                            guarded_mutation_call(plan, action, before_digest),
                            sink,
                        ),
                    )
                    combined_forbidden = tuple(dict.fromkeys(
                        forbidden + sink._protected_values()
                    ))
                    _reject_reflection(outcome, combined_forbidden)
                except Exception:
                    outcome = {
                        "status": "unknown",
                        "created_resource": None,
                        "recovery_instructions": [],
                    }
                    combined_forbidden = tuple(dict.fromkeys(
                        forbidden + sink._protected_values()
                    ))
                recovery = _issuance_recovery(sink)
                try:
                    resource = _created_resource(
                        action, outcome["created_resource"], combined_forbidden,
                    )
                except ValueError:
                    resource = None
                    outcome = {**outcome, "status": "unknown"}
                if outcome["status"] != "accepted" or not sink.delivered:
                    row_status = (
                        "unknown"
                        if outcome["status"] == "unknown" or sink.delivered
                        else "definite_failure"
                    )
                    current = {
                        "action_id": action["action_id"],
                        "status": row_status,
                        "before_digest": before_digest,
                        "after_digest": None,
                        "recovery_instructions": recovery,
                    }
                    later = [
                        {**row, "status": "skipped"}
                        for row in _not_started({
                            "actions": plan["actions"][action_index + 1:],
                        })
                    ]
                    if resource is not None:
                        created_resources.append(resource)
                    overall = (
                        "unknown"
                        if row_status == "unknown"
                        else ("failed" if not action_results else "partial")
                    )
                    return _result(
                        plan,
                        overall,
                        action_results + [current] + later,
                        created_resources,
                        "not_started",
                        recovery,
                    ), 1
                contract = registry.actions[action["action_type"]]
                try:
                    after_value = registry.validate_payload_envelope(
                        adapter.observe({
                            "phase": "after",
                            "plan_id": plan["plan_id"],
                            "plan_hash": plan["plan_hash"],
                            "action_id": action["action_id"],
                        }),
                        contract["observation_schema"],
                        "adapter after observation",
                    )
                    _reject_reflection(after_value, combined_forbidden)
                    if (
                        after_value["payload"]["state"]
                        != action["expected_success"]["payload"]["state"]
                    ):
                        raise ValueError("success observation mismatch")
                    after_digest = registry.observation_digest(after_value)
                except Exception:
                    current = {
                        "action_id": action["action_id"],
                        "status": "unknown",
                        "before_digest": before_digest,
                        "after_digest": None,
                        "recovery_instructions": recovery,
                    }
                    return _result(
                        plan,
                        "unknown",
                        action_results + [current],
                        created_resources,
                        "not_started",
                        recovery,
                    ), 1
                if resource is not None:
                    created_resources.append(resource)
                action_results.append({
                    "action_id": action["action_id"],
                    "status": "succeeded",
                    "before_digest": before_digest,
                    "after_digest": after_digest,
                    "recovery_instructions": [],
                })
                profile = sink._consume((
                    plan["plan_id"], plan["plan_hash"], action["action_id"],
                ))
                try:
                    installed = install_session.apply(
                        credential=profile,
                        fault=context.install_fault,
                    )
                except Exception:
                    action_results[-1]["recovery_instructions"] = recovery
                    return _result(
                        plan,
                        "partial",
                        action_results,
                        created_resources,
                        "failed",
                        recovery,
                    ), 1
                return _result(
                    plan,
                    "completed",
                    action_results,
                    created_resources,
                    installed.status,
                ), 0
        except ConfigurationChanged:
            current = {
                "action_id": action["action_id"],
                "status": "not_started",
                "before_digest": before_digest,
                "after_digest": None,
                "recovery_instructions": [],
            }
            overall = "partial" if action_results else "plan_stale"
            return _result(
                plan,
                overall,
                action_results + [current],
                created_resources,
                "configuration_changed",
            ), 1 if action_results else 3
        except InstallError:
            current = {
                "action_id": action["action_id"],
                "status": "not_started",
                "before_digest": before_digest,
                "after_digest": None,
                "recovery_instructions": [],
            }
            overall = "partial" if action_results else "failed"
            return _result(
                plan,
                overall,
                action_results + [current],
                created_resources,
                "failed",
            ), 1
    finally:
        sink.clear()


def execute_setup_plan(
    plan_value: Any,
    confirmation_value: Any,
    *,
    adapter: SetupAdapter,
    context: ExecutionContext,
    login_done: bool = False,
    handle_registry=None,
) -> Tuple[Dict[str, Any], int]:
    plan = validate_setup_plan(plan_value)
    registry = setup_contract_for_plan(plan)
    try:
        confirmation = validate_confirmation(confirmation_value, plan)
    except ValueError:
        return _result(plan, "blocked", _not_started(plan), [], "not_started"), 3
    if confirmation["decision"] == "reject":
        return _result(plan, "blocked", _not_started(plan), [], "not_started"), 3
    if confirmation["plan_id"] != plan["plan_id"] or confirmation["plan_hash"] != plan["plan_hash"]:
        return _result(plan, "plan_stale", _not_started(plan), [], "not_started"), 3
    category = plan["authorization_scope"]["credential_source_category"]
    if context.persisted and category == "process-memory":
        return _result(plan, "plan_stale", _not_started(plan), [], "not_started"), 3
    if category == "planned-issuance" and not _adapter_supports_credential_sink(adapter):
        return _result(plan, "blocked", _not_started(plan), [], "not_started"), 3
    if not live_gate_satisfied(plan, adapter, context):
        return _result(plan, "blocked", _not_started(plan), [], "not_started"), 3
    bound_credential = None
    if category == "process-memory":
        handle_id = plan["local_install"]["payload"]["credential_handle_id"]
        try:
            if handle_registry is None:
                raise ValueError("missing Credential handle registry")
            bound_credential = handle_registry.consume(
                handle_id, plan["plan_id"], plan["plan_hash"],
            )
        except Exception:
            return _result(
                plan, "plan_stale", _not_started(plan), [], "not_started",
            ), 3
    try:
        _validate_local_snapshots(plan)
        install_plan, selected_credential = prepare_setup_install(
            plan,
            context=PlanningContext(
                project_root=context.project_root,
                config_home=context.config_home,
                environ=context.environ,
                use_local_key=True,
                now=context.now,
            ),
            credential=bound_credential,
        )
    except Exception:
        return _result(plan, "plan_stale", _not_started(plan), [], "configuration_changed"), 3
    forbidden = _forbidden_values(selected_credential, adapter)
    if not login_done:
        try:
            login_result = adapter.wait_for_login({
                "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                "authorization_scope": plan["authorization_scope"],
            })
            if login_result is not None:
                raise ValueError("adapter login seam returned unexpected data")
        except Exception:
            return _result(plan, "failed", _not_started(plan), [], "not_started"), 1
    action_results = []
    created_resources = []
    for action_index, action in enumerate(plan["actions"]):
        contract = registry.actions[action["action_type"]]
        try:
            before_value = registry.validate_payload_envelope(
                adapter.observe({
                    "phase": "before", "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"], "action_id": action["action_id"],
                }),
                contract["observation_schema"], "adapter before observation",
            )
            _reject_reflection(before_value, forbidden)
            before_digest = registry.observation_digest(before_value)
        except Exception:
            remaining = _not_started({"actions": plan["actions"][action_index:]})
            return _result(plan, "failed" if not action_results else "partial", action_results + remaining, created_resources, "not_started"), 1
        if before_digest != action["before_digest"]:
            current = {
                "action_id": action["action_id"], "status": "not_started",
                "before_digest": before_digest, "after_digest": None,
                "recovery_instructions": [],
            }
            later = _not_started({"actions": plan["actions"][action_index + 1:]})
            status = "plan_stale" if not action_results else "partial"
            return _result(plan, status, action_results + [current] + later, created_resources, "not_started"), 3 if status == "plan_stale" else 1
        if action["action_type"] == "issue-long-lived-access-key":
            return _execute_credential_issuance(
                plan=plan,
                action=action,
                action_index=action_index,
                before_digest=before_digest,
                adapter=adapter,
                context=context,
                install_plan=install_plan,
                forbidden=forbidden,
                action_results=action_results,
                created_resources=created_resources,
                registry=registry,
            )
        try:
            outcome = validate_mutation_outcome(
                adapter.guarded_mutate(
                    guarded_mutation_call(plan, action, before_digest),
                ),
            )
            _reject_reflection(outcome, forbidden)
        except Exception:
            outcome = {
                "status": "unknown",
                "created_resource": None,
                "recovery_instructions": ["manual provider inspection required"],
            }
        if outcome["status"] != "accepted":
            row_status = "unknown" if outcome["status"] == "unknown" else "definite_failure"
            current = {
                "action_id": action["action_id"], "status": row_status,
                "before_digest": before_digest, "after_digest": None,
                "recovery_instructions": outcome["recovery_instructions"],
            }
            later = [{**row, "status": "skipped"} for row in _not_started({"actions": plan["actions"][action_index + 1:]})]
            overall = "unknown" if row_status == "unknown" else ("failed" if not action_results else "partial")
            return _result(plan, overall, action_results + [current] + later, created_resources, "not_started", outcome["recovery_instructions"]), 1
        try:
            after_value = registry.validate_payload_envelope(
                adapter.observe({
                    "phase": "after", "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"], "action_id": action["action_id"],
                }),
                contract["observation_schema"], "adapter after observation",
            )
            _reject_reflection(after_value, forbidden)
            if after_value["payload"]["state"] != action["expected_success"]["payload"]["state"]:
                raise ValueError("success observation mismatch")
            after_digest = registry.observation_digest(after_value)
        except Exception:
            current = {
                "action_id": action["action_id"], "status": "unknown",
                "before_digest": before_digest, "after_digest": None,
                "recovery_instructions": ["manual provider inspection required"],
            }
            later = [{**row, "status": "skipped"} for row in _not_started({"actions": plan["actions"][action_index + 1:]})]
            return _result(plan, "unknown", action_results + [current] + later, created_resources, "not_started", current["recovery_instructions"]), 1
        resource = outcome["created_resource"]
        if resource is not None:
            try:
                resource_type = validate_setup_identifier(
                    resource["resource_type"], "created resource type",
                )
                validate_provider_identifier(resource["resource_id"], forbidden)
            except (KeyError, IdentifierRejected, SetupContractError):
                current = {
                    "action_id": action["action_id"], "status": "unknown",
                    "before_digest": before_digest, "after_digest": None,
                    "recovery_instructions": ["manual provider inspection required"],
                }
                later = [
                    {**row, "status": "skipped"}
                    for row in _not_started({"actions": plan["actions"][action_index + 1:]})
                ]
                return _result(
                    plan, "unknown", action_results + [current] + later,
                    created_resources, "not_started", current["recovery_instructions"],
                ), 1
            created_resources.append({
                "action_id": action["action_id"], "resource_type": resource_type,
                "resource_id": resource["resource_id"],
            })
        action_results.append({
            "action_id": action["action_id"], "status": "succeeded",
            "before_digest": before_digest, "after_digest": after_digest,
            "recovery_instructions": [],
        })
    try:
        installed = apply_install_plan(install_plan)
    except ConfigurationChanged:
        return _result(
            plan, "partial", action_results, created_resources,
            "configuration_changed",
        ), 1
    except Exception:
        return _result(plan, "partial", action_results, created_resources, "failed"), 1
    return _result(plan, "completed", action_results, created_resources, installed.status), 0
