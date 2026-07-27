import ast
import inspect
from pathlib import Path
from urllib.parse import quote

import pytest

from delivery_schema import (
    BODY_FIELDS, DISPOSITIONS, DeliverySchemaError, OBJECT_REFERENCE_FIELDS,
    OUTCOME_WRITE_CERTAINTY, VERIFICATION_CHANNELS, body_of, parse_typed,
    serialize_artifact,
)
from target_contract import contract_hash
import delivery_reference
from delivery_reference import (
    CONTENT_STABILITIES, DISPOSITION_REQUIRED_CHANNELS,
    DISPOSITION_WRITE_CERTAINTY, ReferenceError, build_object_reference_v2,
    build_verification, parse_object_reference_v2,
)


GOLDENS = Path(__file__).parents[1] / "goldens" / "delivery"
SHA = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
AT = "2026-07-27T00:00:00Z"
# Credential-shaped material built here and nowhere else, so the credential
# screen is exercised against a string the module under test has no other way
# of knowing about. AWS's own documentation example key -- never a live secret.
# Deliberately not AWS-key-shaped. An earlier revision used AWS's own
# documentation example secret key; it is public sample text and never a live
# credential, but its shape trips secret scanners and push protection on the
# public repository this module ships from. The screen does not look at the
# shape of the string -- it refuses whatever the caller declared as a
# credential when that string appears inside a version_id -- so an obvious
# placeholder exercises exactly the same code path.
SECRET = "CREDENTIAL/VALUE-THAT-MUST-NOT-APPEAR"
# The five addressing fields are here because the reference binds location to
# them (LOCATION_BINDING_FIELDS); the rest of a contract_snapshot deliberately
# is not, because the constructor does not require one and Task 8 owns that
# shape. Spelled out once and read back by location() below, so the two sides
# of the binding cannot drift apart in the fixture itself.
CONTRACT = {
    "contract_version": 1,
    "addressing": "virtual",
    "bucket": "example-bucket",
    "endpoint": "https://s3.amazonaws.com",
    "provider": "aws-s3",
    "region": "us-east-1",
}


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
        "location": {"provider": CONTRACT["provider"],
                     "endpoint": CONTRACT["endpoint"],
                     "addressing": CONTRACT["addressing"],
                     "region": CONTRACT["region"],
                     "bucket": CONTRACT["bucket"], "key": "images/a.png",
                     "version_id": None},
        "operation_id": "0" * 32,
        "plan_hash": "sha256:" + "0" * 64,
        "plan_id": "1" * 32,
        "retention": {"mode": "retain", "days": None,
                      "enforcement": "external-unverified"},
        "root_recovery_id": "2" * 32,
        "target_contract": CONTRACT,
        # Recomputed, not spelled out: the two fields are bound, so a literal
        # here would be a second source of truth for the same value.
        "target_contract_hash": contract_hash(CONTRACT),
        "target_ref": "project:images",
        "verifications": [verification()],
    }
    kwargs.update(overrides)
    return build_object_reference_v2(**kwargs)


def location(**overrides):
    item = dict(body_of(reference())["location"])
    item.update(overrides)
    return item


def retention(**overrides):
    item = {"mode": "retain", "days": None, "enforcement": "external-unverified"}
    item.update(overrides)
    return item


def access(**overrides):
    item = {"mode": "private", "public_base_url": None,
            "presign_expires_seconds": 3600}
    item.update(overrides)
    return item


def test_object_reference_fields_are_locked():
    assert OBJECT_REFERENCE_FIELDS == (
        "access", "content", "content_stability", "disposition", "location",
        "object_written", "operation_id", "plan_hash", "plan_id", "retention",
        "root_recovery_id", "target_contract", "target_contract_hash",
        "target_ref", "verifications",
    )
    assert BODY_FIELDS["s3-upload.object-reference"] == OBJECT_REFERENCE_FIELDS


# test_every_registered_field_has_a_producer used to sit here, asserting
# set(body_of(reference())) == set(OBJECT_REFERENCE_FIELDS). build_typed calls
# _exact_body (scripts/delivery_schema.py:198-213), which raises unless those
# two sets are already equal, so the assert line could never be the failure
# point: deleting a produced field turns reference() itself into an exception
# and reddens eleven tests, the assert among them only by collateral. Removed
# rather than rewritten -- nothing is lost, because a registry entry with no
# producer still fails test_object_reference_fields_are_locked and every test
# that calls reference().


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
    pinned = body_of(reference(location=location(version_id="v-1"),
                               credentials=()))
    assert pinned["content_stability"] == "version_pinned"


def test_a_current_key_verification_never_claims_a_pinned_scope():
    with pytest.raises(ReferenceError):
        reference(verifications=[verification(url_scope="exact-version")])


def test_a_pinned_reference_still_accepts_a_current_key_verification():
    pinned = location(version_id="v-1")
    item = body_of(reference(location=pinned, credentials=(),
                             verifications=[verification(url_scope="exact-version")]))
    assert item["verifications"][0]["url_scope"] == "exact-version"
    settled = body_of(reference(location=pinned, credentials=()))
    assert settled["verifications"][0]["url_scope"] == "current-key"


def test_the_content_stability_vocabulary_is_locked_and_wired_to_the_scope_table():
    # CONTENT_STABILITIES had no producer, no consumer and no test: rewriting
    # it to ("nonsense",) left all 943 tests green. It is now the single source
    # of both literals used by the scope table and by the derivation, and this
    # is where that wiring is checked.
    assert CONTENT_STABILITIES == ("current_key_unpinned", "version_pinned")
    assert set(delivery_reference._SCOPES_FOR_STABILITY) == set(CONTENT_STABILITIES)
    assert body_of(reference())["content_stability"] in CONTENT_STABILITIES
    # The three assertions above cannot see the wiring itself. Replacing
    # `_UNPINNED, _PINNED = CONTENT_STABILITIES` with the same two literals
    # spelled out by hand leaves the whole suite green (M9, full scope): the
    # values are identical either way, so no behaviour distinguishes a
    # vocabulary that is read from a vocabulary that is merely duplicated --
    # and a duplicate is what let CONTENT_STABILITIES dangle in the first
    # place. Same situation as gate.arm(), and the same answer: when the
    # invariant is structural, pin the structure.
    tree = ast.parse(inspect.getsource(delivery_reference))
    binding = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Tuple)
        and [getattr(name, "id", None) for name in node.targets[0].elts]
        == ["_UNPINNED", "_PINNED"]
    ]
    assert len(binding) == 1, "_UNPINNED/_PINNED must be unpacked exactly once"
    assert isinstance(binding[0].value, ast.Name)
    assert binding[0].value.id == "CONTENT_STABILITIES"

    table = next(
        node.value for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and "_SCOPES_FOR_STABILITY" in {
            getattr(target, "id", None)
            for target in (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
        }
    )
    assert [getattr(key, "id", None) for key in table.keys] == ["_UNPINNED", "_PINNED"]

    builder = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "build_object_reference_v2")
    derivation = next(node for node in ast.walk(builder)
                      if isinstance(node, ast.Assign)
                      and getattr(node.targets[0], "id", None) == "stability")
    names = {child.id for child in ast.walk(derivation.value)
             if isinstance(child, ast.Name)}
    assert {"_PINNED", "_UNPINNED"} <= names
    assert not [child for child in ast.walk(builder)
                if isinstance(child, ast.Constant)
                and child.value in CONTENT_STABILITIES]


def test_a_content_field_set_is_exact():
    with pytest.raises(ReferenceError):
        reference(content={"size": 11, "sha256": SHA, "etag": "w/x"})
    with pytest.raises(ReferenceError):
        reference(content={"size": 11})


@pytest.mark.parametrize("field,value", [
    ("size", -1), ("size", True), ("size", "11"), ("size", 1.0), ("size", None),
    ("sha256", SHA.upper()), ("sha256", SHA[:63]), ("sha256", SHA + "0"),
    ("sha256", "sha256:" + SHA), ("sha256", ""), ("sha256", None),
])
def test_content_size_and_sha256_are_validated(field, value):
    # disposition="created" with no verifications on purpose. Written the
    # obvious way -- keeping the default adopted fixture and its one
    # verification -- this test passed even with _size and _sha256 gutted to
    # `return value`, because the verification-vs-content comparison then
    # rejected the reference instead. Measured, not guessed: the mutation run
    # came back 1044 passed. A created reference carries no verification, so
    # nothing but _content can refuse these values.
    with pytest.raises(ReferenceError):
        reference(disposition="created", verifications=[],
                  content=dict({"size": 11, "sha256": SHA}, **{field: value}))


# Every bad value here is a literal written in this file, sharing no constant
# with _ID_RE / _DIGEST_RE: the test states the shapes, it does not ask the
# implementation what they are.
_BAD_IDENTIFIERS = [
    "0" * 31, "0" * 33, "A" * 32, "g" * 32, "", None, 0, "0" * 16 + "-" * 16,
]
_BAD_DIGESTS = [
    "0" * 64, "sha256:" + "0" * 63, "sha256:" + "0" * 65, "sha256:" + "A" * 64,
    "sha1:" + "0" * 64, "sha256:", "", None,
]


@pytest.mark.parametrize("field,value", [
    (field, value)
    for field in ("operation_id", "plan_id", "root_recovery_id")
    for value in _BAD_IDENTIFIERS
] + [
    (field, value)
    for field in ("plan_hash", "target_contract_hash")
    for value in _BAD_DIGESTS
])
def test_identifiers_and_digests_are_shaped(field, value):
    with pytest.raises(ReferenceError):
        reference(**{field: value})


@pytest.mark.parametrize("value", ["", None, 7, "project:\x00images",
                                   "project:im\nages"])
def test_the_target_ref_must_be_non_empty_single_line_text(value):
    with pytest.raises(ReferenceError):
        reference(target_ref=value)


@pytest.mark.parametrize("value", ["", None, 7, "ex\x00ample", "ex\nample"])
@pytest.mark.parametrize("field", ["bucket", "provider", "region"])
def test_the_location_text_fields_must_be_non_empty_single_line_text(field, value):
    with pytest.raises(ReferenceError):
        reference(location=location(**{field: value}))


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


@pytest.mark.parametrize("expires", [0, 604801, -1, True, None, "3600", 1.0])
def test_presign_expiry_bounds_are_enforced(expires):
    with pytest.raises(ReferenceError):
        reference(access=access(presign_expires_seconds=expires))
    assert body_of(reference(access=access(presign_expires_seconds=1)))
    assert body_of(reference(access=access(presign_expires_seconds=604800)))


@pytest.mark.parametrize("mode", ["", "Private", "public-read", "unlisted",
                                  None, 1])
def test_an_unknown_access_mode_is_refused(mode):
    with pytest.raises(ReferenceError):
        reference(access=access(mode=mode))


def test_an_access_field_set_is_exact():
    with pytest.raises(ReferenceError):
        reference(access=dict(access(), acl="public-read"))
    stripped = access()
    del stripped["public_base_url"]
    with pytest.raises(ReferenceError):
        reference(access=stripped)


@pytest.mark.parametrize("value", [
    {"mode": "retain", "days": 5, "enforcement": "external-unverified"},
    {"mode": "expire", "days": None, "enforcement": "external-unverified"},
    {"mode": "expire", "days": 0, "enforcement": "external-unverified"},
    {"mode": "expire", "days": -1, "enforcement": "external-unverified"},
    {"mode": "expire", "days": True, "enforcement": "external-unverified"},
    {"mode": "expire", "days": "7", "enforcement": "external-unverified"},
    {"mode": "delete", "days": None, "enforcement": "external-unverified"},
    {"mode": None, "days": None, "enforcement": "external-unverified"},
    {"mode": "retain", "days": None, "enforcement": "provider-enforced"},
    {"mode": "retain", "days": None, "enforcement": None},
    {"mode": "retain", "days": None},
    {"mode": "retain", "days": None, "enforcement": "external-unverified",
     "locked": True},
])
def test_a_retention_policy_is_validated_field_by_field(value):
    with pytest.raises(ReferenceError):
        reference(retention=value)


def test_a_retention_day_count_beyond_the_safe_integer_is_refused():
    # The failure has to land in the constructor. Before this bound
    # build_object_reference_v2 returned an artifact for days=2**53 and only
    # serialize_artifact refused it -- i.e. the reference blew up at the write
    # in front of result_out, after the caller had been told it was valid.
    with pytest.raises(ReferenceError):
        reference(retention=retention(mode="expire", days=1 << 53))
    largest = reference(retention=retention(mode="expire", days=(1 << 53) - 1))
    assert body_of(largest)["retention"]["days"] == (1 << 53) - 1
    # Same value, proven writable: that is what the bound is protecting.
    assert serialize_artifact(largest)


@pytest.mark.parametrize("key", ["/leading", "trailing/", "a/../b", "a//b",
                                 "a/./b", "", "a\nb"])
def test_a_reference_refuses_an_invalid_object_key(key):
    # v1 refused every one of these through validate_object_key
    # (artifacts._validate_reference, scripts/artifacts.py:167); v2 accepted
    # all four of the first ones until this check was restored.
    with pytest.raises(ReferenceError):
        reference(location=location(key=key))


@pytest.mark.parametrize("endpoint", [
    "https://S3.AMAZONAWS.COM:443",   # host case and default port
    "https://s3.amazonaws.com:443",   # default port left in
    "https://s3.amazonaws.com/",      # trailing slash
    "s3.amazonaws.com",               # scheme implied, not written
    "not-a-url",
    "ftp://s3.amazonaws.com",
    "https://s3.amazonaws.com?x=1",
    "https://[::1",                   # bare ValueError from urlsplit
])
def test_a_reference_refuses_an_unnormalized_endpoint(endpoint):
    with pytest.raises(ReferenceError):
        reference(location=location(endpoint=endpoint))


def test_a_version_id_never_carries_credential_material():
    # SECRET is built at the top of this file and handed in as the caller's
    # credential material; the module has no other route to it, so nothing
    # here is comparing the implementation against itself.
    # The third carrier hides the secret behind percent-encoding, which is why
    # the screen decodes before comparing. Derived rather than spelled out so
    # it cannot drift away from SECRET, with the encoding itself asserted --
    # a placeholder with nothing to encode would turn this into a duplicate of
    # the second carrier without anyone noticing.
    encoded = quote(SECRET, safe="")
    assert encoded != SECRET
    for carrier in (SECRET, "v-" + SECRET, "v-" + encoded):
        with pytest.raises(ReferenceError):
            reference(location=location(version_id=carrier),
                      credentials=[SECRET])
    # Shape, not just leakage: v1's screen also caps the length and admits
    # only printable ASCII.
    for malformed in ("", "v 1", "v\x7f1", "v" * 4097, "v-é", 7):
        with pytest.raises(ReferenceError):
            reference(location=location(version_id=malformed))
    clean = reference(location=location(version_id="v-1"), credentials=[SECRET])
    assert body_of(clean)["location"]["version_id"] == "v-1"


def test_the_credential_screen_runs_on_the_read_side_too():
    # Read/write symmetry: an artifact produced elsewhere, with no credential
    # known at construction time, must still be refused by the reader that
    # does know the credential. This is why parse_object_reference_v2 takes
    # credentials rather than only the constructor.
    leaked = reference(location=location(version_id="v-" + SECRET),
                       credentials=())
    raw = serialize_artifact(leaked).decode("utf-8")
    assert parse_object_reference_v2(raw) == leaked
    with pytest.raises(ReferenceError):
        parse_object_reference_v2(raw, credentials=[SECRET])


class _LibraryBug(ValueError):
    """A bare ValueError out of a v2_schema helper, i.e. a bug in that helper.

    A ValueError subclass but not a SchemaError, so pytest.raises(_LibraryBug)
    cannot be satisfied by the ReferenceError the module raises for genuinely
    invalid keys -- ReferenceError is a ValueError too, which is why the
    assertion names this type instead of ValueError.
    """


def test_a_library_bug_in_validate_object_key_is_not_reported_as_an_invalid_key(
        monkeypatch):
    # The narrow except SchemaError, stated as behaviour. Widening it back to
    # except ValueError makes the raise below come out as
    # ReferenceError("invalid location.key"), i.e. the caller is told their
    # data is bad when in fact the validator is broken -- and nothing else in
    # the suite can tell the difference (M18, full scope, survived).
    # The patch lands on delivery_reference's own global rather than on
    # v2_schema: _location resolves the name at call time through this
    # module's namespace, so patching the source module would not be seen.
    def boom(value, **kwargs):
        raise _LibraryBug("simulated implementation bug")

    monkeypatch.setattr(delivery_reference, "validate_object_key", boom)
    with pytest.raises(_LibraryBug):
        reference()
    # And the real invalid keys are still refused as invalid keys, so this is
    # a narrowing of blame and not a hole: SchemaError is all validate_object_key
    # raises, checked against scripts/v2_schema.py:215-229.
    monkeypatch.undo()
    with pytest.raises(ReferenceError):
        reference(location=location(key="/leading"))


def test_malformed_credentials_are_the_callers_bug_not_the_artifacts():
    # parse_object_reference_v2(raw, credentials=[7]) used to answer
    # ReferenceError("object reference body is not constructible") -- a verdict
    # on somebody else's artifact, produced by a TypeError raised inside this
    # process because of this caller's own argument. The question this module
    # answers is "can I trust the artifact I was handed", so getting that
    # answer wrong is worse than any refusal.
    raw = serialize_artifact(
        reference(location=location(version_id="v-1"),
                  credentials=())).decode("utf-8")
    for malformed in ([7], None, SECRET, object()):
        with pytest.raises(TypeError):
            parse_object_reference_v2(raw, credentials=malformed)
        with pytest.raises(TypeError):
            reference(location=location(version_id="v-1"), credentials=malformed)
        # Screened independently of the data being screened: a null version_id
        # gives the screen nothing to look at, and the malformed argument used
        # to be accepted in silence on exactly that path.
        with pytest.raises(TypeError):
            reference(credentials=malformed)
    # A one-shot iterable is legal and is consumed exactly once, at the entry.
    assert body_of(reference(credentials=iter([SECRET])))["content"]
    with pytest.raises(ReferenceError):
        reference(location=location(version_id="v-" + SECRET),
                  credentials=iter([SECRET]))


def test_a_public_reference_may_still_carry_only_authenticated_evidence():
    # Records what the schema allows TODAY, deliberately. Task 8 has not
    # implemented anonymous verification yet, so a rule requiring an anonymous
    # channel here would make the references Task 6 and Task 7 produce
    # unconstructible. Task 8 must FLIP this test to require the anonymous
    # channel -- deleting it would remove the only place the gap is written
    # down. Same shape as the Step 0 assertion that pins 403 to
    # blocking_reasons == [].
    public = body_of(reference(access=access(
        mode="public", public_base_url="https://cdn.example.com",
        presign_expires_seconds=None)))
    assert public["access"]["mode"] == "public"
    assert [entry["channel"] for entry in public["verifications"]] == [
        "authenticated_full_get",
    ]
    # And the mirror image: created claims object_written=True while carrying
    # only anonymous evidence.
    created = body_of(reference(
        disposition="created",
        verifications=[verification("anonymous_public_get")]))
    assert created["object_written"] is True
    assert [entry["channel"] for entry in created["verifications"]] == [
        "anonymous_public_get",
    ]


def test_the_reference_does_not_alias_the_callers_nested_objects():
    contract = dict(CONTRACT)
    item = reference(target_contract=contract)
    contract["contract_version"] = 99
    assert body_of(item)["target_contract"] == CONTRACT


def test_a_reference_may_not_point_where_its_target_contract_does_not():
    # v1 recomputed target_fingerprint from provider / endpoint / addressing /
    # region / bucket and compared it against the value in the artifact
    # (artifacts._validate_reference, scripts/artifacts.py:170-179). _contract
    # is a different check and not a substitute: it proves the contract blob
    # still hashes to the digest beside it, which any self-consistent pair
    # satisfies. Measured before this binding existed: a contract naming
    # https://s3.amazonaws.com beside a location naming https://evil.example,
    # with attacker-bucket, eu-west-9 and provider minio, was accepted by the
    # constructor and by the strict read side alike.
    elsewhere = dict(CONTRACT, endpoint="https://evil.example",
                     bucket="attacker-bucket", region="eu-west-9",
                     provider="minio")
    with pytest.raises(ReferenceError):
        reference(target_contract=elsewhere,
                  target_contract_hash=contract_hash(elsewhere))
    # One field at a time, so no single comparison can stand in for the other
    # four: with only the combined case above, four of the five could be
    # dropped and nothing would say so.
    here = body_of(reference())["location"]
    for field, value in (("addressing", "path"), ("bucket", "other-bucket"),
                         ("endpoint", "https://s3.eu-west-9.amazonaws.com"),
                         ("provider", "minio"), ("region", "eu-west-9")):
        drifted = dict(CONTRACT, **{field: value})
        assert drifted[field] != here[field], field
        with pytest.raises(ReferenceError):
            reference(target_contract=drifted,
                      target_contract_hash=contract_hash(drifted))
    # A contract that names no location at all is refused rather than waved
    # through: leaving the five optional would make the binding avoidable by
    # deleting them. This is the whole requirement -- nothing here asks
    # target_contract to be a complete contract_snapshot, which is Task 8's
    # shape to settle.
    #
    # The fourth case is a different refusal and is here deliberately: all five
    # fields are present, one of them is null, and it is refused as drift
    # ("location disagrees with target_contract: region"), not as absence --
    # run, not assumed. A field that exists but carries null is not a missing
    # field, so nothing in _bind_location's absence half would catch it; the
    # equality half does.
    for absent in ({}, {"contract_version": 1}, {"lol": "not a contract"},
                   dict(CONTRACT, region=None)):
        with pytest.raises(ReferenceError):
            reference(target_contract=absent,
                      target_contract_hash=contract_hash(absent))
    # And the read side, which is where an artifact somebody else produced
    # arrives. Serialized and re-parsed, so the constructor's own refusal
    # cannot be what makes this pass.
    item = dict(reference())
    item["target_contract"] = elsewhere
    item["target_contract_hash"] = contract_hash(elsewhere)
    raw = serialize_artifact(item).decode("utf-8")
    assert parse_typed(raw, expected_type="s3-upload.object-reference")
    with pytest.raises(ReferenceError):
        parse_object_reference_v2(raw)


def test_a_versioned_reference_may_not_be_built_unscreened():
    # The same discipline delivery_records._object_reference enforces one layer
    # out, applied at the layer that actually writes version_id. Measured
    # before the sentinel: build_object_reference_v2 with
    # version_id="v-" + SECRET and no credentials argument at all was accepted,
    # and Task 6/7/8 call this constructor directly with real provider values.
    with pytest.raises(ReferenceError):
        reference(location=location(version_id="v-1"))
    with pytest.raises(ReferenceError):
        reference(location=location(version_id="v-" + SECRET))
    # What is refused is not declaring, not holding nothing: an explicit empty
    # sequence still builds, and still runs the charset half of the screen.
    assert body_of(reference(location=location(version_id="v-1"),
                             credentials=()))["location"]["version_id"] == "v-1"
    with pytest.raises(ReferenceError):
        reference(location=location(version_id="v 1"), credentials=())
    # An unversioned reference needs no declaration, which is what keeps this a
    # rule about provider strings rather than about paperwork.
    assert body_of(reference())["location"]["version_id"] is None


def test_parse_refuses_a_boolean_written_as_a_json_number():
    # Python calls False == 0 and True == 1, and object_written is stripped out
    # of the rebuild input as a DERIVED_FIELD, so comparing the rebuilt body to
    # the parsed one as dicts saw no difference between `false` and `0`: the
    # rebuild produced False, the artifact carried 0, and the dicts compared
    # equal. Run, not assumed -- this artifact parsed clean, and the only thing
    # in the process that refused it was delivery_records' identity
    # comparison, which a reference parsed on its own never reaches.
    raw = serialize_artifact(reference()).decode("utf-8")
    assert '"object_written":false' in raw
    numeric = raw.replace('"object_written":false', '"object_written":0')
    # The envelope and the field set are intact, so nothing ahead of the
    # re-derivation has any reason to refuse it.
    assert parse_typed(numeric, expected_type="s3-upload.object-reference")
    with pytest.raises(ReferenceError):
        parse_object_reference_v2(numeric)


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
