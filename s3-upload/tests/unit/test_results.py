import pytest

from results import ResultError, build_result, exit_code_for_result, validate_result


EMPTY_RETENTION = {"mode": None, "days": None, "enforcement": None}
RETAIN = {"mode": "retain", "days": None, "enforcement": "external-unverified"}


def test_upload_dry_run_has_all_keys_and_exit_depends_on_plan():
    result = build_result(
        "upload",
        "dry_run",
        object_written=False,
        url_kind="presigned",
        retention=RETAIN,
        plan={"executable": True},
    )
    assert list(result) == [
        "schema_version", "operation", "status", "object_written", "object_reference",
        "url", "url_kind", "expires_at", "retention", "delete_scope",
        "deleted_version_id", "checkpoint_id", "plan",
    ]
    assert exit_code_for_result(result) == 0
    result["plan"] = {"executable": False}
    assert exit_code_for_result(result) == 2


def test_ambiguous_result_requires_checkpoint_and_never_claims_write():
    result = build_result(
        "upload",
        "ambiguous",
        object_written=None,
        retention=RETAIN,
        checkpoint_id="123456789abc4def8123456789abcdef",
    )
    assert result["object_reference"] is None and result["url"] is None
    assert exit_code_for_result(result) == 1
    result["checkpoint_id"] = None
    with pytest.raises(ResultError, match="checkpoint"):
        validate_result(result)


def test_collision_and_delete_exit_contracts():
    collision = build_result("upload", "collision", object_written=False, retention=RETAIN)
    assert exit_code_for_result(collision) == 4

    delete_dry_run = build_result(
        "delete",
        "dry_run",
        object_reference={"placeholder": True},
        retention=RETAIN,
        delete_scope="current-key",
        plan={"executable": True},
        validate_reference=False,
    )
    assert delete_dry_run["object_written"] is None
    assert exit_code_for_result(delete_dry_run) == 0


def test_plan_is_only_allowed_for_dry_run():
    with pytest.raises(ResultError, match="plan"):
        build_result("url", "ok", plan={"executable": True})


def test_retention_container_is_always_present_and_closed():
    result = build_result("abort", "aborted")
    assert result["retention"] == EMPTY_RETENTION
    result["retention"]["extra"] = True
    with pytest.raises(ResultError, match="retention"):
        validate_result(result)


def test_presigned_url_requires_expiry_and_public_url_forbids_it():
    with pytest.raises(ResultError, match="expires_at"):
        build_result("url", "ok", url="https://example.test", url_kind="presigned", retention=RETAIN)
    with pytest.raises(ResultError, match="public"):
        build_result(
            "url", "ok", url="https://example.test", url_kind="public",
            expires_at="2026-07-22T12:00:00Z", retention=RETAIN,
        )
