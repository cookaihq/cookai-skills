from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from dotenv_parser import DotenvError, parse_dotenv
from safe_io import FileSecurityError, read_regular_file, validate_directory
from strict_json import StrictJSONError, loads
from v2_schema import (
    CredentialProfile, SchemaError, ScopedReference, UploadTarget,
    parse_credential_map, parse_project_config, parse_reference, parse_target,
)


PROJECT_CREDENTIALS = "S3_UPLOAD_PROJECT_CREDENTIALS_JSON"
GLOBAL_CREDENTIALS = "S3_UPLOAD_GLOBAL_CREDENTIALS_JSON"


class ResolutionError(ValueError):
    pass


class CredentialExpiringError(ResolutionError):
    def __init__(self, message: str, *, resolved: "ResolvedTarget") -> None:
        super().__init__(message)
        self.resolved = resolved


@dataclass(frozen=True)
class ResolvedTarget:
    ref: ScopedReference
    source: str
    target: UploadTarget
    credential: Optional[CredentialProfile]
    credential_source: Optional[str]
    credential_state: str

    @property
    def endpoint(self) -> str:
        return self.target.endpoint

    @property
    def addressing(self) -> str:
        return self.target.addressing

    @property
    def target_fingerprint(self) -> str:
        return self.target.location_fingerprint()

    def presign_effective_seconds(self, requested: Optional[int], now: datetime) -> Optional[int]:
        if requested is None or self.credential is None:
            return None
        remaining = self.credential.remaining_seconds(now)
        return requested if remaining is None else min(requested, remaining - 60)


def _dotenv(text: Optional[str], label: str, allowed_keys) -> Dict[str, str]:
    try:
        return parse_dotenv(text, allowed_keys=allowed_keys, label=label)
    except DotenvError as exc:
        raise ResolutionError(str(exc)) from exc


def _read(path: str, *, secret: bool, missing_ok: bool = True) -> Optional[str]:
    try:
        return read_regular_file(path, max_bytes=1048576, secret=secret, missing_ok=missing_ok)
    except (FileSecurityError, OSError) as exc:
        raise ResolutionError(f"unsafe or unavailable configuration path: {os.path.basename(path)}") from exc


def _json(text: str, label: str):
    try:
        return loads(text)
    except StrictJSONError as exc:
        raise ResolutionError(f"invalid {label}: {exc}") from exc


def _project_config(cwd: str) -> Tuple[Optional[ScopedReference], Dict[str, ScopedReference]]:
    path = os.path.join(cwd, ".s3-upload", "config.json")
    text = _read(path, secret=False)
    if text is None:
        return None, {}
    try:
        return parse_project_config(_json(text, "project config"))
    except SchemaError as exc:
        raise ResolutionError(str(exc)) from exc


def _select(*, cwd: str, environ: Dict[str, str], cli_target: Optional[str],
            cli_caller: Optional[str]) -> Tuple[ScopedReference, str]:
    if cli_target is not None:
        if not cli_target:
            raise ResolutionError("--target cannot be empty")
        return parse_reference(cli_target, "Target reference"), "cli"
    if environ.get("S3_UPLOAD_TARGET"):
        return parse_reference(environ["S3_UPLOAD_TARGET"], "Target reference"), "process"
    env_local_text = _read(os.path.join(cwd, ".env.local"), secret=True)
    env_text = _read(os.path.join(cwd, ".env"), secret=False)
    env_local = _dotenv(env_local_text, ".env.local", {"S3_UPLOAD_TARGET"})
    env = _dotenv(env_text, ".env", {"S3_UPLOAD_TARGET"})
    if env_local.get("S3_UPLOAD_TARGET"):
        return parse_reference(env_local["S3_UPLOAD_TARGET"], "Target reference"), "project-env-local"
    if env.get("S3_UPLOAD_TARGET"):
        return parse_reference(env["S3_UPLOAD_TARGET"], "Target reference"), "project-env"
    caller = cli_caller if cli_caller is not None else environ.get("S3_UPLOAD_CALLER_SKILL")
    if caller is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", caller):
        raise ResolutionError("invalid caller Skill identifier")
    default, mappings = _project_config(cwd)
    if caller and caller in mappings:
        return mappings[caller], "skill-mapping"
    if default is not None:
        return default, "project-default"
    raise ResolutionError("no Upload Target selector could be resolved")


def _load_target(ref: ScopedReference, *, cwd: str, config_home: str,
                 allow_candidates: bool) -> UploadTarget:
    if ref.scope == "project":
        root = os.path.join(cwd, ".s3-upload")
        directory = os.path.join(root, "targets")
        try:
            validate_directory(root)
            validate_directory(directory)
        except (FileSecurityError, OSError) as exc:
            raise ResolutionError("project Target directory is unsafe or unavailable") from exc
        path = os.path.join(directory, ref.name + ".json")
        secret = False
    else:
        try:
            validate_directory(config_home, exact_mode=0o700)
            validate_directory(os.path.join(config_home, "targets"), exact_mode=0o700)
        except (FileSecurityError, OSError) as exc:
            raise ResolutionError("global Target directory is unsafe or unavailable") from exc
        path = os.path.join(config_home, "targets", ref.name + ".json")
        secret = True
    text = _read(path, secret=secret, missing_ok=False)
    try:
        return parse_target(_json(text or "", "Upload Target"), expected_scope=ref.scope, allow_candidates=allow_candidates)
    except SchemaError as exc:
        raise ResolutionError(str(exc)) from exc


def _credential_map(ref: ScopedReference, *, cwd: str, config_home: str,
                    environ: Dict[str, str]) -> Tuple[Dict[str, CredentialProfile], Optional[str]]:
    if ref.scope == "project":
        env_text = _read(os.path.join(cwd, ".env"), secret=False)
        env_values = _dotenv(env_text, ".env", {PROJECT_CREDENTIALS})
        if PROJECT_CREDENTIALS in env_values:
            raise ResolutionError(f"{PROJECT_CREDENTIALS} must not appear in .env")
        source = environ.get(PROJECT_CREDENTIALS, "")
        source_name = "process-project-credentials"
        if not source:
            local_text = _read(os.path.join(cwd, ".env.local"), secret=True)
            local_values = _dotenv(
                local_text, ".env.local", {PROJECT_CREDENTIALS},
            )
            source = local_values.get(PROJECT_CREDENTIALS, "")
            source_name = "project-env-local"
    else:
        source = environ.get(GLOBAL_CREDENTIALS, "")
        source_name = "process-global-credentials"
        if not source:
            home_text = _read(os.path.join(config_home, ".env"), secret=True)
            home_values = _dotenv(home_text, "home .env", {GLOBAL_CREDENTIALS})
            source = home_values.get(GLOBAL_CREDENTIALS, "")
            source_name = "global-env"
    if not source:
        return {}, None
    try:
        return parse_credential_map(_json(source, "Credential map")), source_name
    except SchemaError as exc:
        raise ResolutionError(str(exc)) from exc


def resolve_target(*, cwd: str, config_home: str, environ: Dict[str, str],
                   cli_target: Optional[str], cli_caller: Optional[str],
                   use_local_key: bool, now: Optional[datetime] = None,
                   allow_candidates: bool = False) -> ResolvedTarget:
    cwd = os.path.abspath(cwd)
    config_home = os.path.abspath(os.path.expanduser(config_home))
    try:
        ref, source = _select(
            cwd=cwd, environ=environ, cli_target=cli_target, cli_caller=cli_caller,
        )
    except SchemaError as exc:
        raise ResolutionError(str(exc)) from exc
    if ref.scope == "global" and source not in {"cli", "process"} and not use_local_key:
        raise ResolutionError("indirect Global Target selection requires --use-local-key")
    target = _load_target(ref, cwd=cwd, config_home=config_home, allow_candidates=allow_candidates)
    profiles, credential_source = _credential_map(
        target.credential, cwd=cwd, config_home=config_home, environ=environ,
    )
    selected = profiles.get(target.credential.name)
    if selected is None:
        return ResolvedTarget(ref, source, target, None, credential_source, "credential_unavailable")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    remaining = selected.remaining_seconds(moment)
    if remaining is not None and remaining <= 60:
        raise CredentialExpiringError(
            "temporary Credential Profile must have more than 60 whole seconds remaining",
            resolved=ResolvedTarget(ref, source, target, None, credential_source, "credential_expiring"),
        )
    return ResolvedTarget(ref, source, target, selected, credential_source, "available")
