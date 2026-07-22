from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit

from headers import validate_cache_control, validate_content_disposition
from strict_json import MAX_SAFE_INTEGER, StrictJSONError, canonicalize


NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
EXPERIMENTAL_PROVIDERS = {"aliyun-oss", "tencent-cos"}
NORMAL_PROVIDERS = {"aws-s3", "cloudflare-r2", "custom"} | EXPERIMENTAL_PROVIDERS


class SchemaError(ValueError):
    pass


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be an object")
    return value


def _exact(value: Dict[str, Any], keys: Tuple[str, ...], label: str) -> None:
    expected = set(keys)
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise SchemaError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise SchemaError(f"{label} is missing fields: {', '.join(missing)}")


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{label} must be an integer")
    if not low <= value <= high:
        raise SchemaError(f"{label} must be between {low} and {high}")
    return value


def _single_line(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise SchemaError(f"{label} must be a {'non-empty ' if nonempty else ''}string")
    if CONTROL_RE.search(value) or "\n" in value or "\r" in value:
        raise SchemaError(f"{label} must be single-line and contain no control characters")
    return value


@dataclass(frozen=True)
class ScopedReference:
    scope: str
    name: str

    @property
    def text(self) -> str:
        return f"{self.scope}:{self.name}"


def parse_reference(value: Any, label: str = "reference") -> ScopedReference:
    if not isinstance(value, str) or value.count(":") != 1:
        raise SchemaError(f"{label} must be project:<name> or global:<name>")
    scope, name = value.split(":", 1)
    if scope not in {"project", "global"} or not NAME_RE.fullmatch(name):
        raise SchemaError(f"invalid {label}")
    return ScopedReference(scope, name)


@dataclass(frozen=True)
class CredentialProfile:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: Optional[datetime]

    @property
    def kind(self) -> str:
        return "temporary" if self.session_token else "permanent"

    def remaining_seconds(self, now: datetime) -> Optional[int]:
        if self.expires_at is None:
            return None
        return int((self.expires_at - now.astimezone(timezone.utc)).total_seconds())


def parse_credential(value: Any) -> CredentialProfile:
    item = _object(value, "Credential Profile")
    _exact(item, ("access_key_id", "secret_access_key", "session_token", "expires_at"), "Credential Profile")
    access_key = item["access_key_id"]
    secret = item["secret_access_key"]
    token = item["session_token"]
    expiry = item["expires_at"]
    if not isinstance(access_key, str) or len(access_key.encode("ascii", "ignore")) != len(access_key) or len(access_key) < 8 or not TOKEN_RE.fullmatch(access_key):
        raise SchemaError("access_key_id must be an ASCII RFC 9110 token of at least 8 bytes")
    if not isinstance(secret, str) or len(secret) < 8 or any(not 0x21 <= ord(char) <= 0x7E for char in secret):
        raise SchemaError("secret_access_key must be visible ASCII of at least 8 bytes")
    if not isinstance(token, str) or (token and (len(token) < 8 or any(not 0x21 <= ord(char) <= 0x7E for char in token))):
        raise SchemaError("session_token must be empty or visible ASCII of at least 8 bytes")
    parsed_expiry = None
    if expiry is not None:
        if not isinstance(expiry, str) or not RFC3339_UTC_RE.fullmatch(expiry):
            raise SchemaError("expires_at must be null or UTC RFC 3339 with seconds precision")
        try:
            parsed_expiry = datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise SchemaError("expires_at is not a valid UTC timestamp") from exc
    if bool(token) != (parsed_expiry is not None):
        raise SchemaError("Session Token and expires_at must either both be present or both be absent")
    return CredentialProfile(access_key, secret, token, parsed_expiry)


def parse_credential_map(value: Any) -> Dict[str, CredentialProfile]:
    mapping = _object(value, "Credential map")
    if not mapping:
        raise SchemaError("Credential map must contain at least one entry")
    result: Dict[str, CredentialProfile] = {}
    for name, profile in mapping.items():
        if not NAME_RE.fullmatch(name):
            raise SchemaError("invalid Credential Profile name")
        result[name] = parse_credential(profile)
    return result


def _normalize_host(parts: Any, label: str) -> Tuple[str, Optional[int]]:
    if not parts.hostname:
        raise SchemaError(f"{label} must contain a host")
    try:
        host = parts.hostname.encode("ascii").decode("ascii")
    except UnicodeEncodeError as exc:
        raise SchemaError(f"{label} host must be an ASCII DNS name or IP literal") from exc
    if "%" in host or host.endswith(".") or ".." in host:
        raise SchemaError(f"{label} host has an ambiguous DNS spelling")
    try:
        port = parts.port
    except ValueError as exc:
        raise SchemaError(f"{label} port must be an integer from 1 to 65535") from exc
    if port == 0:
        raise SchemaError(f"{label} port must be an integer from 1 to 65535")
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(not label for label in labels):
            raise SchemaError(f"{label} host has an empty DNS label")
        host = host.lower()
    else:
        host = parsed_ip.compressed.lower()
    return host, port


def normalize_endpoint(value: str) -> str:
    value = _single_line(value, "endpoint")
    if any(char.isspace() for char in value):
        raise SchemaError("endpoint must not contain whitespace")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise SchemaError("endpoint must use HTTP or HTTPS")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise SchemaError("endpoint must not contain userinfo, path, query, or fragment")
    host, port = _normalize_host(parts, "endpoint")
    scheme = parts.scheme.lower()
    if port == (443 if scheme == "https" else 80):
        port = None
    host_text = f"[{host}]" if ":" in host else host
    netloc = host_text + (f":{port}" if port is not None else "")
    return urlunsplit((scheme, netloc, "", "", ""))


def _strict_percent_decode(segment: str, label: str) -> str:
    index = 0
    while index < len(segment):
        if segment[index] == "%":
            if index + 2 >= len(segment) or not re.fullmatch(r"[0-9A-Fa-f]{2}", segment[index + 1:index + 3]):
                raise SchemaError(f"{label} contains an invalid percent escape")
            index += 3
        else:
            index += 1
    try:
        return unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError(f"{label} percent escapes must decode as UTF-8") from exc


def normalize_public_base(value: str) -> str:
    value = _single_line(value, "public_base_url")
    if any(char.isspace() for char in value):
        raise SchemaError("public_base_url must not contain whitespace")
    parts = urlsplit(value)
    if parts.scheme.lower() != "https" or parts.username or parts.password or parts.query or parts.fragment:
        raise SchemaError("public_base_url must be HTTPS without userinfo, query, or fragment")
    host, port = _normalize_host(parts, "public_base_url")
    path = parts.path
    for segment in path.split("/"):
        if segment in {".", ".."} or _strict_percent_decode(segment, "public_base_url path") in {".", ".."}:
            raise SchemaError("public_base_url path must not contain dot segments")
    if port == 443:
        port = None
    host_text = f"[{host}]" if ":" in host else host
    netloc = host_text + (f":{port}" if port is not None else "")
    return urlunsplit(("https", netloc, path.rstrip("/"), "", ""))


def validate_object_key(value: Any, *, prefix: bool = False) -> str:
    label = "prefix" if prefix else "Object Key"
    value = _single_line(value, label, nonempty=not prefix)
    if prefix and value == "":
        return value
    if value.startswith("/") or (not prefix and value.endswith("/")):
        raise SchemaError(f"invalid {label}")
    if prefix and value and not value.endswith("/"):
        raise SchemaError("prefix must end with /")
    segments = value.split("/")
    if prefix and value:
        segments = segments[:-1]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise SchemaError(f"invalid {label} segment")
    return value


def _dns_bucket(bucket: str) -> bool:
    try:
        ipaddress.ip_address(bucket)
        return False
    except ValueError:
        pass
    return 3 <= len(bucket) <= 63 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in bucket.split(".")
    )


@dataclass(frozen=True)
class AccessPolicy:
    mode: str
    public_base_url: Optional[str]
    presign_expires_seconds: Optional[int]


@dataclass(frozen=True)
class RetentionPolicy:
    mode: str
    days: Optional[int]

    def result(self) -> Dict[str, Any]:
        return {"mode": self.mode, "days": self.days, "enforcement": "external-unverified"}


@dataclass(frozen=True)
class ObjectHeaders:
    cache_control: Optional[str]
    content_disposition: Optional[str]


@dataclass(frozen=True)
class Limits:
    soft_max_bytes: int
    multipart_threshold_bytes: Optional[int]
    part_size_bytes: Optional[int]


@dataclass(frozen=True)
class RetryPolicy:
    part_max_attempts: int
    collision_max_attempts: int


@dataclass(frozen=True)
class SetupOptions:
    exclusive_prefix: bool
    integration_test: bool
    cors: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class UploadTarget:
    schema_version: int
    credential: ScopedReference
    provider: str
    region: str
    endpoint: str
    addressing: str
    bucket: str
    prefix: str
    access: AccessPolicy
    retention: RetentionPolicy
    collision: str
    object_headers: ObjectHeaders
    limits: Limits
    retry: RetryPolicy
    setup: SetupOptions
    endpoint_explicit: bool
    addressing_explicit: bool

    def location_fingerprint(self) -> str:
        value = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "addressing": self.addressing,
            "region": self.region,
            "bucket": self.bucket,
        }
        return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()


def _parse_cors(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    item = _object(value, "setup.cors")
    keys = ("allowed_origins", "allowed_methods", "allowed_headers", "expose_headers", "max_age_seconds")
    _exact(item, keys, "setup.cors")
    result: Dict[str, Any] = {}
    for key in keys[:-1]:
        entries = item[key]
        if not isinstance(entries, list) or any(not isinstance(entry, str) or not entry for entry in entries):
            raise SchemaError(f"setup.cors.{key} must be an array of non-empty strings")
        result[key] = list(entries)
    result["max_age_seconds"] = _integer(item["max_age_seconds"], 0, MAX_SAFE_INTEGER, "setup.cors.max_age_seconds")
    return result


def parse_target(value: Any, *, expected_scope: str, allow_candidates: bool = False) -> UploadTarget:
    item = _object(value, "Upload Target")
    keys = (
        "schema_version", "credential", "provider", "region", "endpoint", "addressing",
        "bucket", "prefix", "access", "retention", "collision", "object_headers",
        "limits", "retry", "setup",
    )
    _exact(item, keys, "Upload Target")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool):
        raise SchemaError("Upload Target schema_version must be 1")
    credential_ref = parse_reference(item["credential"], "credential reference")
    if credential_ref.scope != expected_scope:
        raise SchemaError("Upload Target and Credential Profile must use the same scope")
    provider = item["provider"]
    allowed = NORMAL_PROVIDERS | (EXPERIMENTAL_PROVIDERS if allow_candidates else set())
    if provider not in allowed:
        raise SchemaError(f"unknown or unavailable provider: {provider}")
    region = _single_line(item["region"], "region")
    bucket = _single_line(item["bucket"], "bucket")
    prefix = validate_object_key(item["prefix"], prefix=True)
    endpoint_explicit = item["endpoint"] is not None
    addressing_explicit = item["addressing"] is not None
    if endpoint_explicit and not isinstance(item["endpoint"], str):
        raise SchemaError("endpoint must be a string or null")
    if addressing_explicit and item["addressing"] not in {"path", "virtual", "bucket-bound"}:
        raise SchemaError("addressing must be path, virtual, bucket-bound, or null")
    if endpoint_explicit:
        endpoint = normalize_endpoint(item["endpoint"])
    elif provider == "aws-s3":
        endpoint = "https://s3.amazonaws.com" if region == "us-east-1" else f"https://s3.{region}.amazonaws.com"
    elif provider == "aliyun-oss":
        endpoint = f"https://s3.oss-{region}.aliyuncs.com"
    elif provider == "tencent-cos":
        endpoint = f"https://cos.{region}.myqcloud.com"
    else:
        raise SchemaError(f"endpoint is required for provider {provider}")
    if addressing_explicit:
        addressing = item["addressing"]
    elif provider in {"aws-s3", "aliyun-oss", "tencent-cos"}:
        addressing = "virtual"
    elif provider == "cloudflare-r2":
        addressing = "path"
    else:
        raise SchemaError(f"addressing is required for provider {provider}")
    if addressing == "virtual":
        host = urlsplit(endpoint).hostname or ""
        try:
            ipaddress.ip_address(host)
            host_is_ip = True
        except ValueError:
            host_is_ip = False
        if host_is_ip or host == "localhost" or "." not in host or not _dns_bucket(bucket):
            raise SchemaError("virtual addressing requires a DNS endpoint and DNS-compatible bucket")

    access_value = _object(item["access"], "access")
    _exact(access_value, ("mode", "public_base_url", "presign_expires_seconds"), "access")
    mode = access_value["mode"]
    if mode == "private":
        if access_value["public_base_url"] is not None:
            raise SchemaError("private access requires public_base_url=null")
        expires = _integer(access_value["presign_expires_seconds"], 1, 604800, "presign_expires_seconds")
        access = AccessPolicy("private", None, expires)
    elif mode == "public":
        if not isinstance(access_value["public_base_url"], str) or access_value["presign_expires_seconds"] is not None:
            raise SchemaError("public access requires an HTTPS base and null presign expiry")
        access = AccessPolicy("public", normalize_public_base(access_value["public_base_url"]), None)
    else:
        raise SchemaError("access.mode must be private or public")

    retention_value = _object(item["retention"], "retention")
    _exact(retention_value, ("mode", "days"), "retention")
    if retention_value["mode"] == "retain" and retention_value["days"] is None:
        retention = RetentionPolicy("retain", None)
    elif retention_value["mode"] == "expire":
        retention = RetentionPolicy("expire", _integer(retention_value["days"], 1, MAX_SAFE_INTEGER, "retention.days"))
    else:
        raise SchemaError("invalid retention policy")

    headers = _object(item["object_headers"], "object_headers")
    _exact(headers, ("cache_control", "content_disposition"), "object_headers")
    for key in ("cache_control", "content_disposition"):
        if headers[key] is not None and not isinstance(headers[key], str):
            raise SchemaError(f"object_headers.{key} must be a string or null")
    try:
        cache_control = None if headers["cache_control"] is None else validate_cache_control(headers["cache_control"])
        content_disposition = (
            None
            if headers["content_disposition"] is None
            else validate_content_disposition(headers["content_disposition"])
        )
    except ValueError as exc:
        raise SchemaError(str(exc)) from exc
    object_headers = ObjectHeaders(cache_control, content_disposition)

    limits_value = _object(item["limits"], "limits")
    _exact(limits_value, ("soft_max_bytes", "multipart_threshold_bytes", "part_size_bytes"), "limits")
    soft = _integer(limits_value["soft_max_bytes"], 1, 536870912, "soft_max_bytes")
    threshold, part_size = limits_value["multipart_threshold_bytes"], limits_value["part_size_bytes"]
    if (threshold is None) != (part_size is None):
        raise SchemaError("multipart threshold and part size must both be null or integers")
    if threshold is not None:
        threshold = _integer(threshold, 5242880, soft, "multipart_threshold_bytes")
        part_size = _integer(part_size, 5242880, min(threshold, soft), "part_size_bytes")
    limits = Limits(soft, threshold, part_size)

    retry_value = _object(item["retry"], "retry")
    _exact(retry_value, ("part_max_attempts", "collision_max_attempts"), "retry")
    retry = RetryPolicy(
        _integer(retry_value["part_max_attempts"], 1, 5, "part_max_attempts"),
        _integer(retry_value["collision_max_attempts"], 1, 5, "collision_max_attempts"),
    )
    if item["collision"] not in {"replace", "unique", "reject"}:
        raise SchemaError("collision must be replace, unique, or reject")

    setup_value = _object(item["setup"], "setup")
    _exact(setup_value, ("exclusive_prefix", "integration_test", "cors"), "setup")
    if not isinstance(setup_value["exclusive_prefix"], bool) or not isinstance(setup_value["integration_test"], bool):
        raise SchemaError("setup flags must be booleans")
    setup = SetupOptions(setup_value["exclusive_prefix"], setup_value["integration_test"], _parse_cors(setup_value["cors"]))
    if access.mode == "public" and (not prefix or not setup.exclusive_prefix):
        raise SchemaError("public access requires a non-empty exclusive prefix")
    if retention.mode == "expire" and (not prefix or not setup.exclusive_prefix):
        raise SchemaError("expiring retention requires a non-empty exclusive prefix")
    return UploadTarget(
        1, credential_ref, provider, region, endpoint, addressing, bucket, prefix,
        access, retention, item["collision"], object_headers, limits, retry, setup,
        endpoint_explicit, addressing_explicit,
    )


def parse_project_config(value: Any) -> Tuple[Optional[ScopedReference], Dict[str, ScopedReference]]:
    item = _object(value, "project config")
    _exact(item, ("schema_version", "default_target", "skill_targets"), "project config")
    if item["schema_version"] != 1 or isinstance(item["schema_version"], bool):
        raise SchemaError("project config schema_version must be 1")
    default = None if item["default_target"] is None else parse_reference(item["default_target"], "default_target")
    mappings_value = _object(item["skill_targets"], "skill_targets")
    mappings: Dict[str, ScopedReference] = {}
    for caller, reference in mappings_value.items():
        if not NAME_RE.fullmatch(caller):
            raise SchemaError("invalid caller Skill identifier")
        mappings[caller] = parse_reference(reference, "Skill Target Mapping")
    return default, mappings
