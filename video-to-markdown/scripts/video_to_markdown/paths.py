from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEFAULT_OUTPUT_PARENT = "video-to-markdown-output"
UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]+")


class PathError(ValueError):
    pass


@dataclass(frozen=True)
class SourcePolicy:
    mode: str
    directory: Path | None


def is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def source_filename(value: str) -> str:
    if is_http_url(value):
        name = Path(unquote(urlsplit(value).path)).name
        return safe_leaf(name or "source-video")
    return safe_leaf(Path(value).name)


def safe_leaf(value: str, *, require_markdown: bool = False) -> str:
    if not isinstance(value, str):
        raise PathError("name must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if normalized in {"", ".", ".."} or Path(normalized).name != normalized:
        raise PathError("name must be one non-empty path component")
    normalized = UNSAFE_NAME.sub("_", normalized).strip(" ._")
    if not normalized:
        raise PathError("name contains no safe characters")
    if len(normalized.encode("utf-8")) > 180:
        while len(normalized.encode("utf-8")) > 180:
            normalized = normalized[:-1]
    if require_markdown and Path(normalized).suffix.lower() != ".md":
        raise PathError("document name must end in .md")
    return normalized


def safe_video_stem(value: str) -> str:
    filename = source_filename(value)
    stem = Path(filename).stem or filename
    return safe_leaf(stem)


def task_dir_name(video: str, now: datetime | None = None) -> str:
    moment = datetime.now().astimezone() if now is None else now
    return "%s-%s" % (moment.strftime("%Y%m%d%H%M%S%f"), safe_video_stem(video))


def detect_project_root(cwd: Path, explicit: str | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise PathError("--project-root is not an existing directory: %s" % resolved)
        return resolved
    try:
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd.resolve()
    if probe.returncode == 0 and probe.stdout.strip():
        return Path(probe.stdout.strip()).resolve()
    return cwd.resolve()


def resolve_output_parent(
    *,
    cwd: Path,
    project_root: Path,
    configured_base: str | None,
    explicit_parent: str | None,
) -> Path:
    if explicit_parent:
        path = Path(explicit_parent).expanduser()
        return (cwd / path).resolve() if not path.is_absolute() else path.resolve()
    if configured_base:
        base = Path(configured_base).expanduser()
        base = cwd / base if not base.is_absolute() else base
    else:
        base = project_root
    return (base.resolve() / DEFAULT_OUTPUT_PARENT)


def parse_source_policy(value: str | None) -> SourcePolicy:
    if not value:
        return SourcePolicy("external_local", None)
    if value == "project":
        return SourcePolicy("project", None)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise PathError(
            "VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR must be 'project' or an absolute path"
        )
    return SourcePolicy("absolute", candidate.resolve())


def create_task_directory(parent: Path, name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    task_dir = parent / safe_leaf(name)
    try:
        task_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise PathError("task directory already exists: %s" % task_dir) from exc
    return task_dir.resolve()
