import builtins
import errno
import hashlib
import http.client
import json
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path

import pdf_source
import pytest
import workflow


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PDF_SHA256 = "d7dd0115be8b79ae057b3f6ca0fcee578085ba6919dcb70e8643a2aff537d9b5"
NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
PUBLIC_ADDRESS = "93.184.216.34"


def make_endpoint(address, *, port=443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return pdf_source.ResolvedEndpoint(
        family=family,
        socktype=socket.SOCK_STREAM,
        protocol=socket.IPPROTO_TCP,
        sockaddr=sockaddr,
        canonical_ip=address,
    )


class ScriptedResponse:
    def __init__(self, *, body, status=200, headers=None, peer_ip=PUBLIC_ADDRESS):
        self.status = status
        self.headers = [] if headers is None else headers
        self.peer_ip = peer_ip
        self._body = bytearray(body)
        self.closed = False

    def read(self, size, *, timeout):
        assert timeout > 0
        chunk = bytes(self._body[:size])
        del self._body[:size]
        return chunk

    def close(self):
        self.closed = True


class FailingReadResponse(ScriptedResponse):
    def __init__(self, *, first_chunk=b"", failure=TimeoutError("read-secret")):
        super().__init__(
            body=first_chunk,
            headers=[("Content-Type", "application/pdf")],
        )
        self.failure = failure
        self.read_count = 0

    def read(self, size, *, timeout):
        self.read_count += 1
        if self.read_count == 1 and self._body:
            return super().read(size, timeout=timeout)
        raise self.failure


class ScriptedTransport:
    def __init__(self, *hops):
        self.hops = list(hops)
        self.resolve_calls = []
        self.connect_calls = []
        self.get_calls = []
        self.sessions = []

    def resolve(self, host, port):
        self.resolve_calls.append((host, port))
        hop = self.hops[len(self.connect_calls)]
        assert host == hop["host"]
        assert port == hop.get("port", 443)
        return hop.get("endpoints", [make_endpoint(PUBLIC_ADDRESS, port=port)])

    def connect_https(
        self,
        host,
        port,
        *,
        endpoint,
        server_hostname,
        connect_timeout,
        proxies,
    ):
        assert connect_timeout > 0
        assert proxies is False
        hop = self.hops[len(self.connect_calls)]
        assert host == hop["host"]
        assert port == hop.get("port", 443)
        assert server_hostname == host
        expected_endpoints = hop.get(
            "endpoints", [make_endpoint(PUBLIC_ADDRESS, port=port)]
        )
        assert endpoint == expected_endpoints[0]
        self.connect_calls.append((host, port, endpoint.canonical_ip))
        session = ScriptedSession(
            self,
            peer_ip=hop.get("peer_ip", PUBLIC_ADDRESS),
            response=hop["response"],
        )
        self.sessions.append(session)
        return session


class ScriptedSession:
    def __init__(self, transport, *, peer_ip, response):
        self.transport = transport
        self.peer_ip = peer_ip
        self.response = response
        self.closed = False

    def get(self, request_target, *, headers, read_timeout, redirects, retries):
        lowered_headers = {name.lower(): value for name, value in headers.items()}
        assert "authorization" not in lowered_headers
        assert "cookie" not in lowered_headers
        assert "proxy-authorization" not in lowered_headers
        assert "referer" not in lowered_headers
        assert "secret-aihub-key" not in "\n".join(lowered_headers.values())
        assert read_timeout > 0
        assert redirects is False
        assert retries == 0
        self.transport.get_calls.append(request_target)
        return self.response

    def close(self):
        self.closed = True


def invoke(capsys, argv, *, cwd, transport):
    rc = workflow.main(
        argv,
        environ={
            "AIHUB_API_KEY": "secret-aihub-key",
            "HTTP_PROXY": "http://proxy-user:proxy-secret@127.0.0.1:8080",
            "HTTPS_PROXY": "http://proxy-user:proxy-secret@127.0.0.1:8080",
            "COOKIE": "browser-cookie-secret",
        },
        cwd=str(cwd),
        config_home=str(Path(cwd) / "config-home"),
        transport=transport,
        now=NOW,
    )
    captured = capsys.readouterr()
    stdout_lines = captured.out.splitlines()
    assert len(stdout_lines) == 1
    return rc, json.loads(stdout_lines[0]), captured.out, captured.err


class NeverTransport:
    def resolve(self, *_args, **_kwargs):
        raise AssertionError("invalid URL input must be rejected before DNS")


def assert_no_committed_bundle(root):
    assert not root.exists() or not list(root.iterdir())


def start_public_bundle(capsys, tmp_path, *, source_url="https://docs.example/report.pdf"):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )
    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", source_url],
        cwd=tmp_path,
        transport=transport,
    )
    assert rc == 0
    return Path(result["work_bundle"]), result


def test_start_safely_freezes_a_public_https_pdf_into_the_shared_bundle_contract(
    tmp_path, capsys
):
    source_url = (
        "https://docs.example/report.pdf?token=supersecret#private-fragment"
    )
    redacted_url = "https://docs.example/report.pdf"
    response = ScriptedResponse(
        body=PDF_BYTES,
        headers=[
            ("Content-Type", "application/pdf; charset=binary"),
            ("Content-Length", str(len(PDF_BYTES))),
        ],
    )
    transport = ScriptedTransport(
        {"host": "docs.example", "response": response}
    )

    rc, started, stdout, stderr = invoke(
        capsys,
        ["start", "--source", source_url, "--output-dir", "bundles"],
        cwd=tmp_path,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert rc == 0
    assert started["outcome"] == "created"
    assert started["evidence_hash"] == f"sha256:{PDF_SHA256}"
    assert started["artifacts"] == {
        "manifest": "manifest.json",
        "source_pdf": "01-source/source.pdf",
    }
    assert bundle.name == f"20240102-030405-report-{PDF_SHA256[:8]}"
    assert (bundle / "01-source" / "source.pdf").read_bytes() == PDF_BYTES
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in bundle.rglob("*")
        if path.is_dir()
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in bundle.rglob("*")
        if path.is_file()
    )
    assert manifest["source"] == {
        "original_name": "report.pdf",
        "origin": {
            "kind": "https",
            "initial_url": redacted_url,
            "final_url": redacted_url,
            "input_url_sha256": (
                "2bae3a3cc88be93fbd348e922960d9f20d1660f899a30b17ff3bff9525bad1c5"
            ),
            "download": {
                "content_type": "application/pdf",
                "redirect_count": 0,
                "hops": [
                    {
                        "url": redacted_url,
                        "status_code": 200,
                        "resolved_addresses": [PUBLIC_ADDRESS],
                        "peer_ip": PUBLIC_ADDRESS,
                    }
                ],
            },
        },
        "physical_path": "01-source/source.pdf",
        "sha256": PDF_SHA256,
        "size_bytes": len(PDF_BYTES),
    }
    assert transport.resolve_calls == [("docs.example", 443)]
    assert transport.connect_calls == [
        ("docs.example", 443, PUBLIC_ADDRESS)
    ]
    assert transport.get_calls == ["/report.pdf?token=supersecret"]
    assert all(session.closed for session in transport.sessions)
    assert response.closed
    persisted_text = "\n".join(
        path.read_text(errors="replace")
        for path in bundle.rglob("*")
        if path.is_file()
    )
    for secret_part in ("token=supersecret", "private-fragment"):
        assert secret_part not in stdout
        assert secret_part not in stderr
        assert secret_part not in persisted_text

    fail_if_called = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("inspect and resume must use the frozen source")
    )
    inspect_rc, inspected, _stdout, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        transport=fail_if_called,
    )
    resume_rc, resumed, _stdout, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
        ],
        cwd=tmp_path,
        transport=fail_if_called,
    )

    assert inspect_rc == resume_rc == 0
    assert inspected == {**started, "outcome": "inspected"}
    assert resumed == {**started, "outcome": "no_progress"}
    assert hashlib.sha256(source_url.encode()).hexdigest() == manifest["source"][
        "origin"
    ]["input_url_sha256"]


def test_start_revalidates_each_https_redirect_and_redacts_every_persisted_url(
    tmp_path, capsys
):
    source_url = "https://docs.example/start?first=secret#initial"
    redirect_url = (
        "https://cdn.example/final.pdf?second=secret#redirect-fragment"
    )
    first_response = ScriptedResponse(
        status=302,
        body=b"ignored redirect body",
        headers=[("Location", redirect_url)],
    )
    final_response = ScriptedResponse(
        body=PDF_BYTES,
        headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", str(len(PDF_BYTES))),
        ],
        peer_ip="8.8.8.8",
    )
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "endpoints": [make_endpoint(PUBLIC_ADDRESS)],
            "response": first_response,
        },
        {
            "host": "cdn.example",
            "endpoints": [make_endpoint("8.8.8.8")],
            "peer_ip": "8.8.8.8",
            "response": final_response,
        },
    )

    rc, result, stdout, stderr = invoke(
        capsys,
        ["start", "--source", source_url, "--output-dir", "bundles"],
        cwd=tmp_path,
        transport=transport,
    )

    bundle = Path(result["work_bundle"])
    origin = json.loads((bundle / "manifest.json").read_text())["source"]["origin"]
    assert rc == 0
    assert origin == {
        "kind": "https",
        "initial_url": "https://docs.example/start",
        "final_url": "https://cdn.example/final.pdf",
        "input_url_sha256": (
            "d1085f53dbb20b8f680598d4af60a7f4d747817ed9f0adabefa1c9aaf96a93f3"
        ),
        "download": {
            "content_type": "application/pdf",
            "redirect_count": 1,
            "hops": [
                {
                    "url": "https://docs.example/start",
                    "status_code": 302,
                    "resolved_addresses": [PUBLIC_ADDRESS],
                    "peer_ip": PUBLIC_ADDRESS,
                },
                {
                    "url": "https://cdn.example/final.pdf",
                    "status_code": 200,
                    "resolved_addresses": ["8.8.8.8"],
                    "peer_ip": "8.8.8.8",
                },
            ],
        },
    }
    assert transport.resolve_calls == [
        ("docs.example", 443),
        ("cdn.example", 443),
    ]
    assert transport.get_calls == [
        "/start?first=secret",
        "/final.pdf?second=secret",
    ]
    assert first_response.closed and final_response.closed
    assert all(session.closed for session in transport.sessions)
    persisted = "\n".join(
        path.read_text(errors="replace")
        for path in bundle.rglob("*")
        if path.is_file()
    )
    for secret in (
        "first=secret",
        "second=secret",
        "#initial",
        "redirect-fragment",
    ):
        assert secret not in stdout
        assert secret not in stderr
        assert secret not in persisted


@pytest.mark.parametrize(
    ("source", "error_code"),
    [
        ("http://docs.example/report.pdf", "unsafe_source_scheme"),
        ("ftp://docs.example/report.pdf", "unsafe_source_scheme"),
        ("file:///tmp/report.pdf", "unsafe_source_scheme"),
        ("https:opaque", "invalid_source_url"),
        ("https:///report.pdf", "invalid_source_url"),
        (
            "https://user:password@docs.example/report.pdf",
            "source_authentication_not_supported",
        ),
        (
            "https://@docs.example/report.pdf",
            "source_authentication_not_supported",
        ),
        ("https://docs.example:99999/report.pdf", "invalid_source_url"),
        ("https://docs.example:/report.pdf", "invalid_source_url"),
        ("https://docs.example/report\x00.pdf", "invalid_source_url"),
        ("https://docs.example/report\n.pdf", "invalid_source_url"),
        ("https://d\u00f6cs.example/report.pdf", "invalid_source_url"),
    ],
)
def test_start_rejects_unsafe_or_authenticated_url_syntax_before_dns(
    tmp_path, capsys, source, error_code
):
    rc, result, stdout, stderr = invoke(
        capsys,
        ["start", "--source", source, "--output-dir", "bundles"],
        cwd=tmp_path,
        transport=NeverTransport(),
    )

    assert rc == 3
    assert result["work_bundle"] is None
    assert result["outcome"] == "error"
    assert result["action_required"] == "provide_public_https_pdf"
    assert result["errors"][0]["code"] == error_code
    assert source not in stdout
    assert source not in stderr
    output_root = tmp_path / "bundles"
    assert not output_root.exists() or not list(output_root.iterdir())


@pytest.mark.parametrize(
    ("address", "error_code"),
    [
        ("127.0.0.1", "unsafe_source_address"),
        ("10.0.0.1", "unsafe_source_address"),
        ("169.254.1.1", "unsafe_source_address"),
        ("192.0.2.1", "unsafe_source_address"),
        ("0.0.0.0", "unsafe_source_address"),
        ("224.0.0.1", "unsafe_source_address"),
        ("100.64.0.1", "unsafe_source_address"),
        ("::1", "unsafe_source_address"),
        ("fe80::1", "unsafe_source_address"),
        ("fc00::1", "unsafe_source_address"),
        ("2001:db8::1", "unsafe_source_address"),
        ("::ffff:8.8.8.8", "source_dns_invalid"),
    ],
)
def test_start_rejects_every_non_public_resolved_address_before_connecting(
    tmp_path, capsys, address, error_code
):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "endpoints": [make_endpoint(address)],
            "response": ScriptedResponse(body=PDF_BYTES),
        }
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/report.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == error_code
    assert transport.connect_calls == []
    assert transport.get_calls == []
    output_root = tmp_path / "pdf2markdown-output"
    assert not output_root.exists() or not list(output_root.iterdir())


def test_start_rejects_mixed_public_and_private_dns_before_connecting(
    tmp_path, capsys
):
    mixed = ScriptedTransport(
        {
            "host": "mixed.example",
            "endpoints": [
                make_endpoint(PUBLIC_ADDRESS),
                make_endpoint("10.0.0.1"),
            ],
            "response": ScriptedResponse(body=PDF_BYTES),
        }
    )

    mixed_rc, mixed_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://mixed.example/report.pdf"],
        cwd=tmp_path,
        transport=mixed,
    )

    assert mixed_rc == 3
    assert mixed_error["errors"][0]["code"] == "unsafe_source_address"
    assert mixed.connect_calls == []


def test_start_rejects_a_peer_that_does_not_match_the_pinned_endpoint(
    tmp_path, capsys
):
    rebound = ScriptedTransport(
        {
            "host": "rebound.example",
            "endpoints": [
                make_endpoint(PUBLIC_ADDRESS),
                make_endpoint("1.1.1.1"),
            ],
            "peer_ip": "1.1.1.1",
            "response": ScriptedResponse(body=PDF_BYTES),
        }
    )

    rebound_rc, rebound_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://rebound.example/report.pdf"],
        cwd=tmp_path,
        transport=rebound,
    )

    assert rebound_rc == 3
    assert rebound_error["errors"][0]["code"] == "source_peer_mismatch"
    assert rebound.connect_calls == [
        ("rebound.example", 443, PUBLIC_ADDRESS)
    ]
    assert rebound.get_calls == []
    assert rebound.sessions[0].closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_start_follows_supported_relative_https_redirects_with_a_fresh_peer_check(
    tmp_path, capsys, status
):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                status=status,
                body=b"not the source",
                headers=[("Location", "../final.pdf?redirect=secret#fragment")],
            ),
        },
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        },
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/a/start"],
        cwd=tmp_path,
        transport=transport,
    )

    bundle = Path(result["work_bundle"])
    origin = json.loads((bundle / "manifest.json").read_text())["source"]["origin"]
    assert rc == 0
    assert (bundle / "01-source" / "source.pdf").read_bytes() == PDF_BYTES
    assert origin["final_url"] == "https://docs.example/final.pdf"
    assert origin["download"]["redirect_count"] == 1
    assert [hop["status_code"] for hop in origin["download"]["hops"]] == [
        status,
        200,
    ]
    assert transport.resolve_calls == [
        ("docs.example", 443),
        ("docs.example", 443),
    ]
    assert transport.get_calls == ["/a/start", "/final.pdf?redirect=secret"]


@pytest.mark.parametrize(
    ("location_headers", "error_code"),
    [
        ([('Location', 'http://other.example/final.pdf')], "unsafe_source_scheme"),
        (
            [('Location', 'https://user@other.example/final.pdf')],
            "source_authentication_not_supported",
        ),
        ([], "invalid_source_redirect"),
        (
            [('Location', '/one.pdf'), ('Location', '/two.pdf')],
            "invalid_source_redirect",
        ),
    ],
)
def test_start_rejects_unsafe_or_ambiguous_redirect_locations_without_a_second_get(
    tmp_path, capsys, location_headers, error_code
):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                status=302,
                body=b"redirect body",
                headers=location_headers,
            ),
        }
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/start.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == error_code
    assert transport.get_calls == ["/start.pdf"]
    output_root = tmp_path / "pdf2markdown-output"
    assert not list(output_root.iterdir())


def test_start_rejects_a_private_redirect_target_before_its_connect(tmp_path, capsys):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                status=302,
                body=b"redirect body",
                headers=[("Location", "https://private.example/final.pdf")],
            ),
        },
        {
            "host": "private.example",
            "endpoints": [make_endpoint("10.0.0.1")],
            "response": ScriptedResponse(body=PDF_BYTES),
        },
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/start.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == "unsafe_source_address"
    assert transport.resolve_calls == [
        ("docs.example", 443),
        ("private.example", 443),
    ]
    assert transport.connect_calls == [
        ("docs.example", 443, PUBLIC_ADDRESS)
    ]
    assert transport.get_calls == ["/start.pdf"]


def test_start_rejects_a_redirect_loop_that_only_changes_the_fragment(
    tmp_path, capsys
):
    loop_transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                status=302,
                body=b"redirect",
                headers=[("Location", "/one.pdf#changed-fragment")],
            ),
        }
    )
    loop_rc, loop_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/one.pdf#initial"],
        cwd=tmp_path,
        transport=loop_transport,
    )

    assert loop_rc == 3
    assert loop_error["errors"][0]["code"] == "source_redirect_loop"
    assert len(loop_transport.get_calls) == 1


def test_start_rejects_a_redirect_chain_beyond_the_hard_limit(tmp_path, capsys):
    limit_transport = ScriptedTransport(
        *[
            {
                "host": "limit.example",
                "response": ScriptedResponse(
                    status=302,
                    body=b"redirect",
                    headers=[("Location", f"/{index + 1}.pdf")],
                ),
            }
            for index in range(6)
        ]
    )
    limit_rc, limit_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://limit.example/0.pdf"],
        cwd=tmp_path,
        transport=limit_transport,
    )

    assert limit_rc == 3
    assert limit_error["errors"][0]["code"] == "source_redirect_limit_exceeded"
    assert len(limit_transport.get_calls) == 6


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (401, "source_authentication_required"),
        (403, "source_authentication_required"),
        (404, "source_http_error"),
        (429, "source_http_error"),
        (500, "source_http_error"),
    ],
)
def test_start_reports_http_failures_without_reading_or_leaking_the_response(
    tmp_path, capsys, status, error_code
):
    secret = "response-secret-capability"
    response = ScriptedResponse(
        status=status,
        body=secret.encode(),
        headers=[("X-Diagnostic", secret)],
    )
    transport = ScriptedTransport(
        {"host": "docs.example", "response": response}
    )

    rc, result, stdout, stderr = invoke(
        capsys,
        [
            "start",
            "--source",
            f"https://docs.example/report.pdf?token={secret}",
        ],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == error_code
    assert response._body == bytearray(secret.encode())
    assert response.closed and transport.sessions[0].closed
    assert secret not in stdout
    assert secret not in stderr
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


@pytest.mark.parametrize(
    ("headers", "body", "error_code"),
    [
        ([('Content-Type', 'text/html')], PDF_BYTES, "invalid_pdf_content_type"),
        ([], PDF_BYTES, "invalid_pdf_content_type"),
        (
            [('Content-Type', 'application/octet-stream')],
            PDF_BYTES,
            "invalid_pdf_content_type",
        ),
        (
            [('Content-Type', 'application/pdf')],
            b"<!doctype html><p>not pdf</p>",
            "invalid_pdf",
        ),
        (
            [('Content-Type', 'application/pdf')],
            b"%PDF-not really a pdf",
            "invalid_pdf",
        ),
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Type', 'application/pdf'),
            ],
            PDF_BYTES,
            "invalid_pdf_content_type",
        ),
        (
            [('Content-Type', 'application/pdf; x=y, text/html')],
            PDF_BYTES,
            "invalid_pdf_content_type",
        ),
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Encoding', 'gzip'),
            ],
            PDF_BYTES,
            "unsupported_content_encoding",
        ),
    ],
)
def test_start_requires_content_type_signature_and_parser_identity_independently(
    tmp_path, capsys, headers, body, error_code
):
    response = ScriptedResponse(body=body, headers=headers)
    transport = ScriptedTransport(
        {"host": "docs.example", "response": response}
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/looks-valid.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == error_code
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_start_accepts_a_parseable_pdf_without_a_pdf_url_extension(tmp_path, capsys):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/download"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 0
    bundle = Path(result["work_bundle"])
    assert (bundle / "01-source" / "source.pdf").read_bytes() == PDF_BYTES
    assert json.loads((bundle / "manifest.json").read_text())["source"][
        "original_name"
    ] == "download"


@pytest.mark.parametrize(
    ("headers", "body", "error_code"),
    [
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Length', str(256 * 1024 * 1024 + 1)),
            ],
            b"",
            "source_size_limit_exceeded",
        ),
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Length', '1'),
                ('Content-Length', '1'),
            ],
            PDF_BYTES,
            "invalid_source_response",
        ),
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Length', 'not-a-number'),
            ],
            PDF_BYTES,
            "invalid_source_response",
        ),
        (
            [
                ('Content-Type', 'application/pdf'),
                ('Content-Length', str(len(PDF_BYTES) + 1)),
            ],
            PDF_BYTES,
            "invalid_source_response",
        ),
    ],
)
def test_start_enforces_declared_and_streamed_response_bounds(
    tmp_path, capsys, headers, body, error_code
):
    transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(body=body, headers=headers),
        }
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://docs.example/report.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"][0]["code"] == error_code
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_start_reports_dns_failure_without_leaking_transport_details(tmp_path, capsys):
    class DnsFailure:
        def resolve(self, *_args, **_kwargs):
            raise OSError("dns-secret")

    dns_rc, dns_error, dns_stdout, dns_stderr = invoke(
        capsys,
        ["start", "--source", "https://dns.example/report.pdf?dns-secret"],
        cwd=tmp_path,
        transport=DnsFailure(),
    )
    assert dns_rc == 3
    assert dns_error["errors"][0]["code"] == "source_dns_failed"
    assert "dns-secret" not in dns_stdout
    assert "dns-secret" not in dns_stderr


def test_start_enforces_the_connect_timeout_without_leaking_transport_details(
    tmp_path, capsys
):
    class ConnectTimeout:
        def resolve(self, _host, port):
            return [make_endpoint(PUBLIC_ADDRESS, port=port)]

        def connect_https(self, *_args, **_kwargs):
            raise TimeoutError("connect-secret")

    connect_rc, connect_error, connect_stdout, connect_stderr = invoke(
        capsys,
        ["start", "--source", "https://connect.example/report.pdf?connect-secret"],
        cwd=tmp_path,
        transport=ConnectTimeout(),
    )
    assert connect_rc == 3
    assert connect_error["errors"][0]["code"] == "source_connect_timeout"
    assert "connect-secret" not in connect_stdout
    assert "connect-secret" not in connect_stderr


def test_start_cleans_a_partial_source_after_an_idle_read_timeout(tmp_path, capsys):
    read_response = FailingReadResponse(first_chunk=b"%PDF-")
    read_transport = ScriptedTransport(
        {"host": "read.example", "response": read_response}
    )
    read_rc, read_error, read_stdout, read_stderr = invoke(
        capsys,
        ["start", "--source", "https://read.example/report.pdf?read-secret"],
        cwd=tmp_path,
        transport=read_transport,
    )
    assert read_rc == 3
    assert read_error["errors"][0]["code"] == "source_read_timeout"
    assert "read-secret" not in read_stdout
    assert "read-secret" not in read_stderr


def test_start_maps_an_incomplete_http_body_to_a_structured_read_failure(
    tmp_path, capsys
):
    response = FailingReadResponse(
        first_chunk=b"%PDF-",
        failure=http.client.IncompleteRead(b"partial-secret", 100),
    )
    transport = ScriptedTransport(
        {"host": "truncated.example", "response": response}
    )

    rc, result, stdout, stderr = invoke(
        capsys,
        ["start", "--source", "https://truncated.example/report.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 3
    assert result["errors"] == [
        {
            "code": "source_read_failed",
            "message": "The HTTPS source download failed.",
        }
    ]
    assert "partial-secret" not in stdout
    assert "partial-secret" not in stderr
    assert response.closed and transport.sessions[0].closed
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_start_enforces_a_total_body_read_deadline(tmp_path, capsys, monkeypatch):
    moments = iter([0.0, 31.0])
    monkeypatch.setattr(pdf_source.time, "monotonic", lambda: next(moments))
    total_transport = ScriptedTransport(
        {
            "host": "slow.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )
    total_rc, total_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://slow.example/report.pdf"],
        cwd=tmp_path,
        transport=total_transport,
    )
    assert total_rc == 3
    assert total_error["errors"][0]["code"] == "source_read_timeout"
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_start_cleans_a_partial_source_when_disk_capacity_is_exhausted(
    tmp_path, capsys, monkeypatch
):
    class NoFreeSpace:
        free = 0

    monkeypatch.setattr(pdf_source.shutil, "disk_usage", lambda _path: NoFreeSpace())
    capacity_transport = ScriptedTransport(
        {
            "host": "disk.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )
    capacity_rc, capacity_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://disk.example/report.pdf"],
        cwd=tmp_path,
        transport=capacity_transport,
    )
    assert capacity_rc == 3
    assert capacity_error["errors"][0]["code"] == "source_disk_limit_exceeded"
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_start_cleans_a_partial_source_after_a_disk_write_failure(
    tmp_path, capsys, monkeypatch
):
    original_write = pdf_source.os.write

    def fail_write(_descriptor, _data):
        raise OSError(errno.ENOSPC, "disk-secret")

    monkeypatch.setattr(pdf_source.os, "write", fail_write)
    write_transport = ScriptedTransport(
        {
            "host": "write.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )
    write_rc, write_error, write_stdout, write_stderr = invoke(
        capsys,
        ["start", "--source", "https://write.example/report.pdf?disk-secret"],
        cwd=tmp_path,
        transport=write_transport,
    )
    assert original_write is not fail_write
    assert write_rc == 3
    assert write_error["errors"][0]["code"] == "source_disk_write_failed"
    assert "disk-secret" not in write_stdout
    assert "disk-secret" not in write_stderr
    assert_no_committed_bundle(tmp_path / "pdf2markdown-output")


def test_https_bundle_uses_the_existing_settings_generation_history_and_resume_contract(
    tmp_path, capsys
):
    bundle, started = start_public_bundle(
        capsys,
        tmp_path,
        source_url="https://docs.example/report.pdf?source=secret",
    )

    rc, overridden, _stdout, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        transport=NeverTransport(),
    )
    inspect_rc, inspected, _stdout, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        transport=NeverTransport(),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert rc == inspect_rc == 0
    assert overridden["generation"] == inspected["generation"] == 2
    assert overridden["outcome"] == "settings_overridden"
    assert inspected["outcome"] == "inspected"
    assert overridden["evidence_hash"] == started["evidence_hash"]
    assert manifest["settings_snapshot"]["interaction_mode"] == "auto"
    assert manifest["source"]["origin"]["kind"] == "https"
    assert private_state["generation"] == 2
    assert [event["event"] for event in history] == [
        "bundle_started",
        "settings_override_intent",
        "settings_override_prepared",
        "settings_override_committed",
    ]
    assert "source=secret" not in json.dumps(manifest)
    assert "source=secret" not in json.dumps(history)


@pytest.mark.parametrize(
    "mutation",
    [
        "query_in_manifest",
        "private_resolved_address",
        "peer_not_resolved",
        "redirect_count_mismatch",
        "unknown_origin_field",
        "missing_input_hash",
    ],
)
def test_inspect_and_resume_reject_tampered_https_origin_evidence(
    tmp_path, capsys, mutation
):
    bundle, _started = start_public_bundle(capsys, tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    origin = manifest["source"]["origin"]
    if mutation == "query_in_manifest":
        origin["final_url"] += "?leaked=secret"
    elif mutation == "private_resolved_address":
        origin["download"]["hops"][0]["resolved_addresses"] = ["10.0.0.1"]
        origin["download"]["hops"][0]["peer_ip"] = "10.0.0.1"
    elif mutation == "peer_not_resolved":
        origin["download"]["hops"][0]["peer_ip"] = "1.1.1.1"
    elif mutation == "redirect_count_mismatch":
        origin["download"]["redirect_count"] = 1
    elif mutation == "unknown_origin_field":
        origin["raw_url"] = "https://docs.example/report.pdf?leaked=secret"
    else:
        del origin["input_url_sha256"]
    manifest_path.write_text(json.dumps(manifest))

    inspect_rc, inspected, inspect_stdout, inspect_stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        transport=NeverTransport(),
    )
    resume_rc, resumed, resume_stdout, resume_stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "1",
        ],
        cwd=tmp_path,
        transport=NeverTransport(),
    )

    assert inspect_rc == resume_rc == 4
    assert inspected["errors"][0]["code"] == "invalid_bundle"
    assert resumed["errors"][0]["code"] == "invalid_bundle"
    for output in (inspect_stdout, inspect_stderr, resume_stdout, resume_stderr):
        assert "leaked=secret" not in output


def test_start_validates_and_records_all_public_a_and_aaaa_results(tmp_path, capsys):
    ipv6 = "2606:4700:4700::1111"
    transport = ScriptedTransport(
        {
            "host": "dualstack.example",
            "endpoints": [
                make_endpoint(PUBLIC_ADDRESS),
                make_endpoint(ipv6),
            ],
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )

    rc, result, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", "https://dualstack.example/report.pdf"],
        cwd=tmp_path,
        transport=transport,
    )

    assert rc == 0
    bundle = Path(result["work_bundle"])
    hop = json.loads((bundle / "manifest.json").read_text())["source"]["origin"][
        "download"
    ]["hops"][0]
    assert hop["resolved_addresses"] == [PUBLIC_ADDRESS, ipv6]
    assert hop["peer_ip"] == PUBLIC_ADDRESS
    assert transport.connect_calls == [
        ("dualstack.example", 443, PUBLIC_ADDRESS)
    ]


def test_start_rejects_a_signature_only_local_file_that_the_parser_cannot_open(
    tmp_path, capsys
):
    malformed = tmp_path / "malformed.pdf"
    malformed.write_bytes(b"%PDF-not really a pdf")
    local_rc, local_error, _stdout, _stderr = invoke(
        capsys,
        ["start", "--source", str(malformed), "--output-dir", "local-bundles"],
        cwd=tmp_path,
        transport=NeverTransport(),
    )
    assert local_rc == 3
    assert local_error["errors"][0]["code"] == "invalid_pdf"
    assert_no_committed_bundle(tmp_path / "local-bundles")


def test_local_and_https_sources_share_the_missing_pdf_parser_dependency_gate(
    tmp_path, capsys, monkeypatch
):
    original_import = builtins.__import__

    def import_without_fitz(name, *args, **kwargs):
        if name == "fitz":
            raise ImportError("fitz unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_fitz)
    valid_local = tmp_path / "valid.pdf"
    valid_local.write_bytes(PDF_BYTES)
    missing_local_rc, missing_local, _stdout, _stderr = invoke(
        capsys,
        [
            "start",
            "--source",
            str(valid_local),
            "--output-dir",
            "missing-local",
        ],
        cwd=tmp_path,
        transport=NeverTransport(),
    )
    https_transport = ScriptedTransport(
        {
            "host": "docs.example",
            "response": ScriptedResponse(
                body=PDF_BYTES,
                headers=[("Content-Type", "application/pdf")],
            ),
        }
    )
    missing_https_rc, missing_https, _stdout, _stderr = invoke(
        capsys,
        [
            "start",
            "--source",
            "https://docs.example/report.pdf",
            "--output-dir",
            "missing-https",
        ],
        cwd=tmp_path,
        transport=https_transport,
    )

    assert missing_local_rc == missing_https_rc == 3
    assert missing_local["errors"][0]["code"] == "pdf_parser_unavailable"
    assert missing_https["errors"][0]["code"] == "pdf_parser_unavailable"
    assert_no_committed_bundle(tmp_path / "missing-local")
    assert_no_committed_bundle(tmp_path / "missing-https")
