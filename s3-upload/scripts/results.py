from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

from artifacts import ArtifactError, serialize_object_reference


OPERATIONS = {"upload", "url", "delete", "resume", "reconcile", "abort"}
STATUSES = {
    "ok", "dry_run", "partial_success", "not_started", "collision", "deleted",
    "not_deleted", "aborted", "ambiguous",
}
UUID4_RE = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
RESULT_KEYS = (
    "schema_version", "operation", "status", "object_written", "object_reference",
    "url", "url_kind", "expires_at", "retention", "delete_scope",
    "deleted_version_id", "checkpoint_id", "plan",
)


class ResultError(ValueError):
    pass


def empty_retention() -> Dict[str, Any]:
    return {"mode": None, "days": None, "enforcement": None}


def _retention(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"mode", "days", "enforcement"}:
        raise ResultError("retention must be the closed three-field container")
    mode, days, enforcement = value["mode"], value["days"], value["enforcement"]
    if mode is None:
        if days is not None or enforcement is not None:
            raise ResultError("empty retention must contain only null values")
    elif mode == "retain":
        if days is not None or enforcement != "external-unverified":
            raise ResultError("invalid retained policy result")
    elif mode == "expire":
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or enforcement != "external-unverified":
            raise ResultError("invalid expiring policy result")
    else:
        raise ResultError("invalid retention mode")


def _timestamp(value: str) -> None:
    if not RFC3339_RE.fullmatch(value):
        raise ResultError("expires_at must be a UTC RFC 3339 timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ResultError("expires_at must be a UTC RFC 3339 timestamp") from exc


def validate_result(result: Any, *, validate_reference: bool = True) -> Dict[str, Any]:
    if not isinstance(result, dict) or set(result) != set(RESULT_KEYS):
        raise ResultError("result must contain exactly the v1 result keys")
    if result["schema_version"] != 1 or isinstance(result["schema_version"], bool):
        raise ResultError("result schema_version must be 1")
    if result["operation"] not in OPERATIONS or result["status"] not in STATUSES:
        raise ResultError("invalid result operation or status")
    if result["object_written"] is not None and not isinstance(result["object_written"], bool):
        raise ResultError("object_written must be boolean or null")
    if result["object_reference"] is not None and validate_reference:
        try:
            serialize_object_reference(result["object_reference"])
        except ArtifactError as exc:
            raise ResultError("invalid result Object Reference") from exc
    if result["url"] is not None and (not isinstance(result["url"], str) or not result["url"]):
        raise ResultError("url must be a non-empty string or null")
    if result["url_kind"] not in {None, "public", "presigned"}:
        raise ResultError("invalid url_kind")
    if result["expires_at"] is not None:
        if not isinstance(result["expires_at"], str):
            raise ResultError("expires_at must be a string or null")
        _timestamp(result["expires_at"])
    if result["url"] is not None:
        if result["url_kind"] == "presigned" and result["expires_at"] is None:
            raise ResultError("presigned URL requires expires_at")
        if result["url_kind"] == "public" and result["expires_at"] is not None:
            raise ResultError("public URL must not have expires_at")
        if result["url_kind"] is None:
            raise ResultError("URL requires url_kind")
    elif result["expires_at"] is not None:
        raise ResultError("expires_at requires a URL")
    _retention(result["retention"])
    if result["delete_scope"] not in {None, "exact-version", "current-key"}:
        raise ResultError("invalid delete_scope")
    if result["deleted_version_id"] is not None:
        if result["delete_scope"] != "exact-version" or not isinstance(result["deleted_version_id"], str):
            raise ResultError("deleted_version_id requires exact-version deletion")
    if result["checkpoint_id"] is not None and (
        not isinstance(result["checkpoint_id"], str) or not UUID4_RE.fullmatch(result["checkpoint_id"])
    ):
        raise ResultError("invalid checkpoint_id")
    if result["status"] == "dry_run":
        if not isinstance(result["plan"], dict) or not isinstance(result["plan"].get("executable"), bool):
            raise ResultError("dry_run requires a complete plan")
        if result["checkpoint_id"] is not None or result["url"] is not None:
            raise ResultError("dry_run must not contain a checkpoint or signed URL")
        if result["operation"] == "upload" and result["object_written"] is not False:
            raise ResultError("upload dry_run requires object_written=false")
        if result["operation"] == "delete" and result["object_written"] is not None:
            raise ResultError("delete dry_run requires object_written=null")
    elif result["plan"] is not None:
        raise ResultError("plan is only allowed for dry_run")
    if result["status"] == "ambiguous":
        if result["checkpoint_id"] is None:
            raise ResultError("ambiguous result requires a checkpoint")
        if result["object_written"] is not None or result["object_reference"] is not None or result["url"] is not None:
            raise ResultError("ambiguous result must not claim an object or URL")
    if result["status"] == "collision" and result["object_written"] is not False:
        raise ResultError("collision requires object_written=false")
    if result["status"] == "aborted" and result["operation"] not in {"abort", "reconcile"}:
        raise ResultError("aborted status requires abort or reconcile operation")
    return result


def build_result(operation: str, status: str, *, object_written: Optional[bool] = None,
                 object_reference: Optional[Dict[str, Any]] = None,
                 url: Optional[str] = None, url_kind: Optional[str] = None,
                 expires_at: Optional[str] = None,
                 retention: Optional[Dict[str, Any]] = None,
                 delete_scope: Optional[str] = None,
                 deleted_version_id: Optional[str] = None,
                 checkpoint_id: Optional[str] = None,
                 plan: Optional[Dict[str, Any]] = None,
                 validate_reference: bool = True) -> Dict[str, Any]:
    result = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "object_written": object_written,
        "object_reference": object_reference,
        "url": url,
        "url_kind": url_kind,
        "expires_at": expires_at,
        "retention": empty_retention() if retention is None else dict(retention),
        "delete_scope": delete_scope,
        "deleted_version_id": deleted_version_id,
        "checkpoint_id": checkpoint_id,
        "plan": plan,
    }
    return validate_result(result, validate_reference=validate_reference)


def exit_code_for_result(result: Dict[str, Any]) -> int:
    validate_result(result, validate_reference=False)
    if result["status"] == "dry_run":
        return 0 if result["plan"]["executable"] else 2
    if result["status"] == "collision":
        return 4
    if result["status"] in {"ok", "deleted", "aborted"}:
        return 0
    return 1
