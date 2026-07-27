from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from typing import Any, Callable, Dict, Optional, Union
from urllib.error import HTTPError, URLError

from artifacts import (
    ArtifactError, CheckpointStore, ReferenceOutputSnapshot, build_object_reference,
    preflight_reference_output, write_reference_output,
)
from body_verifier import VerificationError, verify_body
from capabilities import EXECUTABLE_CAPABILITY_STATES
from config import Connection
from planning import (
    derive_contract_key, provider_candidate_for_target, registry_for_target,
)
from provider_candidates import build_candidate_put_request
from response_parser import parse_operation_response
from resolver import ResolvedTarget
from results import build_result, validate_result
from s3 import (
    Response, TransportError, build_signed_request, open_body_stream,
    parse_provider_identifier, presign_get, public_url,
)
from source_file import SourceError, VerifiedSource


class OperationError(RuntimeError):
    pass


class DefinitiveNoWrite(OperationError):
    pass


ClockInput = Optional[Union[datetime, Callable[[], datetime]]]


@dataclass
class OperationOutcome:
    result: Dict[str, Any]
    store: CheckpointStore
    checkpoint_id: str
    retain_checkpoint: bool

    def finalize(self) -> None:
        if not self.retain_checkpoint:
            try:
                self.store.remove(self.checkpoint_id)
            except (ArtifactError, FileNotFoundError):
                pass


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(now: ClockInput) -> datetime:
    value = now() if callable(now) else now
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _signed_moment(resolved: ResolvedTarget, now: ClockInput) -> datetime:
    moment = _moment(now)
    credential = resolved.credential
    if credential is None:
        raise OperationError("Credential Profile is unavailable")
    remaining = credential.remaining_seconds(moment)
    if remaining is not None and remaining <= 60:
        raise OperationError(
            "temporary Credential Profile must have more than 60 whole seconds remaining"
        )
    return moment


def connection_for(resolved: ResolvedTarget) -> Connection:
    credential = resolved.credential
    if credential is None:
        raise OperationError("Credential Profile is unavailable")
    target = resolved.target
    return Connection(
        access_key_id=credential.access_key_id,
        secret_access_key=credential.secret_access_key,
        session_token=credential.session_token,
        bucket=target.bucket,
        endpoint=target.endpoint,
        region=target.region,
        provider=target.provider,
        addressing=target.addressing,
        public_base_url=target.access.public_base_url or "",
        prefix=target.prefix,
        max_bytes=target.limits.soft_max_bytes,
        presign_expires=target.access.presign_expires_seconds or 3600,
    )


def _unknown_status(status: int) -> bool:
    return 300 <= status < 400 or status in {408, 425, 429} or status >= 500


def _set_state(checkpoint: Dict[str, Any], state: str, moment: datetime,
               **changes: Any) -> Dict[str, Any]:
    updated = dict(checkpoint)
    updated["state"] = state
    updated["updated_at"] = _timestamp(moment)
    updated.update(changes)
    return updated


def _unique_key(base_key: str, token: str) -> str:
    directory, separator, basename = base_key.rpartition("/")
    suffix_at = basename.rfind(".")
    if suffix_at > 0:
        basename = basename[:suffix_at] + "-" + token + basename[suffix_at:]
    else:
        basename = basename + "-" + token
    return directory + separator + basename


def _new_uuid(factory: Callable[[], uuid.UUID], *excluded: str) -> str:
    for _ in range(8):
        value = factory().hex
        if value not in excluded:
            return value
    raise OperationError("could not allocate an independent operation identifier")


# The verification GET must carry a positive signed lifetime even when the
# Target never presigns caller-facing URLs (public access mode leaves
# presign_expires_seconds null). It only has to outlive one full-body read.
ADOPTION_PRESIGN_SECONDS = 300


def _adopt_planned_content(*, resolved: ResolvedTarget, connection: Connection,
                           registry: Any, contract_key: Any,
                           checkpoint: Dict[str, Any], now: ClockInput,
                           open_stream: Callable[..., Any]) -> bool:
    """Prove the colliding remote object already is the planned content.

    One presigned full-body GET, folded through body_verifier.verify_body
    against the checkpointed source size and SHA-256. True is only returned
    on a double match; anything else -- different bytes, different length, a
    body that could not be read in full, or a signing/credential problem --
    returns False and leaves the caller with the conservative collision.
    HEAD and ETag never participate.
    """
    # URL construction, parameter derivation and the verifying read share one
    # try: a failure anywhere in the three is the same non-answer.
    # VerificationError carries verify_body's own wrapped read failures
    # (http.client.HTTPException included -- IncompleteRead descends from it
    # and from neither OSError nor ValueError), and _signed_moment raises
    # OperationError. The raw transport families are named defensively
    # alongside them, covering any raise from this function's own segments
    # outside verify_body's wrapping.
    try:
        if registry.lookup(contract_key, "PresignGetObject").state \
                not in EXECUTABLE_CAPABILITY_STATES:
            return False
        moment = _signed_moment(resolved, now)
        requested = checkpoint["upload_plan"]["presign_expires_seconds"]
        if not isinstance(requested, int) or isinstance(requested, bool) \
                or requested < 1:
            requested = ADOPTION_PRESIGN_SECONDS
        effective = resolved.presign_effective_seconds(requested, moment)
        if not isinstance(effective, int) or isinstance(effective, bool) \
                or effective < 1:
            return False
        url = presign_get(
            connection,
            checkpoint["object_reference_draft"]["location"]["key"],
            effective,
            moment,
        )
        verify_body(
            open_stream=open_stream,
            url=url,
            headers={},
            channel="authenticated_full_get",
            url_scope="current-key",
            expected_size=checkpoint["source"]["size"],
            expected_sha256=checkpoint["source"]["sha256"],
            now=moment,
        )
        return True
    except (VerificationError, OperationError, HTTPError, URLError,
            TransportError, HTTPException, OSError, ValueError):
        return False


def execute_single_put(*, resolved: ResolvedTarget, plan: Dict[str, Any],
                       transport: Callable[..., Response], project_root: str,
                       config_home: str, now: ClockInput,
                       checkpoint_notice: Callable[[str], None],
                       source: VerifiedSource,
                       checkpoint_created: Optional[Callable[[str], None]] = None,
                       retain_definitive_checkpoint: bool = False,
                       uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
                       open_stream: Optional[Callable[..., Any]] = None) -> OperationOutcome:
    if not plan["executable"] or plan["upload_mode"] != "single-put":
        raise OperationError("single Put execution requires an executable single-Put plan")
    moment = _moment(now)
    target = resolved.target
    connection = connection_for(resolved)
    collision = plan["collision"]
    policy = collision["policy"]
    maximum = collision["max_attempts"]
    if policy not in {"replace", "unique", "reject"}:
        raise OperationError("unsupported single Put collision policy")
    if policy != "unique" and maximum != 1:
        raise OperationError("non-unique single Put must use one collision attempt")
    if (
        source.snapshot.path != plan["source"]["path"]
        or source.snapshot.size != plan["source"]["size"]
    ):
        raise SourceError("planned source descriptor does not match the upload plan")
    with source:
        body = source.single_put_bytes()
        checkpoint_id = _new_uuid(uuid_factory)
        operation_id = _new_uuid(uuid_factory, checkpoint_id)
        actual_key = (
            _unique_key(plan["object_key"], _new_uuid(uuid_factory, checkpoint_id, operation_id))
            if policy == "unique"
            else plan["object_key"]
        )
        reference = build_object_reference(
            target_ref=resolved.ref.text,
            target=target,
            key=actual_key,
            version_id=None,
        )
        reference_snapshot = None
        if plan["reference_out"] is not None:
            reference_snapshot = preflight_reference_output(
                plan["reference_out"]["path"],
                project_root=project_root,
                config_home=config_home,
                source_identity=(source.snapshot.device, source.snapshot.inode),
            )
        request_moment = _signed_moment(resolved, now)
        checkpoint = {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "kind": "put",
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
                "policy": policy,
                "base_key": plan["object_key"],
                "attempt": 1,
                "max_attempts": maximum,
            },
            "source": source.snapshot.as_checkpoint(),
            "reference_out": None if reference_snapshot is None else reference_snapshot.value,
            "multipart": None,
            "delete_scope": None,
        }
        store = CheckpointStore(project_root)
        store.create(checkpoint)
        if checkpoint_created is not None:
            checkpoint_created(checkpoint_id)
        active_credentials = (
            connection.access_key_id,
            connection.secret_access_key,
            connection.session_token,
        )
        contract_key = derive_contract_key(target)
        registry = registry_for_target(target, contract_key)
        metadata_enabled = (
            registry.lookup(contract_key, "ReservedMetadataRoundTrip").state == "enabled"
        )
        notice_sent = False
        version = None
        adopted = False
        while True:
            checkpoint = _set_state(checkpoint, "put_in_flight", request_moment)
            store.replace(checkpoint)
            if not notice_sent:
                checkpoint_notice(checkpoint_id)
                notice_sent = True
            request_headers = [
                ("content-length", str(len(body))),
                ("content-type", checkpoint["upload_plan"]["content_type"]),
            ]
            if checkpoint["upload_plan"]["cache_control"] is not None:
                request_headers.append(("cache-control", checkpoint["upload_plan"]["cache_control"]))
            if checkpoint["upload_plan"]["content_disposition"] is not None:
                request_headers.append(("content-disposition", checkpoint["upload_plan"]["content_disposition"]))
            if metadata_enabled:
                request_headers.extend(
                    (
                        ("x-amz-meta-s3-upload-sha256", checkpoint["source"]["sha256"]),
                        ("x-amz-meta-s3-upload-operation-id", checkpoint["operation_id"]),
                    )
                )
            if policy != "replace":
                request_headers.append(("if-none-match", "*"))
            actual_key = checkpoint["object_reference_draft"]["location"]["key"]
            candidate = provider_candidate_for_target(target)
            if candidate is None:
                signed = build_signed_request(
                    connection,
                    method="PUT",
                    key=actual_key,
                    body=body,
                    headers=tuple(request_headers),
                    now=request_moment,
                )
            else:
                signed = build_candidate_put_request(
                    candidate,
                    connection,
                    key=actual_key,
                    body=body,
                    content_type=checkpoint["upload_plan"]["content_type"],
                    extra_headers=tuple(request_headers[2:]),
                    now=request_moment,
                )
            try:
                response = transport(signed.method, signed.url, signed.headers, signed.body)
            except Exception:
                response = None
            parsed = None if response is None else parse_operation_response(
                response,
                operation="PutObject",
                active_credentials=active_credentials,
                conditional=policy != "replace",
            )
            if parsed is None or parsed.classification in {"unknown", "identifier_rejected"}:
                checkpoint = _set_state(checkpoint, "put_unknown", moment)
                store.replace(checkpoint)
                result = build_result(
                    "upload", "ambiguous", object_written=None,
                    retention=target.retention.result(), checkpoint_id=checkpoint_id,
                )
                return OperationOutcome(result, store, checkpoint_id, True)
            if parsed.classification == "precondition":
                attempt = checkpoint["collision"]["attempt"]
                if policy == "reject":
                    # The key is taken. Before calling that a collision, one
                    # presigned full-body read may prove the object already
                    # is the planned content; only that proof turns the
                    # outcome into adoption, and no second Put is ever sent.
                    if _adopt_planned_content(
                        resolved=resolved,
                        connection=connection,
                        registry=registry,
                        contract_key=contract_key,
                        checkpoint=checkpoint,
                        now=now,
                        open_stream=(
                            open_body_stream if open_stream is None else open_stream
                        ),
                    ):
                        adopted = True
                        version = None
                        break
                if policy != "unique" or attempt >= maximum:
                    result = build_result(
                        "upload", "collision", object_written=False,
                        object_reference=checkpoint["object_reference_draft"],
                        retention=target.retention.result(),
                    )
                    return OperationOutcome(result, store, checkpoint_id, False)
                operation_id = _new_uuid(uuid_factory, checkpoint_id, checkpoint["operation_id"])
                token = _new_uuid(uuid_factory, checkpoint_id, operation_id)
                reference = build_object_reference(
                    target_ref=resolved.ref.text,
                    target=target,
                    key=_unique_key(plan["object_key"], token),
                    version_id=None,
                )
                updated_collision = dict(checkpoint["collision"])
                updated_collision["attempt"] = attempt + 1
                checkpoint = _set_state(
                    checkpoint,
                    "prepared",
                    request_moment,
                    operation_id=operation_id,
                    object_reference_draft=reference,
                    collision=updated_collision,
                )
                store.replace(checkpoint)
                request_moment = _signed_moment(resolved, now)
                continue
            if parsed.classification != "success":
                if retain_definitive_checkpoint:
                    try:
                        store.replace(_set_state(checkpoint, "not_started", moment))
                    except (ArtifactError, OSError):
                        pass
                else:
                    try:
                        store.remove(checkpoint_id)
                    except ArtifactError:
                        pass
                raise DefinitiveNoWrite(
                    "remote Put returned a definitive no-write response"
                )
            version = parsed.identifiers.get("version_id")
            break

    reference = build_object_reference(
        target_ref=resolved.ref.text,
        target=target,
        key=checkpoint["object_reference_draft"]["location"]["key"],
        version_id=version,
        credentials=active_credentials,
    )
    checkpoint = _set_state(
        checkpoint, "complete", request_moment, object_reference_draft=reference
    )
    store.replace(checkpoint)
    # The remote identity in every terminal upload result is the planned
    # content the checkpoint recorded: written by this Put, or proven equal
    # by the adoption read.
    remote = {
        "key": reference["location"]["key"],
        "size": checkpoint["source"]["size"],
        "sha256": checkpoint["source"]["sha256"],
    }
    try:
        if target.access.mode == "public":
            url = public_url(
                target.access.public_base_url or "", reference["location"]["key"]
            )
            url_kind = "public"
            expires_at = None
        else:
            presign_moment = _signed_moment(resolved, now)
            requested = checkpoint["upload_plan"]["presign_expires_seconds"]
            effective = resolved.presign_effective_seconds(
                requested, presign_moment
            )
            if not isinstance(effective, int) or effective <= 0:
                raise OperationError("presigned URL duration is unavailable")
            url = presign_get(
                connection, reference["location"]["key"], effective, presign_moment
            )
            url_kind = "presigned"
            expires_at = _timestamp(
                presign_moment + timedelta(seconds=effective)
            )
    except Exception:
        result = build_result(
            "upload", "partial_success", object_written=not adopted,
            object_reference=reference, url_kind=("public" if target.access.mode == "public" else "presigned"),
            retention=target.retention.result(),
            checkpoint_id=checkpoint_id, remote=remote,
        )
        return OperationOutcome(result, store, checkpoint_id, True)
    if reference_snapshot is not None:
        try:
            write_reference_output(reference_snapshot, reference)
        except ArtifactError:
            result = build_result(
                "upload", "partial_success", object_written=not adopted,
                object_reference=reference, url=url, url_kind=url_kind,
                expires_at=expires_at, retention=target.retention.result(),
                checkpoint_id=checkpoint_id, remote=remote,
            )
            return OperationOutcome(result, store, checkpoint_id, True)
    result = build_result(
        "upload", "adopted" if adopted else "ok",
        object_written=not adopted, object_reference=reference,
        url=url, url_kind=url_kind, expires_at=expires_at,
        retention=target.retention.result(), remote=remote,
    )
    return OperationOutcome(result, store, checkpoint_id, False)


def generate_object_url(*, resolved: ResolvedTarget, reference: Dict[str, Any],
                        presign_expires: Optional[int], now: ClockInput) -> Dict[str, Any]:
    if reference["target_fingerprint"] != resolved.target_fingerprint:
        raise OperationError("Object Reference Target fingerprint does not match the selected Target")
    if resolved.credential is None:
        raise OperationError("Credential Profile is unavailable")
    contract_key = derive_contract_key(resolved.target)
    registry = registry_for_target(resolved.target, contract_key)
    access = reference["access"]
    if access["mode"] == "public":
        if presign_expires is not None:
            raise OperationError("--presign-expires is unavailable for a public Object Reference")
        url = public_url(access["public_base_url"], reference["location"]["key"])
        url_kind = "public"
        expires_at = None
    else:
        presign_capability = registry.lookup(contract_key, "PresignGetObject")
        if presign_capability.state not in EXECUTABLE_CAPABILITY_STATES:
            raise OperationError("PresignGetObject capability is unavailable")
        moment = _signed_moment(resolved, now)
        requested = access["presign_expires_seconds"] if presign_expires is None else presign_expires
        if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 604800:
            raise OperationError("presign expiry must be from 1 to 604800 seconds")
        effective = resolved.presign_effective_seconds(requested, moment)
        if effective is None or effective <= 0:
            raise OperationError("Credential Profile cannot sign a positive URL lifetime")
        url = presign_get(connection_for(resolved), reference["location"]["key"], effective, moment)
        url_kind = "presigned"
        expires_at = _timestamp(moment + timedelta(seconds=effective))
    return build_result(
        "url", "ok", object_reference=reference, url=url, url_kind=url_kind,
        expires_at=expires_at, retention=reference["retention"],
    )


def _delete_terminal_result(operation: str, status: str, checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    reference = checkpoint["object_reference_draft"]
    scope = checkpoint["delete_scope"]
    return build_result(
        operation,
        status,
        object_reference=reference,
        retention=reference["retention"],
        delete_scope=scope,
        deleted_version_id=(
            reference["location"]["version_id"]
            if status == "deleted" and scope == "exact-version"
            else None
        ),
    )


def execute_delete(*, resolved: ResolvedTarget, reference: Dict[str, Any],
                   plan: Dict[str, Any], transport: Callable[..., Response],
                   project_root: str, now: ClockInput,
                   checkpoint_notice: Callable[[str], None],
                   uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> OperationOutcome:
    if not plan["executable"] or plan["delete_scope"] not in {"current-key", "exact-version"}:
        raise OperationError("Delete execution requires an executable scoped plan")
    if reference["target_fingerprint"] != resolved.target_fingerprint:
        raise OperationError("Object Reference Target fingerprint does not match the selected Target")
    moment = _moment(now)
    checkpoint_id = _new_uuid(uuid_factory)
    operation_id = _new_uuid(uuid_factory, checkpoint_id)
    checkpoint = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "kind": "delete",
        "state": "prepared",
        "operation_id": operation_id,
        "created_at": _timestamp(moment),
        "updated_at": _timestamp(moment),
        "target_ref": reference["target_ref"],
        "target_fingerprint": resolved.target_fingerprint,
        "object_reference_draft": reference,
        "upload_plan": None,
        "collision": None,
        "source": None,
        "reference_out": None,
        "multipart": None,
        "delete_scope": plan["delete_scope"],
    }
    request_moment = _signed_moment(resolved, now)
    store = CheckpointStore(project_root)
    store.create(checkpoint)
    checkpoint = _set_state(checkpoint, "delete_in_flight", request_moment)
    store.replace(checkpoint)
    checkpoint_notice(checkpoint_id)
    query = ()
    parser_operation = "DeleteObjectCurrentKey"
    if checkpoint["delete_scope"] == "exact-version":
        query = (("versionId", reference["location"]["version_id"]),)
        parser_operation = "DeleteObjectVersion"
    connection = connection_for(resolved)
    signed = build_signed_request(
        connection,
        method="DELETE",
        key=reference["location"]["key"],
        query=query,
        now=request_moment,
    )
    try:
        response = transport(signed.method, signed.url, signed.headers, signed.body)
    except Exception:
        response = None
    active_credentials = (
        connection.access_key_id,
        connection.secret_access_key,
        connection.session_token,
    )
    parsed = None if response is None else parse_operation_response(
        response,
        operation=parser_operation,
        active_credentials=active_credentials,
    )
    if parsed is None or parsed.classification in {"unknown", "identifier_rejected"}:
        checkpoint = _set_state(checkpoint, "delete_unknown", request_moment)
        store.replace(checkpoint)
        result = build_result(
            "delete",
            "ambiguous",
            retention=reference["retention"],
            checkpoint_id=checkpoint_id,
        )
        return OperationOutcome(result, store, checkpoint_id, True)
    if parsed.classification != "success":
        try:
            store.remove(checkpoint_id)
        except ArtifactError:
            pass
        raise OperationError("remote Delete returned a definitive no-delete response")
    checkpoint = _set_state(checkpoint, "deleted", request_moment)
    store.replace(checkpoint)
    return OperationOutcome(
        _delete_terminal_result("delete", "deleted", checkpoint),
        store,
        checkpoint_id,
        False,
    )


def reconcile_delete(*, resolved: ResolvedTarget, checkpoint: Dict[str, Any],
                     store: CheckpointStore, transport: Callable[..., Response],
                     now: ClockInput) -> OperationOutcome:
    moment = _moment(now)
    checkpoint_id = checkpoint["checkpoint_id"]
    state = checkpoint["state"]
    reference = checkpoint["object_reference_draft"]
    scope = checkpoint["delete_scope"]
    if state == "prepared":
        checkpoint = _set_state(checkpoint, "not_started", moment)
        store.replace(checkpoint)
        return OperationOutcome(
            _delete_terminal_result("reconcile", "not_started", checkpoint),
            store,
            checkpoint_id,
            False,
        )
    if state == "delete_in_flight":
        checkpoint = _set_state(checkpoint, "delete_unknown", moment)
        store.replace(checkpoint)
        state = "delete_unknown"
    if state == "delete_unknown":
        contract_key = derive_contract_key(resolved.target)
        registry = registry_for_target(resolved.target, contract_key)
        observer_name = (
            "ObserveDeleteVersion" if scope == "exact-version" else "ObserveDeleteCurrentKey"
        )
        if registry.lookup(contract_key, observer_name).state != "enabled":
            result = build_result(
                "reconcile",
                "ambiguous",
                retention=reference["retention"],
                checkpoint_id=checkpoint_id,
            )
            return OperationOutcome(result, store, checkpoint_id, True)
        query = ()
        if scope == "exact-version":
            query = (("versionId", reference["location"]["version_id"]),)
        request_moment = _signed_moment(resolved, now)
        signed = build_signed_request(
            connection_for(resolved),
            method="HEAD",
            key=reference["location"]["key"],
            query=query,
            now=request_moment,
        )
        try:
            response = transport(signed.method, signed.url, signed.headers, signed.body)
        except Exception:
            response = None
        if response is not None and response.status in {404, 410}:
            checkpoint = _set_state(checkpoint, "deleted", request_moment)
            store.replace(checkpoint)
            state = "deleted"
        elif response is not None and 200 <= response.status < 300:
            checkpoint = _set_state(checkpoint, "not_deleted", request_moment)
            store.replace(checkpoint)
            state = "not_deleted"
        else:
            result = build_result(
                "reconcile",
                "ambiguous",
                retention=reference["retention"],
                checkpoint_id=checkpoint_id,
            )
            return OperationOutcome(result, store, checkpoint_id, True)
    if state in {"deleted", "not_deleted", "not_started"}:
        return OperationOutcome(
            _delete_terminal_result("reconcile", state, checkpoint),
            store,
            checkpoint_id,
            False,
        )
    raise OperationError("checkpoint state is not reconcilable as a Delete")


class _RemoteObjectAbsent(Exception):
    """The verifying GET answered 404: the object definitively is not there.

    Deliberately a plain Exception subclass: verify_body names HTTPError,
    URLError, TransportError, HTTPException, OSError and ValueError as the
    families it folds into verification_incomplete, and this signal must
    escape that folding to reach reconcile_put as its own answer.
    """


def _absence_signalling(open_stream: Callable[..., Any]) -> Callable[..., Any]:
    def opened(method, url, headers, timeout=30):
        try:
            return open_stream(method, url, headers, timeout=timeout)
        except HTTPError as exc:
            if exc.code == 404:
                raise _RemoteObjectAbsent() from exc
            raise
    return opened


def _reconcile_full_read(*, resolved: ResolvedTarget, connection: Connection,
                         checkpoint: Dict[str, Any], moment: datetime,
                         open_stream: Callable[..., Any]) -> str:
    """Settle a put_unknown checkpoint with one presigned full-body GET.

    Returns "verified" only when the remote bytes match the checkpointed
    source size and SHA-256 in full, "absent" only on a definitive 404, and
    "unknown" for everything else. Same shape as _adopt_planned_content:
    URL parameter derivation, presigning and the verifying read share one
    try, and the except names every family the three can raise --
    HTTPException included, because http.client.IncompleteRead descends
    from it and from neither OSError nor ValueError.
    """
    try:
        requested = checkpoint["upload_plan"]["presign_expires_seconds"]
        if not isinstance(requested, int) or isinstance(requested, bool) \
                or requested < 1:
            requested = ADOPTION_PRESIGN_SECONDS
        effective = resolved.presign_effective_seconds(requested, moment)
        if not isinstance(effective, int) or isinstance(effective, bool) \
                or effective < 1:
            return "unknown"
        url = presign_get(
            connection,
            checkpoint["object_reference_draft"]["location"]["key"],
            effective,
            moment,
        )
        verify_body(
            open_stream=_absence_signalling(open_stream),
            url=url,
            headers={},
            channel="authenticated_full_get",
            url_scope="current-key",
            expected_size=checkpoint["source"]["size"],
            expected_sha256=checkpoint["source"]["sha256"],
            now=moment,
        )
        return "verified"
    except _RemoteObjectAbsent:
        return "absent"
    except (VerificationError, OperationError, HTTPError, URLError,
            TransportError, HTTPException, OSError, ValueError):
        return "unknown"


def reconcile_put(*, resolved: ResolvedTarget, checkpoint: Dict[str, Any],
                  store: CheckpointStore, transport: Callable[..., Response],
                  config_home: str, now: ClockInput,
                  open_stream: Optional[Callable[..., Any]] = None) -> OperationOutcome:
    moment = _moment(now)
    checkpoint_id = checkpoint["checkpoint_id"]
    reference = checkpoint["object_reference_draft"]
    retention = reference["retention"]
    state = checkpoint["state"]
    # A checkpoint that arrives already complete was proven written by the
    # Put's own 2xx; a checkpoint converged below by the verifying read
    # proves presence and content but claims no writer identity, so its
    # results carry object_written=null throughout.
    written_claim: Optional[bool] = True
    if state == "prepared":
        checkpoint = _set_state(checkpoint, "not_started", moment)
        store.replace(checkpoint)
        result = build_result(
            "reconcile", "not_started", object_written=False, retention=retention,
        )
        return OperationOutcome(result, store, checkpoint_id, False)
    if state == "put_in_flight":
        checkpoint = _set_state(checkpoint, "put_unknown", moment)
        store.replace(checkpoint)
        state = "put_unknown"
    if state == "put_unknown":
        # Read-only reconciliation: one presigned GET, zero write requests.
        # The transport that carries mutations is never invoked on this
        # branch.
        contract_key = derive_contract_key(resolved.target)
        registry = registry_for_target(resolved.target, contract_key)
        if registry.lookup(contract_key, "PresignGetObject").state \
                not in EXECUTABLE_CAPABILITY_STATES:
            result = build_result(
                "reconcile", "ambiguous", object_written=None,
                retention=retention, checkpoint_id=checkpoint_id,
            )
            return OperationOutcome(result, store, checkpoint_id, True)
        request_moment = _signed_moment(resolved, now)
        read = _reconcile_full_read(
            resolved=resolved,
            connection=connection_for(resolved),
            checkpoint=checkpoint,
            moment=request_moment,
            open_stream=open_body_stream if open_stream is None else open_stream,
        )
        if read == "verified":
            checkpoint = _set_state(checkpoint, "complete", request_moment)
            store.replace(checkpoint)
            state = "complete"
            written_claim = None
        elif read == "absent":
            checkpoint = _set_state(checkpoint, "not_started", request_moment)
            store.replace(checkpoint)
            state = "not_started"
        if state == "put_unknown":
            result = build_result(
                "reconcile", "ambiguous", object_written=None,
                retention=retention, checkpoint_id=checkpoint_id,
            )
            return OperationOutcome(result, store, checkpoint_id, True)
    if state == "complete":
        source = checkpoint["source"]
        remote = {
            "key": reference["location"]["key"],
            "size": source["size"],
            "sha256": source["sha256"],
        }
        try:
            generated = generate_object_url(
                resolved=resolved, reference=checkpoint["object_reference_draft"],
                presign_expires=None, now=now,
            )
        except Exception:
            result = build_result(
                "reconcile",
                "partial_success",
                object_written=written_claim,
                object_reference=checkpoint["object_reference_draft"],
                url_kind=(
                    "public"
                    if checkpoint["object_reference_draft"]["access"]["mode"] == "public"
                    else "presigned"
                ),
                retention=retention,
                remote=remote,
            )
            return OperationOutcome(result, store, checkpoint_id, False)
        generated["operation"] = "reconcile"
        generated["remote"] = remote
        validate_result(generated, validate_reference=False)
        if checkpoint["reference_out"] is not None:
            source = checkpoint["source"]
            identity = (
                None if source["device"] is None else int(source["device"]),
                None if source["inode"] is None else int(source["inode"]),
            )
            snapshot = ReferenceOutputSnapshot(
                checkpoint["reference_out"],
                store.project_root,
                config_home,
                identity,
            )
            try:
                write_reference_output(snapshot, checkpoint["object_reference_draft"])
            except ArtifactError:
                result = build_result(
                    "reconcile",
                    "partial_success",
                    object_written=written_claim,
                    object_reference=generated["object_reference"],
                    url=generated["url"],
                    url_kind=generated["url_kind"],
                    expires_at=generated["expires_at"],
                    retention=retention,
                    checkpoint_id=checkpoint_id,
                    remote=remote,
                )
                return OperationOutcome(result, store, checkpoint_id, True)
        return OperationOutcome(generated, store, checkpoint_id, False)
    if state == "not_started":
        result = build_result(
            "reconcile", "not_started", object_written=False, retention=retention,
        )
        return OperationOutcome(result, store, checkpoint_id, False)
    raise OperationError("checkpoint state is not reconcilable as a Put")
