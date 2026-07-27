import inspect

from capabilities import Capability, CapabilityRegistry
from planning import derive_contract_key, registry_for_target
from strict_json import canonicalize
from target_contract import contract_hash, contract_snapshot, credential_binding_hash
from v2_schema import ScopedReference, parse_target


def target(**overrides):
    value = {
        "schema_version": 1,
        "credential": "project:images-key",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "example-bucket",
        "prefix": "images/",
        "access": {"mode": "private", "public_base_url": None, "presign_expires_seconds": 3600},
        "retention": {"mode": "retain", "days": None},
        "collision": "reject",
        "object_headers": {"cache_control": None, "content_disposition": None},
        "limits": {"soft_max_bytes": 1048576, "multipart_threshold_bytes": None, "part_size_bytes": None},
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {"exclusive_prefix": True, "integration_test": False, "cors": None},
    }
    value.update(overrides)
    return parse_target(value, expected_scope="project")


def snapshot(item):
    key = derive_contract_key(item)
    return contract_snapshot(
        target_ref=ScopedReference("project", "images"),
        config_scope="project",
        project_root="/tmp/project",
        target=item,
        contract_key=key,
        registry=registry_for_target(item, key),
    )


def test_public_base_url_drift_changes_hash():
    first = target(access={
        "mode": "public", "public_base_url": "https://cdn.example.com/", "presign_expires_seconds": None,
    })
    second = target(access={
        "mode": "public", "public_base_url": "https://cdn.example.net/", "presign_expires_seconds": None,
    })
    assert contract_hash(snapshot(first)) != contract_hash(snapshot(second))


def test_contract_snapshot_has_no_credential_value_channel():
    assert set(inspect.signature(contract_snapshot).parameters) == {
        "target_ref",
        "config_scope",
        "project_root",
        "target",
        "contract_key",
        "registry",
    }
    assert isinstance(target().credential, ScopedReference)


def test_snapshot_exposes_exactly_one_credential_derived_field():
    item = target()
    assert set(snapshot(item)) == {
        "access",
        "addressing",
        "bucket",
        "capabilities",
        "collision",
        "config_scope",
        "contract_key",
        "contract_version",
        "credential_binding_hash",
        "endpoint",
        "limits",
        "object_headers",
        "prefix",
        "project_root",
        "provider",
        "region",
        "retention",
        "retry",
        "setup",
        "target_ref",
    }
    assert snapshot(item)["credential_binding_hash"] == credential_binding_hash(item.credential)


def test_snapshot_bytes_carry_no_credential_selector():
    item = target()
    raw = canonicalize(snapshot(item)).decode("utf-8")
    assert item.credential.name not in raw


def test_contract_hash_is_deterministic():
    item = target()
    assert contract_hash(snapshot(item)) == contract_hash(snapshot(item))


def test_credential_selector_drift_changes_binding_and_hash():
    assert credential_binding_hash(ScopedReference("project", "images-key")) != credential_binding_hash(
        ScopedReference("project", "images-key-2")
    )
    assert contract_hash(snapshot(target())) != contract_hash(
        snapshot(target(credential="project:images-key-2"))
    )


def test_evidence_scope_drift_changes_hash():
    item = target()
    key = derive_contract_key(item)
    reference = ScopedReference("project", "images")
    def with_evidence(evidence_id):
        return contract_hash(contract_snapshot(
            target_ref=reference, config_scope="project", project_root="/tmp/project",
            target=item, contract_key=key,
            registry=CapabilityRegistry([(key, [Capability("GetObject", "enabled", evidence_id)])]),
        ))
    assert with_evidence("aliyun-bucket-a") != with_evidence("aliyun-bucket-b")


def test_contract_hash_matches_golden_vector():
    # Absolute literal, derived from current implementation behaviour (not
    # independently specified) — pins CONTRACT_DOMAIN, CONTRACT_VERSION, the
    # digest algorithm, and the snapshot shape all at once, so any of those
    # silently changing fails loudly instead of only failing a relative
    # (!=/==) comparison. Re-derived when the minimal caller contract enabled
    # ConditionalPutObject in the aws-s3/cloudflare-r2 baseline presets.
    assert contract_hash(snapshot(target())) == (
        "sha256:6ab3658d79e9867d0b853d6e8af89125b698fcd9e5e9d6e1fb315a4008b7fe25"
    )


def test_credential_binding_hash_matches_golden_vector():
    # Absolute literal, derived from current implementation behaviour (not
    # independently specified) — pins CREDENTIAL_BINDING_DOMAIN and the digest
    # algorithm.
    assert credential_binding_hash(ScopedReference("project", "images-key")) == (
        "sha256:f5953ab6774af81184b4706335d72960a954c7b0605924890474bbb11c5c50a6"
    )


def test_config_scope_is_part_of_identity():
    item = target()
    key = derive_contract_key(item)
    def with_scope(scope):
        return contract_hash(contract_snapshot(
            target_ref=ScopedReference("project", "images"), config_scope=scope,
            project_root="/tmp/project", target=item, contract_key=key,
            registry=registry_for_target(item, key),
        ))
    assert with_scope("project") != with_scope("global")
