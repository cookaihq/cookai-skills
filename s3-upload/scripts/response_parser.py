from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence
from xml.etree import ElementTree

from s3 import Response, parse_provider_identifier


@dataclass(frozen=True)
class OperationResponse:
    classification: str
    identifiers: Dict[str, Any]


def _header(response: Response, name: str) -> Optional[str]:
    for raw_name, value in (response.headers or {}).items():
        if raw_name.lower() == name.lower():
            return value if isinstance(value, str) else None
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(response: Response) -> Optional[ElementTree.Element]:
    body = response.body
    if not body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        return None
    try:
        return ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return None


def _child_text(root: ElementTree.Element, name: str) -> Optional[str]:
    for element in root.iter():
        if _local_name(element.tag) == name:
            return element.text or ""
    return None


def _identifier(value: Any, active_credentials: Sequence[str]) -> Optional[str]:
    result = parse_provider_identifier(value, active_credentials=active_credentials)
    return result.value if result.classification == "accepted" else None


def _rejected() -> OperationResponse:
    return OperationResponse("identifier_rejected", {})


def parse_operation_response(response: Response, *, operation: str,
                             active_credentials: Sequence[str],
                             conditional: bool = False) -> OperationResponse:
    status = response.status
    if conditional and status == 412:
        return OperationResponse("precondition", {})
    if 300 <= status < 400 or status in {408, 425, 429} or status >= 500:
        return OperationResponse("unknown", {})
    if not 200 <= status < 300:
        if operation in {
            "UploadPart", "CompleteMultipartUpload", "AbortMultipartUpload", "ListParts",
        }:
            root = _xml(response)
            if (
                status == 404
                and root is not None
                and _local_name(root.tag) == "Error"
                and _child_text(root, "Code") == "NoSuchUpload"
            ):
                return OperationResponse("session_absent", {})
        return OperationResponse("definitive_failure", {})
    identifiers: Dict[str, Any] = {}
    if operation == "PutObject":
        version = _header(response, "x-amz-version-id")
        if version is None:
            version = _header(response, "x-oss-version-id")
        if version is not None:
            parsed = _identifier(version, active_credentials)
            if parsed is None:
                return _rejected()
            identifiers["version_id"] = parsed
    elif operation == "CreateMultipartUpload":
        root = _xml(response)
        if root is None:
            return OperationResponse("unknown", {})
        if _local_name(root.tag) == "Error":
            return OperationResponse("definitive_failure", {})
        if _local_name(root.tag) != "InitiateMultipartUploadResult":
            return OperationResponse("unknown", {})
        upload_id = _child_text(root, "UploadId")
        parsed = _identifier(upload_id, active_credentials)
        if parsed is None:
            return _rejected()
        identifiers["upload_id"] = parsed
    elif operation == "UploadPart":
        parsed = _identifier(_header(response, "etag"), active_credentials)
        if parsed is None:
            return _rejected()
        identifiers["etag"] = parsed
    elif operation == "CompleteMultipartUpload":
        root = _xml(response)
        if root is None:
            return OperationResponse("unknown", {})
        if _local_name(root.tag) == "Error":
            return OperationResponse("definitive_failure", {})
        if _local_name(root.tag) != "CompleteMultipartUploadResult":
            return OperationResponse("unknown", {})
        if _child_text(root, "Error") is not None:
            return OperationResponse("definitive_failure", {})
        version = _child_text(root, "VersionId")
        if version is None:
            version = _header(response, "x-amz-version-id") or _header(response, "x-oss-version-id")
        if version is not None:
            parsed = _identifier(version, active_credentials)
            if parsed is None:
                return _rejected()
            identifiers["version_id"] = parsed
    elif operation == "ListParts":
        root = _xml(response)
        if root is None:
            return OperationResponse("unknown", {})
        if _local_name(root.tag) == "Error":
            return OperationResponse("definitive_failure", {})
        if _local_name(root.tag) != "ListPartsResult":
            return OperationResponse("unknown", {})
        etags = []
        expected_number = 1
        for part in (element for element in root.iter() if _local_name(element.tag) == "Part"):
            number_text = _child_text(part, "PartNumber")
            etag = _child_text(part, "ETag")
            try:
                number = int(number_text or "")
            except ValueError:
                return OperationResponse("unknown", {})
            if number != expected_number:
                return OperationResponse("unknown", {})
            expected_number += 1
            parsed = _identifier(etag, active_credentials)
            if parsed is None:
                return _rejected()
            etags.append(parsed)
        identifiers["part_etags"] = etags
    elif operation not in {
        "HeadObject", "GetObject", "DeleteObjectCurrentKey", "DeleteObjectVersion",
        "AbortMultipartUpload", "ObserveDeleteCurrentKey", "ObserveDeleteVersion",
    }:
        raise ValueError("unsupported operation response parser")
    return OperationResponse("success", identifiers)
