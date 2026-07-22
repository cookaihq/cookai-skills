from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Union
from xml.etree import ElementTree
import uuid

from artifacts import (
    ArtifactError,
    CheckpointStore,
    ReferenceOutputSnapshot,
    build_object_reference,
    parse_checkpoint,
    preflight_reference_output,
    write_reference_output,
)
from capabilities import (
    CapabilityRegistry,
    LiveTestInterlock,
    OperationShape,
    plan_operation,
)
from operations import connection_for, generate_object_url
from planning import derive_contract_key, registry_for_target
from resolver import ResolvedTarget
from response_parser import parse_operation_response
from results import build_result, validate_result
from s3 import Response, build_signed_request, parse_provider_identifier
from source_file import (
    SourceError,
    SourcePart,
    VerifiedSource,
    verify_resumable_source,
)


class MultipartError(RuntimeError):
    pass


ClockInput = Optional[Union[datetime, Callable[[], datetime]]]


@dataclass
class MultipartOutcome:
    result: Dict[str, Any]
    store: CheckpointStore
    checkpoint_id: str
    retain_checkpoint: bool

    def finalize(self) -> None:
        if self.retain_checkpoint:
            return
        try:
            self.store.remove(self.checkpoint_id)
        except (ArtifactError, FileNotFoundError):
            pass


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(now: ClockInput) -> datetime:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise MultipartError("invalid clock value")
    return value.astimezone(timezone.utc)


def _set_state(checkpoint: Dict[str, Any], state: str, moment: datetime,
               **changes: Any) -> Dict[str, Any]:
    updated = copy.deepcopy(checkpoint)
    updated["state"] = state
    updated["updated_at"] = _timestamp(moment)
    updated.update(changes)
    return updated


def _active_credentials(resolved: ResolvedTarget):
    credential = resolved.credential
    if credential is None:
        raise MultipartError("Credential Profile is unavailable")
    return (
        credential.access_key_id,
        credential.secret_access_key,
        credential.session_token,
    )


def _require_credential_lifetime(resolved: ResolvedTarget, moment: datetime) -> None:
    credential = resolved.credential
    if credential is None:
        raise MultipartError("Credential Profile is unavailable")
    remaining = credential.remaining_seconds(moment)
    if remaining is not None and remaining <= 60:
        raise MultipartError(
            "temporary Credential Profile must have more than 60 whole seconds remaining"
        )


def _validated_request_moment(
    resolved: ResolvedTarget, now: ClockInput,
) -> datetime:
    moment = _moment(now)
    _require_credential_lifetime(resolved, moment)
    return moment


def _authorize_upload(
    resolved: ResolvedTarget,
    collision: str,
    *,
    access_mode: Optional[str] = None,
    registry: Optional[CapabilityRegistry],
    execution_mode: str,
    live_test_interlock: Optional[LiveTestInterlock],
    allow_insecure_http: bool,
) -> None:
    key = derive_contract_key(resolved.target)
    selected_registry = registry or registry_for_target(resolved.target, key)
    authorization = plan_operation(
        OperationShape(
            operation="upload",
            access_mode=access_mode or resolved.target.access.mode,
            upload_mode="multipart",
            collision=collision,
        ),
        contract_key=key,
        registry=selected_registry,
        execution_mode=execution_mode,
        target_ref=resolved.ref.text,
        live_test_interlock=live_test_interlock,
        allow_insecure_http=allow_insecure_http,
        credential_available=resolved.credential is not None,
    )
    if not authorization.executable:
        raise MultipartError(
            "multipart execution is blocked: " + ",".join(authorization.blocking_reasons)
        )


def _capability_allowed(
    resolved: ResolvedTarget,
    operation: str,
    *,
    registry: Optional[CapabilityRegistry],
    execution_mode: str,
    live_test_interlock: Optional[LiveTestInterlock],
) -> bool:
    key = derive_contract_key(resolved.target)
    selected_registry = registry or registry_for_target(resolved.target, key)
    capability = selected_registry.lookup(key, operation)
    if capability.state == "enabled":
        return True
    return bool(
        capability.state == "test-only"
        and execution_mode == "test-only"
        and live_test_interlock is not None
        and live_test_interlock.enabled
        and live_test_interlock.target_ref == resolved.ref.text
    )


def _request(
    *,
    resolved: ResolvedTarget,
    method: str,
    key: str,
    query=(),
    body: bytes = b"",
    headers=(),
    now: ClockInput,
    transport: Callable[..., Response],
    request_builder: Callable[..., Any],
) -> Optional[Response]:
    moment = _validated_request_moment(resolved, now)
    signed = request_builder(
        connection_for(resolved),
        method=method,
        key=key,
        query=query,
        body=body,
        headers=headers,
        now=moment,
    )
    try:
        return transport(signed.method, signed.url, signed.headers, signed.body)
    except Exception:
        return None


def _response(
    response: Optional[Response],
    *,
    operation: str,
    resolved: ResolvedTarget,
    conditional: bool = False,
):
    if response is None:
        from response_parser import OperationResponse

        return OperationResponse("unknown", {})
    return parse_operation_response(
        response,
        operation=operation,
        active_credentials=_active_credentials(resolved),
        conditional=conditional,
    )


def _request_headers(plan: Dict[str, Any], content_length: int):
    values = [
        ("content-length", str(content_length)),
        ("content-type", plan["headers"]["content_type"]),
    ]
    if plan["headers"]["cache_control"] is not None:
        values.append(("cache-control", plan["headers"]["cache_control"]))
    if plan["headers"]["content_disposition"] is not None:
        values.append(("content-disposition", plan["headers"]["content_disposition"]))
    return tuple(values)


def _header(response: Response, name: str) -> Optional[str]:
    for raw_name, value in (response.headers or {}).items():
        if raw_name.lower() == name.lower() and isinstance(value, str):
            return value
    return None


def _head_completion_observation(
    response: Optional[Response],
    checkpoint: Dict[str, Any],
    resolved: ResolvedTarget,
):
    if response is None or not 200 <= response.status < 300:
        return "not-complete", None
    version_values = []
    for name in ("x-amz-version-id", "x-oss-version-id", "x-cos-version-id"):
        value = _header(response, name)
        if value is not None:
            version_values.append(value)
    if len(set(version_values)) > 1:
        return "inconclusive", None
    version_id = None
    if version_values:
        parsed = parse_provider_identifier(
            version_values[0], active_credentials=_active_credentials(resolved)
        )
        if parsed.classification != "accepted":
            return "inconclusive", None
        version_id = parsed.value
    if not (
        _header(response, "x-amz-meta-s3-upload-operation-id")
        == checkpoint["operation_id"]
        and _header(response, "x-amz-meta-s3-upload-sha256")
        == checkpoint["source"]["sha256"]
        and _header(response, "content-length")
        == str(checkpoint["source"]["size"])
    ):
        return "not-complete", None
    reference = copy.deepcopy(checkpoint["object_reference_draft"])
    reference["location"]["version_id"] = version_id
    return "confirmed", reference


def _completion_body(parts) -> bytes:
    root = ElementTree.Element("CompleteMultipartUpload")
    for row in parts:
        part = ElementTree.SubElement(root, "Part")
        ElementTree.SubElement(part, "PartNumber").text = str(row["part_number"])
        ElementTree.SubElement(part, "ETag").text = row["etag"]
    return ElementTree.tostring(root, encoding="utf-8", short_empty_elements=True)


def _complete_result(
    *,
    resolved: ResolvedTarget,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
    project_root: str,
    config_home: str,
    operation: str,
    now: ClockInput,
) -> MultipartOutcome:
    checkpoint_id = checkpoint["checkpoint_id"]
    reference = checkpoint["object_reference_draft"]
    try:
        generated = generate_object_url(
            resolved=resolved,
            reference=reference,
            presign_expires=None,
            now=now,
        )
        generated["operation"] = operation
        generated["object_written"] = True
        generated = validate_result(generated)
    except Exception:
        result = build_result(
            operation,
            "partial_success",
            object_written=True,
            object_reference=reference,
            url_kind=("public" if reference["access"]["mode"] == "public" else "presigned"),
            retention=reference["retention"],
            checkpoint_id=checkpoint_id,
        )
        return MultipartOutcome(result, store, checkpoint_id, True)
    if checkpoint["reference_out"] is not None:
        snapshot = ReferenceOutputSnapshot(
            checkpoint["reference_out"], project_root, config_home,
            (
                None if checkpoint["source"]["device"] is None else int(checkpoint["source"]["device"]),
                None if checkpoint["source"]["inode"] is None else int(checkpoint["source"]["inode"]),
            ),
        )
        try:
            write_reference_output(snapshot, reference)
        except ArtifactError:
            result = dict(generated)
            result["status"] = "partial_success"
            result["checkpoint_id"] = checkpoint_id
            result = validate_result(result)
            return MultipartOutcome(result, store, checkpoint_id, True)
    return MultipartOutcome(generated, store, checkpoint_id, False)


def _partial(
    operation: str,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
) -> MultipartOutcome:
    result = build_result(
        operation,
        "partial_success",
        object_written=False,
        object_reference=checkpoint["object_reference_draft"],
        retention=checkpoint["object_reference_draft"]["retention"],
        checkpoint_id=checkpoint["checkpoint_id"],
    )
    return MultipartOutcome(result, store, checkpoint["checkpoint_id"], True)


def _ambiguous(
    operation: str,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
) -> MultipartOutcome:
    result = build_result(
        operation,
        "ambiguous",
        object_written=None,
        retention=checkpoint["object_reference_draft"]["retention"],
        checkpoint_id=checkpoint["checkpoint_id"],
    )
    return MultipartOutcome(result, store, checkpoint["checkpoint_id"], True)


def _validate_recovery_checkpoint(
    resolved: ResolvedTarget, checkpoint: Dict[str, Any]
) -> None:
    try:
        parse_checkpoint(checkpoint, _active_credentials(resolved))
    except ArtifactError as exc:
        raise MultipartError("checkpoint identifier or schema is invalid") from exc
    if checkpoint.get("kind") != "multipart":
        raise MultipartError("checkpoint is not a multipart upload")
    if checkpoint.get("target_fingerprint") != resolved.target_fingerprint:
        raise MultipartError("Checkpoint Target fingerprint does not match the selected Target")
    if checkpoint["object_reference_draft"]["target_fingerprint"] != resolved.target_fingerprint:
        raise MultipartError("checkpoint Object Reference Target fingerprint mismatch")


def _checkpoint_plan(checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    reference = checkpoint["object_reference_draft"]
    upload_plan = checkpoint["upload_plan"]
    return {
        "object_key": reference["location"]["key"],
        "headers": {
            "content_type": upload_plan["content_type"],
            "cache_control": upload_plan["cache_control"],
            "content_disposition": upload_plan["content_disposition"],
        },
        "access": {
            "mode": reference["access"]["mode"],
            "url_kind": "public" if reference["access"]["mode"] == "public" else "presigned",
            "presign_expires_seconds": upload_plan["presign_expires_seconds"],
            "public_base_url": reference["access"]["public_base_url"],
        },
        "collision": {"policy": checkpoint["collision"]["policy"], "max_attempts": 1},
    }


def _validate_initial_plan(resolved: ResolvedTarget, plan: Dict[str, Any]) -> None:
    target = resolved.target
    threshold = target.limits.multipart_threshold_bytes
    if threshold is None or plan["source"]["size"] < threshold:
        raise MultipartError("source does not select multipart at the Target threshold")
    if plan["collision"] != {"policy": target.collision, "max_attempts": 1}:
        raise MultipartError("multipart collision plan does not match the Target")
    access = plan["access"]
    if (
        access["mode"] != target.access.mode
        or access["public_base_url"] != target.access.public_base_url
    ):
        raise MultipartError("multipart access plan does not match the Target")
    if plan.get("target_ref", resolved.ref.text) != resolved.ref.text:
        raise MultipartError("multipart plan Target reference changed")
    if plan.get("target_fingerprint", resolved.target_fingerprint) != resolved.target_fingerprint:
        raise MultipartError("multipart plan Target fingerprint changed")
    contract = plan.get("contract_key")
    if contract is not None and contract != derive_contract_key(target).as_dict():
        raise MultipartError("multipart plan Contract Key changed")


def _finish_existing_session(
    *,
    resolved: ResolvedTarget,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
    transport: Callable[..., Response],
    project_root: str,
    config_home: str,
    operation: str,
    request_now: ClockInput,
    request_builder: Callable[..., Any],
) -> MultipartOutcome:
    plan = _checkpoint_plan(checkpoint)
    request_moment = _validated_request_moment(resolved, request_now)
    checkpoint = _set_state(checkpoint, "completing", request_moment)
    store.replace(checkpoint)
    body = _completion_body(checkpoint["multipart"]["acknowledged_parts"])
    headers = [
        ("content-length", str(len(body))),
        ("content-type", "application/xml"),
    ]
    conditional = checkpoint["collision"]["policy"] != "replace"
    if conditional:
        headers.append(("if-none-match", "*"))
    response = _request(
        resolved=resolved,
        method="POST",
        key=plan["object_key"],
        query=(("uploadId", checkpoint["multipart"]["upload_id"]),),
        body=body,
        headers=tuple(headers),
        now=request_moment,
        transport=transport,
        request_builder=request_builder,
    )
    parsed = _response(
        response,
        operation="CompleteMultipartUpload",
        resolved=resolved,
        conditional=conditional,
    )
    if parsed.classification == "precondition":
        checkpoint = _set_state(
            checkpoint, "collision_detected", request_moment
        )
        store.replace(checkpoint)
        result = build_result(
            operation,
            "collision",
            object_written=False,
            object_reference=checkpoint["object_reference_draft"],
            retention=checkpoint["object_reference_draft"]["retention"],
            checkpoint_id=checkpoint["checkpoint_id"],
        )
        return MultipartOutcome(
            result, store, checkpoint["checkpoint_id"], True
        )
    if parsed.classification == "session_absent":
        store.remove(checkpoint["checkpoint_id"])
        raise MultipartError("multipart session no longer exists")
    if parsed.classification == "definitive_failure":
        checkpoint = _set_state(checkpoint, "uploading", request_moment)
        store.replace(checkpoint)
        return _partial(operation, checkpoint, store)
    if parsed.classification != "success":
        checkpoint = _set_state(
            checkpoint, "completion_unknown", request_moment
        )
        store.replace(checkpoint)
        return _ambiguous(operation, checkpoint, store)
    previous_reference = checkpoint["object_reference_draft"]
    reference = build_object_reference(
        target_ref=checkpoint["target_ref"],
        target=resolved.target,
        key=plan["object_key"],
        version_id=parsed.identifiers.get("version_id"),
        credentials=_active_credentials(resolved),
    )
    reference["access"] = previous_reference["access"]
    reference["retention"] = previous_reference["retention"]
    checkpoint = _set_state(
        checkpoint, "complete", request_moment, object_reference_draft=reference
    )
    store.replace(checkpoint)
    return _complete_result(
        resolved=resolved,
        checkpoint=checkpoint,
        store=store,
        project_root=project_root,
        config_home=config_home,
        operation=operation,
        now=request_now,
    )


def resume_multipart(
    *,
    resolved: ResolvedTarget,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
    transport: Callable[..., Response],
    project_root: str,
    config_home: str,
    now: ClockInput,
    registry: Optional[CapabilityRegistry] = None,
    execution_mode: str = "normal",
    live_test_interlock: Optional[LiveTestInterlock] = None,
    allow_insecure_http: bool = False,
    request_builder: Callable[..., Any] = build_signed_request,
) -> MultipartOutcome:
    _validate_recovery_checkpoint(resolved, checkpoint)
    state = checkpoint["state"]
    if state == "initiating":
        checkpoint = _set_state(checkpoint, "initiation_unknown", _moment(now))
        store.replace(checkpoint)
        return _ambiguous("resume", checkpoint, store)
    if state == "completing":
        checkpoint = _set_state(checkpoint, "completion_unknown", _moment(now))
        store.replace(checkpoint)
        raise MultipartError("completion_unknown requires reconcile")
    if state == "completion_unknown":
        raise MultipartError("completion_unknown requires reconcile")
    if state == "initiation_unknown":
        return _ambiguous("resume", checkpoint, store)
    if state not in {"prepared", "initiated", "uploading"}:
        raise MultipartError("multipart checkpoint is not resumable")
    _authorize_upload(
        resolved,
        checkpoint["collision"]["policy"],
        access_mode=checkpoint["object_reference_draft"]["access"]["mode"],
        registry=registry,
        execution_mode=execution_mode,
        live_test_interlock=live_test_interlock,
        allow_insecure_http=allow_insecure_http,
    )
    moment = _moment(now)
    part_size = checkpoint["multipart"]["part_size_bytes"]
    if state == "prepared":
        try:
            verify_resumable_source(
                checkpoint["source"], (), part_size_bytes=part_size
            )
        except SourceError as exc:
            raise SourceError("prepared multipart source is unavailable or changed") from exc
        request_moment = _validated_request_moment(resolved, now)
        checkpoint = _set_state(checkpoint, "initiating", request_moment)
        store.replace(checkpoint)
        plan = _checkpoint_plan(checkpoint)
        create_headers = list(_request_headers(plan, 0))
        if _capability_allowed(
            resolved,
            "ReservedMetadataRoundTrip",
            registry=registry,
            execution_mode=execution_mode,
            live_test_interlock=live_test_interlock,
        ):
            create_headers.extend((
                ("x-amz-meta-s3-upload-operation-id", checkpoint["operation_id"]),
                ("x-amz-meta-s3-upload-sha256", checkpoint["source"]["sha256"]),
            ))
        response = _request(
            resolved=resolved,
            method="POST",
            key=plan["object_key"],
            query=(("uploads", ""),),
            headers=tuple(create_headers),
            now=request_moment,
            transport=transport,
            request_builder=request_builder,
        )
        parsed = _response(
            response, operation="CreateMultipartUpload", resolved=resolved
        )
        if parsed.classification == "definitive_failure":
            store.remove(checkpoint["checkpoint_id"])
            raise MultipartError("remote multipart initiation definitively failed")
        if parsed.classification != "success":
            checkpoint = _set_state(
                checkpoint, "initiation_unknown", request_moment
            )
            store.replace(checkpoint)
            return _ambiguous("resume", checkpoint, store)
        checkpoint = _set_state(checkpoint, "initiated", request_moment)
        checkpoint["multipart"]["upload_id"] = parsed.identifiers["upload_id"]
        store.replace(checkpoint)
        return resume_multipart(
            resolved=resolved,
            checkpoint=checkpoint,
            store=store,
            transport=transport,
            project_root=project_root,
            config_home=config_home,
            now=now,
            registry=registry,
            execution_mode=execution_mode,
            live_test_interlock=live_test_interlock,
            allow_insecure_http=allow_insecure_http,
            request_builder=request_builder,
        )
    try:
        verify_resumable_source(
            checkpoint["source"],
            checkpoint["multipart"]["acknowledged_parts"],
            part_size_bytes=part_size,
        )
        with VerifiedSource.open(
            checkpoint["source"]["path"],
            soft_max_bytes=checkpoint["source"]["size"],
        ) as source:
            for part in source.parts(part_size):
                acknowledged = checkpoint["multipart"]["acknowledged_parts"]
                if part.number <= len(acknowledged):
                    expected = acknowledged[part.number - 1]
                    if (
                        expected["size"] != part.size
                        or expected["sha256"] != part.sha256
                    ):
                        raise SourceError("acknowledged part no longer matches source")
                    continue
                in_flight = checkpoint["multipart"]["in_flight_part"]
                if in_flight is not None:
                    if (
                        in_flight["part_number"] != part.number
                        or in_flight["size"] != part.size
                        or in_flight["sha256"] != part.sha256
                    ):
                        raise SourceError("in-flight part no longer matches source")
                    if in_flight["attempt"] >= checkpoint["multipart"]["part_max_attempts"]:
                        return _partial("resume", checkpoint, store)
                    request_moment = _validated_request_moment(resolved, now)
                    checkpoint = copy.deepcopy(checkpoint)
                    checkpoint["multipart"]["in_flight_part"]["attempt"] += 1
                    checkpoint["updated_at"] = _timestamp(request_moment)
                else:
                    request_moment = _validated_request_moment(resolved, now)
                    checkpoint = _set_state(
                        checkpoint, "uploading", request_moment
                    )
                    next_part = part.as_checkpoint()
                    next_part["attempt"] = 1
                    checkpoint["multipart"]["in_flight_part"] = next_part
                store.replace(checkpoint)
                response = _request(
                    resolved=resolved,
                    method="PUT",
                    key=checkpoint["object_reference_draft"]["location"]["key"],
                    query=(
                        ("partNumber", str(part.number)),
                        ("uploadId", checkpoint["multipart"]["upload_id"]),
                    ),
                    body=part.data,
                    headers=(("content-length", str(part.size)),),
                    now=request_moment,
                    transport=transport,
                    request_builder=request_builder,
                )
                parsed = _response(response, operation="UploadPart", resolved=resolved)
                if parsed.classification != "success":
                    if parsed.classification == "session_absent":
                        store.remove(checkpoint["checkpoint_id"])
                        raise MultipartError("multipart session no longer exists")
                    if parsed.classification == "definitive_failure":
                        checkpoint = copy.deepcopy(checkpoint)
                        checkpoint["multipart"]["in_flight_part"] = None
                        store.replace(checkpoint)
                    return _partial("resume", checkpoint, store)
                row = dict(part.as_checkpoint())
                row["etag"] = parsed.identifiers["etag"]
                checkpoint = copy.deepcopy(checkpoint)
                checkpoint["multipart"]["acknowledged_parts"].append(row)
                checkpoint["multipart"]["in_flight_part"] = None
                store.replace(checkpoint)
    except SourceError:
        checkpoint = _set_state(checkpoint, "uploading", moment)
        store.replace(checkpoint)
        return _partial("resume", checkpoint, store)
    return _finish_existing_session(
        resolved=resolved,
        checkpoint=checkpoint,
        store=store,
        transport=transport,
        project_root=project_root,
        config_home=config_home,
        operation="resume",
        request_now=now,
        request_builder=request_builder,
    )


def reconcile_multipart(
    *,
    resolved: ResolvedTarget,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
    transport: Callable[..., Response],
    project_root: str,
    config_home: str,
    now: ClockInput,
    registry: Optional[CapabilityRegistry] = None,
    execution_mode: str = "normal",
    live_test_interlock: Optional[LiveTestInterlock] = None,
    allow_insecure_http: bool = False,
    request_builder: Callable[..., Any] = build_signed_request,
) -> MultipartOutcome:
    _validate_recovery_checkpoint(resolved, checkpoint)
    if resolved.target.endpoint.startswith("http://") and not allow_insecure_http:
        raise MultipartError("HTTP Target requires explicit insecure transport opt-in")
    moment = _moment(now)
    state = checkpoint["state"]
    if state == "initiating":
        checkpoint = _set_state(checkpoint, "initiation_unknown", moment)
        store.replace(checkpoint)
        state = "initiation_unknown"
    elif state == "completing":
        checkpoint = _set_state(checkpoint, "completion_unknown", moment)
        store.replace(checkpoint)
        state = "completion_unknown"
    elif state == "aborting":
        checkpoint = _set_state(checkpoint, "abort_unknown", moment)
        store.replace(checkpoint)
        state = "abort_unknown"
    if state == "prepared":
        checkpoint = _set_state(checkpoint, "not_started", moment)
        store.replace(checkpoint)
        result = build_result(
            "reconcile",
            "not_started",
            object_written=False,
            retention=checkpoint["object_reference_draft"]["retention"],
        )
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], False)
    if state == "not_started":
        result = build_result(
            "reconcile",
            "not_started",
            object_written=False,
            retention=checkpoint["object_reference_draft"]["retention"],
        )
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], False)
    if state == "complete":
        return _complete_result(
            resolved=resolved,
            checkpoint=checkpoint,
            store=store,
            project_root=project_root,
            config_home=config_home,
            operation="reconcile",
            now=now,
        )
    if state == "aborted":
        result = build_result("reconcile", "aborted")
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], False)
    if state == "collision_detected":
        result = build_result(
            "reconcile",
            "collision",
            object_written=False,
            object_reference=checkpoint["object_reference_draft"],
            retention=checkpoint["object_reference_draft"]["retention"],
            checkpoint_id=checkpoint["checkpoint_id"],
        )
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], True)
    if state in {"initiated", "uploading"}:
        return _partial("reconcile", checkpoint, store)
    if state == "initiation_unknown":
        return _ambiguous("reconcile", checkpoint, store)
    if state == "completion_unknown":
        can_observe = (
            _capability_allowed(
                resolved,
                "HeadObject",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
            and _capability_allowed(
                resolved,
                "ReservedMetadataRoundTrip",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
        )
        if not can_observe:
            return _ambiguous("reconcile", checkpoint, store)
        response = _request(
            resolved=resolved,
            method="HEAD",
            key=checkpoint["object_reference_draft"]["location"]["key"],
            now=now,
            transport=transport,
            request_builder=request_builder,
        )
        observation, reference = _head_completion_observation(
            response, checkpoint, resolved
        )
        if observation == "confirmed":
            checkpoint = _set_state(
                checkpoint, "complete", moment,
                object_reference_draft=reference,
            )
            store.replace(checkpoint)
            return _complete_result(
                resolved=resolved,
                checkpoint=checkpoint,
                store=store,
                project_root=project_root,
                config_home=config_home,
                operation="reconcile",
                now=now,
            )
        return _ambiguous("reconcile", checkpoint, store)
    if state == "abort_unknown":
        can_head = (
            _capability_allowed(
                resolved,
                "HeadObject",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
            and _capability_allowed(
                resolved,
                "ReservedMetadataRoundTrip",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
        )
        if not can_head:
            return _ambiguous("reconcile", checkpoint, store)
        head = _request(
            resolved=resolved,
            method="HEAD",
            key=checkpoint["object_reference_draft"]["location"]["key"],
            now=now,
            transport=transport,
            request_builder=request_builder,
        )
        observation, reference = _head_completion_observation(
            head, checkpoint, resolved
        )
        if observation == "confirmed":
            checkpoint = _set_state(
                checkpoint, "complete", moment,
                object_reference_draft=reference,
            )
            checkpoint["multipart"]["return_state"] = None
            store.replace(checkpoint)
            return _complete_result(
                resolved=resolved,
                checkpoint=checkpoint,
                store=store,
                project_root=project_root,
                config_home=config_home,
                operation="reconcile",
                now=now,
            )
        if observation == "inconclusive" or head is None or not (
            200 <= head.status < 300 or head.status == 404
        ):
            return _ambiguous("reconcile", checkpoint, store)
        can_observe_session = (
            _capability_allowed(
                resolved,
                "ListParts",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
            and _capability_allowed(
                resolved,
                "ObserveMultipartSession",
                registry=registry,
                execution_mode=execution_mode,
                live_test_interlock=live_test_interlock,
            )
        )
        if not can_observe_session:
            return _ambiguous("reconcile", checkpoint, store)
        observed = _request(
            resolved=resolved,
            method="GET",
            key=checkpoint["object_reference_draft"]["location"]["key"],
            query=(("uploadId", checkpoint["multipart"]["upload_id"]),),
            now=now,
            transport=transport,
            request_builder=request_builder,
        )
        parsed = _response(
            observed, operation="ListParts", resolved=resolved
        )
        if parsed.classification == "success":
            return_state = checkpoint["multipart"]["return_state"]
            checkpoint = _set_state(checkpoint, return_state, moment)
            checkpoint["multipart"]["return_state"] = None
            store.replace(checkpoint)
            return _partial("reconcile", checkpoint, store)
        if parsed.classification == "session_absent":
            checkpoint = _set_state(checkpoint, "aborted", moment)
            checkpoint["multipart"]["return_state"] = None
            checkpoint["multipart"]["in_flight_part"] = None
            store.replace(checkpoint)
            result = build_result("reconcile", "aborted")
            return MultipartOutcome(
                result, store, checkpoint["checkpoint_id"], False
            )
        return _ambiguous("reconcile", checkpoint, store)
    raise MultipartError("multipart checkpoint state is not reconcilable")


def abort_multipart(
    *,
    resolved: ResolvedTarget,
    checkpoint: Dict[str, Any],
    store: CheckpointStore,
    transport: Callable[..., Response],
    confirm_abort: bool,
    now: ClockInput,
    registry: Optional[CapabilityRegistry] = None,
    execution_mode: str = "normal",
    live_test_interlock: Optional[LiveTestInterlock] = None,
    allow_insecure_http: bool = False,
    request_builder: Callable[..., Any] = build_signed_request,
) -> MultipartOutcome:
    _validate_recovery_checkpoint(resolved, checkpoint)
    if not confirm_abort:
        raise MultipartError("abort requires explicit confirmation")
    moment = _moment(now)
    state = checkpoint["state"]
    if state == "aborting":
        checkpoint = _set_state(checkpoint, "abort_unknown", moment)
        store.replace(checkpoint)
        return _ambiguous("abort", checkpoint, store)
    if state not in {"initiated", "uploading", "collision_detected"}:
        raise MultipartError("multipart checkpoint cannot be explicitly aborted")
    if resolved.target.endpoint.startswith("http://") and not allow_insecure_http:
        raise MultipartError("HTTP Target requires explicit insecure transport opt-in")
    if not _capability_allowed(
        resolved,
        "AbortMultipartUpload",
        registry=registry,
        execution_mode=execution_mode,
        live_test_interlock=live_test_interlock,
    ):
        raise MultipartError("AbortMultipartUpload capability is unavailable")
    request_moment = _validated_request_moment(resolved, now)
    checkpoint = _set_state(checkpoint, "aborting", request_moment)
    checkpoint["multipart"]["return_state"] = state
    store.replace(checkpoint)
    response = _request(
        resolved=resolved,
        method="DELETE",
        key=checkpoint["object_reference_draft"]["location"]["key"],
        query=(("uploadId", checkpoint["multipart"]["upload_id"]),),
        headers=(("content-length", "0"),),
        now=request_moment,
        transport=transport,
        request_builder=request_builder,
    )
    parsed = _response(
        response, operation="AbortMultipartUpload", resolved=resolved
    )
    if parsed.classification == "success":
        checkpoint = _set_state(checkpoint, "aborted", request_moment)
        checkpoint["multipart"]["return_state"] = None
        checkpoint["multipart"]["in_flight_part"] = None
        store.replace(checkpoint)
        result = build_result("abort", "aborted")
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], False)
    if parsed.classification == "session_absent":
        checkpoint = _set_state(checkpoint, "aborted", request_moment)
        checkpoint["multipart"]["return_state"] = None
        checkpoint["multipart"]["in_flight_part"] = None
        store.replace(checkpoint)
        result = build_result("abort", "aborted")
        return MultipartOutcome(result, store, checkpoint["checkpoint_id"], False)
    if parsed.classification == "definitive_failure":
        return_state = checkpoint["multipart"]["return_state"]
        checkpoint = _set_state(checkpoint, return_state, request_moment)
        checkpoint["multipart"]["return_state"] = None
        store.replace(checkpoint)
        return _partial("abort", checkpoint, store)
    checkpoint = _set_state(checkpoint, "abort_unknown", request_moment)
    store.replace(checkpoint)
    return _ambiguous("abort", checkpoint, store)


def execute_multipart(
    *,
    resolved: ResolvedTarget,
    plan: Dict[str, Any],
    transport: Callable[..., Response],
    project_root: str,
    config_home: str,
    now: ClockInput,
    checkpoint_notice: Callable[[str], None],
    source: VerifiedSource,
    registry: Optional[CapabilityRegistry] = None,
    execution_mode: str = "normal",
    live_test_interlock: Optional[LiveTestInterlock] = None,
    allow_insecure_http: bool = False,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    request_builder: Callable[..., Any] = build_signed_request,
) -> MultipartOutcome:
    if (
        not plan.get("executable")
        or plan.get("upload_mode") != "multipart"
        or plan.get("collision", {}).get("policy") not in {"replace", "unique", "reject"}
    ):
        raise MultipartError("multipart execution requires an executable multipart plan")
    collision = plan["collision"]["policy"]
    _validate_initial_plan(resolved, plan)
    _authorize_upload(
        resolved,
        collision,
        registry=registry,
        execution_mode=execution_mode,
        live_test_interlock=live_test_interlock,
        allow_insecure_http=allow_insecure_http,
    )
    target = resolved.target
    part_size = target.limits.part_size_bytes
    if part_size is None:
        raise MultipartError("multipart part size is unavailable")
    moment = _moment(now)
    if (
        source.snapshot.path != plan["source"]["path"]
        or source.snapshot.size != plan["source"]["size"]
    ):
        raise SourceError("planned source descriptor does not match the upload plan")
    with source:
        source.verify_unchanged()
        reference = build_object_reference(
            target_ref=resolved.ref.text,
            target=target,
            key=plan["object_key"],
            version_id=None,
        )
        reference["access"] = {
            "mode": plan["access"]["mode"],
            "public_base_url": plan["access"]["public_base_url"],
            "presign_expires_seconds": plan["access"]["presign_expires_seconds"],
        }
        reference_snapshot = None
        if plan["reference_out"] is not None:
            reference_snapshot = preflight_reference_output(
                plan["reference_out"]["path"],
                project_root=project_root,
                config_home=config_home,
                source_identity=(source.snapshot.device, source.snapshot.inode),
            )
        initiation_moment = _validated_request_moment(resolved, now)
        checkpoint_id = uuid_factory().hex
        operation_id = uuid_factory().hex
        if checkpoint_id == operation_id:
            operation_id = uuid_factory().hex
        checkpoint = {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "kind": "multipart",
            "state": "prepared",
            "operation_id": operation_id,
            "created_at": _timestamp(moment),
            "updated_at": _timestamp(moment),
            "target_ref": resolved.ref.text,
            "target_fingerprint": resolved.target_fingerprint,
            "object_reference_draft": reference,
            "upload_plan": {
                "content_type": plan["headers"]["content_type"],
                "cache_control": plan["headers"]["cache_control"],
                "content_disposition": plan["headers"]["content_disposition"],
                "presign_expires_seconds": plan["access"]["presign_expires_seconds"],
            },
            "collision": {
                "policy": collision,
                "base_key": plan["object_key"],
                "attempt": 1,
                "max_attempts": 1,
            },
            "source": source.snapshot.as_checkpoint(),
            "reference_out": None if reference_snapshot is None else reference_snapshot.value,
            "multipart": {
                "upload_id": None,
                "part_size_bytes": part_size,
                "part_max_attempts": target.retry.part_max_attempts,
                "return_state": None,
                "in_flight_part": None,
                "acknowledged_parts": [],
            },
            "delete_scope": None,
        }
        store = CheckpointStore(project_root)
        store.create(checkpoint)
        checkpoint = _set_state(checkpoint, "initiating", initiation_moment)
        store.replace(checkpoint)
        checkpoint_notice(checkpoint_id)
        create_headers = list(_request_headers(plan, 0))
        if _capability_allowed(
            resolved,
            "ReservedMetadataRoundTrip",
            registry=registry,
            execution_mode=execution_mode,
            live_test_interlock=live_test_interlock,
        ):
            create_headers.extend((
                ("x-amz-meta-s3-upload-operation-id", operation_id),
                ("x-amz-meta-s3-upload-sha256", source.snapshot.sha256),
            ))
        create_response = _request(
            resolved=resolved,
            method="POST",
            key=plan["object_key"],
            query=(("uploads", ""),),
            headers=tuple(create_headers),
            now=initiation_moment,
            transport=transport,
            request_builder=request_builder,
        )
        parsed = _response(
            create_response, operation="CreateMultipartUpload", resolved=resolved
        )
        if parsed.classification != "success":
            if parsed.classification == "definitive_failure":
                store.remove(checkpoint_id)
                raise MultipartError("remote multipart initiation definitively failed")
            checkpoint = _set_state(
                checkpoint, "initiation_unknown", initiation_moment
            )
            store.replace(checkpoint)
            return _ambiguous("upload", checkpoint, store)
        checkpoint = _set_state(checkpoint, "initiated", initiation_moment)
        checkpoint["multipart"]["upload_id"] = parsed.identifiers["upload_id"]
        store.replace(checkpoint)

        try:
            for part in source.parts(part_size):
                checkpoint, progress = _upload_fresh_part(
                    resolved=resolved,
                    plan=plan,
                    checkpoint=checkpoint,
                    part=part,
                    store=store,
                    transport=transport,
                    request_now=now,
                    request_builder=request_builder,
                )
                if progress != "acknowledged":
                    return _partial("upload", checkpoint, store)
        except SourceError:
            checkpoint = _set_state(checkpoint, "uploading", moment)
            store.replace(checkpoint)
            return _partial("upload", checkpoint, store)

    return _finish_existing_session(
        resolved=resolved,
        checkpoint=checkpoint,
        store=store,
        transport=transport,
        project_root=project_root,
        config_home=config_home,
        operation="upload",
        request_now=now,
        request_builder=request_builder,
    )


def _upload_fresh_part(
    *,
    resolved: ResolvedTarget,
    plan: Dict[str, Any],
    checkpoint: Dict[str, Any],
    part: SourcePart,
    store: CheckpointStore,
    transport: Callable[..., Response],
    request_now: ClockInput,
    request_builder: Callable[..., Any],
) -> tuple[Dict[str, Any], str]:
    request_moment = _validated_request_moment(resolved, request_now)
    in_flight = part.as_checkpoint()
    in_flight["attempt"] = 1
    checkpoint = _set_state(checkpoint, "uploading", request_moment)
    checkpoint["multipart"]["in_flight_part"] = in_flight
    store.replace(checkpoint)
    response = _request(
        resolved=resolved,
        method="PUT",
        key=plan["object_key"],
        query=(
            ("partNumber", str(part.number)),
            ("uploadId", checkpoint["multipart"]["upload_id"]),
        ),
        body=part.data,
        headers=(("content-length", str(part.size)),),
        now=request_moment,
        transport=transport,
        request_builder=request_builder,
    )
    parsed = _response(response, operation="UploadPart", resolved=resolved)
    if parsed.classification != "success":
        if parsed.classification == "session_absent":
            store.remove(checkpoint["checkpoint_id"])
            raise MultipartError("multipart session no longer exists")
        if parsed.classification == "definitive_failure":
            checkpoint["multipart"]["in_flight_part"] = None
            store.replace(checkpoint)
            return checkpoint, "definitive_failure"
        return checkpoint, "unknown"
    acknowledged = dict(part.as_checkpoint())
    acknowledged["etag"] = parsed.identifiers["etag"]
    checkpoint["multipart"]["acknowledged_parts"].append(acknowledged)
    checkpoint["multipart"]["in_flight_part"] = None
    store.replace(checkpoint)
    return checkpoint, "acknowledged"
