import pytest

from headers import HeaderError, resolve_upload_headers, validate_cache_control, validate_content_disposition, validate_content_type


def test_content_type_accepts_tokens_quoted_parameters_and_fallback():
    assert validate_content_type('text/plain; charset="utf-8"') == 'text/plain; charset="utf-8"'
    assert resolve_upload_headers("archive.unknown", None, None, None, None, None) == {
        "content_type": "application/octet-stream",
        "cache_control": None,
        "content_disposition": None,
    }


@pytest.mark.parametrize(
    "value",
    ["image", "image/", " image/png", "image/png ", "image/图", "image/png\nX: y", "image/png; charset"],
)
def test_content_type_rejects_invalid_values(value):
    with pytest.raises(HeaderError):
        validate_content_type(value)


def test_cache_control_accepts_standard_directives():
    assert validate_cache_control('public, max-age=31536000, immutable') == 'public, max-age=31536000, immutable'
    assert validate_cache_control('no-cache="set-cookie"') == 'no-cache="set-cookie"'


@pytest.mark.parametrize("value", [" public", "public,", "max-age=", "max age=1", "public\nprivate", "缓存"])
def test_cache_control_rejects_invalid_values(value):
    with pytest.raises(HeaderError):
        validate_cache_control(value)


def test_content_disposition_accepts_rfc8187_filename():
    value = "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"
    assert validate_content_disposition(value) == value
    assert validate_content_disposition('inline; filename="report.pdf"') == 'inline; filename="report.pdf"'


@pytest.mark.parametrize(
    "value",
    [
        'attachment; filename="报告.pdf"',
        "attachment; filename*=UTF-8''%ZZ",
        "attachment; filename*=UNKNOWN''report.pdf",
        "attachment; filename*=UTF-8're port'report.pdf",
        " attachment",
        "attachment; filename",
    ],
)
def test_content_disposition_rejects_invalid_values(value):
    with pytest.raises(HeaderError):
        validate_content_disposition(value)


def test_operation_overrides_target_defaults_and_values_remain_exact():
    result = resolve_upload_headers(
        "cover.png",
        "public, max-age=60",
        'inline; filename="default.png"',
        "image/webp",
        "private, max-age=0",
        "attachment; filename*=UTF-8''cover.webp",
    )
    assert result == {
        "content_type": "image/webp",
        "cache_control": "private, max-age=0",
        "content_disposition": "attachment; filename*=UTF-8''cover.webp",
    }
