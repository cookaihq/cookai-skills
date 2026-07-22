import json
from pathlib import Path
import socket

import pytest

from provider_setup_candidates import (
    ALIYUN_OSS_SETUP_IDENTITY,
    TENCENT_COS_SETUP_IDENTITY,
    ProviderSetupError,
    fixture_extension_for,
    lookup_setup_contract,
)
from setup_adapters import FixtureAdapter, validate_fixture
from setup_contracts import validate_setup_result
from setup_executor import CredentialSink


ROOT = Path(__file__).parents[2]


def identity_kwargs(identity):
    return {
        "provider": identity["provider"],
        "contract_id": identity["contract_id"],
        "surface_version": identity["surface_version"],
        "registry_revision": identity["registry_revision"],
    }


def observation(contract, *, new_bucket=False, prefix="assets/", **changes):
    provider = contract.provider
    bucket = (
        "website-assets-1250000000"
        if provider == "tencent-cos"
        else "website-assets"
    )
    payload = {
        "account": "account-10001",
        "region": "ap-guangzhou" if provider == "tencent-cos" else "us-west-1",
        "bucket": bucket,
        "bucket_identity": bucket,
        "prefix": prefix,
        "surface_marker": contract.surface_version,
        "state": "absent" if new_bucket else "present",
        "dedicated": True,
        "new_bucket": new_bucket,
        "prefix_empty": True,
        "prefix_overlap": False,
        "public_access_change_scope": "new-bucket" if new_bucket else "prefix",
        "account_public_access": "allows-public",
        "bucket_public_access": "allows-public",
        "public_base_url": (
            f"https://{bucket}.cos.ap-guangzhou.myqcloud.com"
            if provider == "tencent-cos"
            else f"https://{bucket}.oss-us-west-1.aliyuncs.com"
        ),
        "public_policy_rules": [],
        "lifecycle_rules": [
            {
                "id": "unrelated-archive",
                "prefix": "archive/",
                "enabled": True,
                "expiration_days": 365,
            }
        ],
        "cors_rules": [
            {
                "id": "unrelated-cors",
                "allowed_origins": ["https://admin.example"],
                "allowed_methods": ["GET"],
                "allowed_headers": [],
                "expose_headers": ["ETag"],
                "max_age_seconds": 600,
            }
        ],
    }
    payload.update(changes)
    return {
        "schema": contract.observation_schema,
        "payload": payload,
    }


def request(contract, *, new_bucket=False, prefix="assets/", **changes):
    bucket = (
        "website-assets-1250000000"
        if contract.provider == "tencent-cos"
        else "website-assets"
    )
    region = "ap-guangzhou" if contract.provider == "tencent-cos" else "us-west-1"
    endpoint = (
        "https://cos.ap-guangzhou.myqcloud.com"
        if contract.provider == "tencent-cos"
        else "https://s3.oss-us-west-1.aliyuncs.com"
    )
    value = {
        "provider": contract.provider,
        "credential_source_category": "persistent-existing",
        "credential_persistence": "project",
        "proposed_target": {
            "provider": contract.provider,
            "region": region,
            "endpoint": endpoint,
            "addressing": "virtual",
            "bucket": bucket,
            "prefix": prefix,
            "access": {
                "mode": "public",
                "public_base_url": observation(
                    contract, new_bucket=new_bucket, prefix=prefix,
                )["payload"]["public_base_url"],
                "presign_expires_seconds": None,
            },
            "retention": {"mode": "expire", "days": 30},
            "setup": {
                "exclusive_prefix": True,
                "integration_test": True,
                "cors": {
                    "allowed_origins": ["https://www.example.com"],
                    "allowed_methods": ["GET", "HEAD"],
                    "allowed_headers": [],
                    "expose_headers": ["ETag"],
                    "max_age_seconds": 600,
                },
            },
        },
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_provider_setup_contracts_are_exact_test_only_registries(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))

    assert contract is not None
    assert contract.remote_evidence == "not-tested"
    assert contract.fixture_kind == "synthetic/docs-derived"
    with pytest.raises(ProviderSetupError, match="normal assisted setup is unavailable"):
        contract.registry_contract(["create-dedicated-bucket"], mode="normal")

    registry = contract.registry_contract(
        ["create-dedicated-bucket", "issue-long-lived-access-key"],
        mode="test-only",
    )
    assert [row["state"] for row in registry["action_contracts"]] == [
        "test-only", "test-only",
    ]
    assert all("hypothesis" in row["evidence_id"] for row in registry["action_contracts"])
    assert all("live" not in row["evidence_id"] for row in registry["action_contracts"])


def test_unknown_or_stale_setup_identity_has_no_extension():
    assert lookup_setup_contract(
        provider="aliyun-oss",
        contract_id=ALIYUN_OSS_SETUP_IDENTITY["contract_id"],
        surface_version="aliyun-oss-console.changed-v2",
        registry_revision=ALIYUN_OSS_SETUP_IDENTITY["registry_revision"],
    ) is None


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_fixture_extension_is_bound_to_the_exact_provider_contract(identity):
    extension = fixture_extension_for(
        identity["provider"], identity["contract_id"],
        identity["surface_version"], identity["registry_revision"],
    )

    assert extension.provider == identity["provider"]
    assert extension.contract_id == identity["contract_id"]
    assert extension.surface_version == identity["surface_version"]
    assert extension.registry_revision == identity["registry_revision"]
    assert tuple(extension.credential_delivery_fields) == (
        "access_key_id", "secret_access_key",
    )
    assert fixture_extension_for(
        "tencent-cos",
        TENCENT_COS_SETUP_IDENTITY["contract_id"],
        TENCENT_COS_SETUP_IDENTITY["surface_version"],
        "tencent-cos-setup-registry.changed-v2",
    ) is None


def test_oss_and_cos_setup_schemas_and_evidence_are_not_reused():
    oss = lookup_setup_contract(**identity_kwargs(ALIYUN_OSS_SETUP_IDENTITY))
    cos = lookup_setup_contract(**identity_kwargs(TENCENT_COS_SETUP_IDENTITY))

    assert oss.observation_schema != cos.observation_schema
    assert set(oss.actions) == set(cos.actions)
    for action_type in oss.actions:
        assert oss.actions[action_type]["evidence_id"] != cos.actions[action_type]["evidence_id"]
        for field in (
            "observation_schema", "resource_scope_schema", "mutation_schema",
            "diff_schema", "success_schema", "recovery_schema",
        ):
            assert oss.actions[action_type][field] != cos.actions[action_type][field]
    with pytest.raises(ProviderSetupError, match="wrong-purpose or unregistered schema"):
        oss.validate_payload_envelope(
            observation(cos), oss.observation_schema, "OSS observation",
        )


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_provider_mutation_parameters_are_closed_and_action_specific(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    action = contract.build_action(
        "apply-prefix-public-read", observation(contract)["payload"],
        request(contract),
    )
    schema = contract.actions["apply-prefix-public-read"]["mutation_schema"]
    changed = json.loads(json.dumps(action["mutation"]))
    changed["payload"]["parameters"]["unreviewed_control"] = True

    with pytest.raises(ProviderSetupError, match="parameters"):
        contract.validate_payload_envelope(changed, schema, "mutation")

    wrong_action = json.loads(json.dumps(action["mutation"]))
    wrong_action["payload"]["operation"] = "merge-prefix-lifecycle"
    with pytest.raises(ProviderSetupError):
        contract.validate_payload_envelope(wrong_action, schema, "mutation")


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_managed_lifecycle_and_cors_diffs_preserve_unrelated_rules(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    observed = observation(contract)
    candidate_request = request(contract)

    lifecycle = contract.build_action(
        "merge-prefix-lifecycle", observed["payload"], candidate_request,
    )
    cors = contract.build_action(
        "merge-bucket-cors", observation(contract, new_bucket=True)["payload"],
        request(contract, new_bucket=True),
    )

    lifecycle_after = lifecycle["diff"]["payload"]["after_rules"]
    cors_after = cors["diff"]["payload"]["after_rules"]
    assert lifecycle_after[0] == observed["payload"]["lifecycle_rules"][0]
    assert lifecycle_after[1]["prefix"] == "assets/"
    assert lifecycle_after[1]["expiration_days"] == 30
    assert cors_after[0] == observed["payload"]["cors_rules"][0]
    assert cors_after[1]["allowed_origins"] == ["https://www.example.com"]


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_public_read_diff_contains_exact_managed_policy_and_preserves_unrelated(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    unrelated = {
        "id": "unrelated-private-reader",
        "effect": "allow",
        "principal": "account-20002",
        "actions": ["GetObject"],
        "resource": "unrelated-resource",
    }
    observed = observation(contract, public_policy_rules=[unrelated])

    action = contract.build_action(
        "apply-prefix-public-read", observed["payload"], request(contract),
    )

    before = action["diff"]["payload"]["before_rules"]
    after = action["diff"]["payload"]["after_rules"]
    changes = action["diff"]["payload"]["control_changes"]
    assert before == [unrelated]
    assert after[0] == unrelated
    assert after[1]["principal"] == "*"
    assert after[1]["actions"] == ["GetObject"]
    assert after[1]["resource"] == (
        "qcs::cos:ap-guangzhou:uid/1250000000:"
        "website-assets-1250000000/assets/*"
        if contract.provider == "tencent-cos"
        else "acs:oss:*:account-10001:website-assets/assets/*"
    )
    assert changes == [{
        "control": "bucket-policy",
        "before": "preserve-unrelated",
        "after": "append-managed-prefix-read",
    }]


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_every_candidate_action_diff_names_the_exact_control_change(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    expected = {
        "create-dedicated-bucket": {
            "control": "bucket",
            "before": "absent",
            "after": "create-private-dedicated",
        },
        "apply-new-bucket-public-read": {
            "control": "bucket-policy",
            "before": "preserve-unrelated",
            "after": "append-managed-prefix-read",
        },
        "apply-prefix-public-read": {
            "control": "bucket-policy",
            "before": "preserve-unrelated",
            "after": "append-managed-prefix-read",
        },
        "merge-prefix-lifecycle": {
            "control": "lifecycle-rules",
            "before": "preserve-unrelated",
            "after": "append-managed-prefix-expiry",
        },
        "merge-bucket-cors": {
            "control": "cors-rules",
            "before": "preserve-unrelated",
            "after": "append-managed-cors",
        },
        "issue-long-lived-access-key": {
            "control": "sub-identity-access-key",
            "before": "absent",
            "after": "issue-once-and-deliver-to-bound-sink",
        },
    }

    for action_type, expected_change in expected.items():
        new_bucket = action_type in {
            "create-dedicated-bucket",
            "apply-new-bucket-public-read",
            "merge-bucket-cors",
        }
        candidate_request = request(contract, new_bucket=new_bucket)
        if action_type == "issue-long-lived-access-key":
            candidate_request["credential_source_category"] = "planned-issuance"
        action = contract.build_action(
            action_type,
            observation(contract, new_bucket=new_bucket)["payload"],
            candidate_request,
        )

        assert action["diff"]["payload"]["control_changes"] == [
            expected_change,
        ]

        changed = json.loads(json.dumps(action["diff"]))
        changed["payload"]["control_changes"][0]["after"] = "unreviewed"
        with pytest.raises(ProviderSetupError, match="control changes"):
            contract.validate_payload_envelope(
                changed, contract.actions[action_type]["diff_schema"], "diff",
            )


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_public_policy_overlap_stops_automatic_merge(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    overlapping = {
        "id": "existing-public-rule",
        "effect": "allow",
        "principal": "*",
        "actions": ["GetObject"],
        "resource": "assets/*",
    }
    observed = observation(contract, public_policy_rules=[overlapping])

    with pytest.raises(ProviderSetupError, match="policy rule overlap"):
        contract.build_action(
            "apply-prefix-public-read", observed["payload"], request(contract),
        )


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_unsafe_prefix_and_bucket_wide_merges_stop_before_action(identity):
    contract = lookup_setup_contract(**identity_kwargs(identity))
    candidate_request = request(contract)

    for unsafe in (
        observation(contract, prefix_empty=False),
        observation(contract, prefix_overlap=True),
        observation(contract, public_access_change_scope="bucket-wide"),
        observation(contract, public_access_change_scope="account-level"),
    ):
        with pytest.raises(ProviderSetupError):
            contract.build_action(
                "apply-prefix-public-read", unsafe["payload"], candidate_request,
            )

    overlapping_rule = {
        "id": "existing-overlap",
        "prefix": "assets/legacy/",
        "enabled": True,
        "expiration_days": 90,
    }
    unsafe = observation(contract, lifecycle_rules=[overlapping_rule])
    with pytest.raises(ProviderSetupError, match="overlap"):
        contract.build_action(
            "merge-prefix-lifecycle", unsafe["payload"], candidate_request,
        )


def test_oss_mainland_default_public_endpoint_cannot_be_repaired_by_public_base_url():
    contract = lookup_setup_contract(**identity_kwargs(ALIYUN_OSS_SETUP_IDENTITY))
    observed = observation(
        contract,
        region="cn-hangzhou",
        public_base_url="https://website-assets.example.invalid",
    )
    candidate_request = request(contract)
    candidate_request["proposed_target"] = {
        **candidate_request["proposed_target"],
        "region": "cn-hangzhou",
        "endpoint": "https://s3.oss-cn-hangzhou.aliyuncs.com",
        "access": {
            **candidate_request["proposed_target"]["access"],
            "public_base_url": "https://website-assets.example.invalid",
        },
    }

    with pytest.raises(ProviderSetupError, match="PublicEndpointForbidden"):
        contract.build_action(
            "apply-prefix-public-read", observed["payload"], candidate_request,
        )


def test_oss_public_base_url_keeps_the_same_bucket_and_region_identity():
    contract = lookup_setup_contract(**identity_kwargs(ALIYUN_OSS_SETUP_IDENTITY))
    observed = observation(
        contract, public_base_url="https://other-bucket.oss-us-west-1.aliyuncs.com",
    )
    candidate_request = request(contract)
    candidate_request["proposed_target"] = {
        **candidate_request["proposed_target"],
        "access": {
            **candidate_request["proposed_target"]["access"],
            "public_base_url": "https://other-bucket.oss-us-west-1.aliyuncs.com",
        },
    }

    with pytest.raises(ProviderSetupError, match="Public Base URL"):
        contract.build_action(
            "apply-prefix-public-read", observed["payload"], candidate_request,
        )


def test_cos_requires_one_complete_bucket_identity_and_virtual_addressing():
    contract = lookup_setup_contract(**identity_kwargs(TENCENT_COS_SETUP_IDENTITY))
    observed = observation(contract)

    mismatch = request(contract)
    mismatch["proposed_target"] = {
        **mismatch["proposed_target"], "bucket": "website-assets-1250000001",
    }
    with pytest.raises(ProviderSetupError, match="bucket identity"):
        contract.build_action(
            "apply-prefix-public-read", observed["payload"], mismatch,
        )

    path_style = request(contract)
    path_style["proposed_target"] = {
        **path_style["proposed_target"], "addressing": "path",
    }
    with pytest.raises(ProviderSetupError, match="virtual-hosted"):
        contract.build_action(
            "apply-prefix-public-read", observed["payload"], path_style,
        )


@pytest.mark.parametrize("provider", ["aliyun-oss", "tencent-cos"])
def test_versioned_provider_setup_assets_are_synthetic_and_never_enabled(provider):
    base = ROOT / "references" / "maintainers" / "setup" / provider
    sources = json.loads((base / "official-sources.v1.json").read_text())
    contract = json.loads((base / "setup-capability-contract.v1.json").read_text())
    playbook = json.loads((base / "playbook.v1.json").read_text())

    assert sources["retrieved_at"] == "2026-07-22"
    assert all(source["url"].startswith("https://") for source in sources["sources"])
    assert contract["fixture_kind"] == "synthetic/docs-derived"
    assert contract["remote_evidence"] == "not-tested"
    assert all(row["state"] in {"test-only", "disabled"} for row in contract["actions"])
    assert not any(row["state"] == "enabled" for row in contract["actions"])
    assert playbook["network_requests_allowed"] is False
    assert playbook["live_evidence_ids"] == []
    assert playbook["data_plane_evidence_enables_setup"] is False

    code_contract = lookup_setup_contract(
        provider=contract["provider"],
        contract_id=contract["contract_id"],
        surface_version=contract["surface_version"],
        registry_revision=contract["registry_revision"],
    )
    source_ids = {source["id"] for source in sources["sources"]}
    assert code_contract is not None
    assert [row["action_type"] for row in contract["actions"]] == list(
        code_contract.actions,
    )
    for documented in contract["actions"]:
        code_row = code_contract.actions[documented["action_type"]]
        for key in (
            "action_type", "state", "evidence_id", "observation_schema",
            "resource_scope_schema", "mutation_schema", "diff_schema",
            "success_schema", "recovery_schema",
        ):
            assert documented[key] == code_row[key]
        assert set(documented["source_ids"]) <= source_ids


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_provider_fixture_replays_strictly_with_one_shot_sentinel_sink(
    identity, monkeypatch,
):
    provider = identity["provider"]
    fixture_path = (
        ROOT / "tests" / "fixtures" / "provider_setup" / provider
        / "strict-replay.v1.json"
    )
    fixture = json.loads(fixture_path.read_text())
    extension = fixture_extension_for(
        provider, identity["contract_id"], identity["surface_version"],
        identity["registry_revision"],
    )
    assert validate_fixture(fixture, extension) == fixture

    def network_trap(*args, **kwargs):
        raise AssertionError("synthetic provider setup fixture attempted network access")

    monkeypatch.setattr(socket, "socket", network_trap)
    adapter = FixtureAdapter(fixture, extension)
    adapter.wait_for_login({"scenario": "strict-replay-v1", "provider": provider})
    before = adapter.observe({
        "phase": "before", "scenario": "strict-replay-v1", "provider": provider,
    })
    assert before["payload"]["state"] == "present"
    outcome = adapter.guarded_mutate({
        "action_id": "action-1",
        "action_type": "apply-prefix-public-read",
        "scenario": "strict-replay-v1",
        "provider": provider,
    })
    assert outcome["status"] == "accepted"
    adapter.observe({
        "phase": "after", "scenario": "strict-replay-v1", "provider": provider,
    })

    adapter.wait_for_login({"scenario": "issuance-v1", "provider": provider})
    adapter.observe({
        "phase": "before", "scenario": "issuance-v1", "provider": provider,
    })
    binding = ("provider-plan", "provider-plan-hash", "action-2")
    sink = CredentialSink(*binding)
    issuance = adapter.guarded_mutate(
        {
            "action_id": "action-2",
            "action_type": "issue-long-lived-access-key",
            "scenario": "issuance-v1",
            "provider": provider,
        },
        sink,
    )
    assert issuance["status"] == "accepted"
    profile = sink._consume(binding)
    assert profile["session_token"] == ""
    assert profile["expires_at"] is None
    assert profile["access_key_id"] in fixture["redaction_sentinels"]
    assert profile["secret_access_key"] in fixture["redaction_sentinels"]
    adapter.observe({
        "phase": "after", "scenario": "issuance-v1", "provider": provider,
    })
    adapter.assert_consumed()

    with pytest.raises(ValueError, match="extra adapter call"):
        adapter.observe({"phase": "extra"})


@pytest.mark.parametrize(
    "identity",
    [ALIYUN_OSS_SETUP_IDENTITY, TENCENT_COS_SETUP_IDENTITY],
)
def test_provider_fixture_rejects_order_drift_and_stale_surface(identity):
    provider = identity["provider"]
    fixture = json.loads((
        ROOT / "tests" / "fixtures" / "provider_setup" / provider
        / "strict-replay.v1.json"
    ).read_text())
    extension = fixture_extension_for(
        provider, identity["contract_id"], identity["surface_version"],
        identity["registry_revision"],
    )
    adapter = FixtureAdapter(fixture, extension)
    with pytest.raises(ValueError, match="order or request digest mismatch"):
        adapter.observe({"phase": "before"})

    stale = json.loads(json.dumps(fixture))
    stale["calls"][1]["response"]["payload"]["surface_marker"] = "changed-v2"
    with pytest.raises(ProviderSetupError, match="surface marker is stale"):
        validate_fixture(stale, extension)


@pytest.mark.parametrize("provider", ["aliyun-oss", "tencent-cos"])
def test_provider_golden_results_are_strict_and_never_contain_sentinels(provider):
    fixture = json.loads((
        ROOT / "tests" / "fixtures" / "provider_setup" / provider
        / "strict-replay.v1.json"
    ).read_text())
    golden_root = ROOT / "tests" / "goldens" / "provider_setup" / provider
    for name in (
        "completed.v1.json", "issuance-unknown.v1.json",
        "issuance-local-failure.v1.json",
    ):
        path = golden_root / name
        raw = path.read_text()
        result = json.loads(raw)
        assert validate_setup_result(result) == result
        assert all(sentinel not in raw for sentinel in fixture["redaction_sentinels"])
    unknown = json.loads((golden_root / "issuance-unknown.v1.json").read_text())
    local_failure = json.loads((
        golden_root / "issuance-local-failure.v1.json"
    ).read_text())
    assert unknown["status"] == "unknown"
    assert local_failure["status"] == "partial"
    assert unknown["recovery_instructions"] == ["manual_revoke_and_reissue"]
    assert local_failure["recovery_instructions"] == [
        "manual_revoke_and_reissue",
    ]
