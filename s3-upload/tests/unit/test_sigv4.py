from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from config import Connection
from s3 import build_signed_request, http_request, parse_provider_identifier


NOW = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)


def connection(**overrides):
    values = {
        "access_key_id": "AKIDEXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "bucket": "bucket",
        "endpoint": "https://s3.amazonaws.com",
        "region": "us-east-1",
        "addressing": "virtual",
    }
    values.update(overrides)
    return Connection(**values)


def test_generic_request_preserves_the_v1_put_signature():
    request = build_signed_request(
        connection(),
        method="PUT",
        key="test.txt",
        body=b"hello",
        headers=(("content-length", "5"), ("content-type", "text/plain")),
        now=NOW,
    )

    assert request.method == "PUT"
    assert request.url == "https://bucket.s3.amazonaws.com/test.txt"
    assert request.body == b"hello"
    assert request.headers["authorization"].endswith(
        "Signature=e225efe81d5d14ab1fa6592aef323342207dd6ca77569cc9de00a2a04b671098"
    )


def test_generic_request_sorts_encoded_query_without_losing_duplicates():
    request = build_signed_request(
        connection(session_token="TOKENEXAMPLE"),
        method="PUT",
        key="test.txt",
        query=(
            ("uploadId", "id +/%"),
            ("x", "b"),
            ("partNumber", "2"),
            ("x", "a"),
            ("empty", ""),
        ),
        body=b"part",
        headers=(("content-length", "4"),),
        now=NOW,
    )

    canonical_query = (
        "empty=&partNumber=2&uploadId=id%20%2B%2F%25&x=a&x=b"
    )
    assert request.url == (
        "https://bucket.s3.amazonaws.com/test.txt?" + canonical_query
    )
    assert request.canonical_request == "\n".join(
        [
            "PUT",
            "/test.txt",
            canonical_query,
            "content-length:4\n"
            "host:bucket.s3.amazonaws.com\n"
            "x-amz-content-sha256:37a680133bd09342f934afb8dd2c7d9e1b624da5f35e3a38adb103e37c055ed1\n"
            "x-amz-date:20130524T000000Z\n"
            "x-amz-security-token:TOKENEXAMPLE\n",
            "content-length;host;x-amz-content-sha256;x-amz-date;x-amz-security-token",
            "37a680133bd09342f934afb8dd2c7d9e1b624da5f35e3a38adb103e37c055ed1",
        ]
    )


def test_generic_request_accepts_an_explicit_payload_profile_and_normalizes_headers():
    request = build_signed_request(
        connection(),
        method="HEAD",
        key="a b/\u4e2d+%.txt",
        headers=(("X-Custom", "  alpha\t  beta  "),),
        payload_hash="UNSIGNED-PAYLOAD",
        now=NOW,
    )

    assert request.url == (
        "https://bucket.s3.amazonaws.com/a%20b/%E4%B8%AD%2B%25.txt"
    )
    assert request.headers["x-custom"] == "alpha beta"
    assert request.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert request.canonical_request.endswith("\nUNSIGNED-PAYLOAD")


def test_generic_request_rejects_case_insensitive_duplicate_headers():
    with pytest.raises(ValueError, match="duplicate header"):
        build_signed_request(
            connection(),
            method="GET",
            key="test.txt",
            headers=(("X-Custom", "one"), ("x-custom", "two")),
            now=NOW,
        )


@pytest.mark.parametrize(
    "name",
    [
        "authorization",
        "host",
        "x-amz-content-sha256",
        "x-amz-date",
        "x-amz-security-token",
    ],
)
def test_generic_request_rejects_caller_supplied_signer_headers(name):
    with pytest.raises(ValueError, match="reserved header"):
        build_signed_request(
            connection(),
            method="GET",
            key="test.txt",
            headers=((name, "caller-value"),),
            now=NOW,
        )


@pytest.mark.parametrize(
    "method,headers,error",
    [
        ("get", (), "invalid method"),
        ("GET\nDELETE", (), "invalid method"),
        ("GET", (("x-custom", "caf\u00e9"),), "invalid header value"),
    ],
)
def test_generic_request_rejects_ambiguous_method_or_header_bytes(
    method, headers, error
):
    with pytest.raises(ValueError, match=error):
        build_signed_request(
            connection(), method=method, key="test.txt", headers=headers, now=NOW
        )


def test_signed_transport_exposes_redirect_without_a_second_request():
    received = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            length = int(self.headers.get("content-length", "0"))
            received.append((self.path, dict(self.headers), self.rfile.read(length)))
            if self.path == "/start":
                self.send_response(307)
                self.send_header("Location", "/must-not-run")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = http_request(
            "GET",
            f"http://127.0.0.1:{server.server_port}/start",
            {
                "authorization": "signed-value",
                "x-amz-security-token": "session-value",
            },
            b"request-body",
            timeout=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 307
    assert [request[0] for request in received] == ["/start"]
    assert received[0][2] == b"request-body"


def test_signed_transport_does_not_follow_a_cross_host_redirect():
    source_requests = []
    target_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            target_requests.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            source_requests.append((self.path, dict(self.headers)))
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_port}/must-not-run",
            )
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    threads = [
        Thread(target=target.serve_forever, daemon=True),
        Thread(target=source.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        response = http_request(
            "GET",
            f"http://127.0.0.1:{source.server_port}/start",
            {
                "authorization": "signed-value",
                "x-amz-security-token": "session-value",
            },
            b"",
            timeout=2,
        )
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert response.status == 302
    assert [request[0] for request in source_requests] == ["/start"]
    assert target_requests == []


def test_provider_identifier_accepts_bounded_visible_ascii():
    value = "etag-!#$&'*+.^_`|~-4096"

    result = parse_provider_identifier(
        value,
        active_credentials=("ACCESS12345678", "SECRET12345678", "TOKEN12345678"),
    )

    assert result.classification == "accepted"
    assert result.value == value

    boundary = parse_provider_identifier(
        "x" * 4096,
        active_credentials=("ACCESS12345678", "SECRET12345678", "TOKEN12345678"),
    )
    assert boundary.classification == "accepted"
    assert boundary.value == "x" * 4096


@pytest.mark.parametrize(
    "value",
    [
        "",
        "line\nbreak",
        "x" * 4097,
        "prefix-SECRET12345678-suffix",
        "prefix-%53%45%43%52%45%54%31%32%33%34%35%36%37%38-suffix",
        "malformed-%2",
    ],
)
def test_provider_identifier_rejection_never_returns_the_untrusted_value(value):
    result = parse_provider_identifier(
        value,
        active_credentials=("ACCESS12345678", "SECRET12345678", "TOKEN12345678"),
    )

    assert result.classification == "identifier_rejected"
    assert result.value is None
    if value:
        assert value not in repr(result)


@pytest.mark.parametrize(
    "reflected",
    [
        "ACCESS12345678",
        "%41%43%43%45%53%53%31%32%33%34%35%36%37%38",
        "SECRET12345678",
        "%53%45%43%52%45%54%31%32%33%34%35%36%37%38",
        "TOKEN12345678",
        "%54%4F%4B%45%4E%31%32%33%34%35%36%37%38",
    ],
)
def test_identifier_guard_rejects_each_complete_active_credential(reflected):
    result = parse_provider_identifier(
        "prefix-" + reflected + "-suffix",
        active_credentials=("ACCESS12345678", "SECRET12345678", "TOKEN12345678"),
    )

    assert result.classification == "identifier_rejected"
    assert result.value is None


@pytest.mark.parametrize(
    "method,query,body,headers,expected_suffix",
    [
        ("PUT", (), b"data", (("if-none-match", "*"),), "/object.bin"),
        ("HEAD", (), b"", (), "/object.bin"),
        ("GET", (), b"", (), "/object.bin"),
        ("DELETE", (), b"", (), "/object.bin"),
        (
            "DELETE",
            (("versionId", "version/one"),),
            b"",
            (),
            "/object.bin?versionId=version%2Fone",
        ),
        ("POST", (("uploads", ""),), b"", (), "/object.bin?uploads="),
        (
            "PUT",
            (("uploadId", "upload/one"), ("partNumber", "1")),
            b"\x00\xffpart",
            (),
            "/object.bin?partNumber=1&uploadId=upload%2Fone",
        ),
        (
            "GET",
            (("uploadId", "upload/one"),),
            b"",
            (),
            "/object.bin?uploadId=upload%2Fone",
        ),
        (
            "POST",
            (("uploadId", "upload/one"),),
            b"<CompleteMultipartUpload/>",
            (("if-none-match", "*"),),
            "/object.bin?uploadId=upload%2Fone",
        ),
        (
            "DELETE",
            (("uploadId", "upload/one"),),
            b"",
            (),
            "/object.bin?uploadId=upload%2Fone",
        ),
    ],
)
def test_all_v2_object_operations_use_the_generic_request_seam(
    method, query, body, headers, expected_suffix
):
    request = build_signed_request(
        connection(),
        method=method,
        key="object.bin",
        query=query,
        body=body,
        headers=headers,
        now=NOW,
    )

    assert request.method == method
    assert request.url == "https://bucket.s3.amazonaws.com" + expected_suffix
    assert request.body == body
    assert request.canonical_request.split("\n", 1)[0] == method
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    if headers:
        assert "if-none-match" in request.headers["authorization"]


def test_generic_request_supports_path_addressing_and_streaming_payload_profile():
    request = build_signed_request(
        connection(
            endpoint="https://localhost:9000",
            bucket="my.bucket",
            addressing="path",
            session_token="TOKENEXAMPLE",
        ),
        method="PUT",
        key="a/\u4e2d.bin",
        body=b"stream-frame",
        headers=(("content-encoding", "aws-chunked"),),
        payload_hash="STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
        now=NOW,
    )

    assert request.url == "https://localhost:9000/my.bucket/a/%E4%B8%AD.bin"
    assert request.headers["x-amz-security-token"] == "TOKENEXAMPLE"
    assert request.headers["x-amz-content-sha256"] == (
        "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
    )


def test_list_parts_signature_matches_independent_aws_cli_vector():
    request = build_signed_request(
        connection(
            endpoint="http://127.0.0.1:9",
            bucket="bucket",
            addressing="path",
            session_token="TOKENEXAMPLE",
        ),
        method="GET",
        key="test.txt",
        query=(("uploadId", "id +/%"),),
        now=datetime(2026, 7, 22, 3, 41, 30, tzinfo=timezone.utc),
    )

    # Independently cross-checked against AWS CLI 2.27.54 ListParts signing.
    assert request.headers["authorization"].endswith(
        "Signature=ee699b02ce2845eb2264473fc16577a52dff622aa104b812d7ad4e929da98eb5"
    )
