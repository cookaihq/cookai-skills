"""Command-level contracts for Doc2X conversion attempts."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import fitz
import pytest
import conversion_attempt
import workflow


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
        "not_started",
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
    assert manifest["conversion_attempts"][0]["reason_code"] == (
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
    assert pending["conversion_attempt_state"] == "result_pending"
    manifest = json.loads((bundle / "manifest.json").read_text())
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
    assert stopped["conversion_attempt_state"] == expected_state
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
        assert resumed["conversion_attempt_state"] == "poll_transient"


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


@pytest.mark.parametrize(
    ("poll_environ", "expected_reason"),
    [
        ({}, "credential_source_missing"),
        ({"AIHUB_API_KEY": "different-key"}, "credential_source_changed"),
    ],
)
def test_resume_persists_missing_and_drifted_creation_credentials_without_polling(
    tmp_path, capsys, monkeypatch, poll_environ, expected_reason
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
    assert blocked["conversion_attempt_state"] == expected_reason
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason_code"] == expected_reason
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


@pytest.mark.parametrize(
    ("http_status", "expected_reason"),
    [(401, "poll_unauthorized"), (404, "task_unavailable")],
)
def test_poll_401_and_404_have_distinct_recoverable_reasons_on_the_same_task(
    tmp_path, capsys, monkeypatch, http_status, expected_reason
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
    assert recoverable["conversion_attempt_state"] == expected_reason
    assert rejected.calls == 1
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason_code"] == expected_reason
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
    assert recoverable["conversion_attempt_state"] == "poll_transient"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason_code"] == "poll_transient"
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
    assert timed_out["conversion_attempt_state"] == "poll_timeout"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason_code"] == "poll_timeout"
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
    assert second["conversion_attempt_state"] == "poll_transient"
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
    assert timed_out["conversion_attempt_state"] == "result_pending_timeout"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["conversion_attempts"][-1]["reason_code"] == (
        "result_pending_timeout"
    )
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
    expected_state = "poll_transient" if boundary == "private" else "result_ready"
    assert recovered["conversion_attempt_state"] == expected_state
    manifest_text = (bundle / "manifest.json").read_text()
    history_text = (bundle / ".state" / "history.ndjson").read_text()
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert result_url not in manifest_text
    assert result_url not in history_text
    if boundary == "private":
        assert private_state["result_urls"] == []
    else:
        assert private_state["result_urls"][-1]["url"] == result_url


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
        "not_started",
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
