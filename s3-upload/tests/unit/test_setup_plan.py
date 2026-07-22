from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from setup_contracts import validate_setup_plan
from provider_setup_candidates import (
    ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY,
    lookup_setup_contract,
)
from setup_plan import (
    PlanningContext, build_setup_plan, preflight_plan_sink, publish_setup_plan,
    read_setup_input,
)
from strict_json import canonicalize


PLAN_ID = "123e4567-e89b-42d3-a456-426614174000"
ACCESS_KEY = "PROJECTKEY1234"
SECRET = "project-secret-value"


def credential():
    return {
        "access_key_id": ACCESS_KEY,
        "secret_access_key": SECRET,
        "session_token": "",
        "expires_at": None,
    }


def proposed_target():
    return {
        "schema_version": 1,
        "credential": "project:setup-key",
        "provider": "custom",
        "region": "test-1",
        "endpoint": "https://s3.example.test",
        "addressing": "virtual",
        "bucket": "setup-bucket",
        "prefix": "uploads/",
        "access": {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 3600,
        },
        "retention": {"mode": "retain", "days": None},
        "collision": "replace",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": None,
            "part_size_bytes": None,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": False, "integration_test": False, "cors": None},
    }


def setup_request(*, source="persistent-existing", persistence="project", actions=None):
    return {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-request",
        "mode": "test-only",
        "provider": "custom",
        "account_hint": "account-1",
        "target_ref": "project:setup-target",
        "credential_ref": "project:setup-key",
        "credential_source_category": source,
        "credential_persistence": persistence,
        "proposed_target": proposed_target(),
        "requested_action_types": actions or ["create-bucket"],
        "selector_change": {
            "kind": "project-default",
            "caller_skill": None,
            "before": None,
            "after": "project:setup-target",
        },
    }


def setup_observation(*, state="absent"):
    return {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-observation",
        "provider": "custom",
        "contract_id": "generic.console.v1",
        "surface_version": "synthetic.v1",
        "registry_revision": "generic.v1",
        "observation": {
            "schema": {"id": "generic.observation", "version": 1},
            "payload": {
                "account": "account-1",
                "region": "test-1",
                "bucket": "setup-bucket",
                "prefix": "uploads/",
                "surface_marker": "synthetic-v1",
                "dedicated": True,
                "new_bucket": True,
                "prefix_empty": True,
                "prefix_overlap": False,
                "public_base_url": "https://cdn.example.test",
                "state": state,
            },
        },
    }


def planning_context(tmp_path: Path, *, environ=None):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON="
        + json.dumps({"setup-key": credential()}, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return PlanningContext(
        project_root=str(tmp_path),
        config_home=str(tmp_path / "home"),
        environ=environ or {},
        use_local_key=False,
    )


def test_builds_secret_free_registry_bound_plan_with_stable_hash(tmp_path):
    plan = build_setup_plan(
        setup_request(),
        setup_observation(),
        context=planning_context(tmp_path),
        plan_id_factory=lambda: PLAN_ID,
    )

    assert list(plan) == [
        "schema_version", "artifact_type", "mode", "plan_id", "plan_hash",
        "setup_contract", "authorization_scope", "observations", "actions",
        "local_install", "recovery_limits",
    ]
    unhashed = {key: value for key, value in plan.items() if key != "plan_hash"}
    assert plan["plan_hash"] == "sha256:" + hashlib.sha256(canonicalize(unhashed)).hexdigest()
    assert plan["setup_contract"]["action_contracts"][0]["state"] == "test-only"
    assert plan["authorization_scope"]["action_ids"] == ["action-1"]
    assert plan["actions"][0]["action_type"] == "create-bucket"
    assert plan["local_install"]["schema"] == {"id": "s3-upload.local-install", "version": 1}
    encoded = canonicalize(plan)
    assert SECRET.encode() not in encoded and ACCESS_KEY.encode() not in encoded
    assert b"secret_access_key" not in encoded
    assert validate_setup_plan(plan) == plan


def test_plan_cli_publishes_exact_bytes_and_rejects_process_map(tmp_path):
    context = planning_context(tmp_path)
    request_path = tmp_path / "request.json"
    observation_path = tmp_path / "observation.json"
    request_path.write_text(json.dumps(setup_request()), encoding="utf-8")
    observation_path.write_text(json.dumps(setup_observation()), encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts" / "setup.py"
    plan_path = tmp_path / "artifacts" / "plan.json"
    plan_path.parent.mkdir()
    environment = {
        **os.environ,
        "S3_UPLOAD_CONFIG_HOME": context.config_home,
    }

    completed = subprocess.run(
        [
            "python3", str(script), "plan",
            "--request-file", str(request_path),
            "--observation-file", str(observation_path),
            "--plan-out", str(plan_path),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == plan_path.read_bytes()
    assert not completed.stdout.endswith(b"\n")
    assert validate_setup_plan(json.loads(completed.stdout))
    assert SECRET.encode() not in completed.stdout

    blocked_path = tmp_path / "artifacts" / "blocked.json"
    environment["S3_UPLOAD_PROJECT_CREDENTIALS_JSON"] = json.dumps(
        {"setup-key": credential()}, separators=(",", ":")
    )
    blocked = subprocess.run(
        [
            "python3", str(script), "plan",
            "--request-file", str(request_path),
            "--observation-file", str(observation_path),
            "--plan-out", str(blocked_path),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
    )
    assert blocked.returncode == 2
    assert blocked.stdout == b""
    assert not blocked_path.exists()
    assert SECRET.encode() not in blocked.stderr


@pytest.mark.parametrize(
    ("observation_changes", "allowed"),
    [
        ({"dedicated": True, "new_bucket": True, "state": "absent"}, True),
        ({"dedicated": True, "new_bucket": False, "state": "present"}, True),
        ({"dedicated": False, "new_bucket": False, "state": "present"}, False),
        ({"dedicated": True, "new_bucket": False, "prefix_empty": False, "state": "present"}, False),
        ({"dedicated": True, "new_bucket": False, "prefix_overlap": True, "state": "present"}, False),
    ],
)
def test_unspecified_access_defaults_only_for_verified_dedicated_storage(
    tmp_path, observation_changes, allowed,
):
    request = setup_request()
    request["proposed_target"]["access"] = None
    request["proposed_target"]["setup"]["exclusive_prefix"] = True
    observation = setup_observation()
    observation["observation"]["payload"].update(observation_changes)

    if not allowed:
        with pytest.raises(ValueError):
            build_setup_plan(
                request, observation, context=planning_context(tmp_path),
                plan_id_factory=lambda: PLAN_ID,
            )
        return

    plan = build_setup_plan(
        request, observation, context=planning_context(tmp_path),
        plan_id_factory=lambda: PLAN_ID,
    )
    assert plan["local_install"]["payload"]["proposed_target"]["access"] == {
        "mode": "public",
        "public_base_url": "https://cdn.example.test",
        "presign_expires_seconds": None,
    }


def test_in_process_plan_records_only_an_opaque_process_credential_handle(tmp_path):
    request = setup_request(source="process-memory", persistence="this-run")
    environment = {
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON": json.dumps(
            {"setup-key": credential()}, separators=(",", ":"),
        ),
    }

    plan = build_setup_plan(
        request,
        setup_observation(),
        context=planning_context(tmp_path, environ=environment),
        plan_id_factory=lambda: PLAN_ID,
        credential_handle_id="handle-1",
    )

    local = plan["local_install"]["payload"]
    assert local["credential_handle_id"] == "handle-1"
    assert local["credential_slot"] == {
        "name": "setup-key",
        "state": "process-memory",
        "secret_file_role": "process-map",
        "version_token": None,
    }
    assert all(row["role"] != "credential" for row in local["file_snapshots"])
    encoded = canonicalize(plan)
    assert ACCESS_KEY.encode() not in encoded and SECRET.encode() not in encoded


def test_planned_issuance_requires_an_absent_persistent_slot(tmp_path):
    request = setup_request(
        source="planned-issuance",
        persistence="project",
        actions=["create-bucket", "issue-long-lived-access-key"],
    )
    request["credential_ref"] = "project:new-setup-key"
    request["proposed_target"]["credential"] = "project:new-setup-key"

    plan = build_setup_plan(
        request,
        setup_observation(),
        context=planning_context(tmp_path),
        plan_id_factory=lambda: PLAN_ID,
    )

    local = plan["local_install"]["payload"]
    assert local["credential_slot"]["name"] == "new-setup-key"
    assert local["credential_slot"]["state"] == "absent"
    assert plan["actions"][-1]["credential_delivery"] == {
        "fields": ["access_key_id", "secret_access_key"],
        "one_time": True,
        "destination": "project",
        "requires_memory_sink": True,
    }
    encoded = canonicalize(plan)
    assert b"SETUP-PLANNING-ONLY" not in encoded
    assert ACCESS_KEY.encode() not in encoded and SECRET.encode() not in encoded

    conflicting = setup_request(
        source="planned-issuance",
        persistence="project",
        actions=["issue-long-lived-access-key"],
    )
    with pytest.raises(ValueError):
        build_setup_plan(
            conflicting,
            setup_observation(),
            context=planning_context(tmp_path),
            plan_id_factory=lambda: PLAN_ID,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "resource-scope",
        "mutation-purpose",
        "diff-purpose",
        "target-provider",
        "credential-persistence",
        "credential-slot",
        "extra-observation",
    ],
)
def test_rejects_rehashed_plan_with_inconsistent_internal_projection(tmp_path, mutation):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: PLAN_ID,
    )
    changed = copy.deepcopy(plan)
    if mutation == "resource-scope":
        changed["actions"][0]["resource_scope"]["payload"]["bucket"] = "other-bucket"
    elif mutation == "mutation-purpose":
        changed["actions"][0]["mutation"]["payload"]["operation"] = "write-cors"
    elif mutation == "diff-purpose":
        changed["actions"][0]["diff"]["payload"]["summary"] = "write-cors"
    elif mutation == "target-provider":
        changed["local_install"]["payload"]["proposed_target"]["provider"] = "aws-s3"
    elif mutation == "credential-persistence":
        changed["authorization_scope"]["credential_persistence"] = "this-run"
        changed["local_install"]["payload"]["credential_persistence"] = "this-run"
    elif mutation == "credential-slot":
        changed["local_install"]["payload"]["credential_slot"]["state"] = "absent"
    else:
        extra = copy.deepcopy(changed["observations"][0])
        extra["observation_id"] = "extra-observation"
        changed["observations"].append(extra)
    unhashed = {key: value for key, value in changed.items() if key != "plan_hash"}
    changed["plan_hash"] = "sha256:" + hashlib.sha256(canonicalize(unhashed)).hexdigest()

    with pytest.raises(ValueError):
        validate_setup_plan(changed)


@pytest.mark.parametrize(
    "identity", [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_builds_provider_owned_candidate_setup_plan_without_generic_schema_fallback(
    tmp_path, identity,
):
    contract = lookup_setup_contract(
        provider=identity["provider"],
        contract_id=identity["contract_id"],
        surface_version=identity["surface_version"],
        registry_revision=identity["registry_revision"],
    )
    region = "ap-guangzhou" if contract.provider == "tencent-cos" else "us-west-1"
    bucket = (
        "website-assets-1250000000"
        if contract.provider == "tencent-cos"
        else "website-assets"
    )
    endpoint = (
        f"https://cos.{region}.myqcloud.com"
        if contract.provider == "tencent-cos"
        else f"https://s3.oss-{region}.aliyuncs.com"
    )
    public_base_url = (
        f"https://{bucket}.cos.{region}.myqcloud.com"
        if contract.provider == "tencent-cos"
        else f"https://{bucket}.oss-{region}.aliyuncs.com"
    )
    request = setup_request(actions=["apply-prefix-public-read"])
    request["provider"] = contract.provider
    request["account_hint"] = "account-10001"
    request["proposed_target"] = {
        **request["proposed_target"],
        "provider": contract.provider,
        "region": region,
        "endpoint": endpoint,
        "addressing": "virtual",
        "bucket": bucket,
        "prefix": "assets/",
        "access": {
            "mode": "public",
            "public_base_url": public_base_url,
            "presign_expires_seconds": None,
        },
        "setup": {
            "exclusive_prefix": True,
            "integration_test": True,
            "cors": None,
        },
        "credential": "project:setup-key",
    }
    observed = {
        "schema": dict(contract.observation_schema),
        "payload": {
            "account": "account-10001",
            "region": region,
            "bucket": bucket,
            "bucket_identity": bucket,
            "prefix": "assets/",
            "surface_marker": contract.surface_version,
            "state": "present",
            "dedicated": True,
            "new_bucket": False,
            "prefix_empty": True,
            "prefix_overlap": False,
            "public_access_change_scope": "prefix",
            "account_public_access": "allows-public",
                "bucket_public_access": "allows-public",
                "public_base_url": public_base_url,
                "public_policy_rules": [],
                "lifecycle_rules": [],
            "cors_rules": [],
        },
    }
    observation = {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-observation",
        "provider": contract.provider,
        "contract_id": contract.contract_id,
        "surface_version": contract.surface_version,
        "registry_revision": contract.registry_revision,
        "observation": observed,
    }

    plan = build_setup_plan(
        request,
        observation,
        context=planning_context(tmp_path),
        plan_id_factory=lambda: PLAN_ID,
    )

    assert plan["setup_contract"]["provider"] == contract.provider
    assert plan["actions"][0]["mutation"]["schema"] == dict(
        contract.actions["apply-prefix-public-read"]["mutation_schema"],
    )
    assert plan["actions"][0]["mutation"]["schema"]["id"] != "generic.mutation"
    assert validate_setup_plan(plan) == plan


def test_setup_inputs_reject_hardlink_aliases(tmp_path):
    first = tmp_path / "request.json"
    alias = tmp_path / "request-alias.json"
    first.write_text(json.dumps(setup_request()), encoding="utf-8")
    os.link(first, alias)

    with pytest.raises(ValueError):
        read_setup_input(str(first))
    with pytest.raises(ValueError):
        read_setup_input(str(alias))


def test_plan_sink_no_replace_and_existing_plan_cas_preserve_competitor(tmp_path):
    context = planning_context(tmp_path)
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=context,
        plan_id_factory=lambda: PLAN_ID,
    )
    output_dir = tmp_path / "artifacts"
    output_dir.mkdir()
    absent_path = output_dir / "absent-plan.json"
    absent = preflight_plan_sink(
        str(absent_path), context=context, inputs=(),
    )
    absent_path.write_bytes(b"competitor")

    with pytest.raises(ValueError):
        publish_setup_plan(absent, plan)
    assert absent_path.read_bytes() == b"competitor"

    existing_path = output_dir / "existing-plan.json"
    existing_path.write_bytes(canonicalize(plan))
    existing_path.chmod(0o600)
    existing = preflight_plan_sink(
        str(existing_path), context=context, inputs=(),
    )
    replacement = build_setup_plan(
        setup_request(), setup_observation(), context=context,
        plan_id_factory=lambda: "223e4567-e89b-42d3-a456-426614174000",
    )
    existing_path.write_bytes(canonicalize(replacement))
    existing_path.chmod(0o600)

    with pytest.raises(ValueError):
        publish_setup_plan(existing, plan)
    assert existing_path.read_bytes() == canonicalize(replacement)


@pytest.mark.parametrize(
    "reflected",
    [SECRET, "".join(f"%{byte:02X}" for byte in SECRET.encode("utf-8"))],
)
def test_planner_rejects_raw_and_percent_encoded_credential_reflection(
    tmp_path, reflected,
):
    observation = setup_observation()
    observation["observation"]["payload"]["surface_marker"] = reflected

    with pytest.raises(ValueError):
        build_setup_plan(
            setup_request(), observation, context=planning_context(tmp_path),
            plan_id_factory=lambda: PLAN_ID,
        )
