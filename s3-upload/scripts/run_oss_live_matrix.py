#!/usr/bin/env python3
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict
from xml.etree import ElementTree

from config import Connection
from dotenv_parser import DotenvError, parse_dotenv
from evidence import UnitTestResult, create_evidence_run_config, run_evidence_matrix
from live_adapter import S3EvidenceAdapter
from provider_candidates import aliyun_oss_candidate, build_candidate_registry
from safe_io import FileSecurityError, read_regular_file
from s3 import http_request
from v2_schema import parse_credential


AUTHORIZED_OPERATIONS = frozenset(
    {
        "PutObject",
        "HeadObject",
        "GetObject",
        "PresignGetObject",
        "DeleteObjectCurrentKey",
        "ObserveDeleteCurrentKey",
        "CreateMultipartUpload",
        "UploadPart",
        "ListParts",
        "CompleteMultipartUpload",
        "AbortMultipartUpload",
        "ObserveMultipartSession",
        "ReservedMetadataRoundTrip",
        "ResponseParsing",
        "Reconciliation",
    }
)
CORE_OPERATIONS = {
    "PutObject",
    "HeadObject",
    "GetObject",
    "PresignGetObject",
    "DeleteObjectCurrentKey",
    "ObserveDeleteCurrentKey",
}
REQUIRED_FIELDS = {
    "S3_UPLOAD_PROVIDER",
    "S3_UPLOAD_ACCESS_KEY_ID",
    "S3_UPLOAD_SECRET_ACCESS_KEY",
    "S3_UPLOAD_BUCKET",
    "S3_UPLOAD_REGION",
    "S3_UPLOAD_ENDPOINT",
    "S3_UPLOAD_ADDRESSING",
}
UNIT_TEST_ENVIRONMENT_DENYLIST = {
    "PYTEST_ADDOPTS",
    "S3_UPLOAD_LIVE_TEST",
    "S3_UPLOAD_LIVE_TEST_TARGET",
    "S3_UPLOAD_PROJECT_CREDENTIALS_JSON",
    "S3_UPLOAD_GLOBAL_CREDENTIALS_JSON",
    "S3_UPLOAD_ACCESS_KEY_ID",
    "S3_UPLOAD_SECRET_ACCESS_KEY",
    "S3_UPLOAD_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}


class LiveFixtureError(ValueError):
    pass


def _junit_counts(path: Path):
    try:
        root = ElementTree.parse(path).getroot()
        suites = (
            [root]
            if root.tag.rsplit("}", 1)[-1] == "testsuite"
            else [
                item for item in root
                if item.tag.rsplit("}", 1)[-1] == "testsuite"
            ]
        )
        if not suites:
            raise ValueError("JUnit report has no test suite")
        total = sum(int(item.attrib["tests"]) for item in suites)
        failed = sum(int(item.attrib.get("failures", "0")) for item in suites)
        errors = sum(int(item.attrib.get("errors", "0")) for item in suites)
        skipped = sum(int(item.attrib.get("skipped", "0")) for item in suites)
        passed = total - failed - errors - skipped
        if passed < 0:
            raise ValueError("JUnit counts do not add up")
        return total, passed, failed, errors, skipped
    except (ElementTree.ParseError, KeyError, OSError, ValueError):
        return 1, 0, 0, 1, 0


def run_unit_tests(*, repository_root=None, subprocess_runner=subprocess.run):
    root = Path(
        repository_root or Path(__file__).resolve().parents[2]
    ).resolve()
    environment = dict(os.environ)
    for name in UNIT_TEST_ENVIRONMENT_DENYLIST:
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="s3-upload-unit-tests-") as directory:
        junit_path = Path(directory) / "pytest-junit.xml"
        command = (
            sys.executable,
            "-m",
            "pytest",
            "s3-upload/tests/unit",
            "--junitxml",
            str(junit_path),
        )
        completed = subprocess_runner(
            command,
            cwd=str(root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = completed.stdout
        if isinstance(output, str):
            output = output.encode("utf-8")
        if not isinstance(output, bytes) or not output:
            output = b"[s3-upload] pytest produced no output\n"
            returncode = 1
        else:
            returncode = completed.returncode
        total, passed, failed, errors, skipped = _junit_counts(junit_path)
    try:
        pytest_version = metadata.version("pytest")
    except metadata.PackageNotFoundError:
        pytest_version = "unavailable"
    return UnitTestResult(
        command=command,
        output=output,
        returncode=returncode,
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        python_version=platform.python_version(),
        pytest_version=pytest_version,
    )


def _git_fixture_gate(project_root: str, path: str) -> None:
    try:
        worktree = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LiveFixtureError("project fixture Git state is unavailable") from exc
    relative = os.path.relpath(path, worktree)
    if relative == ".." or relative.startswith(".." + os.sep):
        raise LiveFixtureError("project fixture is outside the Git worktree")
    tracked = subprocess.run(
        ["git", "-C", worktree, "ls-files", "--error-unmatch", "--", relative],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ignored = subprocess.run(
        ["git", "-C", worktree, "check-ignore", "--no-index", "-q", "--", relative],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if tracked.returncode == 0 or ignored.returncode != 0:
        raise LiveFixtureError("project fixture must be untracked and effectively ignored")


def load_oss_fixture(project_root: str) -> Dict[str, str]:
    root = os.path.abspath(os.path.expanduser(project_root))
    path = os.path.join(root, ".env.local")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise LiveFixtureError("project .env.local is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise LiveFixtureError("project .env.local must be an owned single-link 0600 regular file")
    _git_fixture_gate(root, path)
    try:
        text = read_regular_file(path, max_bytes=1048576, secret=True, missing_ok=False)
        values = parse_dotenv(
            text,
            allowed_keys=REQUIRED_FIELDS | {"S3_UPLOAD_SESSION_TOKEN"},
            label=".env.local",
        )
    except (DotenvError, FileSecurityError, OSError) as exc:
        raise LiveFixtureError("project .env.local could not be parsed safely") from exc
    missing = sorted(name for name in REQUIRED_FIELDS if not values.get(name))
    if missing:
        raise LiveFixtureError("project OSS fixture is incomplete")
    selected = {name: values[name] for name in REQUIRED_FIELDS}
    if selected["S3_UPLOAD_PROVIDER"] != "custom":
        raise LiveFixtureError("authorized OSS fixture must retain provider=custom provenance")
    if selected["S3_UPLOAD_ADDRESSING"] != "virtual":
        raise LiveFixtureError("authorized OSS fixture must use virtual addressing")
    if values.get("S3_UPLOAD_SESSION_TOKEN"):
        raise LiveFixtureError("authorized OSS fixture must be a permanent credential")
    selected["S3_UPLOAD_SESSION_TOKEN"] = ""
    return selected


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run the maintainer-only bounded OSS data-plane evidence matrix"
    )
    value.add_argument("--project-root", required=True)
    value.add_argument("--evidence-dir")
    return value


def main(argv=None, *, transport=http_request, unit_test_runner=None) -> int:
    args = parser().parse_args(argv)
    try:
        unit_tests = (unit_test_runner or run_unit_tests)()
        if not isinstance(unit_tests, UnitTestResult) or not unit_tests.successful:
            print("[s3-upload] unit_test_error", file=sys.stderr)
            return 1
        fixture = load_oss_fixture(args.project_root)
        candidate = aliyun_oss_candidate(
            region=fixture["S3_UPLOAD_REGION"],
            bucket=fixture["S3_UPLOAD_BUCKET"],
            endpoint=fixture["S3_UPLOAD_ENDPOINT"],
        )
        credential = parse_credential(
            {
                "access_key_id": fixture["S3_UPLOAD_ACCESS_KEY_ID"],
                "secret_access_key": fixture["S3_UPLOAD_SECRET_ACCESS_KEY"],
                "session_token": "",
                "expires_at": None,
            }
        )
        connection = Connection(
            access_key_id=credential.access_key_id,
            secret_access_key=credential.secret_access_key,
            session_token="",
            bucket=candidate.bucket,
            endpoint=candidate.service_endpoint,
            region=candidate.region,
            provider=candidate.provider,
            addressing=candidate.addressing,
        )
        target_ref = "project:authorized-oss-live-fixture"
        default_evidence = (
            Path(__file__).resolve().parent.parent
            / "tests"
            / "results"
            / uuid.uuid4().hex
        )
        evidence_dir = os.path.abspath(
            os.path.expanduser(args.evidence_dir or str(default_evidence))
        )
        config = create_evidence_run_config(
            target_ref=target_ref,
            target_integration_test=True,
            provider=candidate.provider,
            exact_endpoint=candidate.service_endpoint,
            account_applicability="authorized project OSS fixture; IAM policy scope unknown",
            privilege_verdict="unknown",
            authorized_operations=AUTHORIZED_OPERATIONS,
            evidence_dir=evidence_dir,
        )
        result = run_evidence_matrix(
            config=config,
            process_environ={
                "S3_UPLOAD_LIVE_TEST": "1",
                "S3_UPLOAD_LIVE_TEST_TARGET": target_ref,
            },
            contract_key=candidate.contract_key,
            registry=build_candidate_registry((candidate,)),
            credential=credential,
            source_bytes=b"s3-upload bounded OSS compatibility evidence\n",
            adapter=S3EvidenceAdapter(candidate, connection, transport),
            unit_tests=unit_tests,
        )
        statuses = {
            row["operation"]: row["status"] for row in result.report["operations"]
        }
        cleanup_ok = all(
            row["status"] == "passed" for row in result.report["cleanup"]
        )
        core_ok = all(statuses.get(name) == "passed" for name in CORE_OPERATIONS)
        summary = {
            "schema_version": 1,
            "classification": "evidence-not-obtained",
            "reason": "credential-privilege-unknown",
            "core_object_loop_passed": core_ok,
            "cleanup_passed": cleanup_ok,
            "residual_count": len(result.report["residuals"]),
            "evidence_id": result.report["evidence_id"],
            "evidence_dir": result.evidence_dir,
        }
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        return 0 if core_ok and cleanup_ok and not result.report["residuals"] else 1
    except Exception as exc:
        print(f"[s3-upload] live_test_error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
