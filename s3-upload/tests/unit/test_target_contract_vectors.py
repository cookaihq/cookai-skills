import inspect

from capabilities import Capability, CapabilityRegistry
from planning import derive_contract_key, registry_for_target
from strict_json import canonicalize
from target_contract import contract_hash, contract_snapshot, credential_binding_hash
from v2_schema import CredentialProfile, ScopedReference, parse_target


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


def test_snapshot_credential_field_is_exactly_the_binding_hash():
    item = target()
    assert snapshot(item)["credential_binding_hash"] == credential_binding_hash(item.credential)


def test_credential_value_rotation_does_not_change_hash():
    item = target()
    before = contract_hash(snapshot(item))
    rotated = CredentialProfile(
        access_key_id="ROTATEDKEY1234",
        secret_access_key="rotated-secret-value",
        session_token="",
        expires_at=None,
    )
    raw = canonicalize(snapshot(item)).decode("utf-8")
    assert rotated.access_key_id not in raw
    assert rotated.secret_access_key not in raw
    assert item.credential.name not in raw
    assert contract_hash(snapshot(item)) == before


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
