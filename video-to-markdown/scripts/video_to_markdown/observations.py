from __future__ import annotations

from pathlib import Path

from . import sampling, state


class ObservationError(ValueError):
    pass


REQUIRED_KEYS = {
    "observation_id",
    "start_ms",
    "end_ms",
    "description",
    "evidence_frame_ids",
    "motion_dependent",
    "confidence",
    "needs_resample",
    "resample_reason",
}


def _frame_ids(batch_state: dict) -> set[str]:
    result = {
        item["frame_id"]
        for item in batch_state.get("first_pass", {}).get("frames", [])
        if isinstance(item, dict) and isinstance(item.get("frame_id"), str)
    }
    for round_record in batch_state.get("resampling_rounds") or []:
        result.update(
            item["frame_id"]
            for item in round_record.get("frames", [])
            if isinstance(item, dict) and isinstance(item.get("frame_id"), str)
        )
    return result


def latest_observations(document: dict) -> list[dict]:
    latest: dict[str, dict] = {}
    for revision in document.get("revisions") or []:
        if not isinstance(revision, dict):
            continue
        for item in revision.get("observations") or []:
            if isinstance(item, dict) and isinstance(item.get("observation_id"), str):
                previous = latest.get(item["observation_id"])
                if previous is None or int(item.get("revision", 0)) > int(
                    previous.get("revision", 0)
                ):
                    latest[item["observation_id"]] = item
    return [latest[key] for key in sorted(latest)]


def record(
    *,
    task_dir: Path,
    manifest: dict,
    batch_id: str,
    input_document: dict,
) -> dict:
    if not isinstance(input_document, dict) or input_document.get("schema_version") != 1:
        raise ObservationError("observation input must use schema_version 1")
    incoming = input_document.get("observations")
    if not isinstance(incoming, list) or not incoming:
        raise ObservationError("observation input must contain a non-empty observations array")
    summary, batch_state = sampling.load_batch(task_dir, manifest, batch_id)
    valid_frames = _frame_ids(batch_state)
    existing = state.read_json(task_dir / "visual-observations.json")
    previous_by_id = {item["observation_id"]: item for item in latest_observations(existing)}
    revision_number = int(batch_state.get("observation_revisions", 0)) + 1
    normalized = []
    seen = set()
    for item in incoming:
        if not isinstance(item, dict) or set(item) != REQUIRED_KEYS:
            raise ObservationError(
                "each observation must contain exactly: %s"
                % ", ".join(sorted(REQUIRED_KEYS))
            )
        observation_id = item["observation_id"]
        if not isinstance(observation_id, str) or not observation_id.strip() or observation_id in seen:
            raise ObservationError("observation_id must be non-empty and unique in the input")
        seen.add(observation_id)
        start_ms = item["start_ms"]
        end_ms = item["end_ms"]
        if type(start_ms) is not int or type(end_ms) is not int or start_ms > end_ms:
            raise ObservationError("observation time values must be ordered integers")
        if start_ms < summary["start_ms"] or end_ms > summary["end_ms"]:
            raise ObservationError("observation interval is outside the selected batch")
        description = item["description"]
        if not isinstance(description, str) or not description.strip():
            raise ObservationError("observation description is required")
        evidence = item["evidence_frame_ids"]
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(value, str) and value in valid_frames for value in evidence
        ):
            raise ObservationError("every observation must cite existing frame IDs")
        if type(item["motion_dependent"]) is not bool or type(item["needs_resample"]) is not bool:
            raise ObservationError("motion_dependent and needs_resample must be booleans")
        if item["confidence"] not in {"certain", "uncertain"}:
            raise ObservationError("confidence must be certain or uncertain")
        reason = item["resample_reason"]
        if item["needs_resample"] and (not isinstance(reason, str) or not reason.strip()):
            raise ObservationError("needs_resample requires resample_reason")
        if not item["needs_resample"] and reason not in {None, ""}:
            raise ObservationError("resample_reason must be null when needs_resample is false")
        previous_revision = int(previous_by_id.get(observation_id, {}).get("revision", 0))
        normalized.append(
            {
                **item,
                "observation_id": observation_id.strip(),
                "description": description.strip(),
                "resample_reason": reason.strip() if isinstance(reason, str) and reason else None,
                "batch_id": batch_id,
                "revision": previous_revision + 1,
                "recorded_at": state.utc_now(),
            }
        )
    existing.setdefault("revisions", []).append(
        {
            "batch_id": batch_id,
            "batch_revision": revision_number,
            "recorded_at": state.utc_now(),
            "observations": normalized,
        }
    )
    existing["latest"] = latest_observations(existing)
    state.atomic_write_json(task_dir / "visual-observations.json", existing)
    batch_state["observation_revisions"] = revision_number
    state.atomic_write_json(task_dir / summary["state_path"], batch_state)
    unresolved = any(
        item["confidence"] == "uncertain" or item["needs_resample"] for item in normalized
    )
    summary["status"] = "needs_resample" if unresolved else "observed"
    state.save_manifest(task_dir, manifest)
    return {"batch_id": batch_id, "revision": revision_number, "observations": normalized}
