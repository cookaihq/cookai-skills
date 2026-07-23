"""Command-level contracts for adopting an immutable raw conversion."""

import hashlib
import io
import json
import os
import shutil
import socket
import stat
import struct
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import fitz
import pdf_source
import pytest
import raw_conversion
import result_archive
import workflow


NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
PUBLIC_ADDRESS = "93.184.216.34"


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        return Response(200, json.dumps(self.payload).encode("utf-8"))


class StatusResponse:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return Response(self.status, b"")


class NeverNetwork:
    def __call__(self, *_args, **_kwargs):
        raise AssertionError("network access is not expected")


class NeverArchiveNetwork:
    def resolve(self, *_args, **_kwargs):
        raise AssertionError("raw recovery must use the complete local ZIP")


class ArchiveResponse:
    def __init__(self, body, *, status=200, headers=None):
        self.status = status
        self.headers = (
            [
                ("Content-Type", "application/zip"),
                ("Content-Length", str(len(body))),
            ]
            if headers is None
            else headers
        )
        self._body = bytearray(body)
        self.closed = False

    def read(self, size, *, timeout):
        assert 0 < size <= 64 * 1024
        assert timeout > 0
        chunk = bytes(self._body[:size])
        del self._body[:size]
        return chunk

    def close(self):
        self.closed = True


class InterruptedArchiveResponse(ArchiveResponse):
    def __init__(self):
        super().__init__(b"PK")
        self.read_count = 0

    def read(self, size, *, timeout):
        self.read_count += 1
        if self.read_count == 1:
            return super().read(size, timeout=timeout)
        raise OSError("simulated result interruption")


class SwapPartOnHeadersResponse(ArchiveResponse):
    def __init__(self, body, *, swap):
        self.status = 200
        self._headers = [
            ("Content-Type", "application/zip"),
            ("Content-Length", str(len(body))),
        ]
        self._body = bytearray(body)
        self.closed = False
        self.swap = swap
        self.swapped = False

    @property
    def headers(self):
        if not self.swapped:
            self.swap()
            self.swapped = True
        return self._headers


class ArchiveSession:
    def __init__(self, transport, response, *, peer_ip=PUBLIC_ADDRESS):
        self.transport = transport
        self.response = response
        self.peer_ip = peer_ip
        self.closed = False

    def get(self, request_target, *, headers, read_timeout, redirects, retries):
        lowered = {key.lower(): value for key, value in headers.items()}
        assert "authorization" not in lowered
        assert "cookie" not in lowered
        assert "referer" not in lowered
        assert "proxy-authorization" not in lowered
        assert lowered["accept-encoding"] == "identity"
        assert read_timeout == 30.0
        assert redirects is False
        assert retries == 0
        self.transport.get_calls.append((request_target, dict(headers)))
        return self.response

    def close(self):
        self.closed = True


class ArchiveTransport:
    def __init__(
        self,
        body=None,
        *,
        response=None,
        endpoints=None,
        peer_ip=PUBLIC_ADDRESS,
    ):
        self.response = ArchiveResponse(body) if response is None else response
        self.endpoints = endpoints
        self.peer_ip = peer_ip
        self.resolve_calls = []
        self.connect_calls = []
        self.get_calls = []
        self.sessions = []

    def resolve(self, host, port):
        self.resolve_calls.append((host, port))
        return self.endpoints or [
            pdf_source.ResolvedEndpoint(
                family=socket.AF_INET,
                socktype=socket.SOCK_STREAM,
                protocol=socket.IPPROTO_TCP,
                sockaddr=(PUBLIC_ADDRESS, port),
                canonical_ip=PUBLIC_ADDRESS,
            )
        ]

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
        assert (host, port) == ("results.example", 443)
        assert server_hostname == host
        assert endpoint.canonical_ip == PUBLIC_ADDRESS
        assert connect_timeout == 10.0
        assert proxies is False
        self.connect_calls.append((host, port, endpoint.canonical_ip))
        session = ArchiveSession(self, self.response, peer_ip=self.peer_ip)
        self.sessions.append(session)
        return session


class RedirectArchiveTransport:
    def __init__(self, *hops):
        self.hops = list(hops)
        self.resolve_calls = []
        self.connect_calls = []
        self.get_calls = []
        self.sessions = []

    def resolve(self, host, port):
        hop = self.hops[len(self.connect_calls)]
        assert host == hop["host"]
        self.resolve_calls.append((host, port))
        return hop["endpoints"]

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
        hop = self.hops[len(self.connect_calls)]
        assert host == server_hostname == hop["host"]
        assert port == 443
        assert endpoint == hop["endpoints"][0]
        assert connect_timeout == 10.0
        assert proxies is False
        self.connect_calls.append((host, port, endpoint.canonical_ip))
        session = ArchiveSession(
            self,
            hop["response"],
            peer_ip=hop.get("peer_ip", endpoint.canonical_ip),
        )
        self.sessions.append(session)
        return session


def invoke(capsys, argv, *, cwd, environ=None, transport=None, now=NOW):
    rc = workflow.main(
        argv,
        environ={} if environ is None else environ,
        cwd=str(cwd),
        config_home=str(Path(cwd) / "config-home"),
        transport=transport,
        now=now,
    )
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    return rc, json.loads(lines[0]), captured.err


def install_preflight_dependencies(tmp_path, monkeypatch):
    python_packages = tmp_path / "python-packages"
    bs4 = python_packages / "bs4"
    bs4.mkdir(parents=True)
    (bs4 / "__init__.py").write_text(
        '__version__ = "4.13.0"\n'
        "class Paragraph:\n"
        "    def get_text(self):\n"
        "        return 'preflight'\n"
        "class BeautifulSoup:\n"
        "    def __init__(self, value, parser):\n"
        "        self.value = value\n"
        "        self.parser = parser\n"
        "    def find(self, name):\n"
        "        return Paragraph() if name == 'p' else None\n"
    )
    monkeypatch.syspath_prepend(str(python_packages))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pandoc = bin_dir / "pandoc"
    pandoc.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  printf 'pandoc 3.6.4\\n'\n"
        "else\n"
        "  printf '{\"pandoc-api-version\":[1,23],\"meta\":{},\"blocks\":[]}\\n'\n"
        "fi\n"
    )
    pandoc.chmod(0o700)
    return {"PATH": str(bin_dir)}


def ready_result_bundle(
    tmp_path, capsys, monkeypatch, *, page_count=1, interaction_mode="confirm"
):
    source = tmp_path / "input.pdf"
    document = fitz.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page(width=72, height=72)
        page.insert_text((8, 18), f"Raw conversion page {page_number}")
    document.save(source)
    document.close()
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    key = "test-aihub-key-123456"

    start_argv = ["start", "--source", str(source)]
    if interaction_mode != "confirm":
        start_argv.extend(["--interaction-mode", interaction_mode])
    rc, started, _stderr = invoke(
        capsys,
        start_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert rc == 0
    bundle = Path(started["work_bundle"])
    rc, baseline, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(started["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert rc == 0
    record = tmp_path / "preflight.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "pass",
                "pages": [
                    {
                        "page_number": page_number,
                        "classification": "content",
                        "risk_codes": [],
                        "evidence": [f"Page {page_number} is readable."],
                    }
                    for page_number in range(1, page_count + 1)
                ],
            }
        )
    )
    rc, recorded, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(baseline["generation"]),
            "--action-id",
            baseline["action_id"],
            "--evidence-hash",
            baseline["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert rc == 0
    upload = JsonResponse(
        {"url": "https://files.example/source.pdf?token=source-private"}
    )
    rc, staged, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(recorded["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=upload,
    )
    assert rc == 0
    create = JsonResponse({"id": "task-result-001"})
    rc, submitted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=create,
    )
    assert rc == 0
    result_url = "https://results.example/result.zip?token=result-private"
    poll = JsonResponse(
        {"status": "completed", "results": [{"url": result_url}]}
    )
    rc, ready, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(submitted["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=poll,
    )
    assert rc == 0
    assert ready["outcome"] == "result_ready"
    return bundle, ready, dependencies, key, result_url


def make_zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def make_custom_zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for info, content in entries:
            archive.writestr(info, content)
    return output.getvalue()


def unix_member(name, file_type):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (file_type | 0o600) << 16
    return info


def patch_zip_flags(data, flag):
    updated = bytearray(data)
    local = updated.index(b"PK\x03\x04")
    central = updated.index(b"PK\x01\x02")
    struct.pack_into("<H", updated, local + 6, flag)
    struct.pack_into("<H", updated, central + 8, flag)
    return bytes(updated)


def patch_zip_method(data, method):
    updated = bytearray(data)
    local = updated.index(b"PK\x03\x04")
    central = updated.index(b"PK\x01\x02")
    struct.pack_into("<H", updated, local + 8, method)
    struct.pack_into("<H", updated, central + 10, method)
    return bytes(updated)


def make_deflated_sizes(*sizes):
    output = io.BytesIO()
    zero_chunk = b"\x00" * (1024 * 1024)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, size in enumerate(sizes):
            with archive.open(f"large-{index}.bin", "w") as member:
                remaining = size
                while remaining:
                    chunk = zero_chunk[: min(len(zero_chunk), remaining)]
                    member.write(chunk)
                    remaining -= len(chunk)
    return output.getvalue()


def make_many_members(count):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr(f"entries/{index:05d}", b"")
    return output.getvalue()


def unsafe_archives(request_filename):
    markdown = f"{request_filename}.md"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = make_custom_zip(
            [
                (zipfile.ZipInfo(markdown), b"one"),
                (zipfile.ZipInfo(markdown), b"two"),
            ]
        )
    unicode_conflict = make_zip([("\u00e9.md", b"one"), ("e\u0301.md", b"two")])
    prefix_conflict = make_zip([("node", b"file"), ("node/child.md", b"child")])
    casefold_prefix_conflict = make_zip(
        [("Node", b"file"), ("node/child.md", b"child")]
    )
    unicode_prefix_conflict = make_zip(
        [("\u00e9", b"file"), ("e\u0301/child.md", b"child")]
    )
    descendant_first_conflict = make_zip(
        [("node/child.md", b"child"), ("node", b"file")]
    )
    implicit_casefold_conflict = make_zip(
        [("Node/one.md", b"one"), ("node/two.md", b"two")]
    )
    implicit_unicode_conflict = make_zip(
        [("\u00e9/one.md", b"one"), ("e\u0301/two.md", b"two")]
    )
    symlink = make_custom_zip(
        [(unix_member("link.md", stat.S_IFLNK), b"target.md")]
    )
    device = make_custom_zip(
        [(unix_member("device.md", stat.S_IFCHR), b"")]
    )
    ordinary = make_zip([(markdown, b"body")])
    nul_name = make_zip([("safeXname.md", b"body")]).replace(
        b"safeXname.md", b"safe\x00name.md"
    )
    corrupt_crc = make_zip([(markdown, b"unique-payload")]).replace(
        b"unique-payload", b"corrupt-payload", 1
    )
    return [
        (b"not-a-zip", "invalid_result_archive"),
        (make_zip([("/absolute.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("../outside.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("a/../../outside.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("./relative.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("a/./relative.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("dir\\outside.md", b"body")]), "unsafe_archive_path"),
        (make_zip([("C:/outside.md", b"body")]), "unsafe_archive_path"),
        (nul_name, "unsafe_archive_path"),
        (duplicate, "archive_path_conflict"),
        (unicode_conflict, "archive_path_conflict"),
        (prefix_conflict, "archive_path_conflict"),
        (casefold_prefix_conflict, "archive_path_conflict"),
        (unicode_prefix_conflict, "archive_path_conflict"),
        (descendant_first_conflict, "archive_path_conflict"),
        (implicit_casefold_conflict, "archive_path_conflict"),
        (implicit_unicode_conflict, "archive_path_conflict"),
        (symlink, "unsupported_archive_member_type"),
        (device, "unsupported_archive_member_type"),
        (
            make_custom_zip([(zipfile.ZipInfo("payload-directory/"), b"payload")]),
            "unsupported_archive_member_type",
        ),
        (patch_zip_flags(ordinary, 1), "encrypted_archive_unsupported"),
        (patch_zip_method(ordinary, 99), "unsupported_archive_compression"),
        (corrupt_crc, "invalid_result_archive"),
    ]


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


def test_resume_downloads_validates_and_atomically_adopts_one_raw_conversion(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    manifest_before = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest_before["conversion_attempts"][-1][
        "request_summary"
    ]["filename"]
    markdown = b"# Converted\n\nRaw body.\n"
    archive_bytes = make_zip(
        [(f"nested/{request_filename}.md", markdown), ("nested/image.png", b"PNG")]
    )
    transport = ArchiveTransport(archive_bytes)

    rc, converted, stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={
            **dependencies,
            "AIHUB_API_KEY": key,
            "HTTPS_PROXY": "http://proxy.invalid",
            "COOKIE": "browser-secret",
        },
        transport=transport,
    )

    final = bundle / "03-converted" / "attempts" / "conversion-attempt-0001"
    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    assert rc == 0, (converted, stderr)
    assert converted["outcome"] == "raw_conversion_adopted"
    assert converted["conversion_state"] == "converted"
    assert converted["generation"] == ready["generation"] + 1
    assert converted["artifacts"]["raw_markdown"] == (
        "03-converted/attempts/conversion-attempt-0001/raw/"
        f"nested/{request_filename}.md"
    )
    assert (final / "result.zip").read_bytes() == archive_bytes
    assert (final / "raw" / "nested" / f"{request_filename}.md").read_bytes() == markdown
    assert stat.S_IMODE((final / "result.zip").stat().st_mode) == 0o600
    assert stat.S_IMODE((final / "raw" / "nested").stat().st_mode) == 0o700
    assert manifest["conversion_state"] == "converted"
    assert private_state["generation"] == manifest["generation"]
    assert transport.resolve_calls == [("results.example", 443)]
    assert transport.get_calls[0][0] == "/result.zip?token=result-private"
    assert transport.response.closed
    assert all(session.closed for session in transport.sessions)
    public_text = json.dumps(converted) + stderr + json.dumps(manifest) + history_text
    assert result_url not in public_text
    assert key not in public_text
    assert not list((bundle / "03-converted" / "attempts").glob("*.part"))

    inspected_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert inspected_rc == 0
    assert inspected["conversion_state"] == "converted"
    assert inspected["artifacts"] == converted["artifacts"]


def test_interrupted_download_reuses_one_intent_and_the_same_private_result_url(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    interrupted = ArchiveTransport(response=InterruptedArchiveResponse())

    first_rc, interrupted_result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=interrupted,
    )

    history_after_first = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert first_rc == 4
    assert interrupted_result["errors"][0]["code"] == "result_download_failed"
    assert sum(event.get("event") == "raw_conversion_intent" for event in history_after_first) == 1
    assert sum(event.get("event") == "raw_conversion_prepared" for event in history_after_first) == 0
    assert json.loads((bundle / "manifest.json").read_text())[
        "conversion_state"
    ] == "result_downloading"

    archive_bytes = make_zip([(f"{request_filename}.md", b"# Recovered\n")])
    recovered_transport = ArchiveTransport(archive_bytes)
    second_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=recovered_transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert second_rc == 0, recovered
    assert recovered["outcome"] == "raw_conversion_adopted"
    assert sum(event.get("event") == "raw_conversion_intent" for event in history) == 1
    assert sum(event.get("event") == "raw_conversion_prepared" for event in history) == 1
    assert sum(event.get("event") == "raw_conversion_committed" for event in history) == 1
    assert interrupted.get_calls[0][0] == "/result.zip?token=result-private"
    assert recovered_transport.get_calls[0][0] == "/result.zip?token=result-private"
    assert result_url not in (bundle / ".state" / "history.ndjson").read_text()


def test_process_crash_during_download_recovers_the_same_operation_and_url(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive_bytes = make_zip([(f"{request_filename}.md", b"# Download crash\n")])

    class CrashAfterPartialDownload(ArchiveResponse):
        def __init__(self, body):
            super().__init__(body)
            self.read_count = 0

        def read(self, size, *, timeout):
            self.read_count += 1
            if self.read_count == 1:
                partial_size = max(1, min(size, len(self._body) // 2))
                return super().read(partial_size, timeout=timeout)
            raise SimulatedProcessCrash

    interrupted = ArchiveTransport(
        response=CrashAfterPartialDownload(archive_bytes)
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=interrupted,
            now=NOW,
        )
    capsys.readouterr()

    recovered_transport = ArchiveTransport(archive_bytes)
    rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=recovered_transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert rc == 0, recovered
    assert recovered["outcome"] == "raw_conversion_adopted"
    assert len(manifest["conversion_attempts"]) == 1
    assert manifest["conversion_attempts"][0]["task_id"] == "task-result-001"
    assert sum(
        event.get("event") == "raw_conversion_reservation" for event in history
    ) == 1
    assert sum(event.get("event") == "raw_conversion_intent" for event in history) == 1
    assert interrupted.get_calls[0][0] == recovered_transport.get_calls[0][0]


def test_result_download_rejects_mixed_public_and_private_dns_before_connect(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = ArchiveTransport(
        b"unused",
        endpoints=[make_endpoint(PUBLIC_ADDRESS), make_endpoint("10.0.0.1")],
    )

    rc, rejected, stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    history = (bundle / ".state" / "history.ndjson").read_text()
    assert rc == 4
    assert rejected["errors"][0]["code"] == "unsafe_result_address"
    assert transport.connect_calls == []
    assert "raw_conversion_intent" in history
    assert result_url not in history + stderr + json.dumps(rejected)


def test_result_download_rejects_a_peer_outside_the_verified_dns_set(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = ArchiveTransport(b"unused", peer_ip="8.8.8.8")

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "result_peer_mismatch"
    assert transport.get_calls == []


def test_result_download_rejects_declared_archive_size_before_reading_body(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    response = ArchiveResponse(
        b"",
        headers=[("Content-Length", str(256 * 1024 * 1024 + 1))],
    )
    transport = ArchiveTransport(response=response)

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    intent = next(event for event in history if event.get("event") == "raw_conversion_intent")
    assert rc == 4
    assert rejected["errors"][0]["code"] == "archive_size_limit_exceeded"
    assert response._body == bytearray()
    assert intent["limits"] == {
        "max_archive_bytes": 256 * 1024 * 1024,
        "max_total_compressed_bytes": 256 * 1024 * 1024,
        "max_total_uncompressed_bytes": 256 * 1024 * 1024,
        "max_member_bytes": 256 * 1024 * 1024,
        "max_staging_disk_bytes": 512 * 1024 * 1024,
        "max_members": zipfile.ZIP_FILECOUNT_LIMIT,
        "max_member_path_bytes": 1024,
        "max_path_component_bytes": 255,
        "max_path_depth": 128,
        "max_total_path_components": zipfile.ZIP_FILECOUNT_LIMIT,
        "supported_compression": [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    }


def test_result_download_rejects_non_identity_content_encoding(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    response = ArchiveResponse(
        b"compressed-on-wire",
        headers=[("Content-Encoding", "gzip")],
    )

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=response),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "unsupported_result_encoding"


def test_result_download_revalidates_redirect_dns_before_the_next_connect(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    redirect_secret = "https://private.example/archive.zip?redirect=secret"
    transport = RedirectArchiveTransport(
        {
            "host": "results.example",
            "endpoints": [make_endpoint(PUBLIC_ADDRESS)],
            "response": ArchiveResponse(
                b"ignored",
                status=302,
                headers=[("Location", redirect_secret)],
            ),
        },
        {
            "host": "private.example",
            "endpoints": [make_endpoint("10.0.0.1")],
            "response": ArchiveResponse(b"must not be read"),
        },
    )

    rc, rejected, stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    persisted_public = (
        (bundle / "manifest.json").read_text()
        + (bundle / ".state" / "history.ndjson").read_text()
    )
    assert rc == 4
    assert rejected["errors"][0]["code"] == "unsafe_result_address"
    assert transport.resolve_calls == [
        ("results.example", 443),
        ("private.example", 443),
    ]
    assert len(transport.connect_calls) == 1
    assert result_url not in persisted_public + stderr
    assert "redirect=secret" not in persisted_public + stderr


def test_part_directory_swap_cannot_redirect_result_writes_outside_the_bundle(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    attempts = bundle / "03-converted" / "attempts"
    external = tmp_path / "external-target"
    external.mkdir(mode=0o700)
    external.chmod(0o700)

    def swap_part_directory():
        part = next(path for path in attempts.iterdir() if path.name.endswith(".part"))
        detached = attempts / f"{part.name}.detached"
        part.rename(detached)
        part.symlink_to(external, target_is_directory=True)

    response = SwapPartOnHeadersResponse(
        make_zip([(f"{request_filename}.md", b"# Contained\n")]),
        swap=swap_part_directory,
    )
    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=response),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"
    assert list(external.iterdir()) == []
    assert not (
        attempts / "conversion-attempt-0001"
    ).exists()


def test_preseeded_part_directory_is_rejected_before_intent_or_network(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    token = "a" * 32
    monkeypatch.setattr(raw_conversion.secrets, "token_hex", lambda _size: token)
    part = bundle / "03-converted" / "attempts" / (
        f".conversion-attempt-0001.raw-{token}.part"
    )
    part.mkdir(mode=0o700)
    part.chmod(0o700)
    transport = ArchiveTransport(b"must not be requested")

    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert rc == 4
    assert result["errors"][0]["code"] == "staging_path_conflict"
    assert not any(
        event.get("event") == "raw_conversion_reservation" for event in history
    )
    assert not any(event.get("event") == "raw_conversion_intent" for event in history)
    assert transport.resolve_calls == []


def test_preseeded_final_attempt_is_rejected_before_reservation_or_network(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    final = (
        bundle
        / "03-converted"
        / "attempts"
        / "conversion-attempt-0001"
    )
    final.mkdir(mode=0o700)
    final.chmod(0o700)
    transport = ArchiveTransport(b"must not be requested")

    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert rc == 4
    assert result["errors"][0]["code"] == "staging_path_conflict"
    assert not any(
        event.get("event") == "raw_conversion_reservation" for event in history
    )
    assert transport.resolve_calls == []


@pytest.mark.parametrize(
    "boundary",
    [
        "before_reservation",
        "after_reservation",
        "after_marker",
        "after_mkdir",
        "before_marker_cleanup",
        "after_marker_cleanup",
    ],
)
def test_every_raw_reservation_crash_boundary_recovers_the_owned_directory(
    tmp_path, capsys, monkeypatch, boundary
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    tokens = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(raw_conversion.secrets, "token_hex", lambda _size: next(tokens))
    original_append = raw_conversion.bundle.append_history
    original_marker = raw_conversion._create_owner_marker
    original_mkdir = raw_conversion._create_staging_directory
    original_cleanup = raw_conversion._clear_owner_marker

    def crash_at_reservation(value, *, state_fd):
        if value.get("event") == "raw_conversion_reservation":
            if boundary == "after_reservation":
                original_append(value, state_fd=state_fd)
            raise SimulatedProcessCrash
        return original_append(value, state_fd=state_fd)

    def crash_after_marker(*args, **kwargs):
        result = original_marker(*args, **kwargs)
        raise SimulatedProcessCrash

    def crash_after_mkdir(*args, **kwargs):
        result = original_mkdir(*args, **kwargs)
        raise SimulatedProcessCrash

    def crash_at_cleanup(*args, **kwargs):
        if boundary == "after_marker_cleanup":
            original_cleanup(*args, **kwargs)
        raise SimulatedProcessCrash

    if boundary in {"before_reservation", "after_reservation"}:
        monkeypatch.setattr(raw_conversion.bundle, "append_history", crash_at_reservation)
    elif boundary == "after_marker":
        monkeypatch.setattr(raw_conversion, "_create_owner_marker", crash_after_marker)
    elif boundary == "after_mkdir":
        monkeypatch.setattr(raw_conversion, "_create_staging_directory", crash_after_mkdir)
    else:
        monkeypatch.setattr(raw_conversion, "_clear_owner_marker", crash_at_cleanup)

    initial_transport = ArchiveTransport(b"must not be requested")
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=initial_transport,
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(raw_conversion.bundle, "append_history", original_append)
    monkeypatch.setattr(raw_conversion, "_create_owner_marker", original_marker)
    monkeypatch.setattr(raw_conversion, "_create_staging_directory", original_mkdir)
    monkeypatch.setattr(raw_conversion, "_clear_owner_marker", original_cleanup)

    transport = ArchiveTransport(
        make_zip([(f"{request_filename}.md", b"# Reservation recovery\n")])
    )
    rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    reservation = next(
        event for event in history if event.get("event") == "raw_conversion_reservation"
    )
    attempts = bundle / "03-converted" / "attempts"
    assert rc == 0, (boundary, recovered)
    assert recovered["outcome"] == "raw_conversion_adopted"
    assert initial_transport.resolve_calls == []
    assert sum(
        event.get("event") == "raw_conversion_reservation" for event in history
    ) == 1
    assert sum(event.get("event") == "raw_conversion_intent" for event in history) == 1
    assert not (attempts / reservation["owner_marker_name"]).exists()
    assert len(json.loads((bundle / "manifest.json").read_text())["conversion_attempts"]) == 1


@pytest.mark.parametrize("tamper", ["owner_marker", "staging_payload"])
def test_reservation_only_recovery_rejects_foreign_ownership_evidence(
    tmp_path, capsys, monkeypatch, tamper
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    original_mkdir = raw_conversion._create_staging_directory

    def crash_after_mkdir(*args, **kwargs):
        result = original_mkdir(*args, **kwargs)
        raise SimulatedProcessCrash

    monkeypatch.setattr(raw_conversion, "_create_staging_directory", crash_after_mkdir)
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=ArchiveTransport(b"must not be requested"),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(raw_conversion, "_create_staging_directory", original_mkdir)

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    reservation = next(
        event for event in history if event.get("event") == "raw_conversion_reservation"
    )
    attempts = bundle / "03-converted" / "attempts"
    if tamper == "owner_marker":
        marker = attempts / reservation["owner_marker_name"]
        marker.write_text("foreign\n")
        marker.chmod(0o600)
    else:
        foreign = attempts / reservation["staging_name"] / "foreign.bin"
        foreign.write_bytes(b"foreign")
        foreign.chmod(0o600)

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )
    assert rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"


def test_pending_raw_intent_rejects_a_replaced_part_directory(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    interrupted_rc, interrupted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=InterruptedArchiveResponse()),
    )
    assert interrupted_rc == 4
    assert interrupted["errors"][0]["code"] == "result_download_failed"

    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    intent = next(event for event in history if event.get("event") == "raw_conversion_intent")
    part = bundle / "03-converted" / "attempts" / intent["staging_name"]
    shutil.rmtree(part)
    part.mkdir(mode=0o700)
    part.chmod(0o700)
    archive = part / "result.zip"
    archive.write_bytes(make_zip([(f"{request_filename}.md", b"# Foreign\n")]))
    archive.chmod(0o600)

    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )

    assert rc == 4
    assert result["errors"][0]["code"] == "integrity_violation"
    assert not (part.parent / "conversion-attempt-0001").exists()


def test_result_archive_rejects_unsafe_paths_types_formats_and_conflicts(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]

    for index, (archive_bytes, error_code) in enumerate(
        unsafe_archives(request_filename)
    ):
        if index:
            # Each deterministic rejection gets an independent work bundle.
            case_root = tmp_path / f"case-{index}"
            case_root.mkdir()
            bundle, ready, dependencies, key, _result_url = ready_result_bundle(
                case_root, capsys, monkeypatch
            )
        transport = ArchiveTransport(archive_bytes)
        rc, rejected, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            cwd=bundle.parent.parent,
            environ={**dependencies, "AIHUB_API_KEY": key},
            transport=transport,
        )
        final = (
            bundle
            / "03-converted"
            / "attempts"
            / "conversion-attempt-0001"
        )
        manifest = json.loads((bundle / "manifest.json").read_text())
        history = [
            json.loads(line)
            for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
        ]
        assert rc == 0, (error_code, rejected)
        assert rejected["outcome"] == error_code
        assert rejected["conversion_state"] == "terminal_error"
        assert rejected["raw_conversion_state"] == "rejected"
        assert manifest["raw_conversion"]["reason_code"] == error_code
        assert history[-1]["event"] == "raw_conversion_rejected"
        assert not final.exists()

    inspected_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=bundle.parent.parent,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(inspected["generation"]),
        ],
        cwd=bundle.parent.parent,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    assert inspected_rc == resumed_rc == 0
    assert inspected["raw_conversion_state"] == "rejected"
    assert resumed["outcome"] == manifest["raw_conversion"]["reason_code"]
    assert resumed["generation"] == inspected["generation"]


def test_inspect_detects_added_or_removed_empty_directories_in_the_raw_tree(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive_bytes = make_custom_zip(
        [
            (zipfile.ZipInfo("assets/"), b""),
            (zipfile.ZipInfo(f"nested/{request_filename}.md"), b"# Tree\n"),
        ]
    )
    rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(archive_bytes),
    )
    raw = (
        bundle
        / "03-converted"
        / "attempts"
        / "conversion-attempt-0001"
        / "raw"
    )
    assert rc == 0, converted
    assert (raw / "assets").is_dir()

    added = raw / "added-empty"
    added.mkdir(mode=0o700)
    added.chmod(0o700)
    added_rc, added_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert added_rc == 4
    assert added_result["errors"][0]["code"] == "integrity_violation"

    added.rmdir()
    (raw / "assets").rmdir()
    removed_rc, removed_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert removed_rc == 4
    assert removed_result["errors"][0]["code"] == "integrity_violation"


def test_main_markdown_selection_is_exact_recursive_and_never_order_based(
    tmp_path, capsys, monkeypatch
):
    cases = [
        (
            "exact-preferred",
            lambda name: [("other.md", b"other"), (f"deep/{name}.md", b"exact")],
            "converted",
            lambda name: f"deep/{name}.md",
        ),
        (
            "single-fallback",
            lambda _name: [("deep/only.md", b"only")],
            "converted",
            lambda _name: "deep/only.md",
        ),
        (
            "multiple-exact",
            lambda name: [(f"a/{name}.md", b"a"), (f"b/{name}.md", b"b")],
            "terminal_error",
            lambda _name: None,
        ),
        (
            "multiple-fallback",
            lambda _name: [("a.md", b"a"), ("b.md", b"b")],
            "terminal_error",
            lambda _name: None,
        ),
        (
            "case-sensitive-zero",
            lambda name: [(f"{name}.MD", b"upper")],
            "terminal_error",
            lambda _name: None,
        ),
    ]
    for index, (label, entries, expected_state, selected) in enumerate(cases):
        case_root = tmp_path / label
        case_root.mkdir()
        bundle, ready, dependencies, key, _result_url = ready_result_bundle(
            case_root, capsys, monkeypatch
        )
        request_filename = json.loads((bundle / "manifest.json").read_text())[
            "conversion_attempts"
        ][-1]["request_summary"]["filename"]
        rc, result, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            cwd=case_root,
            environ={**dependencies, "AIHUB_API_KEY": key},
            transport=ArchiveTransport(make_zip(entries(request_filename))),
        )
        manifest = json.loads((bundle / "manifest.json").read_text())
        record = manifest["raw_conversion"]
        final = (
            bundle
            / "03-converted"
            / "attempts"
            / "conversion-attempt-0001"
        )
        selected_member = selected(request_filename)
        assert rc == 0, (index, result)
        assert result["conversion_state"] == expected_state
        assert final.is_dir()
        if selected_member is None:
            assert result["outcome"] == "unexpected_result_layout"
            assert record["reason_code"] == "unexpected_result_layout"
            assert record["main_markdown_path"] is None
            assert "raw_markdown" not in manifest["artifacts"]
        else:
            assert result["outcome"] == "raw_conversion_adopted"
            assert record["main_markdown_path"].endswith(f"/raw/{selected_member}")


def test_confirmed_layout_retry_preserves_two_independent_raw_attempts(
    tmp_path, capsys, monkeypatch
):
    bundle, first_ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    first_rc, layout_error, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(first_ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(make_zip([("a.md", b"a"), ("b.md", b"b")])),
    )
    assert first_rc == 0
    assert layout_error["outcome"] == "unexpected_result_layout"
    assert layout_error["action_required"] == "resolve_unexpected_result_layout"

    decision_rc, authorized, _stderr = invoke(
        capsys,
        [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(layout_error["generation"]),
            "--action-id",
            layout_error["action_id"],
            "--evidence-hash",
            layout_error["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "The ambiguous result layout requires a new conversion charge.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert decision_rc == 0
    assert authorized["outcome"] == "conversion_retry_authorized"

    create = JsonResponse({"id": "task-result-002"})
    create_rc, submitted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(authorized["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=create,
    )
    assert create_rc == 0, submitted
    assert submitted["conversion_attempt_state"] == "submitted"

    second_result_url = "https://results.example/result.zip?token=result-private-2"
    poll = JsonResponse(
        {"status": "completed", "results": [{"url": second_result_url}]}
    )
    poll_rc, second_ready, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(submitted["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=poll,
    )
    assert poll_rc == 0
    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    adopt_rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(second_ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(
            make_zip([(f"{request_filename}.md", b"# Second attempt\n")])
        ),
    )

    attempts = bundle / "03-converted" / "attempts"
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert adopt_rc == 0, converted
    assert converted["outcome"] == "raw_conversion_adopted"
    assert (attempts / "conversion-attempt-0001" / "raw" / "a.md").is_file()
    assert (
        attempts
        / "conversion-attempt-0002"
        / "raw"
        / f"{request_filename}.md"
    ).is_file()
    assert sum(event.get("event") == "raw_conversion_intent" for event in history) == 2
    inspect_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert inspect_rc == 0, inspected
    assert inspected["conversion_state"] == "converted"
    resume_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    assert resume_rc == 0, resumed
    assert resumed["outcome"] == "converted"
    old_archive = attempts / "conversion-attempt-0001" / "result.zip"
    old_archive_bytes = old_archive.read_bytes()
    old_archive.write_bytes(old_archive_bytes + b"tampered old archive")
    old_archive.chmod(0o600)
    archive_rc, archive_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert archive_rc == 4
    assert archive_result["errors"][0]["code"] == "integrity_violation"
    old_archive.write_bytes(old_archive_bytes)
    old_archive.chmod(0o600)
    old_markdown = attempts / "conversion-attempt-0001" / "raw" / "a.md"
    old_markdown.write_bytes(b"tampered old attempt")
    old_markdown.chmod(0o600)
    tampered_rc, tampered, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert tampered_rc == 4
    assert tampered["errors"][0]["code"] == "integrity_violation"


def test_layout_retry_action_rebinds_across_settings_override(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    _layout_rc, layout_error, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(make_zip([("a.md", b"a"), ("b.md", b"b")])),
    )
    override_rc, overridden, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(layout_error["generation"]),
            "--publish-mode",
            "upload",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    assert override_rc == 0, overridden
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["action_required"] == "resolve_unexpected_result_layout"
    assert overridden["action_id"] == layout_error["action_id"]
    decision_rc, authorized, _stderr = invoke(
        capsys,
        [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(overridden["generation"]),
            "--action-id",
            overridden["action_id"],
            "--evidence-hash",
            overridden["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "The raw layout still requires an explicitly paid retry.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert decision_rc == 0, authorized
    assert authorized["outcome"] == "conversion_retry_authorized"


def test_layout_retry_action_is_removed_when_override_switches_to_auto(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    _layout_rc, layout_error, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(make_zip([("a.md", b"a"), ("b.md", b"b")])),
    )
    override_rc, overridden, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(layout_error["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert override_rc == 0, overridden
    assert overridden["action_required"] is None
    assert overridden["action_id"] is None
    assert manifest["raw_conversion"]["pending_action"] is None


@pytest.mark.parametrize("expired_status", [401, 403, 404])
def test_expired_result_url_refreshes_the_same_task_then_adopts(
    tmp_path, capsys, monkeypatch, expired_status
):
    bundle, ready, dependencies, key, old_result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    expired_transport = ArchiveTransport(
        response=ArchiveResponse(b"", status=expired_status)
    )
    expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=expired_transport,
    )
    assert expired_rc == 0, expired
    assert expired["outcome"] == "result_url_unavailable"
    assert expired["conversion_state"] == "recoverable_error"

    new_result_url = "https://results.example/result.zip?token=result-private-new"
    refresh = JsonResponse(
        {"status": "completed", "results": [{"url": new_result_url}]}
    )
    refresh_rc, refreshed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(expired["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=refresh,
    )
    assert refresh_rc == 0, refreshed
    assert refreshed["outcome"] == "result_ready"
    assert len(refresh.calls) == 1
    assert refresh.calls[0][0] == "GET"
    assert "task-result-001" in refresh.calls[0][1]

    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    adopt_rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(refreshed["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(
            make_zip([(f"{request_filename}.md", b"# Refreshed\n")])
        ),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    public_state = (
        (bundle / "manifest.json").read_text()
        + (bundle / ".state" / "history.ndjson").read_text()
    )
    assert adopt_rc == 0, converted
    assert converted["outcome"] == "raw_conversion_adopted"
    assert len(manifest["conversion_attempts"]) == 1
    assert manifest["conversion_attempts"][0]["task_id"] == "task-result-001"
    assert len(manifest["raw_conversions"]) == 2
    assert len(private_state["result_urls"]) == 2
    assert old_result_url not in public_state
    assert new_result_url not in public_state


def test_second_raw_operation_recovers_after_prepared_without_network(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _old_result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    _expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=ArchiveResponse(b"", status=403)),
    )
    refreshed_url = "https://results.example/result.zip?token=second-operation"
    _refresh_rc, refreshed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(expired["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=JsonResponse(
            {"status": "completed", "results": [{"url": refreshed_url}]}
        ),
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    archive_bytes = make_zip([(f"{request_filename}.md", b"# Recovered\n")])
    original_append_prepared = raw_conversion._append_prepared

    def crash_before_second_prepared(**_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        raw_conversion, "_append_prepared", crash_before_second_prepared
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(refreshed["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=ArchiveTransport(archive_bytes),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        raw_conversion, "_append_prepared", original_append_prepared
    )

    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(refreshed["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )
    assert recovered_rc == 0, recovered
    assert recovered["outcome"] == "raw_conversion_adopted"


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("credential_missing", "credential_source_missing"),
        ("credential_changed", "credential_source_changed"),
        ("http_401", "poll_unauthorized"),
        ("http_403", "poll_transient"),
        ("http_404", "task_unavailable"),
        ("http_503", "poll_transient"),
    ],
)
def test_result_url_refresh_failures_remain_bound_to_the_same_task(
    tmp_path, capsys, monkeypatch, failure, expected_state
):
    bundle, ready, dependencies, key, old_result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=ArchiveResponse(b"", status=404)),
    )
    assert expired_rc == 0, expired

    if failure == "credential_missing":
        refresh_environ = dependencies
        refresh_transport = NeverNetwork()
    elif failure == "credential_changed":
        refresh_environ = {**dependencies, "AIHUB_API_KEY": "different-key"}
        refresh_transport = NeverNetwork()
    else:
        refresh_environ = {**dependencies, "AIHUB_API_KEY": key}
        refresh_transport = StatusResponse(int(failure.removeprefix("http_")))
    refresh_rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(expired["generation"]),
        ],
        cwd=tmp_path,
        environ=refresh_environ,
        transport=refresh_transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    public_state = (
        (bundle / "manifest.json").read_text()
        + (bundle / ".state" / "history.ndjson").read_text()
    )
    assert refresh_rc == 0, result
    assert result["outcome"] == expected_state
    assert result["conversion_state"] == "recoverable_error"
    assert len(manifest["conversion_attempts"]) == 1
    assert manifest["conversion_attempts"][0]["task_id"] == "task-result-001"
    assert manifest["conversion_attempts"][0]["state"] == expected_state
    assert old_result_url not in public_state


@pytest.mark.parametrize(
    ("payload", "expected_state"),
    [
        (
            {
                "status": "completed",
                "results": [
                    {"url": "https://results.example/one.zip"},
                    {"url": "https://results.example/two.zip"},
                ],
            },
            "unexpected_result_count",
        ),
        (
            {
                "status": "completed",
                "results": [{"url": "http://results.example/unsafe.zip"}],
            },
            "unsafe_result_url",
        ),
    ],
)
def test_result_url_refresh_state_is_not_masked_by_the_expired_raw_operation(
    tmp_path, capsys, monkeypatch, payload, expected_state
):
    bundle, ready, dependencies, key, _old_result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    _expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=ArchiveResponse(b"", status=404)),
    )
    _refresh_rc, refreshed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(expired["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=JsonResponse(payload),
    )
    assert refreshed["outcome"] == expected_state

    if expected_state == "unexpected_result_count":
        resumed_rc, resumed, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(refreshed["generation"]),
            ],
            cwd=tmp_path,
            environ={**dependencies, "AIHUB_API_KEY": key},
            transport=NeverNetwork(),
        )
        assert resumed_rc == 0, resumed
        assert resumed["outcome"] == "unexpected_result_count"
        return

    replacement_url = "https://results.example/replacement.zip?token=fresh"
    repoll = JsonResponse(
        {"status": "completed", "results": [{"url": replacement_url}]}
    )
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(refreshed["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=repoll,
    )
    assert resumed_rc == 0, resumed
    assert resumed["outcome"] == "result_ready"
    assert len(repoll.calls) == 1


class SimulatedProcessCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary",
    [
        "extraction",
        "prepared",
        "rename",
        "parent_fsync",
        "manifest",
        "committed",
    ],
)
def test_every_raw_adoption_crash_boundary_recovers_without_a_new_task_or_get(
    tmp_path, capsys, monkeypatch, boundary
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive_bytes = make_zip(
        [(f"{request_filename}.md", b"# Crash\n"), ("asset.bin", b"asset")]
    )
    originals = {}
    if boundary == "extraction":
        originals["write_member"] = result_archive._write_member

        def crash_after_one_member(*args, **kwargs):
            result = originals["write_member"](*args, **kwargs)
            raise SimulatedProcessCrash

        monkeypatch.setattr(result_archive, "_write_member", crash_after_one_member)
    elif boundary == "prepared":
        originals["append_prepared"] = raw_conversion._append_prepared

        def crash_before_prepared(**_kwargs):
            raise SimulatedProcessCrash

        monkeypatch.setattr(raw_conversion, "_append_prepared", crash_before_prepared)
    elif boundary == "rename":
        originals["rename"] = raw_conversion._rename_staging

        def crash_before_rename(*_args, **_kwargs):
            raise SimulatedProcessCrash

        monkeypatch.setattr(raw_conversion, "_rename_staging", crash_before_rename)
    elif boundary == "parent_fsync":
        originals["fsync"] = raw_conversion._fsync_attempts

        def crash_before_parent_fsync(*_args, **_kwargs):
            raise SimulatedProcessCrash

        monkeypatch.setattr(raw_conversion, "_fsync_attempts", crash_before_parent_fsync)
    elif boundary == "manifest":
        originals["atomic"] = raw_conversion.bundle.atomic_write_json

        def crash_before_manifest(name, value, *, dir_fd):
            if name == "manifest.json" and "raw_conversion" in value:
                raise SimulatedProcessCrash
            return originals["atomic"](name, value, dir_fd=dir_fd)

        monkeypatch.setattr(
            raw_conversion.bundle, "atomic_write_json", crash_before_manifest
        )
    else:
        originals["append"] = raw_conversion.bundle.append_history

        def crash_before_committed(value, *, state_fd):
            if value.get("event") == "raw_conversion_committed":
                raise SimulatedProcessCrash
            return originals["append"](value, state_fd=state_fd)

        monkeypatch.setattr(
            raw_conversion.bundle, "append_history", crash_before_committed
        )

    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=ArchiveTransport(archive_bytes),
            now=NOW,
        )
    capsys.readouterr()

    if boundary == "extraction":
        monkeypatch.setattr(result_archive, "_write_member", originals["write_member"])
    elif boundary == "prepared":
        monkeypatch.setattr(
            raw_conversion, "_append_prepared", originals["append_prepared"]
        )
    elif boundary == "rename":
        monkeypatch.setattr(raw_conversion, "_rename_staging", originals["rename"])
    elif boundary == "parent_fsync":
        recovery_parent_fsync_calls = []

        def track_recovery_parent_fsync(attempts_fd):
            recovery_parent_fsync_calls.append(attempts_fd)
            return originals["fsync"](attempts_fd)

        monkeypatch.setattr(
            raw_conversion, "_fsync_attempts", track_recovery_parent_fsync
        )
    elif boundary == "manifest":
        monkeypatch.setattr(
            raw_conversion.bundle, "atomic_write_json", originals["atomic"]
        )
    else:
        monkeypatch.setattr(
            raw_conversion.bundle, "append_history", originals["append"]
        )

    rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert rc == 0, (boundary, recovered)
    assert recovered["outcome"] == "raw_conversion_adopted"
    assert recovered["generation"] == ready["generation"] + 1
    assert manifest["conversion_attempts"][-1]["task_id"] == "task-result-001"
    assert sum(event.get("event") == "raw_conversion_intent" for event in history) == 1
    assert sum(event.get("event") == "raw_conversion_prepared" for event in history) == 1
    assert sum(event.get("event") == "raw_conversion_committed" for event in history) == 1
    if boundary == "parent_fsync":
        assert recovery_parent_fsync_calls


def test_committed_zip_tree_and_main_markdown_are_immutable_and_reverified(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive_bytes = make_zip(
        [(f"{request_filename}.md", b"# Immutable\n"), ("asset.bin", b"asset")]
    )
    rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(archive_bytes),
    )
    final = (
        bundle
        / "03-converted"
        / "attempts"
        / "conversion-attempt-0001"
    )
    raw = final / "raw"
    markdown_path = raw / f"{request_filename}.md"
    assert rc == 0, converted

    original_markdown = markdown_path.read_bytes()
    markdown_path.write_bytes(b"tampered")
    tampered_rc, tampered, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert tampered_rc == 4
    assert tampered["errors"][0]["code"] == "integrity_violation"
    markdown_path.write_bytes(original_markdown)
    markdown_path.chmod(0o600)

    archive_path = final / "result.zip"
    original_archive = archive_path.read_bytes()
    archive_path.write_bytes(original_archive + b"tamper")
    zip_rc, zip_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert zip_rc == 4
    assert zip_result["errors"][0]["code"] == "integrity_violation"
    archive_path.write_bytes(original_archive)
    archive_path.chmod(0o600)

    external = tmp_path / "linked.bin"
    external.write_bytes(b"linked")
    external.chmod(0o600)
    os.link(external, raw / "hardlink.bin")
    link_rc, link_result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert link_rc == 4
    assert link_result["errors"][0]["code"] == "integrity_violation"
    (raw / "hardlink.bin").unlink()
    external.unlink()

    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    assert resumed_rc == 0
    assert resumed["outcome"] == "converted"
    assert resumed["generation"] == converted["generation"]


@pytest.mark.parametrize("replacement", ["file", "directory"])
def test_committed_inspection_rejects_path_replacement_during_verification(
    tmp_path, capsys, monkeypatch, replacement
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    _adopt_rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(
            make_zip([(f"{request_filename}.md", b"# Stable\n")])
        ),
    )
    final = (
        bundle
        / "03-converted"
        / "attempts"
        / "conversion-attempt-0001"
    )
    raw = final / "raw"
    markdown = raw / f"{request_filename}.md"
    swapped = False
    if replacement == "file":
        target_inode = markdown.stat().st_ino
        detached = raw / "detached.md"
        original_read = raw_conversion.os.read

        def read_then_replace(descriptor, size):
            nonlocal swapped
            chunk = original_read(descriptor, size)
            if not swapped and os.fstat(descriptor).st_ino == target_inode:
                swapped = True
                markdown.rename(detached)
                markdown.write_bytes(b"tampered")
                markdown.chmod(0o600)
            return chunk

        monkeypatch.setattr(raw_conversion.os, "read", read_then_replace)
    else:
        target_inode = raw.stat().st_ino
        detached = final / "raw-detached"
        original_tree_records = raw_conversion._tree_records

        def records_then_replace(descriptor, **kwargs):
            nonlocal swapped
            records = original_tree_records(descriptor, **kwargs)
            if not swapped and os.fstat(descriptor).st_ino == target_inode:
                swapped = True
                raw.rename(detached)
                raw.mkdir(mode=0o700)
                raw.chmod(0o700)
                replacement_markdown = raw / f"{request_filename}.md"
                replacement_markdown.write_bytes(b"tampered")
                replacement_markdown.chmod(0o600)
            return records

        monkeypatch.setattr(raw_conversion, "_tree_records", records_then_replace)

    inspect_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert swapped
    assert inspect_rc == 4, (converted, inspected)
    assert inspected["errors"][0]["code"] == "integrity_violation"


@pytest.mark.parametrize(
    "artifact",
    ["01-source/source-inventory.json", "02-pages/page-0001.png"],
)
def test_resume_revalidates_the_frozen_baseline_before_raw_recovery_or_network(
    tmp_path, capsys, monkeypatch, artifact
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    interrupted_rc, interrupted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(response=InterruptedArchiveResponse()),
    )
    assert interrupted_rc == 4
    assert interrupted["errors"][0]["code"] == "result_download_failed"
    attempts_before = sorted(
        path.name for path in (bundle / "03-converted" / "attempts").iterdir()
    )

    target = bundle / artifact
    target.write_bytes(target.read_bytes() + b"tamper")
    target.chmod(0o600)

    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )

    assert rc == 4
    assert result["errors"][0]["code"] == "integrity_violation"
    assert sorted(
        path.name for path in (bundle / "03-converted" / "attempts").iterdir()
    ) == attempts_before


@pytest.mark.parametrize("drift", ["both_paths", "missing", "hash"])
def test_prepared_recovery_rejects_path_or_hash_drift(
    tmp_path, capsys, monkeypatch, drift
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive_bytes = make_zip([(f"{request_filename}.md", b"# Prepared\n")])
    original_rename = raw_conversion._rename_staging

    def crash_before_rename(*_args, **_kwargs):
        raise SimulatedProcessCrash

    monkeypatch.setattr(raw_conversion, "_rename_staging", crash_before_rename)
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=ArchiveTransport(archive_bytes),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(raw_conversion, "_rename_staging", original_rename)
    attempts = bundle / "03-converted" / "attempts"
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    intent = next(event for event in history if event.get("event") == "raw_conversion_intent")
    part = attempts / intent["staging_name"]
    final = attempts / "conversion-attempt-0001"
    if drift == "both_paths":
        shutil.copytree(part, final)
    elif drift == "missing":
        shutil.rmtree(part)
    else:
        markdown_path = part / "raw" / f"{request_filename}.md"
        markdown_path.write_bytes(b"changed")
        markdown_path.chmod(0o600)

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverArchiveNetwork(),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"
    assert json.loads((bundle / "manifest.json").read_text())[
        "conversion_state"
    ] == "result_downloading"


def test_archive_member_count_and_uncompressed_byte_limits_are_enforced(
    tmp_path, capsys, monkeypatch
):
    mib = 1024 * 1024
    cases = [
        (
            "member-size",
            make_deflated_sizes(256 * mib + 1),
            "archive_member_size_limit_exceeded",
        ),
        (
            "total-size",
            make_deflated_sizes(129 * mib, 129 * mib),
            "archive_uncompressed_limit_exceeded",
        ),
        (
            "member-count",
            make_many_members(zipfile.ZIP_FILECOUNT_LIMIT + 1),
            "archive_member_limit_exceeded",
        ),
    ]
    for label, archive_bytes, reason in cases:
        case_root = tmp_path / label
        case_root.mkdir()
        bundle, ready, dependencies, key, _result_url = ready_result_bundle(
            case_root, capsys, monkeypatch
        )
        rc, rejected, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(ready["generation"]),
            ],
            cwd=case_root,
            environ={**dependencies, "AIHUB_API_KEY": key},
            transport=ArchiveTransport(archive_bytes),
        )
        assert rc == 0, rejected
        assert rejected["outcome"] == reason
        assert not (
            bundle
            / "03-converted"
            / "attempts"
            / "conversion-attempt-0001"
        ).exists()


@pytest.mark.parametrize(
    ("label", "member_path", "reason"),
    [
        (
            "path-bytes",
            "/".join(["a" * 200] * 6) + "/document.md",
            "archive_member_path_limit_exceeded",
        ),
        (
            "component-bytes",
            "a" * 256 + ".md",
            "archive_path_component_limit_exceeded",
        ),
        (
            "path-depth",
            "/".join(["a"] * 128 + ["document.md"]),
            "archive_path_depth_limit_exceeded",
        ),
        (
            "canonical-component-bytes",
            "\u0130" * 85 + ".md",
            "archive_path_component_limit_exceeded",
        ),
    ],
)
def test_archive_member_path_resource_limits_are_enforced(
    tmp_path, capsys, monkeypatch, label, member_path, reason
):
    case_root = tmp_path / label
    case_root.mkdir()
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        case_root, capsys, monkeypatch
    )

    rc, rejected, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=case_root,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(make_zip([(member_path, b"body")])),
    )

    assert rc == 0, rejected
    assert rejected["outcome"] == reason
    assert not (
        bundle / "03-converted" / "attempts" / "conversion-attempt-0001"
    ).exists()


@pytest.mark.parametrize(
    ("label", "member_path"),
    [
        ("component-boundary", "a" * 252 + ".md"),
        (
            "path-boundary",
            "/".join(["a" * 255, "b" * 255, "c" * 255, "d" * 251, "a.md"]),
        ),
        ("depth-boundary", "/".join(["a"] * 127 + ["a.md"])),
        ("canonical-component-boundary", "\u0130" * 84 + ".md"),
    ],
)
def test_archive_member_path_resource_boundaries_are_inclusive(
    tmp_path, capsys, monkeypatch, label, member_path
):
    case_root = tmp_path / label
    case_root.mkdir()
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        case_root, capsys, monkeypatch
    )

    rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=case_root,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(make_zip([(member_path, b"body")])),
    )

    assert rc == 0, converted
    assert converted["outcome"] == "raw_conversion_adopted"


def test_archive_total_path_component_budget_is_inclusive(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(result_archive, "MAX_TOTAL_PATH_COMPONENTS", 4)
    accepted_root = tmp_path / "accepted"
    accepted_root.mkdir()
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        accepted_root, capsys, monkeypatch
    )
    accepted = make_zip([("dir/asset.bin", b"asset"), ("dir/document.md", b"body")])
    accepted_rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=accepted_root,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(accepted),
    )
    assert accepted_rc == 0, converted
    assert converted["outcome"] == "raw_conversion_adopted"

    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        rejected_root, capsys, monkeypatch
    )
    rejected = make_zip(
        [
            ("dir/asset.bin", b"asset"),
            ("dir/document.md", b"body"),
            ("extra.bin", b"extra"),
        ]
    )
    rejected_rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=rejected_root,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(rejected),
    )
    assert rejected_rc == 0, result
    assert result["outcome"] == "archive_path_component_budget_exceeded"


def test_explicit_directory_after_same_spelling_implicit_directory_is_accepted(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    archive = make_zip(
        [(f"nested/{request_filename}.md", b"body"), ("nested/", b"")]
    )

    rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(archive),
    )

    assert rc == 0, converted
    assert converted["outcome"] == "raw_conversion_adopted"


def test_converted_bundle_keeps_recoverable_settings_overrides(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    rc, converted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=ArchiveTransport(
            make_zip([(f"{request_filename}.md", b"# Settings\n")])
        ),
    )
    assert rc == 0, converted

    override_rc, overridden, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )

    assert override_rc == 0, overridden
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["conversion_state"] == "converted"
    assert overridden["generation"] == converted["generation"] + 1
    inspect_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    assert inspect_rc == 0, inspected
    assert inspected["generation"] == overridden["generation"]
    assert inspected["artifacts"] == converted["artifacts"]
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(inspected["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverArchiveNetwork(),
    )
    assert resumed_rc == 0, resumed
    assert resumed["outcome"] == "converted"
    assert resumed["generation"] == inspected["generation"]
