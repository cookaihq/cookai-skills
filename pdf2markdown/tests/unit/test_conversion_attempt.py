"""Command-level contracts for Doc2X conversion attempts."""

import hashlib
import io
import itertools
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
import conversion_attempt
import raw_conversion
import workflow
from test_raw_conversion import ArchiveTransport, make_zip, ready_result_bundle


NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
AIHUB_UPLOAD_URL = "https://api.aihubmax.com/v1/files/upload/stream"
DOC2X_CREATE_URL = "https://api.aihubmax.com/v1/run/generations"


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class NeverNetwork:
    def __call__(self, *_args, **_kwargs):
        raise AssertionError("network access is not expected")


class CountingNeverNetwork(NeverNetwork):
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(("__call__", args, kwargs))
        return super().__call__(*args, **kwargs)

    def resolve(self, *args, **kwargs):
        self.calls.append(("resolve", args, kwargs))
        raise AssertionError("network access is not expected")

    def connect_https(self, *args, **kwargs):
        self.calls.append(("connect_https", args, kwargs))
        raise AssertionError("network access is not expected")


class SuccessfulUpload:
    def __init__(self, url):
        self.url = url
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        return Response(200, json.dumps({"url": self.url}).encode("utf-8"))


class SuccessfulCreate:
    def __init__(self, task_id):
        self.task_id = task_id
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        return Response(200, json.dumps({"id": self.task_id}).encode("utf-8"))


class PollStatus:
    def __init__(self, task_id, status, *, results=None, error=None):
        self.task_id = task_id
        self.status = status
        self.results = results
        self.error = error
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        payload = {"status": self.status}
        if self.results is not None:
            payload["results"] = self.results
        if self.error is not None:
            payload["error"] = self.error
        return Response(200, json.dumps(payload).encode("utf-8"))


class StatusCreate:
    def __init__(self, status, body=b'{"error":"upstream"}'):
        self.status = status
        self.body = body
        self.calls = 0

    def __call__(self, _method, _url, _headers, _body):
        self.calls += 1
        return Response(self.status, self.body)


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterCreate:
    def __init__(self):
        self.calls = 0

    def __call__(self, _method, _url, _headers, _body):
        self.calls += 1
        raise SimulatedProcessCrash


class LostPoll:
    def __init__(self):
        self.calls = 0

    def __call__(self, _method, _url, _headers, _body):
        self.calls += 1
        raise OSError("poll response was lost")


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


def ready_staged_bundle(
    tmp_path, capsys, monkeypatch, *, risk_codes=None, interaction_mode=None
):
    risk_codes = [] if risk_codes is None else risk_codes
    interaction_mode = interaction_mode or ("auto" if risk_codes else "confirm")
    source = tmp_path / "input.pdf"
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 18), "Doc2X submission")
    document.save(source)
    document.close()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    dependencies = install_preflight_dependencies(tmp_path, monkeypatch)

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
        transport=NeverNetwork(),
    )
    assert start_rc == 0
    bundle = Path(started["work_bundle"])
    advance_rc, advanced, _stderr = invoke(
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
    assert advance_rc == 0
    record_path = tmp_path / "preflight.json"
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "warning" if risk_codes else "pass",
                "pages": [
                    {
                        "page_number": 1,
                        "classification": "risk" if risk_codes else "content",
                        "risk_codes": risk_codes,
                        "evidence": ["The page is readable."],
                    }
                ],
            }
        )
    )
    record_rc, recorded, _stderr = invoke(
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
            str(record_path),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert record_rc == 0

    key = "test-aihub-key-123456"
    source_url = "https://files.aihubmax.com/source.pdf?token=private-bearer"
    upload = SuccessfulUpload(source_url)
    stage_rc, staged, _stderr = invoke(
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
    assert stage_rc == 0
    assert staged["source_upload_state"] == "source_upload_ready"
    assert len(upload.calls) == 1
    return bundle, staged, dependencies, key, source_url, source_sha256


def test_resume_submits_one_fixed_doc2x_request_and_persists_its_task_identity(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, source_url, source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-001")

    rc, result, stderr = invoke(
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
    assert result["outcome"] == "conversion_submitted"
    assert result["conversion_state"] == "submitted"
    assert result["conversion_attempt_state"] == "submitted"
    assert result["action_required"] is None
    assert len(create.calls) == 1
    method, url, headers, body = create.calls[0]
    assert method == "POST"
    assert url == DOC2X_CREATE_URL
    assert headers["Authorization"] == f"Bearer {key}"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "model": "doc2x-v3",
        "pdf_url": source_url,
        "page_count": 1,
        "filename": f"document-{source_sha256[:8]}",
        "convert_mode": "md",
        "formula_mode": "dollar",
        "merge_cross_page_forms": False,
    }

    manifest_text = (bundle / "manifest.json").read_text()
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["conversion_attempts"][-1]["attempt_id"] == "conversion-attempt-0001"
    assert manifest["conversion_attempts"][-1]["state"] == "submitted"
    assert manifest["conversion_attempts"][-1]["task_id"] == "task-001"
    assert manifest["conversion_attempts"][-1]["request_summary"]["pdf_url_sha256"] == (
        "sha256:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    )
    public_output = json.dumps(result) + stderr + manifest_text + history_text
    assert key not in public_output
    assert source_url not in public_output


def test_cross_page_table_preflight_evidence_enables_the_fixed_merge_option(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(
            tmp_path, capsys, monkeypatch, risk_codes=["cross_page_table"]
        )
    )
    create = SuccessfulCreate("task-cross-page")

    rc, result, _stderr = invoke(
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
    assert result["conversion_state"] == "submitted"
    assert len(create.calls) == 1
    assert json.loads(create.calls[0][3])["merge_cross_page_forms"] is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b'{"error":"upstream"}'),
        (201, b'{"id":"unexpected-created"}'),
        (202, b'{"id":"unexpected-accepted"}'),
        (200, json.dumps({"id": "unsafe\nidentity"}).encode("utf-8")),
        (200, json.dumps({"id": "t" * 257}).encode("utf-8")),
    ],
)
def test_a_create_response_without_a_safe_id_is_unknown_and_resume_never_replays_it(
    tmp_path, capsys, monkeypatch, status, body
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = StatusCreate(status, body)

    rc, unknown, _stderr = invoke(
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
    assert unknown["outcome"] == "submission_unknown"
    assert unknown["conversion_state"] == "submission_unknown"
    assert unknown["conversion_attempt_state"] == "submission_unknown"
    assert unknown["action_required"] == "resolve_submission_unknown"
    assert unknown["action_id"]
    assert unknown["evidence_hash"].startswith("sha256:")
    assert create.calls == 1

    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unknown["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )

    assert resumed_rc == 0, json.dumps(resumed, sort_keys=True)
    assert resumed["conversion_state"] == "submission_unknown"
    assert resumed["generation"] == unknown["generation"]
    assert resumed["action_id"] == unknown["action_id"]
    assert create.calls == 1


def test_confirm_retry_action_appends_a_new_attempt_before_a_second_create(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    first = StatusCreate(401)
    _rc, unknown, _stderr = invoke(
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
        transport=first,
    )
    old_attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][0]

    decision_rc, authorized, _stderr = invoke(
        capsys,
        [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unknown["generation"]),
            "--action-id",
            unknown["action_id"],
            "--evidence-hash",
            unknown["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "I accept the possible duplicate conversion charge.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert decision_rc == 0
    assert authorized["outcome"] == "conversion_retry_authorized"
    assert authorized["conversion_state"] == "ready_to_submit"
    assert [item["state"] for item in manifest["conversion_attempts"]] == [
        "submission_unknown",
        # Task 2.1c folds the retry placeholder's flat "not_started" onto
        # "authorized"; the row is otherwise unchanged.
        "authorized",
    ]
    assert manifest["conversion_attempts"][0] == old_attempt

    second = SuccessfulCreate("task-002")
    second_rc, submitted, _stderr = invoke(
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
        transport=second,
    )

    assert second_rc == 0
    assert submitted["conversion_state"] == "submitted"
    assert len(second.calls) == 1
    final_manifest = json.loads((bundle / "manifest.json").read_text())
    assert [item["state"] for item in final_manifest["conversion_attempts"]] == [
        "submission_unknown",
        "submitted",
    ]
    assert final_manifest["conversion_attempts"][0] == old_attempt
    assert final_manifest["conversion_attempts"][1]["attempt_id"] == (
        "conversion-attempt-0002"
    )


def test_a_process_crash_after_create_recovers_unknown_and_two_resumes_do_not_replay(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = CrashAfterCreate()

    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=create,
            now=NOW,
        )
    capsys.readouterr()
    assert create.calls == 1

    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"] + 1),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert recovered_rc == 0
    assert recovered["conversion_state"] == "submission_unknown"
    assert recovered["action_required"] == "resolve_submission_unknown"

    second_rc, second, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(recovered["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert second_rc == 0
    assert second["generation"] == recovered["generation"]
    assert second["conversion_state"] == "submission_unknown"


def test_a_crash_after_submit_intent_recovers_unknown_without_ever_sending_create(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("must-not-be-created")
    original_atomic_write = conversion_attempt.bundle.atomic_write_json
    crashed = False

    def crash_once(name, value, *, dir_fd):
        nonlocal crashed
        if not crashed and name == "private.json":
            crashed = True
            raise SimulatedProcessCrash
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(conversion_attempt.bundle, "atomic_write_json", crash_once)
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=create,
            now=NOW,
        )
    capsys.readouterr()
    assert create.calls == []

    recovered_rc, recovered, _stderr = invoke(
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
        transport=NeverNetwork(),
    )

    assert recovered_rc == 0
    assert recovered["conversion_state"] == "submission_unknown"
    assert recovered["action_required"] == "resolve_submission_unknown"
    manifest = json.loads((bundle / "manifest.json").read_text())
    # schema v2 (task 2.1b): the folded `reason` is single-valued per state,
    # and the branch the wire actually took survives in `reason_detail`.
    assert manifest["conversion_attempts"][0]["reason"] == "no_task_id"
    assert manifest["conversion_attempts"][0]["reason_detail"] == (
        "interrupted_before_result_commit"
    )
    assert create.calls == []


def test_a_crash_after_safe_create_result_recovers_the_same_submitted_task(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-durable-result")
    original_atomic_write = conversion_attempt.bundle.atomic_write_json
    private_writes = 0

    def crash_on_result_write(name, value, *, dir_fd):
        nonlocal private_writes
        if name == "private.json":
            private_writes += 1
            if private_writes == 2:
                raise SimulatedProcessCrash
        return original_atomic_write(name, value, dir_fd=dir_fd)

    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", crash_on_result_write
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=create,
            now=NOW,
        )
    capsys.readouterr()
    assert len(create.calls) == 1

    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"] + 1),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )

    assert recovered_rc == 0
    assert recovered["outcome"] == "conversion_submitted"
    assert recovered["conversion_state"] == "submitted"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["task_id"] == (
        "task-durable-result"
    )
    assert len(create.calls) == 1


def test_resume_polls_only_the_submitted_task_and_persists_processing_progress(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-processing")
    _create_rc, submitted, _stderr = invoke(
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
    poll = PollStatus("task-processing", "processing")

    poll_rc, processing, _stderr = invoke(
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
    assert processing["outcome"] == "conversion_processing"
    assert processing["conversion_state"] == "submitted"
    assert processing["conversion_attempt_state"] == "processing"
    assert len(poll.calls) == 1
    method, url, headers, body = poll.calls[0]
    assert method == "GET"
    assert url == (
        "https://api.aihubmax.com/v1/tasks/task-processing?sync_upstream=true"
    )
    assert headers["Authorization"] == f"Bearer {key}"
    assert body == b""
    manifest = json.loads((bundle / "manifest.json").read_text())
    attempt = manifest["conversion_attempts"][-1]
    assert attempt["task_id"] == "task-processing"
    assert attempt["poll_count"] == 1
    assert attempt["state"] == "processing"
    assert len(create.calls) == 1


def test_completed_task_with_one_https_result_keeps_the_full_url_private(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-result")
    _create_rc, submitted, _stderr = invoke(
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
    result_url = "https://results.aihubmax.com/result.zip?token=signed-private"
    poll = PollStatus(
        "task-result", "completed", results=[{"url": result_url}]
    )

    poll_rc, ready, stderr = invoke(
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
    assert ready["outcome"] == "result_ready"
    assert ready["conversion_state"] == "result_downloading"
    assert ready["conversion_attempt_state"] == "result_ready"
    assert ready["action_required"] is None
    manifest_text = (bundle / "manifest.json").read_text()
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    manifest = json.loads(manifest_text)
    expected_hash = "sha256:" + hashlib.sha256(result_url.encode("utf-8")).hexdigest()
    assert manifest["conversion_attempts"][-1]["result_url_sha256"] == expected_hash
    assert manifest["conversion_attempts"][-1]["request_summary"]["filename"].startswith(
        "document-"
    )
    assert private_state["result_urls"] == [
        {
            "attempt_id": "conversion-attempt-0001",
            "task_id": "task-result",
            "url": result_url,
            "url_sha256": expected_hash,
            "observed_at": "2024-01-02T03:04:05Z",
            "expires_at": None,
            "validity_window_hours": 24,
        }
    ]
    public_output = json.dumps(ready) + stderr + manifest_text + history_text
    assert result_url not in public_output
    assert key not in public_output
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "inspect",
            "--work-bundle",
            str(bundle),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert resumed_rc == 0, json.dumps(resumed, sort_keys=True)
    assert resumed["outcome"] == "inspected"
    assert resumed["generation"] == ready["generation"]


def test_expired_result_reference_blocks_adoption_before_any_network(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-expired-result")
    _create_rc, submitted, _stderr = invoke(
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
    result_url = "https://results.aihubmax.com/result.zip?token=signed-private"
    poll = PollStatus(
        "task-expired-result", "completed", results=[{"url": result_url}]
    )
    poll_rc, ready, _stderr = invoke(
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
    assert ready["outcome"] == "result_ready"
    assert ready["conversion_state"] == "result_downloading"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["result_observed_at"] == (
        "2024-01-02T03:04:05Z"
    )
    assert manifest["conversion_attempts"][-1]["result_validity_hours"] == 24

    never = CountingNeverNetwork()
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
        transport=never,
        now=datetime(2024, 1, 3, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert never.calls == []
    assert expired_rc == 0, expired
    assert expired["outcome"] == "result_url_unavailable"
    assert expired["conversion_state"] == "recoverable_error"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["raw_conversion"]["reason_code"] == "result_url_unavailable"


FIRST_RESULT_URL = "https://results.aihubmax.com/result.zip?token=first-private"
SECOND_RESULT_URL = "https://results.aihubmax.com/result.zip?token=second-private"
FIRST_EXPIRY = datetime(2024, 1, 3, 3, 4, 5, tzinfo=timezone.utc)
SECOND_EXPIRY = datetime(2024, 1, 4, 3, 4, 5, tzinfo=timezone.utc)


def renewed_expired_result_bundle(tmp_path, capsys, monkeypatch):
    """Drive one attempt through ready, expiry, renewal, expiry, and repeat.

    The same Doc2X task is polled again after each local expiry. The first
    refresh answers with a different URL; the second answers with the exact
    same URL that is already recorded and already expired.
    """
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    def resume(expected_generation, *, transport, now=NOW):
        rc, result, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(expected_generation),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=transport,
            now=now,
        )
        assert rc == 0, json.dumps(result, sort_keys=True)
        return result

    submitted = resume(
        staged["generation"], transport=SuccessfulCreate("task-renewal")
    )
    ready = resume(
        submitted["generation"],
        transport=PollStatus(
            "task-renewal", "completed", results=[{"url": FIRST_RESULT_URL}]
        ),
    )
    first_expiry_network = CountingNeverNetwork()
    first_expired = resume(
        ready["generation"], transport=first_expiry_network, now=FIRST_EXPIRY
    )
    renewal_poll = PollStatus(
        "task-renewal", "completed", results=[{"url": SECOND_RESULT_URL}]
    )
    renewed = resume(
        first_expired["generation"], transport=renewal_poll, now=FIRST_EXPIRY
    )
    second_expiry_network = CountingNeverNetwork()
    second_expired = resume(
        renewed["generation"], transport=second_expiry_network, now=SECOND_EXPIRY
    )
    repeat_poll = PollStatus(
        "task-renewal", "completed", results=[{"url": SECOND_RESULT_URL}]
    )
    repeated = resume(
        second_expired["generation"], transport=repeat_poll, now=SECOND_EXPIRY
    )
    return {
        "bundle": bundle,
        "environ": environ,
        "resume": resume,
        "ready": ready,
        "first_expired": first_expired,
        "first_expiry_network": first_expiry_network,
        "renewal_poll": renewal_poll,
        "renewed": renewed,
        "second_expired": second_expired,
        "second_expiry_network": second_expiry_network,
        "repeat_poll": repeat_poll,
        "repeated": repeated,
    }


def test_locally_expired_reference_refreshes_same_task_and_same_url_does_not_extend(
    tmp_path, capsys, monkeypatch
):
    driven = renewed_expired_result_bundle(tmp_path, capsys, monkeypatch)
    bundle = driven["bundle"]
    first_sha256 = (
        "sha256:" + hashlib.sha256(FIRST_RESULT_URL.encode("utf-8")).hexdigest()
    )
    second_sha256 = (
        "sha256:" + hashlib.sha256(SECOND_RESULT_URL.encode("utf-8")).hexdigest()
    )

    # Both local expiries are decided without touching the network.
    assert driven["first_expiry_network"].calls == []
    assert driven["second_expiry_network"].calls == []
    assert driven["first_expired"]["outcome"] == "result_url_unavailable"
    assert driven["second_expired"]["outcome"] == "result_url_unavailable"

    # Branch one: a different URL renews the reference on the same task.
    renewed = driven["renewed"]
    assert renewed["outcome"] == "result_ready"
    assert renewed["conversion_state"] == "result_downloading"
    assert len(driven["renewal_poll"].calls) == 1
    assert driven["renewal_poll"].calls[0][0] == "GET"
    assert "task-renewal" in driven["renewal_poll"].calls[0][1]
    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    renewed_attempt = manifest["conversion_attempts"][-1]
    assert len(manifest["conversion_attempts"]) == 1
    assert renewed_attempt["task_id"] == "task-renewal"
    assert renewed_attempt["result_url_sha256"] == second_sha256
    assert renewed_attempt["result_observed_at"] == "2024-01-03T03:04:05Z"
    assert renewed_attempt["result_validity_hours"] == 24
    assert [record["url"] for record in private_state["result_urls"]] == [
        FIRST_RESULT_URL,
        SECOND_RESULT_URL,
    ]
    assert private_state["result_urls"][0]["url_sha256"] == first_sha256
    assert private_state["result_urls"][1]["url_sha256"] == second_sha256
    assert private_state["result_urls"][1]["observed_at"] == "2024-01-03T03:04:05Z"
    assert (
        conversion_attempt.result_reference_is_expired(
            renewed_attempt, at="2024-01-03T03:04:05Z"
        )
        is False
    )

    # Branch two: the identical URL neither extends nor appends a version.
    repeated = driven["repeated"]
    assert repeated["outcome"] == "result_ready"
    assert repeated["conversion_state"] == "result_downloading"
    assert len(driven["repeat_poll"].calls) == 1
    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    repeated_attempt = manifest["conversion_attempts"][-1]
    assert len(manifest["conversion_attempts"]) == 1
    assert repeated_attempt["result_url_sha256"] == second_sha256
    assert repeated_attempt["result_observed_at"] == "2024-01-03T03:04:05Z"
    assert repeated_attempt["result_validity_hours"] == 24
    assert [record["url"] for record in private_state["result_urls"]] == [
        FIRST_RESULT_URL,
        SECOND_RESULT_URL,
    ]
    assert (
        conversion_attempt.result_reference_is_expired(
            repeated_attempt, at="2024-01-04T03:04:05Z"
        )
        is True
    )
    public_state = (bundle / "manifest.json").read_text() + (
        bundle / ".state" / "history.ndjson"
    ).read_text()
    assert FIRST_RESULT_URL not in public_state
    assert SECOND_RESULT_URL not in public_state


def test_unrenewed_result_reference_stops_refreshing_the_same_task(
    tmp_path, capsys, monkeypatch
):
    driven = renewed_expired_result_bundle(tmp_path, capsys, monkeypatch)
    bundle = driven["bundle"]
    resume = driven["resume"]

    # Every further resume must converge: the same task has already answered
    # with the same, already expired URL, so there is nothing left to fetch.
    never = CountingNeverNetwork()
    generation = driven["repeated"]["generation"]
    outcomes = []
    generations = []
    for _ in range(6):
        result = resume(generation, transport=never, now=SECOND_EXPIRY)
        outcomes.append(result["outcome"])
        generation = result["generation"]
        generations.append(generation)

    assert never.calls == []
    assert outcomes == ["result_url_not_renewed"] * 6
    assert generations == [generations[0]] * 6
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_state"] == "terminal_error"
    assert manifest["raw_conversion"]["reason_code"] == "result_url_not_renewed"
    assert manifest["raw_conversion"]["state"] == "rejected"
    assert [record["reason_code"] for record in manifest["raw_conversions"]] == [
        "result_url_unavailable",
        "result_url_unavailable",
        "result_url_not_renewed",
    ]
    assert len(manifest["conversion_attempts"]) == 1
    assert manifest["conversion_attempts"][-1]["state"] == "result_ready"
    # Critical fix (task 2.2c review round 1): the terminal ledger verdict is
    # detected locally, but not by the expiry check -- it must not be folded
    # onto LOCALLY_DETECTED_PAIRS' ("result_ready", "result_url_expired"),
    # which is reserved for the recoverable, resumable member. Decision 9.3's
    # exit for this reason code belongs to a later task (3.1c); until then the
    # attempt's reason stays untouched (None).
    assert manifest["raw_conversion"]["detected_by"] == "local_ledger"
    assert manifest["conversion_attempts"][-1]["reason"] is None


def test_result_url_not_renewed_is_classified_only_as_a_ledger_rejection():
    # result_url_not_renewed is deliberately kept out of the archive
    # rejection sets and lives only in LEDGER_RESULT_REJECTIONS. If it were
    # folded into RECOVERABLE_ARCHIVE_REJECTIONS,
    # _reference_already_unavailable would self-match its own terminal
    # record and the resume loop would never converge (the livelock this
    # reason code exists to close). If it were folded into
    # DETERMINISTIC_ARCHIVE_REJECTIONS, the archive-exception branches would
    # be able to report this reason code even though it is never derived
    # from a ResultArchiveError.
    assert (
        "result_url_not_renewed"
        not in raw_conversion.RECOVERABLE_ARCHIVE_REJECTIONS
    )
    assert (
        "result_url_not_renewed"
        not in raw_conversion.DETERMINISTIC_ARCHIVE_REJECTIONS
    )
    assert "result_url_not_renewed" in raw_conversion.LEDGER_RESULT_REJECTIONS


def test_conversion_attempt_error_during_expiry_check_is_translated_with_context(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-malformed-validity-hours")
    _create_rc, submitted, _stderr = invoke(
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
    result_url = "https://results.aihubmax.com/result.zip?token=signed-private"
    poll = PollStatus(
        "task-malformed-validity-hours", "completed", results=[{"url": result_url}]
    )
    poll_rc, ready, _stderr = invoke(
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
    assert ready["conversion_state"] == "result_downloading"

    # A malformed-but-load-tolerant value: 24.0 passes every `== 24`
    # equality check performed while loading/validating the bundle
    # (including the history-replay comparison, since 24 == 24.0 in
    # Python) but fails the stricter `type(...) is not int` guard inside
    # conversion_attempt.result_reference_is_expired.
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["conversion_attempts"][-1]["result_validity_hours"] = 24.0
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )

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
        transport=NeverNetwork(),
    )

    assert rc == 4, json.dumps(result, sort_keys=True)
    assert result["errors"][0]["code"] == "integrity_violation"
    assert result["errors"][0]["code"] != "internal_error", json.dumps(
        result, sort_keys=True
    )
    assert result["work_bundle"] == str(bundle)
    assert result["generation"] == ready["generation"]
    assert result["conversion_state"] == "result_downloading"


def test_completed_task_without_a_nonempty_result_stays_pending_on_the_same_task(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-empty-result")
    _create_rc, submitted, _stderr = invoke(
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
    poll = PollStatus("task-empty-result", "completed", results=[])

    poll_rc, pending, _stderr = invoke(
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
    assert pending["outcome"] == "result_pending"
    assert pending["conversion_state"] == "submitted"
    # Task 2.1c folds flat `result_pending` onto ("processing", None), the
    # pair it shares with `pending` and `processing`. `upstream_status` is
    # what still tells the three apart (design.md Decision 1 note 3), so it
    # is asserted here rather than dropped -- without it this row would no
    # longer distinguish an empty completed result from an ordinary in-flight
    # poll, which is exactly what the test is about.
    assert pending["conversion_attempt_state"] == "processing"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] is None
    assert manifest["conversion_attempts"][-1]["upstream_status"] == "completed"
    assert (
        manifest["conversion_attempts"][-1]["result_pending_deadline_at"]
        is not None
    )
    assert manifest["conversion_attempts"][-1]["task_id"] == "task-empty-result"
    assert len(manifest["conversion_attempts"]) == 1
    assert len(poll.calls) == 1


@pytest.mark.parametrize(
    ("results", "expected_state"),
    [
        (
            [
                {"url": "https://results.aihubmax.com/a.zip?token=one"},
                {"url": "https://results.aihubmax.com/b.zip?token=two"},
            ],
            "unexpected_result_count",
        ),
        ([{"url": "http://results.aihubmax.com/unsafe.zip"}], "unsafe_result_url"),
        ([{"url": 123}], "unsafe_result_url"),
        ([{}], "unsafe_result_url"),
        ([{"url": ""}], "unsafe_result_url"),
    ],
)
def test_completed_task_with_unsafe_or_ambiguous_results_stops_without_guessing(
    tmp_path, capsys, monkeypatch, results, expected_state
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-unsafe-result")
    _create_rc, submitted, _stderr = invoke(
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
    poll = PollStatus("task-unsafe-result", "completed", results=results)

    poll_rc, stopped, _stderr = invoke(
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
    assert stopped["outcome"] == expected_state
    assert stopped["conversion_state"] == "terminal_error"
    # Both parametrized rows fold onto `failed`; `reason` carries the flat
    # name that used to sit in conversion_attempt_state, so asserting both
    # keeps the row's identity pinned exactly as tightly as before.
    assert stopped["conversion_attempt_state"] == "failed"
    assert json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["reason"] == expected_state
    assert stopped["action_required"] == (
        "resolve_unexpected_result_count"
        if expected_state == "unexpected_result_count"
        else None
    )
    assert json.loads((bundle / ".state" / "private.json").read_text())[
        "result_urls"
    ] == []
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(stopped["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert resumed_rc == 0
    if expected_state == "unexpected_result_count":
        assert resumed["conversion_state"] == "terminal_error"
        assert resumed["generation"] == stopped["generation"]
    else:
        assert resumed["conversion_state"] == "recoverable_error"
        assert resumed["conversion_attempt_state"] == "failed"
        assert json.loads((bundle / "manifest.json").read_text())[
            "conversion_attempts"
        ][-1]["reason"] == "poll_transient"


def test_confirm_can_authorize_a_new_attempt_after_unexpected_result_count(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-many-results"),
    )
    _poll_rc, stopped, _stderr = invoke(
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
        transport=PollStatus(
            "task-many-results",
            "completed",
            results=[
                {"url": "https://results.aihubmax.com/a.zip"},
                {"url": "https://results.aihubmax.com/b.zip"},
            ],
        ),
    )

    rc, authorized, _stderr = invoke(
        capsys,
        [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(stopped["generation"]),
            "--action-id",
            stopped["action_id"],
            "--evidence-hash",
            stopped["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "I accept the possible duplicate conversion charge.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    assert rc == 0
    assert authorized["outcome"] == "conversion_retry_authorized"
    assert authorized["conversion_state"] == "ready_to_submit"


def test_unsafe_result_url_resume_only_repolls_the_same_task(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-unsafe-repoll"),
    )
    _poll_rc, unsafe, _stderr = invoke(
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
        transport=PollStatus(
            "task-unsafe-repoll",
            "completed",
            results=[{"url": "http://results.aihubmax.com/unsafe.zip"}],
        ),
    )
    safe_url = "https://results.aihubmax.com/safe.zip?token=private"
    repoll = PollStatus(
        "task-unsafe-repoll", "completed", results=[{"url": safe_url}]
    )

    rc, ready, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unsafe["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=repoll,
    )

    assert rc == 0
    assert ready["conversion_attempt_state"] == "result_ready"
    assert len(repoll.calls) == 1
    assert "/v1/tasks/task-unsafe-repoll?sync_upstream=true" in repoll.calls[0][1]


def test_failed_task_stops_with_a_bound_confirm_action_and_never_recreates_itself(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-failed")
    _create_rc, submitted, _stderr = invoke(
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
    upstream_secret = "signed-query-secret-must-not-persist"
    poll = PollStatus(
        "task-failed", "failed", error={"message": upstream_secret}
    )

    poll_rc, failed, stderr = invoke(
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
    assert failed["outcome"] == "task_failed"
    assert failed["conversion_state"] == "awaiting_user"
    assert failed["conversion_attempt_state"] == "failed"
    assert failed["action_required"] == "resolve_task_failed"
    assert failed["action_id"]
    public_state = (
        json.dumps(failed)
        + stderr
        + (bundle / "manifest.json").read_text()
        + (bundle / ".state" / "history.ndjson").read_text()
    )
    assert upstream_secret not in public_state

    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(failed["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert resumed_rc == 0
    assert resumed["conversion_attempt_state"] == "failed"
    assert resumed["action_id"] == failed["action_id"]
    assert len(create.calls) == 1


# expected_attempt_reason is deliberately a separate column from
# expected_reason: schema v2 (task 2.1b) renames the persisted reason of the
# drifted-credential state to credential_fingerprint_changed, while the flat
# state name -- and with it outcome/conversion_attempt_state -- is untouched
# this substep. Collapsing the two columns back into one would hide exactly
# that rename.
@pytest.mark.parametrize(
    ("poll_environ", "expected_reason", "expected_attempt_reason"),
    [
        ({}, "credential_source_missing", "credential_source_missing"),
        (
            {"AIHUB_API_KEY": "different-key"},
            "credential_source_changed",
            "credential_fingerprint_changed",
        ),
    ],
)
def test_resume_persists_missing_and_drifted_creation_credentials_without_polling(
    tmp_path,
    capsys,
    monkeypatch,
    poll_environ,
    expected_reason,
    expected_attempt_reason,
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-credential-bound")
    _create_rc, submitted, _stderr = invoke(
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

    blocked_rc, blocked, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(submitted["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, **poll_environ},
        transport=NeverNetwork(),
    )

    assert blocked_rc == 0
    assert blocked["outcome"] == expected_reason
    assert blocked["conversion_state"] == "recoverable_error"
    # Folded onto `failed`; the discriminating value is the `reason` asserted
    # on the next line, which the test already pinned before the fold.
    assert blocked["conversion_attempt_state"] == "failed"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] == expected_attempt_reason
    assert manifest["conversion_attempts"][-1]["reason_detail"] is None
    assert manifest["conversion_attempts"][-1]["poll_count"] == 0
    assert len(manifest["conversion_attempts"]) == 1

    poll = PollStatus("task-credential-bound", "processing")
    restored_rc, restored, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(blocked["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=poll,
    )
    assert restored_rc == 0
    assert restored["conversion_attempt_state"] == "processing"
    assert len(poll.calls) == 1
    assert len(create.calls) == 1


# See the credential test above for why expected_attempt_reason is its own
# column: 401 keeps the flat state name poll_unauthorized but persists the
# renamed reason poll_authentication_rejected from schema v2 onward.
@pytest.mark.parametrize(
    ("http_status", "expected_reason", "expected_attempt_reason"),
    [
        (401, "poll_unauthorized", "poll_authentication_rejected"),
        (404, "task_unavailable", "task_unavailable"),
    ],
)
def test_poll_401_and_404_have_distinct_recoverable_reasons_on_the_same_task(
    tmp_path, capsys, monkeypatch, http_status, expected_reason, expected_attempt_reason
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-auth-diagnostic")
    _create_rc, submitted, _stderr = invoke(
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
    rejected = StatusCreate(http_status)

    poll_rc, recoverable, _stderr = invoke(
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
        transport=rejected,
    )

    assert poll_rc == 0
    assert recoverable["outcome"] == expected_reason
    assert recoverable["conversion_state"] == "recoverable_error"
    assert recoverable["conversion_attempt_state"] == "failed"
    assert rejected.calls == 1
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] == expected_attempt_reason
    assert manifest["conversion_attempts"][-1]["reason_detail"] is None
    assert manifest["conversion_attempts"][-1]["task_id"] == (
        "task-auth-diagnostic"
    )

    poll = PollStatus("task-auth-diagnostic", "processing")
    restored_at = (
        datetime(2024, 1, 2, 3, 4, 13, tzinfo=timezone.utc)
        if expected_reason == "task_unavailable"
        else NOW
    )
    restored_rc, restored, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(recoverable["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=poll,
        now=restored_at,
    )
    assert restored_rc == 0
    assert restored["conversion_attempt_state"] == "processing"
    assert len(poll.calls) == 1
    assert len(create.calls) == 1


@pytest.mark.parametrize(
    "poll_failure",
    [
        lambda: StatusCreate(429),
        lambda: StatusCreate(503),
        lambda: StatusCreate(200, b"{not-json"),
        LostPoll,
    ],
)
def test_transient_poll_failures_are_persisted_and_only_the_same_task_is_retried(
    tmp_path, capsys, monkeypatch, poll_failure
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-transient")
    _create_rc, submitted, _stderr = invoke(
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
    failure = poll_failure()

    poll_rc, recoverable, _stderr = invoke(
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
        transport=failure,
    )

    assert poll_rc == 0
    assert recoverable["outcome"] == "poll_transient"
    assert recoverable["conversion_state"] == "recoverable_error"
    assert recoverable["conversion_attempt_state"] == "failed"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] == "poll_transient"
    assert manifest["conversion_attempts"][-1]["reason_detail"] == "poll_transient"
    assert manifest["conversion_attempts"][-1]["poll_count"] == 1
    assert manifest["conversion_attempts"][-1]["next_poll_at"] == (
        "2024-01-02T03:04:13Z"
    )

    poll = PollStatus("task-transient", "processing")
    restored_rc, restored, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(recoverable["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=poll,
        now=datetime(2024, 1, 2, 3, 4, 13, tzinfo=timezone.utc),
    )
    assert restored_rc == 0
    assert restored["conversion_attempt_state"] == "processing"
    assert len(poll.calls) == 1
    assert len(create.calls) == 1


def test_processing_poll_window_expires_without_an_extra_network_request(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-poll-timeout")
    _create_rc, submitted, _stderr = invoke(
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
    first_poll = PollStatus("task-poll-timeout", "processing")
    _poll_rc, processing, _stderr = invoke(
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
        transport=first_poll,
        now=NOW,
    )
    attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]
    assert attempt["poll_deadline_at"] == "2024-01-02T03:16:05Z"

    timeout_rc, timed_out, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(processing["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
        now=datetime(2024, 1, 2, 3, 16, 5, tzinfo=timezone.utc),
    )

    assert timeout_rc == 0
    assert timed_out["outcome"] == "poll_timeout"
    assert timed_out["conversion_state"] == "recoverable_error"
    assert timed_out["conversion_attempt_state"] == "failed"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] == "poll_timeout"
    assert manifest["conversion_attempts"][-1]["reason_detail"] is None
    assert manifest["conversion_attempts"][-1]["task_id"] == "task-poll-timeout"
    assert len(first_poll.calls) == 1
    assert len(create.calls) == 1


def test_transient_poll_backoff_is_persisted_exponential_and_zero_network_until_due(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-backoff"),
    )
    first_failure = StatusCreate(503)
    _first_rc, first, _stderr = invoke(
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
        transport=first_failure,
        now=NOW,
    )
    before = (bundle / "manifest.json").read_bytes()
    early = PollStatus("task-backoff", "processing")

    early_rc, waiting, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(first["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=early,
        now=datetime(2024, 1, 2, 3, 4, 12, tzinfo=timezone.utc),
    )

    assert early_rc == 0
    assert waiting["outcome"] == "poll_backoff"
    assert waiting["generation"] == first["generation"]
    assert early.calls == []
    assert (bundle / "manifest.json").read_bytes() == before

    second_failure = StatusCreate(503)
    _second_rc, second, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(first["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=second_failure,
        now=datetime(2024, 1, 2, 3, 4, 13, tzinfo=timezone.utc),
    )
    attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]
    assert second["conversion_attempt_state"] == "failed"
    assert attempt["reason"] == "poll_transient"
    assert attempt["consecutive_transient_count"] == 2
    assert attempt["next_poll_at"] == "2024-01-02T03:04:29Z"

    success = PollStatus("task-backoff", "processing")
    _success_rc, processing, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(second["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=success,
        now=datetime(2024, 1, 2, 3, 4, 29, tzinfo=timezone.utc),
    )
    attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]
    assert processing["conversion_attempt_state"] == "processing"
    assert attempt["consecutive_transient_count"] == 0
    assert attempt["next_poll_at"] is None


def test_completed_empty_result_window_has_its_own_bounded_timeout(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-result-timeout")
    _create_rc, submitted, _stderr = invoke(
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
    empty = PollStatus("task-result-timeout", "completed", results=[])
    _poll_rc, pending, _stderr = invoke(
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
        transport=empty,
        now=NOW,
    )
    attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]
    assert attempt["result_pending_deadline_at"] == "2024-01-02T03:16:05Z"

    timeout_rc, timed_out, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(pending["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
        now=datetime(2024, 1, 2, 3, 16, 5, tzinfo=timezone.utc),
    )

    assert timeout_rc == 0
    assert timed_out["outcome"] == "result_pending_timeout"
    assert timed_out["conversion_state"] == "recoverable_error"
    assert timed_out["conversion_attempt_state"] == "failed"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason"] == (
        "result_pending_timeout"
    )
    assert manifest["conversion_attempts"][-1]["reason_detail"] is None
    assert manifest["conversion_attempts"][-1]["task_id"] == (
        "task-result-timeout"
    )
    assert len(empty.calls) == 1
    assert len(create.calls) == 1

    result_url = "https://results.aihubmax.com/after-timeout.zip?token=private"
    available = PollStatus(
        "task-result-timeout", "completed", results=[{"url": result_url}]
    )
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(timed_out["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=available,
        now=datetime(2024, 1, 2, 3, 16, 6, tzinfo=timezone.utc),
    )

    resumed_attempt = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]
    assert resumed_rc == 0
    assert resumed["conversion_attempt_state"] == "result_ready"
    assert resumed_attempt["poll_started_at"] == "2024-01-02T03:16:06Z"
    assert resumed_attempt["poll_deadline_at"] == "2024-01-02T03:28:06Z"
    assert len(available.calls) == 1


def test_completed_task_deduplicates_identical_full_result_urls(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create = SuccessfulCreate("task-duplicate-result")
    _create_rc, submitted, _stderr = invoke(
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
    result_url = "https://results.aihubmax.com/same.zip?token=exact"
    poll = PollStatus(
        "task-duplicate-result",
        "completed",
        results=[{"url": result_url}, {"url": result_url}],
    )

    poll_rc, ready, _stderr = invoke(
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
    assert ready["conversion_attempt_state"] == "result_ready"
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert len(private_state["result_urls"]) == 1
    assert private_state["result_urls"][0]["url"] == result_url


def test_auto_mode_keeps_submission_unknown_stopped_without_an_action_or_replay(
    tmp_path, capsys, monkeypatch
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(
            tmp_path, capsys, monkeypatch, interaction_mode="auto"
        )
    )
    create = StatusCreate(500)

    create_rc, unknown, _stderr = invoke(
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

    assert create_rc == 0
    assert unknown["conversion_state"] == "submission_unknown"
    assert unknown["action_required"] is None
    assert unknown["action_id"] is None
    assert create.calls == 1
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unknown["generation"]),
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=NeverNetwork(),
    )
    assert resumed_rc == 0
    assert resumed["generation"] == unknown["generation"]
    assert resumed["action_required"] is None
    assert create.calls == 1


def _install_conversion_journal_crash(monkeypatch, *, event, boundary):
    original_atomic_write = conversion_attempt.bundle.atomic_write_json
    original_append_history = conversion_attempt.bundle.append_history
    crashed = False

    def atomic_write(name, value, *, dir_fd):
        nonlocal crashed
        if not crashed and boundary == name.removesuffix(".json"):
            crashed = True
            raise SimulatedProcessCrash
        return original_atomic_write(name, value, dir_fd=dir_fd)

    def append_history(value, *, state_fd):
        nonlocal crashed
        if not crashed and boundary == "commit" and value.get("event") == event:
            crashed = True
            raise SimulatedProcessCrash
        return original_append_history(value, state_fd=state_fd)

    monkeypatch.setattr(conversion_attempt.bundle, "atomic_write_json", atomic_write)
    monkeypatch.setattr(conversion_attempt.bundle, "append_history", append_history)
    return original_atomic_write, original_append_history


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
def test_poll_result_journal_recovers_each_write_boundary_without_leaking_result_url(
    tmp_path, capsys, monkeypatch, boundary
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-poll-crash"),
    )
    result_url = "https://results.aihubmax.com/crash.zip?token=private-result"
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch,
        event="conversion_poll_result_committed",
        boundary=boundary,
    )

    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(submitted["generation"]),
            ],
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=PollStatus(
                "task-poll-crash", "completed", results=[{"url": result_url}]
            ),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )

    recovered_rc, recovered, _stderr = invoke(
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
        transport=NeverNetwork(),
    )

    assert recovered_rc == 0, json.dumps(recovered, sort_keys=True)
    expected_state = "failed" if boundary == "private" else "result_ready"
    expected_reason = "poll_transient" if boundary == "private" else None
    assert recovered["conversion_attempt_state"] == expected_state
    assert json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["reason"] == expected_reason
    manifest_text = (bundle / "manifest.json").read_text()
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert result_url not in manifest_text
    assert result_url not in history_text
    if boundary == "private":
        assert private_state["result_urls"] == []
    else:
        assert private_state["result_urls"][-1]["url"] == result_url


REFRESH_TASK_ID = "task-refresh-crash"
REFRESH_FIRST_URL = "https://results.example/refresh.zip?token=refresh-first"
REFRESH_SECOND_URL = "https://results.example/refresh.zip?token=refresh-second"
REFRESH_EXPIRY = datetime(2024, 1, 3, 3, 4, 5, tzinfo=timezone.utc)


def _refresh_ready_bundle(tmp_path, capsys, monkeypatch):
    """Drive one attempt to a result reference that is about to expire."""
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    def resume(expected_generation, *, transport, now=NOW):
        rc, result, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(expected_generation),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=transport,
            now=now,
        )
        assert rc == 0, json.dumps(result, sort_keys=True)
        return result

    submitted = resume(
        staged["generation"], transport=SuccessfulCreate(REFRESH_TASK_ID)
    )
    ready = resume(
        submitted["generation"],
        transport=PollStatus(
            REFRESH_TASK_ID, "completed", results=[{"url": REFRESH_FIRST_URL}]
        ),
    )
    assert ready["outcome"] == "result_ready"
    return bundle, environ, resume, ready


def _crash_resume(tmp_path, bundle, environ, generation, *, transport, now):
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(generation),
            ],
            environ=environ,
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=transport,
            now=now,
        )


# conversion_state, attempt.state, attempt.reason, attempt.reason_detail,
# len(private result_urls) after recovery. Pinning the settled state -- not
# just "rc 0 and no network" -- is what keeps a recovery that silently
# discards a valid decision visible.
#
# schema v2 (task 2.1b) split the single v1 `reason_code` column into two, so
# this table carries both: the downgraded row is the only one where they
# differ, and asserting only one of them would stop distinguishing "recovery
# downgraded to poll_transient because the payload was lost" from "recovery
# downgraded to poll_transient for some other transient reason".
REFRESH_RECOVERY_EXPECTATIONS = {
    # The rebuilt intent carries the reservation's own `at`, so a crash before
    # the intent is durable replays exactly the decision the `intent` boundary
    # replays. Task 2.2c: both boundaries crash strictly before the local-
    # expiry rejection is committed, so recovery is what actually commits it
    # for the first time -- and now that raw_conversion.py's local-expiry
    # branch folds the attempt onto ("result_ready", "result_url_expired"),
    # the recovered reason is that value, not None.
    ("reservation", "new"): (
        "recoverable_error", "result_ready", "result_url_expired", None, 1,
    ),
    ("intent", "new"): (
        "recoverable_error", "result_ready", "result_url_expired", None, 1,
    ),
    ("prepared", "new"): ("converted", "result_ready", None, None, 2),
    # private.json never reached the disk, so the renewed URL is genuinely
    # gone: recovery must downgrade the decision instead of inventing a URL.
    ("private", "new"): (
        "recoverable_error",
        # Task 2.1c folds flat `poll_transient` onto ("failed",
        # "poll_transient"); the reason column below already carried the
        # discriminating value before the fold, so this row stays as tight.
        "failed",
        "poll_transient",
        "result_private_payload_lost",
        1,
    ),
    ("manifest", "new"): ("result_downloading", "result_ready", None, None, 2),
    # A refresh that answers with the same URL appends no new version, so the
    # payload the crash left behind is the record already on disk rather than
    # "one more than before". It is not lost, and recovery must keep the
    # result_ready decision at either boundary.
    ("private", "same"): ("result_downloading", "result_ready", None, None, 1),
    ("manifest", "same"): ("result_downloading", "result_ready", None, None, 1),
}

# The journal event the crash left dangling, so a row cannot claim a boundary
# it never actually reached.
REFRESH_CRASH_LAST_EVENT = {
    "reservation": "raw_conversion_reservation",
    "intent": "raw_conversion_intent",
    "prepared": "raw_conversion_intent",
    "private": "conversion_poll_result_intent",
    "manifest": "conversion_poll_result_intent",
}


@pytest.mark.parametrize(
    ("boundary", "renewal"),
    [
        ("reservation", "new"),
        ("intent", "new"),
        ("prepared", "new"),
        ("private", "new"),
        ("manifest", "new"),
        ("private", "same"),
        ("manifest", "same"),
    ],
)
def test_expired_refresh_crash_boundaries_recover_without_new_task_or_get(
    tmp_path, capsys, monkeypatch, boundary, renewal
):
    # A locally expired result reference is refreshed by re-polling the same
    # Doc2X task. Crashing anywhere along that journey must never cost a
    # second conversion (a new, billable attempt) and must never repeat the
    # result GET: recovery has to finish the interrupted operation from the
    # durable journal alone.
    bundle, environ, resume, ready = _refresh_ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    request_filename = json.loads((bundle / "manifest.json").read_text())[
        "conversion_attempts"
    ][-1]["request_summary"]["filename"]
    generation = ready["generation"]

    if boundary in {"reservation", "intent"}:
        if boundary == "reservation":
            # Crash after raw_conversion_reservation, before its intent.
            attribute = "_ensure_reserved_staging"
        else:
            # Crash after raw_conversion_intent, before the expiry rejection.
            attribute = "_commit_rejection"
        original = getattr(raw_conversion, attribute)

        def crash(*_args, **_kwargs):
            raise SimulatedProcessCrash

        monkeypatch.setattr(raw_conversion, attribute, crash)
        _crash_resume(
            tmp_path,
            bundle,
            environ,
            generation,
            transport=CountingNeverNetwork(),
            now=REFRESH_EXPIRY,
        )
        monkeypatch.setattr(raw_conversion, attribute, original)
    else:
        expired = resume(
            generation, transport=CountingNeverNetwork(), now=REFRESH_EXPIRY
        )
        assert expired["outcome"] == "result_url_unavailable"
        generation = expired["generation"]
        refresh_url = (
            REFRESH_SECOND_URL if renewal == "new" else REFRESH_FIRST_URL
        )
        refresh_poll = PollStatus(
            REFRESH_TASK_ID, "completed", results=[{"url": refresh_url}]
        )
        if boundary in {"private", "manifest"}:
            # Crash while committing the refreshed poll result.
            (
                original_atomic_write,
                original_append_history,
            ) = _install_conversion_journal_crash(
                monkeypatch,
                event="conversion_poll_result_committed",
                boundary=boundary,
            )
            _crash_resume(
                tmp_path,
                bundle,
                environ,
                generation,
                transport=refresh_poll,
                now=REFRESH_EXPIRY,
            )
            monkeypatch.setattr(
                conversion_attempt.bundle, "atomic_write_json", original_atomic_write
            )
            monkeypatch.setattr(
                conversion_attempt.bundle, "append_history", original_append_history
            )
        else:
            # Crash after the refreshed archive is extracted, before the
            # raw_conversion_prepared event is durable.
            renewed = resume(
                generation, transport=refresh_poll, now=REFRESH_EXPIRY
            )
            assert renewed["outcome"] == "result_ready"
            generation = renewed["generation"]
            original_append_prepared = raw_conversion._append_prepared

            def crash_before_prepared(**_kwargs):
                raise SimulatedProcessCrash

            monkeypatch.setattr(
                raw_conversion, "_append_prepared", crash_before_prepared
            )
            _crash_resume(
                tmp_path,
                bundle,
                environ,
                generation,
                transport=ArchiveTransport(
                    make_zip([(f"{request_filename}.md", b"# Refresh\n")])
                ),
                now=REFRESH_EXPIRY,
            )
            monkeypatch.setattr(
                raw_conversion, "_append_prepared", original_append_prepared
            )
    capsys.readouterr()
    crashed_history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert crashed_history[-1]["event"] == REFRESH_CRASH_LAST_EVENT[boundary], (
        boundary,
        renewal,
        crashed_history[-1]["event"],
    )

    never = CountingNeverNetwork()
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=never,
        now=REFRESH_EXPIRY,
    )

    # No repeated result GET: recovery is decided from durable state alone.
    assert never.calls == [], (boundary, never.calls)
    assert recovered_rc == 0, (boundary, json.dumps(recovered, sort_keys=True))
    manifest = json.loads((bundle / "manifest.json").read_text())
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    # No new (billable) conversion attempt was created by the recovery.
    assert len(manifest["conversion_attempts"]) == 1
    assert manifest["conversion_attempts"][-1]["task_id"] == REFRESH_TASK_ID
    assert sum(
        event.get("event") == "conversion_submit_intent" for event in history
    ) == 1
    public_state = (bundle / "manifest.json").read_text() + (
        bundle / ".state" / "history.ndjson"
    ).read_text()
    assert REFRESH_FIRST_URL not in public_state
    assert REFRESH_SECOND_URL not in public_state
    # Recovery settled on the right decision, not merely on some self
    # consistent one.
    (
        expected_conversion_state,
        expected_attempt_state,
        expected_reason,
        expected_reason_detail,
        expected_result_urls,
    ) = REFRESH_RECOVERY_EXPECTATIONS[(boundary, renewal)]
    attempt = manifest["conversion_attempts"][-1]
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert manifest["conversion_state"] == expected_conversion_state, (
        boundary,
        renewal,
    )
    assert attempt["state"] == expected_attempt_state, (boundary, renewal)
    assert attempt["reason"] == expected_reason, (boundary, renewal)
    assert attempt["reason_detail"] == expected_reason_detail, (boundary, renewal)
    assert len(private_state["result_urls"]) == expected_result_urls, (
        boundary,
        renewal,
    )


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
def test_conversion_retry_journal_recovers_each_write_boundary_idempotently(
    tmp_path, capsys, monkeypatch, boundary
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, unknown, _stderr = invoke(
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
        transport=StatusCreate(500),
    )
    argv = [
        "record",
        "conversion",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(unknown["generation"]),
        "--action-id",
        unknown["action_id"],
        "--evidence-hash",
        unknown["evidence_hash"],
        "--decision",
        "retry",
        "--basis",
        "I accept the possible duplicate conversion charge.",
    ]
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch,
        event="conversion_retry_committed",
        boundary=boundary,
    )

    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            argv,
            environ=dependencies,
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=NeverNetwork(),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )

    recovered_rc, recovered, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    assert recovered_rc == 0, json.dumps(recovered, sort_keys=True)
    assert recovered["outcome"] == "conversion_retry_authorized"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [attempt["state"] for attempt in manifest["conversion_attempts"]] == [
        "submission_unknown",
        "authorized",
    ]
    assert len(
        [
            event
            for event in (
                json.loads(line)
                for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
            )
            if event.get("event") == "conversion_retry_intent"
        ]
    ) == 1


@pytest.mark.parametrize("boundary", ["private", "manifest", "commit"])
def test_layout_retry_journal_recovers_inside_a_raw_bearing_bundle(
    tmp_path, capsys, monkeypatch, boundary
):
    # A retry authorized after a raw conversion rejection lands in a bundle
    # whose history already mixes raw conversion events with conversion
    # attempt operations. Recovering that retry has to replay the whole
    # prefix, so it needs the reducer that owns every event in it.
    bundle, ready, dependencies, key, _result_url = ready_result_bundle(
        tmp_path, capsys, monkeypatch
    )
    layout_rc, layout_error, _stderr = invoke(
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
    assert layout_rc == 0, json.dumps(layout_error, sort_keys=True)
    assert layout_error["outcome"] == "unexpected_result_layout"
    assert layout_error["action_required"] == "resolve_unexpected_result_layout"

    argv = [
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
    ]
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch,
        event="conversion_retry_committed",
        boundary=boundary,
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            argv,
            environ=dependencies,
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=CountingNeverNetwork(),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )

    never = CountingNeverNetwork()
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=dependencies,
        transport=never,
    )

    assert never.calls == [], (boundary, never.calls)
    assert recovered_rc == 0, (boundary, json.dumps(recovered, sort_keys=True))
    assert recovered["outcome"] == "conversion_retry_authorized"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert [attempt["state"] for attempt in manifest["conversion_attempts"]] == [
        "result_ready",
        "authorized",
    ]
    history = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert sum(
        event.get("event") == "conversion_retry_intent" for event in history
    ) == 1


@pytest.mark.parametrize(
    ("operation", "boundary"),
    [("poll", "manifest"), ("retry", "manifest")],
)
def test_pending_conversion_journal_rejects_wrong_generation_without_writes_or_network(
    tmp_path, capsys, monkeypatch, operation, boundary
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    transport = SuccessfulCreate("task-generation-guard")
    _create_rc, conversion_result, _stderr = invoke(
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
        transport=transport if operation == "poll" else StatusCreate(500),
    )
    if operation == "poll":
        argv = [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(conversion_result["generation"]),
        ]
        crash_transport = PollStatus("task-generation-guard", "processing")
        commit_event = "conversion_poll_result_committed"
    else:
        argv = [
            "record",
            "conversion",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(conversion_result["generation"]),
            "--action-id",
            conversion_result["action_id"],
            "--evidence-hash",
            conversion_result["evidence_hash"],
            "--decision",
            "retry",
            "--basis",
            "I accept duplicate-charge risk.",
        ]
        crash_transport = NeverNetwork()
        commit_event = "conversion_retry_committed"
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch, event=commit_event, boundary=boundary
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            argv,
            environ={**dependencies, "AIHUB_API_KEY": key},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=crash_transport,
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )
    paths = [
        bundle / "manifest.json",
        bundle / ".state" / "private.json",
        bundle / ".state" / "history.ndjson",
    ]
    before = [path.read_bytes() for path in paths]
    never = NeverNetwork()
    bad_argv = list(argv)
    generation_index = bad_argv.index("--expected-generation") + 1
    bad_argv[generation_index] = "999999"

    rc, result, _stderr = invoke(
        capsys,
        bad_argv,
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=never,
    )

    assert rc == 5
    assert result["errors"][0]["code"] == "generation_conflict"
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize(
    "mutation",
    ["intent_extra_key", "intent_unknown_schema", "malformed_committed", "unsafe_task_id"],
)
def test_malformed_conversion_state_is_rejected_without_internal_error(
    tmp_path, capsys, monkeypatch, mutation
):
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    _create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-before-mutation"),
    )
    history_path = bundle / ".state" / "history.ndjson"
    history = [json.loads(line) for line in history_path.read_text().splitlines()]
    first = next(
        index
        for index, event in enumerate(history)
        if event.get("event") == "conversion_submit_intent"
    )
    if mutation == "intent_extra_key":
        history[first]["unexpected"] = True
    elif mutation == "intent_unknown_schema":
        history[first]["schema_version"] = 999
    elif mutation == "malformed_committed":
        history[first + 1] = []
    else:
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["conversion_attempts"][-1]["task_id"] = "unsafe/task/id"
        result_intent = history[first + 2]
        result_intent["attempt"]["task_id"] = "unsafe/task/id"
        history[first + 3]["manifest_hash"] = conversion_attempt.object_hash(manifest)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    history_path.write_text(
        "".join(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in history)
    )

    rc, result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    assert rc == 4, json.dumps(result, sort_keys=True)
    assert result["errors"][0]["code"] == "invalid_bundle"
    assert result["errors"][0]["code"] != "internal_error"


def test_result_reference_expiry_is_derived_from_observed_at_plus_validity_hours():
    attempt = {
        "result_observed_at": "2024-01-02T03:04:05Z",
        "result_validity_hours": 24,
    }
    fake_clock_past_expiry = "2024-01-03T03:04:06Z"

    assert conversion_attempt.result_reference_is_expired(
        attempt, at=fake_clock_past_expiry
    ) is True

    # One second before the expiry point: not expired yet. Kills a "return
    # True" stub and a sign-flipped comparison.
    assert conversion_attempt.result_reference_is_expired(
        attempt, at="2024-01-03T03:04:04Z"
    ) is False
    # Exactly at the expiry point: expired. Pins the ">=" boundary semantics.
    assert conversion_attempt.result_reference_is_expired(
        attempt, at="2024-01-03T03:04:05Z"
    ) is True
    # Changing result_validity_hours must move the expiry point. Kills an
    # implementation that ignores result_validity_hours or mishandles its
    # unit (e.g. treating hours as seconds).
    assert conversion_attempt.result_reference_is_expired(
        {**attempt, "result_validity_hours": 1}, at="2024-01-02T04:04:05Z"
    ) is True
    assert conversion_attempt.result_reference_is_expired(
        {**attempt, "result_validity_hours": 1}, at="2024-01-02T04:04:04Z"
    ) is False


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(
            {
                "state": "submitting",
                "result_observed_at": None,
                "result_validity_hours": None,
            },
            id="non_result_ready_state",
        ),
        pytest.param(None, id="attempt_not_a_dict"),
    ],
)
def test_result_reference_is_expired_returns_false_when_not_applicable(attempt):
    # Mirrors waiting_for_poll_backoff's combined guard (conversion_attempt.py
    # :201-205): `not isinstance(attempt, dict) or <state not applicable>`
    # reports "not applicable" by returning False, it does not raise. A real
    # non-result_ready attempt always carries result_observed_at/
    # result_validity_hours as None (see conversion_attempt.py:645-646,
    # :1465-1466), which is the signal this predicate's state guard uses in
    # place of a literal state-set membership check.
    assert conversion_attempt.result_reference_is_expired(
        attempt, at="2024-01-02T03:04:05Z"
    ) is False


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(
            {"result_observed_at": "2024-01-02T03:04:05Z"},
            id="result_validity_hours_missing",
        ),
        pytest.param(
            {"result_validity_hours": 24},
            id="result_observed_at_missing",
        ),
        pytest.param(
            {
                "result_observed_at": "2024-01-02T03:04:05Z",
                "result_validity_hours": "24",
            },
            id="result_validity_hours_wrong_type",
        ),
    ],
)
def test_result_reference_is_expired_rejects_malformed_attempt(attempt):
    # Mirrors waiting_for_poll_backoff's field guard (conversion_attempt.py
    # :206-210): once the predicate is in the applicable state, a missing or
    # malformed required field raises ConversionAttemptError("integrity_
    # violation", ...) instead of a native TypeError/KeyError, so callers
    # never see result["errors"][0]["code"] == "internal_error"
    # (test_malformed_conversion_state_is_rejected_without_internal_error
    # pins the same contract for the wider CLI surface).
    with pytest.raises(conversion_attempt.ConversionAttemptError) as excinfo:
        conversion_attempt.result_reference_is_expired(
            attempt, at="2024-01-02T03:04:05Z"
        )
    assert excinfo.value.code == "integrity_violation"


def test_canonical_accounting_matches_writer_bytes_including_trailing_lf(tmp_path):
    # Local-state capacity admission (plan.md 2.2/2.3) needs to estimate how
    # many bytes a candidate payload will occupy on disk *before* writing it,
    # using the same canonical encoder bundle.atomic_write_json/append_history
    # actually use. If the accounting side re-implemented its own json.dumps
    # call with different parameters, the estimate could silently drift from
    # what gets persisted. This pins the two paths to the same source: the
    # byte length conversion_attempt.canonical_state_byte_length reports must
    # equal, exactly, the length of the compact ASCII JSON + single trailing
    # LF that bundle.atomic_write_json actually writes to disk -- including
    # for a payload containing non-ASCII content, where ensure_ascii's value
    # changes the byte count the most.
    value = {
        "schema_version": 1,
        "note": "文件名包含中文与非 ASCII 字符 café",
        "nested": {"z": 1, "a": [1, 2, 3], "empty": {}},
    }
    expected_bytes = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    dir_fd = os.open(str(tmp_path), directory_flags)
    try:
        conversion_attempt.bundle.atomic_write_json(
            "canonical-accounting.json", value, dir_fd=dir_fd
        )
    finally:
        os.close(dir_fd)

    written_bytes = (tmp_path / "canonical-accounting.json").read_bytes()

    assert written_bytes == expected_bytes
    assert conversion_attempt.canonical_state_byte_length(value) == len(
        expected_bytes
    )


def test_worst_case_admission_uses_upper_bounds_for_unknown_response():
    # plan.md 2.2 / design.md: admission for create/ordinary-poll/refresh must
    # assume the *worst legal* unknown response before it has been validated
    # -- a 4,096-byte task_id (spec's byte bound, not doc2x.TASK_ID_PATTERN's
    # stricter 256-char/charset-restricted match, which is a fail-closed
    # tightening the admission math must not rely on) and a 16,384-byte
    # result URL (spec.md's ceiling, enforced by doc2x.valid_https_url in the
    # same UTF-8 byte unit) -- and must account for
    # ensure_ascii=True's worst-case \uXXXX escape inflation of up to 6 JSON
    # output bytes per raw UTF-8 input byte. manifest/private/history are
    # each compared against their own ceiling (8 MiB / 8 MiB / 64 MiB)
    # independently, so headroom in one file can never mask an overrun in
    # another.
    assert conversion_attempt.TASK_ID_UPPER_BOUND_BYTES == 4096
    assert conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES == 16384
    assert conversion_attempt.JSON_STRING_ESCAPE_MAX_BYTES_PER_UTF8_BYTE == 6

    # The escape-inflation upper bound applies per raw UTF-8 byte, plus the
    # two wrapping quote bytes every JSON string gets.
    assert conversion_attempt.worst_case_json_string_bytes(1) == 2 + 6
    assert (
        conversion_attempt.worst_case_json_string_bytes(
            conversion_attempt.TASK_ID_UPPER_BOUND_BYTES
        )
        == 2 + 4096 * 6
    )
    assert (
        conversion_attempt.worst_case_json_string_bytes(
            conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES
        )
        == 2 + 16384 * 6
    )
    assert conversion_attempt.MAX_MANIFEST_CANDIDATE_BYTES == 8 * 1024 * 1024
    assert conversion_attempt.MAX_PRIVATE_CANDIDATE_BYTES == 8 * 1024 * 1024
    assert conversion_attempt.bundle.MAX_STATE_BYTES == 64 * 1024 * 1024

    manifest_ceiling = conversion_attempt.MAX_MANIFEST_CANDIDATE_BYTES
    private_ceiling = conversion_attempt.MAX_PRIVATE_CANDIDATE_BYTES
    history_ceiling = conversion_attempt.bundle.MAX_STATE_BYTES
    # Cost of upgrading one `""` placeholder to its worst-case bound.
    task_id_bytes = conversion_attempt.worst_case_json_string_bytes(
        conversion_attempt.TASK_ID_UPPER_BOUND_BYTES
    ) - 2
    url_bytes = conversion_attempt.worst_case_json_string_bytes(
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES
    ) - 2

    # Baseline: every file has comfortable headroom -> all admitted.
    baseline = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=1_000,
        history_candidate_bytes=1_000,
        manifest_unreceived_task_id_count=1,
        history_unreceived_task_id_count=1,
    )
    assert baseline == {"manifest": True, "private": True, "history": True}

    # Exactly at the manifest ceiling once the worst-case task_id lands ->
    # still admitted (<=, not <).
    at_manifest_ceiling = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=manifest_ceiling - task_id_bytes,
        private_candidate_bytes=1_000,
        history_candidate_bytes=1_000,
        manifest_unreceived_task_id_count=1,
    )
    assert at_manifest_ceiling["manifest"] is True

    # One byte over the manifest ceiling once the worst-case task_id lands ->
    # rejected, while private/history (comfortable headroom) stay admitted.
    # This pins that the three files are judged independently: manifest's
    # overrun must not be masked by private/history's headroom.
    only_manifest_over = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=manifest_ceiling - task_id_bytes + 1,
        private_candidate_bytes=1_000,
        history_candidate_bytes=1_000,
        manifest_unreceived_task_id_count=1,
    )
    assert only_manifest_over == {"manifest": False, "private": True, "history": True}

    only_private_over = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling - url_bytes + 1,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=1,
    )
    assert only_private_over == {"manifest": True, "private": False, "history": True}

    only_history_over = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=1_000,
        history_candidate_bytes=history_ceiling - task_id_bytes - url_bytes + 1,
        history_unreceived_task_id_count=1,
        history_unreceived_result_url_count=1,
    )
    assert only_history_over == {"manifest": True, "private": True, "history": False}

    # The upper bound is actually used, not a stand-in placeholder/zero: the
    # same current_bytes value that overruns the ceiling once the worst-case
    # task_id is added stays comfortably admitted when the operation cannot
    # add one at all.
    without_worst_case_addition = (
        conversion_attempt.worst_case_admission_for_unknown_response(
            manifest_candidate_bytes=manifest_ceiling - task_id_bytes + 1,
            private_candidate_bytes=1_000,
            history_candidate_bytes=1_000,
            manifest_unreceived_task_id_count=0,
        )
    )
    assert without_worst_case_addition["manifest"] is True


def test_manifest_and_private_candidate_ceilings_match_workflow_read_ceiling():
    # workflow._read_json (workflow.py:696-707) already bounds manifest.json
    # and private.json reads to workflow.MAX_STATE_BYTES (workflow.py:34, 8
    # MiB) via _read_private_file's default max_bytes. Candidate admission's
    # manifest/private ceilings reuse that same already-existing 8 MiB value
    # rather than inventing a third one; this pins the two so a future edit
    # to either side cannot silently drift them apart.
    assert conversion_attempt.MAX_MANIFEST_CANDIDATE_BYTES == workflow.MAX_STATE_BYTES
    assert conversion_attempt.MAX_PRIVATE_CANDIDATE_BYTES == workflow.MAX_STATE_BYTES


def test_worst_case_admission_counts_whole_candidate_not_just_unknown_values():
    # design.md:305 requires admission to take the *largest serialized size*
    # across every legal candidate for each file, and design.md:296 requires
    # known fields to be counted at their exact C() length. So the caller
    # hands in a candidate's exact canonical byte length -- keys, punctuation,
    # nested objects, sha256 digests, timestamps and the trailing LF all
    # included -- and the counts only say how many not-yet-received bounded
    # strings inside that candidate still sit at their `""` placeholder.
    task_id_bound = conversion_attempt.TASK_ID_UPPER_BOUND_BYTES
    url_bound = conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES

    # A private.json result-reference candidate, URL still a placeholder. Its
    # cost is dominated by structure, not by the one unknown value: the record
    # carries a 71-byte sha256 digest, an attempt id, a task id and a
    # timestamp, none of which the old two-boolean contract could express.
    private_candidate = {
        "schema_version": 1,
        "generation": 7,
        "result_urls": [
            {
                "attempt_id": "attempt-1",
                "task_id": "task-1",
                "url": "",
                "url_sha256": "sha256:" + "0" * 64,
                "observed_at": "2026-01-01T00:00:00Z",
                "expires_at": None,
                "validity_window_hours": 24,
            }
        ],
    }
    private_candidate_bytes = conversion_attempt.canonical_state_byte_length(
        private_candidate
    )
    # Structure alone costs far more than the placeholder value's 2 bytes.
    assert private_candidate_bytes > 150

    # A create appends *two* history events, and each carries a full event
    # shell: the whole attempt object, an operation id, timestamps and two
    # sha256 digests. history_candidate_bytes is the current file plus every
    # event the operation would append, each already including its trailing LF.
    submit_intent_event = {
        "schema_version": 1,
        "event": "conversion_submit_intent",
        "operation_id": "attempt-1-submit",
        "expected_generation": 6,
        "new_generation": 7,
        "at": "2026-01-01T00:00:00Z",
        "attempt": {"attempt_id": "attempt-1", "state": "submitting", "task_id": ""},
        "previous_attempt": None,
        "previous_manifest_hash": "sha256:" + "1" * 64,
        "previous_private_hash": "sha256:" + "2" * 64,
    }
    submit_started_event = {
        "schema_version": 1,
        "event": "conversion_submit_started",
        "operation_id": "attempt-1-submit",
        "previous_generation": 6,
        "generation": 7,
        "at": "2026-01-01T00:00:00Z",
        "manifest_hash": "sha256:" + "3" * 64,
        "private_hash": "sha256:" + "4" * 64,
    }
    history_current_bytes = 4_096
    appended = conversion_attempt.canonical_state_byte_length(
        submit_intent_event
    ) + conversion_attempt.canonical_state_byte_length(submit_started_event)
    # Two events' shells, so the sum is far more than one event's worth.
    assert appended > 400
    history_candidate_bytes = history_current_bytes + appended

    private_ceiling = conversion_attempt.MAX_PRIVATE_CANDIDATE_BYTES
    unknown_url_bytes = conversion_attempt.worst_case_json_string_bytes(url_bound) - 2

    # The private candidate's own bytes are part of the budget: sitting exactly
    # at the ceiling once the placeholder is upgraded is admitted, one byte
    # more is not. If the structure bytes were dropped the flip point would
    # move by private_candidate_bytes.
    at_ceiling = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling - unknown_url_bytes,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=1,
    )
    assert at_ceiling["private"] is True
    over_ceiling = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling - unknown_url_bytes + 1,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=1,
    )
    assert over_ceiling == {"manifest": True, "private": False, "history": True}

    # A candidate that fits on its own but not once its placeholder is
    # upgraded to the worst-case bound.
    tight = private_ceiling - private_candidate_bytes
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling,
        history_candidate_bytes=1_000,
    )["private"] is True
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=1,
    )["private"] is False
    assert tight > 0

    # Each unreceived placeholder is charged separately: the same candidate
    # holding two unknown result URLs costs twice the upgrade.
    two_unknowns = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling - 2 * unknown_url_bytes,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=2,
    )
    assert two_unknowns["private"] is True
    two_unknowns_over = conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_ceiling - 2 * unknown_url_bytes + 1,
        history_candidate_bytes=1_000,
        private_unreceived_result_url_count=2,
    )
    assert two_unknowns_over["private"] is False

    # History mixes both kinds of unknown, on top of the real two-event shell.
    history_ceiling = conversion_attempt.bundle.MAX_STATE_BYTES
    unknown_task_id_bytes = (
        conversion_attempt.worst_case_json_string_bytes(task_id_bound) - 2
    )
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=1_000,
        history_candidate_bytes=(
            history_ceiling - unknown_task_id_bytes - unknown_url_bytes
        ),
        history_unreceived_task_id_count=1,
        history_unreceived_result_url_count=1,
    )["history"] is True
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=1_000,
        history_candidate_bytes=(
            history_ceiling - unknown_task_id_bytes - unknown_url_bytes + 1
        ),
        history_unreceived_task_id_count=1,
        history_unreceived_result_url_count=1,
    )["history"] is False

    # The real two-event create candidate is admitted against a real ceiling.
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=1_000,
        private_candidate_bytes=private_candidate_bytes,
        history_candidate_bytes=history_candidate_bytes,
        private_unreceived_result_url_count=1,
        history_unreceived_task_id_count=1,
    ) == {"manifest": True, "private": True, "history": True}


def test_worst_case_admission_reads_every_bound_and_ceiling_at_call_time(monkeypatch):
    # design.md:305: "ceiling 以可注入常量测试；生产值保持 8/8/64 MiB". plan.md 2.3
    # drives its boundary cases by shrinking these numbers, so every value the
    # verdict depends on -- the two upper bounds as well as the three ceilings
    # -- must be read from the module when the function runs. A bound folded
    # into an import-time constant would ignore the injected value and quietly
    # keep judging against the production number.
    monkeypatch.setattr(conversion_attempt, "TASK_ID_UPPER_BOUND_BYTES", 1)
    monkeypatch.setattr(conversion_attempt, "RESULT_URL_UPPER_BOUND_BYTES", 2)
    monkeypatch.setattr(conversion_attempt, "MAX_MANIFEST_CANDIDATE_BYTES", 100)
    monkeypatch.setattr(conversion_attempt, "MAX_PRIVATE_CANDIDATE_BYTES", 100)
    monkeypatch.setattr(conversion_attempt.bundle, "MAX_STATE_BYTES", 100)

    # Injected bounds: one unknown task_id now costs 6*1 bytes and one unknown
    # result URL 6*2 bytes, so a 94-byte candidate lands exactly on the
    # injected 100-byte ceiling and a 95-byte one overruns it.
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=94,
        private_candidate_bytes=88,
        history_candidate_bytes=82,
        manifest_unreceived_task_id_count=1,
        private_unreceived_result_url_count=1,
        history_unreceived_task_id_count=1,
        history_unreceived_result_url_count=1,
    ) == {"manifest": True, "private": True, "history": True}
    assert conversion_attempt.worst_case_admission_for_unknown_response(
        manifest_candidate_bytes=95,
        private_candidate_bytes=89,
        history_candidate_bytes=83,
        manifest_unreceived_task_id_count=1,
        private_unreceived_result_url_count=1,
        history_unreceived_task_id_count=1,
        history_unreceived_result_url_count=1,
    ) == {"manifest": False, "private": False, "history": False}


def _bundle_state_snapshot(bundle):
    """Every byte and every directory entry the operation must leave alone.

    Byte *contents* (not lengths, not mtimes) of the three durable files, plus
    the full entry list of the bundle root and its .state directory so a
    leftover ``.manifest.json.<pid>.<n>`` temporary from
    bundle._atomic_write_bytes -- or a half-written NDJSON line -- shows up as
    a difference.
    """
    state = bundle / ".state"
    return {
        "manifest": (bundle / "manifest.json").read_bytes(),
        "private": (state / "private.json").read_bytes(),
        "history": (state / "history.ndjson").read_bytes(),
        "root_entries": sorted(entry.name for entry in bundle.iterdir()),
        "state_entries": sorted(entry.name for entry in state.iterdir()),
    }


def _capacity_exhaustion_bundle(path, tmp_path, capsys, monkeypatch):
    """Drive a bundle to the state each admission path starts from.

    Returns the bundle, the environment, the generation the next resume must
    quote, and the moment that resume runs at.
    """
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    def resume(expected_generation, *, transport, now=NOW):
        rc, result, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(expected_generation),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=transport,
            now=now,
        )
        assert rc == 0, json.dumps(result, sort_keys=True)
        return result

    if path == "create":
        # conversion_state == "ready_to_submit": the next resume would append
        # conversion_submit_intent and then POST create.
        return bundle, environ, staged["generation"], NOW

    submitted = resume(
        staged["generation"], transport=SuccessfulCreate("task-capacity")
    )
    if path == "ordinary_poll":
        # conversion_state == "submitted": the next resume would GET the task
        # and then append conversion_poll_result_intent.
        return bundle, environ, submitted["generation"], NOW

    ready = resume(
        submitted["generation"],
        transport=PollStatus(
            "task-capacity", "completed", results=[{"url": FIRST_RESULT_URL}]
        ),
    )
    expired = resume(
        ready["generation"], transport=CountingNeverNetwork(), now=FIRST_EXPIRY
    )
    assert expired["outcome"] == "result_url_unavailable"
    assert expired["conversion_state"] == "recoverable_error"
    # recoverable_error + result_ready + result_url_unavailable: the next
    # resume re-polls the *same* task for a fresh URL (the 1.3 refresh branch).
    return bundle, environ, expired["generation"], FIRST_EXPIRY


@pytest.mark.parametrize("path", ["create", "ordinary_poll", "result_refresh"])
def test_capacity_exhaustion_stops_before_intent_and_leaves_bytes_untouched(
    path, tmp_path, capsys, monkeypatch
):
    # design.md:305 / spec.md "本地状态容量在外部调用前耗尽": when the worst
    # legal response of create, an ordinary poll or a result refresh would push
    # a candidate past its ceiling, the command must stop with
    # local_state_capacity_exhausted and
    # action_required=preserve_work_bundle_and_stop *before* the first intent
    # and before the external call. Because the first intent is what makes the
    # operation durable, "before" is only observable as: the three files still
    # hold their exact previous bytes, no temporary or half-written file was
    # left behind, and the transport ledger counted zero accesses on all three
    # entry points (__call__, resolve, connect_https -- archive downloads use
    # the latter two, which a __call__-only double would not see).
    bundle, environ, generation, at = _capacity_exhaustion_bundle(
        path, tmp_path, capsys, monkeypatch
    )
    before = _bundle_state_snapshot(bundle)

    # An injected ceiling far below any real candidate. design.md:305 requires
    # the ceilings to be injectable precisely so this boundary can be driven
    # without building an 8 MiB bundle.
    monkeypatch.setattr(conversion_attempt, "MAX_MANIFEST_CANDIDATE_BYTES", 1)

    never = CountingNeverNetwork()
    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=never,
        now=at,
    )

    assert rc != 0, json.dumps(result, sort_keys=True)
    assert result["outcome"] == "error"
    assert [error["code"] for error in result["errors"]] == [
        "local_state_capacity_exhausted"
    ]
    assert result["action_required"] == "preserve_work_bundle_and_stop"
    assert never.calls == []
    assert _bundle_state_snapshot(bundle) == before


def test_poll_admission_counts_the_bytes_its_crash_recovery_would_append(
    tmp_path, capsys, monkeypatch
):
    # spec.md:198 / design.md:305: the admission must budget the whole worst
    # case of the operation it is admitting, and a poll's worst case does not
    # end at conversion_poll_result_committed. If the process dies between the
    # intent and the private write, the next resume finishes the operation from
    # the journal inside recover_interrupted_attempt -- which runs at the very
    # top of _advance, before either admission call site, and returns straight
    # afterwards. That continuation therefore has no admission of its own, and
    # it is *larger* than the committed event it replaces: when the result URL
    # is gone it appends conversion_poll_result_recovered_transient, which
    # embeds a whole attempt object the direct committed event does not carry.
    #
    # Budgeting only the direct branch would let a poll through at a history
    # ceiling its own crash recovery cannot fit under -- and that recovery has
    # no way to back out: bundle.append_history would raise BundleStateError on
    # every later resume, replaying the same failure forever. So the operation
    # must be refused up front, at the poll, with the bundle untouched.
    #
    # The ceiling below is not computed from the admission's own arithmetic
    # (that would only prove the code agrees with itself). It is measured by
    # actually crashing and recovering this exact bundle and reading the byte
    # size the recovery really left behind.
    bundle, environ, resume, ready = _refresh_ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    expired = resume(
        ready["generation"], transport=CountingNeverNetwork(), now=REFRESH_EXPIRY
    )
    assert expired["outcome"] == "result_url_unavailable"
    generation = expired["generation"]
    history_path = bundle / ".state" / "history.ndjson"
    argv = [
        "resume",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(generation),
    ]

    # Phase 1 -- run the crash and the recovery for real, and measure it.
    snapshot = tmp_path / "pre-poll-bundle"
    shutil.copytree(bundle, snapshot)
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch,
        event="conversion_poll_result_committed",
        boundary="private",
    )
    _crash_resume(
        tmp_path,
        bundle,
        environ,
        generation,
        transport=PollStatus(
            REFRESH_TASK_ID, "completed", results=[{"url": REFRESH_SECOND_URL}]
        ),
        now=REFRESH_EXPIRY,
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )
    capsys.readouterr()
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=environ,
        transport=CountingNeverNetwork(),
        now=REFRESH_EXPIRY,
    )
    assert recovered_rc == 0, json.dumps(recovered, sort_keys=True)
    recovered_history = [
        json.loads(line)
        for line in history_path.read_text().splitlines()
    ]
    # The continuation this test is about, not the ordinary committed event.
    assert recovered_history[-1]["event"] == (
        "conversion_poll_result_recovered_transient"
    )
    recovered_history_bytes = len(history_path.read_bytes())

    # Phase 2 -- rewind to the moment the poll had not started yet, and give
    # the bundle one byte less than that recovery actually consumed.
    shutil.rmtree(bundle)
    shutil.copytree(snapshot, bundle)
    before = _bundle_state_snapshot(bundle)
    assert len(before["history"]) < recovered_history_bytes - 1
    monkeypatch.setattr(
        conversion_attempt.bundle, "MAX_STATE_BYTES", recovered_history_bytes - 1
    )

    never = CountingNeverNetwork()
    rc, result, _stderr = invoke(
        capsys,
        argv,
        cwd=tmp_path,
        environ=environ,
        transport=never,
        now=REFRESH_EXPIRY,
    )

    assert rc != 0, json.dumps(result, sort_keys=True)
    assert result["outcome"] == "error"
    assert [error["code"] for error in result["errors"]] == [
        "local_state_capacity_exhausted"
    ]
    assert result["action_required"] == "preserve_work_bundle_and_stop"
    assert never.calls == []
    assert _bundle_state_snapshot(bundle) == before


def test_private_ceiling_exhaustion_stops_before_intent(
    tmp_path, capsys, monkeypatch
):
    # worst_case_admission_for_unknown_response judges the three files against
    # three separate ceilings, so each of them has to be able to stop an
    # operation on its own. The manifest ceiling is driven by
    # test_capacity_exhaustion_stops_before_intent_and_leaves_bytes_untouched
    # and the history ceiling by
    # test_poll_admission_counts_the_bytes_its_crash_recovery_would_append;
    # this is the private.json one, so a wiring mistake that leaves the private
    # verdict out of the refusal cannot hide behind the other two.
    bundle, environ, generation, at = _capacity_exhaustion_bundle(
        "ordinary_poll", tmp_path, capsys, monkeypatch
    )
    before = _bundle_state_snapshot(bundle)
    monkeypatch.setattr(conversion_attempt, "MAX_PRIVATE_CANDIDATE_BYTES", 1)

    never = CountingNeverNetwork()
    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=never,
        now=at,
    )

    assert rc != 0, json.dumps(result, sort_keys=True)
    assert result["outcome"] == "error"
    assert [error["code"] for error in result["errors"]] == [
        "local_state_capacity_exhausted"
    ]
    assert result["action_required"] == "preserve_work_bundle_and_stop"
    assert never.calls == []
    assert _bundle_state_snapshot(bundle) == before


def test_capacity_admission_refuses_an_operation_it_cannot_size():
    # assert_local_state_capacity picks the candidate builder from `operation`.
    # An operation it does not recognise has no worst case it can compute, so
    # it must refuse rather than fall through to "nothing exceeded anything" --
    # otherwise adding a fourth writing path and forgetting its candidates
    # would silently produce an admission that always admits.
    with pytest.raises(conversion_attempt.ConversionAttemptError) as refused:
        conversion_attempt.assert_local_state_capacity(
            operation="conversion_retry",
            manifest={},
            private_state={},
            history_bytes=0,
            at="2024-01-02T03:04:05Z",
        )
    assert refused.value.code == "integrity_violation"


def test_capacity_admission_reads_history_at_the_history_ceiling(
    tmp_path, capsys, monkeypatch
):
    # spec.md:199 / design.md:448: a bundle whose history.ndjson is over
    # workflow's 8 MiB *state* ceiling but inside bundle's 64 MiB *history*
    # ceiling is legal. The admission has to read that file to know how much
    # room is left, and reading it at the wrong ceiling turns a legal bundle
    # into invalid_bundle + repair_or_restore_work_bundle -- the one action
    # design.md:448 forbids here, because it authorises the user to truncate
    # append-only history or rebuild a task that may already have been charged.
    # It would also make the 64 MiB history ceiling unreachable: an 8 MiB cap
    # on the term it is compared against leaves the history verdict constantly
    # true.
    #
    # scripts/review.py:2808 already reads the same file at the same ceiling
    # for the same reason.
    bundle, environ, generation, at = _capacity_exhaustion_bundle(
        "ordinary_poll", tmp_path, capsys, monkeypatch
    )
    original_read = workflow._read_private_file
    history_ceilings = []

    def recording_read(name, *, dir_fd, max_bytes=workflow.MAX_STATE_BYTES):
        if name == "history.ndjson":
            history_ceilings.append(max_bytes)
        return original_read(name, dir_fd=dir_fd, max_bytes=max_bytes)

    monkeypatch.setattr(workflow, "_read_private_file", recording_read)
    # Stop at the admission so the only history read under observation is the
    # admission's own.
    monkeypatch.setattr(conversion_attempt, "MAX_MANIFEST_CANDIDATE_BYTES", 1)
    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(generation),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=CountingNeverNetwork(),
        now=at,
    )
    assert rc != 0, json.dumps(result, sort_keys=True)
    assert history_ceilings == [conversion_attempt.bundle.MAX_STATE_BYTES]

    # And that ceiling is load-bearing, not decoration: a history file just
    # past workflow's state ceiling is still a legal history, and the default
    # this call site must not fall back to rejects it with exactly the action
    # design.md:448 rules out.
    oversized = tmp_path / "oversized-state"
    oversized.mkdir(mode=0o700)
    history = oversized / "history.ndjson"
    history.write_bytes(b"x" * (workflow.MAX_STATE_BYTES + 8))
    history.chmod(0o600)
    state_fd = os.open(oversized, os.O_RDONLY)
    try:
        assert len(
            original_read(
                "history.ndjson",
                dir_fd=state_fd,
                max_bytes=conversion_attempt.bundle.MAX_STATE_BYTES,
            )
        ) == workflow.MAX_STATE_BYTES + 8
        with pytest.raises(workflow.WorkflowError) as rejected:
            original_read("history.ndjson", dir_fd=state_fd)
    finally:
        os.close(state_fd)
    assert rejected.value.code == "invalid_bundle"
    assert rejected.value.action_required == "repair_or_restore_work_bundle"


def test_result_url_upper_bound_matches_doc2x_valid_https_url_boundary():
    # spec.md's "Completed 结果不安全" scenario: any result URL over 16,384
    # UTF-8 *bytes* is unsafe_result_url. doc2x.valid_https_url
    # (doc2x.py:243-258) is the only gate that URL passes before it is written
    # verbatim into private.json, so RESULT_URL_UPPER_BOUND_BYTES (which the
    # capacity admission math budgets for) must equal the bound that gate
    # actually enforces -- otherwise admission budgets for a URL smaller than
    # one the gate would still let through.
    prefix = "https://example.com/"
    at_bound = prefix + "a" * (
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES - len(prefix)
    )
    over_bound = at_bound + "a"
    # ASCII: one character is one UTF-8 byte, so this exercises the boundary
    # in both units at once.
    assert len(at_bound.encode("utf-8")) == (
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES
    )
    assert conversion_attempt.doc2x.valid_https_url(at_bound) is True
    assert conversion_attempt.doc2x.valid_https_url(over_bound) is False

    # Non-ASCII: 16,384 astral-plane characters are 65,476 UTF-8 bytes, four
    # times over the spec's byte ceiling. This is the case that separates a
    # byte bound from a code-point bound, so it must be rejected for the
    # admission budget above to hold.
    over_bound_in_bytes_only = prefix + "\U0001d11e" * (
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES - len(prefix)
    )
    assert len(over_bound_in_bytes_only) == (
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES
    )
    assert len(over_bound_in_bytes_only.encode("utf-8")) > (
        conversion_attempt.RESULT_URL_UPPER_BOUND_BYTES
    )
    # The gate measures UTF-8 bytes, the same unit spec.md and
    # RESULT_URL_UPPER_BOUND_BYTES use, so this URL is rejected and the
    # admission budget above is a real upper bound on what can reach
    # private.json. Measuring code points here instead would let this URL
    # through at four times the budgeted byte cost.
    assert (
        conversion_attempt.doc2x.valid_https_url(over_bound_in_bytes_only) is False
    )


def test_flat_state_migration_table_covers_the_whole_flat_domain():
    import conversion_attempt as ca

    expected_domain = {
        "not_started", "submitting", "submitted", "submission_unknown",
        "pending", "processing", "result_pending", "result_ready",
        "unsafe_result_url", "unexpected_result_count", "failed",
        "poll_transient", "poll_unauthorized", "task_unavailable",
        "credential_source_missing", "credential_source_changed",
        "poll_timeout", "result_pending_timeout",
    }
    assert ca.FLAT_STATE_DOMAIN == expected_domain
    # 今天扁平 state 的两份真相必须都被这张表覆盖，且不多不少。
    assert set(ca.POLL_STATE_CONTRACT) <= ca.FLAT_STATE_DOMAIN
    assert set(ca.FLAT_STATE_MIGRATION) == ca.FLAT_STATE_DOMAIN
    assert {target for target, _, _ in ca.FLAT_STATE_MIGRATION.values()} == {
        "authorized", "submitting", "submitted", "processing",
        "failed", "result_ready", "submission_unknown",
    }
    reasons = {reason for _, reason, _ in ca.FLAT_STATE_MIGRATION.values()}
    assert reasons - {None} <= {
        "no_task_id", "credential_source_missing",
        "credential_fingerprint_changed", "poll_authentication_rejected",
        "task_unavailable", "poll_transient", "poll_timeout",
        "result_pending_timeout", "task_failed", "result_url_expired",
        "unsafe_result_url", "unexpected_result_count",
    }


def test_flat_state_migration_agrees_with_the_live_conversion_state_projection():
    import conversion_attempt as ca

    # 跳过的是「不经 _conversion_state_for_attempt 投影」的 state：
    #   not_started / submitting —— 落该函数的默认分支，投影不负责；
    #   submission_unknown —— 由提交路径直写字面量（conversion_attempt.py:1486、
    #     :1630），不经此函数。design.md Decision 1 第 4 行要求它最终投影为
    #     submission_unknown，但那属于后续折叠任务 2.1c / 4.2a；本任务是
    #     characterization，固化现状，不得改生产行为。
    # result_ready 是唯一一个 reason 可空可非空的行，投影随 reason 变，
    # 不能拿单值的 _conversion_state_for_attempt 去比。
    for flat, (_, reason, top_level) in ca.FLAT_STATE_MIGRATION.items():
        if flat in {"not_started", "submitting", "submission_unknown"} or (
            flat == "result_ready" and reason is not None
        ):
            continue
        assert ca._conversion_state_for_poll_result(flat) == top_level, flat


def test_legal_triples_is_the_single_owner_of_state_legality():
    import conversion_attempt as ca

    # ⚠️ 2026-07-26 重写（2.1a 复审 Important #1）。初版用与生产代码**同一个
    # 推导式**构造 contract 再与该推导式的产物比较，重构一落地就变成 x == x：
    # 把 POLL_STATE_CONTRACT 重新硬编码回字面量也照样过。断言 2 同理——它原本
    # 拿表去比一条独立的 if/elif 链，而 _conversion_state_for_attempt 现在也从
    # 表派生，于是 15 次循环里 14 次是同义反复。
    #
    # 因此本测试改用**独立 oracle**：下面的字面量是重构前 c0d197a 的真实值，
    # 逐字抄自被删除的 POLL_STATE_CONTRACT / expected_manifest_state 字面量。
    # 它不从任何生产表派生，所以生产侧任何一处回退成字面量、或派生逻辑写错，
    # 都会红。
    # ⚠️ 2026-07-26（任务 2.1c）：原本这张 oracle 是三元组
    # (http_status, upstream_status, reason_code)，与 POLL_STATE_CONTRACT 整体
    # 比对。2.1c 把 wire reason_code 从 POLL_STATE_CONTRACT 的值里折掉了（自
    # schema v2 起它没有任何生产读者），所以 oracle 拆成两张：
    # CONTRACT_BEFORE_REFACTOR 保留前两列，WIRE_REASON_CODE_BEFORE_REFACTOR
    # 单独钉 reason_code。两张都仍逐字抄自重构前 c0d197a 的真实值，覆盖面与
    # 拆分前完全相同——14 行 × 3 列一格不少。
    CONTRACT_BEFORE_REFACTOR = {
        "submitted": (200, None),
        "pending": (200, "pending"),
        "processing": (200, "processing"),
        "result_pending": (200, "completed"),
        "result_ready": (200, "completed"),
        "unsafe_result_url": (200, "completed"),
        "unexpected_result_count": (200, "completed"),
        "failed": (200, "failed"),
        "credential_source_missing": (None, None),
        "credential_source_changed": (None, None),
        "poll_unauthorized": (401, None),
        "task_unavailable": (404, None),
        "poll_timeout": (None, None),
        "result_pending_timeout": (None, "completed"),
    }
    WIRE_REASON_CODE_BEFORE_REFACTOR = {
        "submitted": None,
        "pending": None,
        "processing": None,
        "result_pending": None,
        "result_ready": None,
        "unsafe_result_url": "unsafe_result_url",
        "unexpected_result_count": "unexpected_result_count",
        "failed": "task_failed",
        "credential_source_missing": "credential_source_missing",
        "credential_source_changed": "credential_source_changed",
        "poll_unauthorized": "poll_unauthorized",
        "task_unavailable": "task_unavailable",
        "poll_timeout": "poll_timeout",
        "result_pending_timeout": "result_pending_timeout",
    }
    # 折叠后 attempt_state 列不再是行标识，flat_state 列才是。这张 oracle
    # 是独立字面量（design.md Decision 1 表格的前两列），用来钉住
    # LEGAL_TRIPLES 从 FLAT_STATE_MIGRATION 派生出来的折叠列没有走样。
    FOLDED_PAIR_BY_FLAT_STATE = {
        "not_started": ("authorized", None),
        "submitting": ("submitting", None),
        "submitted": ("submitted", None),
        "submission_unknown": ("submission_unknown", "no_task_id"),
        "pending": ("processing", None),
        "processing": ("processing", None),
        "result_pending": ("processing", None),
        "result_ready": ("result_ready", None),
        "unsafe_result_url": ("failed", "unsafe_result_url"),
        "unexpected_result_count": ("failed", "unexpected_result_count"),
        "failed": ("failed", "task_failed"),
        "poll_transient": ("failed", "poll_transient"),
        "poll_unauthorized": ("failed", "poll_authentication_rejected"),
        "task_unavailable": ("failed", "task_unavailable"),
        "credential_source_missing": ("failed", "credential_source_missing"),
        "credential_source_changed": ("failed", "credential_fingerprint_changed"),
        "poll_timeout": ("failed", "poll_timeout"),
        "result_pending_timeout": ("failed", "result_pending_timeout"),
    }
    CONVERSION_STATE_BEFORE_REFACTOR = {
        "not_started": "ready_to_submit",
        "submitting": "submitting",
        "submitted": "submitted",
        "pending": "submitted",
        "processing": "submitted",
        "result_pending": "submitted",
        "submission_unknown": "submission_unknown",
        "result_ready": "result_downloading",
        "unsafe_result_url": "terminal_error",
        "unexpected_result_count": "terminal_error",
        "failed": "awaiting_user",
        "credential_source_missing": "recoverable_error",
        "credential_source_changed": "recoverable_error",
        "poll_unauthorized": "recoverable_error",
        "task_unavailable": "recoverable_error",
        "poll_transient": "recoverable_error",
        "poll_timeout": "recoverable_error",
        "result_pending_timeout": "recoverable_error",
    }

    # 表的行集必须被钉住：否则多一行、少一行、重复一行都只会以 KeyError
    # 间接暴露，而「唯一 owner 表」这条性质本身没有断言保护。
    assert len(ca.LEGAL_TRIPLES) == 18
    # 2.1c：行标识从 attempt_state 移到 flat_state（wire 分类），因为折叠后
    # 18 行只落 7 个 attempt_state。行集仍必须与扁平全域一一对应。
    assert {row.flat_state for row in ca.LEGAL_TRIPLES} == ca.FLAT_STATE_DOMAIN
    assert {
        row.flat_state: (row.attempt_state, row.reason)
        for row in ca.LEGAL_TRIPLES
    } == FOLDED_PAIR_BY_FLAT_STATE

    # 2.1b 前置 B（2.1a 复审转来）：四行占位 None 必须被显式钉住。这四行的
    # reason_code / http_status / upstream_status 被 POLL_STATE_CONTRACT 与
    # _LEGAL_TRIPLE_BY_FLAT_STATE 两处派生全部过滤掉，改成任意垃圾值今天
    # 都不会变红；而 2.1b 起就有读者读这几行（v1 降级要靠 .reason_code 取
    # 线上值）。把「恒为 None」从「碰巧如此」升格为显式契约。
    placeholder_states = {
        "not_started", "submitting", "submission_unknown", "poll_transient",
    }
    assert placeholder_states == set(ca._NON_CONTRACT_STATES)
    assert {
        row.flat_state: (row.reason_code, row.http_status, row.upstream_status)
        for row in ca.LEGAL_TRIPLES
        if row.flat_state in placeholder_states
    } == {state: (None, None, None) for state in placeholder_states}

    # 派生处 1：POLL_STATE_CONTRACT。与独立 oracle 比，不与自身推导式比。
    assert ca.POLL_STATE_CONTRACT == CONTRACT_BEFORE_REFACTOR
    # wire reason_code 列（2.1c 从 POLL_STATE_CONTRACT 折出来后仍由
    # LEGAL_TRIPLES 拥有），同样与独立 oracle 比，覆盖 14 个 contract 行。
    assert {
        row.flat_state: row.reason_code
        for row in ca.LEGAL_TRIPLES
        if row.flat_state not in placeholder_states
    } == WIRE_REASON_CODE_BEFORE_REFACTOR

    # 表的 conversion_state 列必须与独立 oracle 全 18 行一致——含
    # not_started / submitting / submission_unknown 这三行，它们被下面的
    # 投影循环排除（函数输入域比表窄），若不在这里钉住就没有任何测试覆盖。
    assert {
        row.flat_state: row.conversion_state for row in ca.LEGAL_TRIPLES
    } == CONVERSION_STATE_BEFORE_REFACTOR

    # _conversion_state_for_attempt 的输入域比表窄：那三个 state 今天永远落它
    # 的默认分支（返回 "submitted"），与表值不符。这不是缺陷——它只接收 poll
    # 结果 state：调用点 conversion_attempt.py 的 _poll_transition 硬守卫
    # `result.state not in POLL_RESULT_STATES` 即 raise；另一处调用点不在调用
    # 时守卫，而是由 _valid_attempt 的逐 state 形状检查在函数返回前排除
    # （2.1a 复审订正：协调者原先以 task_id 互斥解释，那只覆盖
    # submission_unknown，not_started / submitting 靠的是形状检查）。
    # ⚠️ 2.1a 是纯重构，**不得**为了让本循环覆盖它们而给该函数加分支——
    # Task 1.1 正因这样做被判越界并回退，用户已裁决该目标投影归 2.1c / 4.2a。
    NON_POLL_OBSERVATIONS = {"not_started", "submitting", "submission_unknown"}
    for flat in ca.FLAT_STATE_DOMAIN - NON_POLL_OBSERVATIONS:
        assert (
            CONVERSION_STATE_BEFORE_REFACTOR[flat]
            == ca._conversion_state_for_poll_result(flat)
        ), flat
        # 2.1c 起同一投影有第二个入口：读**已存记录**的折叠 (state, reason)。
        # 两个入口必须对同一行给出同一个 conversion_state，否则 poll 直写路径
        # 与崩溃恢复重放路径会写出不同的 manifest。
        assert (
            CONVERSION_STATE_BEFORE_REFACTOR[flat]
            == ca._conversion_state_for_attempt(*FOLDED_PAIR_BY_FLAT_STATE[flat])
        ), flat

    # 派生处 3（易漏项）：容量准入的最坏 poll 分支也必须从同一张表构造。
    # 实测形态：13 个表派生分支（每个非 poll_transient 的 poll 结果 state 一
    # 个）+ 4 个 poll_transient 分支（2 个 reason code × 2 个最坏 HTTP 状态）
    # = 17。断言这两部分而不是只断言总数，避免任一侧变化被另一侧掩盖。
    branches = ca._poll_response_branches()
    assert len(branches) == 17
    table_driven = [b for b in branches if b.state != "poll_transient"]
    assert len(table_driven) == 13
    assert {b.state for b in table_driven} == set(CONTRACT_BEFORE_REFACTOR) - {
        "submitted"
    }
    for branch in table_driven:
        http_status, upstream_status = CONTRACT_BEFORE_REFACTOR[branch.state]
        assert branch.http_status == http_status, branch.state
        assert branch.upstream_status == upstream_status, branch.state
        assert branch.reason_code == WIRE_REASON_CODE_BEFORE_REFACTOR[
            branch.state
        ], branch.state
    assert len([b for b in branches if b.state == "poll_transient"]) == 4


# --- Task 2.1b: the schema v2 attempt field set ----------------------------
#
# The two key sets below are independent literal oracles, transcribed once so
# no assertion in this section ever compares a production comprehension with
# itself (the failure mode the 2.1a review caught). SCHEMA_V1_ATTEMPT_KEYS is
# the field set the pre-2.1b implementation wrote, copied verbatim from
# conversion_attempt.ATTEMPT_KEYS at schema version 1.

SCHEMA_V1_ATTEMPT_KEYS = frozenset(
    {
        "schema_version", "attempt_id", "state", "api_base", "request_summary",
        "request_hash", "credential", "staging_identity", "submitted_at",
        "response_at", "http_status", "reason_code", "task_id",
        "pending_action", "authorization", "poll_started_at",
        "poll_deadline_at", "last_polled_at", "poll_count", "upstream_status",
        "next_poll_at", "consecutive_transient_count", "result_url_sha256",
        "result_observed_at", "result_validity_hours",
        "result_pending_started_at", "result_pending_deadline_at",
    }
)

SCHEMA_V2_ATTEMPT_KEYS = frozenset(
    {
        "schema_version", "attempt_id", "state", "api_base", "request_summary",
        "request_hash", "credential", "staging_identity", "submitted_at",
        "response_at", "http_status", "task_id", "pending_action",
        "authorization", "poll_started_at", "poll_deadline_at",
        "last_polled_at", "poll_count", "upstream_status", "next_poll_at",
        "consecutive_transient_count", "result_url_sha256",
        "result_observed_at", "result_validity_hours",
        "result_pending_started_at", "result_pending_deadline_at",
        "reason", "reason_detail", "authorization_kind",
        "result_refresh_round_count",
    }
)


def test_attempt_schema_version_two_carries_the_final_field_set():
    import conversion_attempt as ca

    assert ca.SCHEMA_VERSION == 2
    assert "reason_code" not in ca.ATTEMPT_KEYS
    assert {
        "reason", "reason_detail", "authorization_kind",
        "result_refresh_round_count",
    } <= ca.ATTEMPT_KEYS
    # design.md Migration Plan 步骤 3：字段集变更一次性落位。整集比对（而非
    # 只查四个新键）才能钉住「这是唯一一次变更」——多带或少带任何一个字段
    # 都会红。
    assert set(ca.ATTEMPT_KEYS) == set(SCHEMA_V2_ATTEMPT_KEYS)
    assert len(SCHEMA_V1_ATTEMPT_KEYS) == 27
    assert len(SCHEMA_V2_ATTEMPT_KEYS) == 30
    assert SCHEMA_V1_ATTEMPT_KEYS - SCHEMA_V2_ATTEMPT_KEYS == {"reason_code"}
    assert SCHEMA_V2_ATTEMPT_KEYS - SCHEMA_V1_ATTEMPT_KEYS == {
        "reason", "reason_detail", "authorization_kind",
        "result_refresh_round_count",
    }


def test_the_non_contract_state_exclusion_domain_has_a_single_owner():
    """2.1b 前置 A（2.1a 复审转来）。

    `NON_POLL_OBSERVATIONS | {"poll_transient"}` 这个排除域原本在
    `_LEGAL_TRIPLE_BY_FLAT_STATE` 与 `POLL_STATE_CONTRACT` 两处推导式的
    `if` 里各写一遍。两处一旦不一致，索引域与 contract 就分歧——正是 2.1a
    刚消灭的漂移形态以新形式回归。这里钉住：域有名字、且两处派生的键集都
    恰好等于「全域减去它」。
    """
    import conversion_attempt as ca

    assert set(ca._NON_CONTRACT_STATES) == {
        "not_started", "submitting", "submission_unknown", "poll_transient",
    }
    contract_domain = set(ca.FLAT_STATE_DOMAIN) - set(ca._NON_CONTRACT_STATES)
    assert len(contract_domain) == 14
    assert set(ca.POLL_STATE_CONTRACT) == contract_domain
    assert set(ca._LEGAL_TRIPLE_BY_FLAT_STATE) == contract_domain


def test_reason_detail_producer_and_validator_read_one_table(monkeypatch):
    """Review fix (Important #2, task 2.1b).

    `_attempt_reason_columns` (the writer) and `_valid_reason_detail` (the
    validator) must derive `reason_detail`'s legality from the same table,
    `_REASON_DETAIL_DOMAIN`, rather than from two independently listed
    constants that only happened to agree today (the writer used to read a
    separate module-level `_REASON_DETAIL_CODES`, defined as the union of
    `_REASON_DETAIL_DOMAIN`'s values -- nothing enforced that the two stayed
    equal as the vocabulary grows in 2.2/2.3, and that constant has since
    been removed in favour of this single table).

    Proof of "one table" is that widening `_REASON_DETAIL_DOMAIN` alone,
    without touching the writer, changes what the writer emits. A writer that
    reads a separate constant would not move: it would keep dropping the
    newly-domained code as `None`, and a validator that then accepted it
    would drift the two apart -- reproducing, one level down, exactly the
    two-tables-that-must-agree shape task 2.1a already removed from
    `POLL_STATE_CONTRACT` and `expected_manifest_state`.
    """
    import conversion_attempt as ca

    # Task 2.1c re-keys `_REASON_DETAIL_DOMAIN` from the flat state to the
    # folded `reason`. This probe deliberately uses a flat state
    # (`credential_source_changed`) whose folded reason
    # (`credential_fingerprint_changed`) is a *different* string -- unlike
    # `poll_timeout`, which folds onto a reason of the same name and so could
    # not tell "keyed by flat state" apart from "keyed by reason": monkeypatching
    # under either name would land on the domain entry the other indexing
    # scheme also reads, so a regression back to flat-state keying would still
    # pass. The writer is handed the flat name and looks the reason up itself;
    # the validator and the monkeypatch below are handed the reason directly.
    probe_flat_state = "credential_source_changed"
    probe_reason = "credential_fingerprint_changed"
    # Deliberately a third string, distinct from both probe_flat_state and
    # probe_reason: if the writer regressed to looking `_REASON_DETAIL_DOMAIN`
    # up by `reason_code` instead of by `reason`, probe_code == probe_reason
    # would make that regression invisible (the lookup key would accidentally
    # still resolve to the entry this test monkeypatches below).
    probe_code = "credential_fingerprint_changed_on_wire"
    # Today `credential_fingerprint_changed_on_wire` is not in
    # `_REASON_DETAIL_DOMAIN`, so both sides must drop the code.
    assert (
        ca._attempt_reason_columns(probe_flat_state, probe_code)["reason_detail"]
        is None
    )
    assert ca._valid_reason_detail(probe_reason, probe_code) is False

    monkeypatch.setitem(
        ca._REASON_DETAIL_DOMAIN, probe_reason, frozenset({probe_code})
    )

    # The validator reading the widened table is not interesting on its own
    # -- `_valid_reason_detail` already reads `_REASON_DETAIL_DOMAIN`
    # directly. The writer moving in lockstep, with no code of its own
    # touched, is the evidence the two share one table.
    assert (
        ca._attempt_reason_columns(probe_flat_state, probe_code)["reason_detail"]
        == probe_code
    )
    assert ca._valid_reason_detail(probe_reason, probe_code) is True


def test_locally_detected_pairs_reason_matches_the_closed_vocabularys_extra_member():
    """Task 2.2a / design.md Decision 4 -- the pair-set equality assertion
    that used to live inside test_conversion_reason_vocabulary_is_closed_to_
    twelve_values, split into its own test (2.2b front-loaded fix #2, 2.2a
    review Minor) so a mutation on either side is reported against this
    assertion directly instead of being potentially masked by assertion
    order inside a shared test with the twelve-value literal check below.

    What this actually pins (corrected from the original docstring, 2.2b
    front-loaded fix #1): the *eleven* migration-derived reasons on both
    sides come from the same FLAT_STATE_MIGRATION expression --
    `{reason for _state, reason in LEGAL_STATE_REASON_PAIRS if reason is not
    None}`'s eleven wire-derived members and CONVERSION_REASONS'
    `_REASONS_FROM_FLAT_STATE_MIGRATION` component are both literally derived
    from FLAT_STATE_MIGRATION.values(), so a reason added to (or removed
    from) the migration table moves *both* sides in lockstep and this
    equality stays green regardless -- it does NOT detect "a reason was added
    to the migration table but not to the vocabulary" (that scenario cannot
    even arise: the vocabulary's migration-derived component is not a second,
    independently-maintained literal to drift out of sync).

    What this assertion actually is weight-bearing for is narrower: it pins
    that LOCALLY_DETECTED_PAIRS' one non-wire reason (`result_url_expired`)
    is exactly CONVERSION_REASONS' one literal member beyond those eleven.
    Drift that only this assertion (and not the twelve-value literal below)
    would catch: a locally-detected pair added to LOCALLY_DETECTED_PAIRS
    without adding its reason to CONVERSION_REASONS' literal splice, or vice
    versa.
    """
    import conversion_attempt as ca

    assert {
        reason for _state, reason in ca.LEGAL_STATE_REASON_PAIRS
        if reason is not None
    } == ca.CONVERSION_REASONS


def test_conversion_reason_vocabulary_is_closed_to_twelve_values():
    """Task 2.2a / design.md Decision 4 -- the closed reason vocabulary a
    stored attempt's `reason` column may take.

    The subset assertion (not equality) against LEGAL_TRIPLES is deliberate:
    eleven of the twelve values are already the distinct reasons
    FLAT_STATE_MIGRATION/LEGAL_TRIPLES produce; the twelfth,
    `result_url_expired`, is produced by no flat_state at all (it is detected
    locally, not classified off the wire), and the two characterization tables
    stay at their measured 18 rows. Equality against LEGAL_TRIPLES would
    therefore be wrong, today and permanently.

    The hand-typed literal equality below is what actually stops
    CONVERSION_REASONS from silently drifting off FLAT_STATE_MIGRATION
    (2.2b front-loaded fix #1, correcting this docstring's earlier claim that
    the pair-set equality -- now
    test_locally_detected_pairs_reason_matches_the_closed_vocabularys_extra_
    member above -- was what did this): CONVERSION_REASONS' eleven-reason
    component recomputes off FLAT_STATE_MIGRATION at import time, so nothing
    besides a literal comparison can catch it drifting from what this test
    expects the migration table to still produce.
    """
    import conversion_attempt as ca

    assert ca.CONVERSION_REASONS == {
        "no_task_id", "credential_source_missing",
        "credential_fingerprint_changed", "poll_authentication_rejected",
        "task_unavailable", "poll_transient", "poll_timeout",
        "result_pending_timeout", "task_failed", "result_url_expired",
        "unsafe_result_url", "unexpected_result_count",
    }
    assert {row.reason for row in ca.LEGAL_TRIPLES} - {None} <= ca.CONVERSION_REASONS


def test_locally_detected_pairs_have_no_wire_row_key_collision():
    """2.2b front-loaded fix #3 (2.2a review Minor) -- symmetric to
    _locally_detected_observations' single-element-unpacking guard above,
    which turns a future ambiguous re-labelling into an import-time
    ValueError instead of silently widening its gate.

    `_MANIFEST_STATE_BY_FOLDED_STATE` splices LOCALLY_DETECTED_PAIRS into a
    plain dict via `|`, whose right operand silently wins on a key collision.
    If a future locally-detected pair ever shared a key with a wire-derived
    (attempt_state, reason) pair, the union would silently overwrite that
    pair's conversion_state projection instead of raising -- there is no
    unpacking or other loud failure guarding this particular splice today.
    This test pins today's disjointness between the two operands, so a
    colliding addition turns this red instead of leaving the silent-overwrite
    risk undetected.
    """
    import conversion_attempt as ca

    assert not (
        frozenset(ca.LOCALLY_DETECTED_PAIRS)
        & {(row.attempt_state, row.reason) for row in ca.LEGAL_TRIPLES}
    )


def test_reason_detail_is_a_total_refinement_of_exactly_two_reasons():
    """Task 2.2a -- REASON_DETAILS is the public name for the table task 2.1c
    already built as `_REASON_DETAIL_DOMAIN`. The `is` check pins that the
    rename is an alias to the exact same dict object, not a second literal
    that only happens to agree with it today (the two-tables-that-must-agree
    shape task 2.1a/2.1b already removed elsewhere in this module).
    """
    import conversion_attempt as ca

    assert ca.REASON_DETAILS is ca._REASON_DETAIL_DOMAIN
    assert set(ca.REASON_DETAILS) == {"no_task_id", "poll_transient"}
    assert ca.REASON_DETAILS["no_task_id"] == {
        "no_task_id", "invalid_transport_result",
        "network_result_unknown", "interrupted_before_result_commit",
    }
    assert ca.REASON_DETAILS["poll_transient"] == {
        "poll_transient", "result_private_payload_lost",
    }


@pytest.mark.parametrize("reason", sorted({
    "credential_source_missing", "credential_fingerprint_changed",
    "poll_authentication_rejected", "task_unavailable", "poll_timeout",
    "result_pending_timeout", "task_failed", "result_url_expired",
    "unsafe_result_url", "unexpected_result_count",
}))
def test_the_other_ten_reasons_forbid_a_non_null_detail(reason):
    """The ten reasons outside REASON_DETAILS' two keys must reject any
    non-null reason_detail and accept only None.

    The brief this substep is driven from illustrates this via
    `ca._valid_attempt(make_attempt(...), manifest=make_manifest(), ...)`,
    but neither `make_attempt` nor `make_manifest` exists anywhere in this
    file (same brief-vs-reality gap flagged in 1.2/2.1a/2.1b's reports) --
    and for `reason="result_url_expired"` no real `_valid_attempt`-shaped
    attempt can be constructed in this substep's scope at all: no flat_state
    folds onto it yet, so `(state, "result_url_expired")` is not a member of
    LEGAL_STATE_REASON_PAIRS for any state, and `_valid_attempt` would reject
    the pair before ever reaching the reason_detail check -- regardless of
    what reason_detail carries. Wiring a real flat_state onto
    `result_url_expired` requires touching FLAT_STATE_MIGRATION/LEGAL_TRIPLES,
    which this substep's allowed file set forbids.

    `_valid_reason_detail` is the actual, already-existing single-owner
    function this exact relationship is delegated to (see REASON_DETAILS'
    module comment, and test_reason_detail_producer_and_validator_read_one_table
    above, which exercises it the same direct way) -- so it is called
    directly here instead of round-tripping through the unrelated parts of
    `_valid_attempt`'s state machine (task_id shape, timestamps, credential
    fields, ...) that this substep does not touch and that constructing a
    full attempt dict would otherwise have to satisfy for no reason connected
    to what this test is about.
    """
    import conversion_attempt as ca

    assert ca._valid_reason_detail(reason, "no_task_id") is False
    assert ca._valid_reason_detail(reason, None) is True


def test_a_detail_outside_its_reason_bucket_is_rejected():
    import conversion_attempt as ca

    assert (
        ca._valid_reason_detail("no_task_id", "result_private_payload_lost")
        is False
    )


def test_the_locally_detected_result_url_expired_pair_is_legal(
    tmp_path, capsys, monkeypatch
):
    """Task 2.2a fix round 1 (review Critical #1) -- design.md Decision 1
    row 8's second form must be a *legal* stored pair, not merely a member of
    the reason vocabulary.

    `result_url_expired` is the one reason no wire classification produces: it
    is detected locally, when the result URL a `result_ready` attempt already
    holds has passed its validity window. So it has no FLAT_STATE_MIGRATION /
    LEGAL_TRIPLES row -- those two tables characterize the *measured wire*
    domain and stay at 18 rows -- and every derivation that defines legality
    has to splice `LOCALLY_DETECTED_PAIRS` in instead.

    Without that splice `("result_ready", "result_url_expired")` is not in
    LEGAL_STATE_REASON_PAIRS, so `_valid_attempt` rejects any attempt carrying
    it at its very first gate -- which would make tasks 2.2c and 3.1a, both
    written against "2.2a already introduced result_url_expired", hit a wall
    the moment they write the pair out.

    The attempt is not hand-built: it is the real v2 record a bundle driven to
    `result_ready` through main()'s public boundary holds, re-labelled with
    the locally-detected reason and nothing else. Hand-building would prove
    only that a dict of my own devising satisfies the validator; taking the
    genuine record and moving exactly the one column under test is what makes
    a green here mean "a record the production writer could hold is accepted".

    `reason_detail` stays None: `result_url_expired` is not one of
    REASON_DETAILS' two keys, so a non-null detail is forbidden for it.

    This is also the first direct call to `_valid_attempt` anywhere in the
    suite -- every other test reaches it end-to-end through drive/resume --
    and therefore the first assertion that pins its `_valid_reason_detail`
    delegation from the outside.

    2.2b front-loaded fix #4 (2.2a review Minor): the earlier version of this
    test stopped at `_valid_attempt` on a single record. `_MANIFEST_STATE_BY_
    FOLDED_STATE[pair]` (asserted as a bare dict lookup above) is actually
    consumed one level up, by `valid_private_state`'s `expected_manifest_state`
    check (conversion_attempt.py, comparing `manifest["conversion_state"]`
    against the pair's projection) -- so the bundle-level acceptance check
    at the bottom of this test flips the manifest's top-level
    `conversion_state` to that same projection and calls `valid_private_state`
    directly, pinning derivation 2 at the layer that actually reads it.
    """
    import conversion_attempt as ca

    pair = ("result_ready", "result_url_expired")
    assert pair in ca.LEGAL_STATE_REASON_PAIRS
    assert ca._MANIFEST_STATE_BY_FOLDED_STATE[pair] == "recoverable_error"
    # The two characterization tables are untouched: neither describes a
    # locally-detected classification, and a synthetic row in either would
    # destroy their "these 18 are what the wire actually returns" meaning.
    assert len(ca.FLAT_STATE_MIGRATION) == 18
    assert len(ca.LEGAL_TRIPLES) == 18
    assert "result_url_expired" not in {
        reason for _s, reason, _c in ca.FLAT_STATE_MIGRATION.values()
    }

    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    task_id = "task-locally-expired"
    create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate(task_id),
    )
    assert create_rc == 0
    poll_rc, ready, _stderr = invoke(
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
        transport=PollStatus(
            task_id,
            "completed",
            results=[
                {"url": "https://results.aihubmax.com/ready.zip?token=one"}
            ],
        ),
    )
    assert poll_rc == 0
    assert ready["conversion_attempt_state"] == "result_ready"

    manifest = json.loads((bundle / "manifest.json").read_text())
    generation = manifest["generation"]
    attempt = manifest["conversion_attempts"][-1]
    # Control: the genuine record is accepted, and carries the pair the wire
    # produces for this state.
    assert (attempt["state"], attempt["reason"]) == ("result_ready", None)
    assert (
        ca._valid_attempt(attempt, manifest=manifest, generation=generation) is True
    )

    expired = dict(attempt, reason="result_url_expired", reason_detail=None)
    assert (
        ca._valid_attempt(expired, manifest=manifest, generation=generation) is True
    )
    # ...and the detail gate still applies to it: outside REASON_DETAILS'
    # two keys, only None is legal.
    assert (
        ca._valid_attempt(
            dict(expired, reason_detail="no_task_id"),
            manifest=manifest,
            generation=generation,
        )
        is False
    )

    # Bundle-level acceptance (2.2b front-loaded fix #4): the same re-labelled
    # record, at the layer that actually consumes _MANIFEST_STATE_BY_FOLDED_
    # STATE -- valid_private_state's expected_manifest_state check -- rather
    # than only the bare dict lookup asserted above.
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    manifest["conversion_attempts"][-1] = expired
    manifest["conversion_state"] = ca._MANIFEST_STATE_BY_FOLDED_STATE[pair]
    assert ca.valid_private_state(private_state, manifest) is True


def _schema_v1_attempt(attempt, wire_reason_code_by_state):
    """The exact schema v1 record a v1 implementation would have written for
    this v2 attempt.

    v1's single `reason_code` field held today's *wire* reason code, so it is
    rebuilt from the two columns v2 split it into: `reason_detail` for the two
    states whose wire value ranges over a set (submission_unknown,
    poll_transient), and LEGAL_TRIPLES' own `reason_code` column -- the owner
    of the wire value, per its docstring -- for the 14 single-valued contract
    states. The four placeholder rows contribute None, which is what a v1
    attempt in those states carried.
    """
    downgraded = {
        key: value
        for key, value in attempt.items()
        if key in SCHEMA_V1_ATTEMPT_KEYS
    }
    detail = attempt.get("reason_detail")
    downgraded["schema_version"] = 1
    downgraded["reason_code"] = (
        detail
        if detail is not None
        else wire_reason_code_by_state.get(
            (attempt["state"], attempt.get("reason"))
        )
    )
    return downgraded


def test_a_schema_version_one_attempt_fails_closed(tmp_path, capsys, monkeypatch):
    """A schema v1 bundle must be refused by the v2 implementation.

    design.md fixes this as a hard break: no migrator and no dual-write
    compatibility window, so the only correct behaviour is to fail closed --
    rc 4 / invalid_bundle -- with nothing written and nothing sent.

    The rejection is attributed before the CLI runs: `valid_private_state`
    accepts the v2 manifest and refuses the same manifest carrying v1
    attempts, so the rc 4 below cannot be explained away as an incidental
    history-hash mismatch caused by rewriting manifest.json.
    """
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    create_rc, submitted, _stderr = invoke(
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
        transport=SuccessfulCreate("task-schema-v2"),
    )
    assert create_rc == 0
    assert submitted["conversion_attempt_state"] == "submitted"

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    attempt = manifest["conversion_attempts"][-1]

    # Control: the v2 record this bundle actually holds is well formed, and
    # its four new columns carry their 2.1b values (`submitted` folds to no
    # reason at all; authorization_kind remains the placeholder 2.2/2.3 will
    # give meaning to, while result_refresh_round_count starts at 0 before any
    # result URL is observed).
    assert set(attempt) == set(SCHEMA_V2_ATTEMPT_KEYS)
    assert attempt["schema_version"] == 2
    assert attempt["reason"] is None
    assert attempt["reason_detail"] is None
    assert attempt["authorization_kind"] is None
    assert attempt["result_refresh_round_count"] == 0
    assert conversion_attempt.valid_private_state(private_state, manifest) is True

    # Keyed by the folded (state, reason) pair since 2.1c -- that is what a
    # stored record carries. Keying on attempt_state alone would silently keep
    # only the last of the ten rows that share `failed`. The pair is
    # well defined for this column too: the three rows that collapse onto
    # ("processing", None) all carry reason_code None.
    wire_reason_code_by_state = {
        (row.attempt_state, row.reason): row.reason_code
        for row in conversion_attempt.LEGAL_TRIPLES
    }
    v1_manifest = json.loads(json.dumps(manifest))
    v1_manifest["conversion_attempts"] = [
        _schema_v1_attempt(item, wire_reason_code_by_state)
        for item in v1_manifest["conversion_attempts"]
    ]
    assert set(v1_manifest["conversion_attempts"][-1]) == set(
        SCHEMA_V1_ATTEMPT_KEYS
    )
    assert conversion_attempt.valid_private_state(private_state, v1_manifest) is (
        False
    )

    (bundle / "manifest.json").write_text(
        json.dumps(v1_manifest, sort_keys=True, separators=(",", ":"))
    )

    rc, result, _stderr = invoke(
        capsys,
        ["inspect", "--work-bundle", str(bundle)],
        cwd=tmp_path,
        environ=dependencies,
    )

    assert rc == 4, json.dumps(result, sort_keys=True)
    assert result["errors"][0]["code"] == "invalid_bundle"

    # `inspect` cannot demonstrate the "nothing written, nothing sent" half of
    # fail-closed: workflow._inspect's signature (`def _inspect(args, *, cwd:
    # Path)`) never receives a transport at all, and _inspect_bundle only
    # reads manifest.json -- so `transport.calls == []` and an unchanged byte
    # snapshot would hold here for *any* bundle, valid or not, regardless of
    # whether the schema check works. Drive the same v1 bundle through
    # `resume` instead: it does receive a transport, and does write when it
    # has work to do (the same pattern
    # test_capacity_exhaustion_stops_before_intent_and_leaves_bytes_untouched
    # uses transport/byte-snapshot assertions for). resume's recovery chain
    # (recover_pending_operation / recover_interrupted_adoption /
    # conversion_attempt_module.recover_interrupted_attempt / ...) runs ahead
    # of the schema check (workflow._resume calls _inspect_bundle only after
    # those), so seeing zero transport calls and zero byte drift here is real
    # evidence the v1 rejection happens before any of that recovery machinery
    # could reach the network or the disk -- not a tautology of the command's
    # shape.
    resume_before = _bundle_state_snapshot(bundle)
    resume_transport = CountingNeverNetwork()
    resume_rc, resume_result, _resume_stderr = invoke(
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
        transport=resume_transport,
    )

    assert resume_rc == 4, json.dumps(resume_result, sort_keys=True)
    assert resume_result["errors"][0]["code"] == "invalid_bundle"
    assert resume_transport.calls == []
    assert _bundle_state_snapshot(bundle) == resume_before


# --- Task 1.3: characterization of the pre-fold flat state projections -----
#
# Everything below pins the *observable* (conversion_state,
# conversion_attempt_state, outcome, action_required) quadruple each flat
# state produces through main()'s public boundary today, before task 2.1c
# folds 18 flat states down to 7. drive_to_flat_state and
# FLAT_STATE_OBSERVABLES are produced here for 2.1c/4.3 to reuse.


class _CapturedIO:
    """Ambient stdout/stderr capture that duck-types capsys's readouterr()
    (an object with .out/.err) without requiring the real capsys fixture.

    drive_to_flat_state runs inside a parametrized test that only requests
    (tmp_path, flat_state) -- matching the reusable interface task 2.1c/4.3
    are meant to call -- so it cannot receive capsys via fixture injection.
    invoke()/ready_staged_bundle() already assume an object shaped like
    capsys, so this swaps sys.stdout/sys.stderr for plain StringIO buffers
    (the same technique capsys itself uses under the hood) and hands that
    back out, letting every existing helper run completely unmodified.

    A context manager rather than a plain object with a close() method: the
    swap must always be paired with a restore, and making that pairing a
    type-level property (enter/exit) means callers can't forget the
    restore the way a bare close() call relies on the caller remembering to
    make (and to make even when the wrapped body raises).
    """

    def __init__(self):
        self._out = io.StringIO()
        self._err = io.StringIO()
        self._real_out = None
        self._real_err = None

    def __enter__(self):
        self._real_out = sys.stdout
        self._real_err = sys.stderr
        sys.stdout = self._out
        sys.stderr = self._err
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.stdout = self._real_out
        sys.stderr = self._real_err
        return False

    def readouterr(self):
        out = self._out.getvalue()
        err = self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return SimpleNamespace(out=out, err=err)


_drive_to_flat_state_call_counter = itertools.count()


def drive_to_flat_state(tmp_path, flat_state):
    """Drive a fresh work bundle to `flat_state` through main()'s public
    CLI boundary and return the machine result JSON of the call that lands
    on it.

    Reuses the existing fixture classes (StatusCreate, PollStatus,
    CrashAfterCreate) and ready_staged_bundle exactly as today's other
    tests do; the only new mechanism is _CapturedIO, which stands in for
    the capsys fixture (see its docstring).

    Each call gets its own subdirectory of tmp_path -- callers (this
    module's invisible-branches test included) may drive more than one
    flat_state off a single tmp_path fixture, and ready_staged_bundle's
    install_preflight_dependencies() would otherwise collide creating the
    same python-packages/bs4 stub twice under one shared directory. The
    directory name carries a monotonic call counter (not just flat_state)
    so that repeated calls for the *same* flat_state under one tmp_path --
    e.g. a fold-before/fold-after comparison -- get distinct scratch
    directories instead of colliding on mkdir().
    """
    call_root = tmp_path / (
        f"drive-{flat_state}-{next(_drive_to_flat_state_call_counter)}"
    )
    call_root.mkdir()
    with _CapturedIO() as capture:
        with pytest.MonkeyPatch.context() as monkeypatch:
            bundle, staged, dependencies, key, _source_url, _source_sha256 = (
                ready_staged_bundle(call_root, capture, monkeypatch)
            )
            create_environ = {**dependencies, "AIHUB_API_KEY": key}

            def _resume(transport, *, expected_generation, environ=None, now=NOW):
                _rc, result, _stderr = invoke(
                    capture,
                    [
                        "resume",
                        "--work-bundle",
                        str(bundle),
                        "--expected-generation",
                        str(expected_generation),
                    ],
                    cwd=call_root,
                    environ=create_environ if environ is None else environ,
                    transport=transport,
                    now=now,
                )
                return result

            if flat_state == "not_started":
                unknown = _resume(
                    StatusCreate(401), expected_generation=staged["generation"]
                )
                _rc, authorized, _stderr = invoke(
                    capture,
                    [
                        "record",
                        "conversion",
                        "--work-bundle",
                        str(bundle),
                        "--expected-generation",
                        str(unknown["generation"]),
                        "--action-id",
                        unknown["action_id"],
                        "--evidence-hash",
                        unknown["evidence_hash"],
                        "--decision",
                        "retry",
                        "--basis",
                        "characterization drive to not_started.",
                    ],
                    cwd=call_root,
                    environ=dependencies,
                    transport=NeverNetwork(),
                )
                return authorized

            if flat_state == "submission_unknown":
                return _resume(
                    StatusCreate(401), expected_generation=staged["generation"]
                )

            if flat_state == "submitting":
                # begin_attempt commits the "submitting" checkpoint to disk
                # before create_task is called; a crash there (simulating a
                # killed process, same as the existing crash-recovery
                # tests) leaves that checkpoint on disk without workflow.py
                # ever getting to print a result for this call. inspect
                # (not resume) then reads the bundle back without running
                # any further recovery/business logic, which is the only
                # way this flat state is observable through main().
                with pytest.raises(SimulatedProcessCrash):
                    workflow.main(
                        [
                            "resume",
                            "--work-bundle",
                            str(bundle),
                            "--expected-generation",
                            str(staged["generation"]),
                        ],
                        environ=create_environ,
                        cwd=str(call_root),
                        config_home=str(call_root / "config-home"),
                        transport=CrashAfterCreate(),
                        now=NOW,
                    )
                capture.readouterr()
                _rc, inspected, _stderr = invoke(
                    capture,
                    ["inspect", "--work-bundle", str(bundle)],
                    cwd=call_root,
                    environ=dependencies,
                    transport=NeverNetwork(),
                )
                return inspected

            task_id = f"task-{flat_state}"
            submitted = _resume(
                SuccessfulCreate(task_id), expected_generation=staged["generation"]
            )

            if flat_state == "submitted":
                return submitted
            if flat_state == "pending":
                return _resume(
                    PollStatus(task_id, "pending"),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "processing":
                return _resume(
                    PollStatus(task_id, "processing"),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "result_pending":
                return _resume(
                    PollStatus(task_id, "completed", results=[]),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "result_ready":
                return _resume(
                    PollStatus(
                        task_id,
                        "completed",
                        results=[
                            {
                                "url": (
                                    "https://results.aihubmax.com/ready.zip"
                                    "?token=one"
                                )
                            }
                        ],
                    ),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "unsafe_result_url":
                return _resume(
                    PollStatus(
                        task_id,
                        "completed",
                        results=[
                            {"url": "http://results.aihubmax.com/unsafe.zip"}
                        ],
                    ),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "unexpected_result_count":
                return _resume(
                    PollStatus(
                        task_id,
                        "completed",
                        results=[
                            {"url": "https://results.aihubmax.com/a.zip?token=one"},
                            {"url": "https://results.aihubmax.com/b.zip?token=two"},
                        ],
                    ),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "failed":
                return _resume(
                    PollStatus(
                        task_id,
                        "failed",
                        error={"message": "characterization failure"},
                    ),
                    expected_generation=submitted["generation"],
                )
            if flat_state == "poll_transient":
                return _resume(
                    StatusCreate(429), expected_generation=submitted["generation"]
                )
            if flat_state == "poll_unauthorized":
                return _resume(
                    StatusCreate(401), expected_generation=submitted["generation"]
                )
            if flat_state == "task_unavailable":
                return _resume(
                    StatusCreate(404), expected_generation=submitted["generation"]
                )
            if flat_state == "credential_source_missing":
                return _resume(
                    NeverNetwork(),
                    expected_generation=submitted["generation"],
                    environ=dependencies,
                )
            if flat_state == "credential_source_changed":
                return _resume(
                    NeverNetwork(),
                    expected_generation=submitted["generation"],
                    environ={**dependencies, "AIHUB_API_KEY": "a-different-key"},
                )
            if flat_state == "poll_timeout":
                processing = _resume(
                    PollStatus(task_id, "processing"),
                    expected_generation=submitted["generation"],
                    now=NOW,
                )
                return _resume(
                    NeverNetwork(),
                    expected_generation=processing["generation"],
                    now=datetime(2024, 1, 2, 3, 16, 5, tzinfo=timezone.utc),
                )
            if flat_state == "result_pending_timeout":
                pending = _resume(
                    PollStatus(task_id, "completed", results=[]),
                    expected_generation=submitted["generation"],
                    now=NOW,
                )
                return _resume(
                    NeverNetwork(),
                    expected_generation=pending["generation"],
                    now=datetime(2024, 1, 2, 3, 16, 5, tzinfo=timezone.utc),
                )
            raise AssertionError(
                f"drive_to_flat_state: unknown flat_state {flat_state!r}"
            )


FLAT_STATE_OBSERVABLES = {
    # not_started/pending/processing/submitting corrected from the
    # illustrative plan values to what main() actually returns (task 1.3
    # characterization runs the real CLI and records its output, not what
    # the plan predicted):
    #  - not_started: the record path's outcome literal is
    #    "conversion_retry_authorized" (workflow.py's only
    #    "conversion_authorized"-shaped literal is
    #    "conversion_retry_authorized"; plain "conversion_authorized" is
    #    not a string this codebase ever prints). `record conversion`
    #    prints this outcome unconditionally (workflow.py:3024-3025 sets it
    #    for every `record conversion` call, independent of the attempt
    #    state being recorded against) -- it is command-derived, does not
    #    carry state information, and therefore does NOT constitute
    #    evidence that folding leaves outcome unchanged.
    #  - pending/processing: an ordinary poll's outcome falls through
    #    workflow.py's `f"conversion_{poll_result.state}"` default (the same
    #    rule result_pending's sibling result_ready overrides but pending/
    #    processing do not), so the printed outcome is
    #    "conversion_pending"/"conversion_processing", not the bare state
    #    name.
    #  - submitting: only reachable by crashing the process between
    #    begin_attempt's checkpoint write and create_task's completion
    #    (CrashAfterCreate, same fixture the existing crash-recovery tests
    #    use), then reading the bundle back with `inspect` -- the one
    #    command that returns a result without also recovering the
    #    crash forward past "submitting". `inspect` always reports outcome
    #    "inspected"; "conversion_submitting" is not a string any production
    #    code path prints. `inspect` prints "inspected" for any state it
    #    observes (workflow.py:1269 for the has_conversion_attempt branch,
    #    :1285 for the generic fallback) -- it is command-derived, not
    #    state-derived, so this outcome cell does NOT constitute evidence
    #    that folding leaves outcome unchanged. The other three columns on
    #    this row are not discounted: conversion_state,
    #    conversion_attempt_state, and action_required all come from the
    #    same conversion_attempt.result_from_manifest projection
    #    (workflow.py:1267-1270) that produces the other 17 rows -- only
    #    the outcome cell carries this discount, not the whole row.
    "not_started": (
        "ready_to_submit", "not_started", "conversion_retry_authorized", None
    ),
    "submitting": ("submitting", "submitting", "inspected", None),
    "submitted": ("submitted", "submitted", "conversion_submitted", None),
    "submission_unknown": (
        "submission_unknown", "submission_unknown", "submission_unknown",
        "resolve_submission_unknown",
    ),
    "pending": ("submitted", "pending", "conversion_pending", None),
    "processing": ("submitted", "processing", "conversion_processing", None),
    "result_pending": ("submitted", "result_pending", "result_pending", None),
    "result_ready": ("result_downloading", "result_ready", "result_ready", None),
    "unsafe_result_url": (
        "terminal_error", "unsafe_result_url", "unsafe_result_url", None
    ),
    "unexpected_result_count": (
        "terminal_error", "unexpected_result_count", "unexpected_result_count",
        "resolve_unexpected_result_count",
    ),
    "failed": ("awaiting_user", "failed", "task_failed", "resolve_task_failed"),
    "poll_transient": ("recoverable_error", "poll_transient", "poll_transient", None),
    "poll_unauthorized": (
        "recoverable_error", "poll_unauthorized", "poll_unauthorized", None
    ),
    "task_unavailable": (
        "recoverable_error", "task_unavailable", "task_unavailable", None
    ),
    "credential_source_missing": (
        "recoverable_error", "credential_source_missing",
        "credential_source_missing", None,
    ),
    "credential_source_changed": (
        "recoverable_error", "credential_source_changed",
        "credential_source_changed", None,
    ),
    "poll_timeout": ("recoverable_error", "poll_timeout", "poll_timeout", None),
    "result_pending_timeout": (
        "recoverable_error", "result_pending_timeout", "result_pending_timeout", None,
    ),
}


# ⚠️ 2026-07-26（任务 2.1c）：原先此处有一条参数化用例
# test_flat_state_observable_projection_is_pinned，拿 FLAT_STATE_OBSERVABLES
# 直接比 main() 的实时输出。折叠落地后它描述的是**折叠前**的合同，已改指向
# 折叠后的对照表——见本文件末尾的
# test_folded_state_observable_projection_is_pinned（同样的驱动器、同样的四
# 元组、同样 18 个参数），以及证明「折叠只动了该动的那一格」的
# test_the_fold_moves_exactly_the_attempt_state_cell_and_nothing_else。
#
# FLAT_STATE_OBSERVABLES 本身**一格未改**，作为折叠前基线保留：它是上面那条
# 无损性断言唯一的对照物，覆写或删除它就等于销毁证据。


def _drive_submission_unknown_detail(tmp_path, capsys, monkeypatch, detail):
    """Drive a fresh work bundle to `submission_unknown` through the specific
    create-classification (or crash-recovery) branch named by `detail`, and
    return the machine result of the call that lands on it.

    The four `detail` values are driven by four genuinely different
    mechanisms (doc2x._classify's status/task_id checks for the first two,
    create_task's broad `except Exception` for the third, and
    recover_interrupted_attempt's "conversion_submit_started with no
    completion" branch for the fourth) -- not by a single parametrized
    transport, because no single fixture produces all four.
    """
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    if detail == "interrupted_before_result_commit":
        # A crash during/after the create call, with the process never
        # reaching the point where it commits a definite create outcome,
        # leaves a durable `conversion_submit_started` checkpoint that the
        # next resume's recover_interrupted_attempt folds onto
        # ("submission_unknown", "no_task_id") with this detail --
        # regardless of whether the crash happened before or after the
        # network call itself (test_a_crash_after_submit_intent_recovers_..
        # above exercises the "before" boundary and asserts the identical
        # pairing; CrashAfterCreate exercises the "during" one here).
        create = CrashAfterCreate()
        with pytest.raises(SimulatedProcessCrash):
            workflow.main(
                [
                    "resume",
                    "--work-bundle",
                    str(bundle),
                    "--expected-generation",
                    str(staged["generation"]),
                ],
                environ=environ,
                cwd=str(tmp_path),
                config_home=str(tmp_path / "config-home"),
                transport=create,
                now=NOW,
            )
        capsys.readouterr()
        recovered_rc, recovered, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"] + 1),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=NeverNetwork(),
        )
        assert recovered_rc == 0
        return recovered

    # The other three details are all decided synchronously inside a single
    # create_task call, off the shape of the (simulated) HTTP response:
    #   - no_task_id: a well-formed status the wire actually uses (401) that
    #     carries no task id -- doc2x._classify's fallback branch.
    #   - invalid_transport_result: a response whose `.status` is not an int
    #     in 100..599 at all -- _classify's very first guard. StatusCreate
    #     accepts any status value verbatim (see its __init__ above), so
    #     StatusCreate(None) exercises this without a new fixture class.
    #   - network_result_unknown: the transport raises before a Response
    #     ever comes back -- create_task's `except Exception` catches it.
    #     LostPoll raises a plain OSError (Exception, not
    #     SimulatedProcessCrash's BaseException) regardless of which step
    #     calls it; used here as the *create* transport it exercises this
    #     branch, distinct from its usual role driving poll_transient.
    transport = {
        "no_task_id": StatusCreate(401),
        "invalid_transport_result": StatusCreate(None),
        "network_result_unknown": LostPoll(),
    }[detail]
    rc, result, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
        ],
        cwd=tmp_path,
        environ=environ,
        transport=transport,
    )
    assert rc == 0
    return result


@pytest.mark.parametrize("detail", sorted({
    "no_task_id", "invalid_transport_result",
    "network_result_unknown", "interrupted_before_result_commit",
}))
def test_submission_unknown_branches_are_visible_to_callers(
    tmp_path, capsys, monkeypatch, detail
):
    result = _drive_submission_unknown_detail(tmp_path, capsys, monkeypatch, detail)
    assert result["conversion_attempt_state"] == "submission_unknown"
    assert result["conversion_attempt_reason"] == "no_task_id"
    assert result["conversion_attempt_reason_detail"] == detail


def test_reason_and_detail_keys_always_exist_even_when_null(tmp_path):
    """Decision 3: the two fields are unconditional -- present and null for
    a single-valued state, not merely absent."""
    result = drive_to_flat_state(tmp_path, "submitted")
    assert result["conversion_attempt_reason"] is None
    assert result["conversion_attempt_reason_detail"] is None


def _drive_and_run_command(tmp_path, capsys, monkeypatch, command):
    """Drive a bundle to a state where a conversion attempt with a non-null
    reason exists, then run `command` against it, returning the machine
    result of that call.

    Each of the four commands reaches conversion_attempt.result_from_manifest
    (directly, or through a wrapper that calls it) by a different route:
    resume/inspect read it straight off the manifest; record replays a
    decision through it; advance's recovery path
    (conversion_attempt_module.recover_interrupted_attempt, workflow.py:1541)
    is exercised the same way _drive_submission_unknown_detail's
    interrupted_before_result_commit branch exercises resume's recovery path.
    """
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(tmp_path, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}

    if command == "resume":
        rc, result, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=StatusCreate(401),
        )
        assert rc == 0
        return result

    if command == "inspect":
        _resume_rc, _unknown, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=StatusCreate(401),
        )
        rc, result, _stderr = invoke(
            capsys,
            ["inspect", "--work-bundle", str(bundle)],
            cwd=tmp_path,
            environ=environ,
            transport=NeverNetwork(),
        )
        assert rc == 0
        return result

    if command == "record":
        _resume_rc, unknown, _stderr = invoke(
            capsys,
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"]),
            ],
            cwd=tmp_path,
            environ=environ,
            transport=StatusCreate(401),
        )
        rc, result, _stderr = invoke(
            capsys,
            [
                "record",
                "conversion",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(unknown["generation"]),
                "--action-id",
                unknown["action_id"],
                "--evidence-hash",
                unknown["evidence_hash"],
                "--decision",
                "retry",
                "--basis",
                "task 2.2b four-command projection coverage.",
            ],
            cwd=tmp_path,
            environ=environ,
            transport=NeverNetwork(),
        )
        assert rc == 0
        return result

    if command == "advance":
        create = CrashAfterCreate()
        with pytest.raises(SimulatedProcessCrash):
            workflow.main(
                [
                    "resume",
                    "--work-bundle",
                    str(bundle),
                    "--expected-generation",
                    str(staged["generation"]),
                ],
                environ=environ,
                cwd=str(tmp_path),
                config_home=str(tmp_path / "config-home"),
                transport=create,
                now=NOW,
            )
        capsys.readouterr()
        rc, result, _stderr = invoke(
            capsys,
            [
                "advance",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(staged["generation"] + 1),
                "--visual-capability",
                "available",
            ],
            cwd=tmp_path,
            environ=environ,
            transport=NeverNetwork(),
        )
        assert rc == 0
        return result

    raise AssertionError(f"_drive_and_run_command: unknown command {command!r}")


# The driven conversion_attempt_state each _drive_and_run_command branch
# actually lands on. inspect/advance/resume all read (or recover into) the
# submission_unknown record StatusCreate(401) produces; record replays a
# "retry" decision through it, which re-authorizes a fresh not-yet-submitted
# attempt -- authorized, not submission_unknown.
_FOUR_COMMAND_DRIVEN_STATES = {
    "inspect": "submission_unknown",
    "advance": "submission_unknown",
    "resume": "submission_unknown",
    "record": "authorized",
}


@pytest.mark.parametrize("command", ["inspect", "advance", "resume", "record"])
def test_all_four_commands_expose_both_reason_fields(
    tmp_path, capsys, monkeypatch, command
):
    """2.2b review front-load #1 -- the original name ("expose_the_same_
    reason_fields") only proved all four commands expose the same *keys*, not
    that the driven state is actually the one every branch below assumes.
    Anchoring conversion_attempt_state pins the drive path itself, so a future
    change to _drive_and_run_command that silently lands a different branch
    turns this red instead of continuing to pass on an unintended state.
    """
    result = _drive_and_run_command(tmp_path, capsys, monkeypatch, command)
    assert (
        result["conversion_attempt_state"] == _FOUR_COMMAND_DRIVEN_STATES[command]
    ), command
    assert "conversion_attempt_reason" in result
    assert "conversion_attempt_reason_detail" in result


# (reason, reason_detail) drive_to_flat_state actually lands on for the two
# flat_states test_submission_unknown_and_poll_transient_branches_are_
# visible_after_closure's main loop drives. Paired with FOLDED_STATE_
# OBSERVABLES' four-tuple, this is the precise "every detail has at least one
# exact-value assertion" companion 2.2b review front-load #2 requires.
_DRIVE_TO_FLAT_STATE_REASON_PAIRS = {
    "submission_unknown": ("no_task_id", "no_task_id"),
    "poll_transient": ("poll_transient", "poll_transient"),
}


def test_submission_unknown_and_poll_transient_branches_are_visible_after_closure(
    tmp_path, capsys, monkeypatch
):
    """任务 2.2b 反转（本计划唯一明许的断言反转）。

    这是 task 1.3 的 test_submission_unknown_and_poll_transient_branches_are_
    invisible_today 就地反转：那条测试把「盲区确实存在」钉死，好让这里的
    可观测性净增可被证明而不是被声称；`conversion_attempt.result_from_manifest`
    现在无条件写入 conversion_attempt_reason / conversion_attempt_reason_detail
    （Decision 3：恒存在，值可为 null），两个多分支 state 的分支信息不再对调用
    方不可见。

    整四元组比对沿用不变（与钉表 FOLDED_STATE_OBSERVABLES 共用同一份真相，
    "submitting" 那一行的 outcome discount 同样适用）——本步骤只在同一驱动路径
    上新增对两个 reason 字段"存在且非空"的断言，不放松四元组本身。

    旧账（1.3 复审 Minor #7，转派到本子步骤）：poll_transient 折叠自两个不同
    reason_detail——"poll_transient"（drive_to_flat_state 走的
    StatusCreate(429) 分支）与 "result_private_payload_lost"（仅
    recover_interrupted_attempt 在崩溃恢复时，找到一个 result_ready 决定的
    durable conversion_poll_result_intent 但其 private payload 从未落盘时才会
    产生——见 conversion_attempt.py 的 recover_interrupted_attempt 与
    test_poll_result_journal_recovers_each_write_boundary_without_leaking_
    result_url 的 boundary="private" 分支）。只覆盖前者，「可观测性净增」就只
    证明了一半：本测试在主循环之后额外驱动崩溃恢复分支，把两个 reason 都摆到
    可观测这一侧。

    2.2b review front-load #2: the loop below used to pin only "is not None"
    on the two reason fields, which cannot distinguish "the right value" from
    "some other non-null value a future regression accidentally produces".
    _DRIVE_TO_FLAT_STATE_REASON_PAIRS closes that: submission_unknown is
    driven by StatusCreate(401), whose wire reason_code doc2x._classify folds
    to "no_task_id" -- both the folded reason and its reason_detail (task
    2.1c's _attempt_reason_columns) land on that same value. poll_transient is
    driven by StatusCreate(429) acting as the poll transport, whose wire
    reason_code is "poll_transient" itself -- again identical on both columns.
    """
    for flat_state in ("submission_unknown", "poll_transient"):
        result = drive_to_flat_state(tmp_path, flat_state)
        # 2.1c：改指向折叠后的对照表。整四元组比对这一点没有放松，只是对照物
        # 换成了折叠后的真值——继续沿用折叠前的表会让本用例断言一个已经不成立
        # 的合同，而不是继续钉住「两个分支的判别信息不可见」这条基线。
        assert (
            result["conversion_state"],
            result["conversion_attempt_state"],
            result["outcome"],
            result.get("action_required"),
        ) == FOLDED_STATE_OBSERVABLES[flat_state], flat_state
        assert (
            result["conversion_attempt_reason"],
            result["conversion_attempt_reason_detail"],
        ) == _DRIVE_TO_FLAT_STATE_REASON_PAIRS[flat_state], flat_state

    # poll_transient's second reason_detail, result_private_payload_lost, is
    # never reached through drive_to_flat_state (which only exercises the
    # StatusCreate(429) branch above) -- it is a crash-recovery-only outcome,
    # so it needs its own drive through the same private-write-boundary crash
    # test_poll_result_journal_recovers_each_write_boundary_without_leaking_
    # result_url already exercises with boundary="private".
    lost_payload_root = tmp_path / "poll-transient-payload-lost"
    lost_payload_root.mkdir()
    bundle, staged, dependencies, key, _source_url, _source_sha256 = (
        ready_staged_bundle(lost_payload_root, capsys, monkeypatch)
    )
    environ = {**dependencies, "AIHUB_API_KEY": key}
    create_rc, submitted, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
        ],
        cwd=lost_payload_root,
        environ=environ,
        transport=SuccessfulCreate("task-lost-private-payload"),
    )
    assert create_rc == 0
    result_url = "https://results.aihubmax.com/lost-payload.zip?token=lost"
    original_atomic_write, original_append_history = _install_conversion_journal_crash(
        monkeypatch,
        event="conversion_poll_result_committed",
        boundary="private",
    )
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            [
                "resume",
                "--work-bundle",
                str(bundle),
                "--expected-generation",
                str(submitted["generation"]),
            ],
            environ=environ,
            cwd=str(lost_payload_root),
            config_home=str(lost_payload_root / "config-home"),
            transport=PollStatus(
                "task-lost-private-payload", "completed", results=[{"url": result_url}]
            ),
            now=NOW,
        )
    capsys.readouterr()
    monkeypatch.setattr(
        conversion_attempt.bundle, "atomic_write_json", original_atomic_write
    )
    monkeypatch.setattr(
        conversion_attempt.bundle, "append_history", original_append_history
    )
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(submitted["generation"]),
        ],
        cwd=lost_payload_root,
        environ=environ,
        transport=NeverNetwork(),
    )
    assert recovered_rc == 0
    assert recovered["conversion_attempt_state"] == "failed"
    assert recovered["conversion_attempt_reason"] == "poll_transient"
    assert recovered["conversion_attempt_reason_detail"] == "result_private_payload_lost"


def test_drive_to_flat_state_can_be_called_twice_for_the_same_state(tmp_path):
    """Task 2.1c drives a fold-before/fold-after comparison off the same
    flat_state under one tmp_path fixture; drive_to_flat_state must tolerate
    being called more than once for the same flat_state without colliding on
    its scratch directory.
    """
    first = drive_to_flat_state(tmp_path, "submitted")
    second = drive_to_flat_state(tmp_path, "submitted")
    assert first["conversion_attempt_state"] == "submitted"
    assert second["conversion_attempt_state"] == "submitted"


# --- Task 2.1c: the folded seven-value attempt state domain ----------------


def test_attempt_state_domain_is_closed_to_seven_values():
    import conversion_attempt as ca

    assert ca.ATTEMPT_STATES == {
        "authorized", "submitting", "submitted", "processing",
        "failed", "result_ready", "submission_unknown",
    }
    assert {row.attempt_state for row in ca.LEGAL_TRIPLES} == ca.ATTEMPT_STATES


def test_every_refolded_pair_set_names_a_legal_pair():
    """折叠这一步最危险的失败形态：把某处 `state in {...}` 改写成
    `(state, reason) in {...}` 时把 reason 拼错。

    拼错不会抛异常——那条规则只是从此永不命中，静默失效。最容易踩的两个是
    折叠时被改名的 `credential_source_changed → credential_fingerprint_changed`
    和 `poll_unauthorized → poll_authentication_rejected`：沿用旧名字看起来
    完全合理，却指向一个任何记录都不可能携带的 pair。

    这里把每一处重新以 (state, reason) 为键的集合都钉在
    LEGAL_STATE_REASON_PAIRS 内。LEGAL_STATE_REASON_PAIRS 由
    FLAT_STATE_MIGRATION 派生，而后者的 18 行由本文件的独立字面量 oracle
    （FOLDED_PAIR_BY_FLAT_STATE、FLAT_STATE_MIGRATION 覆盖域测试）钉住，所以
    这不是同义反复。

    workflow.py 的 RESUMABLE_RECOVERABLE_ATTEMPT_PAIRS 额外钉在一条**等值**
    oracle 上，而不是子集：子集断言只能抓住『多出一个非法 pair』，对『少掉
    一个合法 pair』完全瞎——而漏掉一个可续 poll 的 pair 正是这个集合最危险
    的失效模式（resume 会对携带该 pair 的记录静默停止轮询）。等值右侧由
    conversion_attempt 的 POLL_ACTIVE_ATTEMPT_PAIRS 和
    _MANIFEST_STATE_BY_FOLDED_STATE 现算而来，不是同一张手写表的复制。
    """
    import conversion_attempt as ca
    import workflow as wf

    assert set(ca._REFOLDED_PAIR_SETS) == {
        "POLL_ACTIVE_ATTEMPT_PAIRS", "CONFIRMABLE_PAIRS", "_BACKOFF_PAIRS",
        "_CREDENTIAL_ERROR_PAIRS", "_POLL_DEADLINE_PAIRS",
        "_POLL_WINDOW_RESET_PAIRS", "_PROCESSING_PAIR",
        "_POLL_TRANSIENT_PAIR", "_RESULT_PENDING_TIMEOUT_PAIR",
    }
    for name, pairs in ca._REFOLDED_PAIR_SETS.items():
        assert pairs, name
        assert set(pairs) <= set(ca.LEGAL_STATE_REASON_PAIRS), name
    # workflow.py 的那一处也必须是合法 pair。
    assert set(wf.RESUMABLE_RECOVERABLE_ATTEMPT_PAIRS) <= set(
        ca.LEGAL_STATE_REASON_PAIRS
    )
    # ...而且必须恰好是 POLL_ACTIVE_ATTEMPT_PAIRS 里投影到 recoverable_error
    # 的那些 pair——不多不少。子集断言抓不住『少了一个』，等值可以：右侧独立
    # 现算，任何一处遗漏或多余都会让两边不等，从而让测试变红。
    assert set(wf.RESUMABLE_RECOVERABLE_ATTEMPT_PAIRS) == {
        pair
        for pair in ca.POLL_ACTIVE_ATTEMPT_PAIRS
        if ca._MANIFEST_STATE_BY_FOLDED_STATE[pair] == "recoverable_error"
    }


def test_poll_admission_is_keyed_by_state_and_reason_not_state_alone():
    """折叠后 failed 同时覆盖可续 poll 与不可续 poll 两类，state 单独已失去判别力。"""
    import conversion_attempt as ca

    assert ("failed", "poll_transient") in ca.POLL_ACTIVE_ATTEMPT_PAIRS
    assert ("failed", "unsafe_result_url") in ca.POLL_ACTIVE_ATTEMPT_PAIRS
    assert ("failed", "task_failed") not in ca.POLL_ACTIVE_ATTEMPT_PAIRS
    assert ("failed", "unexpected_result_count") not in (
        ca.POLL_ACTIVE_ATTEMPT_PAIRS
    )


# FOLDED_STATE_OBSERVABLES is the post-fold twin of FLAT_STATE_OBSERVABLES:
# the same (conversion_state, conversion_attempt_state, outcome,
# action_required) quadruple, driven through the same drive_to_flat_state and
# main() boundary, recorded after task 2.1c folded the stored attempt state
# from 18 flat values to 7.
#
# It is written as an INDEPENDENT LITERAL, not as a comprehension over
# conversion_attempt.FLAT_STATE_MIGRATION. Deriving it would make
# test_folded_state_observable_projection_is_pinned compare the production
# table with itself the moment the fold lands (the `x == x` self-proof the
# 2.1a review caught), and would make the losslessness test below vacuous.
#
# FLAT_STATE_OBSERVABLES above is deliberately left untouched: it is task
# 1.3's pre-fold baseline snapshot and the only material that can show the
# fold lost nothing. Do not update its cells.
FOLDED_STATE_OBSERVABLES = {
    "not_started": (
        "ready_to_submit", "authorized", "conversion_retry_authorized", None
    ),
    "submitting": ("submitting", "submitting", "inspected", None),
    "submitted": ("submitted", "submitted", "conversion_submitted", None),
    "submission_unknown": (
        "submission_unknown", "submission_unknown", "submission_unknown",
        "resolve_submission_unknown",
    ),
    "pending": ("submitted", "processing", "conversion_pending", None),
    "processing": ("submitted", "processing", "conversion_processing", None),
    "result_pending": ("submitted", "processing", "result_pending", None),
    "result_ready": ("result_downloading", "result_ready", "result_ready", None),
    "unsafe_result_url": ("terminal_error", "failed", "unsafe_result_url", None),
    "unexpected_result_count": (
        "terminal_error", "failed", "unexpected_result_count",
        "resolve_unexpected_result_count",
    ),
    "failed": ("awaiting_user", "failed", "task_failed", "resolve_task_failed"),
    "poll_transient": ("recoverable_error", "failed", "poll_transient", None),
    "poll_unauthorized": (
        "recoverable_error", "failed", "poll_unauthorized", None
    ),
    "task_unavailable": ("recoverable_error", "failed", "task_unavailable", None),
    "credential_source_missing": (
        "recoverable_error", "failed", "credential_source_missing", None,
    ),
    "credential_source_changed": (
        "recoverable_error", "failed", "credential_source_changed", None,
    ),
    "poll_timeout": ("recoverable_error", "failed", "poll_timeout", None),
    "result_pending_timeout": (
        "recoverable_error", "failed", "result_pending_timeout", None,
    ),
}


@pytest.mark.parametrize("flat_state", sorted(FOLDED_STATE_OBSERVABLES))
def test_folded_state_observable_projection_is_pinned(tmp_path, flat_state):
    result = drive_to_flat_state(tmp_path, flat_state)
    expected = FOLDED_STATE_OBSERVABLES[flat_state]
    assert (
        result["conversion_state"],
        result["conversion_attempt_state"],
        result["outcome"],
        result.get("action_required"),
    ) == expected
    assert set(FOLDED_STATE_OBSERVABLES) == conversion_attempt.FLAT_STATE_DOMAIN


# The two rows whose `outcome` cell is COMMAND-derived rather than
# state-derived, per FLAT_STATE_OBSERVABLES' own notes:
#   - not_started: `record conversion` prints "conversion_retry_authorized"
#     unconditionally for every call, whatever attempt state it records
#     against.
#   - submitting: `inspect` prints "inspected" for every state it observes.
# Neither cell carries state information, so an unchanged value there is not
# evidence that the fold left `outcome` alone. They are excluded from the
# outcome comparison below (task 4.4's additional acceptance note); their
# other three cells are NOT discounted and are compared like every other row.
_COMMAND_DERIVED_OUTCOME_ROWS = frozenset({"not_started", "submitting"})


def test_the_fold_moves_exactly_the_attempt_state_cell_and_nothing_else():
    """折叠无损性的正面证明。

    「新表全绿」只说明折叠后的行为被钉住了，不说明折叠没有顺带改掉别的东西。
    这里逐格比对折叠前基线（FLAT_STATE_OBSERVABLES，任务 1.3）与折叠后快照
    （FOLDED_STATE_OBSERVABLES），要求两者的差异**恰好**等于
    FLAT_STATE_MIGRATION 规定的差异：

      - 第 0 格 conversion_state：折叠前后必须相等，且必须等于迁移表第三列
        （迁移表没有承诺改它，所以它必须一格不动）；
      - 第 1 格 conversion_attempt_state：折叠前必须是扁平值本身，折叠后必须
        恰好是迁移表第一列——这是唯一允许移动的一格；
      - 第 2 格 outcome：必须相等（两行命令派生的除外，见上方注释）；
      - 第 3 格 action_required：必须相等（kind 折叠是任务 2.4，不在本步）。

    两张表都是独立字面量，迁移表是生产代码，所以本断言不会退化成同义反复。
    """
    import conversion_attempt as ca

    assert set(FOLDED_STATE_OBSERVABLES) == set(FLAT_STATE_OBSERVABLES)
    assert set(FLAT_STATE_OBSERVABLES) == set(ca.FLAT_STATE_MIGRATION)
    moved = set()
    for flat in sorted(FLAT_STATE_OBSERVABLES):
        before = FLAT_STATE_OBSERVABLES[flat]
        after = FOLDED_STATE_OBSERVABLES[flat]
        folded_state, _folded_reason, folded_conversion_state = (
            ca.FLAT_STATE_MIGRATION[flat]
        )
        assert after[0] == before[0], flat
        assert after[0] == folded_conversion_state, flat
        assert before[1] == flat, flat
        assert after[1] == folded_state, flat
        assert after[3] == before[3], flat
        if flat not in _COMMAND_DERIVED_OUTCOME_ROWS:
            assert after[2] == before[2], flat
        if after[1] != before[1]:
            moved.add(flat)
    # 折叠确实发生了：18 个扁平值里有 12 个的 attempt state 变了名字。少了
    # 说明折叠没落地，多了说明迁移表被改动过。
    assert len(moved) == 12
    assert len({FOLDED_STATE_OBSERVABLES[f][1] for f in FLAT_STATE_OBSERVABLES}) == 7


# --- Task 2.1d: unconditional round accounting behind a disabled ceiling ---
#
# RESULT_REFRESH_ROUND_CEILING defaults to None: the round count is tallied
# on every attempt regardless of whether a ceiling is configured, but the
# (currently unwired) exhaustion check stays fully short-circuited, so the
# default introduces no new decision branch. Which finite ceiling to use, and
# which state transition an exhausted count feeds into, are out of scope here
# (Decision 10, deferred to a later change).

_drive_rotating_result_urls_call_counter = itertools.count()


def read_manifest(bundle):
    return json.loads((bundle / "manifest.json").read_text())


def drive_through_rotating_result_urls(tmp_path, rounds):
    """Drive a fresh work bundle through `rounds` distinct result_ready
    observations of the same Doc2X task.

    A repeat result_ready poll for an attempt already sitting in
    result_ready is only reachable one way in this codebase: raw_conversion
    detects the previously recorded result reference has locally expired
    (result_reference_is_expired) and downgrades conversion_state to
    recoverable_error; the *next* resume then repolls doc2x, which is free
    to answer with the same URL as before or a new one. This mirrors
    _refresh_ready_bundle and the "manifest"/"new" branch of
    test_expired_refresh_crash_boundaries_recover_without_new_task_or_get
    above, generalized from two observations to `rounds`, and swapped onto
    _CapturedIO / pytest.MonkeyPatch.context() (see drive_to_flat_state's
    docstring) so it can run outside the capsys/monkeypatch fixtures --
    the exact reusable (tmp_path, ...) shape drive_to_flat_state established
    for the same reason.

    Returns the work bundle path; callers read the manifest/private state
    back from disk (see read_manifest above).
    """
    if rounds < 1:
        raise AssertionError("drive_through_rotating_result_urls needs rounds >= 1")
    call_root = tmp_path / (
        f"drive-rotating-{next(_drive_rotating_result_urls_call_counter)}"
    )
    call_root.mkdir()
    with _CapturedIO() as capture:
        with pytest.MonkeyPatch.context() as monkeypatch:
            bundle, staged, dependencies, key, _source_url, _source_sha256 = (
                ready_staged_bundle(call_root, capture, monkeypatch)
            )
            environ = {**dependencies, "AIHUB_API_KEY": key}

            def resume(expected_generation, *, transport, now=NOW):
                rc, result, _stderr = invoke(
                    capture,
                    [
                        "resume",
                        "--work-bundle",
                        str(bundle),
                        "--expected-generation",
                        str(expected_generation),
                    ],
                    cwd=call_root,
                    environ=environ,
                    transport=transport,
                    now=now,
                )
                assert rc == 0
                return result

            task_id = "task-rotating-result"
            submitted = resume(
                staged["generation"], transport=SuccessfulCreate(task_id)
            )
            ready = resume(
                submitted["generation"],
                transport=PollStatus(
                    task_id,
                    "completed",
                    results=[
                        {
                            "url": (
                                "https://results.example/rotating.zip"
                                "?token=round-1"
                            )
                        }
                    ],
                ),
            )
            assert ready["outcome"] == "result_ready"
            generation = ready["generation"]
            for round_index in range(2, rounds + 1):
                # result_ready's validity window is a fixed 24h
                # (_valid_attempt pins result_validity_hours == 24); each
                # cycle's "now" has to clear the previous round's window so
                # the local expiry check actually fires.
                cycle_at = NOW + timedelta(hours=24 * (round_index - 1))
                expired = resume(
                    generation, transport=CountingNeverNetwork(), now=cycle_at
                )
                assert expired["outcome"] == "result_url_unavailable", round_index
                renewed = resume(
                    expired["generation"],
                    transport=PollStatus(
                        task_id,
                        "completed",
                        results=[
                            {
                                "url": (
                                    "https://results.example/rotating.zip"
                                    f"?token=round-{round_index}"
                                )
                            }
                        ],
                    ),
                    now=cycle_at,
                )
                assert renewed["outcome"] == "result_ready", round_index
                generation = renewed["generation"]
    return bundle


def test_result_refresh_rounds_are_counted_unconditionally(tmp_path):
    """裁定 2：计数无条件累加，与上界是否启用无关，为后续定阈值提供真实数据。"""
    import conversion_attempt as ca

    assert ca.RESULT_REFRESH_ROUND_CEILING is None
    bundle = drive_through_rotating_result_urls(tmp_path, rounds=3)
    attempt = read_manifest(bundle)["conversion_attempts"][-1]
    assert attempt["result_refresh_round_count"] == 3


def test_result_refresh_ceiling_is_off_by_default_and_injectable(tmp_path, monkeypatch):
    """裁定 2：上界默认哨兵 None 时判定分支完全短路，可观察行为与今天一致；
    注入小上界后超限分支可达。本子步骤不接线任何超限收敛分支
    （result_refresh_rounds_exhausted 此刻只有这里调用它，这是有意的）。
    """
    import conversion_attempt as ca

    bundle = drive_through_rotating_result_urls(tmp_path, rounds=3)
    assert read_manifest(bundle)["conversion_state"] == "result_downloading"

    monkeypatch.setattr(ca, "RESULT_REFRESH_ROUND_CEILING", 2)
    assert ca.result_refresh_rounds_exhausted({"result_refresh_round_count": 2})
    assert not ca.result_refresh_rounds_exhausted({"result_refresh_round_count": 1})
    monkeypatch.setattr(ca, "RESULT_REFRESH_ROUND_CEILING", None)
    assert not ca.result_refresh_rounds_exhausted({"result_refresh_round_count": 99})


def test_result_refresh_round_count_is_unchanged_when_the_same_result_url_is_redelivered(
    tmp_path, capsys, monkeypatch
):
    """`_attempt_reason_columns` recomputes reason/reason_detail/
    authorization_kind fresh from the wire classification on every poll
    observation. If result_refresh_round_count rode along in that same
    producer, it would reset to 0 on this branch too:
    `_recorded_result_url` finds the redelivered
    URL already on file, so the new-URL increment branch in _poll_transition
    never runs to restore it. Outside that one branch, the count must be
    exactly what `updated_attempt = deepcopy(active)` already carried in --
    this is the carry-forward half of "其余路径保持原值" the brief requires,
    which neither of the two tests above can exercise on their own: both
    only ever observe distinct URLs, so their new-URL branch runs on every
    round regardless of whether the reset bug is fixed.
    """
    import conversion_attempt as ca

    bundle, environ, resume, ready = _refresh_ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    assert (
        read_manifest(bundle)["conversion_attempts"][-1][
            "result_refresh_round_count"
        ]
        == 1
    )

    expired = resume(
        ready["generation"], transport=CountingNeverNetwork(), now=REFRESH_EXPIRY
    )
    assert expired["outcome"] == "result_url_unavailable"
    renewed = resume(
        expired["generation"],
        transport=PollStatus(
            REFRESH_TASK_ID, "completed", results=[{"url": REFRESH_FIRST_URL}]
        ),
        now=REFRESH_EXPIRY,
    )
    assert renewed["outcome"] == "result_ready"
    attempt = read_manifest(bundle)["conversion_attempts"][-1]
    assert attempt["result_refresh_round_count"] == 1
