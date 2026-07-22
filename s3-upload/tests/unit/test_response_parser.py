import pytest

from response_parser import parse_operation_response
from s3 import Response


CREDENTIALS = ("ACCESS12345678", "SECRET12345678", "TOKEN12345678")


def test_put_version_id_is_validated_before_return():
    accepted = parse_operation_response(
        Response(200, headers={"x-amz-version-id": "version-1"}),
        operation="PutObject", active_credentials=CREDENTIALS,
    )
    assert accepted.classification == "success"
    assert accepted.identifiers == {"version_id": "version-1"}

    rejected = parse_operation_response(
        Response(200, headers={"x-amz-version-id": "prefix-%53%45%43%52%45%54%31%32%33%34%35%36%37%38"}),
        operation="PutObject", active_credentials=CREDENTIALS,
    )
    assert rejected.classification == "identifier_rejected"
    assert rejected.identifiers == {}
    assert "SECRET12345678" not in repr(rejected)


@pytest.mark.parametrize(
    "header",
    ("x-amz-version-id", "x-oss-version-id", "x-cos-version-id"),
)
def test_put_accepts_each_provider_version_header_alias(header):
    parsed = parse_operation_response(
        Response(200, headers={header: "provider-version"}),
        operation="PutObject",
        active_credentials=CREDENTIALS,
    )

    assert parsed.classification == "success"
    assert parsed.identifiers == {"version_id": "provider-version"}


def test_conflicting_provider_version_aliases_are_inconclusive():
    parsed = parse_operation_response(
        Response(
            200,
            headers={
                "x-amz-version-id": "aws-version",
                "x-cos-version-id": "cos-version",
            },
        ),
        operation="PutObject",
        active_credentials=CREDENTIALS,
    )

    assert parsed.classification == "unknown"
    assert parsed.identifiers == {}


def test_create_multipart_parses_xml_entity_then_checks_reflection():
    accepted = parse_operation_response(
        Response(200, body=b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>"),
        operation="CreateMultipartUpload", active_credentials=CREDENTIALS,
    )
    assert accepted.classification == "success"
    assert accepted.identifiers == {"upload_id": "upload-1"}

    rejected = parse_operation_response(
        Response(200, body=b"<InitiateMultipartUploadResult><UploadId>prefix-SECRET12345678</UploadId></InitiateMultipartUploadResult>"),
        operation="CreateMultipartUpload", active_credentials=CREDENTIALS,
    )
    assert rejected.classification == "identifier_rejected"
    assert rejected.identifiers == {}


@pytest.mark.parametrize("etag", ["", "bad\nvalue", "x" * 4097, "TOKEN12345678"])
def test_upload_part_rejects_invalid_etag_without_returning_it(etag):
    parsed = parse_operation_response(
        Response(200, headers={"etag": etag}),
        operation="UploadPart", active_credentials=CREDENTIALS,
    )
    assert parsed.classification == "identifier_rejected"
    assert parsed.identifiers == {}
    if etag:
        assert etag not in repr(parsed)


def test_complete_http_200_embedded_error_is_not_success():
    parsed = parse_operation_response(
        Response(200, body=b"<Error><Code>InternalError</Code><Message>do not expose</Message></Error>"),
        operation="CompleteMultipartUpload", active_credentials=CREDENTIALS,
    )
    assert parsed.classification == "definitive_failure"
    assert parsed.identifiers == {}
    assert "do not expose" not in repr(parsed)


def test_complete_success_parses_optional_version_and_malformed_xml_is_unknown():
    success = parse_operation_response(
        Response(200, body=b"<CompleteMultipartUploadResult><VersionId>version-2</VersionId></CompleteMultipartUploadResult>"),
        operation="CompleteMultipartUpload", active_credentials=CREDENTIALS,
    )
    assert success.classification == "success"
    assert success.identifiers == {"version_id": "version-2"}
    malformed = parse_operation_response(
        Response(200, body=b"<CompleteMultipartUploadResult>"),
        operation="CompleteMultipartUpload", active_credentials=CREDENTIALS,
    )
    assert malformed.classification == "unknown"


def test_complete_accepts_cos_version_header_when_xml_omits_version():
    parsed = parse_operation_response(
        Response(
            200,
            body=b"<CompleteMultipartUploadResult></CompleteMultipartUploadResult>",
            headers={"x-cos-version-id": "cos-version"},
        ),
        operation="CompleteMultipartUpload",
        active_credentials=CREDENTIALS,
    )

    assert parsed.classification == "success"
    assert parsed.identifiers == {"version_id": "cos-version"}


def test_status_classification_keeps_conditional_precondition_distinct():
    assert parse_operation_response(
        Response(412), operation="PutObject", active_credentials=CREDENTIALS,
        conditional=True,
    ).classification == "precondition"
    assert parse_operation_response(
        Response(503), operation="PutObject", active_credentials=CREDENTIALS,
    ).classification == "unknown"
    assert parse_operation_response(
        Response(403), operation="PutObject", active_credentials=CREDENTIALS,
    ).classification == "definitive_failure"


def test_list_parts_validates_every_returned_etag():
    response = Response(
        200,
        body=b"<ListPartsResult><Part><PartNumber>1</PartNumber><ETag>etag-1</ETag></Part><Part><PartNumber>2</PartNumber><ETag>etag-2</ETag></Part></ListPartsResult>",
    )
    parsed = parse_operation_response(response, operation="ListParts", active_credentials=CREDENTIALS)
    assert parsed.classification == "success"
    assert parsed.identifiers == {"part_etags": ["etag-1", "etag-2"]}


@pytest.mark.parametrize(
    "operation, body",
    [
        ("CreateMultipartUpload", b"<Unexpected><UploadId>upload-1</UploadId></Unexpected>"),
        ("CompleteMultipartUpload", b"<Unexpected><VersionId>version-1</VersionId></Unexpected>"),
        ("ListParts", b"<Unexpected><Part><PartNumber>1</PartNumber><ETag>etag-1</ETag></Part></Unexpected>"),
    ],
)
def test_multipart_success_requires_the_operation_specific_root(operation, body):
    parsed = parse_operation_response(
        Response(200, body=body), operation=operation, active_credentials=CREDENTIALS,
    )
    assert parsed.classification == "unknown"
    assert parsed.identifiers == {}


def test_no_such_upload_is_a_distinct_authoritative_session_observation():
    parsed = parse_operation_response(
        Response(404, body=b"<Error><Code>NoSuchUpload</Code></Error>"),
        operation="ListParts", active_credentials=CREDENTIALS,
    )
    assert parsed.classification == "session_absent"
