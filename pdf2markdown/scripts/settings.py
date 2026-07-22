from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from copy import deepcopy
from pathlib import Path

import strict_json


SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 1024 * 1024
ENVIRONMENT_FIELDS = {
    "interaction_mode": "PDF2MARKDOWN_INTERACTION_MODE",
    "publishing.mode": "PDF2MARKDOWN_PUBLISH_MODE",
    "publishing.uploader": "PDF2MARKDOWN_UPLOADER",
    "publishing.target_ref": "PDF2MARKDOWN_UPLOAD_TARGET",
}
UPLOADER_RE = re.compile(r"(?:skill|tool):[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TARGET_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class SettingsError(ValueError):
    pass


class SettingsWriteError(OSError):
    pass


def default_document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "interaction_mode": "confirm",
        "publishing": {"mode": "skip", "publisher_binding": None},
    }


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(_json_bytes(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise SettingsWriteError("atomic settings write failed") from exc


def _validate_behavior(interaction_mode, publishing) -> None:
    if (
        not isinstance(interaction_mode, str)
        or interaction_mode not in {"confirm", "auto"}
        or not isinstance(publishing, dict)
        or not isinstance(publishing.get("mode"), str)
        or publishing["mode"] not in {"skip", "upload"}
        or publishing.get("publisher_binding") is not None
    ):
        raise SettingsError("invalid behavior settings")
    uploader = publishing.get("uploader")
    target_ref = publishing.get("target_ref")
    if uploader is not None and (
        not isinstance(uploader, str) or UPLOADER_RE.fullmatch(uploader) is None
    ):
        raise SettingsError("invalid uploader selector")
    if target_ref is not None and (
        not isinstance(target_ref, str)
        or TARGET_REF_RE.fullmatch(target_ref) is None
    ):
        raise SettingsError("invalid upload target reference")


def _validate(document) -> dict:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "interaction_mode",
        "publishing",
    }:
        raise SettingsError("unknown settings fields")
    publishing = document.get("publishing")
    publishing_fields = set(publishing) if isinstance(publishing, dict) else set()
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or not isinstance(publishing, dict)
        or not {"mode", "publisher_binding"}.issubset(publishing_fields)
        or not publishing_fields.issubset(
            {"mode", "uploader", "target_ref", "publisher_binding"}
        )
    ):
        raise SettingsError("invalid settings schema")
    _validate_behavior(document.get("interaction_mode"), publishing)
    return document


def _read_bounded_file(path: Path, *, nofollow: bool) -> bytes | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(path, follow_symlinks=not nofollow)
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise SettingsError("settings file is unavailable or unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_SETTINGS_BYTES
        ):
            raise SettingsError("settings file is not a bounded regular file")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_SETTINGS_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_SETTINGS_BYTES:
                raise SettingsError("settings file is too large")
        final = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=not nofollow)
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise SettingsError("settings file changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load(path: Path) -> tuple[dict | None, str | None]:
    data = _read_bounded_file(path, nofollow=True)
    if data is None:
        return None, None
    try:
        document = strict_json.loads(data)
    except strict_json.StrictJsonError as exc:
        raise SettingsError("settings file is not valid JSON") from exc
    document = _validate(document)
    return document, f"sha256:{hashlib.sha256(data).hexdigest()}"


def parse_dotenv(text: str) -> dict[str, str]:
    recognized = set(ENVIRONMENT_FIELDS.values())
    values = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split("=", 1)
        key = raw_key.strip()
        if key not in recognized:
            continue
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise SettingsError("invalid quoted dotenv value")
            value = value[1:-1]
        values[key] = value
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    data = _read_bounded_file(path, nofollow=False)
    if data is None:
        return {}
    try:
        return parse_dotenv(data.decode("utf-8"))
    except UnicodeError as exc:
        raise SettingsError("dotenv file must be UTF-8") from exc


def _nonempty(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError("setting values must be strings")
    return value if value.strip() else None


def _persistent_value(document: dict | None, field: str):
    if document is None:
        return None
    if field == "interaction_mode":
        return document["interaction_mode"]
    name = field.removeprefix("publishing.")
    return document["publishing"].get(name)


def resolve(
    document: dict | None,
    *,
    cli: dict[str, str | None],
    environ: dict[str, str],
    cwd: Path,
    config_home: Path,
    use_local_key: bool,
) -> dict:
    dotenv_local = _read_dotenv(cwd / ".env.local")
    dotenv = _read_dotenv(cwd / ".env")
    home_dotenv = _read_dotenv(config_home / ".env") if use_local_key else {}
    values = {}
    sources = {}
    defaults = {
        "interaction_mode": "confirm",
        "publishing.mode": "skip",
        "publishing.uploader": None,
        "publishing.target_ref": None,
    }
    for field, environment_name in ENVIRONMENT_FIELDS.items():
        candidates = (
            (cli.get(field), "command_line"),
            (environ.get(environment_name), "process_environment"),
            (dotenv_local.get(environment_name), "cwd_dotenv_local"),
            (dotenv.get(environment_name), "cwd_dotenv"),
            (home_dotenv.get(environment_name), "home_dotenv"),
            (_persistent_value(document, field), "persistent_settings"),
            (defaults[field], "built_in_default"),
        )
        for candidate, source in candidates:
            selected = _nonempty(candidate)
            if selected is not None:
                values[field] = selected
                sources[field] = source
                break

    if values["interaction_mode"] not in {"confirm", "auto"}:
        raise SettingsError("interaction mode is invalid")
    if values["publishing.mode"] not in {"skip", "upload"}:
        raise SettingsError("publishing mode is invalid")
    uploader = values.get("publishing.uploader")
    if uploader is not None and UPLOADER_RE.fullmatch(uploader) is None:
        raise SettingsError("uploader selector is invalid")
    target_ref = values.get("publishing.target_ref")
    if target_ref is not None and TARGET_REF_RE.fullmatch(target_ref) is None:
        raise SettingsError("upload target reference is invalid")

    publishing = {
        "mode": values["publishing.mode"],
        "publisher_binding": None,
    }
    if uploader is not None:
        publishing["uploader"] = uploader
    if target_ref is not None:
        publishing["target_ref"] = target_ref
    selected_sources = {
        field: sources[field]
        for field in ENVIRONMENT_FIELDS
        if field in {"interaction_mode", "publishing.mode"}
        or values.get(field) is not None
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "interaction_mode": values["interaction_mode"],
        "publishing": publishing,
        "sources": selected_sources,
    }


def status(
    path: Path,
    *,
    cli: dict[str, str | None],
    environ: dict[str, str],
    cwd: Path,
    config_home: Path,
    home_config_authorized: bool,
) -> dict:
    document, content_hash = load(path)
    resolved = resolve(
        document,
        cli=cli,
        environ=environ,
        cwd=cwd,
        config_home=config_home,
        use_local_key=home_config_authorized,
    )
    publication_execution = (
        {"executable": False, "reason_code": "publication_skipped"}
        if resolved["publishing"]["mode"] == "skip"
        else {"executable": False, "reason_code": "publisher_binding_missing"}
    )
    return {
        "path": str(path),
        "exists": document is not None,
        "persisted": document,
        "effective": resolved,
        "content_hash": content_hash,
        "home_config_authorized": home_config_authorized,
        "publication_execution": publication_execution,
    }


def invocation_cwd_identity(cwd: Path) -> dict:
    try:
        canonical = cwd.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(canonical), flags)
    except (OSError, RuntimeError) as exc:
        raise SettingsError("invocation cwd is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(canonical, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SettingsError("invocation cwd identity is unstable")
        return {
            "path": str(canonical),
            "device": opened.st_dev,
            "inode": opened.st_ino,
        }
    finally:
        os.close(descriptor)


def snapshot(status_value: dict, *, cwd: Path) -> dict:
    effective_settings = status_value["effective"]
    return {
        "schema_version": SCHEMA_VERSION,
        "interaction_mode": effective_settings["interaction_mode"],
        "publishing": dict(effective_settings["publishing"]),
        "sources": dict(effective_settings["sources"]),
        "invocation_cwd": invocation_cwd_identity(cwd),
        "settings_file": {
            "path": status_value["path"],
            "content_hash": status_value["content_hash"],
        },
    }


def validate_snapshot(value) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "interaction_mode",
        "publishing",
        "sources",
        "invocation_cwd",
        "settings_file",
    }:
        raise SettingsError("unknown settings snapshot fields")
    publishing = value.get("publishing")
    publishing_fields = set(publishing) if isinstance(publishing, dict) else set()
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or not isinstance(publishing, dict)
        or not {"mode", "publisher_binding"}.issubset(publishing_fields)
        or not publishing_fields.issubset(
            {"mode", "uploader", "target_ref", "publisher_binding"}
        )
    ):
        raise SettingsError("invalid settings snapshot values")
    _validate_behavior(value.get("interaction_mode"), publishing)
    uploader = publishing.get("uploader")
    target_ref = publishing.get("target_ref")

    expected_source_fields = {"interaction_mode", "publishing.mode"}
    if uploader is not None:
        expected_source_fields.add("publishing.uploader")
    if target_ref is not None:
        expected_source_fields.add("publishing.target_ref")
    sources = value.get("sources")
    allowed_sources = {
        "command_line",
        "process_environment",
        "cwd_dotenv_local",
        "cwd_dotenv",
        "home_dotenv",
        "persistent_settings",
        "built_in_default",
        "work_bundle_snapshot",
        "resume_command_line",
    }
    if (
        not isinstance(sources, dict)
        or set(sources) != expected_source_fields
        or any(source not in allowed_sources for source in sources.values())
    ):
        raise SettingsError("invalid settings snapshot sources")

    cwd_identity = value.get("invocation_cwd")
    settings_file = value.get("settings_file")
    content_hash = (
        settings_file.get("content_hash") if isinstance(settings_file, dict) else None
    )
    if (
        not isinstance(cwd_identity, dict)
        or set(cwd_identity) != {"path", "device", "inode"}
        or not isinstance(cwd_identity.get("path"), str)
        or not Path(cwd_identity["path"]).is_absolute()
        or type(cwd_identity.get("device")) is not int
        or cwd_identity["device"] < 0
        or type(cwd_identity.get("inode")) is not int
        or cwd_identity["inode"] < 0
        or not isinstance(settings_file, dict)
        or set(settings_file) != {"path", "content_hash"}
        or not isinstance(settings_file.get("path"), str)
        or not Path(settings_file["path"]).is_absolute()
        or (
            content_hash is not None
            and (
                not isinstance(content_hash, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash) is None
            )
        )
    ):
        raise SettingsError("invalid settings snapshot metadata")


def snapshot_hash(value: dict) -> str:
    validate_snapshot(value)
    data = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def apply_resume_overrides(
    saved_snapshot: dict, cli: dict[str, str | None]
) -> tuple[dict, list[str]]:
    validate_snapshot(saved_snapshot)
    updated = deepcopy(saved_snapshot)
    overridden_fields = []
    for field in ENVIRONMENT_FIELDS:
        candidate = cli.get(field)
        if candidate is None:
            continue
        if not isinstance(candidate, str) or not candidate:
            raise SettingsError("resume overrides must be non-empty strings")
        if field == "interaction_mode":
            if candidate not in {"confirm", "auto"}:
                raise SettingsError("interaction mode override is invalid")
            updated["interaction_mode"] = candidate
        elif field == "publishing.mode":
            if candidate not in {"skip", "upload"}:
                raise SettingsError("publishing mode override is invalid")
            updated["publishing"]["mode"] = candidate
        elif field == "publishing.uploader":
            if UPLOADER_RE.fullmatch(candidate) is None:
                raise SettingsError("uploader override is invalid")
            updated["publishing"]["uploader"] = candidate
        else:
            if TARGET_REF_RE.fullmatch(candidate) is None:
                raise SettingsError("target override is invalid")
            updated["publishing"]["target_ref"] = candidate
        updated["sources"][field] = "resume_command_line"
        overridden_fields.append(field)
    validate_snapshot(updated)
    return updated, overridden_fields


def validate_snapshot_transition(
    previous: dict, updated: dict, overridden_fields: list[str]
) -> None:
    validate_snapshot(previous)
    validate_snapshot(updated)
    if (
        not isinstance(overridden_fields, list)
        or not overridden_fields
        or len(overridden_fields) != len(set(overridden_fields))
        or any(field not in ENVIRONMENT_FIELDS for field in overridden_fields)
        or previous["invocation_cwd"] != updated["invocation_cwd"]
        or previous["settings_file"] != updated["settings_file"]
        or previous["publishing"]["publisher_binding"]
        != updated["publishing"]["publisher_binding"]
    ):
        raise SettingsError("invalid settings snapshot transition")
    previous_values = {
        "interaction_mode": previous["interaction_mode"],
        "publishing.mode": previous["publishing"]["mode"],
        "publishing.uploader": previous["publishing"].get("uploader"),
        "publishing.target_ref": previous["publishing"].get("target_ref"),
    }
    updated_values = {
        "interaction_mode": updated["interaction_mode"],
        "publishing.mode": updated["publishing"]["mode"],
        "publishing.uploader": updated["publishing"].get("uploader"),
        "publishing.target_ref": updated["publishing"].get("target_ref"),
    }
    for field in ENVIRONMENT_FIELDS:
        if field in overridden_fields:
            if updated["sources"].get(field) != "resume_command_line":
                raise SettingsError("override source is invalid")
        elif (
            previous_values[field] != updated_values[field]
            or previous["sources"].get(field) != updated["sources"].get(field)
        ):
            raise SettingsError("unoverridden settings changed")
