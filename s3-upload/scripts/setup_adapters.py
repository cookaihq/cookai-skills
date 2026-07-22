from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple

from setup_contracts import (
    GENERIC_CONTRACT_ID, GENERIC_PROVIDER, GENERIC_REGISTRY_REVISION,
    GENERIC_SURFACE_VERSION, HASH_RE, schema_ref,
    validate_payload_envelope, validate_setup_identifier,
)
from strict_json import canonicalize


class SetupAdapter(Protocol):
    synthetic: bool

    def wait_for_login(self, context: Dict[str, Any]) -> None:
        ...

    def observe(self, query: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def guarded_mutate(
        self, call: Dict[str, Any], credential_sink: Optional[Any] = None,
    ) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class FixtureContractExtension:
    provider: str
    contract_id: str
    surface_version: str
    registry_revision: str
    observation_schema: Mapping[str, Any]
    observation_validator: Callable[[Any, str], Dict[str, Any]]
    credential_delivery_fields: Tuple[str, ...] = (
        "access_key_id", "secret_access_key",
    )

    def validate_observation(self, value: Any, purpose: str) -> Dict[str, Any]:
        return self.observation_validator(value, purpose)


GENERIC_FIXTURE_EXTENSION = FixtureContractExtension(
    provider=GENERIC_PROVIDER,
    contract_id=GENERIC_CONTRACT_ID,
    surface_version=GENERIC_SURFACE_VERSION,
    registry_revision=GENERIC_REGISTRY_REVISION,
    observation_schema=schema_ref("generic.observation"),
    observation_validator=lambda value, purpose: validate_payload_envelope(
        value, schema_ref("generic.observation"), purpose,
    ),
)


def request_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()


def _line(value: Any, label: str, *, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ValueError(f"invalid {label}")
    return value


def validate_mutation_outcome(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("adapter mutation outcome must be an object")
    expected = {"status", "created_resource", "recovery_instructions"}
    if set(value) != expected:
        raise ValueError("adapter mutation outcome has invalid fields")
    if value["status"] not in {"accepted", "definite_failure", "unknown"}:
        raise ValueError("adapter mutation outcome has invalid status")
    if value["created_resource"] is not None:
        resource = value["created_resource"]
        if not isinstance(resource, dict) or set(resource) != {"resource_type", "resource_id"}:
            raise ValueError("adapter created resource is invalid")
        validate_setup_identifier(resource["resource_type"], "created resource type")
        _line(resource["resource_id"], "created resource id", max_bytes=4096)
    instructions = value["recovery_instructions"]
    if not isinstance(instructions, list):
        raise ValueError("adapter recovery instructions are invalid")
    for instruction in instructions:
        _line(instruction, "adapter recovery instruction", max_bytes=1024)
    return value


def validate_fixture(
    value: Any, extension: Optional[FixtureContractExtension] = None,
) -> Dict[str, Any]:
    extension = extension or GENERIC_FIXTURE_EXTENSION
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "artifact_type", "fixture_kind", "fixture_id",
        "provider", "contract_id", "surface_version", "registry_revision",
        "official_source_refs", "redaction_sentinels", "calls",
    }:
        raise ValueError("invalid Setup fixture fields")
    if (
        value["schema_version"] != 1
        or isinstance(value["schema_version"], bool)
        or value["artifact_type"] != "s3-upload-setup-fixture"
        or value["fixture_kind"] != "synthetic/docs-derived"
    ):
        raise ValueError("invalid Setup fixture identity")
    validate_setup_identifier(value["fixture_id"], "fixture id")
    expected = {
        "provider": extension.provider,
        "contract_id": extension.contract_id,
        "surface_version": extension.surface_version,
        "registry_revision": extension.registry_revision,
    }
    for key, expected_value in expected.items():
        validate_setup_identifier(value[key], "fixture " + key)
        if value[key] != expected_value:
            raise ValueError("fixture registry binding is stale")
    refs = value["official_source_refs"]
    if not isinstance(refs, list) or not refs:
        raise ValueError("fixture requires official source references")
    for source in refs:
        source = _line(source, "fixture source reference", max_bytes=4096)
        parts = urlsplit(source)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("fixture source reference must be HTTPS")
    sentinels = value["redaction_sentinels"]
    if not isinstance(sentinels, list) or len(set(sentinels)) != len(sentinels):
        raise ValueError("invalid fixture redaction sentinels")
    for sentinel in sentinels:
        _line(sentinel, "fixture redaction sentinel", max_bytes=4096)
    calls = value["calls"]
    if not isinstance(calls, list):
        raise ValueError("fixture calls must be an array")
    for call in calls:
        if not isinstance(call, dict) or set(call) != {
            "operation", "request_digest", "response", "credential_delivery",
        }:
            raise ValueError("invalid fixture call fields")
        operation = call["operation"]
        if operation not in {"wait-login", "observe", "guarded-mutate"}:
            raise ValueError("invalid fixture operation")
        if not isinstance(call["request_digest"], str) or not HASH_RE.fullmatch(call["request_digest"]):
            raise ValueError("invalid fixture request digest")
        delivery = call["credential_delivery"]
        if operation == "wait-login":
            if call["response"] is not None or delivery is not None:
                raise ValueError("wait-login fixture call must have null outputs")
        elif operation == "observe":
            extension.validate_observation(call["response"], "fixture observation")
            if delivery is not None:
                raise ValueError("observation fixture call cannot deliver credentials")
        else:
            validate_mutation_outcome(call["response"])
            if delivery is not None:
                if (
                    not isinstance(delivery, dict)
                    or set(delivery) != set(extension.credential_delivery_fields)
                ):
                    raise ValueError("invalid fixture credential delivery")
                for field in extension.credential_delivery_fields:
                    delivered = _line(delivery[field], field, max_bytes=4096)
                    if delivered not in sentinels:
                        raise ValueError("fixture credential must be a redaction sentinel")
    return value


class FixtureAdapter:
    synthetic = True

    def __init__(
        self, fixture: Any,
        extension: Optional[FixtureContractExtension] = None,
    ):
        self.extension = extension or GENERIC_FIXTURE_EXTENSION
        self._fixture = validate_fixture(fixture, self.extension)
        self._index = 0
        self.redaction_sentinels = tuple(self._fixture["redaction_sentinels"])

    def _consume(self, operation: str, request: Dict[str, Any]) -> Dict[str, Any]:
        calls = self._fixture["calls"]
        if self._index >= len(calls):
            raise ValueError("fixture received an extra adapter call")
        row = calls[self._index]
        if row["operation"] != operation or row["request_digest"] != request_digest(request):
            raise ValueError("fixture adapter call order or request digest mismatch")
        self._index += 1
        return row

    def wait_for_login(self, context: Dict[str, Any]) -> None:
        self._consume("wait-login", context)

    def observe(self, query: Dict[str, Any]) -> Dict[str, Any]:
        return copy.deepcopy(self._consume("observe", query)["response"])

    def guarded_mutate(
        self, call: Dict[str, Any], credential_sink: Optional[Any] = None,
    ) -> Dict[str, Any]:
        row = self._consume("guarded-mutate", call)
        delivery = row["credential_delivery"]
        if delivery is not None:
            if credential_sink is None:
                raise ValueError("fixture credential delivery requires a sink")
            credential_sink.deliver(copy.deepcopy(delivery))
        return copy.deepcopy(row["response"])

    def assert_consumed(self) -> None:
        if self._index != len(self._fixture["calls"]):
            raise ValueError("fixture has unconsumed adapter calls")

    def validate_execution_shape(self, plan: Dict[str, Any], *, login_done: bool) -> None:
        from setup_contracts import setup_contract_for_plan
        from setup_executor import guarded_mutation_call

        calls = self._fixture["calls"]
        index = self._index

        def expected(operation: str, request: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal index
            if index >= len(calls):
                raise ValueError("fixture omits an expected adapter call")
            row = calls[index]
            if row["operation"] != operation or row["request_digest"] != request_digest(request):
                raise ValueError("fixture adapter call order or request digest mismatch")
            index += 1
            return row

        if not login_done:
            expected("wait-login", {
                "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"],
                "authorization_scope": plan["authorization_scope"],
            })
        registry = setup_contract_for_plan(plan)
        for action in plan["actions"]:
            before_query = {
                "phase": "before", "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"], "action_id": action["action_id"],
            }
            before = expected("observe", before_query)["response"]
            if registry.observation_digest(before) != action["before_digest"]:
                if index != len(calls):
                    raise ValueError("fixture has calls after a planned observation drift")
                return
            mutation = expected(
                "guarded-mutate",
                guarded_mutation_call(plan, action, action["before_digest"]),
            )
            outcome = mutation["response"]
            if (
                outcome["status"] != "accepted"
                or (
                    action["action_type"] == "issue-long-lived-access-key"
                    and mutation["credential_delivery"] is None
                )
            ):
                if index != len(calls):
                    raise ValueError("fixture has calls after a terminal mutation outcome")
                return
            after_query = {**before_query, "phase": "after"}
            after = expected("observe", after_query)["response"]
            if (
                after["payload"].get("state")
                != action["expected_success"]["payload"].get("state")
            ):
                if index != len(calls):
                    raise ValueError("fixture has calls after a failed success observation")
                return
        if index != len(calls):
            raise ValueError("fixture has extra adapter calls")
