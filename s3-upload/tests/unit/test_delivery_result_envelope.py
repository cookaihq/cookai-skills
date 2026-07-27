import pytest

from action_registry import (
    ACTIONS, AUTHORIZED_ACTION_ORDER, RECOVERY_STATES, allowed_actions,
)
import delivery_reference
from delivery_reference import URL_SCOPES, build_object_reference_v2
from delivery_records import (
    EMPTY_CONTENT_VERIFICATION, RecordError, authorization_required, build_result,
    result_hash,
)
from delivery_schema import (
    OUTCOME_WRITE_CERTAINTY, RESULT_FIELDS, SCHEMA_VERSIONS, URL_KINDS,
    VERIFICATION_CHANNELS, body_of, serialize_artifact,
)
from target_contract import contract_hash


SHA = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
AT = "2026-07-27T00:00:00Z"
CONTRACT = {"contract_version": 1}
# Credential-shaped material declared here and nowhere else, so the screen is
# exercised against a string neither module has another route to. Obvious
# placeholder text, not a key-shaped string: this file ships in a public
# repository with push protection.
SECRET = "CREDENTIAL/VALUE-THAT-MUST-NOT-APPEAR"


def reference(**overrides):
    location = {"provider": "aws-s3", "endpoint": "https://s3.amazonaws.com",
                "addressing": "virtual", "region": "us-east-1",
                "bucket": "example-bucket", "key": "images/a.png",
                "version_id": None}
    location.update(overrides.pop("location", {}))
    kwargs = {
        "access": {"mode": "private", "public_base_url": None,
                   "presign_expires_seconds": 3600},
        "content": {"size": 11, "sha256": SHA},
        "disposition": "created",
        "location": location,
        "operation_id": "0" * 32,
        "plan_hash": "sha256:" + "0" * 64,
        "plan_id": "1" * 32,
        "retention": {"mode": "retain", "days": None,
                      "enforcement": "external-unverified"},
        "root_recovery_id": "2" * 32,
        "target_contract": CONTRACT,
        "target_contract_hash": contract_hash(CONTRACT),
        "target_ref": "project:images",
        "verifications": [],
    }
    kwargs.update(overrides)
    return build_object_reference_v2(**kwargs)


def result(**overrides):
    kwargs = {
        "operation": "publish", "operation_id": "0" * 32, "plan_id": "1" * 32,
        "plan_hash": "sha256:" + "0" * 64,
        "target_contract_hash": "sha256:" + "1" * 64,
        "recovery_id": "2" * 32, "root_recovery_id": "2" * 32,
        "state": "terminal_unacknowledged", "capabilities_ok": True,
        "outcome": "created",
    }
    kwargs.update(overrides)
    return build_result(**kwargs)


def test_result_fields_are_locked_at_twenty_three():
    assert RESULT_FIELDS == (
        "allowed_actions", "authorization_required", "blocking_reasons",
        "content_verification", "object_reference", "object_written", "operation",
        "operation_id", "outcome", "plan_hash", "plan_id",
        "predecessor_operation_id", "predecessor_result_hash", "recovery_id",
        "recovery_state", "result_hash", "retry_safe", "root_recovery_id",
        "target_contract_hash", "url", "url_expires_at", "url_kind", "url_scope",
    )
    assert len(RESULT_FIELDS) == 23
    assert set(body_of(result())) == set(RESULT_FIELDS)


def test_the_result_artifact_type_and_version_are_unchanged():
    # The field set was redefined in place rather than minting
    # (s3-upload.result, 2). This is the assertion that makes that decision
    # visible instead of implicit; see the comment above RESULT_FIELDS for the
    # three facts the exemption rests on.
    assert result()["artifact_type"] == "s3-upload.result"
    assert result()["schema_version"] == 1
    assert SCHEMA_VERSIONS["s3-upload.result"] == 1


def test_object_written_must_agree_with_the_outcome():
    for outcome, certainty in OUTCOME_WRITE_CERTAINTY.items():
        state = "terminal_unacknowledged" if certainty is not False else "blocked"
        body = body_of(result(outcome=outcome, state=state, capabilities_ok=False))
        assert body["outcome"] == outcome
        assert body["object_written"] is certainty, outcome


def test_an_unregistered_outcome_is_refused():
    # "verified" is deliberately plausible: it is what a reader would guess the
    # verification pipeline emits, and it is not in the vocabulary.
    for outcome in ("verified", "ok", "created ", "", None, 7):
        with pytest.raises(RecordError):
            result(outcome=outcome)


def test_inapplicable_fields_are_explicit_nulls_and_empty_containers():
    body = body_of(result(outcome="unknown", state="in_flight_unknown"))
    assert body["object_reference"] is None
    assert body["url"] is None and body["url_kind"] is None
    assert body["url_scope"] is None and body["url_expires_at"] is None
    # A closed container with nothing in it, not null: "nothing was verified"
    # is a statement about this result, while null would say verification does
    # not apply to it -- and it always applies.
    assert body["content_verification"] == {
        "channels": [], "sha256": None, "size": None, "verified_at": None,
    }
    assert body["content_verification"] == EMPTY_CONTENT_VERIFICATION
    assert body["blocking_reasons"] == []


def test_the_empty_verification_container_is_not_shared_between_results():
    first = body_of(result())["content_verification"]
    first["channels"].append("authenticated_full_get")
    assert body_of(result())["content_verification"]["channels"] == []


def test_a_url_requires_its_kind_scope_and_matching_expiry():
    url = "https://cdn.example.com/images/a.png"
    with pytest.raises(RecordError):
        result(url=url)
    with pytest.raises(RecordError):
        result(url=url, url_kind="public", url_scope="current-key",
               url_expires_at=AT)
    with pytest.raises(RecordError):
        result(url=url, url_kind="presigned", url_scope="current-key")
    with pytest.raises(RecordError):
        result(url=url, url_kind="signed", url_scope="current-key")
    with pytest.raises(RecordError):
        result(url=url, url_kind="public", url_scope="whole-bucket")
    with pytest.raises(RecordError):
        result(url="", url_kind="public", url_scope="current-key")
    for orphan in ({"url_kind": "public"}, {"url_scope": "current-key"},
                   {"url_expires_at": AT}):
        with pytest.raises(RecordError):
            result(**orphan)
    public = body_of(result(url=url, url_kind="public", url_scope="current-key"))
    assert (public["url"], public["url_kind"], public["url_scope"],
            public["url_expires_at"]) == (url, "public", "current-key", None)
    presigned = body_of(result(url=url, url_kind="presigned",
                               url_scope="exact-version", url_expires_at=AT))
    assert presigned["url_expires_at"] == AT


def test_a_presigned_expiry_must_be_rfc3339_utc_seconds():
    url = "https://cdn.example.com/images/a.png"
    for bad in ("2026-07-27", "2026-07-27T00:00:00+08:00", "2026-07-27T00:00:00.5Z",
                "", 0, True):
        with pytest.raises(RecordError):
            result(url=url, url_kind="presigned", url_scope="current-key",
                   url_expires_at=bad)


def test_the_url_vocabularies_are_closed_and_have_a_single_home():
    assert URL_KINDS == ("public", "presigned")
    assert URL_SCOPES == ("current-key", "exact-version")
    # delivery_records reads URL_SCOPES from delivery_reference rather than
    # restating it: a second spelling is how a vocabulary comes to dangle.
    import delivery_records
    assert delivery_records.URL_SCOPES is delivery_reference.URL_SCOPES


def test_the_content_verification_container_is_closed_and_self_consistent():
    good = {"channels": ["authenticated_full_get"], "sha256": SHA, "size": 11,
            "verified_at": AT}
    assert body_of(result(content_verification=good))["content_verification"] == good
    both = dict(good, channels=list(VERIFICATION_CHANNELS))
    assert body_of(result(content_verification=both))["content_verification"] == both
    for bad in (
        dict(good, channels=["authenticated_full_get", "authenticated_full_get"]),
        dict(good, channels=["authenticated_full_get", "anonymous_public_get"]),
        dict(good, channels=["presigned_get"]),
        dict(good, channels="authenticated_full_get"),
        dict(good, sha256=SHA.upper()),
        dict(good, sha256=None),
        dict(good, size=None),
        dict(good, size=-1),
        dict(good, size=True),
        dict(good, verified_at="2026-07-27"),
        dict(good, verified_at=None),
        # A digest with no channel names a verification nobody performed.
        {"channels": [], "sha256": SHA, "size": 11, "verified_at": AT},
        {"channels": [], "sha256": None, "size": 11, "verified_at": None},
        dict(good, extra=1),
        {"channels": ["authenticated_full_get"], "sha256": SHA, "size": 11},
        [],
    ):
        with pytest.raises(RecordError):
            result(content_verification=bad)


def test_result_hash_covers_every_new_field():
    baseline = body_of(result())
    for field, value in (
        ("outcome", "adopted"),
        ("object_written", False),
        ("object_reference", body_of(reference())),
        ("content_verification", {"channels": ["authenticated_full_get"],
                                  "sha256": SHA, "size": 11, "verified_at": AT}),
        ("url", "https://cdn.example.com/x"),
        ("url_kind", "public"),
        ("url_scope", "current-key"),
        ("url_expires_at", AT),
    ):
        drifted = dict(baseline)
        assert drifted[field] != value, field
        drifted[field] = value
        assert result_hash(drifted) != baseline["result_hash"], field


def test_authorization_required_is_not_the_intersection_with_allowed_actions():
    # verify_public is a registered action that no state ever offers, so the
    # intersection AUTHORIZED_ACTIONS & allowed_actions(state) can never
    # contain it -- yet it reaches the network and carries a challenge of its
    # own. The premise comes from action_registry, not from delivery_records.
    assert "verify_public" in ACTIONS
    for state in RECOVERY_STATES:
        assert "verify_public" not in allowed_actions(state, capabilities_ok=True)
    assert "verify_public" in authorization_required(
        "terminal_unacknowledged", capabilities_ok=True,
        pending_remote_steps=("verify_public",),
    )


def test_authorization_required_keeps_the_registry_order():
    actions = authorization_required(
        "not_started", capabilities_ok=True, pending_remote_steps=("verify_public",),
    )
    assert list(actions) == [a for a in AUTHORIZED_ACTION_ORDER if a in set(actions)]
    assert set(actions) == {"publish", "verify_public"}
    # The registry order is publish, reconcile, verify_public, resume, abort --
    # not alphabetical, and not the order the caller happened to hand in.
    assert list(actions) == ["publish", "verify_public"]
    reversed_input = authorization_required(
        "multipart_resumable", capabilities_ok=True,
        pending_remote_steps=("verify_public", "reconcile"),
    )
    assert list(reversed_input) == ["reconcile", "verify_public", "resume", "abort"]


def test_authorization_required_refuses_an_unauthorized_pending_step():
    for step in ("inspect", "ack", "publish_public", ""):
        with pytest.raises(RecordError):
            authorization_required("not_started", capabilities_ok=True,
                                   pending_remote_steps=(step,))
    with pytest.raises(RecordError):
        result(pending_remote_steps=("inspect",))


def test_authorization_required_still_lists_the_caller_actions_of_the_state():
    assert "publish" in authorization_required("not_started", capabilities_ok=True)
    assert authorization_required("not_started", capabilities_ok=False) == ()
    # A pending internal step survives the capability filter, because the
    # filter answers "may the caller act", not "did this pipeline leave work".
    assert authorization_required(
        "not_started", capabilities_ok=False,
        pending_remote_steps=("verify_public",),
    ) == ("verify_public",)


def test_a_pending_step_reaches_the_result_body():
    body = body_of(result(pending_remote_steps=("verify_public",)))
    assert body["authorization_required"] == ["verify_public"]
    assert body_of(result())["authorization_required"] == []


def test_an_object_reference_must_agree_with_the_result_it_travels_in():
    item = reference()
    body = body_of(result(object_reference=item))
    assert body["object_reference"] == item
    assert body_of(item)["disposition"] == body["outcome"] == "created"
    assert body_of(item)["object_written"] is body["object_written"] is True
    # created reference in an adopted result: the disposition names what the
    # operation did to the bytes, and two artifacts travelling together may
    # not name two different things.
    with pytest.raises(RecordError):
        result(outcome="adopted", object_reference=item)
    with pytest.raises(RecordError):
        result(outcome="unknown", state="in_flight_unknown", object_reference=item)
    # The three pairs below are the ones the object_written comparison cannot
    # see, because both sides agree on the write certainty and disagree only on
    # the name. Without them the disposition check has no independent payload:
    # deleting it left the whole suite green, since every case tried until then
    # was also caught by the object_written comparison (T11, full scope).
    adopted_ref = reference(disposition="adopted", verifications=[{
        "channel": "authenticated_full_get", "size": 11, "sha256": SHA,
        "url_scope": "current-key", "verified_at": AT,
    }])
    reconciled_ref = reference(disposition="reconciled", verifications=[{
        "channel": "authenticated_full_get", "size": 11, "sha256": SHA,
        "url_scope": "current-key", "verified_at": AT,
    }])
    for outcome, state, ref in (
        ("blocked", "blocked", adopted_ref),
        ("not_applied", "known_not_applied", adopted_ref),
        ("rejected", "blocked", adopted_ref),
        ("unknown", "in_flight_unknown", reconciled_ref),
    ):
        assert OUTCOME_WRITE_CERTAINTY[outcome] is body_of(ref)["object_written"]
        with pytest.raises(RecordError):
            result(outcome=outcome, state=state, object_reference=ref)
    # Every disposition has a matching outcome and refuses the other two.
    adopted = reference(disposition="adopted", verifications=[{
        "channel": "authenticated_full_get", "size": 11, "sha256": SHA,
        "url_scope": "current-key", "verified_at": AT,
    }])
    assert body_of(result(outcome="adopted", state="blocked",
                          object_reference=adopted))["object_written"] is False
    with pytest.raises(RecordError):
        result(outcome="created", object_reference=adopted)


def test_an_object_reference_is_re_parsed_not_trusted():
    item = reference()
    hand_edited = dict(item)
    hand_edited["object_written"] = False
    with pytest.raises(RecordError):
        result(object_reference=hand_edited)
    for junk in ({}, {"artifact_type": "s3-upload.result"}, 7, "not an artifact"):
        with pytest.raises(RecordError):
            result(object_reference=junk)


def test_an_embedded_reference_binds_its_target_contract_to_its_hash():
    # The one v1 check that had no v2 counterpart. Editing target_contract
    # while leaving target_contract_hash alone used to be accepted by
    # parse_object_reference_v2 outright (run, not assumed), which would have
    # let a result carry a contract that no longer hashes to the digest every
    # other artifact in the chain is keyed by.
    item = reference()
    drifted = dict(item)
    drifted["target_contract"] = {"contract_version": 999}
    assert drifted["target_contract_hash"] == contract_hash(CONTRACT)
    raw = serialize_artifact(drifted).decode("utf-8")
    with pytest.raises(delivery_reference.ReferenceError):
        delivery_reference.parse_object_reference_v2(raw)
    with pytest.raises(RecordError):
        result(object_reference=drifted)
    # And the mirror image: the hash edited, the contract left alone.
    other = dict(item)
    other["target_contract_hash"] = contract_hash({"contract_version": 999})
    with pytest.raises(RecordError):
        result(object_reference=other)
    # A contract that cannot be hashed at all is refused here, not by the
    # canonicaliser in front of result_out.
    with pytest.raises(delivery_reference.ReferenceError):
        reference(target_contract={"cycle": {1, 2}},
                  target_contract_hash=contract_hash(CONTRACT))


def test_a_versioned_reference_may_not_be_embedded_unscreened():
    # v1's discipline: the write side passes credentials whenever a real
    # provider version_id is in play (operations.py:338, multipart.py:491) and
    # omits them only where version_id is None (operations.py:170). Here that
    # is structural rather than remembered.
    versioned = reference(location={"version_id": "v-1"})
    with pytest.raises(RecordError):
        result(object_reference=versioned)
    assert body_of(result(object_reference=versioned,
                          credentials=[SECRET]))["object_reference"] == versioned
    assert body_of(result(object_reference=versioned,
                          credentials=()))["object_reference"] == versioned
    # And the screen is real, not a formality: a version_id carrying the
    # caller's credential is refused once the credential is declared.
    leaked = reference(location={"version_id": "v-" + SECRET})
    assert body_of(result(object_reference=leaked,
                          credentials=[]))["object_reference"] == leaked
    with pytest.raises(RecordError):
        result(object_reference=leaked, credentials=[SECRET])
    # An unversioned reference needs no declaration, which is what makes the
    # refusal above a rule about provider strings and not about paperwork.
    assert body_of(result(object_reference=reference()))["object_reference"]
