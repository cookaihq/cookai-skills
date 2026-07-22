from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from capabilities import CapabilityRegistry, ContractKey
from provider_candidates import CANDIDATE_OPERATIONS
from safe_io import atomic_write
from s3 import parse_provider_identifier
from v2_schema import CredentialProfile


MATRIX_OPERATIONS = CANDIDATE_OPERATIONS
GET_EVIDENCE_OPERATIONS = frozenset(
    {"GetObject", "PublicGetObject", "PresignGetObject"}
)
MUTATION_CLEANUP_AUTHORIZATIONS = {
    "PutObject": frozenset({"DeleteObjectCurrentKey"}),
    "ConditionalPutObject": frozenset({"DeleteObjectCurrentKey"}),
    "CreateMultipartUpload": frozenset({"AbortMultipartUpload"}),
    "UploadPart": frozenset({"AbortMultipartUpload"}),
    "CompleteMultipartUpload": frozenset(
        {"DeleteObjectCurrentKey", "AbortMultipartUpload"}
    ),
    "ConditionalCompleteMultipartUpload": frozenset(
        {"DeleteObjectCurrentKey", "AbortMultipartUpload"}
    ),
    "ReservedMetadataRoundTrip": frozenset({"DeleteObjectCurrentKey"}),
}

_TARGET_RE = re.compile(r"(?:project|global):[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_RUN_ID_RE = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
_SENSITIVE_LINE_RE = re.compile(
    rb"(?im)^(authorization|proxy-authorization|cookie|set-cookie|"
    rb"x-amz-security-token|x-oss-security-token):[^\r\n]*"
)
_SIGNED_QUERY_RE = re.compile(
    rb"(?i)(X-Amz-(?:Credential|Security-Token|Signature)|"
    rb"x-oss-(?:credential|security-token|signature))=[^&\s\r\n]*"
)


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class TrackedObject:
    key: str
    version_id: Optional[str]
    checkpoint_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise EvidenceError("tracked object key is required")
        if self.version_id is not None and (
            not isinstance(self.version_id, str) or not self.version_id
        ):
            raise EvidenceError("tracked object version_id is invalid")
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
            raise EvidenceError("tracked object checkpoint_id is required")


@dataclass(frozen=True)
class TrackedSession:
    key: str
    upload_id: Optional[str]
    checkpoint_id: str
    manual_cleanup: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise EvidenceError("tracked session key is required")
        if self.upload_id is not None and (
            not isinstance(self.upload_id, str) or not self.upload_id
        ):
            raise EvidenceError("tracked session upload_id is invalid")
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
            raise EvidenceError("tracked session checkpoint_id is required")
        if self.manual_cleanup is not None and (
            not isinstance(self.manual_cleanup, str) or not self.manual_cleanup
        ):
            raise EvidenceError("tracked session manual_cleanup is invalid")


@dataclass(frozen=True)
class RequestObservation:
    method: str
    url: str
    header_names: Tuple[str, ...]
    body_size: int
    authorization_mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not re.fullmatch(
            r"[A-Z]+", self.method
        ):
            raise EvidenceError("request observation method is invalid")
        if not isinstance(self.url, str):
            raise EvidenceError("request observation URL is invalid")
        parts = urlsplit(self.url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise EvidenceError("request observation URL is invalid")
        if not isinstance(self.header_names, tuple) or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", name)
            for name in self.header_names
        ):
            raise EvidenceError("request observation header names are invalid")
        if tuple(sorted(set(self.header_names))) != self.header_names:
            raise EvidenceError("request observation header names must be sorted and unique")
        if (
            not isinstance(self.body_size, int)
            or isinstance(self.body_size, bool)
            or self.body_size < 0
        ):
            raise EvidenceError("request observation body_size is invalid")
        if self.authorization_mode not in {
            "authorization-header",
            "public-unsigned",
            "presigned-query",
            "none",
        }:
            raise EvidenceError("request observation authorization mode is invalid")

    def as_report(self, sequence: int) -> Mapping[str, Any]:
        parts = urlsplit(self.url)
        return {
            "sequence": sequence,
            "method": self.method,
            "url": urlunsplit(
                (parts.scheme, parts.netloc, parts.path, "", "")
            ),
            "query_redacted": bool(parts.query),
            "header_names": list(self.header_names),
            "body_size": self.body_size,
            "authorization_mode": self.authorization_mode,
        }


@dataclass(frozen=True)
class EvidenceObservation:
    passed: bool
    request_count: int
    requests: Tuple[RequestObservation, ...] = ()
    response_status: Optional[int] = None
    response_headers: Tuple[Tuple[str, str], ...] = ()
    response_body: bytes = b""
    raw_response: bytes = b""
    response_proved_nonsecret: bool = False
    redirect_followup_count: int = 0
    created_objects: Tuple[TrackedObject, ...] = ()
    created_sessions: Tuple[TrackedSession, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise EvidenceError("observation passed must be boolean")
        for name, value in (
            ("request_count", self.request_count),
            ("redirect_followup_count", self.redirect_followup_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise EvidenceError(f"observation {name} must be a non-negative integer")
        if self.response_status is not None and (
            not isinstance(self.response_status, int)
            or isinstance(self.response_status, bool)
            or not 100 <= self.response_status <= 599
        ):
            raise EvidenceError("observation response_status is invalid")
        if not isinstance(self.response_body, bytes) or not isinstance(
            self.raw_response, bytes
        ):
            raise EvidenceError("observation response bytes are invalid")
        if not isinstance(self.response_proved_nonsecret, bool):
            raise EvidenceError("response_proved_nonsecret must be boolean")
        if not isinstance(self.requests, tuple) or any(
            not isinstance(request, RequestObservation) for request in self.requests
        ):
            raise EvidenceError("observation requests are invalid")
        if len(self.requests) != self.request_count:
            raise EvidenceError("observation request_count does not match requests")


@dataclass(frozen=True)
class CleanupObservation:
    passed: bool
    request_count: int
    status: Optional[int]
    manual_cleanup: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise EvidenceError("cleanup passed must be boolean")
        if (
            not isinstance(self.request_count, int)
            or isinstance(self.request_count, bool)
            or self.request_count < 0
        ):
            raise EvidenceError("cleanup request_count is invalid")
        if self.status is not None and (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 100 <= self.status <= 599
        ):
            raise EvidenceError("cleanup status is invalid")
        if self.manual_cleanup is not None and (
            not isinstance(self.manual_cleanup, str) or not self.manual_cleanup
        ):
            raise EvidenceError("cleanup manual_cleanup is invalid")


@dataclass(frozen=True)
class EvidenceRunConfig:
    target_ref: str
    target_integration_test: bool
    provider: str
    exact_endpoint: str
    account_applicability: str
    privilege_verdict: str
    authorized_operations: FrozenSet[str]
    evidence_dir: str
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.target_ref, str) or not _TARGET_RE.fullmatch(
            self.target_ref
        ):
            raise EvidenceError("invalid live-test Target reference")
        if not isinstance(self.target_integration_test, bool):
            raise EvidenceError("target_integration_test must be boolean")
        if not isinstance(self.provider, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", self.provider
        ):
            raise EvidenceError("invalid live-test provider")
        if not isinstance(self.exact_endpoint, str):
            raise EvidenceError("invalid exact live-test endpoint")
        endpoint = urlsplit(self.exact_endpoint)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or "%" in endpoint.hostname
            or endpoint.hostname.endswith(".")
            or ".." in endpoint.hostname
        ):
            raise EvidenceError("invalid exact live-test endpoint")
        if (
            not isinstance(self.account_applicability, str)
            or not self.account_applicability
            or any(ord(character) < 0x20 for character in self.account_applicability)
        ):
            raise EvidenceError("invalid account applicability")
        if self.privilege_verdict not in {
            "least-privilege-confirmed",
            "unknown",
        }:
            raise EvidenceError("invalid credential privilege verdict")
        if not isinstance(self.authorized_operations, frozenset) or any(
            operation not in MATRIX_OPERATIONS
            for operation in self.authorized_operations
        ):
            raise EvidenceError("invalid live-test operation authorization")
        if not isinstance(self.evidence_dir, str) or not os.path.isabs(
            os.path.expanduser(self.evidence_dir)
        ):
            raise EvidenceError("invalid evidence directory")
        if not isinstance(self.run_id, str) or not _RUN_ID_RE.fullmatch(self.run_id):
            raise EvidenceError("run_id must be a lowercase UUID4 hex value")


@dataclass(frozen=True)
class EvidenceOperationContext:
    target_ref: str
    provider: str
    exact_endpoint: str
    contract_key: ContractKey
    run_prefix: str
    source_bytes: bytes = field(repr=False)
    source_size: int
    source_sha256: str
    now: datetime


@dataclass(frozen=True)
class EvidenceRunResult:
    gate_status: str
    persisted: bool
    report: Mapping[str, Any]
    evidence_dir: Optional[str]


def create_evidence_run_config(
    *,
    target_ref: str,
    target_integration_test: bool,
    provider: str,
    exact_endpoint: str,
    account_applicability: str,
    privilege_verdict: str,
    authorized_operations: FrozenSet[str],
    evidence_dir: str,
) -> EvidenceRunConfig:
    return EvidenceRunConfig(
        target_ref=target_ref,
        target_integration_test=target_integration_test,
        provider=provider,
        exact_endpoint=exact_endpoint,
        account_applicability=account_applicability,
        privilege_verdict=privilege_verdict,
        authorized_operations=authorized_operations,
        evidence_dir=evidence_dir,
        run_id=uuid.uuid4().hex,
    )


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _credential_values(credential: Optional[CredentialProfile]) -> Tuple[str, ...]:
    if credential is None:
        return ()
    return tuple(
        value
        for value in (
            credential.access_key_id,
            credential.secret_access_key,
            credential.session_token,
        )
        if value
    )


def _contains_sensitive(data: bytes, credentials: Sequence[str]) -> bool:
    for value in credentials:
        raw = value.encode("ascii")
        encoded = quote(value, safe="").encode("ascii")
        if raw in data or encoded in data:
            return True
    return bool(_SENSITIVE_LINE_RE.search(data) or _SIGNED_QUERY_RE.search(data))


def _redact(data: bytes, credentials: Sequence[str]) -> bytes:
    redacted = data
    replacements = sorted(
        {
            representation
            for value in credentials
            for representation in (
                value.encode("ascii"),
                quote(value, safe="").encode("ascii"),
            )
            if representation
        },
        key=len,
        reverse=True,
    )
    for value in replacements:
        redacted = redacted.replace(value, b"[REDACTED-CREDENTIAL]")
    redacted = _SENSITIVE_LINE_RE.sub(b"[REDACTED-HEADER]", redacted)
    redacted = _SIGNED_QUERY_RE.sub(b"[REDACTED-SIGNED-QUERY]", redacted)
    return redacted


def _response_evidence(
    observation: EvidenceObservation,
    credentials: Sequence[str],
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    if not observation.raw_response:
        return None, None, None
    safe = observation.response_proved_nonsecret and not _contains_sensitive(
        observation.raw_response, credentials
    )
    if safe:
        persisted = observation.raw_response
        kind = "raw_bytes_sha256"
    else:
        persisted = _redact(observation.raw_response, credentials)
        kind = "redacted_bytes_sha256"
    return persisted, kind, hashlib.sha256(persisted).hexdigest()


def _safe_identifier(value: Optional[str], credentials: Sequence[str]) -> Optional[str]:
    if value is None:
        return None
    parsed = parse_provider_identifier(value, active_credentials=credentials)
    return parsed.value if parsed.classification == "accepted" else None


def _gate_reason(
    config: EvidenceRunConfig,
    process_environ: Mapping[str, str],
) -> Optional[str]:
    if process_environ.get("S3_UPLOAD_LIVE_TEST") != "1":
        return "process_live_test_interlock_missing"
    if process_environ.get("S3_UPLOAD_LIVE_TEST_TARGET") != config.target_ref:
        return "process_target_allowlist_mismatch"
    if not config.target_integration_test:
        return "target_not_marked_for_integration_test"
    return None


def _credential_report(
    credential: Optional[CredentialProfile],
    now: datetime,
) -> Tuple[Mapping[str, Any], bool]:
    if credential is None:
        return {"kind": None, "expires_at": None, "unexpired": False}, False
    expiry = credential.expires_at
    unexpired = expiry is None or expiry > now
    return {
        "kind": credential.kind,
        "expires_at": None if expiry is None else _timestamp(expiry),
        "unexpired": unexpired,
    }, unexpired


def _private_directory(path: str) -> str:
    absolute = os.path.abspath(os.path.expanduser(path))
    current = Path(absolute).anchor
    for part in Path(absolute).parts[1:]:
        current = os.path.join(current, part)
        created = False
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            info = os.lstat(current)
            created = True
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EvidenceError("evidence path contains a symlink or non-directory")
        if (created or current == absolute) and info.st_uid != os.geteuid():
            raise EvidenceError("evidence path is not owned by the effective user")
    os.chmod(absolute, 0o700)
    if stat.S_IMODE(os.stat(absolute).st_mode) != 0o700:
        raise EvidenceError("evidence directory permissions must be 0700")
    return absolute


def _require_ignored_if_in_worktree(path: str) -> None:
    parent = os.path.abspath(path)
    while not os.path.exists(parent):
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            return
        parent = next_parent
    if not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    probe = subprocess.run(
        ["git", "-C", parent, "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return
    root = os.path.realpath(probe.stdout.strip())
    real = os.path.realpath(path)
    try:
        common = os.path.commonpath((root, real))
    except ValueError as exc:
        raise EvidenceError("invalid evidence worktree path") from exc
    if common != root:
        return
    relative = os.path.relpath(real, root)
    ignore_probe = os.path.join(relative, ".s3-upload-evidence-probe")
    ignored = subprocess.run(
        ["git", "-C", root, "check-ignore", "-q", "--", ignore_probe],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ignored.returncode != 0:
        raise EvidenceError("evidence directory must be Git ignored")


def _markdown_report(report: Mapping[str, Any]) -> bytes:
    lines = [
        "# S3 provider evidence",
        "",
        f"Evidence ID: `{report['evidence_id']}`",
        f"Provider: `{report['provider']}`",
        f"Endpoint: `{report['exact_endpoint']}`",
        f"Target: `{report['target_ref']}`",
        f"Privilege: `{report['privilege_verdict']}`",
        f"Release eligible: `{str(report['release_eligible']).lower()}`",
        "",
        "## Operations",
        "",
        "| Operation | Status | Reason | Requests | HTTP |",
        "|---|---|---|---:|---:|",
    ]
    for item in report["operations"]:
        lines.append(
            "| {operation} | {status} | {reason} | {request_count} | {response_status} |".format(
                operation=item["operation"],
                status=item["status"],
                reason=item["reason"] or "",
                request_count=item["request_count"],
                response_status=item["response_status"] or "",
            )
        )
    lines.extend(["", "## Cleanup", ""])
    if report["cleanup"]:
        for item in report["cleanup"]:
            lines.append(
                f"- `{item['resource_type']}` `{item['key']}`: `{item['status']}`"
            )
    else:
        lines.append("- No tracked resources.")
    if report["residuals"]:
        lines.extend(["", "## Manual cleanup", ""])
        for item in report["residuals"]:
            lines.append(
                f"- `{item['resource_type']}` `{item['key']}`: {item['manual_cleanup']}"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_bundle(
    path: str,
    report: Mapping[str, Any],
    raw_responses: Sequence[Tuple[str, bytes]],
) -> str:
    absolute = os.path.abspath(os.path.expanduser(path))
    _require_ignored_if_in_worktree(absolute)
    directory = _private_directory(absolute)
    existing = [entry for entry in os.listdir(directory) if not entry.startswith(".")]
    if existing:
        raise EvidenceError("evidence directory must be empty for a new run")
    raw_directory = _private_directory(os.path.join(directory, "raw_responses"))
    for filename, data in raw_responses:
        atomic_write(os.path.join(raw_directory, filename), data, mode=0o600, replace=False)
    serialized = (
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("ascii")
    atomic_write(os.path.join(directory, "report.json"), serialized, mode=0o600, replace=False)
    atomic_write(
        os.path.join(directory, "report.md"),
        _markdown_report(report),
        mode=0o600,
        replace=False,
    )
    log_lines = [
        f"{item['operation']} {item['status']} requests={item['request_count']}"
        for item in report["operations"]
    ]
    atomic_write(
        os.path.join(directory, "test_output.log"),
        ("\n".join(log_lines) + "\n").encode("ascii"),
        mode=0o600,
        replace=False,
    )
    return directory


def run_evidence_matrix(
    *,
    config: EvidenceRunConfig,
    process_environ: Mapping[str, str],
    contract_key: ContractKey,
    registry: CapabilityRegistry,
    credential: Optional[CredentialProfile],
    source_bytes: bytes,
    adapter: Any,
    now: Optional[datetime] = None,
) -> EvidenceRunResult:
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise EvidenceError("live-test source bytes must be non-empty")
    if contract_key.provider != config.provider:
        raise EvidenceError("contract provider does not match the live-test config")
    if urlsplit(config.exact_endpoint).scheme != contract_key.scheme:
        raise EvidenceError("exact endpoint scheme does not match the contract key")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    gate_reason = _gate_reason(config, process_environ)
    if gate_reason is not None:
        return EvidenceRunResult(
            gate_status="not-authorized",
            persisted=False,
            report={"schema_version": 1, "gate_reason": gate_reason, "operations": []},
            evidence_dir=None,
        )
    _require_ignored_if_in_worktree(config.evidence_dir)
    credentials = _credential_values(credential)
    applicability = config.account_applicability.encode("utf-8")
    if _contains_sensitive(applicability, credentials):
        raise EvidenceError("account applicability contains credential material")
    credential_summary, credential_unexpired = _credential_report(credential, moment)
    run_prefix = f"s3-upload-live-test/{config.run_id}/"
    context = EvidenceOperationContext(
        target_ref=config.target_ref,
        provider=config.provider,
        exact_endpoint=config.exact_endpoint,
        contract_key=contract_key,
        run_prefix=run_prefix,
        source_bytes=source_bytes,
        source_size=len(source_bytes),
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        now=moment,
    )
    operation_reports = []
    raw_responses = []
    tracked_objects = []
    tracked_sessions = []
    residuals = []
    for index, operation in enumerate(MATRIX_OPERATIONS, 1):
        capability = registry.lookup(contract_key, operation)
        item = {
            "operation": operation,
            "capability_state": capability.state,
            "capability_evidence_id": capability.evidence_id,
            "status": None,
            "reason": None,
            "request_count": 0,
            "response_status": None,
            "redirect_followup_count": 0,
            "requests": [],
            "response_header_names": [],
            "response_body": None,
            "persisted_response_hash_kind": None,
            "persisted_response_hash": None,
            "raw_response_path": None,
        }
        if capability.state not in {"enabled", "test-only"}:
            item["status"] = "not-supported"
            item["reason"] = "capability_unavailable"
            operation_reports.append(item)
            continue
        if operation not in config.authorized_operations:
            item["status"] = "not-authorized"
            item["reason"] = "operation_authorization_missing"
            operation_reports.append(item)
            continue
        cleanup_required = MUTATION_CLEANUP_AUTHORIZATIONS.get(
            operation, frozenset()
        )
        if not cleanup_required.issubset(config.authorized_operations):
            item["status"] = "not-authorized"
            item["reason"] = "cleanup_authorization_missing"
            operation_reports.append(item)
            continue
        if not credential_unexpired:
            item["status"] = "not-tested"
            item["reason"] = "credential_unavailable_or_expired"
            operation_reports.append(item)
            continue
        try:
            observation = adapter.execute(operation, context)
            if not isinstance(observation, EvidenceObservation):
                raise EvidenceError("adapter returned an invalid observation")
        except Exception:
            item["status"] = "failed"
            item["reason"] = "adapter_execution_failed"
            operation_reports.append(item)
            continue
        item["request_count"] = observation.request_count
        item["requests"] = [
            request.as_report(sequence)
            for sequence, request in enumerate(observation.requests, 1)
        ]
        item["response_status"] = observation.response_status
        item["redirect_followup_count"] = observation.redirect_followup_count
        item["response_header_names"] = sorted(
            {
                name.lower()
                for name, _ in observation.response_headers
                if isinstance(name, str)
                and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
            }
        )
        persisted, hash_kind, persisted_hash = _response_evidence(
            observation, credentials
        )
        if persisted is not None:
            filename = f"{index:02d}-{re.sub(r'(?<!^)(?=[A-Z])', '-', operation).lower()}.response"
            item["persisted_response_hash_kind"] = hash_kind
            item["persisted_response_hash"] = persisted_hash
            item["raw_response_path"] = "raw_responses/" + filename
            raw_responses.append((filename, persisted))
        if operation in GET_EVIDENCE_OPERATIONS:
            matches = (
                len(observation.response_body) == len(source_bytes)
                and hashlib.sha256(observation.response_body).digest()
                == hashlib.sha256(source_bytes).digest()
            )
            item["response_body"] = {
                "size": len(observation.response_body),
                "sha256": hashlib.sha256(observation.response_body).hexdigest(),
                "matches_source": matches,
            }
        status = "passed"
        reason = None
        if not observation.passed:
            status, reason = "failed", "adapter_reported_failure"
        elif observation.request_count == 0:
            status, reason = "failed", "no_request_observed"
        elif observation.response_status is not None and (
            300 <= observation.response_status < 400
        ):
            status, reason = "failed", "redirect_response"
        elif observation.redirect_followup_count != 0:
            status, reason = "failed", "redirect_followup_observed"
        elif operation in GET_EVIDENCE_OPERATIONS and observation.request_count != 1:
            status, reason = "failed", "get_request_count_mismatch"
        elif operation in GET_EVIDENCE_OPERATIONS and not (
            observation.response_status is not None
            and 200 <= observation.response_status < 300
        ):
            status, reason = "failed", "get_response_not_successful"
        elif operation in GET_EVIDENCE_OPERATIONS and not item["response_body"][
            "matches_source"
        ]:
            status, reason = "failed", "response_bytes_mismatch"
        for reference in observation.created_objects:
            if not reference.key.startswith(run_prefix):
                status, reason = "failed", "resource_outside_run_prefix"
                residuals.append(
                    {
                        "resource_type": "object",
                        "key": reference.key,
                        "version_id": None,
                        "upload_id": None,
                        "checkpoint_id": reference.checkpoint_id,
                        "manual_cleanup": "review out-of-scope object without automated deletion",
                    }
                )
                continue
            tracked_objects.append(reference)
        for reference in observation.created_sessions:
            if not reference.key.startswith(run_prefix):
                status, reason = "failed", "resource_outside_run_prefix"
                residuals.append(
                    {
                        "resource_type": "multipart-session",
                        "key": reference.key,
                        "version_id": None,
                        "upload_id": None,
                        "checkpoint_id": reference.checkpoint_id,
                        "manual_cleanup": "review out-of-scope session without automated abort",
                    }
                )
                continue
            tracked_sessions.append(reference)
        item["status"], item["reason"] = status, reason
        operation_reports.append(item)

    cleanup_reports = []
    for reference in tracked_objects:
        safe_version = _safe_identifier(reference.version_id, credentials)
        if "DeleteObjectCurrentKey" not in config.authorized_operations:
            cleanup = CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup="DeleteObjectCurrentKey cleanup was not authorized",
            )
            cleanup_status = "not-authorized"
        elif reference.version_id is not None and safe_version is None:
            cleanup = CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup="provider version identifier was rejected; inspect restricted checkpoint",
            )
            cleanup_status = "failed"
        else:
            try:
                cleanup = adapter.cleanup_object(reference, context)
                if not isinstance(cleanup, CleanupObservation):
                    raise EvidenceError("adapter returned an invalid cleanup observation")
            except Exception:
                cleanup = CleanupObservation(
                    passed=False,
                    request_count=0,
                    status=None,
                    manual_cleanup="delete exact object reference from the restricted checkpoint",
                )
            cleanup_status = "passed" if cleanup.passed else "failed"
        row = {
            "resource_type": "object",
            "key": reference.key,
            "version_id": safe_version,
            "upload_id": None,
            "checkpoint_id": reference.checkpoint_id,
            "status": cleanup_status,
            "request_count": cleanup.request_count,
            "response_status": cleanup.status,
            "manual_cleanup": cleanup.manual_cleanup,
        }
        cleanup_reports.append(row)
        if not cleanup.passed:
            residuals.append(
                {
                    "resource_type": "object",
                    "key": reference.key,
                    "version_id": safe_version,
                    "upload_id": None,
                    "checkpoint_id": reference.checkpoint_id,
                    "manual_cleanup": cleanup.manual_cleanup,
                }
            )
    for reference in tracked_sessions:
        safe_upload_id = _safe_identifier(reference.upload_id, credentials)
        if "AbortMultipartUpload" not in config.authorized_operations:
            cleanup = CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup="AbortMultipartUpload cleanup was not authorized",
            )
            cleanup_status = "not-authorized"
        elif reference.upload_id is None:
            cleanup = CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup=reference.manual_cleanup
                or "initiation result unknown; inspect the restricted checkpoint",
            )
            cleanup_status = "failed"
        elif safe_upload_id is None:
            cleanup = CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup="provider upload identifier was rejected; inspect restricted checkpoint",
            )
            cleanup_status = "failed"
        else:
            try:
                cleanup = adapter.abort_session(reference, context)
                if not isinstance(cleanup, CleanupObservation):
                    raise EvidenceError("adapter returned an invalid cleanup observation")
            except Exception:
                cleanup = CleanupObservation(
                    passed=False,
                    request_count=0,
                    status=None,
                    manual_cleanup="abort exact upload id from the restricted checkpoint",
                )
            cleanup_status = "passed" if cleanup.passed else "failed"
        row = {
            "resource_type": "multipart-session",
            "key": reference.key,
            "version_id": None,
            "upload_id": safe_upload_id,
            "checkpoint_id": reference.checkpoint_id,
            "status": cleanup_status,
            "request_count": cleanup.request_count,
            "response_status": cleanup.status,
            "manual_cleanup": cleanup.manual_cleanup,
        }
        cleanup_reports.append(row)
        if not cleanup.passed:
            residuals.append(
                {
                    "resource_type": "multipart-session",
                    "key": reference.key,
                    "version_id": None,
                    "upload_id": safe_upload_id,
                    "checkpoint_id": reference.checkpoint_id,
                    "manual_cleanup": cleanup.manual_cleanup,
                }
            )
    all_operations_passed = all(
        item["status"] == "passed" for item in operation_reports
    )
    all_cleanup_passed = all(
        item["status"] == "passed" for item in cleanup_reports
    )
    release_eligible = bool(
        config.privilege_verdict == "least-privilege-confirmed"
        and all_operations_passed
        and all_cleanup_passed
        and not residuals
    )
    report = {
        "schema_version": 1,
        "evidence_id": f"{config.provider}-{config.run_id}",
        "provider": config.provider,
        "exact_endpoint": config.exact_endpoint.rstrip("/"),
        "target_ref": config.target_ref,
        "contract_key": contract_key.as_dict(),
        "run_prefix": run_prefix,
        "started_at": _timestamp(moment),
        "finished_at": _timestamp(moment),
        "account_applicability": config.account_applicability,
        "credential": credential_summary,
        "privilege_verdict": config.privilege_verdict,
        "release_eligible": release_eligible,
        "source": {
            "size": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "operations": operation_reports,
        "cleanup": cleanup_reports,
        "residuals": residuals,
    }
    directory = _write_bundle(config.evidence_dir, report, raw_responses)
    return EvidenceRunResult(
        gate_status="authorized",
        persisted=True,
        report=report,
        evidence_dir=directory,
    )
