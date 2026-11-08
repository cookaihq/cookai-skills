"""Finalize names and publish a portable Markdown copy without overwriting files."""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

from . import paths, review, state


class DeliveryError(ValueError):
    pass


def initialize(video: str, task_name: str, copy_directory: Path | None) -> dict:
    return {
        "timestamp": task_name.split("-", 1)[0],
        "original_filename": paths.source_filename(video),
        "copy_directory": str(copy_directory) if copy_directory else None,
        "name_decision": None,
        "title": None,
        "status": "pending",
    }


def _rebase(value, old: Path, new: Path):
    """Relocate whole absolute path values, never replace text inside prose or URLs."""
    if isinstance(value, dict):
        return {
            key: item if key in {"rename_pending", "origin", "request", "invocation_cwd", "project_root"}
            else _rebase(item, old, new)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rebase(item, old, new) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        try:
            relative = Path(value).relative_to(old)
        except ValueError:
            return value
        return str(new / relative)
    return value


def recover_rename(task_dir: Path) -> Path:
    """An intent saved before the first rename makes each subsequent write replayable."""
    task_dir, manifest, _ = state.load_task(task_dir)
    pending = manifest.get("delivery", {}).get("rename_pending")
    if not pending:
        return task_dir
    old, new = Path(pending["old_directory"]), Path(pending["new_directory"])
    if old.parent != new.parent or task_dir not in {old, new}:
        raise DeliveryError("rename recovery directory does not match the saved task")
    old_name = paths.safe_leaf(pending["old_document"], require_markdown=True)
    new_name = paths.safe_leaf(pending["new_document"], require_markdown=True)
    source, destination = task_dir / old_name, task_dir / new_name
    if old_name != new_name and source.exists():
        if destination.exists():
            raise DeliveryError("document rename target already exists: %s" % destination)
        source.rename(destination)
    if not destination.is_file():
        raise DeliveryError("document missing during rename recovery: %s" % destination)
    report = state.read_json(task_dir / "review-report.json")
    report["document"] = new_name
    state.atomic_write_json(task_dir / "review-report.json", report)
    manifest["artifacts"]["document"] = new_name
    state.save_manifest(task_dir, manifest)
    if old != new and task_dir == old:
        if new.exists():
            raise DeliveryError("task rename target already exists: %s" % new)
        print("Final task directory (also use this after an interrupted rename): %s" % new, file=sys.stderr)
        old.rename(new)
        task_dir = new
    if old != new:
        # Includes frame/contact-sheet paths, managed video and dependency receipts.
        # A replay also repairs JSON files written before an interrupted relocation.
        for entry in task_dir.rglob("*.json"):
            if entry == task_dir / state.MANIFEST_NAME:
                continue
            document = state.read_json(entry)
            relocated = _rebase(document, old, new)
            if relocated != document:
                state.atomic_write_json(entry, relocated)
        manifest = _rebase(manifest, old, new)
    manifest["task_dir_name"] = task_dir.name
    manifest["delivery"].pop("rename_pending")
    manifest["delivery"]["status"] = "named"
    state.save_manifest(task_dir, manifest)
    return task_dir


def _copy_plan(task_dir: Path, manifest: dict) -> dict[Path, bytes]:
    settings = manifest["delivery"]
    if settings["copy_directory"] is None:
        return {}
    directory = Path(settings["copy_directory"])
    name = manifest["artifacts"]["document"]
    primary = task_dir / name
    destination = directory / name
    if destination.resolve() == primary.resolve():
        return {}  # The requested destination already holds the primary document.
    if directory.is_relative_to(task_dir / ".state"):
        raise DeliveryError("copy directory cannot be inside private task state")
    asset_name = paths.safe_leaf(name[:-3] + ".assets")
    plan: dict[Path, bytes] = {}
    markdown = primary.read_text(encoding="utf-8")

    def replace(match):
        target = unquote(match.group("target").strip("<>"))
        source = review._safe_relative(task_dir, target)
        if source is None or not source.is_file():
            raise DeliveryError("copy media is missing or unsafe: %s" % target)
        try:
            relative = source.relative_to(task_dir / "assets")
        except ValueError as exc:
            raise DeliveryError("copy media must be inside assets: %s" % target) from exc
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif"}:
            raise DeliveryError("unsupported copy media: %s" % target)
        output = directory / asset_name / relative
        plan[output] = source.read_bytes()
        link = quote((Path(asset_name) / relative).as_posix(), safe="/-._~")
        start, end = match.span("target")
        return match.group(0)[:start - match.start()] + link + match.group(0)[end - match.start():]

    markdown = review.IMAGE_LINK.sub(replace, markdown)
    plan[destination] = markdown.encode("utf-8")  # Publish Markdown after its media.
    if any(not path.resolve().is_relative_to(directory.resolve()) for path in plan):
        raise DeliveryError("a copy target resolves outside the selected copy directory")
    return plan


def _check_existing(path: Path, data: bytes) -> bool:
    if path.is_symlink():
        raise DeliveryError("copy target is a symbolic link: %s" % path)
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise DeliveryError("copy target already exists with different content: %s; choose another --copy-dir" % path)
        return True
    return False


def _publish_file(path: Path, data: bytes) -> None:
    if _check_existing(path, data):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".video-to-markdown-%s.tmp" % uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            if os.name == "nt":
                os.rename(temporary, path)  # Windows rename refuses an existing target.
            else:
                os.link(temporary, path)  # Atomic no-replace publication on POSIX.
        except FileExistsError:
            _check_existing(path, data)
    finally:
        temporary.unlink(missing_ok=True)


def copy_is_current(task_dir: Path, manifest: dict) -> bool:
    if manifest.get("delivery", {}).get("status") != "complete":
        return False
    try:
        return all(path.is_file() and not path.is_symlink() and path.read_bytes() == data
                   for path, data in _copy_plan(task_dir, manifest).items())
    except (OSError, DeliveryError):
        return False


def finalize(task_dir: Path, *, title: str | None, keep_name: bool, copy_directory: str | None) -> dict:
    task_dir = recover_rename(task_dir)
    task_dir, manifest, _ = state.load_task(task_dir)
    result = review.validate_task(task_dir, manifest)
    if not result["complete"]:
        raise DeliveryError("document validation failed: %s" % "; ".join(result["blockers"]))
    settings = manifest["delivery"]
    decision = "content-title" if title is not None else "keep-name" if keep_name else settings["name_decision"]
    chosen_title = paths.safe_leaf(title) if title is not None else None if keep_name else settings["title"]
    if decision is None:
        raise DeliveryError("inspect the video, then use --title for an opaque filename or --keep-name for a meaningful filename")
    if settings["name_decision"] and (decision, chosen_title) != (settings["name_decision"], settings["title"]):
        raise DeliveryError("this task already has a final naming decision; resume it without a new title")
    task_name, document_name = paths.output_names(settings["timestamp"], settings["original_filename"], chosen_title)
    new_dir = task_dir.parent / task_name
    new_doc = task_dir / document_name
    if new_dir != task_dir and new_dir.exists():
        raise DeliveryError("task rename target already exists: %s" % new_dir)
    if document_name != manifest["artifacts"]["document"] and new_doc.exists():
        raise DeliveryError("document rename target already exists: %s" % new_doc)
    if copy_directory is not None:
        settings["copy_directory"] = str(Path(copy_directory).expanduser().resolve())
    settings.update(name_decision=decision, title=chosen_title, status="naming")
    settings["rename_pending"] = {
        "old_directory": str(task_dir), "new_directory": str(new_dir),
        "old_document": manifest["artifacts"]["document"], "new_document": document_name,
    }
    state.save_manifest(task_dir, manifest)
    task_dir = recover_rename(task_dir)
    task_dir, manifest, _ = state.load_task(task_dir)
    validation = review.validate_task(task_dir, manifest)
    if not validation["complete"]:
        raise DeliveryError("renamed task failed validation: %s" % "; ".join(validation["blockers"]))
    manifest["delivery"]["status"] = "copying"
    state.save_manifest(task_dir, manifest)
    try:
        plan = _copy_plan(task_dir, manifest)
        for path, data in plan.items():
            _check_existing(path, data)  # Preflight every destination before writing any.
        for path, data in plan.items():
            _publish_file(path, data)
        receipt = {str(path): hashlib.sha256(data).hexdigest() for path, data in plan.items()}
        if not all(path.read_bytes() == data for path, data in plan.items()):
            raise DeliveryError("copy changed during verification")
    except (OSError, DeliveryError) as exc:
        manifest["delivery"]["status"] = "copy_failed"
        manifest["stage"] = "blocked_delivery"
        state.save_manifest(task_dir, manifest)
        raise DeliveryError("%s; resume finalize with --task-dir %s" % (exc, task_dir)) from exc
    settings = manifest["delivery"]
    settings.update(status="complete", files=receipt)
    manifest["stage"] = "complete"
    state.save_manifest(task_dir, manifest)
    copied = str(Path(settings["copy_directory"]) / document_name) if settings["copy_directory"] else None
    return {"task_dir": str(task_dir), "document": str(task_dir / document_name),
            "copy_document": copied, "stage": "complete", "validation": validation}
