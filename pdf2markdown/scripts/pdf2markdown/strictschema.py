"""严格 exact-key schema 校验助手（基线 §17.2、§8.5：额外或缺失 key 均为合同错误）。"""

import re

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SchemaError(ValueError):
    """对象不满足 strict exact schema。"""


def require_exact_keys(obj, keys, ctx):
    if not isinstance(obj, dict):
        raise SchemaError("%s: expected object" % ctx)
    expected = set(keys)
    actual = set(obj.keys())
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise SchemaError(
            "%s: exact key mismatch (missing=%s, extra=%s)" % (ctx, missing, extra)
        )
    return obj


def is_sha256_hex(value):
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def require_sha256(value, ctx, nullable=False):
    if value is None:
        if nullable:
            return None
        raise SchemaError("%s: null not allowed" % ctx)
    if not is_sha256_hex(value):
        raise SchemaError("%s: not a 64-char lowercase hex SHA-256" % ctx)
    return value


def is_opaque_id(value):
    """不超过 256 bytes 的非空 ASCII opaque string（基线 §17.2）。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return len(raw) <= 256


def require_opaque_id(value, ctx, nullable=False):
    if value is None:
        if nullable:
            return None
        raise SchemaError("%s: null not allowed" % ctx)
    if not is_opaque_id(value):
        raise SchemaError("%s: not a valid opaque id" % ctx)
    return value


def require_enum(value, allowed, ctx, nullable=False):
    if value is None and nullable:
        return None
    if value not in allowed:
        raise SchemaError("%s: %r not in closed set %s" % (ctx, value, sorted(allowed)))
    return value


def require_bool(value, ctx, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, bool):
        raise SchemaError("%s: expected boolean" % ctx)
    return value


def require_nonneg_int(value, ctx, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError("%s: expected non-negative integer" % ctx)
    return value


def require_sorted_unique_ascii(values, allowed, ctx):
    """无重复、按 ASCII 排序的闭合集合（基线 §17.2 warnings/drift_scope）。"""
    if not isinstance(values, list):
        raise SchemaError("%s: expected array" % ctx)
    for v in values:
        require_enum(v, allowed, ctx)
    if values != sorted(set(values)):
        raise SchemaError("%s: must be ASCII-sorted and duplicate-free" % ctx)
    return values
