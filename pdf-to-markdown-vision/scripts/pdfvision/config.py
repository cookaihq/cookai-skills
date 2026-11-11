"""Project-root configuration; reading never creates or migrates configuration."""
from pathlib import Path
import os
import re

from filelock import FileLock

from .files import atomic, within

KEY = "PDF_TO_MARKDOWN_CLEANING_PROFILE"


def value_from(path: Path) -> str | None:
    if not path.exists():
        return None
    value = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, found = line.split("=", 1)
        if key.strip() != KEY:
            continue
        found = found.strip()
        if len(found) >= 2 and found[0] == found[-1] and found[0] in "\"'":
            found = found[1:-1]
        value = found
    return value


def effective(project: Path, use_global=False) -> dict:
    paths = [project / name for name in (".env.pdf-to-markdown-vision", ".env.local", ".env")]
    if use_global:
        paths.append(Path.home() / ".config/pdf-to-markdown-vision/.env")
    # Read every participating file: an unreadable configured source is an error,
    # including one whose value would be shadowed by a higher layer.
    values = [(path, value_from(path)) for path in paths]
    if os.environ.get(KEY, "").strip():
        return {"key": KEY, "value": os.environ[KEY], "source": "process", "project": str(project)}
    for path, found in values:
        if found is not None and found.strip():
            return {"key": KEY, "value": found, "source": path.name if path.parent == project else str(path),
                    "project": str(project)}
    return {"key": KEY, "value": None, "source": None, "project": str(project)}


def select(project: Path, *, cleaning=None, no_cleaning=False, instructions=None, use_global=False) -> dict:
    if no_cleaning:
        return {"enabled": False, "source": "request:no-cleaning"}
    try:
        selected = effective(project, use_global)
        chosen = cleaning if cleaning is not None else (None if instructions is not None else selected["value"])
        source = "request" if cleaning is not None or instructions is not None else selected["source"]
        content = None
        origin = None
        if chosen:
            path = Path(chosen).expanduser()
            if not path.is_absolute():
                path = project / path
            origin = str(path.resolve())
            content = path.read_text(encoding="utf-8-sig")
            if not content.strip():
                raise ValueError("Selected cleaning profile is empty: " + origin)
        if instructions is not None:
            override = Path(instructions).read_text(encoding="utf-8-sig")
            if not override.strip():
                raise ValueError("Temporary cleaning instructions are empty")
            content = (content + "\n\n# 本次覆盖（冲突时优先）\n\n" + override) if content else override
        return {"enabled": content is not None, "content": content, "source": source, "origin": origin,
                "temporary_override": instructions is not None}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"enabled": True, "error": str(exc), "source": "selection-error"}


def save_default(project: Path, profile: str, use_global=False) -> dict:
    candidate = Path(profile)
    if not candidate.is_absolute():
        candidate = project / candidate
    content = candidate.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise ValueError("Selected cleaning profile is empty")
    resolved = candidate.resolve()
    value = resolved.relative_to(project).as_posix() if resolved.is_relative_to(project) else str(resolved)
    if any(c in value for c in "\n\r\""):
        raise ValueError("Profile path cannot be represented by this dotenv format")
    target = project / ".env.local"
    with FileLock(str(project / ".pdf-vision-config.lock"), timeout=0):
        text = target.read_bytes().decode("utf-8-sig") if target.exists() else ""
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines(keepends=True)
        pattern = re.compile(r"^\s*" + KEY + r"\s*=")
        lines = [line for line in lines if not pattern.match(line)]
        preserved = "".join(lines)
        if preserved and not preserved.endswith(("\n", "\r")):
            preserved += newline
        atomic(target, preserved + f'{KEY}="{value}"' + newline)
        if value_from(target) != value:
            raise ValueError("Default profile failed read-back verification")
    return {"saved": str(target), "value": value, "effective": effective(project, use_global)}


def save_profile(project: Path, name: str, source: Path, set_default=False, use_global=False) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9][\w-]{0,63}", name) or name.upper() in {"CON", "PRN", "AUX", "NUL"}:
        raise ValueError("Use a portable descriptive profile name")
    content = source.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise ValueError("A cleaning profile needs actionable content")
    relative = f"config/pdf-to-markdown/cleaning/{name}.md"
    path = within(project, relative)
    atomic(path, content)
    if path.read_text(encoding="utf-8") != content:
        raise ValueError("Cleaning profile failed read-back verification")
    result = {"profile": str(path)}
    if set_default:
        result.update(save_default(project, relative, use_global))
    return result
