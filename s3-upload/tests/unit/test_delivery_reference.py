from pathlib import Path

import pytest

from delivery_schema import (
    BODY_FIELDS, DISPOSITIONS, DeliverySchemaError, OBJECT_REFERENCE_FIELDS,
    OUTCOME_WRITE_CERTAINTY, VERIFICATION_CHANNELS, body_of, parse_typed,
    serialize_artifact,
)
from delivery_reference import (
    DISPOSITION_REQUIRED_CHANNELS, DISPOSITION_WRITE_CERTAINTY, ReferenceError,
    build_object_reference_v2, build_verification, parse_object_reference_v2,
)


GOLDENS = Path(__file__).parents[1] / "goldens" / "delivery"
SHA = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
AT = "2026-07-27T00:00:00Z"


def verification(channel="authenticated_full_get", **overrides):
    item = {"channel": channel, "size": 11, "sha256": SHA,
            "url_scope": "current-key", "verified_at": AT}
    item.update(overrides)
    return build_verification(**item)


def reference(**overrides):
    kwargs = {
        "access": {"mode": "private", "public_base_url": None,
                   "presign_expires_seconds": 3600},
        "content": {"size": 11, "sha256": SHA},
        "disposition": "adopted",
        "location": {"provider": "aws-s3", "endpoint": "https://s3.amazonaws.com",
                     "addressing": "virtual", "region": "us-east-1",
                     "bucket": "example-bucket", "key": "images/a.png",
                     "version_id": None},
        "operation_id": "0" * 32,
        "plan_hash": "sha256:" + "0" * 64,
        "plan_id": "1" * 32,
        "retention": {"mode": "retain", "days": None,
                      "enforcement": "external-unverified"},
        "root_recovery_id": "2" * 32,
        "target_contract": {"contract_version": 1},
        "target_contract_hash": "sha256:" + "3" * 64,
        "target_ref": "project:images",
        "verifications": [verification()],
    }
    kwargs.update(overrides)
    return build_object_reference_v2(**kwargs)


def test_object_reference_fields_are_locked():
    assert OBJECT_REFERENCE_FIELDS == (
        "access", "content", "content_stability", "disposition", "location",
        "object_written", "operation_id", "plan_hash", "plan_id", "retention",
        "root_recovery_id", "target_contract", "target_contract_hash",
        "target_ref", "verifications",
    )
    assert BODY_FIELDS["s3-upload.object-reference"] == OBJECT_REFERENCE_FIELDS


def test_every_registered_field_has_a_producer():
    assert set(body_of(reference())) == set(OBJECT_REFERENCE_FIELDS)


def test_the_three_dispositions_are_mutually_exclusive_on_write_certainty():
    assert DISPOSITION_WRITE_CERTAINTY == {
        "adopted": False, "created": True, "reconciled": None,
    }
    assert set(DISPOSITION_WRITE_CERTAINTY) == set(DISPOSITIONS)


def test_disposition_write_certainty_agrees_with_the_outcome_table():
    # Two tables, two modules: DISPOSITION_WRITE_CERTAINTY is what a reference
    # claims about the bytes, OUTCOME_WRITE_CERTAINTY is what the result
    # envelope claims about the same operation. Nothing derives one from the
    # other, so only this comparison stops them from drifting apart -- and it
    # has to compare values, not just key sets.
    assert set(DISPOSITION_WRITE_CERTAINTY) == set(DISPOSITIONS)
    for disposition in DISPOSITIONS:
        assert (DISPOSITION_WRITE_CERTAINTY[disposition]
                is OUTCOME_WRITE_CERTAINTY[disposition]), disposition


def test_object_written_is_derived_from_the_disposition_not_supplied():
    assert body_of(reference(disposition="adopted"))["object_written"] is False
    created = reference(disposition="created", verifications=[])
    assert body_of(created)["object_written"] is True
    settled = reference(disposition="reconciled")
    assert body_of(settled)["object_written"] is None
    # A full, otherwise valid argument set plus the derived key. Calling with
    # object_written alone would raise TypeError for the thirteen missing
    # keywords whether or not the constructor accepts it, so that spelling
    # would pass under the very mutation it is meant to catch.
    with pytest.raises(TypeError):
        reference(object_written=True)
    with pytest.raises(TypeError):
        reference(content_stability="version_pinned")


def test_reconciled_must_keep_object_written_null():
    item = body_of(reference(disposition="reconciled"))
    assert item["object_written"] is None
    assert item["disposition"] == "reconciled"


def test_adopted_and_reconciled_require_an_authenticated_full_get():
    assert DISPOSITION_REQUIRED_CHANNELS["adopted"] == ("authenticated_full_get",)
    assert DISPOSITION_REQUIRED_CHANNELS["reconciled"] == ("authenticated_full_get",)
    assert DISPOSITION_REQUIRED_CHANNELS["created"] == ()
    for disposition in ("adopted", "reconciled"):
        with pytest.raises(ReferenceError):
            reference(disposition=disposition, verifications=[])
        with pytest.raises(ReferenceError):
            reference(disposition=disposition,
                      verifications=[verification("anonymous_public_get")])


def test_an_unregistered_disposition_is_refused():
    with pytest.raises(ReferenceError):
        reference(disposition="verified")


def test_a_verification_must_match_the_content_it_certifies():
    with pytest.raises(ReferenceError):
        reference(verifications=[verification(size=12)])
    with pytest.raises(ReferenceError):
        reference(verifications=[verification(sha256="0" * 64)])


def test_verifications_are_sorted_unique_by_channel():
    both = [verification("authenticated_full_get"),
            verification("anonymous_public_get")]
    item = body_of(reference(disposition="adopted", verifications=both))
    assert [entry["channel"] for entry in item["verifications"]] == [
        "anonymous_public_get", "authenticated_full_get",
    ]
    with pytest.raises(ReferenceError):
        reference(verifications=[verification(), verification()])


def test_an_unregistered_verification_channel_is_refused():
    with pytest.raises(ReferenceError):
        build_verification(channel="head_object", size=11, sha256=SHA,
                           url_scope="current-key", verified_at=AT)
    assert "head_object" not in VERIFICATION_CHANNELS


def test_a_verification_with_an_unregistered_url_scope_is_refused():
    with pytest.raises(ReferenceError):
        build_verification(channel="authenticated_full_get", size=11, sha256=SHA,
                           url_scope="latest", verified_at=AT)


def test_a_verification_timestamp_must_be_rfc3339_utc_seconds():
    for stamp in ("2026-07-27T00:00:00+08:00", "2026-07-27 00:00:00Z",
                  "2026-07-27T00:00:00.500Z", ""):
        with pytest.raises(ReferenceError):
            verification(verified_at=stamp)


def test_a_verification_field_set_is_exact():
    with pytest.raises(ReferenceError):
        reference(verifications=[dict(verification(), etag="w/x")])
    stripped = verification()
    del stripped["url_scope"]
    with pytest.raises(ReferenceError):
        reference(verifications=[stripped])


def test_content_stability_is_derived_from_the_version_id():
    unpinned = body_of(reference())
    assert unpinned["content_stability"] == "current_key_unpinned"
    pinned = body_of(reference(location=dict(
        body_of(reference())["location"], version_id="v-1")))
    assert pinned["content_stability"] == "version_pinned"


def test_a_current_key_verification_never_claims_a_pinned_scope():
    with pytest.raises(ReferenceError):
        reference(verifications=[verification(url_scope="exact-version")])


def test_a_pinned_reference_still_accepts_a_current_key_verification():
    pinned = dict(body_of(reference())["location"], version_id="v-1")
    item = body_of(reference(location=pinned,
                             verifications=[verification(url_scope="exact-version")]))
    assert item["verifications"][0]["url_scope"] == "exact-version"
    settled = body_of(reference(location=pinned))
    assert settled["verifications"][0]["url_scope"] == "current-key"


def test_a_content_field_set_is_exact():
    with pytest.raises(ReferenceError):
        reference(content={"size": 11, "sha256": SHA, "etag": "w/x"})
    with pytest.raises(ReferenceError):
        reference(content={"size": 11})


def test_a_private_reference_never_carries_a_public_base():
    with pytest.raises(ReferenceError):
        reference(access={"mode": "private",
                          "public_base_url": "https://cdn.example.com",
                          "presign_expires_seconds": 3600})
    with pytest.raises(ReferenceError):
        reference(access={"mode": "public", "public_base_url": None,
                          "presign_expires_seconds": 3600})
    item = body_of(reference(access={"mode": "public",
                                     "public_base_url": "https://cdn.example.com",
                                     "presign_expires_seconds": None}))
    assert item["access"]["mode"] == "public"


def test_an_unnormalized_public_base_is_refused():
    with pytest.raises(ReferenceError):
        reference(access={"mode": "public",
                          "public_base_url": "https://cdn.example.com/assets/",
                          "presign_expires_seconds": None})


@pytest.mark.parametrize("base", [
    "http://cdn.example.com",          # SchemaError: not HTTPS
    "https://cdn.example.com/../a",    # SchemaError: dot segment
    "https://[::1",                    # bare ValueError from urlsplit
])
def test_a_rejected_public_base_never_escapes_as_a_foreign_exception(base):
    # normalize_public_base does not raise only SchemaError: urlsplit raises a
    # plain ValueError("Invalid IPv6 URL") on the third case, which a
    # SchemaError-only clause would let past the constructor. Checked by
    # running each of these through normalize_public_base directly.
    with pytest.raises(ReferenceError):
        reference(access={"mode": "public", "public_base_url": base,
                          "presign_expires_seconds": None})


def test_the_reference_does_not_alias_the_callers_nested_objects():
    contract = {"contract_version": 1}
    item = reference(target_contract=contract)
    contract["contract_version"] = 99
    assert body_of(item)["target_contract"] == {"contract_version": 1}


def test_the_reference_round_trips_through_the_typed_parser():
    item = reference()
    raw = serialize_artifact(item).decode("utf-8")
    assert parse_object_reference_v2(raw) == item
    assert parse_typed(raw, expected_type="s3-upload.object-reference") == item


def test_parse_refuses_a_hand_edited_reference_whose_disposition_and_write_disagree():
    # The material is a hand-edited file on disk, so nothing the constructor
    # does can make this pass: only re-deriving on the read side can.
    raw = (GOLDENS / "object-reference-v2-tampered.json").read_text(
        encoding="utf-8").strip()
    assert '"disposition":"adopted"' in raw and '"object_written":true' in raw
    assert parse_typed(raw, expected_type="s3-upload.object-reference")
    with pytest.raises(ReferenceError):
        parse_object_reference_v2(raw)


def test_a_v1_object_reference_is_incompatible_with_the_v2_parser():
    raw = (GOLDENS / "legacy-object-reference-v1.json").read_text(
        encoding="utf-8").strip()
    with pytest.raises((ReferenceError, DeliverySchemaError)):
        parse_object_reference_v2(raw)


def test_the_reference_carries_no_secret_shaped_field():
    raw = serialize_artifact(reference()).decode("utf-8")
    for forbidden in ("credential", "authorization", "etag", "session_token",
                      "access_key", "signature", "checkpoint"):
        assert forbidden not in raw.lower()


def test_the_adopted_golden_is_byte_stable():
    raw = (GOLDENS / "object-reference-v2-adopted.json").read_text(
        encoding="utf-8").strip()
    assert serialize_artifact(parse_object_reference_v2(raw)).decode("utf-8") == raw
