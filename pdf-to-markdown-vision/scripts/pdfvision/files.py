from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
from typing import Any

from markdown_it import MarkdownIt

SKILL = Path(__file__).resolve().parents[2]


def require_space(path: Path, additional=0) -> None:
    if shutil.disk_usage(path).free < 16 * 1024 * 1024 + additional:
        raise OSError(errno.ENOSPC, "Insufficient disk space; existing results retained, resume after freeing space")


def version() -> str:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r'^  version: "(\d+\.\d+\.\d+)"$', text, re.M)
    if not match:
        raise ValueError("Invalid Skill metadata.version")
    return match[1]


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data.encode("utf-8") if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def within(root: Path, relative: str) -> Path:
    if "\\" in relative or ":" in relative or Path(relative).is_absolute():
        raise ValueError("Expected a portable relative file reference: " + relative)
    path = root / relative
    if ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("File reference escapes its bundle: " + relative)
    return path


def image_refs(markdown: str) -> list[str]:
    refs = []
    for token in MarkdownIt("commonmark").parse(markdown):
        if token.type in {"html_inline", "html_block"}:
            raise ValueError("Raw HTML is not supported in portable Markdown; use Markdown images/tables")
        for child in token.children or []:
            if child.type == "html_inline":
                raise ValueError("Raw HTML is not supported in portable Markdown")
            if child.type == "image":
                refs.append(str(child.attrGet("src") or ""))
    return refs


def validate_images(root: Path, markdown: str) -> list[str]:
    from urllib.parse import unquote
    from PIL import Image
    refs = image_refs(markdown)
    for ref in refs:
        path = within(root, unquote(ref))
        if not path.is_file():
            raise ValueError("Missing image: " + ref)
        with Image.open(path) as image:
            image.verify()
    return refs


def ignore_data(project: Path) -> str:
    if not shutil.which("git"):
        return "git-unavailable"
    probe = subprocess.run(["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, timeout=10)
    if probe.returncode:
        return "non-git"
    target = project / "data/pdf-to-markdown"
    check = subprocess.run(["git", "-C", str(project), "check-ignore", "-q", str(target) + "/"],
                           capture_output=True, timeout=10)
    if check.returncode == 0:
        return "already-ignored"
    path = project / ".gitignore"
    old = path.read_bytes().decode("utf-8") if path.exists() else ""
    line = "/data/pdf-to-markdown/"
    if line not in old.splitlines():
        atomic(path, old + ("\n" if old and not old.endswith("\n") else "") + line + "\n")
    return "ignore-added; tracked files retained"
