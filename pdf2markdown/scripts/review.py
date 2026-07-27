from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath

import bundle
import conversion_actions
import correction
import markdown_assets
import markdown_structure
import raw_conversion


SCHEMA_VERSION = 1
MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_CORRECTION_ARTIFACT_BYTES = 128 * 1024 * 1024
DIALECT = "gfm+github-dollar-math"
CHECK_CATEGORIES = frozenset(
    {
        "text",
        "hierarchy",
        "reading_order",
        "tables",
        "formulas",
        "footnotes",
        "links",
        "captions",
        "images",
    }
)


class ReviewError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _bounded_artifact_bytes(value: bytes, *, artifact: str) -> bytes:
    if len(value) > MAX_RECORD_BYTES:
        raise ReviewError(
            "review_size_limit",
            f"The serialized {artifact} exceeds the durable review artifact limit.",
        )
    return value


def _bounded_json_bytes(value: dict, *, artifact: str) -> bytes:
    return _bounded_artifact_bytes(_json_bytes(value), artifact=artifact)


def _current_schema(value: dict) -> bool:
    return type(value.get("schema_version")) is int and value["schema_version"] == SCHEMA_VERSION


def object_hash(value: dict) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_slug(manifest: dict) -> str:
    stem = Path(manifest["source"]["original_name"]).stem.lower()
    value = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return (value or "document")[:200].rstrip("-")


def _read_regular_file(
    name: str,
    *,
    dir_fd: int,
    max_bytes: int,
    error_code: str = "integrity_violation",
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ReviewError(error_code, "A review input is missing or unsafe.") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > max_bytes
        ):
            raise ReviewError(
                error_code, "A review input is not a bounded private regular file."
            )
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ReviewError(error_code, "A review input exceeds its read limit.")
        final = os.fstat(descriptor)
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise ReviewError(error_code, "A review input changed while it was read.") from exc
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise ReviewError(error_code, "A review input changed while it was read.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_directory(name: str, *, dir_fd: int) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ReviewError("integrity_violation", "A review path is missing or unsafe.") from exc
    opened = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise ReviewError("integrity_violation", "A review path is not a private directory.")
    return descriptor


def _read_bundle_path(path: str, *, root_fd: int, max_bytes: int) -> bytes:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise ReviewError("integrity_violation", "A review artifact path is unsafe.")
    current = os.dup(root_fd)
    try:
        for component in parsed.parts[:-1]:
            child = _open_directory(component, dir_fd=current)
            os.close(current)
            current = child
        return _read_regular_file(parsed.parts[-1], dir_fd=current, max_bytes=max_bytes)
    finally:
        os.close(current)


def _write_exclusive(name: str, data: bytes, *, dir_fd: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise ReviewError(
            "integrity_violation", "A review staging file already exists or is unsafe."
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(dir_fd)


def _promote(temporary_name: str, final_name: str, *, dir_fd: int) -> None:
    try:
        os.stat(final_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ReviewError("integrity_violation", "A review artifact already exists.")
    try:
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
    except OSError as exc:
        raise ReviewError("integrity_violation", "A review artifact could not be promoted.") from exc
    os.fsync(dir_fd)


def _pandoc_dependency(manifest: dict) -> dict:
    dependencies = manifest.get("preflight", {}).get("dependencies", [])
    matches = [
        item
        for item in dependencies
        if isinstance(item, dict)
        and item.get("name") == "pandoc"
        and item.get("available") is True
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("executable"), str)
        or not matches[0]["executable"]
        or not isinstance(matches[0].get("version"), str)
        or not matches[0]["version"]
        or not isinstance(matches[0].get("executable_identity"), dict)
    ):
        raise ReviewError(
            "dependency_missing", "The reviewed Markdown parser is unavailable."
        )
    return matches[0]


def _parse_markdown(
    markdown: bytes,
    *,
    manifest: dict,
    environ: dict[str, str],
    validated_local_targets=(),
) -> dict:
    dependency = _pandoc_dependency(manifest)
    try:
        return markdown_structure.analyze(
            markdown,
            pandoc_executable=dependency["executable"],
            environ=environ,
            expected_version=dependency["version"],
            expected_executable_identity=dependency["executable_identity"],
            validated_local_targets=validated_local_targets,
        )
    except markdown_structure.MarkdownStructureError as exc:
        raise ReviewError(exc.code, exc.message) from None


def _target_descriptor(
    manifest: dict, markdown: bytes, structure: dict, *, local_resources: dict
) -> dict:
    raw = manifest["raw_conversion"]
    return {
        "kind": "raw_conversion",
        "attempt_id": raw["attempt_id"],
        "path": raw["main_markdown_path"],
        "sha256": raw["main_markdown_sha256"],
        "size_bytes": len(markdown),
        "provenance": {
            "source_sha256": manifest["source"]["sha256"],
            "archive_sha256": raw["archive_sha256"],
            "tree_sha256": raw["tree_sha256"],
        },
        "dialect": structure["dialect"],
        "semantic_hash": structure["semantic_hash"],
        "normalized_source_sha256": structure["normalized_source_sha256"],
        "content_index_sha256": structure["content_index_sha256"],
        "structure_evidence_sha256": structure["structure_evidence_sha256"],
        "coordinate_space": structure["coordinate_space"],
        "parser_profile": structure["parser_profile"],
        "lexical_profile": structure["lexical_profile"],
        "html_profile": structure["html_profile"],
        "validated_local_targets": deepcopy(
            structure.get("validated_local_targets", [])
        ),
        "local_resources": deepcopy(local_resources),
    }


def _corrected_target_descriptor(
    manifest: dict,
    markdown: bytes,
    structure: dict,
    *,
    correction_id: str,
    path: str,
    source_target: dict,
    local_resources: dict,
) -> dict:
    return {
        "kind": "corrected_markdown",
        "correction_id": correction_id,
        "path": path,
        "sha256": _bytes_hash(markdown),
        "size_bytes": len(markdown),
        "provenance": {
            "source_sha256": manifest["source"]["sha256"],
            "raw_markdown_sha256": manifest["raw_conversion"][
                "main_markdown_sha256"
            ],
            "previous_target_sha256": source_target["sha256"],
        },
        "dialect": structure["dialect"],
        "semantic_hash": structure["semantic_hash"],
        "normalized_source_sha256": structure["normalized_source_sha256"],
        "content_index_sha256": structure["content_index_sha256"],
        "structure_evidence_sha256": structure["structure_evidence_sha256"],
        "coordinate_space": structure["coordinate_space"],
        "parser_profile": structure["parser_profile"],
        "lexical_profile": structure["lexical_profile"],
        "html_profile": structure["html_profile"],
        "validated_local_targets": deepcopy(
            structure.get("validated_local_targets", [])
        ),
        "local_resources": deepcopy(local_resources),
    }


def _review_evidence(
    *,
    manifest: dict,
    target: dict,
    structure: dict,
    round_id: str,
    prior_rounds: list[dict],
    coverage_mode: str,
    follow_up_requirements: dict,
) -> dict:
    baseline = _baseline_descriptor(manifest)
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "round_id": round_id,
        "target": deepcopy(target),
        "baseline": baseline,
        "markdown_blocks": deepcopy(structure["blocks"]),
        "markdown_block_boundaries": deepcopy(structure["boundaries"]),
        "segment_boundary_policy": {
            "kind": "ordered_adjacent_review_segments",
            "identity_fields": ["before_segment_id", "after_segment_id"],
            "ordering": [
                "source_pages.start",
                "source_pages.end",
                "markdown_blocks.first_index",
                "markdown_blocks.last_index",
                "segment_id",
            ],
        },
        "prior_rounds": deepcopy(prior_rounds),
        "coverage_mode": coverage_mode,
        "follow_up_requirements": deepcopy(follow_up_requirements),
        "structural_validation": {
            "status": "pass" if structure["status"] == "pass" else "blocked",
            "dialect": structure["dialect"],
            "parser_profile": target["parser_profile"],
            "lexical_profile": target["lexical_profile"],
            "html_profile": target["html_profile"],
            "coordinate_space": structure["coordinate_space"],
            "pandoc": deepcopy(structure["pandoc"]),
            "structure_evidence_sha256": structure[
                "structure_evidence_sha256"
            ],
            "issues": deepcopy(structure["issues"]),
        },
    }
    evidence["coverage_basis_sha256"] = object_hash(
        {
            "pages": [item["page_number"] for item in baseline["page_references"]],
            "blocks": [item["block_id"] for item in evidence["markdown_blocks"]],
            "segment_boundary_policy": evidence["segment_boundary_policy"],
            "coverage_mode": coverage_mode,
            "follow_up_requirements": follow_up_requirements,
            "prior_review_sha256": (
                None
                if not prior_rounds
                else object_hash({"round": prior_rounds[-1]})
            ),
        }
    )
    return evidence


def _baseline_descriptor(manifest: dict) -> dict:
    preflight = manifest["preflight"]
    return {
        "source_pdf": {
            "path": manifest["source"]["physical_path"],
            "sha256": manifest["source"]["sha256"],
            "size_bytes": manifest["source"]["size_bytes"],
        },
        "source_inventory": {
            "path": manifest["artifacts"]["source_inventory"],
            "sha256": preflight["inventory_sha256"],
        },
        "page_references": deepcopy(preflight["page_references"]),
    }


def _desired_open_state(
    manifest: dict,
    private_state: dict,
    *,
    review_state: dict,
    evidence_path: str,
    generation: int,
) -> tuple[dict, dict]:
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = generation
    desired_manifest["conversion_state"] = "review_pending"
    desired_manifest["review"] = deepcopy(review_state)
    desired_manifest["artifacts"]["review_evidence"] = evidence_path
    desired_private = deepcopy(private_state)
    desired_private["generation"] = generation
    return desired_manifest, desired_private


def open_review(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    environ: dict[str, str],
    visual_capability: str,
    expected_generation: int,
    at: str,
) -> dict:
    if manifest.get("generation") != expected_generation:
        raise ReviewError("generation_conflict", "The work bundle generation changed.")
    previous_review = manifest.get("review")
    initial_review = manifest.get("conversion_state") == "converted" and previous_review is None
    continuing_review = (
        manifest.get("conversion_state") == "awaiting_user"
        and isinstance(previous_review, dict)
        and previous_review.get("status") == "review_incomplete"
        and previous_review.get("reason_code") == "review_incomplete"
    )
    if not initial_review and not continuing_review:
        raise ReviewError("review_state_mismatch", "The work bundle is not ready for review.")
    if visual_capability != "available":
        raise ReviewError(
            "dependency_missing", "Native visual review capability is unavailable."
        )
    raw_conversion.validate_committed_artifacts(
        descriptors=descriptors, manifest=manifest
    )
    previous_document = None
    previous_rounds = []
    previous_summaries = []
    previous_coverage = None
    if continuing_review:
        validate_committed_artifacts(
            descriptors=descriptors,
            manifest=manifest,
            bundle_root=bundle_root,
        )
        previous_path = manifest.get("artifacts", {}).get("review")
        if not isinstance(previous_path, str):
            raise ReviewError(
                "integrity_violation", "The previous review artifact is unavailable."
            )
        previous_bytes = _read_bundle_path(
            previous_path,
            root_fd=descriptors["root"],
            max_bytes=MAX_RECORD_BYTES,
        )
        try:
            previous_document = bundle.decode_json_object(previous_bytes)
        except bundle.BundleStateError as exc:
            raise ReviewError(
                "integrity_violation", "The previous review artifact is invalid."
            ) from exc
        if (
            previous_document.get("status") != "review_incomplete"
            or not isinstance(previous_document.get("rounds"), list)
            or not previous_document["rounds"]
            or previous_document.get("coverage") != previous_review.get("coverage")
        ):
            raise ReviewError(
                "integrity_violation", "The previous review progress is inconsistent."
            )
        previous_rounds = deepcopy(previous_document["rounds"])
        previous_summaries = deepcopy(previous_review["rounds"])
        previous_coverage = deepcopy(previous_review["coverage"])
    expected_target = previous_review.get("target") if continuing_review else None
    target_path = (
        expected_target.get("path")
        if isinstance(expected_target, dict)
        else manifest["raw_conversion"]["main_markdown_path"]
    )
    if not isinstance(target_path, str):
        raise ReviewError("integrity_violation", "The review target path is invalid.")
    markdown = _read_bundle_path(
        target_path,
        root_fd=descriptors["root"],
        max_bytes=MAX_MARKDOWN_BYTES,
    )
    expected_sha256 = (
        expected_target.get("sha256")
        if isinstance(expected_target, dict)
        else manifest["raw_conversion"]["main_markdown_sha256"]
    )
    if _bytes_hash(markdown) != expected_sha256:
        raise ReviewError("integrity_violation", "The review target Markdown changed.")
    structure = _parse_markdown(
        markdown,
        manifest=manifest,
        environ=environ,
        validated_local_targets=(
            expected_target.get("validated_local_targets", [])
            if isinstance(expected_target, dict)
            else ()
        ),
    )
    local_resources = None
    if not continuing_review:
        raw = manifest["raw_conversion"]
        raw_root = f"03-converted/attempts/{raw['attempt_id']}/raw"
        try:
            inspected_resources = markdown_assets.rebase_local_references(
                markdown,
                bundle_root=bundle_root,
                source_markdown_path=target_path,
                destination_markdown_path=target_path,
                expected_references=structure["resource_references"],
                allowed_bundle_root=raw_root,
            )
        except markdown_assets.MarkdownAssetError as exc:
            raise ReviewError(exc.code, exc.message) from None
        local_resources = {
            "schema_version": SCHEMA_VERSION,
            "markdown_path": target_path,
            "markdown_sha256": "sha256:" + _bytes_hash(markdown),
            "oracle": deepcopy(structure["resource_references"]),
            "reference_count": sum(
                item["count"] for item in inspected_resources["references"]
            ),
            "references": deepcopy(inspected_resources["references"]),
        }
    if not continuing_review or expected_target.get("kind") == "raw_conversion":
        target = _target_descriptor(
            manifest,
            markdown,
            structure,
            local_resources=(
                expected_target["local_resources"]
                if continuing_review
                else local_resources
            ),
        )
    elif expected_target.get("kind") == "corrected_markdown":
        matching_corrections = [
            item
            for item in manifest.get("corrections", [])
            if isinstance(item, dict)
            and item.get("correction_id") == expected_target.get("correction_id")
            and item.get("corrected_markdown", {}).get("path") == target_path
        ]
        if len(matching_corrections) != 1:
            raise ReviewError(
                "integrity_violation", "The corrected review target is unavailable."
            )
        target = _corrected_target_descriptor(
            manifest,
            markdown,
            structure,
            correction_id=expected_target["correction_id"],
            path=target_path,
            source_target=matching_corrections[0]["source_target"],
            local_resources=expected_target["local_resources"],
        )
    else:
        raise ReviewError("integrity_violation", "The review target kind is invalid.")
    baseline = _baseline_descriptor(manifest)
    if continuing_review and (
        target != previous_review.get("target")
        or target != previous_document.get("target")
    ):
        raise ReviewError(
            "dependency_changed", "The review target or parser profile changed."
        )
    round_number = len(previous_summaries) + 1
    round_id = f"review-round-{round_number:04d}"
    evidence_path = f"04-review/review-evidence-round-{round_number:04d}.json"
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "round_id": round_id,
        "target": target,
        "baseline": baseline,
        "markdown_blocks": deepcopy(structure["blocks"]),
        "markdown_block_boundaries": deepcopy(structure["boundaries"]),
        "segment_boundary_policy": {
            "kind": "ordered_adjacent_review_segments",
            "identity_fields": ["before_segment_id", "after_segment_id"],
            "ordering": [
                "source_pages.start",
                "source_pages.end",
                "markdown_blocks.first_index",
                "markdown_blocks.last_index",
                "segment_id",
            ],
        },
        "prior_rounds": previous_rounds,
        "structural_validation": {
            "status": (
                "pass" if structure["status"] == "pass" else "blocked"
            ),
            "dialect": structure["dialect"],
            "parser_profile": target["parser_profile"],
            "lexical_profile": target["lexical_profile"],
            "html_profile": target["html_profile"],
            "coordinate_space": structure["coordinate_space"],
            "pandoc": deepcopy(structure["pandoc"]),
            "structure_evidence_sha256": structure[
                "structure_evidence_sha256"
            ],
            "issues": deepcopy(structure["issues"]),
        },
    }
    evidence["coverage_basis_sha256"] = object_hash(
        {
            "pages": [item["page_number"] for item in baseline["page_references"]],
            "blocks": [item["block_id"] for item in evidence["markdown_blocks"]],
            "segment_boundary_policy": evidence["segment_boundary_policy"],
            "prior_review_sha256": (
                None
                if not previous_summaries
                else previous_summaries[-1]["review_sha256"]
            ),
        }
    )
    initial_coverage = previous_coverage or {
        "source_pages": {
            "covered": 0,
            "required": len(baseline["page_references"]),
            "complete": False,
        },
        "markdown_blocks": {
            "covered": 0,
            "required": len(evidence["markdown_blocks"]),
            "complete": False,
        },
        "boundaries": {"covered": 0, "required": 0, "complete": True},
    }
    evidence_bytes = _bounded_json_bytes(evidence, artifact="review evidence")
    evidence_hash = "sha256:" + _bytes_hash(evidence_bytes)
    action_id = f"review-{uuid.uuid4().hex}"
    new_generation = expected_generation + 1
    pending_action = {
        "kind": "record_review",
        "action_id": action_id,
        "generation": new_generation,
        "evidence_hash": evidence_hash,
    }
    review_state = {
        "schema_version": SCHEMA_VERSION,
        "status": "review_pending",
        "reason_code": None,
        "target": target,
        "evidence": {
            "path": evidence_path,
            "sha256": evidence_hash,
            "size_bytes": len(evidence_bytes),
            "coverage_basis_sha256": evidence["coverage_basis_sha256"],
        },
        "coverage": initial_coverage,
        "rounds": previous_summaries,
        "pending_action": pending_action,
    }
    desired_manifest, desired_private = _desired_open_state(
        manifest,
        private_state,
        review_state=review_state,
        evidence_path=review_state["evidence"]["path"],
        generation=new_generation,
    )
    operation_id = f"review-open-{uuid.uuid4().hex}"
    temporary_name = f".{operation_id}.evidence.part"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_open_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
        "previous_review": deepcopy(previous_review),
        "review": review_state,
        "evidence": evidence,
        "evidence_temporary_name": temporary_name,
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    _write_exclusive(temporary_name, evidence_bytes, dir_fd=descriptors["review"])
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_open_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "intent_hash": object_hash(intent),
        "evidence_sha256": evidence_hash,
        "evidence_size_bytes": len(evidence_bytes),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    _promote(
        temporary_name,
        PurePosixPath(evidence_path).name,
        dir_fd=descriptors["review"],
    )
    bundle.atomic_write_json("private.json", desired_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", desired_manifest, dir_fd=descriptors["root"])
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_open_committed",
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return desired_manifest


def _entry_kind(name: str, *, dir_fd: int) -> str | None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "other"


def _remove_owned_temporary(
    name: str, *, dir_fd: int, max_bytes: int = MAX_RECORD_BYTES
) -> None:
    kind = _entry_kind(name, dir_fd=dir_fd)
    if kind is None:
        return
    if kind != "file":
        raise ReviewError(
            "integrity_violation", "A pending review staging path is unsafe."
        )
    data = _read_regular_file(name, dir_fd=dir_fd, max_bytes=max_bytes)
    del data
    os.unlink(name, dir_fd=dir_fd)
    os.fsync(dir_fd)


def _ensure_private_directory(name: str, *, dir_fd: int) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except FileExistsError:
        pass
    return _open_directory(name, dir_fd=dir_fd)


def _correction_artifact_path(value: str) -> PurePosixPath:
    parsed = PurePosixPath(value) if isinstance(value, str) else None
    if (
        not isinstance(parsed, PurePosixPath)
        or parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or (len(parsed.parts) != 1 and parsed.parts[:-1] != ("assets",))
        or (len(parsed.parts) == 2 and parsed.suffix != ".png")
    ):
        raise ReviewError(
            "integrity_violation", "A correction artifact path is unsafe."
        )
    return parsed


def _ensure_correction_artifact_parents(paths, *, review_fd: int) -> None:
    if any(len(_correction_artifact_path(path).parts) == 2 for path in paths):
        assets_fd = _ensure_private_directory("assets", dir_fd=review_fd)
        os.close(assets_fd)


def _correction_artifact_parent(path: str, *, review_fd: int) -> tuple[int, str]:
    parsed = _correction_artifact_path(path)
    if len(parsed.parts) == 1:
        return os.dup(review_fd), parsed.name
    return _open_directory("assets", dir_fd=review_fd), parsed.name


def _correction_artifact_kind(path: str, *, review_fd: int) -> str | None:
    parent_fd, name = _correction_artifact_parent(path, review_fd=review_fd)
    try:
        return _entry_kind(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _read_correction_artifact(
    path: str, *, review_fd: int, max_bytes: int
) -> bytes:
    parent_fd, name = _correction_artifact_parent(path, review_fd=review_fd)
    try:
        return _read_regular_file(name, dir_fd=parent_fd, max_bytes=max_bytes)
    finally:
        os.close(parent_fd)


def _promote_correction_artifact(
    temporary_name: str, final_path: str, *, review_fd: int
) -> None:
    parsed = _correction_artifact_path(final_path)
    if len(parsed.parts) == 1:
        _promote(temporary_name, parsed.name, dir_fd=review_fd)
        return
    assets_fd = _open_directory("assets", dir_fd=review_fd)
    try:
        if _entry_kind(parsed.name, dir_fd=assets_fd) is not None:
            raise ReviewError(
                "integrity_violation", "A correction artifact already exists."
            )
        try:
            os.rename(
                temporary_name,
                parsed.name,
                src_dir_fd=review_fd,
                dst_dir_fd=assets_fd,
            )
        except OSError as exc:
            raise ReviewError(
                "integrity_violation", "A correction artifact could not be promoted."
            ) from exc
        os.fsync(assets_fd)
        os.fsync(review_fd)
    finally:
        os.close(assets_fd)


def _open_prefix_from_state(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    review_state = intent.get("review")
    previous_review = intent.get("previous_review")
    continuing_review = isinstance(previous_review, dict)
    if (
        type(expected_generation) is not int
        or new_generation != expected_generation + 1
        or not isinstance(review_state, dict)
    ):
        raise ReviewError("integrity_violation", "A pending review intent is invalid.")
    if (
        manifest.get("generation") == expected_generation
        and (
            (
                not continuing_review
                and manifest.get("conversion_state") == "converted"
                and "review" not in manifest
            )
            or (
                continuing_review
                and manifest.get("conversion_state") == "awaiting_user"
                and manifest.get("review") == previous_review
                and previous_review.get("status") == "review_incomplete"
            )
        )
    ):
        prefix_manifest = deepcopy(manifest)
    elif (
        manifest.get("generation") == new_generation
        and manifest.get("conversion_state") == "review_pending"
        and manifest.get("review") == review_state
    ):
        prefix_manifest = deepcopy(manifest)
        prefix_manifest["generation"] = expected_generation
        if continuing_review:
            prefix_manifest["conversion_state"] = "awaiting_user"
            prefix_manifest["review"] = deepcopy(previous_review)
            prefix_manifest["artifacts"]["review_evidence"] = previous_review[
                "evidence"
            ]["path"]
        else:
            prefix_manifest["conversion_state"] = "converted"
            prefix_manifest.pop("review", None)
            prefix_manifest["artifacts"].pop("review_evidence", None)
    else:
        raise ReviewError(
            "integrity_violation", "A pending review manifest is inconsistent."
        )
    prefix_private = deepcopy(private_state)
    if prefix_private.get("generation") not in {expected_generation, new_generation}:
        raise ReviewError(
            "integrity_violation", "A pending review private state is inconsistent."
        )
    if (
        manifest.get("generation") == new_generation
        and private_state.get("generation") == expected_generation
    ):
        raise ReviewError(
            "integrity_violation", "A pending review commit is ordered unsafely."
        )
    prefix_private["generation"] = expected_generation
    if (
        object_hash(prefix_manifest) != intent.get("previous_manifest_hash")
        or object_hash(prefix_private) != intent.get("previous_private_hash")
    ):
        raise ReviewError(
            "integrity_violation", "A pending review prefix hash is invalid."
        )
    return prefix_manifest, prefix_private


def _valid_open_intent(intent: dict) -> bool:
    review_state = intent.get("review") if isinstance(intent, dict) else None
    evidence = intent.get("evidence") if isinstance(intent, dict) else None
    temporary_name = (
        intent.get("evidence_temporary_name") if isinstance(intent, dict) else None
    )
    operation_id = intent.get("operation_id") if isinstance(intent, dict) else None
    evidence_record = review_state.get("evidence") if isinstance(review_state, dict) else None
    evidence_bytes = _json_bytes(evidence) if isinstance(evidence, dict) else b""
    previous_review = intent.get("previous_review") if isinstance(intent, dict) else None
    round_id = evidence.get("round_id") if isinstance(evidence, dict) else None
    return (
        isinstance(intent, dict)
        and _current_schema(intent)
        and intent.get("event") == "review_open_intent"
        and isinstance(operation_id, str)
        and re.fullmatch(r"review-open-[0-9a-f]{32}", operation_id) is not None
        and isinstance(temporary_name, str)
        and temporary_name == f".{operation_id}.evidence.part"
        and isinstance(review_state, dict)
        and review_state.get("status") == "review_pending"
        and isinstance(evidence, dict)
        and isinstance(round_id, str)
        and re.fullmatch(r"review-round-[0-9]{4}", round_id) is not None
        and isinstance(evidence.get("prior_rounds"), list)
        and (
            previous_review is None
            or (
                isinstance(previous_review, dict)
                and previous_review.get("status") == "review_incomplete"
                and review_state.get("rounds") == previous_review.get("rounds")
                and evidence["prior_rounds"]
            )
        )
        and isinstance(evidence_record, dict)
        and evidence_record.get("path")
        == f"04-review/review-evidence-round-{round_id[-4:]}.json"
        and evidence_record.get("sha256")
        == "sha256:" + _bytes_hash(evidence_bytes)
        and evidence_record.get("size_bytes") == len(evidence_bytes)
        and intent.get("new_generation") == intent.get("expected_generation") + 1
    )


def _valid_open_prepared(prepared: dict, intent: dict) -> bool:
    evidence_bytes = _json_bytes(intent["evidence"])
    return (
        isinstance(prepared, dict)
        and _current_schema(prepared)
        and prepared.get("event") == "review_open_prepared"
        and prepared.get("operation_id") == intent.get("operation_id")
        and prepared.get("expected_generation") == intent.get("expected_generation")
        and prepared.get("new_generation") == intent.get("new_generation")
        and prepared.get("at") == intent.get("at")
        and prepared.get("intent_hash") == object_hash(intent)
        and prepared.get("evidence_sha256")
        == "sha256:" + _bytes_hash(evidence_bytes)
        and prepared.get("evidence_size_bytes") == len(evidence_bytes)
    )


def _record_desired_state(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict] | None:
    review_state = intent.get("review") if isinstance(intent, dict) else None
    review_status = review_state.get("status") if isinstance(review_state, dict) else None
    if review_status not in {
        "local_complete",
        "review_incomplete",
        "correction_required",
        "review_ambiguity",
    }:
        return None
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = {
        "local_complete": "local_complete",
        "correction_required": "review_pending",
        "review_incomplete": "awaiting_user",
        "review_ambiguity": "awaiting_user",
    }[review_status]
    final = deepcopy(intent.get("final_markdown"))
    if (review_status == "local_complete") != isinstance(final, dict):
        return None
    desired_manifest["final_markdown"] = final
    desired_manifest["review"] = deepcopy(review_state)
    review_path = intent.get("review_path")
    report_path = intent.get("report_path")
    if not all(
        isinstance(path, str)
        and PurePosixPath(path).parent == PurePosixPath("04-review")
        for path in (review_path, report_path)
    ):
        return None
    desired_manifest["artifacts"].update(
        {"review": review_path, "review_report": report_path}
    )
    if intent.get("request_kind") == "review_decision":
        desired_manifest["artifacts"]["review_decision"] = review_path
    if final is not None:
        desired_manifest["artifacts"]["final_markdown"] = final.get("path")
    else:
        desired_manifest["artifacts"].pop("final_markdown", None)
    desired_private = deepcopy(private_state)
    desired_private["generation"] = intent["new_generation"]
    return desired_manifest, desired_private


def _record_prefix_from_state(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    request_kind = intent.get("request_kind", "review")
    previous_review = intent.get("previous_review")
    prefix_conversion_state = (
        "awaiting_user" if request_kind == "review_decision" else "review_pending"
    )
    prefix_review_status = (
        "review_ambiguity" if request_kind == "review_decision" else "review_pending"
    )
    if type(expected_generation) is not int or new_generation != expected_generation + 1:
        raise ReviewError("integrity_violation", "A pending review record is invalid.")
    if (
        manifest.get("generation") == expected_generation
        and manifest.get("conversion_state") == prefix_conversion_state
        and manifest.get("review") == previous_review
    ):
        prefix_manifest = deepcopy(manifest)
    elif manifest.get("generation") == new_generation:
        desired_review = intent.get("review")
        desired_status = (
            desired_review.get("status") if isinstance(desired_review, dict) else None
        )
        expected_state = {
            "local_complete": "local_complete",
            "correction_required": "review_pending",
            "review_incomplete": "awaiting_user",
            "review_ambiguity": "awaiting_user",
        }.get(desired_status)
        if manifest.get("conversion_state") != expected_state or manifest.get(
            "review"
        ) != desired_review:
            raise ReviewError(
                "integrity_violation", "A pending review result is inconsistent."
            )
        prefix_manifest = deepcopy(manifest)
        prefix_manifest["generation"] = expected_generation
        prefix_manifest["conversion_state"] = prefix_conversion_state
        prefix_manifest["final_markdown"] = None
        if (
            not isinstance(previous_review, dict)
            or previous_review.get("status") != prefix_review_status
            or previous_review.get("pending_action", {}).get("action_id")
            != intent.get("action_id")
            or previous_review.get("pending_action", {}).get("evidence_hash")
            != intent.get("evidence_hash")
        ):
            raise ReviewError(
                "integrity_violation", "A pending review prefix is invalid."
            )
        prefix_manifest["review"] = deepcopy(previous_review)
        previous_artifacts = intent.get("previous_artifacts")
        if not isinstance(previous_artifacts, dict):
            raise ReviewError(
                "integrity_violation", "A pending review prefix is invalid."
            )
        for key in set(intent.get("previous_artifacts", {})) | {
            "review",
            "review_report",
        }:
            previous_path = previous_artifacts.get(key)
            if previous_path is None:
                prefix_manifest["artifacts"].pop(key, None)
            elif isinstance(previous_path, str):
                prefix_manifest["artifacts"][key] = previous_path
            else:
                raise ReviewError(
                    "integrity_violation", "A pending review prefix is invalid."
                )
        prefix_manifest["artifacts"].pop("final_markdown", None)
    else:
        raise ReviewError(
            "integrity_violation", "A pending review record manifest is inconsistent."
        )
    prefix_private = deepcopy(private_state)
    if prefix_private.get("generation") not in {expected_generation, new_generation}:
        raise ReviewError(
            "integrity_violation", "A pending review record private state is invalid."
        )
    if (
        manifest.get("generation") == new_generation
        and private_state.get("generation") == expected_generation
    ):
        raise ReviewError(
            "integrity_violation", "A pending review record commit is ordered unsafely."
        )
    prefix_private["generation"] = expected_generation
    if (
        object_hash(prefix_manifest) != intent.get("previous_manifest_hash")
        or object_hash(prefix_private) != intent.get("previous_private_hash")
    ):
        raise ReviewError(
            "integrity_violation", "A pending review record prefix hash is invalid."
        )
    return prefix_manifest, prefix_private


def _valid_record_intent(intent: dict) -> bool:
    operation_id = intent.get("operation_id") if isinstance(intent, dict) else None
    review_document = (
        intent.get("review_document") if isinstance(intent, dict) else None
    )
    review_path = intent.get("review_path") if isinstance(intent, dict) else None
    report_path = intent.get("report_path") if isinstance(intent, dict) else None
    return (
        isinstance(intent, dict)
        and _current_schema(intent)
        and intent.get("event") == "review_record_intent"
        and isinstance(operation_id, str)
        and re.fullmatch(r"review-record-[0-9a-f]{32}", operation_id) is not None
        and intent.get("new_generation") == intent.get("expected_generation") + 1
        and isinstance(intent.get("payload"), dict)
        and intent.get("request_kind", "review")
        in {"review", "review_decision"}
        and isinstance(intent.get("review"), dict)
        and isinstance(intent.get("previous_review"), dict)
        and isinstance(intent.get("previous_artifacts"), dict)
        and isinstance(review_document, dict)
        and isinstance(review_path, str)
        and PurePosixPath(review_path).parent == PurePosixPath("04-review")
        and isinstance(report_path, str)
        and PurePosixPath(report_path).parent == PurePosixPath("04-review")
        and intent.get("review_temporary_name") == f".{operation_id}.review.part"
        and intent.get("report_temporary_name") == f".{operation_id}.report.part"
    )


def _valid_record_prepared(prepared: dict, intent: dict) -> bool:
    review_bytes = _json_bytes(intent["review_document"])
    report_bytes = _report(intent["review_document"])
    return (
        isinstance(prepared, dict)
        and _current_schema(prepared)
        and prepared.get("event") == "review_record_prepared"
        and prepared.get("operation_id") == intent.get("operation_id")
        and prepared.get("expected_generation") == intent.get("expected_generation")
        and prepared.get("new_generation") == intent.get("new_generation")
        and prepared.get("at") == intent.get("at")
        and prepared.get("intent_hash") == object_hash(intent)
        and prepared.get("review_sha256")
        == "sha256:" + _bytes_hash(review_bytes)
        and prepared.get("review_size_bytes") == len(review_bytes)
        and prepared.get("report_sha256")
        == "sha256:" + _bytes_hash(report_bytes)
        and prepared.get("report_size_bytes") == len(report_bytes)
    )


def _recover_record_operation(
    *,
    descriptors: dict,
    history: list[dict],
    index: int,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    expected_generation: int,
    at: str,
) -> dict:
    intent = history[index]
    suffix = history[index:]
    if not _valid_record_intent(intent):
        raise ReviewError(
            "integrity_violation", "A pending review record intent is invalid."
        )
    if expected_generation not in {
        intent.get("expected_generation"),
        intent.get("new_generation"),
    }:
        raise ReviewError(
            "generation_conflict", "Expected generation does not match the review record."
        )
    prefix_manifest, prefix_private = _record_prefix_from_state(
        manifest, private_state, intent
    )
    resolved_prefix = resolve_history_state(
        history[:index],
        manifest_template=prefix_manifest,
        private_template=prefix_private,
    )
    if resolved_prefix != (prefix_manifest, prefix_private):
        raise ReviewError(
            "integrity_violation", "The review record history prefix is invalid."
        )
    review_bytes = _json_bytes(intent["review_document"])
    report_bytes = _report(intent["review_document"])
    review_temporary = intent["review_temporary_name"]
    report_temporary = intent["report_temporary_name"]
    review_final = PurePosixPath(intent["review_path"]).name
    report_final = PurePosixPath(intent["report_path"]).name
    if len(suffix) == 1:
        for temporary in (review_temporary, report_temporary):
            _remove_owned_temporary(temporary, dir_fd=descriptors["review"])
        if any(
            _entry_kind(name, dir_fd=descriptors["review"]) is not None
            for name in (review_final, report_final)
        ):
            raise ReviewError(
                "integrity_violation", "An unprepared review result already exists."
            )
        _write_exclusive(
            review_temporary, review_bytes, dir_fd=descriptors["review"]
        )
        _write_exclusive(
            report_temporary, report_bytes, dir_fd=descriptors["review"]
        )
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "event": "review_record_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": intent["at"],
            "intent_hash": object_hash(intent),
            "review_sha256": "sha256:" + _bytes_hash(review_bytes),
            "review_size_bytes": len(review_bytes),
            "report_sha256": "sha256:" + _bytes_hash(report_bytes),
            "report_size_bytes": len(report_bytes),
        }
        bundle.append_history(prepared, state_fd=descriptors["state"])
    elif len(suffix) == 2 and _valid_record_prepared(suffix[1], intent):
        prepared = suffix[1]
    else:
        raise ReviewError(
            "integrity_violation", "A pending review record suffix is invalid."
        )
    for temporary, final_name, desired_bytes in (
        (review_temporary, review_final, review_bytes),
        (report_temporary, report_final, report_bytes),
    ):
        temporary_kind = _entry_kind(temporary, dir_fd=descriptors["review"])
        final_kind = _entry_kind(final_name, dir_fd=descriptors["review"])
        if temporary_kind == "file" and final_kind is None:
            if (
                _read_regular_file(
                    temporary,
                    dir_fd=descriptors["review"],
                    max_bytes=MAX_RECORD_BYTES,
                )
                != desired_bytes
            ):
                raise ReviewError(
                    "integrity_violation", "A pending review result changed."
                )
            _promote(temporary, final_name, dir_fd=descriptors["review"])
        elif temporary_kind is None and final_kind == "file":
            if (
                _read_regular_file(
                    final_name,
                    dir_fd=descriptors["review"],
                    max_bytes=MAX_RECORD_BYTES,
                )
                != desired_bytes
            ):
                raise ReviewError(
                    "integrity_violation", "A committed review result changed."
                )
            os.fsync(descriptors["review"])
        else:
            raise ReviewError(
                "integrity_violation", "A pending review result has an unsafe state."
            )
    desired = _record_desired_state(prefix_manifest, prefix_private, intent)
    if desired is None:
        raise ReviewError(
            "integrity_violation", "A pending review result state is invalid."
        )
    desired_manifest, desired_private = desired
    if manifest != prefix_manifest and manifest != desired_manifest:
        raise ReviewError(
            "integrity_violation", "A pending review result manifest is invalid."
        )
    if private_state != prefix_private and private_state != desired_private:
        raise ReviewError(
            "integrity_violation", "A pending review result private state is invalid."
        )
    validate_committed_artifacts(
        descriptors=descriptors,
        manifest=prefix_manifest,
        bundle_root=bundle_root,
    )
    if private_state == prefix_private:
        bundle.atomic_write_json(
            "private.json", desired_private, dir_fd=descriptors["state"]
        )
    if manifest == prefix_manifest:
        bundle.atomic_write_json(
            "manifest.json", desired_manifest, dir_fd=descriptors["root"]
        )
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_committed",
        "operation_id": intent["operation_id"],
        "previous_generation": intent["expected_generation"],
        "generation": intent["new_generation"],
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return {
        "event": intent["event"],
        "expected_generation": intent["expected_generation"],
        "manifest": desired_manifest,
        "private_state": desired_private,
        "intent": intent,
    }


def _correction_prefix_from_state(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict]:
    expected_generation = intent.get("expected_generation")
    new_generation = intent.get("new_generation")
    previous_review = intent.get("previous_review")
    if (
        manifest.get("generation") == expected_generation
        and manifest.get("conversion_state") == "review_pending"
        and manifest.get("review") == previous_review
    ):
        prefix_manifest = deepcopy(manifest)
    elif (
        manifest.get("generation") == new_generation
        and manifest.get("conversion_state") == "review_pending"
        and manifest.get("review") == intent.get("review")
        and manifest.get("corrections", [])
        == [*intent.get("previous_corrections", []), intent.get("correction")]
    ):
        prefix_manifest = deepcopy(manifest)
        prefix_manifest["generation"] = expected_generation
        prefix_manifest["review"] = deepcopy(previous_review)
        previous_corrections = intent.get("previous_corrections", [])
        if previous_corrections:
            prefix_manifest["corrections"] = deepcopy(previous_corrections)
        else:
            prefix_manifest.pop("corrections", None)
        for key, value in intent.get("previous_artifacts", {}).items():
            if value is None:
                prefix_manifest["artifacts"].pop(key, None)
            else:
                prefix_manifest["artifacts"][key] = value
        prefix_manifest["final_markdown"] = None
    else:
        raise ReviewError(
            "integrity_violation", "A pending correction manifest is inconsistent."
        )
    prefix_private = deepcopy(private_state)
    if prefix_private.get("generation") not in {expected_generation, new_generation}:
        raise ReviewError(
            "integrity_violation", "A pending correction private state is inconsistent."
        )
    if (
        manifest.get("generation") == new_generation
        and private_state.get("generation") == expected_generation
    ):
        raise ReviewError(
            "integrity_violation", "A pending correction commit is ordered unsafely."
        )
    prefix_private["generation"] = expected_generation
    if (
        object_hash(prefix_manifest) != intent.get("previous_manifest_hash")
        or object_hash(prefix_private) != intent.get("previous_private_hash")
    ):
        raise ReviewError(
            "integrity_violation", "A pending correction prefix hash is invalid."
        )
    return prefix_manifest, prefix_private


def _recover_correction_operation(
    *,
    descriptors: dict,
    history: list[dict],
    index: int,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    expected_generation: int,
    at: str,
) -> dict:
    intent = history[index]
    suffix = history[index:]
    if not _valid_correction_intent(intent):
        raise ReviewError(
            "integrity_violation", "A pending correction intent is invalid."
        )
    if expected_generation not in {
        intent.get("expected_generation"),
        intent.get("new_generation"),
    }:
        raise ReviewError(
            "generation_conflict", "Expected generation does not match the correction."
        )
    prefix_manifest, prefix_private = _correction_prefix_from_state(
        manifest, private_state, intent
    )
    resolved_prefix = resolve_history_state(
        history[:index],
        manifest_template=prefix_manifest,
        private_template=prefix_private,
    )
    if resolved_prefix != (prefix_manifest, prefix_private):
        raise ReviewError(
            "integrity_violation", "The correction history prefix is invalid."
        )
    temporary_names = intent["temporary_names"]
    _ensure_correction_artifact_parents(
        intent["artifact_manifest"], review_fd=descriptors["review"]
    )
    if len(suffix) == 1:
        source_markdown = _read_bundle_path(
            intent["source_target"]["path"],
            root_fd=descriptors["root"],
            max_bytes=MAX_MARKDOWN_BYTES,
        )
        if _bytes_hash(source_markdown) != intent["source_target"].get("sha256"):
            raise ReviewError("integrity_violation", "The correction target changed.")
        artifacts = _correction_artifacts(
            intent,
            source_markdown=source_markdown,
            review_document=intent["source_review_document"],
            bundle_root=bundle_root,
        )
        if _artifact_manifest(artifacts) != intent.get("artifact_manifest"):
            raise ReviewError(
                "integrity_violation", "The pending correction artifacts changed."
            )
        for final_name, temporary_name in sorted(temporary_names.items()):
            _remove_owned_temporary(
                temporary_name,
                dir_fd=descriptors["review"],
                max_bytes=intent["artifact_manifest"][final_name]["size_bytes"],
            )
            if (
                _correction_artifact_kind(
                    final_name, review_fd=descriptors["review"]
                )
                is not None
            ):
                raise ReviewError(
                    "integrity_violation", "An unprepared correction already exists."
                )
            _write_exclusive(
                temporary_name,
                artifacts[final_name],
                dir_fd=descriptors["review"],
            )
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "event": "correction_record_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": intent["at"],
            "intent_hash": object_hash(intent),
            "artifact_manifest": intent["artifact_manifest"],
            "tree_hash": object_hash({"artifacts": intent["artifact_manifest"]}),
        }
        bundle.append_history(prepared, state_fd=descriptors["state"])
    elif len(suffix) == 2 and _valid_correction_prepared(suffix[1], intent):
        prepared = suffix[1]
    else:
        raise ReviewError(
            "integrity_violation", "A pending correction suffix is invalid."
        )
    for final_name, temporary_name in sorted(temporary_names.items()):
        temporary_kind = _entry_kind(temporary_name, dir_fd=descriptors["review"])
        final_kind = _correction_artifact_kind(
            final_name, review_fd=descriptors["review"]
        )
        expected_artifact = intent["artifact_manifest"][final_name]
        if temporary_kind == "file" and final_kind is None:
            data = _read_regular_file(
                temporary_name,
                dir_fd=descriptors["review"],
                max_bytes=expected_artifact["size_bytes"],
            )
            if (
                "sha256:" + _bytes_hash(data) != expected_artifact["sha256"]
                or len(data) != expected_artifact["size_bytes"]
            ):
                raise ReviewError(
                    "integrity_violation", "A pending correction artifact changed."
                )
            _promote_correction_artifact(
                temporary_name,
                final_name,
                review_fd=descriptors["review"],
            )
        elif temporary_kind is None and final_kind == "file":
            data = _read_correction_artifact(
                final_name,
                review_fd=descriptors["review"],
                max_bytes=expected_artifact["size_bytes"],
            )
            if (
                "sha256:" + _bytes_hash(data) != expected_artifact["sha256"]
                or len(data) != expected_artifact["size_bytes"]
            ):
                raise ReviewError(
                    "integrity_violation", "A committed correction artifact changed."
                )
        else:
            raise ReviewError(
                "integrity_violation", "A pending correction artifact is unsafe."
            )
    desired = _correction_desired_state(prefix_manifest, prefix_private, intent)
    if desired is None:
        raise ReviewError(
            "integrity_violation", "The pending correction state is invalid."
        )
    desired_manifest, desired_private = desired
    if manifest != prefix_manifest and manifest != desired_manifest:
        raise ReviewError(
            "integrity_violation", "The pending correction manifest is invalid."
        )
    if private_state != prefix_private and private_state != desired_private:
        raise ReviewError(
            "integrity_violation", "The pending correction private state is invalid."
        )
    if private_state == prefix_private:
        bundle.atomic_write_json(
            "private.json", desired_private, dir_fd=descriptors["state"]
        )
    if manifest == prefix_manifest:
        bundle.atomic_write_json(
            "manifest.json", desired_manifest, dir_fd=descriptors["root"]
        )
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "correction_record_committed",
        "operation_id": intent["operation_id"],
        "previous_generation": intent["expected_generation"],
        "generation": intent["new_generation"],
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
        "tree_hash": prepared["tree_hash"],
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return {
        "event": intent["event"],
        "expected_generation": intent["expected_generation"],
        "manifest": desired_manifest,
        "private_state": desired_private,
        "intent": intent,
    }


def recover_pending_operation(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    expected_generation: int,
    at: str,
) -> dict | None:
    history = bundle.read_history(state_fd=descriptors["state"])
    intent_indexes = [
        index
        for index, event in enumerate(history)
        if isinstance(event, dict)
        and event.get("event")
        in {"review_open_intent", "review_record_intent", "correction_record_intent"}
    ]
    if not intent_indexes:
        return None
    index = intent_indexes[-1]
    intent = history[index]
    suffix = history[index:]
    if (
        len(suffix) >= 3
        and isinstance(suffix[2], dict)
        and suffix[2].get("event")
        in {
            "review_open_committed",
            "review_record_committed",
            "correction_record_committed",
        }
    ):
        return None
    if isinstance(manifest.get("review"), dict):
        validate_committed_artifacts(
            descriptors=descriptors,
            manifest=manifest,
            bundle_root=bundle_root,
        )
    if intent.get("event") == "review_record_intent":
        return _recover_record_operation(
            descriptors=descriptors,
            history=history,
            index=index,
            manifest=manifest,
            private_state=private_state,
            bundle_root=bundle_root,
            expected_generation=expected_generation,
            at=at,
        )
    if intent.get("event") == "correction_record_intent":
        return _recover_correction_operation(
            descriptors=descriptors,
            history=history,
            index=index,
            manifest=manifest,
            private_state=private_state,
            bundle_root=bundle_root,
            expected_generation=expected_generation,
            at=at,
        )
    if intent.get("event") != "review_open_intent" or not _valid_open_intent(intent):
        raise ReviewError(
            "integrity_violation", "A pending review operation is invalid."
        )
    if expected_generation not in {
        intent.get("expected_generation"),
        intent.get("new_generation"),
    }:
        raise ReviewError(
            "generation_conflict", "Expected generation does not match the pending review."
        )
    prefix_manifest, prefix_private = _open_prefix_from_state(
        manifest, private_state, intent
    )
    resolved_prefix = (
        resolve_history_state(
            history[:index],
            manifest_template=prefix_manifest,
            private_template=prefix_private,
        )
        if isinstance(intent.get("previous_review"), dict)
        else raw_conversion.resolve_history_state(
            history[:index],
            manifest_template=prefix_manifest,
            private_template=prefix_private,
        )
    )
    if resolved_prefix != (prefix_manifest, prefix_private):
        raise ReviewError(
            "integrity_violation", "The pending review history prefix is invalid."
        )
    evidence_bytes = _json_bytes(intent["evidence"])
    temporary_name = intent["evidence_temporary_name"]
    final_name = PurePosixPath(intent["review"]["evidence"]["path"]).name
    if len(suffix) == 1:
        _remove_owned_temporary(temporary_name, dir_fd=descriptors["review"])
        if _entry_kind(final_name, dir_fd=descriptors["review"]) is not None:
            raise ReviewError(
                "integrity_violation", "An unprepared review evidence file exists."
            )
        _write_exclusive(temporary_name, evidence_bytes, dir_fd=descriptors["review"])
        prepared = {
            "schema_version": SCHEMA_VERSION,
            "event": "review_open_prepared",
            "operation_id": intent["operation_id"],
            "expected_generation": intent["expected_generation"],
            "new_generation": intent["new_generation"],
            "at": intent["at"],
            "intent_hash": object_hash(intent),
            "evidence_sha256": "sha256:" + _bytes_hash(evidence_bytes),
            "evidence_size_bytes": len(evidence_bytes),
        }
        bundle.append_history(prepared, state_fd=descriptors["state"])
    elif len(suffix) == 2 and _valid_open_prepared(suffix[1], intent):
        prepared = suffix[1]
    else:
        raise ReviewError(
            "integrity_violation", "A pending review journal suffix is invalid."
        )
    temporary_kind = _entry_kind(temporary_name, dir_fd=descriptors["review"])
    final_kind = _entry_kind(final_name, dir_fd=descriptors["review"])
    if temporary_kind == "file" and final_kind is None:
        temporary_bytes = _read_regular_file(
            temporary_name,
            dir_fd=descriptors["review"],
            max_bytes=MAX_RECORD_BYTES,
        )
        if temporary_bytes != evidence_bytes:
            raise ReviewError(
                "integrity_violation", "Pending review evidence bytes changed."
            )
        _promote(temporary_name, final_name, dir_fd=descriptors["review"])
    elif temporary_kind is None and final_kind == "file":
        final_bytes = _read_regular_file(
            final_name, dir_fd=descriptors["review"], max_bytes=MAX_RECORD_BYTES
        )
        if final_bytes != evidence_bytes:
            raise ReviewError(
                "integrity_violation", "Committed review evidence bytes changed."
            )
        os.fsync(descriptors["review"])
    else:
        raise ReviewError(
            "integrity_violation", "Pending review evidence has an unsafe state."
        )
    desired_manifest, desired_private = _desired_open_state(
        prefix_manifest,
        prefix_private,
        review_state=intent["review"],
        evidence_path=intent["review"]["evidence"]["path"],
        generation=intent["new_generation"],
    )
    if manifest != prefix_manifest and manifest != desired_manifest:
        raise ReviewError(
            "integrity_violation", "Pending review manifest state is invalid."
        )
    if private_state != prefix_private and private_state != desired_private:
        raise ReviewError(
            "integrity_violation", "Pending review private state is invalid."
        )
    if private_state == prefix_private:
        bundle.atomic_write_json(
            "private.json", desired_private, dir_fd=descriptors["state"]
        )
    if manifest == prefix_manifest:
        bundle.atomic_write_json(
            "manifest.json", desired_manifest, dir_fd=descriptors["root"]
        )
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_open_committed",
        "operation_id": intent["operation_id"],
        "previous_generation": intent["expected_generation"],
        "generation": intent["new_generation"],
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return {
        "event": intent["event"],
        "expected_generation": intent["expected_generation"],
        "manifest": desired_manifest,
        "private_state": desired_private,
    }


def load_record_input(path: Path, *, cwd: Path) -> dict:
    candidate = path if path.is_absolute() else cwd / path
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.stat(candidate, follow_symlinks=False)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ReviewError("invalid_review_record", "The review record cannot be read.") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_RECORD_BYTES
        ):
            raise ReviewError(
                "invalid_review_record", "The review record must be a bounded regular file."
            )
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_RECORD_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RECORD_BYTES:
                raise ReviewError(
                    "invalid_review_record", "The review record exceeds its size limit."
                )
        final = os.fstat(descriptor)
        try:
            current = os.stat(candidate, follow_symlinks=False)
        except OSError as exc:
            raise ReviewError(
                "invalid_review_record", "The review record changed while it was read."
            ) from exc
        if (
            final.st_size != size
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
            or (final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise ReviewError(
                "invalid_review_record", "The review record changed while it was read."
            )
        try:
            return bundle.decode_json_object(b"".join(chunks))
        except bundle.BundleStateError as exc:
            raise ReviewError(
                "invalid_review_record", "The review record is not strict JSON."
            ) from exc
    finally:
        os.close(descriptor)


def _validate_checks(checks) -> bool:
    if not isinstance(checks, list) or len(checks) != len(CHECK_CATEGORIES):
        return False
    categories = set()
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"category", "status", "evidence", "finding_ids"}
            or check.get("category") not in CHECK_CATEGORIES
            or check.get("status") not in {"pass", "difference", "ambiguous"}
            or not isinstance(check.get("evidence"), list)
            or not check["evidence"]
            or not all(isinstance(item, str) and item for item in check["evidence"])
            or not isinstance(check.get("finding_ids"), list)
            or not all(isinstance(item, str) and item for item in check["finding_ids"])
        ):
            return False
        if (check["status"] == "pass") != (check["finding_ids"] == []):
            return False
        categories.add(check["category"])
    return categories == CHECK_CATEGORIES


def _bounded_page_range(value, *, required_pages: set[int]) -> set[int] | None:
    if (
        not isinstance(value, dict)
        or set(value) != {"start", "end"}
        or type(value.get("start")) is not int
        or type(value.get("end")) is not int
        or not required_pages
        or value["start"] < 1
        or value["end"] < value["start"]
        or value["end"] > max(required_pages)
    ):
        return None
    pages = set(range(value["start"], value["end"] + 1))
    return pages if pages <= required_pages else None


def _normalize_record(payload: dict, *, evidence: dict) -> tuple[dict, dict]:
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "status",
            "segments",
            "boundaries",
            "findings",
            "page_misc",
            "absence_basis",
        }
        or not _current_schema(payload)
        or payload.get("status")
        not in {
            "local_complete",
            "review_incomplete",
            "correction_required",
            "review_ambiguity",
        }
        or not isinstance(payload.get("segments"), list)
        or not payload["segments"]
        or not isinstance(payload.get("boundaries"), list)
        or not isinstance(payload.get("findings"), list)
        or not isinstance(payload.get("page_misc"), list)
        or not isinstance(payload.get("absence_basis"), list)
        or evidence.get("segment_boundary_policy", {}).get("kind")
        != "ordered_adjacent_review_segments"
        or not isinstance(evidence.get("prior_rounds"), list)
    ):
        raise ReviewError(
            "invalid_review_record", "The review record uses an unknown or incomplete schema."
        )
    required_pages = {
        item["page_number"] for item in evidence["baseline"]["page_references"]
    }
    required_blocks = {item["block_id"] for item in evidence["markdown_blocks"]}
    block_positions = {
        item["block_id"]: index
        for index, item in enumerate(evidence["markdown_blocks"])
    }
    prior_segments = []
    prior_boundaries = []
    coverage_mode = evidence.get("coverage_mode", "cumulative")
    if coverage_mode not in {"cumulative", "fresh"}:
        raise ReviewError(
            "integrity_violation", "The review coverage mode is invalid."
        )
    coverage_rounds = evidence["prior_rounds"] if coverage_mode == "cumulative" else []
    for prior_round in coverage_rounds:
        if (
            not isinstance(prior_round, dict)
            or not isinstance(prior_round.get("segments"), list)
            or not isinstance(prior_round.get("boundaries"), list)
            or prior_round.get("status") != "review_incomplete"
        ):
            raise ReviewError(
                "integrity_violation", "The prior review progress is invalid."
            )
        prior_segments.extend(prior_round["segments"])
        prior_boundaries.extend(prior_round["boundaries"])
    covered_pages = set()
    covered_blocks = set()
    segment_ids = set()
    segment_coordinates = []
    referenced_findings = set()
    for prior_segment in prior_segments:
        segment_id = prior_segment.get("segment_id")
        source_pages = prior_segment.get("source_pages")
        page_range = _bounded_page_range(
            source_pages, required_pages=required_pages
        )
        markdown_blocks = prior_segment.get("markdown_blocks")
        block_indexes = (
            [block_positions.get(block_id) for block_id in markdown_blocks]
            if isinstance(markdown_blocks, list)
            else []
        )
        if (
            not isinstance(segment_id, str)
            or segment_id in segment_ids
            or page_range is None
            or not isinstance(markdown_blocks, list)
            or not markdown_blocks
            or any(index is None for index in block_indexes)
            or block_indexes != sorted(set(block_indexes))
        ):
            raise ReviewError(
                "integrity_violation", "The prior review segments are inconsistent."
        )
        segment_ids.add(segment_id)
        segment_coordinates.append(
            (
                source_pages["start"],
                source_pages["end"],
                block_indexes[0],
                block_indexes[-1],
                segment_id,
            )
        )
        covered_pages.update(page_range)
        covered_blocks.update(markdown_blocks)
    for segment in payload["segments"]:
        source_pages = segment.get("source_pages") if isinstance(segment, dict) else None
        markdown_blocks = segment.get("markdown_blocks") if isinstance(segment, dict) else None
        block_indexes = (
            [block_positions.get(block_id) for block_id in markdown_blocks]
            if isinstance(markdown_blocks, list)
            else []
        )
        page_range = _bounded_page_range(
            source_pages, required_pages=required_pages
        )
        if (
            not isinstance(segment, dict)
            or set(segment)
            != {"segment_id", "source_pages", "markdown_blocks", "checks"}
            or not isinstance(segment.get("segment_id"), str)
            or not segment["segment_id"]
            or segment["segment_id"] in segment_ids
            or page_range is None
            or not isinstance(markdown_blocks, list)
            or not markdown_blocks
            or not all(isinstance(item, str) and item for item in markdown_blocks)
            or not set(markdown_blocks) <= required_blocks
            or block_indexes != sorted(set(block_indexes))
            or not _validate_checks(segment.get("checks"))
        ):
            raise ReviewError(
                "invalid_review_record", "A review segment is invalid or incomplete."
        )
        segment_ids.add(segment["segment_id"])
        segment_coordinates.append(
            (
                source_pages["start"],
                source_pages["end"],
                block_indexes[0],
                block_indexes[-1],
                segment["segment_id"],
            )
        )
        covered_pages.update(page_range)
        covered_blocks.update(markdown_blocks)
        referenced_findings.update(
            finding_id
            for check in segment["checks"]
            for finding_id in check["finding_ids"]
        )
    misc_ids = set()
    for item in payload["page_misc"]:
        source_pages = item.get("source_pages") if isinstance(item, dict) else None
        page_range = _bounded_page_range(
            source_pages, required_pages=required_pages
        )
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "misc_id",
                "kind",
                "treatment",
                "source_pages",
                "semantic_basis",
                "status",
            }
            or not isinstance(item.get("misc_id"), str)
            or not item["misc_id"]
            or item["misc_id"] in misc_ids
            or not isinstance(item.get("kind"), str)
            or not item["kind"]
            or item.get("treatment")
            not in {"omitted", "merged", "relocated", "preserved"}
            or page_range is None
            or not isinstance(item.get("semantic_basis"), list)
            or not item["semantic_basis"]
            or not all(
                isinstance(value, str) and value for value in item["semantic_basis"]
            )
            or item.get("status") != "justified"
        ):
            raise ReviewError(
                "invalid_review_record", "A page-misc treatment is invalid."
            )
        misc_ids.add(item["misc_id"])
    absence_ids = set()
    for item in payload["absence_basis"]:
        source_pages = item.get("source_pages") if isinstance(item, dict) else None
        page_range = _bounded_page_range(
            source_pages, required_pages=required_pages
        )
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "absence_id",
                "category",
                "source_pages",
                "basis",
                "status",
            }
            or not isinstance(item.get("absence_id"), str)
            or not item["absence_id"]
            or item["absence_id"] in absence_ids
            or item.get("category") not in CHECK_CATEGORIES
            or page_range is None
            or not isinstance(item.get("basis"), list)
            or not item["basis"]
            or not all(isinstance(value, str) and value for value in item["basis"])
            or item.get("status") != "verified_absent"
        ):
            raise ReviewError(
                "invalid_review_record", "An absence basis is invalid."
            )
        absence_ids.add(item["absence_id"])
    finding_ids = set()
    finding_kinds = set()
    for finding in payload["findings"]:
        source_pages = finding.get("source_pages") if isinstance(finding, dict) else None
        page_range = _bounded_page_range(
            source_pages, required_pages=required_pages
        )
        markdown_blocks = (
            finding.get("markdown_blocks") if isinstance(finding, dict) else None
        )
        if (
            not isinstance(finding, dict)
            or set(finding)
            != {
                "finding_id",
                "kind",
                "category",
                "source_pages",
                "markdown_blocks",
                "summary",
                "evidence",
                "action",
                "status",
            }
            or not isinstance(finding.get("finding_id"), str)
            or not finding["finding_id"]
            or finding["finding_id"] in finding_ids
            or finding.get("kind") not in {"difference", "ambiguity"}
            or finding.get("category") not in CHECK_CATEGORIES
            or page_range is None
            or not isinstance(markdown_blocks, list)
            or not markdown_blocks
            or not set(markdown_blocks) <= required_blocks
            or not isinstance(finding.get("summary"), str)
            or not finding["summary"]
            or not isinstance(finding.get("evidence"), list)
            or not finding["evidence"]
            or not all(isinstance(item, str) and item for item in finding["evidence"])
            or (
                finding["kind"] == "difference"
                and (
                    finding.get("action") != "correct_markdown"
                    or finding.get("status") != "open"
                )
            )
            or (
                finding["kind"] == "ambiguity"
                and (
                    finding.get("action") != "user_decision_required"
                    or finding.get("status") != "unresolved"
                )
            )
        ):
            raise ReviewError("invalid_review_record", "A review finding is invalid.")
        finding_ids.add(finding["finding_id"])
        finding_kinds.add(finding["kind"])
    finding_kind_by_id = {
        finding["finding_id"]: finding["kind"] for finding in payload["findings"]
    }
    finding_by_id = {
        finding["finding_id"]: finding for finding in payload["findings"]
    }
    prior_covered_boundaries = set()
    for prior_boundary in prior_boundaries:
        pair = (
            prior_boundary.get("before_segment_id"),
            prior_boundary.get("after_segment_id"),
        )
        if not all(isinstance(item, str) and item for item in pair):
            raise ReviewError(
                "integrity_violation", "The prior review boundaries are inconsistent."
            )
        prior_covered_boundaries.add(pair)
    submitted_boundaries = set()
    for boundary in payload["boundaries"]:
        pair = (
            boundary.get("before_segment_id") if isinstance(boundary, dict) else None,
            boundary.get("after_segment_id") if isinstance(boundary, dict) else None,
        )
        if (
            not isinstance(boundary, dict)
            or set(boundary)
            != {
                "before_segment_id",
                "after_segment_id",
                "status",
                "evidence",
                "finding_ids",
            }
            or not all(isinstance(item, str) and item for item in pair)
            or pair in prior_covered_boundaries
            or pair in submitted_boundaries
            or boundary.get("status") not in {"pass", "difference", "ambiguous"}
            or not isinstance(boundary.get("evidence"), list)
            or not boundary["evidence"]
            or not all(
                isinstance(item, str) and item for item in boundary["evidence"]
            )
            or not isinstance(boundary.get("finding_ids"), list)
            or not all(
                isinstance(item, str) and item
                for item in boundary["finding_ids"]
            )
            or (boundary["status"] == "pass") != (boundary["finding_ids"] == [])
        ):
            raise ReviewError("invalid_review_record", "A review boundary is invalid.")
        submitted_boundaries.add(pair)
        referenced_findings.update(boundary["finding_ids"])
    segment_order = [item[-1] for item in sorted(segment_coordinates)]
    required_boundaries = set(zip(segment_order, segment_order[1:]))
    covered_boundaries = (
        prior_covered_boundaries | submitted_boundaries
    ) & required_boundaries
    follow_up = evidence.get("follow_up_requirements")
    if follow_up is not None:
        required_follow_up_segments = (
            set(follow_up.get("segment_ids", [])) if isinstance(follow_up, dict) else set()
        )
        follow_up_boundaries = (
            follow_up.get("boundaries") if isinstance(follow_up, dict) else None
        )
        required_follow_up_boundaries = (
            {
                (item.get("before_segment_id"), item.get("after_segment_id"))
                for item in follow_up_boundaries
                if isinstance(item, dict)
                and set(item) == {"before_segment_id", "after_segment_id"}
                and all(isinstance(value, str) and value for value in item.values())
            }
            if isinstance(follow_up_boundaries, list)
            else set()
        )
        submitted_segment_ids = {
            segment["segment_id"] for segment in payload["segments"]
        }
        prior_rounds_for_follow_up = evidence.get("prior_rounds", [])
        if (
            not isinstance(follow_up, dict)
            or set(follow_up) != {"source_round_id", "segment_ids", "boundaries"}
            or coverage_mode != "fresh"
            or not prior_rounds_for_follow_up
            or follow_up.get("source_round_id")
            != prior_rounds_for_follow_up[-1].get("round_id")
            or not isinstance(follow_up.get("segment_ids"), list)
            or not required_follow_up_segments
            or len(required_follow_up_segments) != len(follow_up["segment_ids"])
            or not all(
                isinstance(value, str) and value for value in follow_up["segment_ids"]
            )
            or not isinstance(follow_up_boundaries, list)
            or len(required_follow_up_boundaries) != len(follow_up_boundaries)
        ):
            raise ReviewError(
                "integrity_violation", "The correction follow-up requirements are invalid."
            )
        if payload["status"] != "review_incomplete" and (
            not required_follow_up_segments <= submitted_segment_ids
            or not required_follow_up_boundaries <= submitted_boundaries
        ):
            raise ReviewError(
                "invalid_review_record",
                "The correction follow-up omits an affected segment or boundary.",
            )
    expected_finding_kind = {
        "local_complete": None,
        "review_incomplete": None,
        "correction_required": "difference",
        "review_ambiguity": "ambiguity",
    }[payload["status"]]
    status_kind = {"difference": "difference", "ambiguous": "ambiguity"}
    statuses_match_findings = all(
        all(
            finding_kind_by_id.get(finding_id) == status_kind[check["status"]]
            for finding_id in check["finding_ids"]
        )
        for segment in payload["segments"]
        for check in segment["checks"]
        if check["status"] != "pass"
    ) and all(
        all(
            finding_kind_by_id.get(finding_id) == status_kind[boundary["status"]]
            for finding_id in boundary["finding_ids"]
        )
        for boundary in payload["boundaries"]
        if boundary["status"] != "pass"
    )
    segment_by_id = {
        segment["segment_id"]: segment
        for segment in [*prior_segments, *payload["segments"]]
    }
    segment_findings_are_bound = all(
        all(
            finding_by_id.get(finding_id, {}).get("category") == check["category"]
            and finding_by_id[finding_id]["source_pages"]["start"]
            >= segment["source_pages"]["start"]
            and finding_by_id[finding_id]["source_pages"]["end"]
            <= segment["source_pages"]["end"]
            and set(finding_by_id[finding_id]["markdown_blocks"])
            <= set(segment["markdown_blocks"])
            for finding_id in check["finding_ids"]
        )
        for segment in payload["segments"]
        for check in segment["checks"]
    )
    boundary_findings_are_bound = True
    for boundary in payload["boundaries"]:
        before = segment_by_id.get(boundary["before_segment_id"])
        after = segment_by_id.get(boundary["after_segment_id"])
        if before is None or after is None:
            boundary_findings_are_bound = False
            break
        minimum_page = min(
            before["source_pages"]["start"], after["source_pages"]["start"]
        )
        maximum_page = max(
            before["source_pages"]["end"], after["source_pages"]["end"]
        )
        boundary_blocks = set(before["markdown_blocks"]) | set(after["markdown_blocks"])
        if not all(
            finding_by_id.get(finding_id, {}).get("source_pages", {}).get("start", 0)
            >= minimum_page
            and finding_by_id[finding_id]["source_pages"]["end"] <= maximum_page
            and set(finding_by_id[finding_id]["markdown_blocks"]) <= boundary_blocks
            for finding_id in boundary["finding_ids"]
        ):
            boundary_findings_are_bound = False
            break
    if (
        not covered_pages <= required_pages
        or not covered_blocks <= required_blocks
        or not submitted_boundaries <= required_boundaries
    ):
        raise ReviewError(
            "invalid_review_record", "The review record references unknown evidence."
        )
    if (
        referenced_findings != finding_ids
        or not statuses_match_findings
        or not segment_findings_are_bound
        or not boundary_findings_are_bound
        or (expected_finding_kind is None and finding_ids)
        or (
            expected_finding_kind is not None
            and (not finding_ids or finding_kinds != {expected_finding_kind})
        )
    ):
        raise ReviewError(
            "invalid_review_record", "Review findings do not match the declared status."
        )
    coverage = {
        "source_pages": {
            "covered": len(covered_pages),
            "required": len(required_pages),
            "complete": covered_pages == required_pages,
        },
        "markdown_blocks": {
            "covered": len(covered_blocks),
            "required": len(required_blocks),
            "complete": covered_blocks == required_blocks,
        },
        "boundaries": {
            "covered": len(covered_boundaries),
            "required": len(required_boundaries),
            "complete": covered_boundaries == required_boundaries,
        },
    }
    coverage_complete = all(item["complete"] for item in coverage.values())
    if payload["status"] != "review_incomplete" and not coverage_complete:
        raise ReviewError(
            "invalid_review_record", "The review record does not cover all required evidence."
        )
    if payload["status"] == "review_incomplete" and coverage_complete:
        raise ReviewError(
            "invalid_review_record", "An incomplete review must identify a coverage gap."
        )
    return deepcopy(payload), coverage


def _report(review: dict) -> bytes:
    coverage = review["coverage"]
    lines = [
        "# Review Report",
        "",
        f"Status: {review['status']}",
        f"Dialect: {review['target']['dialect']}",
        f"Target: {review['target']['path']}",
        "",
        "## Coverage",
        "",
        (
            f"- Source pages: {coverage['source_pages']['covered']}/"
            f"{coverage['source_pages']['required']}"
        ),
        (
            f"- Markdown blocks: {coverage['markdown_blocks']['covered']}/"
            f"{coverage['markdown_blocks']['required']}"
        ),
        (
            f"- Boundaries: {coverage['boundaries']['covered']}/"
            f"{coverage['boundaries']['required']}"
        ),
        "",
        (
            "No content-semantic differences or unresolved findings were recorded."
            if review["status"] == "local_complete"
            else "Review coverage is incomplete; no final Markdown was selected."
            if review["status"] == "review_incomplete"
            else "Review findings must be resolved before a final Markdown is selected."
        ),
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _is_utf8_text(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _normalize_review_decisions(payload: dict, review_document: dict) -> tuple[dict, dict]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "decisions"}
        or not _current_schema(payload)
        or not isinstance(payload.get("decisions"), list)
        or not payload["decisions"]
        or not isinstance(review_document, dict)
        or review_document.get("status") != "review_ambiguity"
        or not isinstance(review_document.get("rounds"), list)
        or not review_document["rounds"]
    ):
        raise ReviewError(
            "invalid_review_decision", "The review decision uses an invalid schema."
        )
    last_round = review_document["rounds"][-1]
    findings = {
        item.get("finding_id"): item
        for item in last_round.get("findings", [])
        if isinstance(item, dict)
        and item.get("kind") == "ambiguity"
        and item.get("status") == "unresolved"
    }
    if not findings or len(findings) != len(last_round.get("findings", [])):
        raise ReviewError(
            "integrity_violation", "The unresolved review findings are invalid."
        )
    normalized = []
    seen = set()
    for item in payload["decisions"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"finding_id", "resolution", "selected_content", "basis"}
            or not isinstance(item.get("finding_id"), str)
            or item["finding_id"] not in findings
            or item["finding_id"] in seen
            or item.get("resolution") not in {"keep_current", "correct_markdown"}
            or (
                item["resolution"] == "keep_current"
                and item.get("selected_content") is not None
            )
            or (
                item["resolution"] == "correct_markdown"
                and (
                    not _is_utf8_text(item.get("selected_content"))
                    or not item["selected_content"]
                )
            )
            or not isinstance(item.get("basis"), list)
            or not item["basis"]
            or not all(isinstance(value, str) and value for value in item["basis"])
        ):
            raise ReviewError(
                "invalid_review_decision",
                "Each ambiguity decision must bind one unresolved finding and user basis.",
            )
        seen.add(item["finding_id"])
        normalized.append(deepcopy(item))
    if seen != set(findings):
        raise ReviewError(
            "invalid_review_decision",
            "Every unresolved ambiguity must receive exactly one decision.",
        )

    decision_by_id = {item["finding_id"]: item for item in normalized}
    corrected_findings = []
    for finding_id, finding in findings.items():
        decision = decision_by_id[finding_id]
        if decision["resolution"] == "correct_markdown":
            corrected = deepcopy(finding)
            corrected["kind"] = "difference"
            corrected["action"] = "correct_markdown"
            corrected["status"] = "open"
            corrected["selected_content"] = decision["selected_content"]
            corrected["decision_basis"] = deepcopy(decision["basis"])
            corrected_findings.append(corrected)
    corrected_ids = {item["finding_id"] for item in corrected_findings}
    decision_document = deepcopy(review_document)
    decision_document["decisions"] = normalized
    decision_document["status"] = (
        "correction_required" if corrected_findings else "local_complete"
    )
    decision_document["reason_code"] = None
    decision_round = decision_document["rounds"][-1]
    decision_round["findings"] = corrected_findings
    decision_round["status"] = decision_document["status"]
    for segment in decision_round.get("segments", []):
        for check in segment.get("checks", []):
            check["finding_ids"] = [
                finding_id
                for finding_id in check["finding_ids"]
                if finding_id in corrected_ids
            ]
            check["status"] = "difference" if check["finding_ids"] else "pass"
    for boundary in decision_round.get("boundaries", []):
        boundary["finding_ids"] = [
            finding_id
            for finding_id in boundary["finding_ids"]
            if finding_id in corrected_ids
        ]
        boundary["status"] = "difference" if boundary["finding_ids"] else "pass"
    return {"schema_version": SCHEMA_VERSION, "decisions": normalized}, decision_document


def commit_review_decision(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    payload: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    at: str,
) -> dict:
    if manifest.get("generation") != expected_generation:
        raise ReviewError("generation_conflict", "The work bundle generation changed.")
    state = manifest.get("review")
    pending = state.get("pending_action") if isinstance(state, dict) else None
    if (
        manifest.get("conversion_state") != "awaiting_user"
        or not isinstance(state, dict)
        or state.get("status") != "review_ambiguity"
        or not isinstance(pending, dict)
    ):
        raise ReviewError("action_already_consumed", "No review decision is pending.")
    if pending.get("action_id") != action_id:
        raise ReviewError(
            "review_action_mismatch", "The review decision action ID does not match."
        )
    if pending.get("evidence_hash") != evidence_hash:
        raise ReviewError(
            "evidence_hash_mismatch", "The review decision evidence hash does not match."
        )
    validate_committed_artifacts(
        descriptors=descriptors,
        manifest=manifest,
        bundle_root=bundle_root,
    )
    previous_summary = state["rounds"][-1]
    previous_bytes = _read_bundle_path(
        previous_summary["review_path"],
        root_fd=descriptors["root"],
        max_bytes=MAX_RECORD_BYTES,
    )
    if "sha256:" + _bytes_hash(previous_bytes) != previous_summary["review_sha256"]:
        raise ReviewError("integrity_violation", "The ambiguity review changed.")
    try:
        previous_document = bundle.decode_json_object(previous_bytes)
    except bundle.BundleStateError as exc:
        raise ReviewError("integrity_violation", "The ambiguity review is invalid.") from exc
    normalized, review_document = _normalize_review_decisions(
        payload, previous_document
    )
    review_bytes = _bounded_json_bytes(
        review_document, artifact="review decision"
    )
    review_sha256 = "sha256:" + _bytes_hash(review_bytes)
    report_bytes = _bounded_artifact_bytes(
        _report(review_document), artifact="review decision report"
    )
    report_sha256 = "sha256:" + _bytes_hash(report_bytes)
    new_generation = expected_generation + 1
    status = review_document["status"]
    pending_action = None
    if status == "correction_required":
        pending_action = {
            "kind": "record_correction",
            "action_id": f"correction-{uuid.uuid4().hex}",
            "generation": new_generation,
            "evidence_hash": object_hash(
                {
                    "review_sha256": review_sha256,
                    "target_sha256": state["target"]["sha256"],
                    "finding_ids": [
                        finding["finding_id"]
                        for finding in review_document["rounds"][-1]["findings"]
                    ],
                }
            ),
        }
    round_number = len(state["rounds"])
    round_id = previous_summary.get("round_id")
    if round_id != f"review-round-{round_number:04d}":
        raise ReviewError(
            "integrity_violation", "The ambiguity review round identity is invalid."
        )
    review_path = f"04-review/review-decision-{round_number:04d}.json"
    report_path = f"04-review/review-decision-{round_number:04d}.md"
    round_summary = {
        "round_id": round_id,
        "evidence_path": state["evidence"]["path"],
        "evidence_sha256": state["evidence"]["sha256"],
        "evidence_size_bytes": state["evidence"]["size_bytes"],
        "review_path": review_path,
        "review_sha256": review_sha256,
        "review_size_bytes": len(review_bytes),
        "report_path": report_path,
        "report_sha256": report_sha256,
        "report_size_bytes": len(report_bytes),
        "recorded_at": at,
    }
    desired_review = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason_code": None,
        "target": deepcopy(state["target"]),
        "evidence": deepcopy(state["evidence"]),
        "coverage": deepcopy(state["coverage"]),
        "rounds": [*deepcopy(state["rounds"][:-1]), round_summary],
        "pending_action": pending_action,
    }
    final_markdown = None
    if status == "local_complete":
        target = state["target"]
        final_markdown = {
            "kind": target["kind"],
            **(
                {"attempt_id": target["attempt_id"]}
                if target["kind"] == "raw_conversion"
                else {"correction_id": target["correction_id"]}
            ),
            "path": target["path"],
            "sha256": target["sha256"],
            "size_bytes": target["size_bytes"],
            "review_round_id": round_id,
            "review_sha256": review_sha256,
            "review_evidence_sha256": state["evidence"]["sha256"],
            "dialect": DIALECT,
            "semantic_hash": target["semantic_hash"],
        }
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = (
        "local_complete" if status == "local_complete" else "review_pending"
    )
    desired_manifest["review"] = desired_review
    desired_manifest["final_markdown"] = final_markdown
    desired_manifest["artifacts"].update(
        {
            "review": review_path,
            "review_report": report_path,
            "review_decision": review_path,
        }
    )
    if final_markdown is not None:
        desired_manifest["artifacts"]["final_markdown"] = final_markdown["path"]
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    operation_id = f"review-record-{uuid.uuid4().hex}"
    review_temporary_name = f".{operation_id}.review.part"
    report_temporary_name = f".{operation_id}.report.part"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_intent",
        "request_kind": "review_decision",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "payload": normalized,
        "review": desired_review,
        "previous_review": deepcopy(state),
        "previous_artifacts": {
            key: manifest.get("artifacts", {}).get(key)
            for key in ("review", "review_report", "review_decision")
        },
        "final_markdown": final_markdown,
        "review_document": review_document,
        "review_path": review_path,
        "report_path": report_path,
        "review_temporary_name": review_temporary_name,
        "report_temporary_name": report_temporary_name,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    _write_exclusive(review_temporary_name, review_bytes, dir_fd=descriptors["review"])
    _write_exclusive(report_temporary_name, report_bytes, dir_fd=descriptors["review"])
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "intent_hash": object_hash(intent),
        "review_sha256": review_sha256,
        "review_size_bytes": len(review_bytes),
        "report_sha256": report_sha256,
        "report_size_bytes": len(report_bytes),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    _promote(review_temporary_name, PurePosixPath(review_path).name, dir_fd=descriptors["review"])
    _promote(report_temporary_name, PurePosixPath(report_path).name, dir_fd=descriptors["review"])
    bundle.atomic_write_json("private.json", desired_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", desired_manifest, dir_fd=descriptors["root"])
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_committed",
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return desired_manifest


def _correction_artifacts(
    intent: dict,
    *,
    source_markdown: bytes,
    review_document: dict,
    bundle_root: Path,
) -> dict[str, bytes]:
    built = correction.apply_corrections(
        intent["payload"],
        review_document=review_document,
        review_evidence=intent["source_review_evidence"],
        source_markdown=source_markdown,
        source_target=intent["source_target"],
        correction_id=intent["correction_id"],
        corrected_path=intent["correction"]["corrected_markdown"]["path"],
        bundle_root=bundle_root,
        at=intent["at"],
        expected_references=intent["resource_reference_oracle"],
    )
    if built["record"] != intent.get("correction_document"):
        raise ReviewError(
            "integrity_violation", "The correction operation is not reproducible."
        )
    correction_summary = intent["correction"]
    artifacts = {
        PurePosixPath(correction_summary["corrected_markdown"]["path"])
        .relative_to("04-review")
        .as_posix(): built["corrected_markdown"],
        PurePosixPath(correction_summary["diff"]["path"])
        .relative_to("04-review")
        .as_posix(): built["diff"],
        PurePosixPath(correction_summary["record"]["path"])
        .relative_to("04-review")
        .as_posix(): built["record_bytes"],
        PurePosixPath(correction_summary["review_evidence"]["path"])
        .relative_to("04-review")
        .as_posix(): _json_bytes(intent["review_evidence"]),
    }
    artifacts.update(built["artifacts"])
    _validate_correction_artifact_budget(
        artifacts, source_markdown=source_markdown
    )
    return artifacts


def _validate_correction_artifact_budget(
    artifacts: dict[str, bytes], *, source_markdown: bytes
) -> None:
    sizes = [len(source_markdown)]
    for value in artifacts.values():
        if not isinstance(value, bytes) or len(value) > MAX_CORRECTION_ARTIFACT_BYTES:
            raise correction.CorrectionError(
                "correction_size_limit",
                "A generated correction artifact exceeds its byte limit.",
            )
        sizes.append(len(value))
    if sum(sizes) > MAX_CORRECTION_ARTIFACT_BYTES:
        raise correction.CorrectionError(
            "correction_size_limit",
            "The generated correction artifact set exceeds its aggregate byte limit.",
        )


def _validate_history_budget(events: list[dict], *, state_fd: int) -> None:
    current = _read_regular_file(
        "history.ndjson", dir_fd=state_fd, max_bytes=bundle.MAX_STATE_BYTES
    )
    # Delegate to bundle.canonical_json_bytes -- the same encoder
    # bundle.append_history uses to actually persist each event -- rather
    # than review._json_bytes's separately maintained copy of the same
    # encoding parameters, so this pre-check can never silently drift from
    # what append_history actually writes to disk.
    if len(current) + sum(
        len(bundle.canonical_json_bytes(event)) for event in events
    ) > bundle.MAX_STATE_BYTES:
        raise correction.CorrectionError(
            "correction_size_limit",
            "The correction journal events exceed the remaining history budget.",
        )


def _artifact_manifest(artifacts: dict[str, bytes]) -> dict:
    return {
        path: {
            "sha256": "sha256:" + _bytes_hash(data),
            "size_bytes": len(data),
        }
        for path, data in sorted(artifacts.items())
    }


def _correction_desired_state(
    manifest: dict, private_state: dict, intent: dict
) -> tuple[dict, dict] | None:
    summary = intent.get("correction") if isinstance(intent, dict) else None
    review_state = intent.get("review") if isinstance(intent, dict) else None
    if (
        not isinstance(summary, dict)
        or not isinstance(review_state, dict)
        or review_state.get("status") != "review_pending"
        or review_state.get("pending_action", {}).get("kind") != "record_review"
    ):
        return None
    corrections = manifest.get("corrections", [])
    if not isinstance(corrections, list):
        return None
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = intent["new_generation"]
    desired_manifest["conversion_state"] = "review_pending"
    desired_manifest["review"] = deepcopy(review_state)
    desired_manifest["final_markdown"] = None
    desired_manifest["corrections"] = [*deepcopy(corrections), deepcopy(summary)]
    desired_manifest["artifacts"].update(
        {
            "corrected_markdown": summary["corrected_markdown"]["path"],
            "corrections_diff": summary["diff"]["path"],
            "correction_record": summary["record"]["path"],
            "review_evidence": summary["review_evidence"]["path"],
        }
    )
    desired_manifest["artifacts"].pop("final_markdown", None)
    desired_private = deepcopy(private_state)
    desired_private["generation"] = intent["new_generation"]
    return desired_manifest, desired_private


def commit_correction_record(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    payload: dict,
    bundle_root: Path,
    environ: dict[str, str],
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    at: str,
) -> dict:
    if manifest.get("generation") != expected_generation:
        raise ReviewError("generation_conflict", "The work bundle generation changed.")
    state = manifest.get("review")
    pending = state.get("pending_action") if isinstance(state, dict) else None
    if (
        manifest.get("conversion_state") != "review_pending"
        or not isinstance(state, dict)
        or state.get("status") != "correction_required"
        or not isinstance(pending, dict)
    ):
        raise ReviewError("action_already_consumed", "No correction action is pending.")
    if pending.get("action_id") != action_id:
        raise ReviewError(
            "review_action_mismatch", "The correction action ID does not match."
        )
    if pending.get("evidence_hash") != evidence_hash:
        raise ReviewError(
            "evidence_hash_mismatch", "The correction evidence hash does not match."
        )
    validate_committed_artifacts(
        descriptors=descriptors,
        manifest=manifest,
        bundle_root=bundle_root,
    )
    summaries = state.get("rounds")
    summary = summaries[-1] if isinstance(summaries, list) and summaries else None
    if not isinstance(summary, dict):
        raise ReviewError(
            "integrity_violation", "The reviewed difference record is unavailable."
        )
    review_bytes = _read_bundle_path(
        summary["review_path"],
        root_fd=descriptors["root"],
        max_bytes=MAX_RECORD_BYTES,
    )
    if "sha256:" + _bytes_hash(review_bytes) != summary.get("review_sha256"):
        raise ReviewError(
            "integrity_violation", "The reviewed difference record changed."
        )
    try:
        review_document = bundle.decode_json_object(review_bytes)
    except bundle.BundleStateError as exc:
        raise ReviewError(
            "integrity_violation", "The reviewed difference record is invalid."
        ) from exc
    source_evidence_bytes = _read_bundle_path(
        state["evidence"]["path"],
        root_fd=descriptors["root"],
        max_bytes=MAX_RECORD_BYTES,
    )
    if "sha256:" + _bytes_hash(source_evidence_bytes) != state["evidence"].get(
        "sha256"
    ):
        raise ReviewError(
            "integrity_violation", "The correction source evidence changed."
        )
    try:
        source_review_evidence = bundle.decode_json_object(source_evidence_bytes)
    except bundle.BundleStateError as exc:
        raise ReviewError(
            "integrity_violation", "The correction source evidence is invalid."
        ) from exc
    source_target = state.get("target")
    if not isinstance(source_target, dict) or source_target.get("kind") not in {
        "raw_conversion",
        "corrected_markdown",
    }:
        raise ReviewError(
            "integrity_violation", "The correction target is invalid."
        )
    source_markdown = _read_bundle_path(
        source_target["path"],
        root_fd=descriptors["root"],
        max_bytes=MAX_MARKDOWN_BYTES,
    )
    if _bytes_hash(source_markdown) != source_target.get("sha256"):
        raise ReviewError("integrity_violation", "The correction target changed.")
    correction_number = len(manifest.get("corrections", [])) + 1
    correction_id = f"correction-{correction_number:04d}"
    source_slug = _source_slug(manifest)
    corrected_name = (
        f"{source_slug}.corrected.md"
        if correction_number == 1
        else f"{source_slug}.corrected-{correction_number:04d}.md"
    )
    diff_name = (
        "corrections.diff"
        if correction_number == 1
        else f"corrections-{correction_number:04d}.diff"
    )
    record_name = f"{correction_id}.json"
    round_number = len(state.get("rounds", [])) + 1
    round_id = f"review-round-{round_number:04d}"
    evidence_name = f"review-evidence-round-{round_number:04d}.json"
    corrected_path = f"04-review/{corrected_name}"
    def reference_oracle(candidate: bytes) -> list[dict]:
        oracle_structure = _parse_markdown(
            candidate,
            manifest=manifest,
            environ=environ,
            validated_local_targets=source_target.get("validated_local_targets", []),
        )
        return oracle_structure["resource_references"]

    built = correction.apply_corrections(
        payload,
        review_document=review_document,
        review_evidence=source_review_evidence,
        source_markdown=source_markdown,
        source_target=source_target,
        correction_id=correction_id,
        corrected_path=corrected_path,
        bundle_root=bundle_root,
        at=at,
        reference_oracle=reference_oracle,
    )
    corrected = built["corrected_markdown"]
    structure = _parse_markdown(
        corrected,
        manifest=manifest,
        environ=environ,
        validated_local_targets=[
            item["rewritten_target"]
            for item in built["record"]["resource_rewrites"]
        ]
        + [
            PurePosixPath(item["output_relative_path"])
            .relative_to("04-review")
            .as_posix()
            for item in built["record"]["crops"]
        ],
    )
    if structure["status"] != "pass":
        raise ReviewError(
            "invalid_correction_record",
            "The corrected Markdown does not satisfy the safe GFM structure contract.",
        )
    target = _corrected_target_descriptor(
        manifest,
        corrected,
        structure,
        correction_id=correction_id,
        path=corrected_path,
        source_target=source_target,
        local_resources={
            "schema_version": SCHEMA_VERSION,
            "markdown_path": corrected_path,
            "markdown_sha256": "sha256:" + _bytes_hash(corrected),
            "oracle": deepcopy(structure["resource_references"]),
            "reference_count": sum(
                item["count"]
                for item in built["record"]["resource_rewrites"]
            ),
            "references": deepcopy(built["record"]["resource_rewrites"]),
        },
    )
    evidence = _review_evidence(
        manifest=manifest,
        target=target,
        structure=structure,
        round_id=round_id,
        prior_rounds=review_document["rounds"],
        coverage_mode="fresh",
        follow_up_requirements={
            "source_round_id": review_document["rounds"][-1]["round_id"],
            "segment_ids": sorted(
                {
                    segment_id
                    for item in built["payload"]["corrections"]
                    for segment_id in item["review_segment_ids"]
                }
            ),
            "boundaries": [
                {
                    "before_segment_id": before,
                    "after_segment_id": after,
                }
                for before, after in sorted(
                    {
                        (
                            boundary["before_segment_id"],
                            boundary["after_segment_id"],
                        )
                        for item in built["payload"]["corrections"]
                        for boundary in item["affected_boundaries"]
                    }
                )
            ],
        },
    )
    try:
        evidence_bytes = _bounded_json_bytes(
            evidence, artifact="follow-up review evidence"
        )
    except ReviewError as exc:
        if exc.code != "review_size_limit":
            raise
        raise correction.CorrectionError("correction_size_limit", exc.message) from None
    review_action = {
        "kind": "record_review",
        "action_id": f"review-{uuid.uuid4().hex}",
        "generation": expected_generation + 1,
        "evidence_hash": "sha256:" + _bytes_hash(evidence_bytes),
    }
    review_state = {
        "schema_version": SCHEMA_VERSION,
        "status": "review_pending",
        "reason_code": None,
        "target": target,
        "evidence": {
            "path": f"04-review/{evidence_name}",
            "sha256": review_action["evidence_hash"],
            "size_bytes": len(evidence_bytes),
            "coverage_basis_sha256": evidence["coverage_basis_sha256"],
        },
        "coverage": {
            "source_pages": {
                "covered": 0,
                "required": len(evidence["baseline"]["page_references"]),
                "complete": False,
            },
            "markdown_blocks": {
                "covered": 0,
                "required": len(evidence["markdown_blocks"]),
                "complete": False,
            },
            "boundaries": {"covered": 0, "required": 0, "complete": True},
        },
        "rounds": deepcopy(state.get("rounds", [])),
        "pending_action": review_action,
    }
    artifact_bytes = {
        corrected_name: corrected,
        diff_name: built["diff"],
        record_name: built["record_bytes"],
        evidence_name: evidence_bytes,
        **built["artifacts"],
    }
    _validate_correction_artifact_budget(
        artifact_bytes, source_markdown=source_markdown
    )
    artifact_manifest = _artifact_manifest(artifact_bytes)

    def descriptor(name: str) -> dict:
        value = artifact_manifest[name]
        return {
            "path": f"04-review/{name}",
            "sha256": value["sha256"],
            "size_bytes": value["size_bytes"],
        }

    correction_summary = {
        "schema_version": SCHEMA_VERSION,
        "correction_id": correction_id,
        "path": corrected_path,
        "source_target": deepcopy(source_target),
        "finding_ids": [
            item["finding_id"] for item in built["payload"]["corrections"]
        ],
        "corrected_markdown": descriptor(corrected_name),
        "diff": descriptor(diff_name),
        "record": descriptor(record_name),
        "review_evidence": descriptor(evidence_name),
        "assets": [descriptor(name) for name in sorted(built["artifacts"])],
        "created_at": at,
    }
    new_generation = expected_generation + 1
    operation_id = f"correction-record-{uuid.uuid4().hex}"
    temporary_names = {
        name: f".{operation_id}-{index:04d}.part"
        for index, name in enumerate(sorted(artifact_bytes), start=1)
    }
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "correction_record_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "payload": built["payload"],
        "source_target": deepcopy(source_target),
        "correction_id": correction_id,
        "correction_document": built["record"],
        "resource_reference_oracle": built["resource_reference_oracle"],
        "source_review_document": review_document,
        "source_review_evidence": source_review_evidence,
        "review_evidence": evidence,
        "review": review_state,
        "correction": correction_summary,
        "previous_review": deepcopy(state),
        "previous_corrections": deepcopy(manifest.get("corrections", [])),
        "previous_artifacts": {
            key: manifest.get("artifacts", {}).get(key)
            for key in (
                "corrected_markdown",
                "corrections_diff",
                "correction_record",
                "review_evidence",
                "final_markdown",
            )
        },
        "artifact_manifest": artifact_manifest,
        "temporary_names": temporary_names,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    desired = _correction_desired_state(manifest, private_state, intent)
    if desired is None:
        raise ReviewError(
            "integrity_violation", "The correction state transition is invalid."
        )
    desired_manifest, desired_private = desired
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "correction_record_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "intent_hash": object_hash(intent),
        "artifact_manifest": artifact_manifest,
        "tree_hash": object_hash({"artifacts": artifact_manifest}),
    }
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "correction_record_committed",
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
        "tree_hash": prepared["tree_hash"],
    }
    _validate_history_budget(
        [intent, prepared, committed], state_fd=descriptors["state"]
    )
    bundle.append_history(intent, state_fd=descriptors["state"])
    _ensure_correction_artifact_parents(
        artifact_bytes, review_fd=descriptors["review"]
    )
    for name, data in sorted(artifact_bytes.items()):
        _write_exclusive(
            temporary_names[name], data, dir_fd=descriptors["review"]
        )
    bundle.append_history(prepared, state_fd=descriptors["state"])
    for name in sorted(artifact_bytes):
        _promote_correction_artifact(
            temporary_names[name], name, review_fd=descriptors["review"]
        )
    bundle.atomic_write_json("private.json", desired_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", desired_manifest, dir_fd=descriptors["root"])
    bundle.append_history(committed, state_fd=descriptors["state"])
    return desired_manifest


def commit_review_record(
    *,
    descriptors: dict,
    manifest: dict,
    private_state: dict,
    bundle_root: Path,
    payload: dict,
    expected_generation: int,
    action_id: str,
    evidence_hash: str,
    at: str,
) -> dict:
    if manifest.get("generation") != expected_generation:
        raise ReviewError("generation_conflict", "The work bundle generation changed.")
    state = manifest.get("review")
    pending = state.get("pending_action") if isinstance(state, dict) else None
    if manifest.get("conversion_state") != "review_pending" or not isinstance(
        pending, dict
    ):
        raise ReviewError("action_already_consumed", "No review action is pending.")
    if pending.get("action_id") != action_id:
        raise ReviewError("review_action_mismatch", "The review action ID does not match.")
    if pending.get("evidence_hash") != evidence_hash:
        raise ReviewError("evidence_hash_mismatch", "The review evidence hash does not match.")
    validate_committed_artifacts(
        descriptors=descriptors,
        manifest=manifest,
        bundle_root=bundle_root,
    )
    evidence_bytes = _read_bundle_path(
        state["evidence"]["path"],
        root_fd=descriptors["root"],
        max_bytes=MAX_RECORD_BYTES,
    )
    actual_evidence_hash = "sha256:" + _bytes_hash(evidence_bytes)
    if actual_evidence_hash != evidence_hash:
        raise ReviewError("integrity_violation", "The review evidence hash changed.")
    try:
        evidence = bundle.decode_json_object(evidence_bytes)
    except bundle.BundleStateError as exc:
        raise ReviewError("integrity_violation", "The review evidence is invalid.") from exc
    normalized, coverage = _normalize_record(payload, evidence=evidence)
    review_status = normalized["status"]
    if (
        review_status == "local_complete"
        and evidence["structural_validation"]["status"] != "pass"
    ):
        raise ReviewError(
            "invalid_review_record",
            "A structurally blocked target requires evidence-bound correction findings.",
        )
    reason_code = {
        "local_complete": None,
        "correction_required": None,
        "review_incomplete": "review_incomplete",
        "review_ambiguity": "review_ambiguity",
    }[review_status]
    round_record = {
        "round_id": evidence["round_id"],
        "target": deepcopy(evidence["target"]),
        "baseline": deepcopy(evidence["baseline"]),
        "segments": normalized["segments"],
        "boundaries": normalized["boundaries"],
        "findings": normalized["findings"],
        "page_misc": normalized["page_misc"],
        "absence_basis": normalized["absence_basis"],
        "coverage": coverage,
        "structural_validation": deepcopy(evidence["structural_validation"]),
        "status": review_status,
        "recorded_at": at,
    }
    prior_rounds = deepcopy(evidence.get("prior_rounds", []))
    review = {
        "schema_version": SCHEMA_VERSION,
        "status": review_status,
        "reason_code": reason_code,
        "target": deepcopy(evidence["target"]),
        "rounds": [*prior_rounds, round_record],
        "coverage": coverage,
    }
    review_bytes = _bounded_json_bytes(review, artifact="review record")
    review_sha256 = "sha256:" + _bytes_hash(review_bytes)
    report_bytes = _bounded_artifact_bytes(
        _report(review), artifact="review report"
    )
    report_sha256 = "sha256:" + _bytes_hash(report_bytes)
    final_markdown = (
        {
            "kind": evidence["target"]["kind"],
            **(
                {"attempt_id": evidence["target"]["attempt_id"]}
                if evidence["target"]["kind"] == "raw_conversion"
                else {"correction_id": evidence["target"]["correction_id"]}
            ),
            "path": evidence["target"]["path"],
            "sha256": evidence["target"]["sha256"],
            "size_bytes": evidence["target"]["size_bytes"],
            "review_round_id": round_record["round_id"],
            "review_sha256": review_sha256,
            "review_evidence_sha256": evidence_hash,
            "dialect": DIALECT,
            "semantic_hash": evidence["target"]["semantic_hash"],
        }
        if review_status == "local_complete"
        else None
    )
    new_generation = expected_generation + 1
    decision_action = None
    if review_status == "correction_required":
        decision_action = {
            "kind": "record_correction",
            "action_id": f"correction-{uuid.uuid4().hex}",
            "generation": new_generation,
            "evidence_hash": object_hash(
                {
                    "review_sha256": review_sha256,
                    "target_sha256": evidence["target"]["sha256"],
                    "finding_ids": [
                        finding["finding_id"] for finding in normalized["findings"]
                    ],
                }
            ),
        }
    elif (
        review_status == "review_ambiguity"
        and manifest["settings_snapshot"]["interaction_mode"] == "confirm"
    ):
        decision_action = {
            "kind": "resolve_review_ambiguity",
            "action_id": f"review-decision-{uuid.uuid4().hex}",
            "generation": new_generation,
            "evidence_hash": object_hash(
                {
                    "review_sha256": review_sha256,
                    "target_sha256": evidence["target"]["sha256"],
                    "finding_ids": [
                        finding["finding_id"] for finding in normalized["findings"]
                    ],
                }
            ),
        }
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = new_generation
    desired_manifest["conversion_state"] = {
        "local_complete": "local_complete",
        "correction_required": "review_pending",
        "review_incomplete": "awaiting_user",
        "review_ambiguity": "awaiting_user",
    }[review_status]
    desired_manifest["final_markdown"] = final_markdown
    round_number = len(state.get("rounds", [])) + 1
    if evidence.get("round_id") != f"review-round-{round_number:04d}":
        raise ReviewError(
            "integrity_violation", "The review round identity is inconsistent."
        )
    review_path = (
        "04-review/review.json"
        if round_number == 1
        else f"04-review/review-round-{round_number:04d}.json"
    )
    report_path = (
        "04-review/review-report.md"
        if round_number == 1
        else f"04-review/review-report-round-{round_number:04d}.md"
    )
    round_summary = {
        "round_id": evidence["round_id"],
        "evidence_path": state["evidence"]["path"],
        "evidence_sha256": state["evidence"]["sha256"],
        "evidence_size_bytes": state["evidence"]["size_bytes"],
        "review_path": review_path,
        "review_sha256": review_sha256,
        "review_size_bytes": len(review_bytes),
        "report_path": report_path,
        "report_sha256": report_sha256,
        "report_size_bytes": len(report_bytes),
        "recorded_at": at,
    }
    desired_manifest["review"] = {
        "schema_version": SCHEMA_VERSION,
        "status": review_status,
        "reason_code": reason_code,
        "target": deepcopy(evidence["target"]),
        "evidence": deepcopy(state["evidence"]),
        "coverage": coverage,
        "rounds": [*deepcopy(state.get("rounds", [])), round_summary],
        "pending_action": decision_action,
    }
    desired_manifest["artifacts"].update(
        {
            "review": review_path,
            "review_report": report_path,
        }
    )
    if final_markdown is not None:
        desired_manifest["artifacts"]["final_markdown"] = final_markdown["path"]
    desired_private = deepcopy(private_state)
    desired_private["generation"] = new_generation
    operation_id = f"review-record-{uuid.uuid4().hex}"
    review_temporary_name = f".{operation_id}.review.part"
    report_temporary_name = f".{operation_id}.report.part"
    intent = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_intent",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "action_id": action_id,
        "evidence_hash": evidence_hash,
        "payload": normalized,
        "review": desired_manifest["review"],
        "previous_review": deepcopy(state),
        "previous_artifacts": {
            key: manifest.get("artifacts", {}).get(key)
            for key in ("review", "review_report")
        },
        "final_markdown": final_markdown,
        "review_document": review,
        "review_path": review_path,
        "report_path": report_path,
        "review_temporary_name": review_temporary_name,
        "report_temporary_name": report_temporary_name,
        "previous_manifest_hash": object_hash(manifest),
        "previous_private_hash": object_hash(private_state),
    }
    bundle.append_history(intent, state_fd=descriptors["state"])
    _write_exclusive(review_temporary_name, review_bytes, dir_fd=descriptors["review"])
    _write_exclusive(report_temporary_name, report_bytes, dir_fd=descriptors["review"])
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_prepared",
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "new_generation": new_generation,
        "at": at,
        "intent_hash": object_hash(intent),
        "review_sha256": review_sha256,
        "review_size_bytes": len(review_bytes),
        "report_sha256": report_sha256,
        "report_size_bytes": len(report_bytes),
    }
    bundle.append_history(prepared, state_fd=descriptors["state"])
    _promote(
        review_temporary_name,
        PurePosixPath(review_path).name,
        dir_fd=descriptors["review"],
    )
    _promote(
        report_temporary_name,
        PurePosixPath(report_path).name,
        dir_fd=descriptors["review"],
    )
    bundle.atomic_write_json("private.json", desired_private, dir_fd=descriptors["state"])
    bundle.atomic_write_json("manifest.json", desired_manifest, dir_fd=descriptors["root"])
    committed = {
        "schema_version": SCHEMA_VERSION,
        "event": "review_record_committed",
        "operation_id": operation_id,
        "previous_generation": expected_generation,
        "generation": new_generation,
        "at": at,
        "manifest_hash": object_hash(desired_manifest),
        "private_hash": object_hash(desired_private),
    }
    bundle.append_history(committed, state_fd=descriptors["state"])
    return desired_manifest


def _apply_review_open(
    manifest: dict, private_state: dict, intent: dict, prepared: dict, committed: dict
) -> tuple[dict, dict] | None:
    if (
        not all(
            isinstance(event, dict) and _current_schema(event)
            for event in (intent, prepared, committed)
        )
        or intent.get("event") != "review_open_intent"
        or prepared.get("event") != "review_open_prepared"
        or committed.get("event") != "review_open_committed"
        or prepared.get("operation_id") != intent.get("operation_id")
        or committed.get("operation_id") != intent.get("operation_id")
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or intent.get("previous_manifest_hash") != object_hash(manifest)
        or intent.get("previous_private_hash") != object_hash(private_state)
        or prepared.get("intent_hash") != object_hash(intent)
    ):
        return None
    review_state = intent.get("review")
    if not isinstance(review_state, dict):
        return None
    desired_manifest, desired_private = _desired_open_state(
        manifest,
        private_state,
        review_state=review_state,
        evidence_path=review_state.get("evidence", {}).get("path"),
        generation=intent["new_generation"],
    )
    if (
        committed.get("manifest_hash") != object_hash(desired_manifest)
        or committed.get("private_hash") != object_hash(desired_private)
        or committed.get("previous_generation") != manifest.get("generation")
        or committed.get("generation") != intent.get("new_generation")
    ):
        return None
    return desired_manifest, desired_private


def _apply_review_record(
    manifest: dict, private_state: dict, intent: dict, prepared: dict, committed: dict
) -> tuple[dict, dict] | None:
    if (
        not all(
            isinstance(event, dict) and _current_schema(event)
            for event in (intent, prepared, committed)
        )
        or intent.get("event") != "review_record_intent"
        or prepared.get("event") != "review_record_prepared"
        or committed.get("event") != "review_record_committed"
        or prepared.get("operation_id") != intent.get("operation_id")
        or committed.get("operation_id") != intent.get("operation_id")
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("new_generation") != manifest.get("generation") + 1
        or intent.get("previous_manifest_hash") != object_hash(manifest)
        or intent.get("previous_private_hash") != object_hash(private_state)
        or prepared.get("intent_hash") != object_hash(intent)
    ):
        return None
    desired_manifest = deepcopy(manifest)
    desired_manifest["generation"] = intent["new_generation"]
    review_state = intent.get("review")
    review_status = review_state.get("status") if isinstance(review_state, dict) else None
    desired_manifest["conversion_state"] = {
        "local_complete": "local_complete",
        "correction_required": "review_pending",
        "review_incomplete": "awaiting_user",
        "review_ambiguity": "awaiting_user",
    }.get(review_status, "awaiting_user")
    desired_manifest["final_markdown"] = deepcopy(intent.get("final_markdown"))
    desired_manifest["review"] = deepcopy(review_state)
    final = desired_manifest["final_markdown"]
    if (
        review_status
        not in {
            "local_complete",
            "review_incomplete",
            "correction_required",
            "review_ambiguity",
        }
        or (review_status == "local_complete" and not isinstance(final, dict))
        or (review_status != "local_complete" and final is not None)
    ):
        return None
    review_path = intent.get("review_path")
    report_path = intent.get("report_path")
    if not all(isinstance(path, str) for path in (review_path, report_path)):
        return None
    desired_manifest["artifacts"].update(
        {"review": review_path, "review_report": report_path}
    )
    if intent.get("request_kind") == "review_decision":
        desired_manifest["artifacts"]["review_decision"] = review_path
    if final is not None:
        desired_manifest["artifacts"]["final_markdown"] = final.get("path")
    desired_private = deepcopy(private_state)
    desired_private["generation"] = intent["new_generation"]
    if (
        committed.get("manifest_hash") != object_hash(desired_manifest)
        or committed.get("private_hash") != object_hash(desired_private)
        or committed.get("previous_generation") != manifest.get("generation")
        or committed.get("generation") != intent.get("new_generation")
    ):
        return None
    return desired_manifest, desired_private


def _correction_summary_artifacts(summary: dict) -> dict | None:
    if not isinstance(summary, dict):
        return None
    descriptors = []
    for key in ("corrected_markdown", "diff", "record", "review_evidence"):
        descriptor = summary.get(key)
        if not isinstance(descriptor, dict):
            return None
        descriptors.append((descriptor, False))
    assets = summary.get("assets")
    if not isinstance(assets, list):
        return None
    descriptors.extend((descriptor, True) for descriptor in assets)
    artifacts = {}
    try:
        for descriptor, is_asset in descriptors:
            if set(descriptor) != {"path", "sha256", "size_bytes"}:
                return None
            path = PurePosixPath(descriptor.get("path"))
            relative = path.relative_to("04-review")
            parsed = _correction_artifact_path(relative.as_posix())
            if (len(parsed.parts) == 2) != is_asset:
                return None
            name = parsed.as_posix()
            if name in artifacts:
                return None
            artifacts[name] = descriptor
    except (TypeError, ValueError, ReviewError):
        return None
    return artifacts


def _valid_correction_intent(intent: dict) -> bool:
    operation_id = intent.get("operation_id") if isinstance(intent, dict) else None
    artifact_manifest = (
        intent.get("artifact_manifest") if isinstance(intent, dict) else None
    )
    temporary_names = (
        intent.get("temporary_names") if isinstance(intent, dict) else None
    )
    correction_summary = intent.get("correction") if isinstance(intent, dict) else None
    expected_artifacts = _correction_summary_artifacts(correction_summary)
    return (
        isinstance(intent, dict)
        and _current_schema(intent)
        and intent.get("event") == "correction_record_intent"
        and isinstance(operation_id, str)
        and re.fullmatch(r"correction-record-[0-9a-f]{32}", operation_id) is not None
        and intent.get("new_generation") == intent.get("expected_generation") + 1
        and isinstance(intent.get("payload"), dict)
        and isinstance(intent.get("source_target"), dict)
        and isinstance(intent.get("source_review_document"), dict)
        and isinstance(intent.get("source_review_evidence"), dict)
        and isinstance(intent.get("correction_document"), dict)
        and isinstance(intent.get("resource_reference_oracle"), list)
        and isinstance(intent.get("review_evidence"), dict)
        and isinstance(intent.get("review"), dict)
        and isinstance(correction_summary, dict)
        and isinstance(intent.get("previous_review"), dict)
        and isinstance(intent.get("previous_corrections"), list)
        and isinstance(intent.get("previous_artifacts"), dict)
        and isinstance(artifact_manifest, dict)
        and expected_artifacts is not None
        and set(artifact_manifest) == set(expected_artifacts)
        and all(
            artifact_manifest[name]
            == {
                "sha256": descriptor["sha256"],
                "size_bytes": descriptor["size_bytes"],
            }
            for name, descriptor in expected_artifacts.items()
        )
        and all(
            isinstance(value, dict)
            and set(value) == {"sha256", "size_bytes"}
            and isinstance(value.get("sha256"), str)
            and value["sha256"].startswith("sha256:")
            and type(value.get("size_bytes")) is int
            and value["size_bytes"] >= 0
            and value["size_bytes"] <= MAX_CORRECTION_ARTIFACT_BYTES
            for value in artifact_manifest.values()
        )
        and isinstance(temporary_names, dict)
        and set(temporary_names) == set(artifact_manifest)
        and all(
            value == f".{operation_id}-{index:04d}.part"
            for index, (_name, value) in enumerate(
                sorted(temporary_names.items()), start=1
            )
        )
    )


def _valid_correction_prepared(prepared: dict, intent: dict) -> bool:
    return (
        isinstance(prepared, dict)
        and _current_schema(prepared)
        and prepared.get("event") == "correction_record_prepared"
        and prepared.get("operation_id") == intent.get("operation_id")
        and prepared.get("expected_generation") == intent.get("expected_generation")
        and prepared.get("new_generation") == intent.get("new_generation")
        and prepared.get("at") == intent.get("at")
        and prepared.get("intent_hash") == object_hash(intent)
        and prepared.get("artifact_manifest") == intent.get("artifact_manifest")
        and prepared.get("tree_hash")
        == object_hash({"artifacts": intent.get("artifact_manifest")})
    )


def _apply_correction_record(
    manifest: dict, private_state: dict, intent: dict, prepared: dict, committed: dict
) -> tuple[dict, dict] | None:
    if (
        not _valid_correction_intent(intent)
        or not _valid_correction_prepared(prepared, intent)
        or not isinstance(committed, dict)
        or not _current_schema(committed)
        or committed.get("event") != "correction_record_committed"
        or committed.get("operation_id") != intent.get("operation_id")
        or intent.get("expected_generation") != manifest.get("generation")
        or intent.get("previous_manifest_hash") != object_hash(manifest)
        or intent.get("previous_private_hash") != object_hash(private_state)
    ):
        return None
    desired = _correction_desired_state(manifest, private_state, intent)
    if desired is None:
        return None
    desired_manifest, desired_private = desired
    if (
        committed.get("previous_generation") != manifest.get("generation")
        or committed.get("generation") != intent.get("new_generation")
        or committed.get("manifest_hash") != object_hash(desired_manifest)
        or committed.get("private_hash") != object_hash(desired_private)
        or committed.get("tree_hash") != prepared.get("tree_hash")
    ):
        return None
    return desired_manifest, desired_private


def apply_settings_override_transition(previous: dict, updated: dict) -> dict:
    del previous
    transitioned = deepcopy(updated)
    review = transitioned.get("review")
    if not isinstance(review, dict):
        return transitioned
    status = review.get("status")
    pending = review.get("pending_action")
    if status in {"review_pending", "correction_required"} and isinstance(
        pending, dict
    ):
        rebound = deepcopy(pending)
        rebound["generation"] = transitioned["generation"]
        review["pending_action"] = rebound
    elif status == "review_ambiguity":
        mode = transitioned.get("settings_snapshot", {}).get("interaction_mode")
        if mode == "auto":
            review["pending_action"] = None
        elif mode == "confirm":
            if isinstance(pending, dict):
                rebound = deepcopy(pending)
                rebound["generation"] = transitioned["generation"]
                review["pending_action"] = rebound
            else:
                round_record = review.get("rounds", [{}])[-1]
                material = object_hash(
                    {
                        "round": round_record,
                        "target": review.get("target"),
                    }
                )
                review["pending_action"] = {
                    "kind": "resolve_review_ambiguity",
                    "action_id": "review-decision-"
                    + hashlib.sha256(
                        f"{transitioned['generation']}:{material}".encode("ascii")
                    ).hexdigest()[:32],
                    "generation": transitioned["generation"],
                    "evidence_hash": material,
                }
    transitioned["review"] = review
    return transitioned


def resolve_history_state(
    history: list[dict], *, manifest_template: dict, private_template: dict
) -> tuple[dict, dict] | None:
    first = next(
        (
            index
            for index, event in enumerate(history)
            if isinstance(event, dict) and event.get("event") == "review_open_intent"
        ),
        None,
    )
    if first is None:
        return raw_conversion.resolve_history_state(
            history,
            manifest_template=manifest_template,
            private_template=private_template,
        )
    prefix = raw_conversion.resolve_history_state(
        history[:first],
        manifest_template=manifest_template,
        private_template=private_template,
    )
    if prefix is None:
        return None
    manifest, private_state = prefix
    offset = first
    while offset < len(history):
        if offset + 2 >= len(history):
            return None
        intent, prepared, committed = history[offset : offset + 3]
        if not all(isinstance(item, dict) for item in (intent, prepared, committed)):
            return None
        if intent.get("event") == "settings_override_intent":
            transitioned = bundle.apply_settings_override_events(
                manifest,
                private_state,
                intent,
                prepared,
                committed,
                manifest_transform=apply_settings_override_transition,
            )
        elif intent.get("event") == "review_open_intent":
            transitioned = _apply_review_open(
                manifest, private_state, intent, prepared, committed
            )
        elif intent.get("event") == "review_record_intent":
            transitioned = _apply_review_record(
                manifest, private_state, intent, prepared, committed
            )
        elif intent.get("event") == "correction_record_intent":
            transitioned = _apply_correction_record(
                manifest, private_state, intent, prepared, committed
            )
        else:
            return None
        if transitioned is None:
            return None
        manifest, private_state = transitioned
        offset += 3
    return manifest, private_state


def valid_history(history: list[dict], manifest: dict, private_state: dict) -> bool:
    return resolve_history_state(
        history, manifest_template=manifest, private_template=private_state
    ) == (manifest, private_state)


def valid_manifest(manifest: dict) -> bool:
    review = manifest.get("review")
    if not isinstance(review, dict) or set(review) != {
        "schema_version",
        "status",
        "reason_code",
        "target",
        "evidence",
        "coverage",
        "rounds",
        "pending_action",
    }:
        return False
    if (
        not _current_schema(review)
        or review.get("status")
        not in {
            "review_pending",
            "local_complete",
            "review_incomplete",
            "correction_required",
            "review_ambiguity",
        }
        or not isinstance(review.get("target"), dict)
        or not isinstance(review.get("evidence"), dict)
        or not isinstance(review.get("coverage"), dict)
        or not isinstance(review.get("rounds"), list)
    ):
        return False
    rounds = review["rounds"]
    if not all(
        isinstance(item, dict)
        and item.get("round_id") == f"review-round-{index:04d}"
        and isinstance(item.get("evidence_path"), str)
        and isinstance(item.get("evidence_sha256"), str)
        and isinstance(item.get("review_path"), str)
        and isinstance(item.get("report_path"), str)
        and isinstance(item.get("review_sha256"), str)
        and isinstance(item.get("report_sha256"), str)
        for index, item in enumerate(rounds, start=1)
    ):
        return False
    corrections = manifest.get("corrections", [])
    if not isinstance(corrections, list) or not all(
        isinstance(item, dict)
        and _current_schema(item)
        and item.get("correction_id") == f"correction-{index:04d}"
        and item.get("path") == item.get("corrected_markdown", {}).get("path")
        and isinstance(item.get("source_target"), dict)
        and isinstance(item.get("finding_ids"), list)
        and item["finding_ids"]
        and all(isinstance(value, str) and value for value in item["finding_ids"])
        and all(
            isinstance(item.get(key), dict)
            and isinstance(item[key].get("path"), str)
            and isinstance(item[key].get("sha256"), str)
            and item[key]["sha256"].startswith("sha256:")
            and type(item[key].get("size_bytes")) is int
            and item[key]["size_bytes"] >= 0
            for key in ("corrected_markdown", "diff", "record", "review_evidence")
        )
        and isinstance(item.get("assets", []), list)
        and all(
            isinstance(asset, dict)
            and isinstance(asset.get("path"), str)
            and PurePosixPath(asset["path"]).parent
            == PurePosixPath("04-review/assets")
            and PurePosixPath(asset["path"]).suffix == ".png"
            and isinstance(asset.get("sha256"), str)
            and asset["sha256"].startswith("sha256:")
            and type(asset.get("size_bytes")) is int
            and 0 < asset["size_bytes"] <= MAX_CORRECTION_ARTIFACT_BYTES
            for asset in item.get("assets", [])
        )
        for index, item in enumerate(corrections, start=1)
    ):
        return False
    pending = review.get("pending_action")
    if manifest.get("conversion_state") == "review_pending":
        if review.get("status") == "review_pending":
            return (
                review.get("reason_code") is None
                and manifest.get("final_markdown") is None
                and isinstance(pending, dict)
                and pending.get("kind") == "record_review"
                and pending.get("generation") == manifest.get("generation")
            )
        return (
            review.get("status") == "correction_required"
            and review.get("reason_code") is None
            and manifest.get("final_markdown") is None
            and len(review["rounds"]) >= 1
            and isinstance(pending, dict)
            and pending.get("kind") == "record_correction"
            and pending.get("generation") == manifest.get("generation")
            and isinstance(pending.get("action_id"), str)
            and pending["action_id"].startswith("correction-")
            and isinstance(pending.get("evidence_hash"), str)
            and pending["evidence_hash"].startswith("sha256:")
        )
    if manifest.get("conversion_state") == "local_complete":
        final = manifest.get("final_markdown")
        raw_final = (
            isinstance(final, dict)
            and final.get("kind") == "raw_conversion"
            and final.get("path")
            == manifest.get("raw_conversion", {}).get("main_markdown_path")
            and final.get("sha256")
            == manifest.get("raw_conversion", {}).get("main_markdown_sha256")
            and not corrections
        )
        corrected_final = (
            isinstance(final, dict)
            and final.get("kind") == "corrected_markdown"
            and corrections
            and final.get("correction_id") == corrections[-1].get("correction_id")
            and final.get("path")
            == corrections[-1].get("corrected_markdown", {}).get("path")
            and "sha256:" + final.get("sha256", "")
            == corrections[-1].get("corrected_markdown", {}).get("sha256")
        )
        return (
            review.get("status") == "local_complete"
            and review.get("reason_code") is None
            and pending is None
            and len(review["rounds"]) >= 1
            and (raw_final or corrected_final)
        )
    if manifest.get("conversion_state") == "awaiting_user":
        interaction_mode = manifest.get("settings_snapshot", {}).get(
            "interaction_mode"
        )
        pending_valid = (
            pending is None
            if review.get("status") == "review_incomplete"
            or interaction_mode == "auto"
            else isinstance(pending, dict)
            and pending.get("kind") == "resolve_review_ambiguity"
            and pending.get("generation") == manifest.get("generation")
            and isinstance(pending.get("action_id"), str)
            and pending["action_id"].startswith("review-decision-")
            and isinstance(pending.get("evidence_hash"), str)
            and pending["evidence_hash"].startswith("sha256:")
        )
        return (
            review.get("status") in {"review_incomplete", "review_ambiguity"}
            and review.get("reason_code")
            == (
                "review_incomplete"
                if review.get("status") == "review_incomplete"
                else "review_ambiguity"
            )
            and pending_valid
            and len(review["rounds"]) >= 1
            and manifest.get("final_markdown") is None
            and "final_markdown" not in manifest.get("artifacts", {})
        )
    return False


def _valid_review_artifact_path(path) -> bool:
    parsed = PurePosixPath(path) if isinstance(path, str) else None
    return (
        isinstance(parsed, PurePosixPath)
        and not parsed.is_absolute()
        and len(parsed.parts) >= 2
        and parsed.parts[0] == "04-review"
        and all(part not in {"", ".", ".."} for part in parsed.parts)
    )


def _crop_asset_authorizations(manifest: dict) -> dict:
    authorized = {}
    for correction_summary in manifest.get("corrections", []):
        correction_id = (
            correction_summary.get("correction_id")
            if isinstance(correction_summary, dict)
            else None
        )
        for asset in correction_summary.get("assets", []):
            path = asset.get("path") if isinstance(asset, dict) else None
            if (
                not isinstance(correction_id, str)
                or not isinstance(path, str)
                or path in authorized
            ):
                raise ReviewError(
                    "integrity_violation",
                    "Correction crop authorization is inconsistent.",
                )
            authorized[path] = {
                "correction_id": correction_id,
                "sha256": asset.get("sha256"),
                "size_bytes": asset.get("size_bytes"),
            }
    return authorized


def _validate_target_local_resources(
    *, descriptors: dict, manifest: dict, bundle_root: Path
) -> None:
    target = manifest.get("review", {}).get("target")
    if not isinstance(target, dict) or not isinstance(
        target.get("local_resources"), dict
    ):
        raise ReviewError(
            "integrity_violation", "The reviewed resource snapshot is missing."
        )
    target_path = target.get("path")
    if not isinstance(target_path, str):
        raise ReviewError(
            "integrity_violation", "The reviewed Markdown target path is invalid."
        )
    markdown = _read_bundle_path(
        target_path,
        root_fd=descriptors["root"],
        max_bytes=MAX_MARKDOWN_BYTES,
    )
    if (
        _bytes_hash(markdown) != target.get("sha256")
        or target["local_resources"].get("markdown_path") != target_path
    ):
        raise ReviewError(
            "integrity_violation", "The reviewed Markdown target changed."
        )
    raw = manifest.get("raw_conversion", {})
    attempt_id = raw.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise ReviewError(
            "integrity_violation", "The active raw resource root is invalid."
        )
    try:
        markdown_assets.validate_local_reference_snapshot(
            markdown,
            bundle_root=bundle_root,
            snapshot=target["local_resources"],
            raw_root=f"03-converted/attempts/{attempt_id}/raw",
            crop_assets=_crop_asset_authorizations(manifest),
        )
    except markdown_assets.MarkdownAssetError:
        raise ReviewError(
            "integrity_violation",
            "A reviewed local resource no longer matches its authorized snapshot.",
        ) from None


def validate_committed_artifacts(
    *, descriptors: dict, manifest: dict, bundle_root: Path
) -> None:
    review = manifest["review"]
    _validate_target_local_resources(
        descriptors=descriptors,
        manifest=manifest,
        bundle_root=bundle_root,
    )
    evidence = review["evidence"]
    if (
        not _valid_review_artifact_path(evidence.get("path"))
    ):
        raise ReviewError(
            "integrity_violation", "The review evidence path is invalid."
        )
    evidence_bytes = _read_bundle_path(
        evidence["path"], root_fd=descriptors["root"], max_bytes=MAX_RECORD_BYTES
    )
    if "sha256:" + _bytes_hash(evidence_bytes) != evidence.get("sha256"):
        raise ReviewError("integrity_violation", "The review evidence hash changed.")
    for summary in review.get("rounds", []):
        for path_key, hash_key, size_key in (
            ("evidence_path", "evidence_sha256", "evidence_size_bytes"),
            ("review_path", "review_sha256", "review_size_bytes"),
            ("report_path", "report_sha256", "report_size_bytes"),
        ):
            path = summary.get(path_key) if isinstance(summary, dict) else None
            if (
                not _valid_review_artifact_path(path)
            ):
                raise ReviewError(
                    "integrity_violation", "A historical review artifact path is invalid."
                )
            artifact_bytes = _read_bundle_path(
                path,
                root_fd=descriptors["root"],
                max_bytes=MAX_RECORD_BYTES,
            )
            if (
                summary.get(hash_key) != "sha256:" + _bytes_hash(artifact_bytes)
                or summary.get(size_key) != len(artifact_bytes)
            ):
                raise ReviewError(
                    "integrity_violation", "A historical review artifact changed."
                )
    if (
        manifest["conversion_state"] in {"local_complete", "awaiting_user"}
        or review.get("status") == "correction_required"
    ):
        review_path = manifest.get("artifacts", {}).get("review")
        report_path = manifest.get("artifacts", {}).get("review_report")
        if not all(
            _valid_review_artifact_path(path)
            for path in (review_path, report_path)
        ):
            raise ReviewError(
                "integrity_violation", "The committed review artifact path is invalid."
            )
        review_bytes = _read_bundle_path(
            review_path,
            root_fd=descriptors["root"],
            max_bytes=MAX_RECORD_BYTES,
        )
        report_bytes = _read_bundle_path(
            report_path,
            root_fd=descriptors["root"],
            max_bytes=MAX_RECORD_BYTES,
        )
        if not report_bytes:
            raise ReviewError("integrity_violation", "The review report is empty.")
        rounds = review.get("rounds")
        summary = rounds[-1] if isinstance(rounds, list) and rounds else None
        if (
            not isinstance(summary, dict)
            or summary.get("review_path") != review_path
            or summary.get("report_path") != report_path
            or summary.get("review_sha256")
            != "sha256:" + _bytes_hash(review_bytes)
            or summary.get("review_size_bytes") != len(review_bytes)
            or summary.get("report_sha256")
            != "sha256:" + _bytes_hash(report_bytes)
            or summary.get("report_size_bytes") != len(report_bytes)
        ):
            raise ReviewError(
                "integrity_violation", "A committed review artifact hash changed."
            )
        if manifest["conversion_state"] == "local_complete" and (
            summary["review_sha256"]
            != manifest["final_markdown"]["review_sha256"]
        ):
            raise ReviewError("integrity_violation", "The final review hash changed.")

    corrections = manifest.get("corrections", [])
    if corrections:
        for item in corrections:
            artifact_descriptors = [
                item[key]
                for key in (
                    "corrected_markdown",
                    "diff",
                    "record",
                    "review_evidence",
                )
            ] + list(item.get("assets", []))
            for descriptor in artifact_descriptors:
                data = _read_bundle_path(
                    descriptor["path"],
                    root_fd=descriptors["root"],
                    max_bytes=MAX_CORRECTION_ARTIFACT_BYTES,
                )
                if (
                    "sha256:" + _bytes_hash(data) != descriptor["sha256"]
                    or len(data) != descriptor["size_bytes"]
                ):
                    raise ReviewError(
                        "integrity_violation", "A correction artifact changed."
                    )
        final = manifest.get("final_markdown")
        if isinstance(final, dict) and final.get("kind") == "corrected_markdown":
            final_bytes = _read_bundle_path(
                final["path"],
                root_fd=descriptors["root"],
                max_bytes=MAX_MARKDOWN_BYTES,
            )
            if _bytes_hash(final_bytes) != final.get("sha256"):
                raise ReviewError(
                    "integrity_violation", "The corrected final Markdown changed."
                )


def result_from_manifest(
    manifest: dict,
    *,
    work_bundle: str,
    outcome: str,
    pending_conversion_operation: bool = False,
) -> dict:
    result = raw_conversion.result_from_manifest(
        manifest,
        work_bundle=work_bundle,
        outcome=outcome,
        pending_conversion_operation=pending_conversion_operation,
    )
    review = manifest.get("review")
    pending = review.get("pending_action") if isinstance(review, dict) else None
    # Task 3.1d: this is design.md Decision 5's MISSED FIFTH WRAPPER (its
    # 2026-07-27 correction ①). Both branches below used to run
    # unconditionally, so every bundle carrying a review slice -- and
    # workflow._inspect_open_bundle dispatches on `has_review` FIRST -- had
    # the projector's answer overwritten or blanked outright, no matter what
    # tier had matched. Tier 1 (resume_pending_conversion_operation) is
    # defined to outrank everything, so that erasure was unconditional
    # precedence inversion, not a review-layer nicety.
    #
    # The review layer's own pending_action vocabulary is outside Decision
    # 5's tiers entirely, exactly like preflight's, so the two answers never
    # actually compete: whenever the projection produced nothing, the review
    # layer's own answer (or its evidence-hash fallback) is the right one and
    # this block behaves exactly as it always did; whenever it produced
    # something, that answer stands and this layer only adds its own keys
    # below. project_conversion_action is a pure function of the same
    # manifest preflight.result_from_manifest already passed it, so asking it
    # again here reads the one implementation rather than second-guessing
    # which layer wrote `result["action_required"]`.
    if (
        conversion_actions.project_conversion_action(
            manifest, pending_conversion_operation=pending_conversion_operation
        )
        is None
    ):
        if isinstance(pending, dict):
            result["action_required"] = pending["kind"]
            result["action_id"] = pending["action_id"]
            result["evidence_hash"] = pending["evidence_hash"]
        else:
            result["action_required"] = None
            result["action_id"] = None
            result["evidence_hash"] = (
                None if not isinstance(review, dict) else review["evidence"]["sha256"]
            )
    result["review_status"] = None if not isinstance(review, dict) else review["status"]
    result["review_coverage"] = (
        None if not isinstance(review, dict) else deepcopy(review["coverage"])
    )
    result["target_dialect"] = (
        None
        if not isinstance(review, dict)
        else review.get("target", {}).get("dialect")
    )
    result["final_markdown"] = manifest.get("final_markdown")
    return result
