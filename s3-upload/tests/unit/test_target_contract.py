from dataclasses import replace

import pytest

from capabilities import Capability, CapabilityRegistry, ContractKey
from planning import derive_contract_key, registry_for_target
from target_contract import (
    CAPABILITY_OPERATIONS,
    CONTRACT_VERSION,
    contract_hash,
    contract_snapshot,
    credential_binding_hash,
)
from v2_schema import ScopedReference, parse_target


def base_target(**overrides):
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


def snapshot_for(target, *, target_ref="project:images", project_root="/tmp/project"):
    reference = ScopedReference(*target_ref.split(":", 1))
    key = derive_contract_key(target)
    return contract_snapshot(
        target_ref=reference,
        config_scope="project",
        project_root=project_root,
        target=target,
        contract_key=key,
        registry=registry_for_target(target, key),
    )


def test_snapshot_declares_contract_version():
    assert snapshot_for(base_target())["contract_version"] == CONTRACT_VERSION


def test_hash_is_stable_for_equal_snapshots():
    assert contract_hash(snapshot_for(base_target())) == contract_hash(snapshot_for(base_target()))


def test_hash_has_sha256_prefix():
    assert contract_hash(snapshot_for(base_target())).startswith("sha256:")
    assert len(contract_hash(snapshot_for(base_target()))) == 71


def test_snapshot_contains_no_credential_values():
    text = repr(snapshot_for(base_target()))
    assert "images-key" not in text
    assert "credential_binding_hash" in text


def test_credential_binding_hash_is_domain_separated():
    first = credential_binding_hash(ScopedReference("project", "images-key"))
    second = credential_binding_hash(ScopedReference("global", "images-key"))
    assert first != second
    assert first.startswith("sha256:")


def test_credential_binding_hash_does_not_collide_across_the_scope_name_boundary():
    first = credential_binding_hash(ScopedReference("project", "a:b"))
    second = credential_binding_hash(ScopedReference("project:a", "b"))
    assert first != second


@pytest.mark.parametrize("overrides", [
    {"prefix": "other/"},
    {"collision": "replace"},
    {"access": {"mode": "public", "public_base_url": "https://cdn.example.com/", "presign_expires_seconds": None}},
    {"retention": {"mode": "expire", "days": 30}},
    {"object_headers": {"cache_control": "public, max-age=60", "content_disposition": None}},
    {"bucket": "other-bucket"},
    {"region": "us-west-2"},
    {"credential": "project:other-key"},
])
def test_any_semantic_drift_changes_hash(overrides):
    assert contract_hash(snapshot_for(base_target())) != contract_hash(
        snapshot_for(base_target(**overrides))
    )


def test_project_root_is_part_of_identity():
    target = base_target()
    assert contract_hash(snapshot_for(target, project_root="/tmp/a")) != contract_hash(
        snapshot_for(target, project_root="/tmp/b")
    )


def test_capability_state_drift_changes_hash():
    target = base_target()
    key = derive_contract_key(target)
    reference = ScopedReference("project", "images")
    permissive = CapabilityRegistry([(key, [Capability("GetObject", "enabled", "evidence-1")])])
    baseline = registry_for_target(target, key)
    assert contract_hash(contract_snapshot(
        target_ref=reference, config_scope="project", project_root="/tmp/project",
        target=target, contract_key=key, registry=permissive,
    )) != contract_hash(contract_snapshot(
        target_ref=reference, config_scope="project", project_root="/tmp/project",
        target=target, contract_key=key, registry=baseline,
    ))


def test_cors_snapshot_mutation_does_not_corrupt_the_frozen_target():
    cors = {
        "allowed_origins": ["https://example.com"],
        "allowed_methods": ["GET"],
        "allowed_headers": [],
        "expose_headers": [],
        "max_age_seconds": 0,
    }
    target = base_target(setup={"exclusive_prefix": True, "integration_test": False, "cors": cors})
    snapshot = snapshot_for(target)
    snapshot["setup"]["cors"]["allowed_origins"].append("https://mutated.example.com")
    assert target.setup.cors["allowed_origins"] == ["https://example.com"]


def test_capability_operations_is_exactly_the_locked_literal():
    # CAPABILITY_OPERATIONS is derived from capabilities.BASELINE_DISABLED_OPERATIONS
    # (sorted). That module cannot be modified by this task, so this pins the
    # *current* literal tuple: any upstream addition/removal there must show up
    # here as a loud CI failure instead of silently reshaping every persisted
    # contract_hash without a CONTRACT_VERSION bump.
    assert CAPABILITY_OPERATIONS == (
        "AbortMultipartUpload",
        "CompleteMultipartUpload",
        "ConditionalCompleteMultipartUpload",
        "ConditionalPutObject",
        "CreateMultipartUpload",
        "DeleteObjectCurrentKey",
        "DeleteObjectVersion",
        "GetObject",
        "HeadObject",
        "ListParts",
        "ObserveDeleteCurrentKey",
        "ObserveDeleteVersion",
        "ObserveMultipartSession",
        "PublicGetObject",
        "ReservedMetadataRoundTrip",
        "UploadPart",
    )


def test_location_fingerprint_does_not_replace_contract_hash():
    first = base_target()
    second = base_target(prefix="other/")
    assert first.location_fingerprint() == second.location_fingerprint()
    assert contract_hash(snapshot_for(first)) != contract_hash(snapshot_for(second))
