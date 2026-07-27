"""Command-level contracts for the built-in AIHub source staging upload."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
import pytest
import aihub_upload
import conversion_attempt
import doc2x
import source_staging
import workflow


NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
AIHUB_UPLOAD_URL = "https://api.aihubmax.com/v1/files/upload/stream"


class Response:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body


class NeverNetwork:
    def __call__(self, *_args, **_kwargs):
        raise AssertionError("network access is not expected before source staging")


class SuccessfulUpload:
    def __init__(self, url):
        self.url = url
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        return Response(200, json.dumps({"url": self.url}).encode("utf-8"))


class StatusUpload:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def __call__(self, method, url, headers, body):
        body_bytes = body if isinstance(body, bytes) else b"".join(body)
        self.calls.append((method, url, dict(headers), body_bytes))
        return Response(self.status, b'{"error":{"type":"authentication_error"}}')


class ScriptedUpload:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.calls = 0

    def __call__(self, _method, _url, _headers, body):
        self.calls += 1
        b"".join(body)
        return Response(self.status, self.body)


class LostUpload:
    def __init__(self):
        self.calls = 0

    def __call__(self, _method, _url, _headers, body):
        self.calls += 1
        b"".join(body)
        raise OSError("the response was lost")


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterPost:
    def __init__(self):
        self.calls = 0

    def __call__(self, _method, _url, _headers, body):
        self.calls += 1
        b"".join(body)
        raise SimulatedProcessCrash


class SequenceClock:
    def __init__(self, *moments):
        self.moments = list(moments)

    def __call__(self):
        assert self.moments
        return self.moments.pop(0)


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


def ready_bundle(tmp_path, capsys, monkeypatch, *, interaction_mode="confirm"):
    source = tmp_path / "input.pdf"
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 18), "Source staging")
    document.save(source)
    document.close()
    source_bytes = source.read_bytes()
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
    record = tmp_path / "preflight-record.json"
    record.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "pass",
                "pages": [
                    {
                        "page_number": 1,
                        "classification": "content",
                        "risk_codes": [],
                        "evidence": ["The source staging text is readable."],
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
            str(record),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert record_rc == 0, recorded
    assert recorded["conversion_state"] == "ready_to_submit"
    return bundle, recorded, dependencies, source_bytes


def test_advance_streams_the_frozen_source_once_and_keeps_the_ready_url_private(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    key = "test-aihub-key-123456"
    staged_url = "https://files.aihubmax.com/source.pdf?token=private-bearer"
    transport = SuccessfulUpload(staged_url)
    uploader_marker = tmp_path / "external-uploader-called"
    for command in ("upload-for-url", "s3-upload", "other-uploader"):
        executable = Path(dependencies["PATH"]) / command
        executable.write_text(
            "#!/usr/bin/python3\n"
            "from pathlib import Path\n"
            f"Path({str(uploader_marker)!r}).write_text('called')\n"
        )
        executable.chmod(0o700)

    rc, result, stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": key},
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    history = (bundle / ".state" / "history.ndjson").read_text()
    assert rc == 0
    assert result["outcome"] == "source_upload_ready"
    assert result["conversion_state"] == "ready_to_submit"
    assert result["source_upload_state"] == "source_upload_ready"
    assert result["generation"] == ready["generation"] + 2
    assert result["action_required"] is None
    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == AIHUB_UPLOAD_URL
    assert headers["Authorization"] == f"Bearer {key}"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert headers["Content-Length"] == str(len(body))
    assert b'name="auto_cleanup"\r\n\r\nfalse' in body
    assert b'name="file"; filename="source.pdf"' in body
    assert body.count(source_bytes) == 1
    assert manifest["source_staging"]["state"] == "source_upload_ready"
    assert manifest["source_staging"]["attempts"][0]["credential"] == {
        "source_id": "process_environment:AIHUB_API_KEY",
        "fingerprint": f"sha256:{hashlib.sha256(key.encode()).hexdigest()}",
        "locator": {"kind": "process_environment", "name": "AIHUB_API_KEY"},
    }
    assert private_state["source_uploads"][0]["url"] == staged_url
    assert private_state["source_uploads"][0]["expires_at"] == (
        "2024-01-05T03:04:05Z"
    )
    serialized_public = json.dumps(result) + json.dumps(manifest) + history + stderr
    assert staged_url not in serialized_public
    assert key not in serialized_public
    assert not uploader_marker.exists()


def test_ready_expiry_is_measured_from_the_locally_observed_completion_time(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    operation = datetime(2024, 1, 2, 3, 0, tzinfo=timezone.utc)
    request = datetime(2024, 1, 2, 3, 1, tzinfo=timezone.utc)
    completed = datetime(2024, 1, 2, 3, 11, tzinfo=timezone.utc)
    clock = SequenceClock(operation, request, completed)

    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "completion-clock-key"},
        transport=SuccessfulUpload("https://files.aihubmax.com/completed.pdf"),
        now=clock,
    )

    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    attempt = private_state["source_uploads"][0]
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_ready"
    assert attempt["completed_at"] == "2024-01-02T03:11:00Z"
    assert attempt["expires_at"] == "2024-01-05T03:11:00Z"


def test_resume_stages_a_preflight_ready_bundle_without_requiring_visual_rework(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = SuccessfulUpload("https://files.aihubmax.com/resume.pdf")

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
        environ={**dependencies, "AIHUB_API_KEY": "resume-key"},
        transport=transport,
    )

    assert rc == 0
    assert result["outcome"] == "source_upload_ready"
    assert result["source_upload_state"] == "source_upload_ready"
    assert len(transport.calls) == 1


def test_process_key_wins_without_reading_or_retaining_invalid_lower_priority_keys(
    tmp_path, capsys, monkeypatch
):
    (tmp_path / ".env.local").write_text('AIHUB_API_KEY="unterminated\n')
    (tmp_path / ".env").write_text("AIHUB_API_KEY=lower-project-key\n")
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / ".env").write_text("AIHUB_API_KEY=lower-home-key\n")
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    selected = "selected-process-key"
    transport = StatusUpload(401)

    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
            "--use-local-key",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": selected},
        transport=transport,
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    serialized = json.dumps(manifest) + json.dumps(result)
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_unknown"
    assert len(transport.calls) == 1
    assert transport.calls[0][2]["Authorization"] == f"Bearer {selected}"
    assert manifest["source_staging"]["attempts"][0]["credential"] == {
        "source_id": "process_environment:AIHUB_API_KEY",
        "fingerprint": f"sha256:{hashlib.sha256(selected.encode()).hexdigest()}",
        "locator": {"kind": "process_environment", "name": "AIHUB_API_KEY"},
    }
    assert "lower-project-key" not in serialized
    assert "lower-home-key" not in serialized


def test_empty_process_value_uses_last_literal_dotenv_local_value(
    tmp_path, capsys, monkeypatch
):
    literal = "${HOME}$(printf-not-executed)`also-literal`"
    dotenv_local = tmp_path / ".env.local"
    dotenv_local.write_text(
        "AIHUB_API_KEY=overridden\n"
        "IGNORED_KEY=ignored\n"
        f"AIHUB_API_KEY='{literal}'\n"
    )
    (tmp_path / ".env").write_text("AIHUB_API_KEY=lower-key\n")
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = StatusUpload(401)

    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "   "},
        transport=transport,
    )

    credential = json.loads((bundle / "manifest.json").read_text())[
        "source_staging"
    ]["attempts"][0]["credential"]
    expected_path = str(dotenv_local.absolute())
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_unknown"
    assert transport.calls[0][2]["Authorization"] == f"Bearer {literal}"
    assert credential == {
        "source_id": f"dotenv:{expected_path}:AIHUB_API_KEY",
        "fingerprint": f"sha256:{hashlib.sha256(literal.encode()).hexdigest()}",
        "locator": {
            "kind": "dotenv",
            "path": expected_path,
            "name": "AIHUB_API_KEY",
        },
    }


def test_empty_dotenv_local_falls_through_to_dotenv(
    tmp_path, capsys, monkeypatch
):
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=''\n")
    dotenv = tmp_path / ".env"
    dotenv.write_text("AIHUB_API_KEY=project-env-key\n")
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = StatusUpload(401)

    rc, _result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    credential = json.loads((bundle / "manifest.json").read_text())[
        "source_staging"
    ]["attempts"][0]["credential"]
    assert rc == 0
    assert transport.calls[0][2]["Authorization"] == "Bearer project-env-key"
    assert credential["source_id"] == (
        f"dotenv:{dotenv.absolute()}:AIHUB_API_KEY"
    )


def test_home_key_is_unreadable_without_current_use_local_key_authorization(
    tmp_path, capsys, monkeypatch
):
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    home_dotenv = config_home / ".env"
    home_dotenv.write_text("AIHUB_API_KEY=authorized-home-key\n")
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    no_network = NeverNetwork()

    missing_rc, missing, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=no_network,
    )
    assert missing_rc == 6
    assert missing["errors"][0]["code"] == "configuration_invalid"
    assert json.loads((bundle / ".state" / "private.json").read_text())[
        "source_uploads"
    ] == []

    transport = StatusUpload(401)
    authorized_rc, _authorized, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
            "--use-local-key",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=transport,
    )

    credential = json.loads((bundle / "manifest.json").read_text())[
        "source_staging"
    ]["attempts"][0]["credential"]
    assert authorized_rc == 0
    assert transport.calls[0][2]["Authorization"] == "Bearer authorized-home-key"
    assert credential["locator"] == {
        "kind": "dotenv",
        "path": str(home_dotenv.absolute()),
        "name": "AIHUB_API_KEY",
    }


def test_project_dotenv_symlink_cannot_bypass_home_key_authorization(
    tmp_path, capsys, monkeypatch
):
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    home_dotenv = config_home / ".env"
    home_key = "home-key-must-remain-unread"
    home_dotenv.write_text(f"AIHUB_API_KEY={home_key}\n")
    (tmp_path / ".env.local").symlink_to(home_dotenv)
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )

    rc, result, stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    serialized = json.dumps(result) + json.dumps(manifest) + stderr
    assert rc == 6
    assert result["errors"][0]["code"] == "configuration_invalid"
    assert "source_staging" not in manifest
    assert private_state["source_uploads"] == []
    assert home_key not in serialized


def test_auto_mode_keeps_an_unknown_upload_stopped_without_replaying_the_post(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    transport = LostUpload()
    arguments = [
        "advance",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(ready["generation"]),
        "--visual-capability",
        "available",
    ]

    first_rc, unknown, _stderr = invoke(
        capsys,
        arguments,
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "auto-unknown-key"},
        transport=transport,
    )
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            *arguments[:4],
            str(unknown["generation"]),
            *arguments[5:],
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "different-key-must-not-run"},
        transport=transport,
    )

    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert first_rc == 0
    assert unknown["conversion_state"] == "awaiting_user"
    assert unknown["source_upload_state"] == "source_upload_unknown"
    assert unknown["action_required"] is None
    assert resumed_rc == 0
    assert resumed["outcome"] == "source_upload_unknown"
    assert resumed["generation"] == unknown["generation"]
    assert resumed["action_required"] is None
    assert transport.calls == 1
    assert len(private_state["source_uploads"]) == 1


def test_confirm_mode_requires_a_bound_decision_before_a_new_staging_attempt(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    lost = LostUpload()
    unknown_rc, unknown, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "first-attempt-key"},
        transport=lost,
    )

    assert unknown_rc == 0
    assert unknown["conversion_state"] == "awaiting_user"
    assert unknown["source_upload_state"] == "source_upload_unknown"
    assert unknown["action_required"] == "resolve_source_upload_unknown"
    assert unknown["action_id"].startswith("source-upload-decision-")

    decision_rc, authorized, decision_stderr = invoke(
        capsys,
        [
            "record",
            "source-staging",
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
            "Accept the possibility that the first upload left an unreachable remote file.",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-read-yet"},
        transport=NeverNetwork(),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    private_before_retry = json.loads(
        (bundle / ".state" / "private.json").read_text()
    )
    assert decision_rc == 0, (authorized, decision_stderr)
    assert authorized["outcome"] == "source_upload_retry_authorized"
    assert authorized["conversion_state"] == "ready_to_submit"
    assert authorized["source_upload_state"] == "source_upload_not_started"
    assert authorized["action_required"] is None
    assert len(manifest["source_staging"]["attempts"]) == 2
    assert manifest["source_staging"]["attempts"][0]["state"] == (
        "source_upload_unknown"
    )
    assert manifest["source_staging"]["attempts"][1]["state"] == (
        "source_upload_not_started"
    )
    assert len(private_before_retry["source_uploads"]) == 2
    assert lost.calls == 1

    successful = SuccessfulUpload("https://files.aihubmax.com/retry.pdf?token=second")
    retry_rc, retried, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(authorized["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "second-attempt-key"},
        transport=successful,
    )

    final_private = json.loads((bundle / ".state" / "private.json").read_text())
    assert retry_rc == 0
    assert retried["outcome"] == "source_upload_ready"
    assert len(successful.calls) == 1
    assert len(final_private["source_uploads"]) == 2
    assert final_private["source_uploads"][0]["state"] == "source_upload_unknown"
    assert final_private["source_uploads"][1]["state"] == "source_upload_ready"


@pytest.mark.parametrize(
    ("status", "expected_state", "expected_conversion", "expected_action"),
    [
        (403, "source_upload_rejected", "recoverable_error", "retry_source_upload"),
        (302, "source_upload_unknown", "awaiting_user", "resolve_source_upload_unknown"),
        (401, "source_upload_unknown", "awaiting_user", "resolve_source_upload_unknown"),
        (413, "source_upload_unknown", "awaiting_user", "resolve_source_upload_unknown"),
        (429, "source_upload_unknown", "awaiting_user", "resolve_source_upload_unknown"),
        (500, "source_upload_unknown", "awaiting_user", "resolve_source_upload_unknown"),
    ],
)
def test_only_the_documented_capacity_403_is_rejected_without_a_write(
    tmp_path,
    capsys,
    monkeypatch,
    status,
    expected_state,
    expected_conversion,
    expected_action,
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = StatusUpload(status)

    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "classification-key"},
        transport=transport,
    )

    assert rc == 0
    assert result["source_upload_state"] == expected_state
    assert result["conversion_state"] == expected_conversion
    assert result["action_required"] == expected_action
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == AIHUB_UPLOAD_URL


@pytest.mark.parametrize(
    "response_body",
    [
        b"{}",
        b'{"url":""}',
        b'{"url":"http://files.example/source.pdf"}',
        b'{"url":"https://first.example/a","url":"https://second.example/b"}',
        b'{"url":"https://user:secret@files.example/source.pdf"}',
        b"\xff\xfe",
        b"[]",
    ],
)
def test_an_abnormal_200_response_is_unknown_and_never_replayed(
    tmp_path, capsys, monkeypatch, response_body
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    transport = ScriptedUpload(200, response_body)

    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "abnormal-response-key"},
        transport=transport,
    )

    private_attempt = json.loads(
        (bundle / ".state" / "private.json").read_text()
    )["source_uploads"][0]
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_unknown"
    assert result["conversion_state"] == "awaiting_user"
    assert private_attempt["url"] is None
    assert private_attempt["expires_at"] is None
    assert transport.calls == 1


def test_a_started_attempt_after_process_death_becomes_unknown_without_a_second_post(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    crash = CrashAfterPost()
    arguments = [
        "advance",
        "--work-bundle",
        str(bundle),
        "--expected-generation",
        str(ready["generation"]),
        "--visual-capability",
        "available",
    ]
    with pytest.raises(SimulatedProcessCrash):
        workflow.main(
            arguments,
            environ={**dependencies, "AIHUB_API_KEY": "crash-key"},
            cwd=str(tmp_path),
            config_home=str(tmp_path / "config-home"),
            transport=crash,
            now=NOW,
        )
    capsys.readouterr()
    manifest_after_crash = json.loads((bundle / "manifest.json").read_text())
    assert manifest_after_crash["source_staging"]["state"] == (
        "source_upload_started"
    )

    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            *arguments[:4],
            str(manifest_after_crash["generation"]),
            *arguments[5:],
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-used"},
        transport=NeverNetwork(),
    )

    assert resumed_rc == 0
    assert resumed["source_upload_state"] == "source_upload_unknown"
    assert resumed["conversion_state"] == "awaiting_user"
    assert crash.calls == 1


@pytest.mark.parametrize("crash_point", ["private", "manifest", "committed"])
def test_a_start_journal_crash_is_recovered_as_unknown_without_sending_the_post(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    original_atomic = source_staging.bundle.atomic_write_json
    original_append = source_staging.bundle.append_history
    crashed = False

    def crash_first_staging_state_write(name, value, *, dir_fd):
        nonlocal crashed
        if (
            crash_point in {"private", "manifest"}
            and not crashed
            and name == f"{crash_point}.json"
            and (
                value.get("source_staging", {}).get("state")
                == "source_upload_started"
                or (
                    value.get("source_uploads")
                    and value["source_uploads"][-1].get("state")
                    == "source_upload_started"
                )
            )
        ):
            crashed = True
            raise OSError("simulated crash after source upload intent")
        return original_atomic(name, value, dir_fd=dir_fd)

    def crash_started_commit(event, *, state_fd):
        nonlocal crashed
        if (
            crash_point == "committed"
            and not crashed
            and event.get("event") == "source_upload_started"
        ):
            crashed = True
            raise OSError("simulated crash before source upload start commit")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(
        source_staging.bundle, "atomic_write_json", crash_first_staging_state_write
    )
    monkeypatch.setattr(source_staging.bundle, "append_history", crash_started_commit)
    crash_rc, failed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "intent-key"},
        transport=NeverNetwork(),
    )
    assert crash_rc == 1
    assert failed["errors"][0]["code"] == "internal_error"
    history_after_crash = [
        json.loads(line)
        for line in (bundle / ".state" / "history.ndjson").read_text().splitlines()
    ]
    assert history_after_crash[-1]["event"] == "source_upload_intent"
    before_stale = {
        path: path.read_bytes()
        for path in (
            bundle / "manifest.json",
            bundle / ".state" / "private.json",
            bundle / ".state" / "history.ndjson",
        )
    }
    stale_rc, stale, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            "999999",
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-used"},
        transport=NeverNetwork(),
    )
    assert stale_rc == 5
    assert stale["errors"][0]["code"] == "generation_conflict"
    assert all(path.read_bytes() == value for path, value in before_stale.items())

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", original_atomic)
    monkeypatch.setattr(source_staging.bundle, "append_history", original_append)
    resumed_rc, resumed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-used"},
        transport=NeverNetwork(),
    )

    assert resumed_rc == 0
    assert resumed["source_upload_state"] == "source_upload_unknown"
    assert resumed["conversion_state"] == "awaiting_user"
    assert len(
        json.loads((bundle / ".state" / "private.json").read_text())[
            "source_uploads"
        ]
    ) == 1


@pytest.mark.parametrize("crash_point", ["private", "manifest", "committed"])
def test_a_received_ready_result_is_recovered_after_each_local_commit_crash(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    original_atomic = source_staging.bundle.atomic_write_json
    original_append = source_staging.bundle.append_history

    def crash_manifest(name, value, *, dir_fd):
        if (
            crash_point in {"private", "manifest"}
            and name == f"{crash_point}.json"
            and value.get("source_staging", {}).get("state")
            == "source_upload_ready"
        ):
            raise OSError("simulated result manifest crash")
        if (
            crash_point == "private"
            and name == "private.json"
            and value.get("source_uploads")
            and value["source_uploads"][-1].get("state") == "source_upload_ready"
        ):
            raise OSError("simulated result private crash")
        return original_atomic(name, value, dir_fd=dir_fd)

    def crash_committed(event, *, state_fd):
        if (
            crash_point == "committed"
            and event.get("event") == "source_upload_result_committed"
        ):
            raise OSError("simulated result committed crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", crash_manifest)
    monkeypatch.setattr(source_staging.bundle, "append_history", crash_committed)
    transport = SuccessfulUpload(
        f"https://files.aihubmax.com/{crash_point}.pdf?token=private"
    )
    crash_rc, crashed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "result-crash-key"},
        transport=transport,
    )
    assert crash_rc == 1
    assert crashed["errors"][0]["code"] == "internal_error"
    assert len(transport.calls) == 1
    manifest_after_crash = json.loads((bundle / "manifest.json").read_text())

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", original_atomic)
    monkeypatch.setattr(source_staging.bundle, "append_history", original_append)
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(manifest_after_crash["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-used"},
        transport=NeverNetwork(),
    )

    assert recovered_rc == 0, recovered
    expected_state = (
        "source_upload_unknown" if crash_point == "private" else "source_upload_ready"
    )
    assert recovered["outcome"] == expected_state
    assert recovered["source_upload_state"] == expected_state
    assert len(transport.calls) == 1


def test_pending_start_intent_rejects_secret_or_url_fields_before_any_state_write(
    tmp_path, capsys, monkeypatch
):
    bundle_path, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    manifest_path = bundle_path / "manifest.json"
    private_path = bundle_path / ".state" / "private.json"
    history_path = bundle_path / ".state" / "history.ndjson"
    manifest = json.loads(manifest_path.read_text())
    private = json.loads(private_path.read_text())
    before_manifest = manifest_path.read_bytes()
    before_private = private_path.read_bytes()
    attempt = {
        "attempt_id": "source-upload-0001",
        "state": "source_upload_started",
        "source_sha256": manifest["source"]["sha256"],
        "credential": {
            "source_id": "process_environment:AIHUB_API_KEY",
            "fingerprint": "sha256:" + "1" * 64,
            "locator": {"kind": "process_environment", "name": "AIHUB_API_KEY"},
            "value": "FULL-SECRET-MUST-NOT-BE-WRITTEN",
        },
        "started_at": "2024-01-02T03:04:05Z",
        "completed_at": None,
        "http_status": None,
        "reason_code": None,
        "url_sha256": None,
        "url": "https://files.example.test/source.pdf?token=secret",
    }
    intent = {
        "schema_version": 1,
        "event": "source_upload_intent",
        "operation_id": "source-upload-0001-start",
        "expected_generation": manifest["generation"],
        "new_generation": manifest["generation"] + 1,
        "at": "2024-01-02T03:04:05Z",
        "source_sha256": manifest["source"]["sha256"],
        "attempt": attempt,
        "previous_manifest_hash": source_staging.object_hash(manifest),
        "previous_private_hash": source_staging.object_hash(private),
    }
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n")

    rc, result, stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "unused"},
        transport=NeverNetwork(),
    )

    assert rc == 4
    assert result["errors"][0]["code"] == "integrity_violation"
    assert manifest_path.read_bytes() == before_manifest
    assert private_path.read_bytes() == before_private
    serialized = json.dumps(result) + stderr + manifest_path.read_text()
    assert "FULL-SECRET-MUST-NOT-BE-WRITTEN" not in serialized
    assert "token=secret" not in serialized


def test_staging_settings_override_rebinds_or_removes_the_unknown_action(
    tmp_path, capsys, monkeypatch
):
    bundle_path, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    lost = LostUpload()
    unknown_rc, unknown, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "override-key"},
        transport=lost,
    )
    assert unknown_rc == 0
    assert unknown["action_required"] is None

    confirm_rc, confirmed, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(unknown["generation"]),
            "--interaction-mode",
            "confirm",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert confirm_rc == 0
    assert confirmed["outcome"] == "settings_overridden"
    confirm_manifest = json.loads((bundle_path / "manifest.json").read_text())
    pending = confirm_manifest["source_staging"]["pending_action"]
    assert pending["kind"] == "resolve_source_upload_unknown"
    assert pending["generation"] == confirmed["generation"]

    auto_rc, automatic, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(confirmed["generation"]),
            "--interaction-mode",
            "auto",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert auto_rc == 0
    assert automatic["outcome"] == "settings_overridden"
    auto_manifest = json.loads((bundle_path / "manifest.json").read_text())
    assert auto_manifest["source_staging"]["pending_action"] is None
    assert lost.calls == 1


@pytest.mark.parametrize("crash_point", ["private", "manifest", "committed"])
def test_expiry_journal_recovers_at_each_commit_boundary(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle_path, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    upload = SuccessfulUpload("https://files.aihubmax.com/expiry.pdf?token=private")
    staged_rc, staged, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "expiry-key"},
        transport=upload,
    )
    assert staged_rc == 0
    original_atomic = source_staging.bundle.atomic_write_json
    original_append = source_staging.bundle.append_history
    crashed = False

    def crash_expiry_state(name, value, *, dir_fd):
        nonlocal crashed
        is_expired = (
            value.get("source_staging", {}).get("state") == "source_upload_expired"
            or (
                value.get("source_uploads")
                and value["source_uploads"][-1].get("state")
                == "source_upload_expired"
            )
        )
        if (
            crash_point in {"private", "manifest"}
            and not crashed
            and name == f"{crash_point}.json"
            and is_expired
        ):
            crashed = True
            raise OSError("simulated expiry state crash")
        return original_atomic(name, value, dir_fd=dir_fd)

    def crash_expiry_commit(event, *, state_fd):
        nonlocal crashed
        if (
            crash_point == "committed"
            and not crashed
            and event.get("event") == "source_upload_expiry_committed"
        ):
            crashed = True
            raise OSError("simulated expiry commit crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", crash_expiry_state)
    monkeypatch.setattr(source_staging.bundle, "append_history", crash_expiry_commit)
    expired_at = NOW + timedelta(hours=72)
    crash_rc, failed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(staged["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=expired_at,
    )
    assert crash_rc == 1
    assert failed["errors"][0]["code"] == "internal_error"

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", original_atomic)
    monkeypatch.setattr(source_staging.bundle, "append_history", original_append)
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        [
            "resume",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(staged["generation"]),
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=expired_at,
    )
    assert recovered_rc == 0, recovered
    assert recovered["source_upload_state"] == "source_upload_expired"
    assert upload.calls and len(upload.calls) == 1


@pytest.mark.parametrize("crash_point", ["private", "manifest", "committed"])
def test_decision_journal_recovers_at_each_commit_boundary(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle_path, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    lost = LostUpload()
    _rc, unknown, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "decision-key"},
        transport=lost,
    )
    arguments = [
        "record",
        "source-staging",
        "--work-bundle",
        str(bundle_path),
        "--expected-generation",
        str(unknown["generation"]),
        "--action-id",
        unknown["action_id"],
        "--evidence-hash",
        unknown["evidence_hash"],
        "--decision",
        "retry",
        "--basis",
        "The previous upload may have left a remote file.",
    ]
    original_atomic = source_staging.bundle.atomic_write_json
    original_append = source_staging.bundle.append_history
    crashed = False

    def crash_decision_state(name, value, *, dir_fd):
        nonlocal crashed
        is_retry = (
            value.get("source_staging", {}).get("state")
            == "source_upload_not_started"
            or (
                value.get("source_uploads")
                and value["source_uploads"][-1].get("state")
                == "source_upload_not_started"
            )
        )
        if (
            crash_point in {"private", "manifest"}
            and not crashed
            and name == f"{crash_point}.json"
            and is_retry
        ):
            crashed = True
            raise OSError("simulated decision state crash")
        return original_atomic(name, value, dir_fd=dir_fd)

    def crash_decision_commit(event, *, state_fd):
        nonlocal crashed
        if (
            crash_point == "committed"
            and not crashed
            and event.get("event") == "source_upload_decision_committed"
        ):
            crashed = True
            raise OSError("simulated decision commit crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", crash_decision_state)
    monkeypatch.setattr(source_staging.bundle, "append_history", crash_decision_commit)
    crash_rc, failed, _stderr = invoke(
        capsys,
        arguments,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert crash_rc == 1
    assert failed["errors"][0]["code"] == "internal_error"

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", original_atomic)
    monkeypatch.setattr(source_staging.bundle, "append_history", original_append)
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        arguments,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert recovered_rc == 0, recovered
    assert recovered["source_upload_state"] == "source_upload_not_started"
    assert lost.calls == 1


@pytest.mark.parametrize("crash_point", ["private", "manifest", "committed"])
def test_wait_elapsed_journal_recovers_at_each_commit_boundary(
    tmp_path, capsys, monkeypatch, crash_point
):
    bundle_path, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    lost = LostUpload()
    _rc, unknown, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "wait-key"},
        transport=lost,
    )
    _wait_rc, waiting, _stderr = invoke(
        capsys,
        [
            "record",
            "source-staging",
            "--work-bundle",
            str(bundle_path),
            "--expected-generation",
            str(unknown["generation"]),
            "--action-id",
            unknown["action_id"],
            "--evidence-hash",
            unknown["evidence_hash"],
            "--decision",
            "wait",
            "--basis",
            "Wait through the conservative remote retention window.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    original_atomic = source_staging.bundle.atomic_write_json
    original_append = source_staging.bundle.append_history
    crashed = False

    def crash_wait_state(name, value, *, dir_fd):
        nonlocal crashed
        staging = value.get("source_staging", {})
        is_renewed = (
            staging.get("state") == "source_upload_unknown"
            and staging.get("pending_action") is not None
            and "wait_until" not in staging
        ) or (
            name == "private.json"
            and value.get("generation") == waiting["generation"] + 1
        )
        if (
            crash_point in {"private", "manifest"}
            and not crashed
            and name == f"{crash_point}.json"
            and is_renewed
        ):
            crashed = True
            raise OSError("simulated wait state crash")
        return original_atomic(name, value, dir_fd=dir_fd)

    def crash_wait_commit(event, *, state_fd):
        nonlocal crashed
        if (
            crash_point == "committed"
            and not crashed
            and event.get("event") == "source_upload_wait_elapsed_committed"
        ):
            crashed = True
            raise OSError("simulated wait commit crash")
        return original_append(event, state_fd=state_fd)

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", crash_wait_state)
    monkeypatch.setattr(source_staging.bundle, "append_history", crash_wait_commit)
    after_wait = NOW + timedelta(hours=73)
    arguments = [
        "resume",
        "--work-bundle",
        str(bundle_path),
        "--expected-generation",
        str(waiting["generation"]),
    ]
    crash_rc, failed, _stderr = invoke(
        capsys,
        arguments,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=after_wait,
    )
    assert crash_rc == 1
    assert failed["errors"][0]["code"] == "internal_error"

    monkeypatch.setattr(source_staging.bundle, "atomic_write_json", original_atomic)
    monkeypatch.setattr(source_staging.bundle, "append_history", original_append)
    recovered_rc, recovered, _stderr = invoke(
        capsys,
        arguments,
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=after_wait,
    )
    assert recovered_rc == 0, recovered
    assert recovered["source_upload_state"] == "source_upload_unknown"
    assert recovered["action_required"] == "resolve_source_upload_unknown"
    assert lost.calls == 1


def test_ready_url_is_reused_until_expiry_then_confirm_requires_a_new_bound_action(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    staged_url = "https://files.aihubmax.com/expiring.pdf?token=keep-private"
    ready_rc, staged, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "expiry-key"},
        transport=SuccessfulUpload(staged_url),
    )
    assert ready_rc == 0
    staged_manifest = json.loads((bundle / "manifest.json").read_text())
    staged_private = json.loads((bundle / ".state" / "private.json").read_text())

    reusable_rc, reusable, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=datetime(2024, 1, 5, 3, 4, 4, tzinfo=timezone.utc),
    )
    assert reusable_rc == 0
    assert reusable["source_upload_state"] == "source_upload_ready"
    # This step reaches the recorded-credential gate (AIHUB_API_KEY is absent
    # from `environ`), which is how it stops short of a create. Task 2.3c
    # turned that gate from a zero-write return into a persisted initial
    # authorization, so the *bundle* generation now moves by one here. What
    # this test is about is unchanged and asserted directly below: the staged
    # URL is reused, not re-uploaded -- source_staging's own records are
    # byte-for-byte what the upload left behind.
    #
    # For the record: by the end of this test the bundle carries that
    # authorized initial attempt and then moves on to a source-upload expiry
    # (asserted below), which leaves the bundle parked in a state
    # (`recoverable_error` / `retry_expired_source_upload`) that cannot be
    # advanced further without an operator decision. This test does not
    # exercise, and makes no claim about, resuming past that point.
    assert reusable["generation"] == staged["generation"] + 1
    reused_manifest = json.loads((bundle / "manifest.json").read_text())
    reused_private = json.loads((bundle / ".state" / "private.json").read_text())
    assert reused_manifest["source_staging"] == staged_manifest["source_staging"]
    assert reused_private["source_uploads"] == staged_private["source_uploads"]

    expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(reusable["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-read"},
        transport=NeverNetwork(),
        now=datetime(2024, 1, 5, 3, 4, 5, tzinfo=timezone.utc),
    )

    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert expired_rc == 0
    assert expired["outcome"] == "source_upload_expired"
    assert expired["source_upload_state"] == "source_upload_expired"
    assert expired["conversion_state"] == "recoverable_error"
    assert expired["action_required"] == "retry_expired_source_upload"
    assert private_state["source_uploads"][0]["url"] == staged_url
    assert private_state["source_uploads"][0]["state"] == "source_upload_expired"
    assert manifest["source_staging"]["attempts"][0]["state"] == (
        "source_upload_expired"
    )
    assert staged_url not in json.dumps(expired) + json.dumps(manifest)


def test_auto_mode_replaces_an_expired_ready_url_without_an_intermediate_question(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="auto"
    )
    first = SuccessfulUpload("https://files.aihubmax.com/old.pdf?token=old")
    _ready_rc, staged, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "old-key"},
        transport=first,
    )
    replacement = SuccessfulUpload(
        "https://files.aihubmax.com/new.pdf?token=new"
    )

    replaced_rc, replaced, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "new-key"},
        transport=replacement,
        now=datetime(2024, 1, 5, 3, 4, 5, tzinfo=timezone.utc),
    )

    private_state = json.loads((bundle / ".state" / "private.json").read_text())
    assert replaced_rc == 0
    assert replaced["outcome"] == "source_upload_ready"
    assert replaced["source_upload_state"] == "source_upload_ready"
    assert replaced["action_required"] is None
    assert len(replacement.calls) == 1
    assert [attempt["state"] for attempt in private_state["source_uploads"]] == [
        "source_upload_expired",
        "source_upload_ready",
    ]


def test_confirm_wait_reissues_a_decision_only_after_the_conservative_window(
    tmp_path, capsys, monkeypatch
):
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    lost = LostUpload()
    _unknown_rc, unknown, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "wait-key"},
        transport=lost,
    )
    wait_rc, waiting, _stderr = invoke(
        capsys,
        [
            "record",
            "source-staging",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(unknown["generation"]),
            "--action-id",
            unknown["action_id"],
            "--evidence-hash",
            unknown["evidence_hash"],
            "--decision",
            "wait",
            "--basis",
            "Wait through a full possible URL validity window.",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
    )
    assert wait_rc == 0
    assert waiting["outcome"] == "source_upload_waiting"
    assert waiting["action_required"] is None

    before_rc, before, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(waiting["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=datetime(2024, 1, 5, 3, 4, 4, tzinfo=timezone.utc),
    )
    assert before_rc == 0
    assert before["outcome"] == "source_upload_waiting"
    assert before["generation"] == waiting["generation"]
    assert before["action_required"] is None

    elapsed_rc, elapsed, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(waiting["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ=dependencies,
        transport=NeverNetwork(),
        now=datetime(2024, 1, 5, 3, 4, 5, tzinfo=timezone.utc),
    )

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert elapsed_rc == 0
    assert elapsed["outcome"] == "source_upload_unknown"
    assert elapsed["generation"] == waiting["generation"] + 1
    assert elapsed["action_required"] == "resolve_source_upload_unknown"
    assert elapsed["action_id"] != unknown["action_id"]
    assert manifest["source_staging"]["wait_history"][0]["wait_until"] == (
        "2024-01-05T03:04:05Z"
    )
    assert lost.calls == 1


def test_aihub_upload_valid_https_url_agrees_with_doc2x_valid_https_url():
    # aihub_upload.valid_https_url is the write-side gate that decides
    # whether an AIHub upload response's URL is allowed into private.json
    # (source_staging.py's source_upload_ready transition); doc2x.valid_https_url
    # is the read-side gate that same field must clear again when a later
    # create/poll/refresh command loads private.json back
    # (conversion_attempt.py, raw_conversion.py). If the two ever disagree, a
    # URL the writer accepts and persists verbatim can be one the reader then
    # rejects with no way to fix the bundle -- so this pins that both gates
    # classify the same inputs identically, in the same unit (UTF-8 bytes,
    # not code points).
    prefix = "https://example.com/"

    # ASCII at/over the 16,384-byte bound: one character is one UTF-8 byte,
    # so this exercises the boundary in both units at once.
    at_bound = prefix + "a" * (16384 - len(prefix))
    over_bound = at_bound + "a"
    assert len(at_bound.encode("utf-8")) == 16384
    assert aihub_upload.valid_https_url(at_bound) is doc2x.valid_https_url(at_bound) is True
    assert (
        aihub_upload.valid_https_url(over_bound)
        is doc2x.valid_https_url(over_bound)
        is False
    )

    # Non-ASCII: 16,384 astral-plane characters are 65,476 UTF-8 bytes --
    # under the 16,384 *code point* bound but four times over the 16,384
    # *byte* bound. This is the case that separates a byte gate from a
    # code-point gate, so both must reject it.
    non_ascii_over_bound = prefix + "\U0001d11e" * (16384 - len(prefix))
    assert len(non_ascii_over_bound) == 16384
    assert len(non_ascii_over_bound.encode("utf-8")) == 65476
    assert aihub_upload.valid_https_url(non_ascii_over_bound) is False
    assert doc2x.valid_https_url(non_ascii_over_bound) is False

    # fail-closed paths both gates must still cover identically.
    fail_closed_cases = [
        None,
        "",
        "http://example.com",
        "https://user:pass@example.com",
        "https://exa mple.com/path",
        "https://example.com/\x01path",
        "https://\ud800.example.com",
        "https://example.com:not-a-port",
    ]
    for case in fail_closed_cases:
        assert aihub_upload.valid_https_url(case) is False
        assert doc2x.valid_https_url(case) is False


# --- I3 (task 3.1a fix round 1): tier-2 dual-implementation equivalence lock
#
# source_staging.result_from_manifest and conversion_attempt.
# project_conversion_action are two independent implementations of tier 2's
# three keys (action_required/action_id/evidence_hash) until task 3.1d wires
# project_conversion_action's own return value through the real
# result_from_manifest call chain (conversion_attempt.result_from_manifest
# today calls source_staging.result_from_manifest first and lets its tier-2
# write stand -- see project_conversion_action's own docstring for why that
# split is still correct as of this task, not a leftover). The review round
# that produced this fix brief falsified "an attempt's existence structurally
# rules out a non-empty staging pending_action": the retry placeholder and the
# credential-gate placeholder both project onto design.md's `not_started`
# row, the only precondition expire_ready_attempt checks, so the two tier-2
# implementations diverging while an attempt also exists is not ruled out by
# design -- it is merely unreached today because an unrelated bug (backlog
# issue #1) independently blocks that path. Until 3.1d's wiring removes the
# second implementation, this lock is what stands between that drift and
# production; it holds the two implementations to the same answer on every
# staging-pending shape reachable today (no conversion_attempts yet), and
# keeps paying for itself as a regression guard once 3.1d lands.


def _staging_pending_manifest_unknown(tmp_path, capsys, monkeypatch):
    """source_upload_unknown (PENDING_ACTION_KIND_BY_STATE's
    resolve_source_upload_unknown kind): any non-403 abnormal status.
    """
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "tier-2-equivalence-unknown-key"},
        transport=StatusUpload(500),
    )
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_unknown"
    return json.loads((bundle / "manifest.json").read_text())


def _staging_pending_manifest_rejected(tmp_path, capsys, monkeypatch):
    """source_upload_rejected (PENDING_ACTION_KIND_BY_STATE's
    retry_source_upload kind): the documented capacity-limit 403.
    """
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch
    )
    rc, result, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "tier-2-equivalence-rejected-key"},
        transport=StatusUpload(403),
    )
    assert rc == 0
    assert result["source_upload_state"] == "source_upload_rejected"
    return json.loads((bundle / "manifest.json").read_text())


def _staging_pending_manifest_expired(tmp_path, capsys, monkeypatch):
    """source_upload_expired (PENDING_ACTION_KIND_BY_STATE's
    retry_expired_source_upload kind): a successful upload whose 72h local
    expiry window (source_staging.py's own check) has since closed, detected
    without any network access on the following resume -- the same shape
    test_ready_url_is_reused_until_expiry_then_confirm_requires_a_new_bound_action
    drives above, trimmed to just the expiry step.
    """
    bundle, ready, dependencies, _source_bytes = ready_bundle(
        tmp_path, capsys, monkeypatch, interaction_mode="confirm"
    )
    staged_url = (
        "https://files.aihubmax.com/tier-2-equivalence-expiry.pdf?token=keep-private"
    )
    ready_rc, staged, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(ready["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "tier-2-equivalence-expiry-key"},
        transport=SuccessfulUpload(staged_url),
    )
    assert ready_rc == 0

    expired_rc, expired, _stderr = invoke(
        capsys,
        [
            "advance",
            "--work-bundle",
            str(bundle),
            "--expected-generation",
            str(staged["generation"]),
            "--visual-capability",
            "available",
        ],
        cwd=tmp_path,
        environ={**dependencies, "AIHUB_API_KEY": "must-not-be-read"},
        transport=NeverNetwork(),
        now=datetime(2024, 1, 5, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert expired_rc == 0
    assert expired["source_upload_state"] == "source_upload_expired"
    return json.loads((bundle / "manifest.json").read_text())


STAGING_PENDING_MANIFEST_BUILDERS = {
    "source_upload_unknown": _staging_pending_manifest_unknown,
    "source_upload_rejected": _staging_pending_manifest_rejected,
    "source_upload_expired": _staging_pending_manifest_expired,
}


def test_staging_pending_manifest_builders_cover_every_pending_action_kind():
    """The equivalence lock below is only as strong as its coverage: this
    pins the builder table's key set to source_staging.
    PENDING_ACTION_KIND_BY_STATE's own domain (not a hand-copied literal), so
    a future fourth staging state fails this loudly instead of the lock
    quietly staying at three-of-four.
    """
    assert set(STAGING_PENDING_MANIFEST_BUILDERS) == set(
        source_staging.PENDING_ACTION_KIND_BY_STATE
    )


@pytest.mark.parametrize(
    "state",
    sorted(STAGING_PENDING_MANIFEST_BUILDERS),
    ids=sorted(STAGING_PENDING_MANIFEST_BUILDERS),
)
def test_tier_2_projection_agrees_with_source_staging_result_from_manifest(
    tmp_path, capsys, monkeypatch, state
):
    """I3 (task 3.1a fix round 1): locks source_staging.result_from_manifest
    and conversion_attempt.project_conversion_action's tier-2 row to the same
    answer on every staging-pending shape production can reach today (see
    the module comment above this block for why the two are still
    independent implementations and why that is a real drift channel, not
    dead code). A future edit that changes one implementation without the
    other fails here rather than shipping silently.
    """
    manifest = STAGING_PENDING_MANIFEST_BUILDERS[state](tmp_path, capsys, monkeypatch)
    pending = manifest["source_staging"]["pending_action"]
    assert pending["kind"] == source_staging.PENDING_ACTION_KIND_BY_STATE[state]
    assert manifest["conversion_attempts"] == []

    direct = source_staging.result_from_manifest(
        manifest,
        work_bundle="tier-2-equivalence-probe",
        outcome="tier_2_equivalence_probe",
    )
    projected = conversion_attempt.project_conversion_action(manifest)

    assert projected is not None
    assert (
        direct["action_required"],
        direct["action_id"],
        direct["evidence_hash"],
    ) == (
        projected["action_required"],
        projected["action_id"],
        projected["evidence_hash"],
    )
