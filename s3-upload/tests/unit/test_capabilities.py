import pytest

from capabilities import (
    BASELINE_DISABLED_OPERATIONS,
    Capability,
    CapabilityContractError,
    CapabilityRegistry,
    ContractKey,
    LiveTestInterlock,
    OperationShape,
    build_v2_baseline_registry,
    plan_operation,
)


def contract_key(**overrides):
    values = {
        "schema_version": 1,
        "provider": "aws-s3",
        "scheme": "https",
        "endpoint_family": "aws-public",
        "region_class": "us-east-1",
        "network_class": "public",
        "addressing": "virtual",
        "signing_profile": "sigv4-s3",
        "payload_profile": "fixed-content-length",
    }
    values.update(overrides)
    return ContractKey(**values)


def test_contract_key_serializes_exactly_nine_fields():
    key = contract_key()

    assert key.as_dict() == {
        "schema_version": 1,
        "provider": "aws-s3",
        "scheme": "https",
        "endpoint_family": "aws-public",
        "region_class": "us-east-1",
        "network_class": "public",
        "addressing": "virtual",
        "signing_profile": "sigv4-s3",
        "payload_profile": "fixed-content-length",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"scheme": "ftp"},
        {"addressing": "auto"},
        {"provider": "AWS-S3"},
        {"endpoint_family": "bad/value"},
        {"region_class": "x" * 129},
    ],
)
def test_contract_key_rejects_values_outside_the_strict_registry_schema(overrides):
    with pytest.raises(CapabilityContractError):
        contract_key(**overrides)


def test_registry_lookup_requires_the_complete_exact_contract_key():
    key = contract_key()
    registry = CapabilityRegistry(
        ((key, (Capability("PutObject", "enabled", "v1-aws-put"),)),)
    )

    assert registry.lookup(key, "PutObject").as_dict() == {
        "operation": "PutObject",
        "state": "enabled",
        "evidence_id": "v1-aws-put",
    }
    assert registry.lookup(
        contract_key(scheme="http"), "PutObject"
    ).as_dict() == {
        "operation": "PutObject",
        "state": "unknown",
        "evidence_id": None,
    }
    assert registry.lookup(
        contract_key(endpoint_family="exact-" + "0" * 64), "PutObject"
    ).state == "unknown"


@pytest.mark.parametrize("provider", ["aliyun-oss", "tencent-cos"])
def test_reserved_provider_names_have_no_implicit_registry_entry(provider):
    registry = CapabilityRegistry(())
    key = contract_key(provider=provider, endpoint_family="candidate")

    capability = registry.lookup(key, "PutObject")

    assert capability.state == "unknown"
    assert capability.evidence_id is None


def test_registry_rejects_duplicate_operations_for_one_contract():
    key = contract_key()
    with pytest.raises(CapabilityContractError, match="duplicate capability"):
        CapabilityRegistry(
            (
                (
                    key,
                    (
                        Capability("PutObject", "enabled", "first"),
                        Capability("PutObject", "disabled", "second"),
                    ),
                ),
            )
        )


def test_v2_baseline_enables_only_v1_put_and_presign_for_approved_contracts():
    aws = contract_key()
    r2 = contract_key(
        provider="cloudflare-r2",
        endpoint_family="cloudflare-r2-public",
        region_class="auto",
        addressing="path",
    )
    custom = contract_key(
        provider="custom",
        endpoint_family="exact-" + "1" * 64,
        region_class="exact-" + "2" * 64,
        addressing="path",
    )
    registry = build_v2_baseline_registry(
        preset_contracts=(aws, r2),
        asserted_custom_contracts=(custom,),
    )

    for key, evidence_prefix in (
        (aws, "v1-aws"),
        (r2, "v1-r2"),
        (custom, "v1-custom"),
    ):
        assert registry.lookup(key, "PutObject") == Capability(
            "PutObject", "enabled", evidence_prefix + "-put"
        )
        assert registry.lookup(key, "PresignGetObject") == Capability(
            "PresignGetObject", "enabled", evidence_prefix + "-presign"
        )
        for operation in BASELINE_DISABLED_OPERATIONS:
            assert registry.lookup(key, operation).state == "disabled"


def test_v2_baseline_does_not_inherit_into_overrides_or_unasserted_custom():
    aws = contract_key()
    overridden = contract_key(endpoint_family="exact-" + "3" * 64)
    unasserted_custom = contract_key(
        provider="custom",
        endpoint_family="exact-" + "4" * 64,
        region_class="exact-" + "5" * 64,
        addressing="path",
    )
    registry = build_v2_baseline_registry(preset_contracts=(aws,))

    assert registry.lookup(overridden, "PutObject").state == "unknown"
    assert registry.lookup(unasserted_custom, "PutObject").state == "unknown"


def test_mainland_oss_public_family_cannot_be_registered_as_custom_baseline():
    disguised = contract_key(
        provider="custom",
        endpoint_family="aliyun-oss-mainland-default-public",
        region_class="exact-" + "8" * 64,
        addressing="virtual",
    )

    with pytest.raises(CapabilityContractError, match="custom baseline"):
        build_v2_baseline_registry(asserted_custom_contracts=(disguised,))


def test_private_single_put_plan_separates_wire_requests_from_required_capabilities():
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
    )

    assert plan.as_dict() == {
        "executable": True,
        "blocking_reasons": [],
        "remote_operations": ["PutObject"],
        "capabilities": [
            {
                "operation": "PutObject",
                "state": "enabled",
                "evidence_id": "v1-aws-put",
            },
            {
                "operation": "PresignGetObject",
                "state": "enabled",
                "evidence_id": "v1-aws-presign",
            },
        ],
    }


def test_public_single_put_plan_does_not_require_presigning():
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="public",
            upload_mode="single-put",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
    )

    assert [item.operation for item in plan.capabilities] == ["PutObject"]
    assert plan.executable is True


@pytest.mark.parametrize("collision", ["unique", "reject"])
def test_conditional_single_put_is_blocked_without_atomic_collision_evidence(collision):
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision=collision,
        ),
        contract_key=key,
        registry=registry,
    )

    assert plan.remote_operations == ("PutObject",)
    assert [item.operation for item in plan.capabilities] == [
        "PutObject",
        "ConditionalPutObject",
        "PresignGetObject",
    ]
    assert plan.blocking_reasons == ("collision_capability_missing",)
    assert plan.executable is False


def test_multipart_plan_orders_wire_operations_and_reports_one_capability_gap():
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="multipart",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
    )

    assert plan.remote_operations == (
        "CreateMultipartUpload",
        "UploadPart",
        "CompleteMultipartUpload",
    )
    assert [item.operation for item in plan.capabilities] == [
        "CreateMultipartUpload",
        "UploadPart",
        "CompleteMultipartUpload",
        "PresignGetObject",
    ]
    assert plan.blocking_reasons == ("multipart_capability_missing",)


def test_conditional_multipart_completion_keeps_collision_gap_distinct():
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="public",
            upload_mode="multipart",
            collision="reject",
        ),
        contract_key=key,
        registry=registry,
    )

    assert [item.operation for item in plan.capabilities] == [
        "CreateMultipartUpload",
        "UploadPart",
        "CompleteMultipartUpload",
        "ConditionalCompleteMultipartUpload",
    ]
    assert plan.blocking_reasons == (
        "multipart_capability_missing",
        "collision_capability_missing",
    )


@pytest.mark.parametrize(
    "scope,delete_operation,observer",
    [
        ("current-key", "DeleteObjectCurrentKey", "ObserveDeleteCurrentKey"),
        ("exact-version", "DeleteObjectVersion", "ObserveDeleteVersion"),
    ],
)
def test_delete_plan_requires_scope_specific_mutation_and_observer(
    scope, delete_operation, observer
):
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(operation="delete", delete_scope=scope),
        contract_key=key,
        registry=registry,
    )

    assert plan.remote_operations == (delete_operation,)
    assert [item.operation for item in plan.capabilities] == [
        delete_operation,
        observer,
    ]
    assert plan.blocking_reasons == ("delete_capability_missing",)
    assert plan.executable is False


@pytest.mark.parametrize(
    "execution_mode,interlock,expected_blocker,executable",
    [
        ("normal", None, "capability_disabled", False),
        (
            "test-only",
            LiveTestInterlock(enabled=False, target_ref="project:synthetic"),
            "live_interlock_missing",
            False,
        ),
        (
            "test-only",
            LiveTestInterlock(enabled=True, target_ref="project:other"),
            "live_interlock_missing",
            False,
        ),
        (
            "test-only",
            LiveTestInterlock(enabled=True, target_ref="project:synthetic"),
            None,
            True,
        ),
    ],
)
def test_test_only_capability_requires_exact_process_interlock(
    execution_mode, interlock, expected_blocker, executable
):
    key = contract_key(provider="synthetic", endpoint_family="synthetic")
    registry = CapabilityRegistry(
        (
            (
                key,
                (
                    Capability("PutObject", "test-only", "synthetic-put"),
                    Capability(
                        "PresignGetObject", "test-only", "synthetic-presign"
                    ),
                ),
            ),
        )
    )

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
        execution_mode=execution_mode,
        target_ref="project:synthetic",
        live_test_interlock=interlock,
    )

    assert plan.executable is executable
    assert plan.blocking_reasons == (() if expected_blocker is None else (expected_blocker,))
    assert {item.state for item in plan.capabilities} == {"test-only"}


@pytest.mark.parametrize(
    "allow_insecure_http,expected_blockers,executable",
    [
        (False, ("insecure_http_opt_in_required",), False),
        (True, (), True),
    ],
)
def test_http_custom_contract_requires_operation_scoped_opt_in(
    allow_insecure_http, expected_blockers, executable
):
    key = contract_key(
        provider="custom",
        scheme="http",
        endpoint_family="exact-" + "6" * 64,
        region_class="exact-" + "7" * 64,
        addressing="path",
    )
    registry = build_v2_baseline_registry(asserted_custom_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
        allow_insecure_http=allow_insecure_http,
    )

    assert plan.blocking_reasons == expected_blockers
    assert plan.executable is executable


def test_missing_credential_blocks_an_otherwise_complete_plan():
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
        credential_available=False,
    )

    assert plan.remote_operations == ("PutObject",)
    assert [item.state for item in plan.capabilities] == ["enabled", "enabled"]
    assert plan.blocking_reasons == ("credential_unavailable",)
    assert plan.executable is False


@pytest.mark.parametrize(
    "shape",
    [
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="single-put",
            collision="replace",
            delete_scope="current-key",
        ),
        OperationShape(
            operation="delete",
            delete_scope="current-key",
            access_mode="private",
        ),
        OperationShape(
            operation="delete",
            delete_scope="exact-version",
            upload_mode="single-put",
        ),
    ],
)
def test_operation_shapes_reject_fields_from_another_operation(shape):
    key = contract_key()
    registry = build_v2_baseline_registry(preset_contracts=(key,))

    with pytest.raises(CapabilityContractError, match="operation shape"):
        plan_operation(shape, contract_key=key, registry=registry)


def test_synthetic_evidence_can_enable_multipart_without_changing_wire_order():
    key = contract_key(provider="synthetic", endpoint_family="synthetic")
    registry = CapabilityRegistry(
        (
            (
                key,
                tuple(
                    Capability(operation, "enabled", "synthetic-" + operation.lower())
                    for operation in (
                        "CreateMultipartUpload",
                        "UploadPart",
                        "CompleteMultipartUpload",
                        "PresignGetObject",
                    )
                ),
            ),
        )
    )

    plan = plan_operation(
        OperationShape(
            operation="upload",
            access_mode="private",
            upload_mode="multipart",
            collision="replace",
        ),
        contract_key=key,
        registry=registry,
    )

    assert plan.executable is True
    assert plan.blocking_reasons == ()
    assert plan.remote_operations == (
        "CreateMultipartUpload",
        "UploadPart",
        "CompleteMultipartUpload",
    )


def test_delete_mutation_without_its_observer_remains_blocked():
    key = contract_key(provider="synthetic", endpoint_family="synthetic")
    registry = CapabilityRegistry(
        (
            (
                key,
                (
                    Capability(
                        "DeleteObjectCurrentKey", "enabled", "synthetic-delete"
                    ),
                    Capability(
                        "ObserveDeleteCurrentKey", "disabled", "observer-unproved"
                    ),
                ),
            ),
        )
    )

    plan = plan_operation(
        OperationShape(operation="delete", delete_scope="current-key"),
        contract_key=key,
        registry=registry,
    )

    assert plan.blocking_reasons == ("delete_capability_missing",)
    assert plan.executable is False
