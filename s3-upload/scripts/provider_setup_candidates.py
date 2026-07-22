from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from strict_json import canonicalize


FIXTURE_KIND = "synthetic/docs-derived"
REMOTE_EVIDENCE = "not-tested"
SETUP_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
COS_BUCKET_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?-[1-9][0-9]{4,19}\Z"
)

ALIYUN_OSS_SETUP_IDENTITY = MappingProxyType({
    "provider": "aliyun-oss",
    "region_policy_class": "aliyun-oss-regional-console",
    "contract_id": "aliyun-oss.setup.console.v1",
    "surface_version": "aliyun-oss-console.synthetic.v1",
    "registry_revision": "aliyun-oss-setup-registry.v1",
})

TENCENT_COS_SETUP_IDENTITY = MappingProxyType({
    "provider": "tencent-cos",
    "region_policy_class": "tencent-cos-regional-console",
    "contract_id": "tencent-cos.setup.console.v1",
    "surface_version": "tencent-cos-console.synthetic.v1",
    "registry_revision": "tencent-cos-setup-registry.v1",
})

TEST_ONLY_ACTIONS = (
    "create-dedicated-bucket",
    "apply-new-bucket-public-read",
    "apply-prefix-public-read",
    "merge-prefix-lifecycle",
    "merge-bucket-cors",
    "issue-long-lived-access-key",
)

DISABLED_ACTIONS = (
    "change-account-public-access",
    "change-existing-bucket-wide-public-access",
)


class ProviderSetupError(ValueError):
    pass


def _exact(value: Any, keys: Iterable[str], label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderSetupError(f"{label} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        raise ProviderSetupError(f"invalid {label} fields")
    return dict(value)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SETUP_ID_RE.fullmatch(value):
        raise ProviderSetupError(f"invalid {label}")
    return value


def _line(value: Any, label: str, *, nullable: bool = False) -> Optional[str]:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ProviderSetupError(f"invalid {label}")
    return value


def _schema_ref(identifier: str) -> Dict[str, Any]:
    return {"id": identifier, "version": 1}


def _validate_schema_ref(value: Any, label: str) -> Dict[str, Any]:
    item = _exact(value, ("id", "version"), label)
    _identifier(item["id"], label + " id")
    if item["version"] != 1 or isinstance(item["version"], bool):
        raise ProviderSetupError(f"invalid {label} version")
    return dict(item)


def _envelope(schema: Mapping[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"schema": dict(schema), "payload": payload}


def _string_list(value: Any, label: str, *, allow_empty: bool = True) -> list:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ProviderSetupError(f"invalid {label}")
    for item in value:
        _line(item, label + " item")
    if len(set(value)) != len(value):
        raise ProviderSetupError(f"duplicate {label} item")
    return value


def _https_base(value: Any, label: str) -> str:
    result = _line(value, label)
    parts = urlsplit(result)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ProviderSetupError(f"invalid {label}")
    return result.rstrip("/")


def _validate_lifecycle_rule(value: Any) -> Dict[str, Any]:
    item = _exact(
        value, ("id", "prefix", "enabled", "expiration_days"),
        "lifecycle rule",
    )
    _identifier(item["id"], "lifecycle rule id")
    if not isinstance(item["prefix"], str):
        raise ProviderSetupError("invalid lifecycle rule prefix")
    if not isinstance(item["enabled"], bool):
        raise ProviderSetupError("invalid lifecycle rule enabled")
    days = item["expiration_days"]
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ProviderSetupError("invalid lifecycle expiration days")
    return copy.deepcopy(item)


def _validate_cors_rule(value: Any) -> Dict[str, Any]:
    item = _exact(
        value,
        (
            "id", "allowed_origins", "allowed_methods", "allowed_headers",
            "expose_headers", "max_age_seconds",
        ),
        "CORS rule",
    )
    _identifier(item["id"], "CORS rule id")
    origins = _string_list(item["allowed_origins"], "CORS origins", allow_empty=False)
    for origin in origins:
        if origin != "*":
            parts = urlsplit(origin)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ProviderSetupError("invalid CORS origin")
    methods = _string_list(item["allowed_methods"], "CORS methods", allow_empty=False)
    if any(method not in {"GET", "HEAD", "PUT", "POST", "DELETE"} for method in methods):
        raise ProviderSetupError("invalid CORS method")
    _string_list(item["allowed_headers"], "CORS allowed headers")
    _string_list(item["expose_headers"], "CORS expose headers")
    age = item["max_age_seconds"]
    if not isinstance(age, int) or isinstance(age, bool) or age < 1:
        raise ProviderSetupError("invalid CORS max age")
    return copy.deepcopy(item)


def _validate_public_policy_rule(value: Any) -> Dict[str, Any]:
    item = _exact(
        value,
        ("id", "effect", "principal", "actions", "resource"),
        "public policy rule",
    )
    _identifier(item["id"], "public policy rule id")
    if item["effect"] not in {"allow", "deny"}:
        raise ProviderSetupError("invalid public policy rule effect")
    _line(item["principal"], "public policy rule principal")
    _string_list(
        item["actions"], "public policy rule actions", allow_empty=False,
    )
    _line(item["resource"], "public policy rule resource")
    return copy.deepcopy(item)


def _validate_public_policy_rules(value: Any) -> list:
    if not isinstance(value, list):
        raise ProviderSetupError("invalid public policy rules")
    result = [_validate_public_policy_rule(row) for row in value]
    ids = [row["id"] for row in result]
    if len(ids) != len(set(ids)):
        raise ProviderSetupError("duplicate public policy rule id")
    return result


def _validate_rules(value: Any, kind: str) -> list:
    if not isinstance(value, list):
        raise ProviderSetupError(f"invalid {kind} rules")
    parser = _validate_lifecycle_rule if kind == "lifecycle" else _validate_cors_rule
    result = [parser(row) for row in value]
    ids = [row["id"] for row in result]
    if len(ids) != len(set(ids)):
        raise ProviderSetupError(f"duplicate {kind} rule id")
    return result


def _prefixes_overlap(first: str, second: str) -> bool:
    return first.startswith(second) or second.startswith(first)


def _cors_rules_overlap(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_origins = set(first["allowed_origins"])
    second_origins = set(second["allowed_origins"])
    origin_overlap = "*" in first_origins or "*" in second_origins or bool(
        first_origins & second_origins
    )
    return origin_overlap and bool(
        set(first["allowed_methods"]) & set(second["allowed_methods"])
    )


def _managed_id(provider: str, purpose: str, material: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonicalize(material)).hexdigest()[:20]
    return f"s3-upload-{provider}-{purpose}-{digest}"


_CONTROL_CHANGES = MappingProxyType({
    "create-dedicated-bucket": ({
        "control": "bucket",
        "before": "absent",
        "after": "create-private-dedicated",
    },),
    "apply-new-bucket-public-read": ({
        "control": "bucket-policy",
        "before": "preserve-unrelated",
        "after": "append-managed-prefix-read",
    },),
    "apply-prefix-public-read": ({
        "control": "bucket-policy",
        "before": "preserve-unrelated",
        "after": "append-managed-prefix-read",
    },),
    "merge-prefix-lifecycle": ({
        "control": "lifecycle-rules",
        "before": "preserve-unrelated",
        "after": "append-managed-prefix-expiry",
    },),
    "merge-bucket-cors": ({
        "control": "cors-rules",
        "before": "preserve-unrelated",
        "after": "append-managed-cors",
    },),
    "issue-long-lived-access-key": ({
        "control": "sub-identity-access-key",
        "before": "absent",
        "after": "issue-once-and-deliver-to-bound-sink",
    },),
})


@dataclass(frozen=True)
class ProviderSetupContract:
    provider: str
    region_policy_class: str
    contract_id: str
    surface_version: str
    registry_revision: str
    observation_schema: Mapping[str, Any]
    actions: Mapping[str, Mapping[str, Any]]
    action_sources: Mapping[str, Tuple[str, ...]]
    source_ids: Tuple[str, ...]
    fixture_kind: str = FIXTURE_KIND
    remote_evidence: str = REMOTE_EVIDENCE

    def registry_contract(
        self, action_types: Sequence[str], *, mode: str = "test-only",
    ) -> Dict[str, Any]:
        if mode != "test-only":
            raise ProviderSetupError("normal assisted setup is unavailable")
        if not isinstance(action_types, (list, tuple)) or not action_types:
            raise ProviderSetupError("setup action types must be a non-empty array")
        rows = []
        seen = set()
        for action_type in action_types:
            _identifier(action_type, "setup action type")
            if action_type in seen:
                raise ProviderSetupError("setup action types must be unique")
            seen.add(action_type)
            row = self.actions.get(action_type)
            if row is None or row["state"] == "disabled":
                raise ProviderSetupError(f"unknown or disabled setup action: {action_type}")
            if row["state"] != "test-only":
                raise ProviderSetupError("candidate setup action is not test-only")
            rows.append(copy.deepcopy(dict(row)))
        return {
            "provider": self.provider,
            "region_policy_class": self.region_policy_class,
            "surface_version": self.surface_version,
            "registry_revision": self.registry_revision,
            "contract_id": self.contract_id,
            "action_contracts": rows,
        }

    def canonical_scope(self, observation_payload: Any) -> Dict[str, str]:
        payload = self._validate_observation_payload(observation_payload)
        return {
            "account": payload["account"],
            "region": payload["region"],
            "bucket": payload["bucket"],
            "prefix": payload["prefix"],
        }

    def observation_digest(self, value: Mapping[str, Any]) -> str:
        validated = self.validate_payload_envelope(
            value, self.observation_schema, "provider setup observation",
        )
        material = {
            "contract_id": self.contract_id,
            "surface_version": self.surface_version,
            "registry_revision": self.registry_revision,
            "value": validated,
        }
        return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()

    def validate_payload_envelope(
        self, value: Any, expected_schema: Mapping[str, Any], purpose: str,
    ) -> Dict[str, Any]:
        item = _exact(value, ("schema", "payload"), purpose)
        schema = _validate_schema_ref(item["schema"], purpose + " schema")
        if schema != dict(expected_schema):
            raise ProviderSetupError(
                f"wrong-purpose or unregistered schema for {purpose}",
            )
        schema_id = schema["id"]
        if schema_id == self.observation_schema["id"]:
            payload = self._validate_observation_payload(item["payload"])
        elif schema_id.endswith(".resource-scope"):
            payload = self._validate_resource_scope(item["payload"])
        elif schema_id.endswith(".mutation"):
            payload = self._validate_mutation(item["payload"])
        elif schema_id.endswith(".diff"):
            payload = self._validate_diff(item["payload"])
        elif schema_id.endswith(".success"):
            payload = self._validate_success(item["payload"])
        elif schema_id.endswith(".recovery"):
            payload = self._validate_recovery(item["payload"])
        else:
            raise ProviderSetupError(
                f"wrong-purpose or unregistered schema for {purpose}",
            )
        return {"schema": schema, "payload": payload}

    def _validate_observation_payload(self, value: Any) -> Dict[str, Any]:
        item = _exact(
            value,
            (
                "account", "region", "bucket", "bucket_identity", "prefix",
                "surface_marker", "state", "dedicated", "new_bucket",
                "prefix_empty", "prefix_overlap", "public_access_change_scope",
                "account_public_access", "bucket_public_access",
                "public_base_url", "public_policy_rules", "lifecycle_rules",
                "cors_rules",
            ),
            self.provider + " observation payload",
        )
        for key in ("account", "region", "bucket", "bucket_identity"):
            _line(item[key], key)
        if not isinstance(item["prefix"], str):
            raise ProviderSetupError("invalid observation prefix")
        if item["surface_marker"] != self.surface_version:
            raise ProviderSetupError("provider setup surface marker is stale")
        if item["state"] not in {"absent", "present"}:
            raise ProviderSetupError("invalid provider resource state")
        for key in (
            "dedicated", "new_bucket", "prefix_empty", "prefix_overlap",
        ):
            if not isinstance(item[key], bool):
                raise ProviderSetupError(f"invalid {key}")
        if item["public_access_change_scope"] not in {
            "none", "new-bucket", "prefix", "bucket-wide", "account-level",
        }:
            raise ProviderSetupError("invalid public access change scope")
        for key in ("account_public_access", "bucket_public_access"):
            if item[key] not in {"allows-public", "blocks-public", "unknown"}:
                raise ProviderSetupError(f"invalid {key}")
        if item["public_base_url"] is not None:
            _https_base(item["public_base_url"], "Public Base URL")
        _validate_public_policy_rules(item["public_policy_rules"])
        _validate_rules(item["lifecycle_rules"], "lifecycle")
        _validate_rules(item["cors_rules"], "cors")
        if self.provider == "tencent-cos" and not COS_BUCKET_RE.fullmatch(item["bucket"]):
            raise ProviderSetupError("COS bucket must be complete BucketName-APPID")
        return copy.deepcopy(item)

    def _validate_resource_scope(self, value: Any) -> Dict[str, Any]:
        item = _exact(value, ("account", "region", "bucket", "prefix"), "resource scope")
        for key in ("account", "region", "bucket"):
            _line(item[key], "resource scope " + key)
        if not isinstance(item["prefix"], str):
            raise ProviderSetupError("invalid resource scope prefix")
        return copy.deepcopy(item)

    def _validate_mutation(self, value: Any) -> Dict[str, Any]:
        item = _exact(value, ("operation", "parameters", "source_ids"), "mutation")
        operation = _identifier(item["operation"], "mutation operation")
        if operation not in self.actions or self.actions[operation]["state"] == "disabled":
            raise ProviderSetupError("mutation action is unavailable")
        parameters = item["parameters"]
        if operation == "create-dedicated-bucket":
            parameters = _exact(
                parameters, ("bucket", "region", "dedicated", "access"),
                "create bucket mutation parameters",
            )
            _line(parameters["bucket"], "create bucket name")
            _line(parameters["region"], "create bucket region")
            if parameters["dedicated"] is not True or parameters["access"] != (
                "private-before-reviewed-public-change"
            ):
                raise ProviderSetupError("invalid create bucket mutation parameters")
        elif operation in {
            "apply-new-bucket-public-read", "apply-prefix-public-read",
        }:
            parameters = _exact(
                parameters,
                ("bucket", "prefix", "public_base_url", "scope"),
                "public-read mutation parameters",
            )
            _line(parameters["bucket"], "public-read bucket")
            if not isinstance(parameters["prefix"], str):
                raise ProviderSetupError("invalid public-read prefix")
            _https_base(parameters["public_base_url"], "public-read Public Base URL")
            expected_scope = (
                "new-bucket"
                if operation == "apply-new-bucket-public-read"
                else "prefix"
            )
            if parameters["scope"] != expected_scope:
                raise ProviderSetupError("invalid public-read mutation scope")
        elif operation == "merge-prefix-lifecycle":
            parameters = _exact(
                parameters, ("managed_rule",),
                "lifecycle mutation parameters",
            )
            _validate_lifecycle_rule(parameters["managed_rule"])
        elif operation == "merge-bucket-cors":
            parameters = _exact(
                parameters, ("managed_rule",), "CORS mutation parameters",
            )
            _validate_cors_rule(parameters["managed_rule"])
        else:
            parameters = _exact(
                parameters,
                (
                    "identity_type", "credential_type", "session_token",
                    "expires_at",
                ),
                "credential issuance mutation parameters",
            )
            if parameters != {
                "identity_type": "least-privilege-sub-identity",
                "credential_type": "long-lived-access-key",
                "session_token": "",
                "expires_at": None,
            }:
                raise ProviderSetupError(
                    "invalid credential issuance mutation parameters",
                )
        sources = _string_list(item["source_ids"], "mutation source ids", allow_empty=False)
        if tuple(sources) != self.action_sources[operation]:
            raise ProviderSetupError("mutation source binding is stale")
        return copy.deepcopy(item)

    def _validate_diff(self, value: Any) -> Dict[str, Any]:
        item = _exact(
            value,
            (
                "summary", "scope", "before_rules", "after_rules",
                "control_changes",
            ),
            "provider setup diff",
        )
        action_type = _identifier(item["summary"], "diff summary")
        if (
            action_type not in self.actions
            or action_type not in _CONTROL_CHANGES
            or self.actions[action_type]["state"] == "disabled"
        ):
            raise ProviderSetupError("diff action is unavailable")
        if item["scope"] not in {"new-bucket", "prefix", "bucket-wide", "identity"}:
            raise ProviderSetupError("invalid provider setup diff scope")
        if not isinstance(item["before_rules"], list) or not isinstance(item["after_rules"], list):
            raise ProviderSetupError("provider setup diff rules must be arrays")
        if action_type in {
            "apply-new-bucket-public-read", "apply-prefix-public-read",
        }:
            _validate_public_policy_rules(item["before_rules"])
            _validate_public_policy_rules(item["after_rules"])
        elif action_type == "merge-prefix-lifecycle":
            _validate_rules(item["before_rules"], "lifecycle")
            _validate_rules(item["after_rules"], "lifecycle")
        elif action_type == "merge-bucket-cors":
            _validate_rules(item["before_rules"], "cors")
            _validate_rules(item["after_rules"], "cors")
        elif item["before_rules"] or item["after_rules"]:
            raise ProviderSetupError("non-merge diff cannot carry rule arrays")
        expected_changes = [
            copy.deepcopy(dict(change))
            for change in _CONTROL_CHANGES[action_type]
        ]
        if item["control_changes"] != expected_changes:
            raise ProviderSetupError("invalid provider setup control changes")
        return copy.deepcopy(item)

    def _validate_success(self, value: Any) -> Dict[str, Any]:
        item = _exact(value, ("state", "checks"), "provider setup success")
        if item["state"] != "present":
            raise ProviderSetupError("invalid provider setup success state")
        _string_list(item["checks"], "provider setup success checks", allow_empty=False)
        return copy.deepcopy(item)

    def _validate_recovery(self, value: Any) -> Dict[str, Any]:
        item = _exact(value, ("retry", "rollback", "manual_steps"), "provider recovery")
        if item["retry"] != "never" or item["rollback"] != "manual":
            raise ProviderSetupError("invalid provider setup recovery boundary")
        _string_list(item["manual_steps"], "provider recovery steps", allow_empty=False)
        return copy.deepcopy(item)

    def _target(self, request: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict) or request.get("provider") != self.provider:
            raise ProviderSetupError("setup request provider mismatch")
        target = request.get("proposed_target")
        if not isinstance(target, dict) or target.get("provider") != self.provider:
            raise ProviderSetupError("proposed Target provider mismatch")
        for key in ("region", "bucket", "prefix"):
            if target.get(key) != payload[key]:
                raise ProviderSetupError("proposed Target bucket identity or scope mismatch")
        if payload["bucket_identity"] != payload["bucket"]:
            raise ProviderSetupError("observed bucket identity mismatch")
        addressing = target.get("addressing") or "virtual"
        if addressing != "virtual":
            raise ProviderSetupError("provider setup requires virtual-hosted addressing")
        access = target.get("access")
        if not isinstance(access, dict) or access.get("mode") != "public":
            raise ProviderSetupError("provider setup candidate requires public Access Mode")
        if access.get("public_base_url") != payload["public_base_url"]:
            raise ProviderSetupError("Public Base URL does not match observed bucket identity")
        endpoint = target.get("endpoint")
        if endpoint is None:
            endpoint = (
                f"https://s3.oss-{payload['region']}.aliyuncs.com"
                if self.provider == "aliyun-oss"
                else f"https://cos.{payload['region']}.myqcloud.com"
            )
        if self.provider == "aliyun-oss":
            if payload["region"].startswith("cn-") and endpoint == (
                f"https://s3.oss-{payload['region']}.aliyuncs.com"
            ):
                raise ProviderSetupError(
                    "PublicEndpointForbidden cannot be repaired by a Public Base URL",
                )
            expected_base = (
                f"https://{payload['bucket']}.oss-{payload['region']}.aliyuncs.com"
            )
            if payload["public_base_url"] != expected_base:
                raise ProviderSetupError(
                    "OSS Public Base URL does not match bucket identity",
                )
        else:
            if not COS_BUCKET_RE.fullmatch(target.get("bucket", "")):
                raise ProviderSetupError("COS bucket identity must be complete BucketName-APPID")
            expected_endpoint = f"https://cos.{payload['region']}.myqcloud.com"
            if endpoint != expected_endpoint:
                raise ProviderSetupError("COS service endpoint does not match bucket identity")
            expected_base = f"https://{payload['bucket']}.cos.{payload['region']}.myqcloud.com"
            if payload["public_base_url"] != expected_base:
                raise ProviderSetupError("COS Public Base URL does not match bucket identity")
        return target

    def _require_existing_prefix(self, payload: Mapping[str, Any]) -> None:
        if (
            not payload["dedicated"]
            or payload["new_bucket"]
            or payload["state"] != "present"
            or not payload["prefix"]
            or not payload["prefix_empty"]
            or payload["prefix_overlap"]
            or payload["public_access_change_scope"] != "prefix"
            or payload["account_public_access"] != "allows-public"
            or payload["bucket_public_access"] != "allows-public"
        ):
            raise ProviderSetupError("existing prefix is not safe for automatic setup")

    def _require_new_bucket(self, payload: Mapping[str, Any]) -> None:
        if (
            not payload["dedicated"]
            or not payload["new_bucket"]
            or payload["state"] != "absent"
            or payload["public_access_change_scope"] != "new-bucket"
        ):
            raise ProviderSetupError("new dedicated bucket precondition is not satisfied")

    def _merge_lifecycle(
        self, payload: Mapping[str, Any], target: Mapping[str, Any],
    ) -> Tuple[list, list, Dict[str, Any]]:
        retention = target.get("retention")
        if (
            not isinstance(retention, dict)
            or retention.get("mode") != "expire"
            or not isinstance(retention.get("days"), int)
            or isinstance(retention.get("days"), bool)
            or retention["days"] < 1
        ):
            raise ProviderSetupError("lifecycle action requires positive expiring retention")
        prefix = payload["prefix"]
        if not prefix:
            raise ProviderSetupError("lifecycle action requires an exclusive non-empty prefix")
        if payload["new_bucket"]:
            self._require_new_bucket(payload)
        else:
            self._require_existing_prefix(payload)
        before = _validate_rules(payload["lifecycle_rules"], "lifecycle")
        desired = {
            "id": _managed_id(
                self.provider, "expire",
                {"bucket": payload["bucket"], "prefix": prefix, "days": retention["days"]},
            ),
            "prefix": prefix,
            "enabled": True,
            "expiration_days": retention["days"],
        }
        after = []
        found = False
        for row in before:
            if row["id"] == desired["id"]:
                if row != desired:
                    raise ProviderSetupError("managed lifecycle rule changed unexpectedly")
                found = True
            elif _prefixes_overlap(row["prefix"], prefix):
                raise ProviderSetupError("lifecycle rule overlap requires manual review")
            after.append(copy.deepcopy(row))
        if not found:
            after.append(desired)
        return before, after, desired

    def _merge_cors(
        self, payload: Mapping[str, Any], target: Mapping[str, Any],
    ) -> Tuple[list, list, Dict[str, Any]]:
        self._require_new_bucket(payload)
        setup = target.get("setup")
        cors = setup.get("cors") if isinstance(setup, dict) else None
        if not isinstance(cors, dict):
            raise ProviderSetupError("CORS action requires a complete Target CORS rule")
        desired = {
            "id": _managed_id(
                self.provider, "cors",
                {"bucket": payload["bucket"], "cors": cors},
            ),
            "allowed_origins": copy.deepcopy(cors.get("allowed_origins")),
            "allowed_methods": copy.deepcopy(cors.get("allowed_methods")),
            "allowed_headers": copy.deepcopy(cors.get("allowed_headers")),
            "expose_headers": copy.deepcopy(cors.get("expose_headers")),
            "max_age_seconds": cors.get("max_age_seconds"),
        }
        desired = _validate_cors_rule(desired)
        before = _validate_rules(payload["cors_rules"], "cors")
        after = []
        found = False
        for row in before:
            if row["id"] == desired["id"]:
                if row != desired:
                    raise ProviderSetupError("managed CORS rule changed unexpectedly")
                found = True
            elif _cors_rules_overlap(row, desired):
                raise ProviderSetupError("CORS rule overlap requires manual review")
            after.append(copy.deepcopy(row))
        if not found:
            after.append(desired)
        return before, after, desired

    def _public_policy_resource(self, payload: Mapping[str, Any]) -> str:
        suffix = f"{payload['bucket']}/{payload['prefix']}*"
        if self.provider == "aliyun-oss":
            return f"acs:oss:*:{payload['account']}:{suffix}"
        appid = payload["bucket"].rsplit("-", 1)[1]
        return (
            f"qcs::cos:{payload['region']}:uid/{appid}:"
            f"{suffix}"
        )

    def _policy_object_prefix(
        self, resource: str, payload: Mapping[str, Any],
    ) -> Optional[str]:
        bucket_prefix = payload["bucket"] + "/"
        if resource.startswith(bucket_prefix):
            candidate = resource[len(bucket_prefix):]
        elif self.provider == "aliyun-oss" and resource.startswith("acs:oss:"):
            marker = ":" + bucket_prefix
            if marker not in resource:
                return None
            candidate = resource.split(marker, 1)[1]
        elif self.provider == "tencent-cos" and resource.startswith("qcs::cos:"):
            marker = ":" + bucket_prefix
            if marker not in resource:
                return None
            candidate = resource.split(marker, 1)[1]
        elif ":" not in resource:
            candidate = resource
        else:
            return None
        if "*" in candidate[:-1] or "?" in candidate:
            return ""
        return candidate[:-1] if candidate.endswith("*") else candidate

    def _merge_public_policy(
        self, payload: Mapping[str, Any],
    ) -> Tuple[list, list, Dict[str, Any]]:
        resource = self._public_policy_resource(payload)
        desired = {
            "id": _managed_id(
                self.provider,
                "public-read",
                {
                    "account": payload["account"],
                    "region": payload["region"],
                    "bucket": payload["bucket"],
                    "prefix": payload["prefix"],
                    "resource": resource,
                },
            ),
            "effect": "allow",
            "principal": "*",
            "actions": ["GetObject"],
            "resource": resource,
        }
        desired = _validate_public_policy_rule(desired)
        desired_prefix = payload["prefix"]
        before = _validate_public_policy_rules(payload["public_policy_rules"])
        after = []
        found = False
        for row in before:
            if row["id"] == desired["id"]:
                if row != desired:
                    raise ProviderSetupError(
                        "managed public policy rule changed unexpectedly",
                    )
                found = True
            elif (
                row["principal"] == "*"
                and any(action == "GetObject" or action.endswith(":GetObject")
                        for action in row["actions"])
            ):
                existing_prefix = self._policy_object_prefix(
                    row["resource"], payload,
                )
                if existing_prefix is not None and _prefixes_overlap(
                    existing_prefix, desired_prefix,
                ):
                    raise ProviderSetupError(
                        "public policy rule overlap requires manual review",
                    )
            after.append(copy.deepcopy(row))
        if not found:
            after.append(desired)
        return before, after, desired

    def build_action(
        self, action_type: str, observation_payload: Any,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        row = self.actions.get(action_type)
        if row is None or row["state"] != "test-only":
            raise ProviderSetupError(f"unknown or disabled setup action: {action_type}")
        payload = self._validate_observation_payload(observation_payload)
        target = self._target(request, payload)
        before_rules = []
        after_rules = []
        if action_type == "create-dedicated-bucket":
            self._require_new_bucket(payload)
            scope = "new-bucket"
            parameters = {
                "bucket": payload["bucket"], "region": payload["region"],
                "dedicated": True, "access": "private-before-reviewed-public-change",
            }
            checks = ["bucket identity and region exactly match the confirmed plan"]
        elif action_type == "apply-new-bucket-public-read":
            self._require_new_bucket(payload)
            if payload["public_base_url"] is None:
                raise ProviderSetupError("public setup requires a Public Base URL")
            before_rules, after_rules, _ = self._merge_public_policy(payload)
            scope = "new-bucket"
            parameters = {
                "bucket": payload["bucket"], "prefix": payload["prefix"],
                "public_base_url": payload["public_base_url"], "scope": scope,
            }
            checks = ["anonymous GET is limited to the confirmed new dedicated bucket"]
        elif action_type == "apply-prefix-public-read":
            self._require_existing_prefix(payload)
            if payload["public_base_url"] is None:
                raise ProviderSetupError("public setup requires a Public Base URL")
            before_rules, after_rules, _ = self._merge_public_policy(payload)
            scope = "prefix"
            parameters = {
                "bucket": payload["bucket"], "prefix": payload["prefix"],
                "public_base_url": payload["public_base_url"], "scope": scope,
            }
            checks = ["anonymous GET is limited to the confirmed exclusive prefix"]
        elif action_type == "merge-prefix-lifecycle":
            before_rules, after_rules, desired = self._merge_lifecycle(payload, target)
            scope = "prefix"
            parameters = {"managed_rule": desired}
            checks = ["managed lifecycle rule is present and unrelated rules are unchanged"]
        elif action_type == "merge-bucket-cors":
            before_rules, after_rules, desired = self._merge_cors(payload, target)
            scope = "bucket-wide"
            parameters = {"managed_rule": desired}
            checks = ["managed CORS rule is present and unrelated rules are unchanged"]
        else:
            if request.get("credential_source_category") != "planned-issuance":
                raise ProviderSetupError("credential issuance requires planned-issuance")
            if request.get("credential_persistence") not in {"project", "global"}:
                raise ProviderSetupError("credential issuance requires persistent scope")
            scope = "identity"
            parameters = {
                "identity_type": "least-privilege-sub-identity",
                "credential_type": "long-lived-access-key",
                "session_token": "",
                "expires_at": None,
            }
            checks = ["one-time Access Key pair was delivered only through the bound sink"]
        built = {
            "resource_scope": _envelope(
                row["resource_scope_schema"], self.canonical_scope(payload),
            ),
            "mutation": _envelope(row["mutation_schema"], {
                "operation": action_type,
                "parameters": parameters,
                "source_ids": list(self.action_sources[action_type]),
            }),
            "diff": _envelope(row["diff_schema"], {
                "summary": action_type,
                "scope": scope,
                "before_rules": before_rules,
                "after_rules": after_rules,
                "control_changes": [
                    copy.deepcopy(dict(change))
                    for change in _CONTROL_CHANGES[action_type]
                ],
            }),
            "expected_success": _envelope(row["success_schema"], {
                "state": "present", "checks": checks,
            }),
            "recovery_limits": _envelope(row["recovery_schema"], {
                "retry": "never",
                "rollback": "manual",
                "manual_steps": [
                    "manual revoke-and-reissue is required if credential issuance or local installation is unknown"
                    if action_type == "issue-long-lived-access-key"
                    else "inspect the exact provider resource before creating a new confirmed plan"
                ],
            }),
        }
        for field_name, contract_name in (
            ("resource_scope", "resource_scope_schema"),
            ("mutation", "mutation_schema"),
            ("diff", "diff_schema"),
            ("expected_success", "success_schema"),
            ("recovery_limits", "recovery_schema"),
        ):
            self.validate_payload_envelope(
                built[field_name], row[contract_name], field_name,
            )
        return built

    def fixture_extension(self):
        from setup_adapters import FixtureContractExtension

        return FixtureContractExtension(
            provider=self.provider,
            contract_id=self.contract_id,
            surface_version=self.surface_version,
            registry_revision=self.registry_revision,
            observation_schema=dict(self.observation_schema),
            observation_validator=lambda value, purpose: self.validate_payload_envelope(
                value, self.observation_schema, purpose,
            ),
            credential_delivery_fields=("access_key_id", "secret_access_key"),
        )


def _contract(identity: Mapping[str, str], source_ids: Sequence[str], action_sources):
    provider = identity["provider"]
    schema_prefix = provider + ".setup"
    schemas = {
        name: _schema_ref(schema_prefix + "." + name)
        for name in (
            "observation", "resource-scope", "mutation", "diff", "success",
            "recovery",
        )
    }
    rows = {}
    for action_type in TEST_ONLY_ACTIONS + DISABLED_ACTIONS:
        state = "test-only" if action_type in TEST_ONLY_ACTIONS else "disabled"
        evidence_action = (
            "issue-persistent-access-key"
            if action_type == "issue-long-lived-access-key"
            else action_type
        )
        row = {
            "action_type": action_type,
            "state": state,
            "evidence_id": (
                f"{provider}-setup-hypothesis-{evidence_action}-v1"
                if state == "test-only"
                else f"{provider}-setup-disabled-{action_type}-v1"
            ),
            "observation_schema": schemas["observation"],
            "resource_scope_schema": schemas["resource-scope"],
            "mutation_schema": schemas["mutation"],
            "diff_schema": schemas["diff"],
            "success_schema": schemas["success"],
            "recovery_schema": schemas["recovery"],
        }
        rows[action_type] = MappingProxyType(row)
    return ProviderSetupContract(
        provider=provider,
        region_policy_class=identity["region_policy_class"],
        contract_id=identity["contract_id"],
        surface_version=identity["surface_version"],
        registry_revision=identity["registry_revision"],
        observation_schema=MappingProxyType(schemas["observation"]),
        actions=MappingProxyType(rows),
        action_sources=MappingProxyType({
            key: tuple(value) for key, value in action_sources.items()
        }),
        source_ids=tuple(source_ids),
    )


_OSS_SOURCES = (
    "aliyun-oss-create-bucket",
    "aliyun-oss-bucket-policy",
    "aliyun-oss-bucket-domain",
    "aliyun-oss-block-public-access",
    "aliyun-oss-lifecycle-prefix-merge",
    "aliyun-oss-cors",
    "aliyun-ram-access-key",
    "aliyun-ram-least-privilege",
    "aliyun-oss-sts",
    "aliyun-oss-public-endpoint-policy",
)

_COS_SOURCES = (
    "tencent-cos-create-bucket",
    "tencent-cos-bucket-identity",
    "tencent-cos-bucket-policy",
    "tencent-cos-lifecycle-prefix-merge",
    "tencent-cos-cors",
    "tencent-cam-sub-user",
    "tencent-cam-access-key",
    "tencent-cos-least-privilege",
    "tencent-sts-assume-role",
    "tencent-cos-region-domain",
)

_OSS_ACTION_SOURCES = {
    "create-dedicated-bucket": ("aliyun-oss-create-bucket",),
    "apply-new-bucket-public-read": (
        "aliyun-oss-create-bucket", "aliyun-oss-block-public-access",
        "aliyun-oss-bucket-domain",
    ),
    "apply-prefix-public-read": (
        "aliyun-oss-bucket-policy", "aliyun-oss-block-public-access",
        "aliyun-oss-bucket-domain",
    ),
    "merge-prefix-lifecycle": ("aliyun-oss-lifecycle-prefix-merge",),
    "merge-bucket-cors": ("aliyun-oss-cors",),
    "issue-long-lived-access-key": (
        "aliyun-ram-access-key", "aliyun-ram-least-privilege",
    ),
    "change-account-public-access": ("aliyun-oss-block-public-access",),
    "change-existing-bucket-wide-public-access": (
        "aliyun-oss-block-public-access",
    ),
}

_COS_ACTION_SOURCES = {
    "create-dedicated-bucket": (
        "tencent-cos-create-bucket", "tencent-cos-bucket-identity",
    ),
    "apply-new-bucket-public-read": (
        "tencent-cos-create-bucket", "tencent-cos-bucket-policy",
    ),
    "apply-prefix-public-read": ("tencent-cos-bucket-policy",),
    "merge-prefix-lifecycle": ("tencent-cos-lifecycle-prefix-merge",),
    "merge-bucket-cors": ("tencent-cos-cors",),
    "issue-long-lived-access-key": (
        "tencent-cam-sub-user", "tencent-cam-access-key",
        "tencent-cos-least-privilege",
    ),
    "change-account-public-access": ("tencent-cos-bucket-policy",),
    "change-existing-bucket-wide-public-access": (
        "tencent-cos-bucket-policy",
    ),
}

ALIYUN_OSS_SETUP_CONTRACT = _contract(
    ALIYUN_OSS_SETUP_IDENTITY, _OSS_SOURCES, _OSS_ACTION_SOURCES,
)
TENCENT_COS_SETUP_CONTRACT = _contract(
    TENCENT_COS_SETUP_IDENTITY, _COS_SOURCES, _COS_ACTION_SOURCES,
)

_CONTRACTS = {
    (
        contract.provider, contract.contract_id, contract.surface_version,
        contract.registry_revision,
    ): contract
    for contract in (ALIYUN_OSS_SETUP_CONTRACT, TENCENT_COS_SETUP_CONTRACT)
}


def lookup_setup_contract(
    *, provider: Any, contract_id: Any, surface_version: Any,
    registry_revision: Any,
) -> Optional[ProviderSetupContract]:
    values = (provider, contract_id, surface_version, registry_revision)
    if any(not isinstance(value, str) or not SETUP_ID_RE.fullmatch(value) for value in values):
        return None
    return _CONTRACTS.get(values)


def fixture_extension_for(
    provider: Any, contract_id: Any, surface_version: Any,
    registry_revision: Any,
):
    contract = lookup_setup_contract(
        provider=provider,
        contract_id=contract_id,
        surface_version=surface_version,
        registry_revision=registry_revision,
    )
    return None if contract is None else contract.fixture_extension()
