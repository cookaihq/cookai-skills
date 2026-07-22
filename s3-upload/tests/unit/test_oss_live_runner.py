from pathlib import Path
import subprocess

import run_oss_live_matrix as runner
from evidence import UnitTestResult
from run_oss_live_matrix import (
    main, run_unit_tests, verify_custom_disguise_rejected,
)


def failed_unit_tests():
    return UnitTestResult(
        command=("python3", "-m", "pytest", "s3-upload/tests/unit"),
        output=b"1 failed, 1 passed in 0.10s\n",
        returncode=1,
        total=2,
        passed=1,
        failed=1,
        errors=0,
        skipped=0,
        python_version="3.12.4",
        pytest_version="8.4.1",
    )


def passed_unit_tests():
    return UnitTestResult(
        command=("python3", "-m", "pytest", "s3-upload/tests/unit"),
        output=b"all unit tests passed\n",
        returncode=0,
        total=1,
        passed=1,
        failed=0,
        errors=0,
        skipped=0,
        python_version="3.12.4",
        pytest_version="8.4.1",
    )


def oss_fixture():
    return {
        "S3_UPLOAD_PROVIDER": "custom",
        "S3_UPLOAD_ACCESS_KEY_ID": "OSSACCESS1234",
        "S3_UPLOAD_SECRET_ACCESS_KEY": "oss-secret-value",
        "S3_UPLOAD_SESSION_TOKEN": "",
        "S3_UPLOAD_BUCKET": "candidate-bucket",
        "S3_UPLOAD_REGION": "cn-beijing",
        "S3_UPLOAD_ENDPOINT": "https://s3.oss-cn-beijing.aliyuncs.com",
        "S3_UPLOAD_ADDRESSING": "virtual",
    }


def test_exact_mainland_endpoint_is_rejected_as_custom_normal_command():
    result = verify_custom_disguise_rejected(oss_fixture())

    assert result == {
        "status": "passed",
        "provider": "custom",
        "exact_endpoint": "https://s3.oss-cn-beijing.aliyuncs.com",
        "blocking_reason": "capability_disabled",
        "request_count": 0,
    }


def test_custom_disguise_gate_runs_before_any_live_matrix_request(
    tmp_path, monkeypatch, capsys,
):
    fixture = oss_fixture()
    gate_calls = []
    transport_calls = []
    monkeypatch.setattr(runner, "load_oss_fixture", lambda _root: fixture)

    def stop_after_gate(value):
        gate_calls.append(value)
        raise runner.LiveFixtureError("stop after custom guard")

    exit_code = main(
        ["--project-root", str(tmp_path)],
        transport=lambda *args: transport_calls.append(args),
        unit_test_runner=passed_unit_tests,
        custom_guard_runner=stop_after_gate,
    )

    assert exit_code == 1
    assert gate_calls == [fixture]
    assert transport_calls == []
    assert "live_test_error" in capsys.readouterr().err


def test_unit_failure_stops_before_fixture_or_live_transport(tmp_path, capsys):
    transport_calls = []
    unit_calls = []

    def run_units():
        unit_calls.append("run")
        return failed_unit_tests()

    def transport(*args):
        transport_calls.append(args)
        raise AssertionError("live transport must not run")

    exit_code = main(
        ["--project-root", str(tmp_path)],
        transport=transport,
        unit_test_runner=run_units,
    )

    assert exit_code == 1
    assert unit_calls == ["run"]
    assert transport_calls == []
    assert "unit_test_error" in capsys.readouterr().err


def test_default_unit_runner_captures_real_pytest_output_and_structured_counts(
    tmp_path, monkeypatch,
):
    calls = []
    monkeypatch.setenv("S3_UPLOAD_LIVE_TEST", "1")
    monkeypatch.setenv("S3_UPLOAD_LIVE_TEST_TARGET", "project:must-not-leak")
    monkeypatch.setenv("S3_UPLOAD_PROJECT_CREDENTIALS_JSON", "must-not-leak")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=everything")

    def run_subprocess(command, **kwargs):
        calls.append((tuple(command), kwargs))
        junit_index = command.index("--junitxml") + 1
        Path(command[junit_index]).write_text(
            '<testsuites name="pytest tests">'
            '<testsuite tests="3" failures="0" errors="0" skipped="1"/>'
            '</testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command, 0, stdout=b"real pytest stdout and stderr\n",
        )

    result = run_unit_tests(
        repository_root=tmp_path,
        subprocess_runner=run_subprocess,
    )

    assert result.successful is True
    assert (result.total, result.passed, result.failed, result.errors, result.skipped) == (
        3, 2, 0, 0, 1,
    )
    assert result.output == b"real pytest stdout and stderr\n"
    command, options = calls[0]
    assert command[1:4] == ("-m", "pytest", "s3-upload/tests/unit")
    assert options["cwd"] == str(tmp_path)
    assert options["stderr"] is subprocess.STDOUT
    assert options["stdout"] is subprocess.PIPE
    for name in (
        "S3_UPLOAD_LIVE_TEST",
        "S3_UPLOAD_LIVE_TEST_TARGET",
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON",
        "PYTEST_ADDOPTS",
    ):
        assert name not in options["env"]
