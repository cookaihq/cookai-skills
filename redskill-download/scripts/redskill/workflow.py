import contextlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import archive, audit
from .errors import DownloadError
from .inputs import safe_component
from .network import Client, ENDPOINT


def plain(path):
    if path.is_symlink() or path.is_junction():
        raise DownloadError("linked_output", "Refusing a linked output or task path", path=str(path))


def write_json(path, data):
    plain(path)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with pending.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def read_json(path):
    plain(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise DownloadError("invalid_local_state", "Cannot read saved state", path=str(path)) from exc


@contextlib.contextmanager
def lock(path):
    plain(path)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DownloadError("task_busy", "Another process is downloading this identifier; retry when it finishes") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def clear_stage_tree(path, state_root):
    """Only remove our incomplete extraction, never a completed download or user-selected folder."""
    plain(path)
    if path.resolve().parent != state_root.resolve() or path.name != "skill":
        raise DownloadError("invalid_local_state", "Unexpected staging path")
    if path.exists():
        for child in path.rglob("*"):
            plain(child)
        shutil.rmtree(path)


def check_cooldown(state):
    if not state.get("retry_at"):
        return
    try:
        deadline = datetime.fromisoformat(state["retry_at"]).timestamp()
    except (ValueError, TypeError) as exc:
        raise DownloadError("invalid_local_state", "Saved cooldown is invalid") from exc
    if deadline > time.time():
        raise DownloadError("retry_later", "The server cooldown has not ended", retry_at=state["retry_at"])


def result(folder, metadata, report, config, reused=False):
    return {"status": "downloaded_with_missing_references" if report["missing_references"] else "downloaded",
            "identifier": metadata["identifier"], "version": metadata["version"], "sha256": metadata["sha256"],
            "sha256_verified": True, "reused": reused, "directory": str(folder),
            "skill_directory": str(folder / "skill"), "zip_path": str(folder / "bundle.zip"),
            "report_path": str(folder / "report.json"), "config": config, **report}


def run(identifier, config, *, client=None):
    safe_component(identifier, "identifier")
    output = Path(config["output_directory"])
    output.mkdir(parents=True, exist_ok=True)
    state_root = output / ".redskill-state"
    plain(state_root)
    state_root.mkdir(exist_ok=True)
    state_path = state_root / (identifier + ".json")
    with lock(state_root / (identifier + ".lock")):
        state = read_json(state_path)
        if not isinstance(state, dict):
            raise DownloadError("invalid_local_state", "Saved state is not an object")
        check_cooldown(state)
        client = client or Client()
        stage = state_root / identifier
        plain(stage)
        stage.mkdir(exist_ok=True)
        identity_root = output / identifier
        plain(identity_root)
        identity_root.mkdir(exist_ok=True)
        try:
            manifest = client.manifest(identifier)
            safe_component(manifest["version"], "version")
            metadata = {key: manifest[key] for key in ("identifier", "version", "sha256")}
            metadata["source"] = ENDPOINT + "?identifier=" + identifier
            metadata["downloaded_at"] = datetime.now(timezone.utc).isoformat()
            canonical = identity_root / manifest["version"]
            candidates = [canonical] + sorted(identity_root.glob(manifest["version"] + "--*"))
            for candidate in candidates:
                if candidate.is_dir() and archive.unchanged(candidate, manifest["sha256"]):
                    try:
                        inventory = read_json(candidate / "inventory.json")
                        saved_manifest = read_json(candidate / "manifest.json")
                        saved_report = read_json(candidate / "report.json")
                    except DownloadError:
                        # Preserve damaged or locally edited records in the old copy.
                        continue
                    if (not isinstance(inventory, dict) or not isinstance(inventory.get("files"), list)
                            or not isinstance(saved_manifest, dict)
                            or any(saved_manifest.get(key) != metadata[key]
                                   for key in ("identifier", "version", "sha256", "source"))
                            or saved_manifest.get("zip_bytes") != (candidate / "bundle.zip").stat().st_size):
                        continue
                    # Re-read every original file; user edits to generated records do not stand in for verification.
                    files = [{"path": p.relative_to(candidate / "skill").as_posix(), "bytes": p.stat().st_size,
                              "sha256": archive.sha256(p)} for p in (candidate / "skill").rglob("*") if p.is_file()]
                    if (any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                            for item in inventory["files"])
                            or sorted(inventory["files"], key=lambda item: item["path"])
                            != sorted(files, key=lambda item: item["path"])):
                        continue
                    report = audit.audit(candidate / "skill", files)
                    report["file_count"] = len(files)
                    report_path = candidate / "report.json"
                    if not report_path.exists():
                        # The report is derived data. Restore it without changing the author's files.
                        write_json(report_path, report)
                    elif saved_report != report:
                        continue
                    write_json(state_path, {"stage": "complete", "directory": str(candidate), **metadata})
                    return result(candidate, metadata, report, config, True)
            write_json(state_path, {"stage": "manifest_received", **metadata})
            bundle = stage / "bundle.zip"
            plain(bundle)
            if not bundle.is_file() or archive.sha256(bundle) != manifest["sha256"]:
                part = stage / "bundle.part"
                plain(part)
                size, actual = client.download(manifest["zip_url"], part)
                if actual != manifest["sha256"]:
                    part.unlink(missing_ok=True)
                    raise DownloadError("sha256_mismatch", "Downloaded ZIP does not match the official SHA256")
                part.replace(bundle)
            else:
                size = bundle.stat().st_size
            write_json(state_path, {"stage": "zip_verified", **metadata})
            tree = stage / "skill"
            clear_stage_tree(tree, stage)
            files = archive.extract(bundle, tree)
            report = audit.audit(tree, files)
            if not report["skill_entrypoints"]:
                raise DownloadError("skill_entrypoint_missing", "The bundle has no SKILL.md; verified ZIP retained for inspection", zip_path=str(bundle))
            report["file_count"] = len(files)
            metadata["zip_bytes"] = size
            write_json(stage / "manifest.json", metadata)
            write_json(stage / "inventory.json", {"files": files})
            write_json(stage / "report.json", report)
            # Publish using a new directory name. Existing versions and local modifications remain untouched.
            destination = canonical
            if destination.exists() or destination.is_symlink():
                destination = identity_root / (manifest["version"] + "--" + manifest["sha256"][:12])
            if destination.exists() or destination.is_symlink():
                destination = identity_root / (manifest["version"] + "--" + manifest["sha256"][:12] + "--" + uuid.uuid4().hex[:12])
            # Same-tool writers are serialized by the OS lock; arbitrary external writers are not covered.
            stage.rename(destination)
            write_json(state_path, {"stage": "complete", "directory": str(destination), **metadata})
            return result(destination, metadata, report, config)
        except DownloadError as exc:
            previous = read_json(state_path)
            previous.update({"stage": "interrupted", "error": exc.code})
            if exc.details.get("retry_at"):
                previous["retry_at"] = exc.details["retry_at"]
            write_json(state_path, previous)
            exc.details["resume"] = "Repeat the same download request with the same workspace and output directory"
            exc.details["state_path"] = str(state_path)
            raise
