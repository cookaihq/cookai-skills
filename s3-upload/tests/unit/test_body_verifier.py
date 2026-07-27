import ast
import hashlib
import inspect
import socket
from datetime import datetime, timezone
from http.client import IncompleteRead

import pytest

import s3
from body_verifier import CHUNK, Verification, VerificationError, verify_body
from delivery_schema import BLOCKING_REASONS


PAYLOAD = b"hello world"
SHA = hashlib.sha256(PAYLOAD).hexdigest()
NOW = datetime(2026, 7, 27, tzinfo=timezone.utc)


class Stream:
    def __init__(self, body=PAYLOAD, status=200, headers=None, chunk=None,
                 explode_after=None, explode=None):
        self.body, self.status = body, status
        self.headers = {"content-length": str(len(body))} if headers is None else headers
        self._at, self._chunk = 0, chunk or len(body)
        self._explode_after = explode_after
        self._explode = explode or OSError("connection reset")
        self.max_request = 0

    def read(self, size):
        self.max_request = max(self.max_request, size)
        if self._explode_after is not None and self._at >= self._explode_after:
            raise self._explode
        data = self.body[self._at:self._at + min(size, self._chunk)]
        self._at += len(data)
        return data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, stream=None, error=None):
        self.stream, self.error, self.calls = stream, error, 0

    def __call__(self, method, url, headers, timeout=30):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.stream


def run(opener, **overrides):
    kwargs = {
        "open_stream": opener, "url": "https://example.invalid/images/a.png",
        "headers": {}, "channel": "authenticated_full_get",
        "url_scope": "current-key", "expected_size": len(PAYLOAD),
        "expected_sha256": SHA, "now": NOW,
    }
    kwargs.update(overrides)
    return verify_body(**kwargs)


def test_a_matching_full_body_verifies():
    opener = Opener(Stream())
    item = run(opener)
    assert isinstance(item, Verification)
    assert (item.size, item.sha256, item.channel) == (11, SHA, "authenticated_full_get")
    assert item.verified_at == "2026-07-27T00:00:00Z"
    assert opener.calls == 1


def test_the_verifier_never_returns_the_body():
    assert inspect.signature(verify_body).return_annotation in (Verification, "Verification")
    fields = set(Verification.__dataclass_fields__)
    assert fields == {"channel", "sha256", "size", "url_scope", "verified_at"}


def test_the_verifier_reads_in_bounded_chunks():
    stream = Stream(body=b"x" * (CHUNK * 2), chunk=4096)
    digest = hashlib.sha256(b"x" * (CHUNK * 2)).hexdigest()
    run(Opener(stream), expected_size=CHUNK * 2, expected_sha256=digest)
    assert stream.max_request <= CHUNK


@pytest.mark.parametrize("status", [201, 204, 206, 301, 302, 400, 403, 404, 500])
def test_a_non_200_status_is_never_verification(status):
    with pytest.raises(VerificationError) as info:
        run(Opener(Stream(status=status)))
    assert info.value.reason == "verification_incomplete"


def test_a_content_range_response_is_refused():
    stream = Stream(headers={"content-length": "11", "content-range": "bytes 0-10/11"})
    with pytest.raises(VerificationError) as info:
        run(Opener(stream))
    assert info.value.reason == "verification_incomplete"


def test_a_redirect_raised_by_the_opener_is_refused():
    from urllib.error import HTTPError
    opener = Opener(error=HTTPError("https://example.invalid/", 302, "Found", {}, None))
    with pytest.raises(VerificationError) as info:
        run(opener)
    assert info.value.reason == "verification_incomplete"


def test_a_declared_length_that_disagrees_is_refused_as_a_mismatch():
    with pytest.raises(VerificationError) as info:
        run(Opener(Stream(headers={"content-length": "12"})))
    assert info.value.reason == "content_mismatch"


def test_an_oversized_body_is_cut_off_at_the_planned_size():
    stream = Stream(body=PAYLOAD + b"!" * 4096, headers={}, chunk=64)
    with pytest.raises(VerificationError) as info:
        run(Opener(stream))
    assert info.value.reason == "content_mismatch"
    # One byte past the plan is all it takes to know the body is too long, and
    # one byte past the plan is therefore all this reader is allowed to pull.
    # The bound is expected_size + 1 rather than expected_size + CHUNK on
    # purpose: measured, the looser bound has no load at all here, because the
    # very first read of an unbounded implementation is capped at CHUNK and
    # then refuses, so 11 + CHUNK can never be exceeded whatever the reader
    # does.
    assert stream._at <= len(PAYLOAD) + 1


def test_a_truncated_body_is_refused_as_incomplete():
    with pytest.raises(VerificationError) as info:
        run(Opener(Stream(body=PAYLOAD[:5], headers={})))
    assert info.value.reason == "verification_incomplete"


def test_a_body_of_the_right_length_but_different_content_is_a_mismatch():
    with pytest.raises(VerificationError) as info:
        run(Opener(Stream(body=b"HELLO WORLD")))
    assert info.value.reason == "content_mismatch"


def test_a_mid_stream_failure_is_refused_as_incomplete():
    with pytest.raises(VerificationError) as info:
        run(Opener(Stream(body=PAYLOAD, chunk=4, explode_after=4)))
    assert info.value.reason == "verification_incomplete"


def test_a_failure_after_the_last_byte_is_still_not_a_verification():
    # The load-bearing half of the read-failure arm, and the reason the arm
    # cannot be reduced to `pass`. When the stream dies at byte 4 the trailing
    # size check refuses the call anyway, so swallowing the OSError there
    # changes nothing observable. When it dies *after* delivering all eleven
    # planned bytes, the digest already matches and the size already matches:
    # swallowing the failure hands back a Verification for a connection that
    # never proved it had reached the end of the object. Measured, not
    # reasoned -- with the arm turned into `pass` the four-byte case still
    # raises and only this one starts returning.
    stream = Stream(body=PAYLOAD, chunk=4, explode_after=len(PAYLOAD))
    with pytest.raises(VerificationError) as info:
        run(Opener(stream))
    assert info.value.reason == "verification_incomplete"


def test_an_incomplete_read_from_the_http_layer_is_refused():
    # http.client.IncompleteRead is the canonical failure of a chunked
    # response that stops early, and it descends from HTTPException, not from
    # OSError and not from ValueError -- checked, not assumed
    # (IncompleteRead.__mro__ is IncompleteRead, HTTPException, Exception).
    # An except arm naming only the urllib and OSError families lets it out of
    # the module as a foreign exception type, which is the one thing a caller
    # deciding between content_mismatch and verification_incomplete cannot
    # handle.
    stream = Stream(body=PAYLOAD, chunk=4,
                    explode_after=len(PAYLOAD), explode=IncompleteRead(b"", 7))
    with pytest.raises(VerificationError) as info:
        run(Opener(stream))
    assert info.value.reason == "verification_incomplete"


def test_an_unregistered_channel_is_refused_before_any_request():
    opener = Opener(Stream())
    with pytest.raises(VerificationError):
        run(opener, channel="head_object")
    assert opener.calls == 0


@pytest.mark.parametrize("overrides", [
    {"url_scope": "whole-bucket"},
    {"expected_size": -1},
    {"expected_size": True},
    {"expected_size": "11"},
    {"expected_sha256": SHA.upper()},
    {"expected_sha256": SHA[:63]},
    {"expected_sha256": "sha256:" + SHA},
    {"now": "2026-07-27T00:00:00Z"},
])
def test_an_unusable_argument_is_refused_before_any_request(overrides):
    # Same shape as the channel check and here for the same reason: every one
    # of these is knowable without asking the network anything, so spending a
    # GET to discover it would be spending a request on a call that had
    # already failed. The observable is the opener's own counter, which the
    # verifier neither sets nor reports.
    opener = Opener(Stream())
    with pytest.raises(VerificationError):
        run(opener, **overrides)
    assert opener.calls == 0


def test_the_verifier_only_ever_names_reasons_the_delivery_vocabulary_registers():
    # The reason strings are read out of delivery_schema rather than retyped
    # here, so this cannot pass by agreeing with itself.
    named = set()
    for node in ast.walk(ast.parse(inspect.getsource(__import__("body_verifier")))):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
                named.add(keyword.value.value)
    assert named == {"content_mismatch", "verification_incomplete"}
    assert named <= set(BLOCKING_REASONS)


def test_the_default_opener_is_the_streaming_entry_point():
    # A default of s3.http_request would pass every test in this file, because
    # every test injects its own opener -- and would then read 8 KiB of a
    # multi-megabyte object in production and call it verified.
    assert inspect.signature(verify_body).parameters["open_stream"].default is (
        s3.open_body_stream)


def test_the_streaming_entry_point_refuses_redirects_and_reads_nothing_itself():
    # open_body_stream is the one function in this feature that no test can
    # exercise without a socket, so it is pinned structurally instead: it must
    # install the same redirect policy the rest of the transport uses, and it
    # must not read the response, because reading is what its 8 KiB-capped
    # sibling does and the whole point of this entry point is not to.
    tree = ast.parse(inspect.getsource(s3.open_body_stream))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "_NoRedirectHandler" in names
    assert "build_opener" in names
    assert "read" not in attributes


def test_http_request_keeps_its_cap_and_is_not_the_one_that_changed():
    # The sibling this entry point exists to avoid disturbing. Every read in
    # it, not the set of caps it uses: http_request reads twice -- once off
    # the response and once off the HTTPError -- and a set would let either
    # one lose its cap while the other kept the set looking right. Measured:
    # the first version of this assertion was a set, and uncapping
    # response.read survived the whole suite.
    tree = ast.parse(inspect.getsource(s3.http_request))
    caps = [node.args[0].value if node.args and isinstance(node.args[0], ast.Constant)
            else None
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read"]
    assert sorted(caps, key=repr) == [8192, 8192]


def test_the_verifier_makes_no_socket_call_of_its_own(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the verifier must go through the injected opener")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    run(Opener(Stream()))
