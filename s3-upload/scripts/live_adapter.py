from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from config import Connection
from evidence import (
    CleanupObservation,
    EvidenceObservation,
    EvidenceOperationContext,
    RequestObservation,
    TrackedObject,
    TrackedSession,
)
from provider_candidates import ProviderCandidate, build_candidate_request
from response_parser import parse_operation_response
from s3 import Response, presign_get


class LiveAdapterError(RuntimeError):
    pass


@dataclass
class S3EvidenceAdapter:
    candidate: ProviderCandidate
    connection: Connection = field(repr=False)
    transport: object = field(repr=False)
    _core_key: Optional[str] = field(default=None, init=False)
    _core_deleted: bool = field(default=False, init=False)
    _multipart_key: Optional[str] = field(default=None, init=False)
    _multipart_upload_id: Optional[str] = field(default=None, init=False, repr=False)
    _multipart_etag: Optional[str] = field(default=None, init=False, repr=False)
    _aborted_key: Optional[str] = field(default=None, init=False)
    _aborted_upload_id: Optional[str] = field(default=None, init=False, repr=False)
    _metadata_key: Optional[str] = field(default=None, init=False)
    _metadata_operation_id: Optional[str] = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.connection.provider != self.candidate.provider:
            raise LiveAdapterError("candidate and connection provider do not match")
        if not callable(self.transport):
            raise LiveAdapterError("live adapter transport must be callable")

    @property
    def _credentials(self) -> Tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.connection.access_key_id,
                self.connection.secret_access_key,
                self.connection.session_token,
            )
            if value
        )

    def _signed(
        self,
        *,
        method: str,
        key: str,
        context: EvidenceOperationContext,
        query: Sequence[Tuple[str, str]] = (),
        body: bytes = b"",
        headers: Sequence[Tuple[str, str]] = (),
    ) -> Tuple[Response, RequestObservation]:
        """Send exactly one physical request and return it with its observation.

        **Deliberate deviation from ADR 0006 rules 2/3 (recorded per rule 6.)**
        Nothing in this adapter retries a transient network failure, and it must
        not start: this is the live evidence harness, and its output is a claim
        about what the provider actually did. `evidence.py` records
        `request_count` per logical operation and validates it strictly —
        `EvidenceObservation` rejects a report whose `request_count` disagrees
        with the number of recorded requests, and a GET-family operation whose
        `request_count != 1` is failed outright with `get_request_count_mismatch`.
        A retry would turn one logical call into two or three physical requests
        and silently invalidate that evidence: the report would claim a provider
        answered a single request when it in fact answered several, and a
        transient failure that the harness is supposed to *report* would be
        papered over instead.

        The exemption was reviewed and granted on 2026-08-18 (see
        `.scratch/_workspace/skill-conventions-compliance-sweep/audit-2026-08-18.md`,
        user decision 4④). It applies only to this adapter and to `evidence.py`;
        the ordinary operation paths (`operations.py`, `multipart.py`) do retry
        their read-semantics calls.
        """
        request = build_candidate_request(
            self.candidate,
            self.connection,
            method=method,
            key=key,
            query=query,
            body=body,
            headers=headers,
            now=context.now,
        )
        response = self.transport(
            request.method, request.url, request.headers, request.body
        )
        if not isinstance(response, Response):
            raise LiveAdapterError("transport returned an invalid response")
        observation = RequestObservation(
            method=request.method,
            url=request.url,
            header_names=tuple(sorted(request.headers)),
            body_size=len(request.body),
            authorization_mode="authorization-header",
        )
        return response, observation

    def _presigned(
        self, *, key: str, context: EvidenceOperationContext
    ) -> Tuple[Response, RequestObservation]:
        if not 1 <= context.presign_expires_seconds <= 604800:
            raise LiveAdapterError("live presign expiry is unavailable")
        url = presign_get(
            self.connection,
            key,
            context.presign_expires_seconds,
            context.now,
        )
        response = self.transport("GET", url, {}, b"")
        if not isinstance(response, Response):
            raise LiveAdapterError("transport returned an invalid response")
        return response, RequestObservation(
            method="GET",
            url=url,
            header_names=(),
            body_size=0,
            authorization_mode="presigned-query",
        )

    @staticmethod
    def _raw(response: Response) -> bytes:
        lines = [f"status:{response.status}".encode("ascii")]
        for name, value in sorted((response.headers or {}).items()):
            lines.append(name.encode("ascii", "replace") + b":" + value.encode("utf-8", "replace"))
        return b"\n".join(lines) + b"\n\n" + response.body

    @classmethod
    def _observation(
        cls,
        *,
        passed: bool,
        responses: Sequence[Response],
        requests: Sequence[RequestObservation],
        response_body: Optional[bytes] = None,
        objects: Sequence[TrackedObject] = (),
        sessions: Sequence[TrackedSession] = (),
    ) -> EvidenceObservation:
        final = responses[-1] if responses else None
        return EvidenceObservation(
            passed=passed,
            request_count=len(requests),
            requests=tuple(requests),
            response_status=None if final is None else final.status,
            response_headers=tuple((final.headers or {}).items()) if final else (),
            response_body=(final.body if response_body is None and final else response_body or b""),
            raw_response=b"".join(cls._raw(response) for response in responses),
            response_proved_nonsecret=False,
            redirect_followup_count=0,
            created_objects=tuple(objects),
            created_sessions=tuple(sessions),
        )

    def execute(
        self, operation: str, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if (
            context.provider != self.candidate.provider
            or context.contract_key != self.candidate.contract_key
            or context.exact_endpoint != self.candidate.service_endpoint
        ):
            raise LiveAdapterError("operation context does not match the candidate")
        method = getattr(self, "_execute_" + operation, None)
        if method is None:
            return self._observation(passed=False, responses=(), requests=())
        return method(context)

    def _execute_PutObject(self, context: EvidenceOperationContext) -> EvidenceObservation:
        key = context.run_prefix + "core-object.bin"
        operation_id = uuid.uuid4().hex
        checkpoint_id = uuid.uuid4().hex
        unknown_object = TrackedObject(key, None, checkpoint_id)
        try:
            response, request = self._signed(
                method="PUT",
                key=key,
                context=context,
                body=context.source_bytes,
                headers=(
                    ("content-length", str(context.source_size)),
                    ("content-type", "application/octet-stream"),
                    ("x-amz-meta-s3-upload-operation-id", operation_id),
                    ("x-amz-meta-s3-upload-sha256", context.source_sha256),
                ),
            )
        except Exception:
            self._core_key = key
            return self._observation(
                passed=False,
                responses=(),
                requests=(),
                objects=(unknown_object,),
            )
        parsed = parse_operation_response(
            response,
            operation="PutObject",
            active_credentials=self._credentials,
        )
        passed = parsed.classification == "success"
        objects = (
            (unknown_object,)
            if parsed.classification in {"unknown", "identifier_rejected"}
            else ()
        )
        if objects:
            self._core_key = key
        if passed:
            self._core_key = key
            objects = (
                TrackedObject(
                    key,
                    parsed.identifiers.get("version_id"),
                    checkpoint_id,
                ),
            )
        return self._observation(
            passed=passed,
            responses=(response,),
            requests=(request,),
            objects=objects,
        )

    def _execute_HeadObject(self, context: EvidenceOperationContext) -> EvidenceObservation:
        if self._core_key is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="HEAD", key=self._core_key, context=context
        )
        length = self._header(response, "content-length")
        return self._observation(
            passed=200 <= response.status < 300 and length == str(context.source_size),
            responses=(response,),
            requests=(request,),
        )

    def _execute_GetObject(self, context: EvidenceOperationContext) -> EvidenceObservation:
        if self._core_key is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="GET", key=self._core_key, context=context
        )
        return self._observation(
            passed=200 <= response.status < 300 and response.body == context.source_bytes,
            responses=(response,),
            requests=(request,),
            response_body=response.body,
        )

    def _execute_PresignGetObject(self, context: EvidenceOperationContext) -> EvidenceObservation:
        if self._core_key is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._presigned(key=self._core_key, context=context)
        return self._observation(
            passed=200 <= response.status < 300 and response.body == context.source_bytes,
            responses=(response,),
            requests=(request,),
            response_body=response.body,
        )

    def _execute_DeleteObjectCurrentKey(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if self._core_key is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="DELETE", key=self._core_key, context=context
        )
        return self._observation(
            passed=200 <= response.status < 300,
            responses=(response,),
            requests=(request,),
        )

    def _execute_ObserveDeleteCurrentKey(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if self._core_key is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="HEAD", key=self._core_key, context=context
        )
        if response.status in {404, 410}:
            self._core_deleted = True
        return self._observation(
            passed=response.status in {404, 410},
            responses=(response,),
            requests=(request,),
        )

    def _execute_CreateMultipartUpload(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        key = context.run_prefix + "multipart-object.bin"
        checkpoint_id = uuid.uuid4().hex
        unknown_session = TrackedSession(
            key,
            None,
            checkpoint_id,
            "CreateMultipartUpload response was lost; inspect the exact run-prefix session",
        )
        try:
            response, request = self._signed(
                method="POST", key=key, query=(("uploads", ""),), context=context
            )
        except Exception:
            return self._observation(
                passed=False,
                responses=(),
                requests=(),
                sessions=(unknown_session,),
            )
        parsed = parse_operation_response(
            response,
            operation="CreateMultipartUpload",
            active_credentials=self._credentials,
        )
        passed = parsed.classification == "success"
        sessions = (
            (unknown_session,)
            if parsed.classification in {"unknown", "identifier_rejected"}
            else ()
        )
        if passed:
            self._multipart_key = key
            self._multipart_upload_id = parsed.identifiers["upload_id"]
            sessions = (
                TrackedSession(key, self._multipart_upload_id, checkpoint_id),
            )
        return self._observation(
            passed=passed,
            responses=(response,),
            requests=(request,),
            sessions=sessions,
        )

    def _execute_UploadPart(self, context: EvidenceOperationContext) -> EvidenceObservation:
        if self._multipart_key is None or self._multipart_upload_id is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="PUT",
            key=self._multipart_key,
            query=(("partNumber", "1"), ("uploadId", self._multipart_upload_id)),
            body=context.source_bytes,
            headers=(("content-length", str(context.source_size)),),
            context=context,
        )
        parsed = parse_operation_response(
            response,
            operation="UploadPart",
            active_credentials=self._credentials,
        )
        passed = parsed.classification == "success"
        if passed:
            self._multipart_etag = parsed.identifiers["etag"]
        return self._observation(
            passed=passed, responses=(response,), requests=(request,)
        )

    def _execute_ListParts(self, context: EvidenceOperationContext) -> EvidenceObservation:
        if self._multipart_key is None or self._multipart_upload_id is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="GET",
            key=self._multipart_key,
            query=(("uploadId", self._multipart_upload_id),),
            context=context,
        )
        parsed = parse_operation_response(
            response,
            operation="ListParts",
            active_credentials=self._credentials,
        )
        passed = (
            parsed.classification == "success"
            and self._multipart_etag is not None
            and parsed.identifiers.get("part_etags") == [self._multipart_etag]
        )
        return self._observation(
            passed=passed, responses=(response,), requests=(request,)
        )

    def _execute_CompleteMultipartUpload(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if None in (self._multipart_key, self._multipart_upload_id, self._multipart_etag):
            return self._observation(passed=False, responses=(), requests=())
        root = ElementTree.Element("CompleteMultipartUpload")
        part = ElementTree.SubElement(root, "Part")
        ElementTree.SubElement(part, "PartNumber").text = "1"
        ElementTree.SubElement(part, "ETag").text = self._multipart_etag
        body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=False)
        checkpoint_id = uuid.uuid4().hex
        unknown_object = TrackedObject(
            self._multipart_key, None, checkpoint_id
        )
        try:
            response, request = self._signed(
                method="POST",
                key=self._multipart_key,
                query=(("uploadId", self._multipart_upload_id),),
                body=body,
                headers=(
                    ("content-length", str(len(body))),
                    ("content-type", "application/xml"),
                ),
                context=context,
            )
        except Exception:
            return self._observation(
                passed=False,
                responses=(),
                requests=(),
                objects=(unknown_object,),
            )
        parsed = parse_operation_response(
            response,
            operation="CompleteMultipartUpload",
            active_credentials=self._credentials,
        )
        passed = parsed.classification == "success"
        objects = (
            (unknown_object,)
            if parsed.classification in {"unknown", "identifier_rejected"}
            else ()
        )
        if passed:
            objects = (
                TrackedObject(
                    self._multipart_key,
                    parsed.identifiers.get("version_id"),
                    checkpoint_id,
                ),
            )
        return self._observation(
            passed=passed,
            responses=(response,),
            requests=(request,),
            objects=objects,
        )

    def _execute_AbortMultipartUpload(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        key = context.run_prefix + "abort-object.bin"
        checkpoint_id = uuid.uuid4().hex
        unknown_session = TrackedSession(
            key,
            None,
            checkpoint_id,
            "CreateMultipartUpload response was lost during abort evidence; "
            "inspect the exact run-prefix session",
        )
        try:
            create, create_request = self._signed(
                method="POST", key=key, query=(("uploads", ""),), context=context
            )
        except Exception:
            return self._observation(
                passed=False,
                responses=(),
                requests=(),
                sessions=(unknown_session,),
            )
        parsed = parse_operation_response(
            create,
            operation="CreateMultipartUpload",
            active_credentials=self._credentials,
        )
        if parsed.classification != "success":
            return self._observation(
                passed=False,
                responses=(create,),
                requests=(create_request,),
                sessions=(unknown_session,)
                if parsed.classification in {"unknown", "identifier_rejected"}
                else (),
            )
        upload_id = parsed.identifiers["upload_id"]
        self._aborted_key, self._aborted_upload_id = key, upload_id
        tracked = TrackedSession(key, upload_id, checkpoint_id)
        try:
            abort, abort_request = self._signed(
                method="DELETE",
                key=key,
                query=(("uploadId", upload_id),),
                context=context,
            )
        except Exception:
            return self._observation(
                passed=False,
                responses=(create,),
                requests=(create_request,),
                sessions=(tracked,),
            )
        return self._observation(
            passed=200 <= abort.status < 300,
            responses=(create, abort),
            requests=(create_request, abort_request),
            sessions=() if 200 <= abort.status < 300 else (tracked,),
        )

    def _execute_ObserveMultipartSession(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if self._aborted_key is None or self._aborted_upload_id is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="GET",
            key=self._aborted_key,
            query=(("uploadId", self._aborted_upload_id),),
            context=context,
        )
        return self._observation(
            passed=response.status in {404, 410},
            responses=(response,),
            requests=(request,),
        )

    def _execute_ReservedMetadataRoundTrip(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        key = context.run_prefix + "metadata-object.bin"
        operation_id = uuid.uuid4().hex
        checkpoint_id = uuid.uuid4().hex
        unknown_object = TrackedObject(key, None, checkpoint_id)
        try:
            put, put_request = self._signed(
                method="PUT",
                key=key,
                body=context.source_bytes,
                headers=(
                    ("content-length", str(context.source_size)),
                    ("x-amz-meta-s3-upload-operation-id", operation_id),
                    ("x-amz-meta-s3-upload-sha256", context.source_sha256),
                ),
                context=context,
            )
        except Exception:
            self._metadata_key, self._metadata_operation_id = key, operation_id
            return self._observation(
                passed=False,
                responses=(),
                requests=(),
                objects=(unknown_object,),
            )
        if not 200 <= put.status < 300:
            return self._observation(
                passed=False, responses=(put,), requests=(put_request,)
            )
        parsed = parse_operation_response(
            put,
            operation="PutObject",
            active_credentials=self._credentials,
        )
        tracked = TrackedObject(
            key,
            parsed.identifiers.get("version_id")
            if parsed.classification == "success"
            else None,
            checkpoint_id,
        )
        self._metadata_key, self._metadata_operation_id = key, operation_id
        try:
            head, head_request = self._signed(
                method="HEAD", key=key, context=context
            )
        except Exception:
            return self._observation(
                passed=False,
                responses=(put,),
                requests=(put_request,),
                objects=(tracked,),
            )
        passed = (
            200 <= head.status < 300
            and self._header(head, "x-amz-meta-s3-upload-operation-id") == operation_id
            and self._header(head, "x-amz-meta-s3-upload-sha256") == context.source_sha256
            and self._header(head, "content-length") == str(context.source_size)
        )
        return self._observation(
            passed=passed,
            responses=(put, head),
            requests=(put_request, head_request),
            objects=(tracked,),
        )

    def _execute_ResponseParsing(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        return self._metadata_observation(context)

    def _execute_Reconciliation(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        return self._metadata_observation(context)

    def _metadata_observation(
        self, context: EvidenceOperationContext
    ) -> EvidenceObservation:
        if self._metadata_key is None or self._metadata_operation_id is None:
            return self._observation(passed=False, responses=(), requests=())
        response, request = self._signed(
            method="HEAD", key=self._metadata_key, context=context
        )
        passed = (
            200 <= response.status < 300
            and self._header(response, "x-amz-meta-s3-upload-operation-id")
            == self._metadata_operation_id
            and self._header(response, "x-amz-meta-s3-upload-sha256")
            == context.source_sha256
            and self._header(response, "content-length") == str(context.source_size)
        )
        return self._observation(
            passed=passed, responses=(response,), requests=(request,)
        )

    @staticmethod
    def _header(response: Response, name: str) -> Optional[str]:
        for raw_name, value in (response.headers or {}).items():
            if raw_name.lower() == name.lower():
                return value
        return None

    def cleanup_object(
        self, reference: TrackedObject, context: EvidenceOperationContext
    ) -> CleanupObservation:
        if reference.version_id is not None:
            return CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup=(
                    "DeleteObjectVersion cleanup requires explicit authorization; "
                    "delete the exact version from the restricted checkpoint"
                ),
            )
        if reference.key == self._core_key and self._core_deleted:
            return CleanupObservation(
                passed=True,
                request_count=0,
                status=404,
                manual_cleanup=None,
            )
        response, _request = self._signed(
            method="DELETE", key=reference.key, context=context
        )
        passed = 200 <= response.status < 300 or response.status in {404, 410}
        return CleanupObservation(
            passed=passed,
            request_count=1,
            status=response.status,
            manual_cleanup=None if passed else "delete the exact run-prefix object",
        )

    def abort_session(
        self, reference: TrackedSession, context: EvidenceOperationContext
    ) -> CleanupObservation:
        if reference.upload_id is None:
            return CleanupObservation(
                passed=False,
                request_count=0,
                status=None,
                manual_cleanup=reference.manual_cleanup or "inspect the exact upload session",
            )
        response, _request = self._signed(
            method="DELETE",
            key=reference.key,
            query=(("uploadId", reference.upload_id),),
            context=context,
        )
        passed = 200 <= response.status < 300 or response.status in {404, 410}
        return CleanupObservation(
            passed=passed,
            request_count=1,
            status=response.status,
            manual_cleanup=None if passed else "abort the exact run-prefix upload session",
        )
