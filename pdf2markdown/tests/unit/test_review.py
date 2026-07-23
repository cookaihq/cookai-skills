import io
import json
from pathlib import Path

import pytest
import review as review_module

import test_raw_conversion as raw_test


INSTALL_PREFLIGHT_DEPENDENCIES = raw_test.install_preflight_dependencies


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
        "        end = f'{end_line}:{max(1, len(lines[-1]))}'\n"
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


def converted_bundle(
    tmp_path,
    capsys,
    monkeypatch,
    *,
    page_count=1,
    markdown=None,
    interaction_mode="confirm",
):
    monkeypatch.setattr(
        raw_test, "install_preflight_dependencies", install_review_dependencies
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
    archive = raw_test.make_zip([(f"nested/{request_filename}.md", markdown)])
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
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["review"]["reason_code"] is None
    assert manifest["final_markdown"] is None
    assert raw_path.read_bytes() == raw_before
    review = json.loads((bundle / "04-review" / "review.json").read_text())
    assert review["rounds"][0]["findings"][0]["finding_id"] == "finding-0001"
    assert not list((bundle / "04-review").glob("*.corrected.md"))


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
    assert rc == 0, (blocked, stderr)
    assert blocked["outcome"] == "correction_required"
    assert blocked["conversion_state"] == "review_pending"
    assert blocked["final_markdown"] is None


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
