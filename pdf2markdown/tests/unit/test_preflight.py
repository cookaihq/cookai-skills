"""Command-level contracts for page baselines and the preflight gate."""

import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

import fitz
import preflight
import pytest
import workflow


NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prohibit_direct_network(monkeypatch):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("preflight commands must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)


def zero_page_pdf_bytes():
    header = b"%PDF-1.4\n"
    catalog_offset = len(header)
    catalog = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    pages_offset = catalog_offset + len(catalog)
    pages = b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    xref_offset = pages_offset + len(pages)
    xref = (
        b"xref\n0 3\n0000000000 65535 f \n"
        + f"{catalog_offset:010d} 00000 n \n".encode()
        + f"{pages_offset:010d} 00000 n \n".encode()
    )
    trailer = (
        b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return header + catalog + pages + xref + trailer


ZERO_PAGE_PDF = zero_page_pdf_bytes()


class NeverNetwork:
    def __init__(self):
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("preflight must not use the network")


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


def make_structured_pdf(path):
    document = fitz.open()
    first = document.new_page(width=72, height=72)
    first.insert_text((8, 18), "Page one")
    first.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(8, 22, 58, 32),
            "uri": "https://example.com/reference?token=secret#section",
        }
    )
    widget = fitz.Widget()
    widget.field_name = "reader-name"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_value = "Ada"
    widget.rect = fitz.Rect(8, 36, 58, 50)
    first.add_widget(widget)
    password = fitz.Widget()
    password.field_name = "account-password"
    password.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    password.field_flags = fitz.PDF_TX_FIELD_IS_PASSWORD
    password.field_value = "do-not-persist"
    password.rect = fitz.Rect(8, 52, 58, 66)
    first.add_widget(password)
    second = document.new_page(width=72, height=72)
    second.insert_text((8, 18), "Page two")
    document.save(path)
    document.close()


def make_encrypted_pdf(path, *, user_password):
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 18), "Encrypted page")
    document.save(
        path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw=user_password,
    )
    document.close()


def make_large_page_pdf(path):
    document = fitz.open()
    page = document.new_page(width=5_000, height=5_000)
    page.insert_text((20, 40), "Large page")
    document.save(path)
    document.close()


def make_repaired_pdf(path):
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 18), "Recoverable xref")
    document.save(path)
    document.close()
    path.write_bytes(path.read_bytes()[:-20])


def make_many_page_pdf(path, *, page_count):
    document = fitz.open()
    for page_number in range(1, page_count + 1):
        page = document.new_page(width=612, height=792)
        page.insert_text((36, 48), f"Page {page_number}")
    document.save(path)
    document.close()


def make_invalid_content_stream_pdf(path):
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 18), "Broken stream")
    content_xref = page.get_contents()[0]
    document.update_stream(content_xref, b"q\nBT\n(unterminated Tj\nET\nQ")
    document.save(path)
    document.close()


def make_visual_risk_pdf(path):
    document = fitz.open()
    document.new_page(width=144, height=144)

    scan = document.new_page(width=144, height=144)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixmap.clear_with(180)
    scan.insert_image(scan.rect, stream=pixmap.tobytes("png"))

    columns = document.new_page(width=144, height=144)
    columns.insert_textbox(fitz.Rect(8, 8, 66, 130), "Left column\nA\nB\nC")
    columns.insert_textbox(fitz.Rect(78, 8, 136, 130), "Right column\nD\nE\nF")
    columns.set_rotation(90)

    mixed = document.new_page(width=144, height=144)
    mixed.insert_text((8, 18), "Table and image")
    mixed.insert_image(fitz.Rect(88, 8, 136, 56), stream=pixmap.tobytes("png"))
    for y in (72, 96, 120, 143):
        mixed.draw_line((8, y), (136, y))
    for x in (8, 72, 136):
        mixed.draw_line((x, 72), (x, 143))

    continuation = document.new_page(width=144, height=144)
    continuation.insert_text((8, 18), "Table continued")
    for y in (0, 24, 48, 72):
        continuation.draw_line((8, y), (136, y))
    for x in (8, 72, 136):
        continuation.draw_line((x, 0), (x, 72))
    document.save(path)
    document.close()


def make_blank_pdf(path, *, page_count=2):
    document = fitz.open()
    for _ in range(page_count):
        document.new_page(width=72, height=72)
    document.save(path)
    document.close()


def test_advance_builds_a_complete_300_dpi_baseline_and_bound_preflight_action(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "structured.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()

    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    advance_rc, advanced, advance_stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    inventory = json.loads(
        (bundle / "01-source" / "source-inventory.json").read_text()
    )
    assert advance_rc == 0
    assert advanced["generation"] == 2
    assert advanced["conversion_state"] == "preflight_pending"
    assert advanced["outcome"] == "awaiting_preflight"
    assert advanced["action_required"] == "record_preflight"
    assert re.fullmatch(r"preflight-[0-9a-f]{32}", advanced["action_id"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", advanced["evidence_hash"])
    assert advanced["artifacts"] == {
        "manifest": "manifest.json",
        "source_pdf": "01-source/source.pdf",
        "source_inventory": "01-source/source-inventory.json",
        "page_references": [
            "02-pages/page-0001.png",
            "02-pages/page-0002.png",
        ],
    }
    assert inventory["schema_version"] == 1
    assert inventory["page_count"] == 2
    assert inventory["render"] == {
        "dpi": 300,
        "format": "png",
        "lossless": True,
    }
    assert [page["page_number"] for page in inventory["pages"]] == [1, 2]
    assert [page["pixel_width"] for page in inventory["pages"]] == [300, 300]
    assert [page["pixel_height"] for page in inventory["pages"]] == [300, 300]
    assert inventory["pages"][0]["links"] == [
        {
            "kind": "external_uri",
            "uri": "https://example.com/reference",
            "full_uri_sha256": "40dcd30274a764cd01d5469dbe1abadb9a396e6f4b10b788c7d13a83b043d45b",
            "query_redacted": True,
            "fragment_redacted": True,
            "from": [8.0, 22.0, 58.0, 32.0],
        }
    ]
    assert inventory["pages"][0]["forms"] == [
        {
            "field_name": "reader-name",
            "field_type": "Text",
            "field_value": "Ada",
            "rect": [8.0, 36.0, 58.0, 50.0],
        },
        {
            "field_name": "account-password",
            "field_type": "Text",
            "field_value": None,
            "value_present": True,
            "value_redacted": True,
            "rect": [8.0, 52.0, 58.0, 66.0],
        },
    ]
    assert "do-not-persist" not in (
        bundle / "01-source" / "source-inventory.json"
    ).read_text()
    for page_number in (1, 2):
        page_path = bundle / "02-pages" / f"page-{page_number:04d}.png"
        pixmap = fitz.Pixmap(page_path)
        assert (pixmap.width, pixmap.height) == (300, 300)
        assert re.fullmatch(
            r"[0-9a-f]{64}", inventory["pages"][page_number - 1]["image_sha256"]
        )
        assert os.stat(page_path).st_mode & 0o777 == 0o600
    assert "awaiting_preflight" in advance_stderr
    assert transport.calls == 0


def test_advance_uses_custom_dpi_and_continuous_page_reference_names(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "twelve-pages.pdf"
    make_many_page_pdf(source, page_count=12)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    advance_rc, advanced, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
            "--render-dpi",
            "144",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    inventory = json.loads(
        (bundle / "01-source" / "source-inventory.json").read_text()
    )
    expected_paths = [
        f"02-pages/page-{page_number:04d}.png"
        for page_number in range(1, 13)
    ]
    assert advance_rc == 0
    assert advanced["conversion_state"] == "preflight_pending"
    assert advanced["artifacts"]["page_references"] == expected_paths
    assert inventory["render"]["dpi"] == 144
    assert [page["page_reference"] for page in inventory["pages"]] == expected_paths
    assert {
        (page["pixel_width"], page["pixel_height"])
        for page in inventory["pages"]
    } == {(1224, 1584)}
    assert sorted(path.name for path in (bundle / "02-pages").iterdir()) == [
        path.removeprefix("02-pages/") for path in expected_paths
    ]
    assert transport.calls == 0


def test_advance_rejects_an_out_of_range_render_dpi_without_writing(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "invalid-dpi.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    bundle = Path(started["work_bundle"])
    before = bundle_bytes(bundle)

    advance_rc, rejected, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
            "--render-dpi",
            "71",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert advance_rc == 2
    assert rejected["errors"][0]["code"] == "invalid_render_dpi"
    assert rejected["action_required"] == "correct_command_arguments"
    assert bundle_bytes(bundle) == before
    assert transport.calls == 0


def test_missing_dependencies_are_recoverable_before_rendering_or_network(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "dependency.pdf"
    make_structured_pdf(source)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert start_rc == 0
    original_import_module = preflight.importlib.import_module

    def missing_preflight_modules(name, *args, **kwargs):
        if name in {"fitz", "bs4"}:
            raise ModuleNotFoundError(name)
        return original_import_module(name, *args, **kwargs)

    monkeypatch.setattr(
        preflight.importlib, "import_module", missing_preflight_modules
    )

    missing_rc, missing, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "unavailable",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert missing_rc == 0
    assert missing["generation"] == 2
    assert missing["conversion_state"] == "recoverable_error"
    assert missing["outcome"] == "dependency_missing"
    assert missing["action_required"] == "restore_preflight_dependencies"
    assert missing["missing_dependencies"] == [
        "pymupdf",
        "pandoc",
        "beautifulsoup4",
        "host_visual",
    ]
    assert manifest["preflight"]["reason_code"] == "dependency_missing"
    assert manifest["preflight"]["resume_state"] == "preparing"
    assert not (bundle / "01-source" / "source-inventory.json").exists()
    assert list((bundle / "02-pages").iterdir()) == []

    monkeypatch.setattr(preflight.importlib, "import_module", original_import_module)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    partial_rc, partial, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "2",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )

    partial_manifest = json.loads((bundle / "manifest.json").read_text())
    assert partial_rc == 0
    assert partial["generation"] == 3
    assert partial["missing_dependencies"] == ["pandoc"]
    assert partial_manifest["preflight"]["missing"] == ["pandoc"]

    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "3",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recovered_rc == 0
    assert recovered["generation"] == 4
    assert recovered["conversion_state"] == "preflight_pending"
    assert (bundle / "01-source" / "source-inventory.json").is_file()
    assert transport.calls == 0


def test_incompatible_pymupdf_api_is_a_recoverable_dependency_error(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "incompatible-pymupdf.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_import_module = preflight.importlib.import_module

    class IncompatiblePyMuPDF:
        VersionBind = "0.0-test"

    def incompatible_pymupdf(name, *args, **kwargs):
        if name == "fitz":
            return IncompatiblePyMuPDF()
        return original_import_module(name, *args, **kwargs)

    monkeypatch.setattr(
        preflight.importlib, "import_module", incompatible_pymupdf
    )

    missing_rc, missing, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    pymupdf = next(
        item
        for item in manifest["preflight"]["dependencies"]
        if item["name"] == "pymupdf"
    )
    assert missing_rc == 0
    assert missing["outcome"] == "dependency_missing"
    assert missing["missing_dependencies"] == ["pymupdf"]
    assert pymupdf["available"] is False
    assert pymupdf["version"] == "0.0-test"
    assert pymupdf["reason"] == "incompatible_api"
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def prepare_baseline(tmp_path, capsys, monkeypatch, *, interaction_mode="confirm"):
    source = tmp_path / "preflight.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_argv = ["start", "--source", str(source)]
    if interaction_mode != "confirm":
        start_argv.extend(["--interaction-mode", interaction_mode])
    start_rc, started, _stderr = invoke(
        capsys,
        start_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    advance_rc, advanced, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert advance_rc == 0
    return Path(started["work_bundle"]), advanced, dependencies, transport


def write_preflight_record(tmp_path, *, summary, pages):
    record = tmp_path / f"{summary}-preflight.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": summary,
                "pages": pages,
            }
        )
    )
    return record


def prepare_confirm_warning(tmp_path, capsys, monkeypatch):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "risk",
                "risk_codes": ["complex_multicolumn"],
                "evidence": ["Two reading columns are visible."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page two content is readable."],
            },
        ],
    )
    warning_rc, warning, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert warning_rc == 0
    assert warning["conversion_state"] == "preflight_warning"
    return bundle, warning, dependencies, transport


def test_record_preflight_pass_atomically_consumes_the_action_and_stops_before_upload(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="pass",
        pages=[
            {
                "page_number": 1,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Printed text, link label, and form value are readable."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Printed page text is readable."],
            },
        ],
    )

    record_rc, recorded, record_stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    preflight_record = json.loads(
        (bundle / "04-review" / "preflight.json").read_text()
    )
    assert record_rc == 0
    assert recorded["generation"] == advanced["generation"] + 1
    assert recorded["conversion_state"] == "ready_to_submit"
    assert recorded["outcome"] == "preflight_recorded"
    assert recorded["action_required"] is None
    assert recorded["action_id"] is None
    assert recorded["artifacts"]["preflight"] == "04-review/preflight.json"
    assert preflight_record["result"] == "pass"
    assert [page["page_number"] for page in preflight_record["pages"]] == [1, 2]
    assert preflight_record["baseline_evidence_hash"] == advanced["evidence_hash"]
    assert os.stat(bundle / "04-review" / "preflight.json").st_mode & 0o777 == 0o600
    assert "preflight_recorded" in record_stderr
    assert transport.calls == 0


def test_derived_preflight_result_limit_is_rejected_before_writing_intent(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="pass",
        pages=[
            {
                "page_number": page_number,
                "classification": "content",
                "risk_codes": [],
                "evidence": [f"Page {page_number} content is readable."],
            }
            for page_number in (1, 2)
        ],
    )
    before = bundle_bytes(bundle)
    monkeypatch.setattr(preflight, "MAX_PREFLIGHT_RESULT_BYTES", 1)

    record_rc, rejected, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert record_rc == 2
    assert rejected["errors"][0]["code"] == "preflight_result_limit_exceeded"
    assert bundle_bytes(bundle) == before
    assert transport.calls == 0


@pytest.mark.parametrize(
    ("decision", "expected_state", "expected_outcome", "expected_status"),
    [
        ("accept", "ready_to_submit", "preflight_warning_accepted", "accepted"),
        ("decline", "terminal_error", "preflight_warning_declined", "declined"),
    ],
)
def test_confirm_warning_requires_a_bound_structured_decision_before_submission(
    tmp_path,
    capsys,
    monkeypatch,
    decision,
    expected_state,
    expected_outcome,
    expected_status,
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "risk",
                "risk_codes": ["complex_multicolumn", "cross_page_table"],
                "evidence": ["Two reading columns and a table continuation are visible."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Printed page text is readable."],
            },
        ],
    )
    warning_rc, warning, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert warning_rc == 0
    assert warning["conversion_state"] == "preflight_warning"
    assert warning["outcome"] == "preflight_warning"
    assert warning["action_required"] == "record_preflight_decision"
    assert re.fullmatch(r"preflight-decision-[0-9a-f]{32}", warning["action_id"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", warning["evidence_hash"])

    decision_rc, decided, _stderr = invoke(
        capsys,
        [
            "record",
            "decision",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(warning["generation"]),
            "--action-id",
            warning["action_id"],
            "--evidence-hash",
            warning["evidence_hash"],
            "--decision",
            decision,
            "--basis",
            "User accepts the documented conversion risks and fee boundary.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    preflight_bytes = (bundle / "04-review" / "preflight.json").read_bytes()
    assert decision_rc == 0
    assert decided["generation"] == warning["generation"] + 1
    assert decided["conversion_state"] == expected_state
    assert decided["outcome"] == expected_outcome
    assert decided["action_required"] is None
    assert manifest["preflight"]["decision"]["status"] == expected_status
    assert manifest["preflight"]["decision"]["source"] == "user_confirmation"
    assert manifest["preflight"]["reason_code"] == (
        None if decision == "accept" else "preflight_declined"
    )
    assert (bundle / "04-review" / "preflight.json").read_bytes() == preflight_bytes
    assert transport.calls == 0


def test_auto_warning_records_policy_acceptance_without_an_intermediate_action(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "risk",
                "risk_codes": ["scanned_content"],
                "evidence": ["The page is a readable scan without extractable text."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Printed page text is readable."],
            },
        ],
    )

    warning_rc, warning, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert warning_rc == 0
    assert warning["conversion_state"] == "ready_to_submit"
    assert warning["outcome"] == "preflight_warning_auto_accepted"
    assert warning["action_required"] is None
    assert manifest["preflight"]["result"]["status"] == "warning"
    assert manifest["preflight"]["decision"]["status"] == "accepted"
    assert manifest["preflight"]["decision"]["source"] == "interaction_mode_auto"
    assert transport.calls == 0


@pytest.mark.parametrize("interaction_mode", ["confirm", "auto"])
def test_blocked_preflight_cannot_be_advanced_in_any_interaction_mode(
    tmp_path, capsys, monkeypatch, interaction_mode
):
    source = tmp_path / "blank.pdf"
    make_blank_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        [
            "start",
            "--source",
            str(source),
            "--interaction-mode",
            interaction_mode,
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    advance_rc, advanced, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert advance_rc == 0
    bundle = Path(started["work_bundle"])
    record = write_preflight_record(
        tmp_path,
        summary="blocked",
        pages=[
            {
                "page_number": 1,
                "classification": "blank",
                "risk_codes": [],
                "evidence": ["The complete rendered page has no visible content."],
            },
            {
                "page_number": 2,
                "classification": "blank",
                "risk_codes": [],
                "evidence": ["The complete rendered page has no visible content."],
            },
        ],
    )
    blocked_rc, blocked, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert blocked_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocked["outcome"] == "preflight_blocked"
    assert blocked["action_required"] is None

    advance_rc, still_blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(blocked["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert advance_rc == 0
    assert still_blocked["generation"] == blocked["generation"]
    assert still_blocked["conversion_state"] == "preflight_blocked"
    assert still_blocked["outcome"] == "preflight_blocked"
    assert transport.calls == 0


def test_zero_page_pdf_is_durably_blocked_after_the_source_is_preserved(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "zero-pages.pdf"
    source.write_bytes(ZERO_PAGE_PDF)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    preflight_record = json.loads(
        (bundle / "04-review" / "preflight.json").read_text()
    )
    assert block_rc == 0
    assert blocked["generation"] == 2
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocked["outcome"] == "preflight_blocked"
    assert blocked["action_required"] is None
    assert manifest["preflight"]["reason_code"] == "unreadable_input"
    assert manifest["preflight"]["deterministic_blockers"] == [
        {
            "code": "zero_pages",
            "pages": [],
            "evidence": "The PDF parser reported zero source pages.",
        }
    ]
    assert preflight_record["result"] == "blocked"
    assert preflight_record["deterministic_blockers"] == manifest["preflight"][
        "deterministic_blockers"
    ]
    assert (bundle / "01-source" / "source.pdf").read_bytes() == ZERO_PAGE_PDF
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_password_requirement_blocks_but_empty_password_encryption_is_not_misclassified(
    tmp_path, capsys, monkeypatch
):
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    protected_source = tmp_path / "protected.pdf"
    make_encrypted_pdf(protected_source, user_password="reader-secret")
    start_rc, protected_started, _stderr = invoke(
        capsys,
        ["start", "--source", str(protected_source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    protected_rc, protected, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            protected_started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    protected_manifest = json.loads(
        (Path(protected_started["work_bundle"]) / "manifest.json").read_text()
    )
    assert protected_rc == 0
    assert protected["conversion_state"] == "preflight_blocked"
    assert protected_manifest["preflight"]["deterministic_blockers"][0]["code"] == (
        "password_required"
    )

    open_source = tmp_path / "empty-password.pdf"
    make_encrypted_pdf(open_source, user_password="")
    start_rc, open_started, _stderr = invoke(
        capsys,
        ["start", "--source", str(open_source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    open_rc, opened, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            open_started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    inventory = json.loads(
        (
            Path(open_started["work_bundle"])
            / "01-source"
            / "source-inventory.json"
        ).read_text()
    )
    assert open_rc == 0
    assert opened["conversion_state"] == "preflight_pending"
    assert inventory["document_security"] == {
        "encryption_metadata_present": True,
        "password_required": False,
    }
    assert transport.calls == 0


def test_single_page_pixel_limit_blocks_before_allocating_a_page_image(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "large-page.pdf"
    make_large_page_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "page_pixel_limit_exceeded"
    assert blocker["pages"] == [1]
    assert blocker["observed_pixels"] == 434_055_556
    assert blocker["limit_pixels"] == 25_000_000
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_repaired_pdf_is_blocked_with_mupdf_evidence_without_polluting_stdout(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "repaired.pdf"
    make_repaired_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads(
        (Path(started["work_bundle"]) / "manifest.json").read_text()
    )
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "damaged_pdf"
    assert blocker["pages"] == []
    assert blocker["mupdf_repaired"] is True
    assert any("repair" in warning.lower() for warning in blocker["mupdf_warnings"])
    assert transport.calls == 0


def bundle_bytes(bundle):
    return {
        str(path.relative_to(bundle)): path.read_bytes()
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    }


def test_invalid_stale_and_replayed_preflight_records_are_rejected_without_writes(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    valid_pages = [
        {
            "page_number": 1,
            "classification": "content",
            "risk_codes": [],
            "evidence": ["Page one content is readable."],
        },
        {
            "page_number": 2,
            "classification": "content",
            "risk_codes": [],
            "evidence": ["Page two content is readable."],
        },
    ]
    record = write_preflight_record(tmp_path, summary="pass", pages=valid_pages)
    before = bundle_bytes(bundle)

    wrong_hash_rc, wrong_hash, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            "sha256:" + "0" * 64,
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert wrong_hash_rc == 5
    assert wrong_hash["errors"][0]["code"] == "evidence_hash_mismatch"
    assert bundle_bytes(bundle) == before

    wrong_action_rc, wrong_action, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            "preflight-" + "0" * 32,
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert wrong_action_rc == 5
    assert wrong_action["errors"][0]["code"] == "preflight_action_mismatch"
    assert bundle_bytes(bundle) == before

    stale_rc, stale, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"] - 1),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert stale_rc == 5
    assert stale["errors"][0]["code"] == "generation_conflict"
    assert bundle_bytes(bundle) == before

    incomplete = write_preflight_record(
        tmp_path, summary="pass", pages=valid_pages[:1]
    )
    incomplete_rc, incomplete_result, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(incomplete),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert incomplete_rc == 2
    assert incomplete_result["errors"][0]["code"] == "invalid_preflight_record"
    assert bundle_bytes(bundle) == before

    contradictory = write_preflight_record(
        tmp_path, summary="warning", pages=valid_pages
    )
    contradictory_rc, contradictory_result, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(contradictory),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert contradictory_rc == 2
    assert contradictory_result["errors"][0]["code"] == (
        "invalid_preflight_record"
    )
    assert bundle_bytes(bundle) == before

    valid = write_preflight_record(tmp_path, summary="pass", pages=valid_pages)
    valid_rc, recorded, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(valid),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert valid_rc == 0
    committed = bundle_bytes(bundle)

    replay_rc, replay, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(recorded["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(valid),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert replay_rc == 5
    assert replay["errors"][0]["code"] == "action_already_consumed"
    assert bundle_bytes(bundle) == committed
    assert transport.calls == 0


def test_inspect_and_resume_preserve_the_same_pending_preflight_action(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    before = bundle_bytes(bundle)

    inspect_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    resume_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert inspect_rc == 0
    assert resume_rc == 0
    for result in (inspected, resumed):
        assert result["generation"] == advanced["generation"]
        assert result["action_id"] == advanced["action_id"]
        assert result["evidence_hash"] == advanced["evidence_hash"]
        assert result["action_required"] == "record_preflight"
    assert resumed["outcome"] == "no_progress"
    assert bundle_bytes(bundle) == before
    assert transport.calls == 0


def test_resume_with_explicit_visual_capability_recovers_a_dependency_gate(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "resume-dependency.pdf"
    make_structured_pdf(source)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert start_rc == 0
    missing_rc, missing, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "unavailable",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert missing_rc == 0
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)

    resume_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            str(missing["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert resume_rc == 0
    assert resumed["generation"] == missing["generation"] + 1
    assert resumed["conversion_state"] == "preflight_pending"
    assert resumed["action_required"] == "record_preflight"
    assert transport.calls == 0


def test_dependency_gate_recovers_an_intent_before_its_prepared_event(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "dependency-intent.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_append = preflight.bundle.append_history

    def fail_prepared(event, *, state_fd):
        if event.get("event") == "preflight_dependency_prepared":
            raise OSError("simulated dependency preparation interruption")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(preflight.bundle, "append_history", fail_prepared)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert crash_rc == 1
    bundle = Path(started["work_bundle"])
    assert json.loads(
        (bundle / ".state" / "history.ndjson").read_text().splitlines()[-1]
    )["event"] == "preflight_dependency_intent"

    monkeypatch.setattr(preflight.bundle, "append_history", original_append)
    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "recoverable_error"
    assert recovered["missing_dependencies"] == ["pandoc"]
    assert transport.calls == 0


def test_resume_override_rebinds_a_pending_preflight_action_to_the_new_generation(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )

    override_rc, overridden, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert override_rc == 0
    assert overridden["generation"] == advanced["generation"] + 1
    assert overridden["conversion_state"] == "preflight_pending"
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["action_id"] == advanced["action_id"]
    assert overridden["evidence_hash"] == advanced["evidence_hash"]
    assert manifest["settings_snapshot"]["interaction_mode"] == "auto"
    assert manifest["preflight"]["pending_action"] == {
        "kind": "record_preflight",
        "action_id": advanced["action_id"],
        "generation": overridden["generation"],
        "evidence_hash": advanced["evidence_hash"],
    }

    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "risk",
                "risk_codes": ["complex_multicolumn"],
                "evidence": ["Two reading columns are visible."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page two content is readable."],
            },
        ],
    )
    warning_rc, warning, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(overridden["generation"]),
            "--action-id",
            overridden["action_id"],
            "--evidence-hash",
            overridden["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    final_manifest = json.loads((bundle / "manifest.json").read_text())
    assert warning_rc == 0
    assert warning["generation"] == overridden["generation"] + 1
    assert warning["conversion_state"] == "ready_to_submit"
    assert warning["outcome"] == "preflight_warning_auto_accepted"
    assert final_manifest["preflight"]["decision"]["source"] == (
        "interaction_mode_auto"
    )
    assert transport.calls == 0


@pytest.mark.parametrize(
    "crash_point", ["prepared", "private", "manifest", "committed"]
)
def test_resume_recovers_a_preflight_settings_override_at_each_commit_boundary(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    argv = [
        "resume",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(advanced["generation"]),
        "--interaction-mode",
        "auto",
    ]
    original_append = workflow.bundle_module.append_history
    original_atomic_write = workflow.bundle_module.atomic_write_json

    def crash_append(event, *, state_fd):
        if (
            crash_point == "prepared"
            and event.get("event") == "settings_override_prepared"
        ) or (
            crash_point == "committed"
            and event.get("event") == "settings_override_committed"
        ):
            raise OSError(f"simulated {crash_point} history crash")
        return original_append(event, state_fd=state_fd)

    def crash_atomic(name, value, *, dir_fd):
        if name == f"{crash_point}.json":
            raise OSError(f"simulated {crash_point} state crash")
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(workflow.bundle_module, "append_history", crash_append)
    monkeypatch.setattr(workflow.bundle_module, "atomic_write_json", crash_atomic)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(workflow.bundle_module, "append_history", original_append)
    monkeypatch.setattr(
        workflow.bundle_module, "atomic_write_json", original_atomic_write
    )

    recover_rc, recovered, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert recover_rc == 0
    assert recovered["generation"] == advanced["generation"] + 1
    assert recovered["outcome"] == "settings_overridden"
    assert recovered["action_id"] == advanced["action_id"]
    assert manifest["settings_snapshot"]["interaction_mode"] == "auto"
    assert manifest["preflight"]["pending_action"]["generation"] == recovered[
        "generation"
    ]
    assert transport.calls == 0


def test_total_pixel_limit_blocks_all_pages_before_rendering_any_image(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "many-pages.pdf"
    make_many_page_pdf(source, page_count=30)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    blocker = json.loads((bundle / "manifest.json").read_text())["preflight"][
        "deterministic_blockers"
    ][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "total_pixel_limit_exceeded"
    assert blocker["pages"] == list(range(1, 31))
    assert blocker["observed_total_pixels"] == 252_450_000
    assert blocker["limit_total_pixels"] == 250_000_000
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_page_count_limit_blocks_before_rendering_any_image(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "too-many-pages.pdf"
    make_many_page_pdf(source, page_count=2_001)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    blocker = json.loads((bundle / "manifest.json").read_text())["preflight"][
        "deterministic_blockers"
    ][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker == {
        "code": "page_limit_exceeded",
        "pages": [],
        "evidence": "The PDF exceeds the 2000 page hard limit.",
        "observed_pages": 2_001,
        "limit_pages": 2_000,
    }
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_source_snapshot_limit_preserves_the_source_and_commits_block_evidence(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "source-limit.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    bundle = Path(started["work_bundle"])
    saved_source = (bundle / "01-source" / "source.pdf").read_bytes()
    limit = len(saved_source) - 1
    monkeypatch.setattr(preflight, "MAX_SOURCE_BYTES", limit)
    monkeypatch.setattr(
        preflight,
        "RESOURCE_LIMITS",
        {**preflight.RESOURCE_LIMITS, "max_source_bytes": limit},
    )

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    blocker = json.loads((bundle / "manifest.json").read_text())["preflight"][
        "deterministic_blockers"
    ][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "source_size_limit_exceeded"
    assert blocker["observed_bytes"] == len(saved_source)
    assert blocker["limit_bytes"] == limit
    assert (bundle / "01-source" / "source.pdf").read_bytes() == saved_source
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_inventory_limit_cleans_render_temps_and_commits_block_evidence(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "inventory-limit.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    monkeypatch.setattr(preflight, "MAX_INVENTORY_BYTES", 1)
    monkeypatch.setattr(
        preflight,
        "RESOURCE_LIMITS",
        {**preflight.RESOURCE_LIMITS, "max_inventory_bytes": 1},
    )
    original_get_pixmap = fitz.Page.get_pixmap
    rendered_pages = []

    def reject_render_after_inventory_is_already_too_large(self, *args, **kwargs):
        rendered_pages.append(self.number + 1)
        if self.number > 0:
            raise RuntimeError("the next page must not be rendered")
        return original_get_pixmap(self, *args, **kwargs)

    monkeypatch.setattr(
        fitz.Page,
        "get_pixmap",
        reject_render_after_inventory_is_already_too_large,
    )

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    blocker = json.loads((bundle / "manifest.json").read_text())["preflight"][
        "deterministic_blockers"
    ][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "inventory_limit_exceeded"
    assert blocker["observed_bytes"] > blocker["limit_bytes"]
    assert rendered_pages == []
    assert list((bundle / "02-pages").iterdir()) == []
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_render_disk_budget_blocks_before_writing_page_images(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "disk-budget.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    class NoFreeBlocks:
        f_bavail = 0
        f_frsize = 4096

    monkeypatch.setattr(preflight.os, "fstatvfs", lambda _fd: NoFreeBlocks())
    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    blocker = json.loads((bundle / "manifest.json").read_text())["preflight"][
        "deterministic_blockers"
    ][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "render_disk_limit_exceeded"
    assert list((bundle / "02-pages").iterdir()) == []
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_advance_recovers_after_manifest_commit_before_the_committed_event(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "commit-crash.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_append = preflight.bundle.append_history

    def fail_committed(event, *, state_fd):
        if event.get("event") == "preflight_baseline_committed":
            raise OSError("simulated committed append crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(preflight.bundle, "append_history", fail_committed)
    crash_rc, crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    assert crashed["errors"][0]["code"] == "internal_error"
    bundle = Path(started["work_bundle"])
    manifest_after_crash = (bundle / "manifest.json").read_bytes()
    page_bytes = {
        path.name: path.read_bytes() for path in (bundle / "02-pages").iterdir()
    }
    intent = json.loads(
        (bundle / ".state" / "history.ndjson").read_text().splitlines()[-2]
    )
    monkeypatch.setattr(preflight.bundle, "append_history", original_append)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "preflight_pending"
    assert recovered["action_id"] == intent["action_id"]
    assert (bundle / "manifest.json").read_bytes() == manifest_after_crash
    assert {
        path.name: path.read_bytes() for path in (bundle / "02-pages").iterdir()
    } == page_bytes
    assert history[-1]["event"] == "preflight_baseline_committed"
    assert transport.calls == 0


def test_advance_rejects_source_tampering_before_mutating_a_prepared_recovery(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "prepared-source-tamper.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_atomic_write = preflight.bundle.atomic_write_json

    def fail_private_commit(name, value, *, dir_fd):
        if name == "private.json":
            raise OSError("simulated private state crash")
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(preflight.bundle, "atomic_write_json", fail_private_commit)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(
        preflight.bundle, "atomic_write_json", original_atomic_write
    )

    bundle = Path(started["work_bundle"])
    replacement = tmp_path / "replacement.pdf"
    make_many_page_pdf(replacement, page_count=1)
    bundled_source = bundle / "01-source" / "source.pdf"
    bundled_source.write_bytes(replacement.read_bytes())
    bundled_source.chmod(0o600)
    before = bundle_bytes(bundle)

    retry_rc, rejected, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert retry_rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"
    assert bundle_bytes(bundle) == before
    assert transport.calls == 0


def test_advance_recovers_private_generation_written_before_manifest_commit(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "manifest-crash.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_atomic = preflight.bundle.atomic_write_json

    def fail_manifest(name, value, *, dir_fd):
        if name == "manifest.json":
            raise OSError("simulated manifest commit crash")
        return original_atomic(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(preflight.bundle, "atomic_write_json", fail_manifest)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    bundle = Path(started["work_bundle"])
    assert json.loads((bundle / "manifest.json").read_text())["generation"] == 1
    assert json.loads((bundle / ".state" / "private.json").read_text())[
        "generation"
    ] == 2
    monkeypatch.setattr(preflight.bundle, "atomic_write_json", original_atomic)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "preflight_pending"
    assert json.loads((bundle / "manifest.json").read_text())["generation"] == 2
    assert transport.calls == 0


def test_advance_recovers_a_partially_promoted_page_baseline(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "promotion-crash.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_promote = preflight._promote_private_file
    promotion_count = 0

    def fail_third_promotion(*args, **kwargs):
        nonlocal promotion_count
        promotion_count += 1
        if promotion_count == 3:
            raise OSError("simulated second-page promotion crash")
        return original_promote(*args, **kwargs)

    monkeypatch.setattr(preflight, "_promote_private_file", fail_third_promotion)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    bundle = Path(started["work_bundle"])
    assert (bundle / "01-source" / "source-inventory.json").is_file()
    assert (bundle / "02-pages" / "page-0001.png").is_file()
    assert not (bundle / "02-pages" / "page-0002.png").exists()
    monkeypatch.setattr(preflight, "_promote_private_file", original_promote)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recover_rc == 0
    assert recovered["conversion_state"] == "preflight_pending"
    assert (bundle / "02-pages" / "page-0002.png").is_file()
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_advance_recovers_an_intent_saved_before_the_first_render_write(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "intent-crash.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file
    failed = False

    def fail_first_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(preflight, "_write_private_file", fail_first_write)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    bundle = Path(started["work_bundle"])
    intent = json.loads(
        (bundle / ".state" / "history.ndjson").read_text().splitlines()[-1]
    )
    assert intent["event"] == "preflight_baseline_intent"
    monkeypatch.setattr(preflight, "_write_private_file", original_write)
    before_read_only_commands = bundle_bytes(bundle)

    inspect_rc, inspected, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", started["work_bundle"]],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    resume_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert inspect_rc == 0
    assert inspected["conversion_state"] == "preparing"
    assert resume_rc == 0
    assert resumed["outcome"] == "no_progress"
    assert bundle_bytes(bundle) == before_read_only_commands

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["action_id"] == intent["action_id"]
    assert recovered["conversion_state"] == "preflight_pending"
    assert transport.calls == 0


def test_dependency_identity_drops_diagnostic_detail_only():
    """`detail` 是诊断文本，不是能力事实：投影必须只去掉它，别的一个不动。"""
    modern = [
        {"name": "pymupdf", "available": True, "version": "1.24.0",
         "reason": None, "detail": None, "purpose": "x"},
        {"name": "pandoc", "available": False, "version": None,
         "reason": "not_installed", "detail": "OSError: dlopen failed",
         "purpose": "y"},
    ]
    legacy = [  # v1.0.0 落盘形状：根本没有 detail 键
        {"name": "pymupdf", "available": True, "version": "1.24.0",
         "reason": None, "purpose": "x"},
        {"name": "pandoc", "available": False, "version": None,
         "reason": "not_installed", "purpose": "y"},
    ]

    assert preflight.dependency_identity(modern) == preflight.dependency_identity(legacy)
    # 能力事实变了仍然要判为不同，投影不能把漂移一起吞掉。
    drifted = json.loads(json.dumps(modern))
    drifted[0]["version"] = "1.25.0"
    assert preflight.dependency_identity(drifted) != preflight.dependency_identity(modern)
    # 非列表 / 非字典元素原样返回，由各自的校验分支判定合法性。
    assert preflight.dependency_identity(None) is None
    assert preflight.dependency_identity(["x"]) == ["x"]


def test_every_dependency_record_carries_the_same_key_shape(tmp_path, monkeypatch):
    """四条依赖记录形状一致：读记录的人不必逐条记住谁带 detail 谁不带。"""
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    status = preflight.check_dependencies(
        environ=dependencies, visual_capability="available"
    )
    names = [record["name"] for record in status["dependencies"]]
    assert names == ["pymupdf", "pandoc", "beautifulsoup4", "host_visual"]
    for record in status["dependencies"]:
        assert "detail" in record, record["name"]
        assert {"name", "available", "version", "reason", "detail", "purpose"} <= set(
            record
        ), record["name"]


def test_a_legacy_baseline_intent_without_detail_resumes_without_dependency_drift(
    tmp_path, capsys, monkeypatch
):
    """v1.0.0 时代落盘的 baseline intent 在新版必须照常 resume。

    `detail` 是后加的纯诊断键。历史上的等值比较是整条记录直接比，于是旧 bundle 一
    resume 就必然不等，被误判成 `dependency_drift`（workflow 侧）/
    `integrity_violation`（preflight 侧）——环境明明一点没变，用户却被要求去
    "restore preflight dependencies"，且这个 bundle 再也推进不了。
    """
    source = tmp_path / "legacy-intent.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    original_write = preflight._write_private_file
    failed = False

    def fail_first_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(preflight, "_write_private_file", fail_first_write)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    # 把落盘的 intent 改回 v1.0.0 形状：每条依赖记录都没有 detail 键。
    history_path = Path(started["work_bundle"]) / ".state" / "history.ndjson"
    lines = history_path.read_text().splitlines()
    intent = json.loads(lines[-1])
    assert intent["event"] == "preflight_baseline_intent"
    assert any("detail" in record for record in intent["dependencies"])
    intent["dependencies"] = [
        {key: value for key, value in record.items() if key != "detail"}
        for record in intent["dependencies"]
    ]
    # 用与 bundle.canonical_json_bytes 一致的落盘编码重写，避免额外的形态差异。
    lines[-1] = json.dumps(
        intent, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    history_path.write_text("\n".join(lines) + "\n")

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recover_rc == 0
    assert recovered.get("code") != "dependency_drift"
    assert recovered.get("code") != "integrity_violation"
    assert recovered["generation"] == 2
    assert recovered["action_id"] == intent["action_id"]
    assert recovered["conversion_state"] == "preflight_pending"
    assert transport.calls == 0


def test_history_append_failure_preserves_the_previous_complete_history(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "history-append.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    bundle = Path(started["work_bundle"])
    history_path = bundle / ".state" / "history.ndjson"
    original_history = history_path.read_bytes()
    original_replace = preflight.bundle.os.replace

    def fail_history_replace(source_name, destination_name, *args, **kwargs):
        if destination_name == "history.ndjson":
            raise OSError("simulated history replace interruption")
        return original_replace(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(preflight.bundle.os, "replace", fail_history_replace)
    crash_rc, crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert crash_rc == 1
    assert crashed["errors"][0]["code"] == "internal_error"
    assert history_path.read_bytes() == original_history
    assert not list((bundle / ".state").glob(".history.ndjson.*"))
    assert list((bundle / "02-pages").iterdir()) == []

    monkeypatch.setattr(preflight.bundle.os, "replace", original_replace)
    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert recover_rc == 0
    assert recovered["conversion_state"] == "preflight_pending"
    assert transport.calls == 0


def test_advance_rejects_a_tampered_baseline_temp_path_without_touching_the_source(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "tampered-temp-path.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file
    failed = False

    def crash_before_first_render_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        preflight, "_write_private_file", crash_before_first_render_write
    )
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    bundle = Path(started["work_bundle"])
    history_path = bundle / ".state" / "history.ndjson"
    events = [json.loads(line) for line in history_path.read_text().splitlines()]
    events[-1]["pages"][0]["temporary_name"] = "../01-source/source.pdf"
    history_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    history_path.chmod(0o600)
    source_before = (bundle / "01-source" / "source.pdf").read_bytes()
    bundle_before = bundle_bytes(bundle)

    retry_rc, rejected, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert retry_rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"
    assert (bundle / "01-source" / "source.pdf").read_bytes() == source_before
    assert bundle_bytes(bundle) == bundle_before
    assert transport.calls == 0


def test_advance_rechecks_dependencies_before_recovering_a_baseline_intent(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "dependency-recovery.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file
    failed = False

    def crash_before_first_render_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        preflight, "_write_private_file", crash_before_first_render_write
    )
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    missing_rc, missing, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "unavailable",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert missing_rc == 0
    assert missing["generation"] == 2
    assert missing["conversion_state"] == "recoverable_error"
    assert missing["outcome"] == "dependency_missing"
    assert missing["missing_dependencies"] == ["pandoc", "host_visual"]
    assert history[-4]["event"] == "preflight_baseline_aborted"
    assert history[-4]["reason_code"] == "dependency_missing"
    assert history[-3]["event"] == "preflight_dependency_intent"
    assert history[-2]["event"] == "preflight_dependency_prepared"
    assert history[-1]["event"] == "preflight_dependency_committed"
    assert not (bundle / "01-source" / "source-inventory.json").exists()
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_advance_reports_when_recovery_finishes_a_different_render_request(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "render-request-mismatch.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file
    failed = False

    def crash_before_first_render_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        preflight, "_write_private_file", crash_before_first_render_write
    )
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    mismatch_rc, mismatch, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
            "--render-dpi",
            "600",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert mismatch_rc == 5
    assert mismatch["errors"][0]["code"] == "recovered_request_mismatch"
    assert mismatch["generation"] == 2
    assert mismatch["conversion_state"] == "preflight_pending"
    assert manifest["preflight"]["render_dpi"] == 300
    assert transport.calls == 0


def test_recovered_baseline_render_failure_becomes_a_durable_block(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "recovered-render-failure.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    original_write = preflight._write_private_file
    failed = False

    def crash_before_first_render_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated render output crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        preflight, "_write_private_file", crash_before_first_render_write
    )
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    original_get_pixmap = fitz.Page.get_pixmap

    def fail_first_page(self, *args, **kwargs):
        if self.number == 0:
            raise RuntimeError("simulated incomplete render during recovery")
        return original_get_pixmap(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", fail_first_page)
    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert block_rc == 0
    assert blocked["generation"] == 2
    assert blocked["conversion_state"] == "preflight_blocked"
    assert manifest["preflight"]["deterministic_blockers"][0]["code"] == (
        "render_failed"
    )
    assert manifest["preflight"]["deterministic_blockers"][0]["pages"] == [1]
    assert history[-4]["event"] == "preflight_baseline_aborted"
    assert history[-3]["event"] == "preflight_block_intent"
    assert history[-2]["event"] == "preflight_block_prepared"
    assert history[-1]["event"] == "preflight_block_committed"
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_aborted_baseline_is_completed_after_a_crash_before_block_commit(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "aborted-before-block.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_get_pixmap = fitz.Page.get_pixmap
    original_commit_block = preflight.commit_deterministic_block
    failed = False

    def fail_render(self, *args, **kwargs):
        if self.number == 0:
            raise RuntimeError("simulated incomplete render")
        return original_get_pixmap(self, *args, **kwargs)

    def fail_first_block_commit(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated block commit interruption")
        return original_commit_block(*args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", fail_render)
    monkeypatch.setattr(
        preflight, "commit_deterministic_block", fail_first_block_commit
    )
    crash_rc, crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    assert crashed["errors"][0]["code"] == "internal_error"
    bundle = Path(started["work_bundle"])
    history_path = bundle / ".state" / "history.ndjson"
    assert json.loads(history_path.read_text().splitlines()[-1])["event"] == (
        "preflight_baseline_aborted"
    )

    monkeypatch.setattr(fitz.Page, "get_pixmap", original_get_pixmap)
    monkeypatch.setattr(
        preflight, "commit_deterministic_block", original_commit_block
    )
    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    history = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "preflight_blocked"
    assert history[-1]["event"] == "preflight_block_committed"
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_aborted_baseline_dependency_gate_is_completed_on_the_next_run(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "aborted-dependency-gate.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file
    failed_write = False

    def stop_after_intent(*args, **kwargs):
        nonlocal failed_write
        if not failed_write:
            failed_write = True
            raise OSError("simulated first render write interruption")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(preflight, "_write_private_file", stop_after_intent)
    intent_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert intent_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)
    original_commit_dependency = preflight.commit_dependency_missing
    failed_dependency = False

    def stop_dependency_commit(*args, **kwargs):
        nonlocal failed_dependency
        if not failed_dependency:
            failed_dependency = True
            raise OSError("simulated dependency state interruption")
        return original_commit_dependency(*args, **kwargs)

    monkeypatch.setattr(
        preflight, "commit_dependency_missing", stop_dependency_commit
    )
    abort_rc, _aborted, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert abort_rc == 1
    bundle = Path(started["work_bundle"])
    aborted_event = json.loads(
        (bundle / ".state" / "history.ndjson").read_text().splitlines()[-1]
    )
    assert aborted_event["event"] == "preflight_baseline_aborted"
    assert aborted_event["reason_code"] == "dependency_missing"

    monkeypatch.setattr(
        preflight, "commit_dependency_missing", original_commit_dependency
    )
    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={"PATH": ""},
        transport=transport,
    )
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "recoverable_error"
    assert recovered["missing_dependencies"] == ["pandoc"]
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_advance_adopts_complete_temp_artifacts_when_prepared_append_crashes(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "prepared-crash.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_append = preflight.bundle.append_history

    def fail_prepared(event, *, state_fd):
        if event.get("event") == "preflight_baseline_prepared":
            raise OSError("simulated prepared append crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(preflight.bundle, "append_history", fail_prepared)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    bundle = Path(started["work_bundle"])
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    intent = history[-1]
    inventory_temp = bundle / "01-source" / intent["inventory_temporary_name"]
    page_temp_inodes = {
        page["final_name"]: (
            bundle / "02-pages" / page["temporary_name"]
        ).stat().st_ino
        for page in intent["pages"]
    }
    inventory_inode = inventory_temp.stat().st_ino
    monkeypatch.setattr(preflight.bundle, "append_history", original_append)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert recover_rc == 0
    assert recovered["action_id"] == intent["action_id"]
    assert (bundle / "01-source" / "source-inventory.json").stat().st_ino == (
        inventory_inode
    )
    assert {
        name: (bundle / "02-pages" / name).stat().st_ino
        for name in page_temp_inodes
    } == page_temp_inodes
    assert transport.calls == 0


def test_advance_rebuilds_an_unprepared_partial_inventory_temp(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "partial-inventory.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file

    def partially_write_inventory(name, data, *, dir_fd):
        if name.endswith("source-inventory.json.part"):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, data[:32])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(dir_fd)
            raise OSError("simulated partial inventory write")
        return original_write(name, data, dir_fd=dir_fd)

    monkeypatch.setattr(
        preflight, "_write_private_file", partially_write_inventory
    )
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    inventory = json.loads(
        (bundle / "01-source" / "source-inventory.json").read_text()
    )
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "preflight_pending"
    assert inventory["page_count"] == 2
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_record_preflight_replays_success_after_commit_before_committed_history(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="pass",
        pages=[
            {
                "page_number": 1,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page one content is readable."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page two content is readable."],
            },
        ],
    )
    original_append = preflight.bundle.append_history

    def fail_committed(event, *, state_fd):
        if event.get("event") == "preflight_record_committed":
            raise OSError("simulated record committed crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(preflight.bundle, "append_history", fail_committed)
    argv = [
        "record",
        "preflight",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(advanced["generation"]),
        "--action-id",
        advanced["action_id"],
        "--evidence-hash",
        advanced["evidence_hash"],
        "--input",
        str(record),
    ]
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    manifest_after_crash = (bundle / "manifest.json").read_bytes()
    preflight_after_crash = (bundle / "04-review" / "preflight.json").read_bytes()
    monkeypatch.setattr(preflight.bundle, "append_history", original_append)

    replay_rc, replayed, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert replay_rc == 0
    assert replayed["generation"] == advanced["generation"] + 1
    assert replayed["conversion_state"] == "ready_to_submit"
    assert replayed["outcome"] == "preflight_recorded"
    assert (bundle / "manifest.json").read_bytes() == manifest_after_crash
    assert (bundle / "04-review" / "preflight.json").read_bytes() == (
        preflight_after_crash
    )
    assert transport.calls == 0


def test_record_preflight_recovers_an_intent_saved_before_payload_temp(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "risk",
                "risk_codes": ["complex_multicolumn"],
                "evidence": ["Multiple reading columns are visible."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page two content is readable."],
            },
        ],
    )
    argv = [
        "record",
        "preflight",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(advanced["generation"]),
        "--action-id",
        advanced["action_id"],
        "--evidence-hash",
        advanced["evidence_hash"],
        "--input",
        str(record),
    ]
    original_write = preflight._write_private_file
    failed = False

    def fail_payload_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated record payload write crash")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(preflight, "_write_private_file", fail_payload_write)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    intent = json.loads(
        (bundle / ".state" / "history.ndjson").read_text().splitlines()[-1]
    )
    assert intent["event"] == "preflight_record_intent"
    monkeypatch.setattr(preflight, "_write_private_file", original_write)

    replay_rc, replayed, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert replay_rc == 0
    assert replayed["conversion_state"] == "preflight_warning"
    assert replayed["action_id"] == intent["next_action_id"]
    assert (bundle / "04-review" / "preflight.json").is_file()
    assert transport.calls == 0


def test_decision_recovery_does_not_report_a_different_replay_as_applied(
    tmp_path, capsys, monkeypatch
):
    bundle, warning, dependencies, transport = prepare_confirm_warning(
        tmp_path, capsys, monkeypatch
    )
    original_append = preflight.bundle.append_history

    def fail_prepared(event, *, state_fd):
        if event.get("event") == "preflight_decision_prepared":
            raise OSError("simulated decision preparation interruption")
        return original_append(event, state_fd=state_fd)

    accept_argv = [
        "record",
        "decision",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(warning["generation"]),
        "--action-id",
        warning["action_id"],
        "--evidence-hash",
        warning["evidence_hash"],
        "--decision",
        "accept",
        "--basis",
        "The documented risks are accepted.",
    ]
    monkeypatch.setattr(preflight.bundle, "append_history", fail_prepared)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        accept_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight.bundle, "append_history", original_append)

    different_argv = list(accept_argv)
    different_argv[different_argv.index("accept")] = "decline"
    different_argv[different_argv.index("The documented risks are accepted.")] = (
        "The documented risks are declined."
    )
    replay_rc, replayed, _stderr = invoke(
        capsys,
        different_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert replay_rc == 5
    assert replayed["errors"][0]["code"] == "recovered_request_mismatch"
    assert manifest["conversion_state"] == "ready_to_submit"
    assert manifest["preflight"]["decision"]["status"] == "accepted"
    assert transport.calls == 0


def test_deterministic_block_recovers_an_intent_before_its_prepared_event(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "block-intent.pdf"
    source.write_bytes(ZERO_PAGE_PDF)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_append = preflight.bundle.append_history

    def fail_prepared(event, *, state_fd):
        if event.get("event") == "preflight_block_prepared":
            raise OSError("simulated block preparation interruption")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(preflight.bundle, "append_history", fail_prepared)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    monkeypatch.setattr(preflight.bundle, "append_history", original_append)

    recover_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    bundle = Path(started["work_bundle"])
    assert recover_rc == 0
    assert recovered["generation"] == 2
    assert recovered["conversion_state"] == "preflight_blocked"
    assert (bundle / "04-review" / "preflight.json").is_file()
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_record_recovery_rejects_a_prepared_action_that_no_longer_matches(
    tmp_path, capsys, monkeypatch
):
    bundle, advanced, dependencies, transport = prepare_baseline(
        tmp_path, capsys, monkeypatch
    )
    record = write_preflight_record(
        tmp_path,
        summary="pass",
        pages=[
            {
                "page_number": 1,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page one content is readable."],
            },
            {
                "page_number": 2,
                "classification": "content",
                "risk_codes": [],
                "evidence": ["Page two content is readable."],
            },
        ],
    )
    original_promote = preflight._promote_private_file

    def stop_before_promotion(*_args, **_kwargs):
        raise OSError("simulated promotion interruption")

    monkeypatch.setattr(preflight, "_promote_private_file", stop_before_promotion)
    crash_rc, _crashed, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert crash_rc == 1
    history_path = bundle / ".state" / "history.ndjson"
    history = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert history[-1]["event"] == "preflight_record_prepared"
    history[-2]["action_id"] = "preflight-" + "0" * 32
    history_path.write_text(
        "".join(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in history)
    )
    monkeypatch.setattr(preflight, "_promote_private_file", original_promote)
    before_manifest = (bundle / "manifest.json").read_bytes()

    replay_rc, replayed, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            "preflight-" + "0" * 32,
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    assert replay_rc == 4
    assert replayed["errors"][0]["code"] == "integrity_violation"
    assert (bundle / "manifest.json").read_bytes() == before_manifest
    assert not (bundle / "04-review" / "preflight.json").exists()
    assert transport.calls == 0


def test_page_render_failure_is_durably_blocked_and_does_not_leave_temp_outputs(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "render-failure.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_get_pixmap = fitz.Page.get_pixmap

    def fail_first_page(self, *args, **kwargs):
        if self.number == 0:
            raise RuntimeError("simulated incomplete render")
        return original_get_pixmap(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", fail_first_page)
    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert manifest["preflight"]["deterministic_blockers"][0]["code"] == (
        "render_failed"
    )
    assert manifest["preflight"]["deterministic_blockers"][0]["pages"] == [1]
    assert any(event["event"] == "preflight_baseline_aborted" for event in history)
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_missing_staged_page_is_durably_blocked_before_baseline_commit(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "missing-staged-page.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_write = preflight._write_private_file

    def omit_second_page(name, data, *, dir_fd):
        if name.endswith("page-0002.png.part"):
            return
        return original_write(name, data, dir_fd=dir_fd)

    monkeypatch.setattr(preflight, "_write_private_file", omit_second_page)

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "page_reference_count_mismatch"
    assert blocker["pages"] == [2]
    assert list((bundle / "02-pages").iterdir()) == []
    assert not any(path.name.endswith(".part") for path in bundle.rglob("*"))
    assert transport.calls == 0


def test_discontinuous_page_plan_is_durably_blocked_before_rendering(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "discontinuous-page-plan.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_plan = preflight._page_plan

    def discontinuous_plan(document, *, dpi):
        plan = original_plan(document, dpi=dpi)
        plan[1]["page_number"] = 3
        plan[1]["final_name"] = "page-0003.png"
        return plan

    monkeypatch.setattr(preflight, "_page_plan", discontinuous_plan)

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "page_reference_numbering_mismatch"
    assert blocker["pages"] == [2]
    assert blocker["observed_page_numbers"] == [1, 3]
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_page_load_failure_is_durably_blocked_before_rendering_any_image(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "page-load-failure.pdf"
    make_structured_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    original_load_page = fitz.Document.load_page

    def fail_second_page(self, page_id):
        if page_id == 1:
            raise RuntimeError("simulated page load failure")
        return original_load_page(self, page_id)

    monkeypatch.setattr(fitz.Document, "load_page", fail_second_page)
    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    bundle = Path(started["work_bundle"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "render_failed"
    assert blocker["pages"] == [2]
    assert list((bundle / "02-pages").iterdir()) == []
    assert transport.calls == 0


def test_mupdf_page_warning_blocks_a_render_that_returns_incomplete_pixels(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "invalid-content.pdf"
    make_invalid_content_stream_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0

    block_rc, blocked, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    manifest = json.loads(
        (Path(started["work_bundle"]) / "manifest.json").read_text()
    )
    blocker = manifest["preflight"]["deterministic_blockers"][0]
    assert block_rc == 0
    assert blocked["conversion_state"] == "preflight_blocked"
    assert blocker["code"] == "render_failed"
    assert blocker["pages"] == [1]
    assert any("page may not be correct" in item for item in blocker["mupdf_warnings"])
    assert transport.calls == 0


def test_real_visual_risk_pages_produce_evidence_bound_warning_records(
    tmp_path, capsys, monkeypatch
):
    source = tmp_path / "visual-risks.pdf"
    make_visual_risk_pdf(source)
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)
    transport = NeverNetwork()
    start_rc, started, _stderr = invoke(
        capsys,
        ["start", "--source", str(source)],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert start_rc == 0
    advance_rc, advanced, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            started["work_bundle"],
            "--expected-generation",
            "1",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    assert advance_rc == 0
    bundle = Path(started["work_bundle"])
    inventory = json.loads(
        (bundle / "01-source" / "source-inventory.json").read_text()
    )
    assert inventory["pages"][0]["text_blocks"] == []
    assert inventory["pages"][0]["images"] == []
    assert inventory["pages"][1]["images"][0]["width"] == 8
    assert inventory["pages"][1]["images"][0]["height"] == 8
    assert inventory["pages"][2]["rotation"] == 90
    assert len(inventory["pages"][2]["text_blocks"]) >= 2
    assert inventory["pages"][3]["text_blocks"]
    assert inventory["pages"][3]["images"]
    assert inventory["pages"][3]["drawing_count"] > 0
    assert inventory["pages"][4]["drawing_count"] > 0

    record = write_preflight_record(
        tmp_path,
        summary="warning",
        pages=[
            {
                "page_number": 1,
                "classification": "blank",
                "risk_codes": [],
                "evidence": ["The full rendered page is blank."],
            },
            {
                "page_number": 2,
                "classification": "risk",
                "risk_codes": ["scanned_content", "low_resolution", "blurred_content"],
                "evidence": ["An 8 by 8 pixel scan is enlarged to the full page."],
            },
            {
                "page_number": 3,
                "classification": "risk",
                "risk_codes": ["abnormal_rotation", "complex_multicolumn"],
                "evidence": ["The page is rotated and contains two reading columns."],
            },
            {
                "page_number": 4,
                "classification": "risk",
                "risk_codes": ["mixed_text_images", "cross_page_table"],
                "evidence": ["Text, an image, and a table continuing at the page edge are visible."],
            },
            {
                "page_number": 5,
                "classification": "risk",
                "risk_codes": ["cross_page_table"],
                "evidence": ["The table continues from the preceding page."],
            },
        ],
    )
    warning_rc, warning, _stderr = invoke(
        capsys,
        [
            "record",
            "preflight",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(advanced["generation"]),
            "--action-id",
            advanced["action_id"],
            "--evidence-hash",
            advanced["evidence_hash"],
            "--input",
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )
    saved = json.loads((bundle / "04-review" / "preflight.json").read_text())
    assert warning_rc == 0
    assert warning["conversion_state"] == "preflight_warning"
    assert [page["page_reference"] for page in saved["pages"]] == [
        f"02-pages/page-{number:04d}.png" for number in range(1, 6)
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", page["image_sha256"]) for page in saved["pages"])
    assert all(page["pixel_width"] > 0 and page["pixel_height"] > 0 for page in saved["pages"])
    assert transport.calls == 0
