"""RFC 8785 JSON Canonicalization Scheme (JCS)（基线 §4 末段）。

canonicalize() 输出无 BOM、无前后空白、无尾换行的 UTF-8 bytes；
loads_strict() 拒绝重复 key、lone surrogate、超出 I-JSON 精确范围的整数
和非有限数字。字符串只做 JSON 必需转义，不做 ASCII-only 转义，不转义 '/'。
对象属性按 UTF-16 code unit 排序。
"""

import hashlib
import json
import math

I_JSON_MAX_INT = 2**53 - 1

_CTRL_SHORT = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r"}


class JCSError(ValueError):
    """输入不满足 JCS/I-JSON 约束。"""


def _check_string(s):
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise JCSError("lone surrogate in string")
    return s


def _escape_string(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif o < 0x20:
            out.append(_CTRL_SHORT.get(o, "\\u%04x" % o))
        elif 0xD800 <= o <= 0xDFFF:
            raise JCSError("lone surrogate in string")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_int(value):
    if abs(value) > I_JSON_MAX_INT:
        raise JCSError("integer outside I-JSON exact range: %d" % value)
    return str(value)


def _format_float(value):
    """按 ECMAScript Number::toString(10) 词法输出（RFC 8785 §3.2.2.3）。"""
    if not math.isfinite(value):
        raise JCSError("non-finite number")
    if value == 0.0:
        return "0"
    sign = "-" if value < 0 else ""
    a = abs(value)
    # Python repr 已是最短往返十进制，与 ES 的最短数字串一致；只需换词法。
    rep = repr(a)
    if "e" in rep or "E" in rep:
        mant, _, exp_part = rep.lower().partition("e")
        exp10 = int(exp_part)
    else:
        mant, exp10 = rep, 0
    if "." in mant:
        int_part, _, frac_part = mant.partition(".")
    else:
        int_part, frac_part = mant, ""
    digits = (int_part + frac_part).lstrip("0")
    # n: 小数点在有效数字串左侧的位置（ES 规范中的 n）；k: 有效数字个数。
    n = len(int_part.lstrip("0")) + exp10 if int_part.lstrip("0") else 0
    if not int_part.lstrip("0"):
        lead_zeros = len(frac_part) - len(frac_part.lstrip("0"))
        n = -lead_zeros + exp10
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
        body = "%se%s%d" % (mantissa, "+" if e >= 0 else "-", abs(e))
    return sign + body


def _serialize(value, out):
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(_format_int(value))
    elif isinstance(value, float):
        out.append(_format_float(value))
    elif isinstance(value, str):
        out.append(_escape_string(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        keys = []
        for k in value:
            if not isinstance(k, str):
                raise JCSError("object key must be string")
            _check_string(k)
            keys.append(k)
        keys.sort(key=lambda k: k.encode("utf-16-be"))
        out.append("{")
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            out.append(_escape_string(k))
            out.append(":")
            _serialize(value[k], out)
        out.append("}")
    else:
        raise JCSError("unsupported type: %r" % type(value))


def canonicalize(value):
    """返回 JCS 规范 JSON 的 UTF-8 bytes。"""
    out = []
    _serialize(value, out)
    return "".join(out).encode("utf-8")


def sha256_canonical(value):
    """规范 JSON 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonicalize(value)).hexdigest()


def _reject_constant(name):
    raise JCSError("non-finite number literal: %s" % name)


def _pairs_hook(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise JCSError("duplicate object key: %r" % k)
        obj[k] = v
    return obj


def _validate_tree(value):
    if isinstance(value, str):
        _check_string(value)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        if abs(value) > I_JSON_MAX_INT:
            raise JCSError("integer outside I-JSON exact range: %d" % value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise JCSError("non-finite number")
    elif isinstance(value, list):
        for item in value:
            _validate_tree(item)
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_string(k)
            _validate_tree(v)
    return value


def loads_strict(data):
    """严格解析 JSON：拒绝重复 key、lone surrogate、超范围整数、非有限数。"""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JCSError("input is not valid UTF-8: %s" % exc)
    try:
        value = json.loads(
            data,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
        )
    except JCSError:
        raise
    except ValueError as exc:
        raise JCSError("invalid JSON: %s" % exc)
    return _validate_tree(value)
