import base64
import io
import hashlib
import json
import os
import shlex
import shutil
from pathlib import Path

import pytest
import correction as correction_module
import raw_conversion as raw_conversion_module
import page_crop as page_crop_module
import review as review_module
import workflow as workflow_module

import test_raw_conversion as raw_test


INSTALL_PREFLIGHT_DEPENDENCIES = raw_test.install_preflight_dependencies
SYSTEM_PANDOC = shutil.which("pandoc")


CHECK_CATEGORIES = [
    "text",
    "hierarchy",
    "reading_order",
    "tables",
    "formulas",
    "footnotes",
    "links",
    "captions",
    "images",
]

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class SimulatedProcessCrash(BaseException):
    pass


def install_review_dependencies(tmp_path, monkeypatch):
    dependencies = INSTALL_PREFLIGHT_DEPENDENCIES(tmp_path, monkeypatch)
    pandoc = Path(dependencies["PATH"]) / "pandoc"
    pandoc.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('pandoc 3.8.2')\n"
        "    raise SystemExit(0)\n"
        "source = sys.stdin.read()\n"
        "blocks = []\n"
        "if source:\n"
        "    parts = source.split('\\n<!-- review-block -->\\n')\n"
        "    line = 1\n"
        "    for part in parts:\n"
        "        lines = part.splitlines() or ['']\n"
        "        end_line = line + len(lines) - 1\n"
        "        end = f'{end_line}:{max(1, len(lines[-1]) + 1)}'\n"
        "        position = f'{line}:1-{end}'\n"
        "        if '<!-- compound-sourcepos -->' in part:\n"
        "            position += f';{line}:1-{line}:2'\n"
        "        attr = ['', [], [['data-pos', position]]]\n"
        "        blocks.append({'t': 'Div', 'c': [attr, ["
        "{'t': 'Para', 'c': [{'t': 'Str', 'c': part.strip()}]}]]})\n"
        "        line = end_line + 2\n"
        "print(json.dumps({'pandoc-api-version': [1, 23, 1], "
        "'meta': {}, 'blocks': blocks}))\n"
    )
    pandoc.chmod(0o700)
    return dependencies


def install_real_pandoc_review_dependencies(tmp_path, monkeypatch):
    if SYSTEM_PANDOC is None:
        pytest.skip("The resource-oracle contract test requires Pandoc.")
    dependencies = INSTALL_PREFLIGHT_DEPENDENCIES(tmp_path, monkeypatch)
    pandoc = Path(dependencies["PATH"]) / "pandoc"
    pandoc.write_text(
        "#!/bin/sh\nexec " + shlex.quote(SYSTEM_PANDOC) + ' "$@"\n'
    )
    pandoc.chmod(0o700)
    return dependencies


def converted_bundle(
    tmp_path,
    capsys,
    monkeypatch,
    *,
    page_count=1,
    markdown=None,
    assets=None,
    interaction_mode="confirm",
    dependency_installer=install_review_dependencies,
):
    monkeypatch.setattr(
        raw_test, "install_preflight_dependencies", dependency_installer
    )
    bundle, ready, dependencies, key, _result_url = raw_test.ready_result_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        page_count=page_count,
        interaction_mode=interaction_mode,
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    request_filename = manifest["conversion_attempts"][-1]["request_summary"][
        "filename"
    ]
    markdown = b"# Converted\n\nRaw body.\n" if markdown is None else markdown
    archive = raw_test.make_zip(
        [
            (f"nested/{request_filename}.md", markdown),
            *([] if assets is None else list(assets)),
        ]
    )
    rc, converted, stderr = raw_test.invoke(
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
        transport=raw_test.ArchiveTransport(archive),
    )
    assert rc == 0, (converted, stderr)
    assert converted["conversion_state"] == "converted"
    return bundle, converted, dependencies


def passing_checks():
    return [
        {
            "category": category,
            "status": "pass",
            "evidence": [f"Verified {category} against the source pages."],
            "finding_ids": [],
        }
        for category in CHECK_CATEGORIES
    ]


def correction_required_bundle(
    tmp_path,
    capsys,
    monkeypatch,
    *,
    markdown=b"# Converted\n\nRaw body.\n",
    page_count=1,
    finding_block="block-000001",
    finding_page=1,
    finding_category="text",
    assets=None,
    dependency_installer=install_review_dependencies,
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        page_count=page_count,
        assets=assets,
        dependency_installer=dependency_installer,
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    segments = []
    for index in range(1, page_count + 1):
        checks = passing_checks()
        if f"block-{index:06d}" == finding_block:
            next(item for item in checks if item["category"] == finding_category).update(
                {
                    "status": "difference",
                    "evidence": ["The source page differs."],
                    "finding_ids": ["finding-0001"],
                }
            )
        segments.append(
            {
                "segment_id": f"segment-{index:04d}",
                "source_pages": {"start": index, "end": index},
                "markdown_blocks": [f"block-{index:06d}"],
                "checks": checks,
            }
        )
    boundaries = [
        {
            "before_segment_id": f"segment-{index:04d}",
            "after_segment_id": f"segment-{index + 1:04d}",
            "status": "pass",
            "evidence": ["The adjacent segments remain continuous."],
            "finding_ids": [],
        }
        for index in range(1, page_count)
    ]
    review_input = tmp_path / "correction-required-helper.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "correction_required",
                "segments": segments,
                "boundaries": boundaries,
                "findings": [
                    {
                        "finding_id": "finding-0001",
                        "kind": "difference",
                        "category": finding_category,
                        "source_pages": {"start": finding_page, "end": finding_page},
                        "markdown_blocks": [finding_block],
                        "summary": "The converted text differs from the source.",
                        "evidence": ["The page reference contains the source wording."],
                        "action": "correct_markdown",
                        "status": "open",
                    }
                ],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, required, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (required, stderr)
    return bundle, required, dependencies


def ambiguity_required_bundle(tmp_path, capsys, monkeypatch):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    checks = passing_checks()
    next(item for item in checks if item["category"] == "text").update(
        {
            "status": "ambiguous",
            "evidence": ["The source glyph can reasonably be read as 1 or I."],
            "finding_ids": ["finding-0001"],
        }
    )
    review_input = tmp_path / "ambiguity-required-helper.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "review_ambiguity",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": checks,
                    }
                ],
                "boundaries": [],
                "findings": [
                    {
                        "finding_id": "finding-0001",
                        "kind": "ambiguity",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "summary": "One source glyph has two reasonable readings.",
                        "evidence": ["The page reference does not disambiguate the glyph."],
                        "action": "user_decision_required",
                        "status": "unresolved",
                    }
                ],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, ambiguous, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (ambiguous, stderr)
    return bundle, converted, ambiguous, dependencies


def simple_correction_payload(*, original=b"Raw body.", replacement="Source body."):
    start = 13
    return {
        "schema_version": 1,
        "corrections": [
            {
                "correction_id": "correction-item-0001",
                "finding_id": "finding-0001",
                "category": "text",
                "source_pages": {"start": 1, "end": 1},
                "markdown_blocks": ["block-000001"],
                "review_segment_ids": ["segment-0001"],
                "affected_boundaries": [],
                "anchor": {
                    "start_byte": start,
                    "end_byte": start + len(original),
                    "expected_text": original.decode(),
                    "expected_sha256": "sha256:" + hashlib.sha256(original).hexdigest(),
                },
                "replacement": replacement,
                "basis": ["Page 1 visibly contains the corrected wording."],
                "representation": "markdown",
                "fallback": None,
            }
        ],
    }


def lossless_crop_payload(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    page = manifest["preflight"]["page_references"][0]
    source_markdown = (bundle / manifest["raw_conversion"]["main_markdown_path"]).read_bytes()
    anchor_start = source_markdown.index(b"Raw body.")
    payload = simple_correction_payload(replacement=None)
    item = payload["corrections"][0]
    item["anchor"].update(
        {
            "start_byte": anchor_start,
            "end_byte": anchor_start + len(b"Raw body."),
        }
    )
    item["category"] = "images"
    item["representation"] = "lossless_crop"
    item["fallback"] = {
        "markdown": {
            "status": "insufficient",
            "reasons": ["The spatial relationships cannot be represented faithfully."],
        },
        "html": {
            "status": "insufficient",
            "reasons": ["Safe HTML cannot preserve the source geometry."],
        },
        "visual_object": {
            "content_class": "visual_object",
            "kind": "diagram",
            "reasons": ["The source region is a visual diagram rather than editable body text."],
        },
    }
    item["crop"] = {
        "page_number": 1,
        "coordinate_space": "page-png-pixels-v1",
        "bbox": {
            "x0": 0,
            "y0": 0,
            "x1": min(64, page["pixel_width"] - 1),
            "y1": min(64, page["pixel_height"] - 1),
        },
        "whole_page_visual_object": False,
        "alt_text": "Source diagram",
    }
    return payload


def test_review_staging_fsyncs_its_parent_directory(tmp_path, monkeypatch):
    review_dir = tmp_path / "04-review"
    review_dir.mkdir()
    review_descriptor = os.open(review_dir, os.O_RDONLY)
    original_fsync = review_module.os.fsync
    synced_descriptors = []

    def record_fsync(descriptor):
        synced_descriptors.append(descriptor)
        return original_fsync(descriptor)

    monkeypatch.setattr(review_module.os, "fsync", record_fsync)
    try:
        review_module._write_exclusive(
            ".review.part", b"review\n", dir_fd=review_descriptor
        )
    finally:
        os.close(review_descriptor)

    assert len(synced_descriptors) == 2
    assert synced_descriptors[-1] == review_descriptor


def test_review_and_correction_artifact_budgets_are_checked_before_commit(
    tmp_path, monkeypatch
):
    value = {"schema_version": 1, "value": "bounded"}
    serialized = review_module._json_bytes(value)
    monkeypatch.setattr(review_module, "MAX_RECORD_BYTES", len(serialized))
    assert review_module._bounded_json_bytes(value, artifact="test") == serialized
    monkeypatch.setattr(review_module, "MAX_RECORD_BYTES", len(serialized) - 1)
    with pytest.raises(review_module.ReviewError) as review_error:
        review_module._bounded_json_bytes(value, artifact="test")
    assert review_error.value.code == "review_size_limit"

    monkeypatch.setattr(review_module, "MAX_CORRECTION_ARTIFACT_BYTES", 10)
    with pytest.raises(correction_module.CorrectionError) as aggregate_error:
        review_module._validate_correction_artifact_budget(
            {"artifact": b"12345"}, source_markdown=b"123456"
        )
    assert aggregate_error.value.code == "correction_size_limit"

    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    history = state_dir / "history.ndjson"
    history.write_bytes(b"{}\n")
    history.chmod(0o600)
    state_descriptor = os.open(state_dir, os.O_RDONLY)
    event = {"schema_version": 1, "event": "test"}
    # Derive the threshold with the same encoder the code under test now uses
    # (bundle.canonical_json_bytes, which is also what bundle.append_history
    # writes). review._json_bytes happens to produce identical bytes today,
    # but deriving the expectation from a second implementation of the same
    # encoding is exactly the accounting/writer split this work removes.
    maximum = (
        len(history.read_bytes())
        + len(review_module.bundle.canonical_json_bytes(event))
        - 1
    )
    monkeypatch.setattr(review_module.bundle, "MAX_STATE_BYTES", maximum)
    try:
        with pytest.raises(correction_module.CorrectionError) as history_error:
            review_module._validate_history_budget([event], state_fd=state_descriptor)
    finally:
        os.close(state_descriptor)
    assert history_error.value.code == "correction_size_limit"


def test_validate_history_budget_uses_shared_canonical_encoder(tmp_path, monkeypatch):
    # review._validate_history_budget pre-checks a batch of events before
    # they are appended one-by-one via bundle.append_history, which persists
    # each event as bundle.canonical_json_bytes(event). _validate_history_budget
    # used to separately re-implement that same encoding via review._json_bytes
    # -- a second, independently maintained copy of the writer's exact
    # encoding parameters. The two encoders' parameters happen to match
    # byte-for-byte today, so there was no live bug, but nothing pinned them
    # together: a future edit to either encoder's parameters could silently
    # drift the pre-check away from what actually lands on disk. This pins
    # _validate_history_budget to delegate its byte accounting to
    # bundle.canonical_json_bytes itself (the same function bundle.append_history
    # uses), not a parallel implementation, by spying on the shared encoder and
    # asserting it is actually invoked for every event being budgeted.
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    history = state_dir / "history.ndjson"
    history.write_bytes(b"{}\n")
    history.chmod(0o600)
    state_descriptor = os.open(state_dir, os.O_RDONLY)
    events = [
        {"schema_version": 1, "event": "first"},
        {"schema_version": 1, "event": "second"},
    ]
    calls = []
    original_encoder = review_module.bundle.canonical_json_bytes

    def spy(value):
        calls.append(value)
        return original_encoder(value)

    monkeypatch.setattr(review_module.bundle, "canonical_json_bytes", spy)
    try:
        review_module._validate_history_budget(events, state_fd=state_descriptor)
    finally:
        os.close(state_descriptor)

    assert calls == events


def test_review_artifact_removed_during_read_is_an_integrity_error(
    tmp_path, monkeypatch
):
    review_dir = tmp_path / "04-review"
    review_dir.mkdir()
    artifact = review_dir / "evidence.json"
    artifact.write_text("{}\n")
    artifact.chmod(0o600)
    review_descriptor = os.open(review_dir, os.O_RDONLY)
    original_stat = review_module.os.stat
    matching_calls = 0

    def remove_before_final_identity_check(path, *args, **kwargs):
        nonlocal matching_calls
        if path == artifact.name and kwargs.get("dir_fd") == review_descriptor:
            matching_calls += 1
            if matching_calls == 2:
                os.unlink(path, dir_fd=review_descriptor)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(review_module.os, "stat", remove_before_final_identity_check)
    try:
        with pytest.raises(review_module.ReviewError) as raised:
            review_module._read_regular_file(
                artifact.name,
                dir_fd=review_descriptor,
                max_bytes=1024,
            )
    finally:
        os.close(review_descriptor)

    assert raised.value.code == "integrity_violation"


@pytest.mark.parametrize(
    ("loader", "error_type", "error_code"),
    [
        (review_module.load_record_input, review_module.ReviewError, "invalid_review_record"),
        (
            correction_module.load_record_input,
            correction_module.CorrectionError,
            "invalid_correction_record",
        ),
    ],
)
def test_external_record_fifo_is_rejected_without_blocking(
    tmp_path, loader, error_type, error_code
):
    record = tmp_path / "record.fifo"
    os.mkfifo(record, 0o600)

    with pytest.raises(error_type) as raised:
        loader(record, cwd=tmp_path)

    assert raised.value.code == error_code


@pytest.mark.parametrize("reader", ["review", "page_png"])
def test_internal_regular_file_reader_rejects_fifo_without_blocking(tmp_path, reader):
    directory = tmp_path / "private"
    directory.mkdir()
    fifo = directory / "artifact.fifo"
    os.mkfifo(fifo, 0o600)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        if reader == "review":
            with pytest.raises(review_module.ReviewError) as raised:
                review_module._read_regular_file(
                    fifo.name, dir_fd=descriptor, max_bytes=1024
                )
        else:
            with pytest.raises(page_crop_module.PageCropError) as raised:
                page_crop_module._read_private_png(
                    fifo.name, dir_fd=descriptor, max_bytes=1024
                )
    finally:
        os.close(descriptor)

    assert raised.value.code == "integrity_violation"


def test_one_page_review_promotes_the_raw_identity_without_copying_markdown(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )

    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (pending, stderr)
    assert pending["outcome"] == "review_pending"
    assert pending["conversion_state"] == "review_pending"
    assert pending["action_required"] == "record_review"
    assert pending["publication_state"] == converted["publication_state"]
    evidence_path = bundle / pending["artifacts"]["review_evidence"]
    evidence = json.loads(evidence_path.read_text())
    assert [block["block_id"] for block in evidence["markdown_blocks"]] == [
        "block-000001"
    ]

    review_input = tmp_path / "review-input.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (completed, stderr)
    assert completed["outcome"] == "local_complete"
    assert completed["conversion_state"] == "local_complete"
    assert completed["publication_state"] == converted["publication_state"]
    assert completed["target_dialect"] == "gfm+github-dollar-math"
    assert completed["review_coverage"] == {
        "source_pages": {"covered": 1, "required": 1, "complete": True},
        "markdown_blocks": {"covered": 1, "required": 1, "complete": True},
        "boundaries": {"covered": 0, "required": 0, "complete": True},
    }
    manifest = json.loads((bundle / "manifest.json").read_text())
    raw = manifest["raw_conversion"]
    final = manifest["final_markdown"]
    assert final["kind"] == "raw_conversion"
    assert final["attempt_id"] == raw["attempt_id"]
    assert final["path"] == raw["main_markdown_path"]
    assert final["sha256"] == raw["main_markdown_sha256"]
    assert final["size_bytes"] > 0
    assert final["dialect"] == "gfm+github-dollar-math"
    assert final["semantic_hash"].startswith("sha256:")
    assert final["review_round_id"] == "review-round-0001"
    assert final["review_sha256"].startswith("sha256:")
    assert final["review_evidence_sha256"].startswith("sha256:")
    assert not list((bundle / "04-review").glob("*.corrected.md"))
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    report = bundle / "04-review" / "review-report.md"
    assert report.is_file()
    assert completed["artifacts"]["review_report"] == "04-review/review-report.md"
    assert review["status"] == "local_complete"
    assert [item["round_id"] for item in review["rounds"]] == [
        "review-round-0001"
    ]
    assert review["coverage"] == {
        "source_pages": {"covered": 1, "required": 1, "complete": True},
        "markdown_blocks": {"covered": 1, "required": 1, "complete": True},
        "boundaries": {"covered": 0, "required": 0, "complete": True},
    }
    review_before = (bundle / "04-review" / "review.json").read_bytes()
    report_before = report.read_bytes()
    final_before = final
    rc, overridden, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(completed["generation"]),
            "--publish-mode",
            "upload",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (overridden, stderr)
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["conversion_state"] == "local_complete"
    assert overridden["publication_state"] == "blocked"
    assert overridden["final_markdown"] == final_before
    assert (bundle / "04-review" / "review.json").read_bytes() == review_before
    assert report.read_bytes() == report_before


def test_review_requires_every_page_block_and_adjacent_boundary(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"# Page one\n"
        b"<!-- review-block -->\n"
        b"## Page two\n"
        b"<!-- review-block -->\n"
        b"## Page three\n"
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        page_count=3,
        markdown=markdown,
    )

    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    evidence = json.loads(
        (bundle / pending["artifacts"]["review_evidence"]).read_text()
    )
    assert [item["page_number"] for item in evidence["baseline"]["page_references"]] == [
        1,
        2,
        3,
    ]
    assert [item["block_id"] for item in evidence["markdown_blocks"]] == [
        "block-000001",
        "block-000002",
        "block-000003",
    ]
    assert [item["boundary_id"] for item in evidence["markdown_block_boundaries"]] == [
        "boundary-000001",
        "boundary-000002",
    ]

    review_input = tmp_path / "multi-page-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": f"segment-{page_number:04d}",
                        "source_pages": {
                            "start": page_number,
                            "end": page_number,
                        },
                        "markdown_blocks": [f"block-{page_number:06d}"],
                        "checks": passing_checks(),
                    }
                    for page_number in range(1, 4)
                ],
                "boundaries": [
                    {
                        "before_segment_id": f"segment-{index:04d}",
                        "after_segment_id": f"segment-{index + 1:04d}",
                        "status": "pass",
                        "evidence": ["The adjacent segments preserve continuity."],
                        "finding_ids": [],
                    }
                    for index in range(1, 3)
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    assert review["coverage"] == {
        "source_pages": {"covered": 3, "required": 3, "complete": True},
        "markdown_blocks": {"covered": 3, "required": 3, "complete": True},
        "boundaries": {"covered": 2, "required": 2, "complete": True},
    }


def test_adjacent_review_segments_require_a_boundary_when_they_share_one_block(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, page_count=1
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "shared-block-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": f"segment-{index:04d}",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                    for index in range(1, 3)
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"


def test_boundary_difference_cannot_reference_a_missing_finding(
    tmp_path, capsys, monkeypatch
):
    markdown = b"# One\n<!-- review-block -->\n# Two\n"
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, page_count=2, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "missing-boundary-finding.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": f"segment-{index:04d}",
                        "source_pages": {"start": index, "end": index},
                        "markdown_blocks": [f"block-{index:06d}"],
                        "checks": passing_checks(),
                    }
                    for index in range(1, 3)
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-0001",
                        "after_segment_id": "segment-0002",
                        "status": "difference",
                        "evidence": ["The transition drops source content."],
                        "finding_ids": ["missing-finding"],
                    }
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"


def test_review_boundary_identity_uses_canonical_evidence_order(
    tmp_path, capsys, monkeypatch
):
    markdown = b"# One\n<!-- review-block -->\n# Two\n"
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, page_count=2, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "reversed-segments.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0002",
                        "source_pages": {"start": 2, "end": 2},
                        "markdown_blocks": ["block-000002"],
                        "checks": passing_checks(),
                    },
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    },
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-0002",
                        "after_segment_id": "segment-0001",
                        "status": "pass",
                        "evidence": ["The submitted boundary was marked as passing."],
                        "finding_ids": [],
                    }
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"


def test_incomplete_review_preserves_progress_without_declaring_a_final_markdown(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"# Page one\n"
        b"<!-- review-block -->\n"
        b"## Page two\n"
        b"<!-- review-block -->\n"
        b"## Page three\n"
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        page_count=3,
        markdown=markdown,
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)

    review_input = tmp_path / "incomplete-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "review_incomplete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, incomplete, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (incomplete, stderr)
    assert incomplete["outcome"] == "review_incomplete"
    assert incomplete["conversion_state"] == "awaiting_user"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["review"]["reason_code"] == "review_incomplete"
    assert manifest["final_markdown"] is None
    assert "final_markdown" not in manifest["artifacts"]
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    assert review["coverage"] == {
        "source_pages": {"covered": 1, "required": 3, "complete": False},
        "markdown_blocks": {"covered": 1, "required": 3, "complete": False},
        "boundaries": {"covered": 0, "required": 0, "complete": True},
    }
    assert (bundle / "04-review" / "review-report.md").is_file()
    assert not list((bundle / "04-review").glob("*.corrected.md"))

    rc, resumed, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(incomplete["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (resumed, stderr)
    assert resumed["outcome"] == "review_pending"
    assert resumed["action_required"] == "record_review"
    assert resumed["review_coverage"] == review["coverage"]

    continuation_input = tmp_path / "continued-review.json"
    continuation_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0002",
                        "source_pages": {"start": 2, "end": 3},
                        "markdown_blocks": ["block-000002", "block-000003"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-0001",
                        "after_segment_id": "segment-0002",
                        "status": "pass",
                        "evidence": ["The resumed segments preserve continuity."],
                        "finding_ids": [],
                    }
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(resumed["generation"]),
            "--action-id",
            resumed["action_id"],
            "--evidence-hash",
            resumed["evidence_hash"],
            "--input",
            str(continuation_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    assert completed["conversion_state"] == "local_complete"
    assert completed["review_coverage"] == {
        "source_pages": {"covered": 3, "required": 3, "complete": True},
        "markdown_blocks": {"covered": 3, "required": 3, "complete": True},
        "boundaries": {"covered": 1, "required": 1, "complete": True},
    }
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert len(manifest["review"]["rounds"]) == 2
    assert (bundle / "04-review" / "review.json").is_file()
    assert (bundle / manifest["artifacts"]["review"]).is_file()


def test_incomplete_review_can_resume_by_filling_an_earlier_coverage_gap(
    tmp_path, capsys, monkeypatch
):
    markdown = b"# Page one\n<!-- review-block -->\n# Page two\n"
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, page_count=2, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    late_input = tmp_path / "late-progress.json"
    late_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "review_incomplete",
                "segments": [
                    {
                        "segment_id": "segment-late",
                        "source_pages": {"start": 2, "end": 2},
                        "markdown_blocks": ["block-000002"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, incomplete, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(late_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (incomplete, stderr)
    rc, resumed, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(incomplete["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (resumed, stderr)
    early_input = tmp_path / "early-progress.json"
    early_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-early",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-early",
                        "after_segment_id": "segment-late",
                        "status": "pass",
                        "evidence": ["The earlier gap joins the preserved later segment."],
                        "finding_ids": [],
                    }
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(resumed["generation"]),
            "--action-id",
            resumed["action_id"],
            "--evidence-hash",
            resumed["evidence_hash"],
            "--input",
            str(early_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (completed, stderr)
    assert completed["conversion_state"] == "local_complete"
    assert completed["review_coverage"]["boundaries"] == {
        "covered": 1,
        "required": 1,
        "complete": True,
    }


def test_evidenced_difference_waits_for_ticket_nine_correction_without_editing_raw(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    raw_path = bundle / converted["artifacts"]["raw_markdown"]
    raw_before = raw_path.read_bytes()
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    checks = passing_checks()
    text_check = next(item for item in checks if item["category"] == "text")
    text_check.update(
        {
            "status": "difference",
            "evidence": ["Page 1 contains 'Source body', not 'Raw body'."],
            "finding_ids": ["finding-0001"],
        }
    )
    review_input = tmp_path / "difference-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "correction_required",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": checks,
                    }
                ],
                "boundaries": [],
                "findings": [
                    {
                        "finding_id": "finding-0001",
                        "kind": "difference",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "summary": "The converted body text differs from the source.",
                        "evidence": ["The page reference shows the source wording."],
                        "action": "correct_markdown",
                        "status": "open",
                    }
                ],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, correction, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (correction, stderr)
    assert correction["outcome"] == "correction_required"
    assert correction["conversion_state"] == "review_pending"
    assert correction["action_required"] == "record_correction"
    assert correction["action_id"].startswith("correction-")
    assert correction["evidence_hash"].startswith("sha256:")
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["review"]["reason_code"] is None
    assert manifest["review"]["pending_action"] == {
        "kind": "record_correction",
        "action_id": correction["action_id"],
        "generation": correction["generation"],
        "evidence_hash": correction["evidence_hash"],
    }
    assert manifest["final_markdown"] is None
    assert raw_path.read_bytes() == raw_before
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    assert review["rounds"][0]["findings"][0]["finding_id"] == "finding-0001"
    assert not list((bundle / "04-review").glob("*.corrected.md"))


def test_structured_correction_creates_an_immutable_draft_and_follow_up_review(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    raw_path = bundle / converted["artifacts"]["raw_markdown"]
    archive_path = next((bundle / "03-converted" / "attempts").glob("*/result.zip"))
    raw_before = raw_path.read_bytes()
    archive_before = archive_path.read_bytes()
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    checks = passing_checks()
    next(item for item in checks if item["category"] == "text").update(
        {
            "status": "difference",
            "evidence": ["Page 1 says 'Source body'."],
            "finding_ids": ["finding-0001"],
        }
    )
    review_input = tmp_path / "correction-required.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "correction_required",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": checks,
                    }
                ],
                "boundaries": [],
                "findings": [
                    {
                        "finding_id": "finding-0001",
                        "kind": "difference",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "summary": "The body text is incorrect.",
                        "evidence": ["The page reference contains the source wording."],
                        "action": "correct_markdown",
                        "status": "open",
                    }
                ],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, correction_required, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (correction_required, stderr)

    replacement_input = tmp_path / "correction.json"
    replacement_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corrections": [
                    {
                        "correction_id": "correction-item-0001",
                        "finding_id": "finding-0001",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "review_segment_ids": ["segment-0001"],
                        "affected_boundaries": [],
                        "anchor": {
                            "start_byte": 13,
                            "end_byte": 22,
                            "expected_text": "Raw body.",
                            "expected_sha256": "sha256:"
                            + hashlib.sha256(b"Raw body.").hexdigest(),
                        },
                        "replacement": "Source body.",
                        "basis": ["Page 1 visibly contains the corrected wording."],
                        "representation": "markdown",
                        "fallback": None,
                    }
                ],
            }
        )
    )
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(correction_required["generation"]),
            "--action-id",
            correction_required["action_id"],
            "--evidence-hash",
            correction_required["evidence_hash"],
            "--input",
            str(replacement_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (corrected, stderr)
    assert corrected["outcome"] == "correction_applied"
    assert corrected["conversion_state"] == "review_pending"
    assert corrected["review_status"] == "review_pending"
    assert corrected["action_required"] == "record_review"
    assert corrected["final_markdown"] is None
    manifest = json.loads((bundle / "manifest.json").read_text())
    correction = manifest["corrections"][-1]
    corrected_path = bundle / correction["corrected_markdown"]["path"]
    diff_path = bundle / correction["diff"]["path"]
    record_path = bundle / correction["record"]["path"]
    assert corrected_path.parent == bundle / "04-review"
    assert corrected_path.name.endswith(".corrected.md")
    assert corrected_path.read_bytes() == b"# Converted\n\nSource body.\n"
    assert b"-Raw body." in diff_path.read_bytes()
    assert b"+Source body." in diff_path.read_bytes()
    correction_record = json.loads(record_path.read_text())
    assert correction_record["corrections"][0]["finding_id"] == "finding-0001"
    assert correction_record["corrections"][0]["original_content"] == "Raw body."
    assert correction_record["corrections"][0]["corrected_content"] == "Source body."
    assert raw_path.read_bytes() == raw_before
    assert archive_path.read_bytes() == archive_before
    assert manifest["final_markdown"] is None
    assert manifest["review"]["target"]["kind"] == "corrected_markdown"
    assert manifest["review"]["target"]["path"] == correction[
        "corrected_markdown"
    ]["path"]

    follow_up_input = tmp_path / "corrected-review.json"
    follow_up_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(corrected["generation"]),
            "--action-id",
            corrected["action_id"],
            "--evidence-hash",
            corrected["evidence_hash"],
            "--input",
            str(follow_up_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    assert completed["outcome"] == "local_complete"
    assert completed["conversion_state"] == "local_complete"
    assert completed["final_markdown"]["kind"] == "corrected_markdown"
    assert completed["final_markdown"]["correction_id"] == "correction-0001"
    assert completed["final_markdown"]["path"] == correction[
        "corrected_markdown"
    ]["path"]
    assert raw_path.read_bytes() == raw_before
    assert archive_path.read_bytes() == archive_before

    rc, inspected, stderr = raw_test.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (inspected, stderr)
    assert inspected["final_markdown"] == completed["final_markdown"]


@pytest.mark.parametrize("gap", ["renamed_segment", "collapsed_boundary"])
def test_corrected_follow_up_cannot_omit_affected_segments_or_boundaries(
    tmp_path, capsys, monkeypatch, gap
):
    markdown = b"Raw body.\n<!-- review-block -->\nSecond body.\n"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        page_count=2,
        finding_block="block-000001",
        finding_page=1,
    )
    correction_payload = simple_correction_payload()
    correction_payload["corrections"][0]["anchor"].update(
        {"start_byte": 0, "end_byte": len(b"Raw body.")}
    )
    correction_payload["corrections"][0]["affected_boundaries"] = [
        {
            "before_segment_id": "segment-0001",
            "after_segment_id": "segment-0002",
        }
    ]
    correction_input = tmp_path / f"follow-up-{gap}-correction.json"
    correction_input.write_text(json.dumps(correction_payload))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)

    def segment(segment_id, page, blocks):
        return {
            "segment_id": segment_id,
            "source_pages": {"start": page[0], "end": page[1]},
            "markdown_blocks": blocks,
            "checks": passing_checks(),
        }

    if gap == "renamed_segment":
        invalid_segments = [
            segment("renamed-segment-0001", (1, 1), ["block-000001"]),
            segment("segment-0002", (2, 2), ["block-000002"]),
        ]
        invalid_boundaries = [
            {
                "before_segment_id": "renamed-segment-0001",
                "after_segment_id": "segment-0002",
                "status": "pass",
                "evidence": ["The submitted adjacent segments are continuous."],
                "finding_ids": [],
            }
        ]
    else:
        invalid_segments = [
            segment(
                "segment-0001",
                (1, 2),
                ["block-000001", "block-000002"],
            )
        ]
        invalid_boundaries = []
    invalid_input = tmp_path / f"invalid-follow-up-{gap}.json"
    invalid_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": invalid_segments,
                "boundaries": invalid_boundaries,
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(corrected["generation"]),
            "--action-id",
            corrected["action_id"],
            "--evidence-hash",
            corrected["evidence_hash"],
            "--input",
            str(invalid_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before

    valid_input = tmp_path / f"valid-follow-up-{gap}.json"
    valid_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    segment("segment-0001", (1, 1), ["block-000001"]),
                    segment("segment-0002", (2, 2), ["block-000002"]),
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-0001",
                        "after_segment_id": "segment-0002",
                        "status": "pass",
                        "evidence": ["The affected adjacent boundary was rechecked."],
                        "finding_ids": [],
                    }
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(corrected["generation"]),
            "--action-id",
            corrected["action_id"],
            "--evidence-hash",
            corrected["evidence_hash"],
            "--input",
            str(valid_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    assert completed["conversion_state"] == "local_complete"
    assert completed["final_markdown"]["kind"] == "corrected_markdown"


def test_incomplete_corrected_review_resumes_the_same_corrected_target(
    tmp_path, capsys, monkeypatch
):
    markdown = b"Raw body.\n<!-- review-block -->\nSecond body.\n"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        page_count=2,
        finding_block="block-000001",
        finding_page=1,
    )
    correction_payload = simple_correction_payload()
    correction_payload["corrections"][0]["anchor"].update(
        {"start_byte": 0, "end_byte": len(b"Raw body.")}
    )
    correction_payload["corrections"][0]["affected_boundaries"] = [
        {
            "before_segment_id": "segment-0001",
            "after_segment_id": "segment-0002",
        }
    ]
    correction_input = tmp_path / "incomplete-corrected-correction.json"
    correction_input.write_text(json.dumps(correction_payload))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)

    incomplete_input = tmp_path / "incomplete-corrected-review.json"
    incomplete_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "review_incomplete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, incomplete, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(corrected["generation"]),
            "--action-id",
            corrected["action_id"],
            "--evidence-hash",
            corrected["evidence_hash"],
            "--input",
            str(incomplete_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (incomplete, stderr)
    assert incomplete["outcome"] == "review_incomplete"

    rc, resumed, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(incomplete["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (resumed, stderr)
    assert resumed["outcome"] == "review_pending"
    manifest = json.loads((bundle / "manifest.json").read_text())
    target = manifest["review"]["target"]
    corrected_summary = manifest["corrections"][-1]["corrected_markdown"]
    assert target["kind"] == "corrected_markdown"
    assert target["path"] == corrected_summary["path"]
    assert "sha256:" + target["sha256"] == corrected_summary["sha256"]


def test_correction_anchor_must_belong_to_the_finding_markdown_block(
    tmp_path, capsys, monkeypatch
):
    markdown = b"Total: 10\n<!-- review-block -->\nTotal: 10\n"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        page_count=2,
        finding_block="block-000001",
        finding_page=1,
    )
    second_start = markdown.rindex(b"Total: 10")
    correction_input = tmp_path / "wrong-block-correction.json"
    correction_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corrections": [
                    {
                        "correction_id": "correction-item-0001",
                        "finding_id": "finding-0001",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "review_segment_ids": ["segment-0001"],
                        "affected_boundaries": [
                            {
                                "before_segment_id": "segment-0001",
                                "after_segment_id": "segment-0002",
                            }
                        ],
                        "anchor": {
                            "start_byte": second_start,
                            "end_byte": second_start + len(b"Total: 10"),
                            "expected_text": "Total: 10",
                            "expected_sha256": "sha256:"
                            + hashlib.sha256(b"Total: 10").hexdigest(),
                        },
                        "replacement": "Total: 11",
                        "basis": ["Page 1 contains the corrected value."],
                        "representation": "markdown",
                        "fallback": None,
                    }
                ],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_correction_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert not list((bundle / "04-review").glob("*.corrected*.md"))


def test_correction_input_removed_during_read_is_a_record_error(
    tmp_path, capsys, monkeypatch
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    correction_input = tmp_path / "removed-during-read.json"
    correction_input.write_text(json.dumps(simple_correction_payload()))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    original_stat = correction_module.os.stat
    matching_calls = 0

    def remove_before_final_identity_check(path, *args, **kwargs):
        nonlocal matching_calls
        if path == correction_input:
            matching_calls += 1
            if matching_calls == 2:
                correction_input.unlink()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(correction_module.os, "stat", remove_before_final_identity_check)
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_correction_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


def test_review_input_removed_during_read_is_a_record_error(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(tmp_path, capsys, monkeypatch)
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "removed-review-during-read.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    original_stat = review_module.os.stat
    matching_calls = 0

    def remove_before_final_identity_check(path, *args, **kwargs):
        nonlocal matching_calls
        if path == review_input:
            matching_calls += 1
            if matching_calls == 2:
                review_input.unlink()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(review_module.os, "stat", remove_before_final_identity_check)
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


@pytest.mark.parametrize("record_kind", ["review", "correction"])
def test_untrusted_page_ranges_are_bounded_before_materializing(
    tmp_path, capsys, monkeypatch, record_kind
):
    if record_kind == "review":
        bundle, converted, dependencies = converted_bundle(
            tmp_path, capsys, monkeypatch
        )
        rc, pending, stderr = raw_test.invoke(
            capsys,
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(converted["generation"]),
                "--visual-capability",
                "available",
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
        assert rc == 0, (pending, stderr)
        payload = {
            "schema_version": 1,
            "status": "local_complete",
            "segments": [
                {
                    "segment_id": "segment-0001",
                    "source_pages": {"start": 1, "end": 10**18},
                    "markdown_blocks": ["block-000001"],
                    "checks": passing_checks(),
                }
            ],
            "boundaries": [],
            "findings": [],
            "page_misc": [],
            "absence_basis": [],
        }
        command = "review"
        expected_code = "invalid_review_record"
    else:
        bundle, pending, dependencies = correction_required_bundle(
            tmp_path, capsys, monkeypatch
        )
        payload = simple_correction_payload()
        payload["corrections"][0]["source_pages"]["end"] = 10**18
        command = "correction"
        expected_code = "invalid_correction_record"
    record_input = tmp_path / f"huge-{record_kind}-page-range.json"
    record_input.write_text(json.dumps(payload))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    generation_before = json.loads((bundle / "manifest.json").read_text())["generation"]

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            command,
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(record_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == expected_code
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert json.loads((bundle / "manifest.json").read_text())["generation"] == generation_before


@pytest.mark.parametrize("record_kind", ["review", "correction", "review-decision"])
def test_record_subcommands_reject_boolean_schema_versions(
    tmp_path, capsys, monkeypatch, record_kind
):
    if record_kind == "review":
        bundle, converted, dependencies = converted_bundle(
            tmp_path, capsys, monkeypatch
        )
        rc, pending, stderr = raw_test.invoke(
            capsys,
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(converted["generation"]),
                "--visual-capability",
                "available",
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
        assert rc == 0, (pending, stderr)
        payload = {
            "schema_version": True,
            "status": "local_complete",
            "segments": [
                {
                    "segment_id": "segment-0001",
                    "source_pages": {"start": 1, "end": 1},
                    "markdown_blocks": ["block-000001"],
                    "checks": passing_checks(),
                }
            ],
            "boundaries": [],
            "findings": [],
            "page_misc": [],
            "absence_basis": [],
        }
        expected_code = "invalid_review_record"
    elif record_kind == "correction":
        bundle, pending, dependencies = correction_required_bundle(
            tmp_path, capsys, monkeypatch
        )
        payload = simple_correction_payload()
        payload["schema_version"] = True
        expected_code = "invalid_correction_record"
    else:
        bundle, _converted, pending, dependencies = ambiguity_required_bundle(
            tmp_path, capsys, monkeypatch
        )
        payload = {
            "schema_version": True,
            "decisions": [
                {
                    "finding_id": "finding-0001",
                    "resolution": "keep_current",
                    "selected_content": None,
                    "basis": ["The user selected the current reading."],
                }
            ],
        }
        expected_code = "invalid_review_decision"
    record_input = tmp_path / f"boolean-schema-{record_kind}.json"
    record_input.write_text(json.dumps(payload))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            record_kind,
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(record_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == expected_code
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


@pytest.mark.parametrize("field", ["expected_text", "replacement"])
def test_correction_rejects_lone_surrogate_text(
    tmp_path, capsys, monkeypatch, field
):
    bundle, pending, dependencies = correction_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    payload = simple_correction_payload()
    item = payload["corrections"][0]
    if field == "expected_text":
        item["anchor"]["expected_text"] = "\ud800"
    else:
        item["replacement"] = "\ud800"
    record_input = tmp_path / f"surrogate-{field}.json"
    record_input.write_text(json.dumps(payload))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(record_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_correction_record"
    assert rejected["action_required"] == "correct_correction_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


def test_review_decision_rejects_lone_surrogate_selected_content(
    tmp_path, capsys, monkeypatch
):
    bundle, _converted, pending, dependencies = ambiguity_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    decision_input = tmp_path / "surrogate-review-decision.json"
    decision_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "finding_id": "finding-0001",
                        "resolution": "correct_markdown",
                        "selected_content": "\ud800",
                        "basis": ["The user selected this reading."],
                    }
                ],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review-decision",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(decision_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_decision"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


@pytest.mark.parametrize("recovery_entrypoint", ["record", "resume"])
def test_correction_recovers_from_a_durable_intent_without_creating_a_second_draft(
    tmp_path, capsys, monkeypatch, recovery_entrypoint
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    correction_input = tmp_path / "recover-correction.json"
    correction_input.write_text(json.dumps(simple_correction_payload()))
    argv = [
        "record",
        "correction",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(required["generation"]),
        "--action-id",
        required["action_id"],
        "--evidence-hash",
        required["evidence_hash"],
        "--input",
        str(correction_input),
    ]
    original_write = review_module._write_exclusive

    def crash_after_intent(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    monkeypatch.setattr(review_module, "_write_exclusive", crash_after_intent)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)

    recovery_argv = (
        argv
        if recovery_entrypoint == "record"
        else [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
        ]
    )
    rc, recovered, stderr = raw_test.invoke(
        capsys,
        recovery_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (recovered, stderr)
    assert recovered["outcome"] == "correction_applied"
    assert recovered["action_required"] == "record_review"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [item["correction_id"] for item in manifest["corrections"]] == [
        "correction-0001"
    ]
    assert len(list((bundle / "04-review").glob("*.corrected*.md"))) == 1
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert sum(item.get("event") == "correction_record_intent" for item in history) == 1
    assert sum(item.get("event") == "correction_record_prepared" for item in history) == 1
    assert sum(item.get("event") == "correction_record_committed" for item in history) == 1


@pytest.mark.parametrize(
    "crash_stage",
    ["prepared", "first_promote", "private", "manifest", "before_committed", "committed"],
)
def test_correction_recovers_each_durable_journal_boundary(
    tmp_path, capsys, monkeypatch, crash_stage
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    correction_input = tmp_path / f"recover-{crash_stage}.json"
    correction_input.write_text(json.dumps(simple_correction_payload()))
    argv = [
        "record",
        "correction",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(required["generation"]),
        "--action-id",
        required["action_id"],
        "--evidence-hash",
        required["evidence_hash"],
        "--input",
        str(correction_input),
    ]
    original_append = review_module.bundle.append_history
    original_promote = review_module._promote_correction_artifact
    original_atomic = review_module.bundle.atomic_write_json
    promoted = 0

    def crash_on_history(event, *, state_fd):
        if crash_stage == "prepared" and event.get("event") == "correction_record_prepared":
            original_append(event, state_fd=state_fd)
            raise SimulatedProcessCrash()
        if event.get("event") == "correction_record_committed":
            if crash_stage == "before_committed":
                raise SimulatedProcessCrash()
            if crash_stage == "committed":
                original_append(event, state_fd=state_fd)
                raise SimulatedProcessCrash()
        return original_append(event, state_fd=state_fd)

    def crash_on_promote(temporary_name, final_path, *, review_fd):
        nonlocal promoted
        original_promote(temporary_name, final_path, review_fd=review_fd)
        promoted += 1
        if crash_stage == "first_promote" and promoted == 1:
            raise SimulatedProcessCrash()

    def crash_on_state(name, value, *, dir_fd):
        original_atomic(name, value, dir_fd=dir_fd)
        if crash_stage == "private" and name == "private.json":
            raise SimulatedProcessCrash()
        if crash_stage == "manifest" and name == "manifest.json":
            raise SimulatedProcessCrash()

    monkeypatch.setattr(review_module.bundle, "append_history", crash_on_history)
    monkeypatch.setattr(review_module, "_promote_correction_artifact", crash_on_promote)
    monkeypatch.setattr(review_module.bundle, "atomic_write_json", crash_on_state)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module.bundle, "append_history", original_append)
    monkeypatch.setattr(review_module, "_promote_correction_artifact", original_promote)
    monkeypatch.setattr(review_module.bundle, "atomic_write_json", original_atomic)

    def rebuild_after_prepared_is_forbidden(*_args, **_kwargs):
        raise AssertionError("prepared correction recovery must not rebuild artifacts")

    monkeypatch.setattr(
        correction_module, "apply_corrections", rebuild_after_prepared_is_forbidden
    )

    if crash_stage == "committed":
        rc, recovered, stderr = raw_test.invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    else:
        rc, recovered, stderr = raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    assert rc == 0, (recovered, stderr)
    assert recovered["conversion_state"] == "review_pending"
    assert recovered["action_required"] == "record_review"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [item["correction_id"] for item in manifest["corrections"]] == [
        "correction-0001"
    ]
    assert len(list((bundle / "04-review").glob("*.corrected*.md"))) == 1
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert sum(item.get("event") == "correction_record_intent" for item in history) == 1
    assert sum(item.get("event") == "correction_record_prepared" for item in history) == 1
    assert sum(item.get("event") == "correction_record_committed" for item in history) == 1


def test_crop_correction_recovers_after_the_nested_asset_is_promoted(
    tmp_path, capsys, monkeypatch
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=b"Raw body.\n",
        finding_category="images",
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    correction_input = tmp_path / "recover-crop-promote.json"
    correction_input.write_text(json.dumps(lossless_crop_payload(bundle)))
    argv = [
        "record",
        "correction",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(required["generation"]),
        "--action-id",
        required["action_id"],
        "--evidence-hash",
        required["evidence_hash"],
        "--input",
        str(correction_input),
    ]
    original_promote = review_module._promote_correction_artifact
    promoted = 0

    def crash_after_nested_asset(temporary_name, final_path, *, review_fd):
        nonlocal promoted
        original_promote(temporary_name, final_path, review_fd=review_fd)
        promoted += 1
        if promoted == 1:
            assert final_path.startswith("assets/")
            raise SimulatedProcessCrash()

    monkeypatch.setattr(
        review_module, "_promote_correction_artifact", crash_after_nested_asset
    )
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_promote_correction_artifact", original_promote)

    rc, recovered, stderr = raw_test.invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (recovered, stderr)
    manifest = json.loads((bundle / "manifest.json").read_text())
    assets = manifest["corrections"][0]["assets"]
    assert len(assets) == 1
    assert (bundle / assets[0]["path"]).is_file()
    assert len(list((bundle / "04-review" / "assets").glob("*.png"))) == 1


def test_safe_html_fallback_requires_structured_necessity_and_the_shared_allowlist(
    tmp_path, capsys, monkeypatch
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    payload = simple_correction_payload(
        replacement=(
            "<table><tbody><tr><th>Label</th><td>Source body.</td>"
            "</tr></tbody></table>"
        )
    )
    item = payload["corrections"][0]
    item["representation"] = "safe_html"
    item["fallback"] = {
        "markdown": {
            "status": "insufficient",
            "reasons": ["The required row and column spans cannot be expressed in GFM."],
        },
        "html": {
            "status": "sufficient",
            "reasons": ["The allowlisted table structure preserves the relationships."],
        },
    }
    correction_input = tmp_path / "safe-html-correction.json"
    correction_input.write_text(json.dumps(payload))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)
    corrected_path = bundle / corrected["artifacts"]["corrected_markdown"]
    assert b"<table>" in corrected_path.read_bytes()

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir()
    unsafe_bundle, unsafe_required, unsafe_dependencies = correction_required_bundle(
        unsafe_root,
        capsys,
        monkeypatch,
    )
    unsafe_payload = simple_correction_payload(
        replacement="<script>alert('unsafe')</script>"
    )
    unsafe_item = unsafe_payload["corrections"][0]
    unsafe_item["representation"] = "safe_html"
    unsafe_item["fallback"] = item["fallback"]
    unsafe_input = tmp_path / "unsafe-html-correction.json"
    unsafe_input.write_text(json.dumps(unsafe_payload))
    history_before = (unsafe_bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(unsafe_bundle),
            "--expected-generation",
            str(unsafe_required["generation"]),
            "--action-id",
            unsafe_required["action_id"],
            "--evidence-hash",
            unsafe_required["evidence_hash"],
            "--input",
            str(unsafe_input),
        ],
        cwd=unsafe_root,
        environ=unsafe_dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_correction_record"
    assert (unsafe_bundle / ".state" / "history.ndjson").read_bytes() == history_before


def test_corrected_markdown_rebases_local_resources_without_copying_them(
    tmp_path, capsys, monkeypatch
):
    markdown = b"![Figure](assets/figure.png) Raw body.\n"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[("nested/assets/figure.png", PNG_1X1)],
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    start = markdown.index(b"Raw body.")
    payload = simple_correction_payload()
    anchor = payload["corrections"][0]["anchor"]
    anchor["start_byte"] = start
    anchor["end_byte"] = start + len(b"Raw body.")
    correction_input = tmp_path / "resource-correction.json"
    correction_input.write_text(json.dumps(payload))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)
    manifest = json.loads((bundle / "manifest.json").read_text())
    summary = manifest["corrections"][-1]
    corrected_path = bundle / summary["corrected_markdown"]["path"]
    asset_path = next((bundle / "03-converted").glob("**/nested/assets/figure.png"))
    expected_target = asset_path.relative_to(bundle).as_posix()
    expected_rebased = "../" + expected_target
    assert f"]({expected_rebased})".encode() in corrected_path.read_bytes()
    record = json.loads((bundle / summary["record"]["path"]).read_text())
    assert record["resource_rewrites"] == [
        {
            **record["resource_rewrites"][0],
            "bundle_path": expected_target,
            "original_target": "assets/figure.png",
            "rewritten_target": expected_rebased,
            "count": 1,
        }
    ]
    assert record["resource_rewrites"][0]["sha256"] == (
        "sha256:" + hashlib.sha256(PNG_1X1).hexdigest()
    )
    assert not (bundle / "04-review" / "assets").exists()


def test_resource_rebase_uses_pandoc_oracle_across_nested_gfm_containers(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"- ![List](assets/list.png)\n"
        b"  [![Nested](assets/nested.png)] Raw body.\n\n"
        b"  >     ![Code](assets/code.png)\n"
    )
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[
            ("nested/assets/list.png", PNG_1X1),
            ("nested/assets/nested.png", PNG_1X1),
            ("nested/assets/code.png", b"code example only"),
        ],
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    payload = simple_correction_payload()
    start = markdown.index(b"Raw body.")
    payload["corrections"][0]["anchor"].update(
        {"start_byte": start, "end_byte": start + len(b"Raw body.")}
    )
    correction_input = tmp_path / "container-resource-correction.json"
    correction_input.write_text(json.dumps(payload))

    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (corrected, stderr)
    manifest = json.loads((bundle / "manifest.json").read_text())
    summary = manifest["corrections"][-1]
    corrected_bytes = (bundle / summary["corrected_markdown"]["path"]).read_bytes()
    expected_targets = {}
    for name in ("list.png", "nested.png"):
        asset_path = next((bundle / "03-converted").glob(f"**/nested/assets/{name}"))
        expected_targets[name] = "../" + asset_path.relative_to(bundle).as_posix()
        assert expected_targets[name].encode() in corrected_bytes
    assert b">     ![Code](assets/code.png)" in corrected_bytes
    assert b">     ![Code](../" not in corrected_bytes
    record = json.loads((bundle / summary["record"]["path"]).read_text())
    assert sum(item["count"] for item in record["resource_rewrites"]) == 2
    assert {item["original_target"] for item in record["resource_rewrites"]} == {
        "assets/list.png",
        "assets/nested.png",
    }
    assert record["resource_reference_oracle"] == [
        {"kind": "image", "target": "assets/list.png"},
        {"kind": "image", "target": "assets/nested.png"},
    ]


def test_resource_rebase_preserves_reference_images_and_html_img_attributes(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b'![Ref][fig] <img src="assets/html.png" alt="HTML"> Raw body.\n\n'
        b'[fig]: assets/ref.png "Reference title"\n'
    )
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[
            ("nested/assets/ref.png", PNG_1X1),
            ("nested/assets/html.png", PNG_1X1),
        ],
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    payload = simple_correction_payload()
    start = markdown.index(b"Raw body.")
    payload["corrections"][0]["anchor"].update(
        {"start_byte": start, "end_byte": start + len(b"Raw body.")}
    )
    correction_input = tmp_path / "reference-html-correction.json"
    correction_input.write_text(json.dumps(payload))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)
    manifest = json.loads((bundle / "manifest.json").read_text())
    summary = manifest["corrections"][-1]
    corrected_bytes = (bundle / summary["corrected_markdown"]["path"]).read_bytes()
    assert b' alt="HTML"' in corrected_bytes
    assert b' "Reference title"' in corrected_bytes
    assert b"]: assets/ref.png" not in corrected_bytes
    assert b'src="assets/html.png"' not in corrected_bytes
    record = json.loads((bundle / summary["record"]["path"]).read_text())
    assert {kind for item in record["resource_rewrites"] for kind in item["kinds"]} == {
        "html_image",
        "markdown_reference_image",
    }
    assert record["resource_reference_oracle"] == [
        {"kind": "html_image", "target": "assets/html.png"},
        {"kind": "image", "target": "assets/ref.png"},
    ]


@pytest.mark.parametrize(
    ("target", "expected_code"),
    [
        ("../../../../../../outside.png", "asset_path_escape"),
        ("assets/missing.png", "asset_missing"),
    ],
)
def test_raw_review_rejects_targets_outside_or_missing_from_the_bundle(
    tmp_path, capsys, monkeypatch, target, expected_code
):
    (tmp_path / "outside.png").write_bytes(b"outside bundle")
    markdown = f"![Unsafe]({target}) Raw body.\n".encode()
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 4
    assert rejected["errors"][0]["code"] == expected_code
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert not list((bundle / "04-review").glob("review-evidence-*.json"))


def test_lossless_crop_is_committed_with_the_correction_and_referenced_locally(
    tmp_path, capsys, monkeypatch
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=b"Raw body.\n",
        finding_category="images",
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    page_path = bundle / "02-pages" / "page-0001.png"
    page_before = page_path.read_bytes()
    correction_input = tmp_path / "crop-correction.json"
    correction_input.write_text(json.dumps(lossless_crop_payload(bundle)))

    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (corrected, stderr)
    manifest = json.loads((bundle / "manifest.json").read_text())
    summary = manifest["corrections"][-1]
    assert len(summary["assets"]) == 1
    asset = summary["assets"][0]
    assert asset["path"].startswith("04-review/assets/")
    crop_path = bundle / asset["path"]
    crop_bytes = crop_path.read_bytes()
    assert crop_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert asset["sha256"] == "sha256:" + hashlib.sha256(crop_bytes).hexdigest()
    corrected_bytes = (bundle / summary["corrected_markdown"]["path"]).read_bytes()
    assert f"![Source diagram](assets/{crop_path.name})".encode() in corrected_bytes
    record = json.loads((bundle / summary["record"]["path"]).read_text())
    assert record["crops"][0]["source_page_number"] == 1
    assert record["crops"][0]["requested_bbox"] == record["crops"][0][
        "actual_bbox"
    ]
    assert record["crops"][0]["output_sha256"] == asset["sha256"]
    assert record["crops"][0]["provenance"]["lossless"] is True
    assert record["crops"][0]["provenance"]["resized"] is False
    assert page_path.read_bytes() == page_before
    evidence = json.loads(
        (bundle / corrected["artifacts"]["review_evidence"]).read_text()
    )
    snapshot = evidence["target"]["local_resources"]
    assert snapshot["reference_count"] == 1
    assert snapshot["oracle"] == [
        {"kind": "image", "target": f"assets/{crop_path.name}"}
    ]
    assert snapshot["references"][0]["bundle_path"] == asset["path"]
    assert snapshot["references"][0]["media_type"] == "image/png"
    assert snapshot["references"][0]["provenance"] == {
        "kind": "lossless_crop",
        "correction_id": summary["correction_id"],
        "correction_item_id": "correction-item-0001",
        "finding_id": "finding-0001",
        "source_page_number": 1,
        "source_image_sha256": record["crops"][0]["source_image_sha256"],
        "requested_bbox": record["crops"][0]["requested_bbox"],
        "output_sha256": asset["sha256"],
    }


@pytest.mark.parametrize("visual_kind", ["music_notation", "form", "timeline"])
def test_visual_object_kind_is_descriptive_with_a_closed_content_class(visual_kind):
    basis = page_crop_module._validate_basis(
        {
            "markdown": {"status": "insufficient", "reasons": ["Not representable."]},
            "html": {"status": "insufficient", "reasons": ["Not representable."]},
            "visual_object": {
                "content_class": "visual_object",
                "kind": visual_kind,
                "reasons": ["The source content is inherently visual."],
            },
        }
    )

    assert basis["visual_object"]["kind"] == visual_kind


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "bounds",
        "whole_page",
        "near_whole",
        "editable_text",
        "text_category",
    ],
)
def test_lossless_crop_rejects_unsafe_or_unnecessary_requests(
    tmp_path, capsys, monkeypatch, invalid_kind
):
    finding_category = "text" if invalid_kind == "text_category" else "images"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=b"Raw body.\n",
        finding_category=finding_category,
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    page = manifest["preflight"]["page_references"][0]
    payload = lossless_crop_payload(bundle)
    item = payload["corrections"][0]
    if invalid_kind == "bounds":
        item["crop"]["bbox"]["x1"] = page["pixel_width"] + 1
    elif invalid_kind == "whole_page":
        item["crop"]["bbox"] = {
            "x0": 0,
            "y0": 0,
            "x1": page["pixel_width"],
            "y1": page["pixel_height"],
        }
    elif invalid_kind == "near_whole":
        item["crop"]["bbox"] = {
            "x0": 0,
            "y0": 0,
            "x1": page["pixel_width"],
            "y1": page["pixel_height"] - 1,
        }
    elif invalid_kind == "editable_text":
        item["fallback"]["visual_object"]["content_class"] = "editable_text"
    else:
        item["category"] = "text"
    correction_input = tmp_path / f"invalid-crop-{invalid_kind}.json"
    correction_input.write_text(json.dumps(payload))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] in {
        "invalid_correction_record",
        "invalid_crop_request",
        "whole_page_crop_disallowed",
    }
    assert rejected["action_required"] == "correct_correction_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert not (bundle / "04-review" / "assets").exists()


def test_ambiguous_source_content_requires_an_evidence_bound_user_decision(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    checks = passing_checks()
    text_check = next(item for item in checks if item["category"] == "text")
    text_check.update(
        {
            "status": "ambiguous",
            "evidence": ["The source glyph can reasonably be read as 1 or I."],
            "finding_ids": ["finding-0001"],
        }
    )
    review_input = tmp_path / "ambiguity-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "review_ambiguity",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": checks,
                    }
                ],
                "boundaries": [],
                "findings": [
                    {
                        "finding_id": "finding-0001",
                        "kind": "ambiguity",
                        "category": "text",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "summary": "One source glyph has two reasonable readings.",
                        "evidence": ["The page reference does not disambiguate the glyph."],
                        "action": "user_decision_required",
                        "status": "unresolved",
                    }
                ],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, ambiguous, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (ambiguous, stderr)
    assert ambiguous["outcome"] == "review_ambiguity"
    assert ambiguous["conversion_state"] == "awaiting_user"
    assert ambiguous["action_required"] == "resolve_review_ambiguity"
    assert ambiguous["action_id"].startswith("review-decision-")
    assert ambiguous["evidence_hash"].startswith("sha256:")
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["review"]["reason_code"] == "review_ambiguity"
    assert manifest["final_markdown"] is None
    assert not list((bundle / "04-review").glob("*.corrected.md"))
    evidence_path = bundle / manifest["review"]["evidence"]["path"]
    review_path = bundle / "04-review" / "review.json"
    evidence_before = evidence_path.read_bytes()
    review_before = review_path.read_bytes()

    rc, automatic, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ambiguous["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (automatic, stderr)
    assert automatic["outcome"] == "settings_overridden"
    assert automatic["conversion_state"] == "awaiting_user"
    assert automatic["action_required"] is None
    assert automatic["final_markdown"] is None
    assert evidence_path.read_bytes() == evidence_before
    assert review_path.read_bytes() == review_before

    rc, confirmed, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(automatic["generation"]),
            "--interaction-mode",
            "confirm",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (confirmed, stderr)
    assert confirmed["action_required"] == "resolve_review_ambiguity"
    assert confirmed["action_id"].startswith("review-decision-")
    assert evidence_path.read_bytes() == evidence_before
    assert review_path.read_bytes() == review_before

    decision_input = tmp_path / "review-decision.json"
    decision_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "finding_id": "finding-0001",
                        "resolution": "keep_current",
                        "selected_content": None,
                        "basis": [
                            "The user confirmed that the current glyph is the intended reading."
                        ],
                    }
                ],
            }
        )
    )
    rc, resolved, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review-decision",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(confirmed["generation"]),
            "--action-id",
            confirmed["action_id"],
            "--evidence-hash",
            confirmed["evidence_hash"],
            "--input",
            str(decision_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (resolved, stderr)
    assert resolved["outcome"] == "review_ambiguity_resolved"
    assert resolved["conversion_state"] == "local_complete"
    assert resolved["action_required"] is None
    assert resolved["final_markdown"]["kind"] == "raw_conversion"
    assert resolved["final_markdown"]["path"] == converted["artifacts"][
        "raw_markdown"
    ]
    assert not list((bundle / "04-review").glob("*.corrected*.md"))
    decision_record = bundle / resolved["artifacts"]["review_decision"]
    decision_document = json.loads(decision_record.read_text())
    assert decision_document["decisions"][0]["finding_id"] == "finding-0001"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [item["round_id"] for item in manifest["review"]["rounds"]] == [
        "review-round-0001"
    ]
    assert resolved["final_markdown"]["review_round_id"] == decision_document[
        "rounds"
    ][-1]["round_id"]


@pytest.mark.parametrize("recovery_entrypoint", ["record", "resume"])
def test_review_decision_recovery_preserves_the_command_outcome(
    tmp_path, capsys, monkeypatch, recovery_entrypoint
):
    bundle, _converted, ambiguous, dependencies = ambiguity_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    decision_input = tmp_path / "recover-review-decision.json"
    decision_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "finding_id": "finding-0001",
                        "resolution": "keep_current",
                        "selected_content": None,
                        "basis": [
                            "The user confirmed that the current glyph is intended."
                        ],
                    }
                ],
            }
        )
    )
    argv = [
        "record",
        "review-decision",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(ambiguous["generation"]),
        "--action-id",
        ambiguous["action_id"],
        "--evidence-hash",
        ambiguous["evidence_hash"],
        "--input",
        str(decision_input),
    ]
    original_write = review_module._write_exclusive

    def crash_after_intent(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    monkeypatch.setattr(review_module, "_write_exclusive", crash_after_intent)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)

    recovery_argv = (
        argv
        if recovery_entrypoint == "record"
        else [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ambiguous["generation"]),
        ]
    )
    rc, recovered, stderr = raw_test.invoke(
        capsys,
        recovery_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (recovered, stderr)
    assert recovered["outcome"] == "review_ambiguity_resolved"
    assert recovered["conversion_state"] == "local_complete"
    assert recovered["action_required"] is None


def test_review_decision_selected_content_binds_the_following_correction(
    tmp_path, capsys, monkeypatch
):
    bundle, _converted, ambiguous, dependencies = ambiguity_required_bundle(
        tmp_path, capsys, monkeypatch
    )
    decision_input = tmp_path / "correct-markdown-decision.json"
    decision_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decisions": [
                    {
                        "finding_id": "finding-0001",
                        "resolution": "correct_markdown",
                        "selected_content": "Source body.",
                        "basis": ["The user selected the source reading shown on page 1."],
                    }
                ],
            }
        )
    )
    rc, decided, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review-decision",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ambiguous["generation"]),
            "--action-id",
            ambiguous["action_id"],
            "--evidence-hash",
            ambiguous["evidence_hash"],
            "--input",
            str(decision_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (decided, stderr)
    assert decided["conversion_state"] == "review_pending"
    assert decided["action_required"] == "record_correction"

    mismatched_input = tmp_path / "mismatched-selected-content.json"
    mismatched_input.write_text(
        json.dumps(simple_correction_payload(replacement="A different reading."))
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(decided["generation"]),
            "--action-id",
            decided["action_id"],
            "--evidence-hash",
            decided["evidence_hash"],
            "--input",
            str(mismatched_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_correction_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before

    correction_input = tmp_path / "selected-content-correction.json"
    correction_input.write_text(json.dumps(simple_correction_payload()))
    rc, corrected, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(decided["generation"]),
            "--action-id",
            decided["action_id"],
            "--evidence-hash",
            decided["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (corrected, stderr)
    record_path = bundle / corrected["artifacts"]["correction_record"]
    correction_item = json.loads(record_path.read_text())["corrections"][0]
    assert correction_item["replacement"] == "Source body."
    assert correction_item["corrected_content"] == "Source body."


@pytest.mark.parametrize(
    ("markdown", "issue_code"),
    [
        (b"<script>alert('unsafe')</script>\n", "unsafe_html_tag"),
        (b"[unsafe](javascript:alert(1))\n", "unsafe_url"),
        (b"```python\nprint('unterminated')\n", "unclosed_fence"),
        (b"[label][missing]\n", "missing_reference_definition"),
        (b"Text[^missing]\n", "missing_footnote_definition"),
        (b"Equation $x + y\n", "unclosed_math_delimiter"),
        (b"| A | B |\n| x | -- |\n| 1 | 2 |\n", "invalid_pipe_table"),
        (b"<div><span>x</div>\n", "misnested_html"),
    ],
    ids=[
        "script-html",
        "javascript-link",
        "unclosed-fence",
        "missing-reference",
        "missing-footnote",
        "unclosed-math",
        "invalid-table",
        "misnested-html",
    ],
)
def test_deterministic_structure_failures_cannot_be_overridden_by_pass_claims(
    tmp_path, capsys, monkeypatch, markdown, issue_code
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    evidence = json.loads(
        (bundle / pending["artifacts"]["review_evidence"]).read_text()
    )
    assert evidence["structural_validation"]["status"] == "blocked"
    assert evidence["structural_validation"]["issues"]
    assert issue_code in {
        item["code"] for item in evidence["structural_validation"]["issues"]
    }

    review_input = tmp_path / "unsafe-claimed-pass.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, blocked, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2, (blocked, stderr)
    assert blocked["errors"][0]["code"] == "invalid_review_record"
    assert blocked["conversion_state"] == "review_pending"
    assert blocked["action_required"] == "correct_review_record"
    assert json.loads((bundle / "manifest.json").read_text())["final_markdown"] is None
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


def test_valid_gfm_math_references_table_urls_and_safe_html_remain_reviewable(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"Price is $5 and $10. Closed math: $x + y$.\n\n"
        b"[Reference][ref] and ![local image](assets/figure.png).\n\n"
        b"| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        b"Footnote[^one].\n\n[^one]: Defined.\n[ref]: https://example.com/doc\n\n"
        b"<span>Safe</span>\n"
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[("nested/assets/figure.png", PNG_1X1)],
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    evidence = json.loads(
        (bundle / pending["artifacts"]["review_evidence"]).read_text()
    )
    assert evidence["structural_validation"]["status"] == "pass"
    assert evidence["structural_validation"]["issues"] == []


def test_review_evidence_uses_nfc_source_coordinates_and_compound_ranges(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        "# Cafe\u0301 中文 😀\n"
        "<!-- compound-sourcepos -->\n"
        "<!-- review-block -->\n"
        "Second block.\n"
    ).encode("utf-8")
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    evidence = json.loads(
        (bundle / pending["artifacts"]["review_evidence"]).read_text()
    )
    target = evidence["target"]
    assert target["coordinate_space"] == "pandoc-sourcepos-nfc-v1"
    assert target["normalized_source_sha256"] != target["sha256"]
    assert target["content_index_sha256"].startswith("sha256:")
    assert target["structure_evidence_sha256"].startswith("sha256:")
    assert len(evidence["markdown_blocks"]) == 2
    assert len(evidence["markdown_blocks"][0]["source_ranges"]) == 2


def test_review_rejects_pandoc_dependency_drift_after_preflight(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    pandoc = Path(dependencies["PATH"]) / "pandoc"
    pandoc.write_text(pandoc.read_text().replace("pandoc 3.8.2", "pandoc 9.9.9"))
    pandoc.chmod(0o700)

    rc, blocked, _stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 4
    assert blocked["errors"][0]["code"] == "dependency_changed"
    assert not (bundle / "04-review" / "review-evidence-round-0001.json").exists()


def test_settings_override_rebinds_a_pending_review_without_rebuilding_evidence(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    evidence_path = bundle / pending["artifacts"]["review_evidence"]
    evidence_before = evidence_path.read_bytes()

    rc, overridden, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (overridden, stderr)
    assert overridden["outcome"] == "settings_overridden"
    assert overridden["conversion_state"] == "review_pending"
    assert overridden["generation"] == pending["generation"] + 1
    assert overridden["action_id"] == pending["action_id"]
    assert overridden["evidence_hash"] == pending["evidence_hash"]
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["review"]["pending_action"]["generation"] == overridden[
        "generation"
    ]
    assert evidence_path.read_bytes() == evidence_before


@pytest.mark.parametrize(
    "crash_boundary",
    [
        "before_evidence",
        "before_prepared",
        "before_promote",
        "before_private",
        "before_manifest",
        "before_committed",
    ],
)
def test_review_open_recovers_every_committed_write_boundary(
    tmp_path, capsys, monkeypatch, crash_boundary
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    original_write = review_module._write_exclusive
    original_append = review_module.bundle.append_history
    original_promote = review_module._promote
    original_atomic = review_module.bundle.atomic_write_json

    def crash_before_evidence(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    def crash_on_history(value, *, state_fd):
        if value.get("event") == {
            "before_prepared": "review_open_prepared",
            "before_committed": "review_open_committed",
        }.get(crash_boundary):
            raise SimulatedProcessCrash()
        return original_append(value, state_fd=state_fd)

    def crash_before_promote(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    def crash_on_state(name, value, *, dir_fd):
        if name == {
            "before_private": "private.json",
            "before_manifest": "manifest.json",
        }.get(crash_boundary):
            raise SimulatedProcessCrash()
        return original_atomic(name, value, dir_fd=dir_fd)

    if crash_boundary == "before_evidence":
        monkeypatch.setattr(review_module, "_write_exclusive", crash_before_evidence)
    elif crash_boundary in {"before_prepared", "before_committed"}:
        monkeypatch.setattr(review_module.bundle, "append_history", crash_on_history)
    elif crash_boundary == "before_promote":
        monkeypatch.setattr(review_module, "_promote", crash_before_promote)
    else:
        monkeypatch.setattr(review_module.bundle, "atomic_write_json", crash_on_state)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(converted["generation"]),
                "--visual-capability",
                "available",
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)
    monkeypatch.setattr(review_module.bundle, "append_history", original_append)
    monkeypatch.setattr(review_module, "_promote", original_promote)
    monkeypatch.setattr(review_module.bundle, "atomic_write_json", original_atomic)

    rc, recovered, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (recovered, stderr)
    assert recovered["outcome"] == "review_pending"
    assert recovered["generation"] == converted["generation"] + 1
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert sum(event.get("event") == "review_open_intent" for event in history) == 1
    assert sum(event.get("event") == "review_open_prepared" for event in history) == 1
    assert sum(event.get("event") == "review_open_committed" for event in history) == 1
    assert not list((bundle / "04-review").glob("*.part"))


@pytest.mark.parametrize(
    "crash_boundary",
    [
        "before_artifacts",
        "before_prepared",
        "before_review_promote",
        "before_report_promote",
        "before_private",
        "before_manifest",
        "before_committed",
    ],
)
def test_review_record_recovers_every_committed_write_boundary(
    tmp_path, capsys, monkeypatch, crash_boundary
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "recoverable-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    argv = [
        "record",
        "review",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(pending["generation"]),
        "--action-id",
        pending["action_id"],
        "--evidence-hash",
        pending["evidence_hash"],
        "--input",
        str(review_input),
    ]
    original_write = review_module._write_exclusive
    original_append = review_module.bundle.append_history
    original_promote = review_module._promote
    original_atomic = review_module.bundle.atomic_write_json

    def crash_before_artifacts(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    def crash_on_history(value, *, state_fd):
        if value.get("event") == {
            "before_prepared": "review_record_prepared",
            "before_committed": "review_record_committed",
        }.get(crash_boundary):
            raise SimulatedProcessCrash()
        return original_append(value, state_fd=state_fd)

    promote_calls = 0

    def crash_on_promote(*args, **kwargs):
        nonlocal promote_calls
        promote_calls += 1
        if crash_boundary == "before_review_promote" or (
            crash_boundary == "before_report_promote" and promote_calls == 2
        ):
            raise SimulatedProcessCrash()
        return original_promote(*args, **kwargs)

    def crash_on_state(name, value, *, dir_fd):
        if name == {
            "before_private": "private.json",
            "before_manifest": "manifest.json",
        }.get(crash_boundary):
            raise SimulatedProcessCrash()
        return original_atomic(name, value, dir_fd=dir_fd)

    if crash_boundary == "before_artifacts":
        monkeypatch.setattr(review_module, "_write_exclusive", crash_before_artifacts)
    elif crash_boundary in {"before_prepared", "before_committed"}:
        monkeypatch.setattr(review_module.bundle, "append_history", crash_on_history)
    elif crash_boundary in {"before_review_promote", "before_report_promote"}:
        monkeypatch.setattr(review_module, "_promote", crash_on_promote)
    else:
        monkeypatch.setattr(review_module.bundle, "atomic_write_json", crash_on_state)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)
    monkeypatch.setattr(review_module.bundle, "append_history", original_append)
    monkeypatch.setattr(review_module, "_promote", original_promote)
    monkeypatch.setattr(review_module.bundle, "atomic_write_json", original_atomic)

    rc, recovered, stderr = raw_test.invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (recovered, stderr)
    assert recovered["outcome"] == "local_complete"
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert sum(event.get("event") == "review_record_intent" for event in history) == 1
    assert sum(event.get("event") == "review_record_prepared" for event in history) == 1
    assert sum(event.get("event") == "review_record_committed" for event in history) == 1
    assert not list((bundle / "04-review").glob("*.part"))


def test_resume_completes_a_pending_review_record_from_its_durable_intent(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "resume-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    original_write = review_module._write_exclusive

    def crash_before_artifacts(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    monkeypatch.setattr(review_module, "_write_exclusive", crash_before_artifacts)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            [
                "record",
                "review",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(pending["generation"]),
                "--action-id",
                pending["action_id"],
                "--evidence-hash",
                pending["evidence_hash"],
                "--input",
                str(review_input),
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)

    rc, resumed, stderr = raw_test.invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (resumed, stderr)
    assert resumed["outcome"] == "local_complete"
    assert resumed["conversion_state"] == "local_complete"


def test_review_action_cas_rejects_mismatches_and_consumed_replay_without_writes(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "cas-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )

    def record(action_id, evidence_hash, generation):
        return raw_test.invoke(
            capsys,
            [
                "record",
                "review",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(generation),
                "--action-id",
                action_id,
                "--evidence-hash",
                evidence_hash,
                "--input",
                str(review_input),
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )

    history_path = bundle / ".state" / "history.ndjson"
    before = history_path.read_bytes()
    rc, mismatch, _stderr = record(
        "review-00000000000000000000000000000000",
        pending["evidence_hash"],
        pending["generation"],
    )
    assert rc == 5
    assert mismatch["errors"][0]["code"] == "review_action_mismatch"
    assert history_path.read_bytes() == before

    rc, mismatch, _stderr = record(
        pending["action_id"],
        "sha256:" + "0" * 64,
        pending["generation"],
    )
    assert rc == 5
    assert mismatch["errors"][0]["code"] == "evidence_hash_mismatch"
    assert history_path.read_bytes() == before

    rc, completed, stderr = record(
        pending["action_id"], pending["evidence_hash"], pending["generation"]
    )
    assert rc == 0, (completed, stderr)
    completed_bytes = history_path.read_bytes()
    rc, replayed, _stderr = record(
        pending["action_id"], pending["evidence_hash"], completed["generation"]
    )
    assert rc == 5
    assert replayed["errors"][0]["code"] == "action_already_consumed"
    assert history_path.read_bytes() == completed_bytes


def test_record_recovery_finishes_the_original_operation_before_rejecting_a_mismatch(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "pending-original-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    argv = [
        "record",
        "review",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(pending["generation"]),
        "--action-id",
        pending["action_id"],
        "--evidence-hash",
        pending["evidence_hash"],
        "--input",
        str(review_input),
    ]
    original_write = review_module._write_exclusive

    def crash_before_artifacts(*_args, **_kwargs):
        raise SimulatedProcessCrash()

    monkeypatch.setattr(review_module, "_write_exclusive", crash_before_artifacts)
    with pytest.raises(SimulatedProcessCrash):
        raw_test.invoke(
            capsys,
            argv,
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
    monkeypatch.setattr(review_module, "_write_exclusive", original_write)
    mismatched_argv = list(argv)
    mismatched_argv[mismatched_argv.index(pending["action_id"])] = (
        "review-00000000000000000000000000000000"
    )

    rc, mismatch, stderr = raw_test.invoke(
        capsys,
        mismatched_argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 5, (mismatch, stderr)
    assert mismatch["errors"][0]["code"] == "recovered_request_mismatch"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_state"] == "local_complete"
    assert manifest["final_markdown"] is not None


@pytest.mark.parametrize("artifact_name", ["review.json", "review-report.md"])
def test_inspect_rejects_tampered_authoritative_review_artifacts(
    tmp_path, capsys, monkeypatch, artifact_name
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "tamper-review.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    artifact = bundle / "04-review" / artifact_name
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    rc, inspected, _stderr = raw_test.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 4
    assert inspected["errors"][0]["code"] == "integrity_violation"


def test_boundary_pairs_without_status_and_evidence_cannot_complete_review(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"# One\n<!-- review-block -->\n"
        b"# Two\n<!-- review-block -->\n# Three\n"
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch, page_count=3, markdown=markdown
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "invalid-boundaries.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": f"segment-{index:04d}",
                        "source_pages": {"start": index, "end": index},
                        "markdown_blocks": [f"block-{index:06d}"],
                        "checks": passing_checks(),
                    }
                    for index in range(1, 4)
                ],
                "boundaries": [
                    {
                        "before_segment_id": "segment-0001",
                        "after_segment_id": "segment-0002",
                    },
                    {
                        "before_segment_id": "segment-0002",
                        "after_segment_id": "segment-0003",
                    },
                ],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


def test_page_misc_treatment_requires_source_pages_and_semantic_basis(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "invalid-page-misc.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [
                    {
                        "misc_id": "misc-0001",
                        "kind": "running_page_number",
                        "treatment": "omitted",
                    }
                ],
                "absence_basis": [],
            }
        )
    )
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 2
    assert rejected["errors"][0]["code"] == "invalid_review_record"


def test_justified_page_misc_and_verified_absence_are_preserved_in_review(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "justified-page-misc.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [
                    {
                        "misc_id": "misc-0001",
                        "kind": "running_page_number",
                        "treatment": "omitted",
                        "source_pages": {"start": 1, "end": 1},
                        "semantic_basis": [
                            "The isolated numeral is only a printed page locator."
                        ],
                        "status": "justified",
                    }
                ],
                "absence_basis": [
                    {
                        "absence_id": "absence-0001",
                        "category": "formulas",
                        "source_pages": {"start": 1, "end": 1},
                        "basis": ["No mathematical notation is present on page 1."],
                        "status": "verified_absent",
                    }
                ],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    assert review["rounds"][0]["page_misc"][0]["misc_id"] == "misc-0001"
    assert review["rounds"][0]["absence_basis"][0]["absence_id"] == (
        "absence-0001"
    )


def test_raw_review_rejects_local_references_into_bundle_control_planes(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b"![Leaked page](../../../../../02-pages/page-0001.png)\n\n"
        b"Raw body.\n"
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "unauthorized_asset_reference"
    assert rejected["generation"] == converted["generation"]
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert not list((bundle / "04-review").glob("review-evidence-*.json"))


def test_raw_review_snapshots_html_links_images_and_media_types(
    tmp_path, capsys, monkeypatch
):
    markdown = (
        b'<a href="assets/manual.pdf">Manual</a> '
        b'<img src="assets/figure.png" alt="Figure">\n'
    )
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[
            ("nested/assets/manual.pdf", b"%PDF-1.7\n%%EOF\n"),
            ("nested/assets/figure.png", PNG_1X1),
        ],
        dependency_installer=install_real_pandoc_review_dependencies,
    )

    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 0, (pending, stderr)
    evidence = json.loads(
        (bundle / pending["artifacts"]["review_evidence"]).read_text()
    )
    snapshot = evidence["target"]["local_resources"]
    assert snapshot["reference_count"] == 2
    assert snapshot["oracle"] == [
        {"kind": "html_image", "target": "assets/figure.png"},
        {"kind": "html_link", "target": "assets/manual.pdf"},
    ]
    assert {
        (item["bundle_path"], item["media_type"])
        for item in snapshot["references"]
    } == {
        (
            "03-converted/attempts/conversion-attempt-0001/raw/nested/assets/figure.png",
            "image/png",
        ),
        (
            "03-converted/attempts/conversion-attempt-0001/raw/nested/assets/manual.pdf",
            "application/pdf",
        ),
    }


@pytest.mark.parametrize(
    ("replacement", "assets"),
    [
        (
            "![Leaked page](../../../../../02-pages/page-0001.png)",
            None,
        ),
        (
            "![Hidden raw asset](assets/hidden.png)",
            [("nested/assets/hidden.png", PNG_1X1)],
        ),
    ],
)
def test_correction_cannot_add_unreviewed_bundle_resources(
    tmp_path, capsys, monkeypatch, replacement, assets
):
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=b"Raw body.\n",
        assets=assets,
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    payload = simple_correction_payload(replacement=replacement)
    payload["corrections"][0]["anchor"].update(
        {"start_byte": 0, "end_byte": len(b"Raw body.")}
    )
    correction_input = tmp_path / "unauthorized-resource-correction.json"
    correction_input.write_text(json.dumps(payload))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "unauthorized_asset_reference"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before
    assert not list((bundle / "04-review").glob("*.corrected*.md"))


def test_correction_cannot_exceed_the_reviewed_reference_multiset(
    tmp_path, capsys, monkeypatch
):
    markdown = b"![Only](assets/figure.png) Raw body.\n"
    bundle, required, dependencies = correction_required_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=markdown,
        assets=[("nested/assets/figure.png", PNG_1X1)],
        dependency_installer=install_real_pandoc_review_dependencies,
    )
    payload = simple_correction_payload(
        replacement="![Duplicate](assets/figure.png) Source body."
    )
    start = markdown.index(b"Raw body.")
    payload["corrections"][0]["anchor"].update(
        {"start_byte": start, "end_byte": start + len(b"Raw body.")}
    )
    correction_input = tmp_path / "duplicate-resource-correction.json"
    correction_input.write_text(json.dumps(payload))

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "correction",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(required["generation"]),
            "--action-id",
            required["action_id"],
            "--evidence-hash",
            required["evidence_hash"],
            "--input",
            str(correction_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 2
    assert rejected["errors"][0]["code"] == "unauthorized_asset_reference"


def test_raw_resource_scanner_has_a_deterministic_unclosed_bracket_budget(
    tmp_path, capsys, monkeypatch
):
    bundle, converted, dependencies = converted_bundle(
        tmp_path,
        capsys,
        monkeypatch,
        markdown=b"[" * 20_000 + b"\n",
    )
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()

    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "asset_scan_budget_exceeded"
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before


@pytest.mark.parametrize("target_kind", ["raw_conversion", "corrected_markdown"])
def test_finalization_revalidates_local_resource_snapshot_after_input_load(
    tmp_path, capsys, monkeypatch, target_kind
):
    markdown = b"![Figure](assets/figure.png) Raw body.\n"
    if target_kind == "raw_conversion":
        bundle, converted, dependencies = converted_bundle(
            tmp_path,
            capsys,
            monkeypatch,
            markdown=markdown,
            assets=[("nested/assets/figure.png", PNG_1X1)],
            dependency_installer=install_real_pandoc_review_dependencies,
        )
        rc, pending, stderr = raw_test.invoke(
            capsys,
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(converted["generation"]),
                "--visual-capability",
                "available",
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
        assert rc == 0, (pending, stderr)
    else:
        bundle, required, dependencies = correction_required_bundle(
            tmp_path,
            capsys,
            monkeypatch,
            markdown=markdown,
            assets=[("nested/assets/figure.png", PNG_1X1)],
            dependency_installer=install_real_pandoc_review_dependencies,
        )
        payload = simple_correction_payload()
        anchor_start = markdown.index(b"Raw body.")
        payload["corrections"][0]["anchor"].update(
            {
                "start_byte": anchor_start,
                "end_byte": anchor_start + len(b"Raw body."),
            }
        )
        correction_input = tmp_path / "snapshot-correction.json"
        correction_input.write_text(json.dumps(payload))
        rc, pending, stderr = raw_test.invoke(
            capsys,
            [
                "record",
                "correction",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(required["generation"]),
                "--action-id",
                required["action_id"],
                "--evidence-hash",
                required["evidence_hash"],
                "--input",
                str(correction_input),
            ],
            cwd=tmp_path,
            environ=dependencies,
            transport=raw_test.NeverNetwork(),
        )
        assert rc == 0, (pending, stderr)

    review_input = tmp_path / f"{target_kind}-complete.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    asset_path = next((bundle / "03-converted").glob("**/assets/figure.png"))
    history_before = (bundle / ".state" / "history.ndjson").read_bytes()
    original_loader = review_module.load_record_input

    def load_then_tamper(*args, **kwargs):
        loaded = original_loader(*args, **kwargs)
        asset_path.write_bytes(PNG_1X1 + b"tampered")
        return loaded

    monkeypatch.setattr(review_module, "load_record_input", load_then_tamper)
    rc, rejected, _stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )

    assert rc == 4
    assert rejected["errors"][0]["code"] == "integrity_violation"
    assert rejected["generation"] == pending["generation"]
    assert (bundle / ".state" / "history.ndjson").read_bytes() == history_before

    monkeypatch.setattr(review_module, "load_record_input", original_loader)
    rc, inspected, _stderr = raw_test.invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 4
    assert inspected["errors"][0]["code"] == "integrity_violation"


def _bundle_history_state(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    return manifest, private_state, history


def test_pending_conversion_history_resolver_understands_review_events(
    tmp_path, capsys, monkeypatch
):
    # `conversion_attempt.recover_interrupted_attempt` replays the durable
    # prefix with whatever reducer `workflow` hands it, so that reducer has to
    # understand every event the bundle can hold. The ladder must therefore
    # follow the manifest's own layering rather than stopping at raw
    # conversion, or a review-bearing bundle would be handed a reducer that
    # hard-fails on its own history.
    bundle, converted, dependencies = converted_bundle(tmp_path, capsys, monkeypatch)

    manifest, private_state, history = _bundle_history_state(bundle)
    assert "review" not in manifest
    # Before any review event the review reducer is a verbatim delegation, so
    # adding the rung cannot change how existing histories are replayed.
    assert review_module.resolve_history_state(
        history, manifest_template=manifest, private_template=private_state
    ) == raw_conversion_module.resolve_history_state(
        history, manifest_template=manifest, private_template=private_state
    )

    rc, pending, stderr = raw_test.invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(converted["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (pending, stderr)
    review_input = tmp_path / "resolver-review-input.json"
    review_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "local_complete",
                "segments": [
                    {
                        "segment_id": "segment-0001",
                        "source_pages": {"start": 1, "end": 1},
                        "markdown_blocks": ["block-000001"],
                        "checks": passing_checks(),
                    }
                ],
                "boundaries": [],
                "findings": [],
                "page_misc": [],
                "absence_basis": [],
            }
        )
    )
    rc, completed, stderr = raw_test.invoke(
        capsys,
        [
            "record",
            "review",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
            "--action-id",
            pending["action_id"],
            "--evidence-hash",
            pending["evidence_hash"],
            "--input",
            str(review_input),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=raw_test.NeverNetwork(),
    )
    assert rc == 0, (completed, stderr)

    manifest, private_state, history = _bundle_history_state(bundle)
    assert "review" in manifest
    # The raw reducer -- the deepest rung the ladder used to reach -- cannot
    # replay this history at all.
    assert (
        raw_conversion_module.resolve_history_state(
            history, manifest_template=manifest, private_template=private_state
        )
        is None
    )
    resolver = workflow_module._conversion_history_resolver(manifest)
    assert (
        resolver(
            history, manifest_template=manifest, private_template=private_state
        )
        is not None
    )
