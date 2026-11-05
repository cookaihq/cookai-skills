"""Inspect/apply a pinned core without overwriting customizations or business files."""
from _runtime_bootstrap import ensure

ensure()

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "runtime" / "skill_ui"
VERSION = re.search(r'^  version: "([^"]+)"$', (ROOT / "SKILL.md").read_text(encoding="utf-8"), re.M)[1]


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.name != ".vendor.json"}


def inspect(target):
    for path in (target, *target.parents):
        if path.is_symlink():
            raise ValueError("Target directory ancestry must not contain symlinks")
    expected = files(SOURCE)
    manifest = target / ".vendor.json"
    existing = files(target) if target.exists() else {}
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    conflicts = []
    if previous:
        if previous.get("package") != "skill-configuration-and-artifacts-ui" or not isinstance(previous.get("files"), dict):
            raise ValueError("Unrecognized vendor manifest")
        old = previous["files"]
        conflicts = sorted(name for name in set(existing) | set(old) if existing.get(name) != old.get(name))
    elif existing or target.exists():
        conflicts = ["unmanaged destination"]
    if target.exists() and any(p.is_symlink() for p in target.rglob("*")):
        conflicts.append("symlink in destination")
    return {"package": "skill-configuration-and-artifacts-ui", "version": VERSION, "files": expected}, {
        "target": str(target), "current_version": previous.get("version") if previous else None, "version": VERSION,
        "status": "conflict" if conflicts else "current" if existing == expected and previous else "ready",
        "conflicts": conflicts, "changed": sorted(name for name in set(existing) | set(expected) if existing.get(name) != expected.get(name))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="Dedicated skill_ui package directory")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--staging-dir", type=Path, help="Required with --apply; ignored task tmp on same filesystem")
    args = parser.parse_args()
    target = args.target.absolute()
    if SOURCE.resolve() == target.resolve() or SOURCE.resolve() in target.resolve().parents:
        raise ValueError("Cannot vendor into the source package")
    manifest, report = inspect(target)
    if args.apply and report["status"] == "ready":
        if not args.staging_dir:
            raise ValueError("--apply requires --staging-dir")
        staging = args.staging_dir.resolve() / ("vendor-" + uuid.uuid4().hex)
        if target.resolve() == staging or target.resolve() in staging.parents:
            raise ValueError("Staging must be outside destination")
        staging.mkdir(parents=True)
        candidate, backup = staging / "candidate", staging / "previous"
        shutil.copytree(SOURCE, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (candidate / ".vendor.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # Recheck immediately before rename. A concurrent writer must still use the same integration lock/protocol.
        if inspect(target)[1] != report:
            raise ValueError("Destination changed while preparing update")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rename(backup)
        try:
            candidate.rename(target)
        except Exception:
            if backup.exists():
                backup.rename(target)
            raise
        report.update(status="applied", recovery=str(staging))
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["status"] == "conflict" else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        sys.exit(1)
