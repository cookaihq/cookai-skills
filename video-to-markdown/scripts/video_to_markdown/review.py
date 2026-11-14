from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote

from . import media, observations, state


CLASSIFICATIONS = {
    "represented_in_body",
    "represented_by_screenshot",
    "represented_by_gif",
    "deliberately_skipped",
    "unresolved",
}
IMAGE_LINK = re.compile(r"!\[[^\]\n]*\]\((?P<target><[^>\n]+>|[^\s)]+)(?:\s+[\"'][^\n]*?[\"'])?\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


class ReviewError(ValueError):
    pass


def _read_required_json(path: Path, blockers: list[str]) -> dict:
    try:
        value = state.read_json(path)
    except state.StateError as exc:
        blockers.append(str(exc))
        return {}
    if not isinstance(value, dict):
        blockers.append("%s must contain a JSON object" % path.name)
        return {}
    return value


def _safe_relative(task_dir: Path, value: str) -> Path | None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    resolved = (task_dir / path).resolve()
    try:
        resolved.relative_to(task_dir.resolve())
    except ValueError:
        return None
    return resolved


def _gfm_anchors(markdown: str) -> set[str]:
    anchors = set()
    counts: dict[str, int] = {}
    for heading in HEADING.findall(markdown):
        text = re.sub(r"[`*_~\[\]()]", "", heading.strip().lower())
        text = re.sub(r"[^\w\-\s\u0080-\uffff]", "", text)
        slug = re.sub(r"\s+", "-", text).strip("-")
        if not slug:
            continue
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add("#%s%s" % (slug, "-%d" % count if count else ""))
    return anchors


def _verify_source(manifest: dict, blockers: list[str]) -> None:
    try:
        media.verify_source(manifest.get("source") or {})
    except media.MediaError as exc:
        blockers.append(str(exc))


def _verify_batch_artifacts(task_dir: Path, manifest: dict, blockers: list[str], gaps: list[str]) -> set[str]:
    frame_ids = set()
    duration = manifest.get("source", {}).get("duration_ms")
    for summary in manifest.get("batches") or []:
        if summary.get("status") not in {"observed"}:
            gaps.append(
                "%s is not fully observed (status=%s)"
                % (summary.get("batch_id"), summary.get("status"))
            )
        try:
            batch_state = state.read_json(task_dir / summary["state_path"])
        except (KeyError, state.StateError) as exc:
            blockers.append("cannot read %s batch state: %s" % (summary.get("batch_id"), exc))
            continue
        frame_groups = [batch_state.get("first_pass", {}).get("frames", [])]
        frame_groups.extend(
            item.get("frames", []) for item in batch_state.get("resampling_rounds") or []
        )
        frame_groups.extend(r.get("frames", []) for r in batch_state.get("clarity_searches", []))
        for group in frame_groups:
            for frame in group:
                if not isinstance(frame, dict):
                    blockers.append("batch frame record is not an object")
                    continue
                frame_id = frame.get("frame_id")
                pts_ms = frame.get("pts_ms")
                if isinstance(frame_id, str):
                    frame_ids.add(frame_id)
                if type(pts_ms) is not int or type(duration) is not int or not 0 <= pts_ms <= duration:
                    blockers.append("frame %s has an out-of-range PTS" % frame_id)
                path_value = frame.get("path")
                if not isinstance(path_value, str):
                    blockers.append("frame %s has no path" % frame_id)
                    continue
                path = Path(path_value).resolve()
                try:
                    path.relative_to((task_dir / "assets" / "frames").resolve())
                except ValueError:
                    blockers.append("frame %s is outside assets/frames" % frame_id)
                    continue
                if not path.is_file():
                    blockers.append("frame file is missing: %s" % path)
                    continue
                digest, _ = state.sha256_file(path)
                if digest != frame.get("sha256"):
                    blockers.append("frame hash changed: %s" % path.name)
    return frame_ids


def _verify_motion(task_dir: Path, manifest: dict, blockers: list[str]) -> None:
    duration = manifest.get("source", {}).get("duration_ms")
    for record in manifest.get("motion_assets") or []:
        start_ms = record.get("start_ms")
        end_ms = record.get("end_ms")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or type(duration) is not int
            or not (0 <= start_ms < end_ms <= duration)
        ):
            blockers.append("motion asset has an out-of-range interval")
        path = _safe_relative(task_dir, str(record.get("path") or ""))
        if path is None or not path.is_file():
            blockers.append("motion asset is missing or unsafe: %s" % record.get("path"))
            continue
        digest, _ = state.sha256_file(path)
        if digest != record.get("sha256"):
            blockers.append("motion asset hash changed: %s" % path.name)


def _verify_transcript_timing(
    transcript_doc: dict,
    duration_ms: object,
    blockers: list[str],
) -> None:
    segments = transcript_doc.get("segments")
    if not isinstance(segments, list):
        blockers.append("transcript.json segments must be an array")
        return
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            blockers.append("transcript segment %d is not an object" % index)
            continue
        segment_id = segment.get("segment_id") or "at index %d" % index
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if start_ms is None and end_ms is None:
            continue
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or type(duration_ms) is not int
            or not (0 <= start_ms <= end_ms <= duration_ms)
        ):
            blockers.append(
                "transcript segment %s has invalid normalized times" % segment_id
            )


def validate_task(task_dir: Path, manifest: dict, *, allow_draft: bool = False) -> dict:
    blockers: list[str] = []
    gaps: list[str] = []
    undispatched_batches: set[str] = set()
    _verify_source(manifest, blockers)
    enabled = manifest.get("vision", {}).get("enabled", True)
    _verify_batch_artifacts(task_dir, manifest, blockers, gaps)
    _verify_motion(task_dir, manifest, blockers)
    if enabled and not manifest.get("adaptive_review_performed"):
        blockers.append("adaptive second-pass review has not been recorded")
    visual_state = manifest.get("vision", {})
    if enabled and not visual_state.get("route"):
        blockers.append("vision method has not been selected")
    if not enabled and (not visual_state.get("reason") or manifest.get("batches") or manifest.get("motion_assets")):
        blockers.append("skipped visuals must have an explicit reason and no visual work or media")

    document_name = manifest.get("artifacts", {}).get("document")
    document_path = _safe_relative(task_dir, str(document_name or ""))
    markdown = ""
    if document_path is None or not document_path.is_file():
        blockers.append("document Markdown is missing or unsafe")
    else:
        try:
            markdown = document_path.read_text(encoding="utf-8")
        except OSError as exc:
            blockers.append("cannot read document Markdown: %s" % exc)
    anchors = _gfm_anchors(markdown)
    markdown_media = set()
    for match in IMAGE_LINK.finditer(markdown):
        value = unquote(match.group("target").strip("<>"))
        path = _safe_relative(task_dir, value)
        if path is None or not path.is_file():
            blockers.append("Markdown media is missing or unsafe: %s" % value)
        else:
            markdown_media.add(value)

    transcript_doc = _read_required_json(task_dir / "transcript.json", blockers)
    visual_doc = _read_required_json(task_dir / "visual-observations.json", blockers)
    review_doc = _read_required_json(task_dir / "review-report.json", blockers)
    _verify_transcript_timing(
        transcript_doc,
        manifest.get("source", {}).get("duration_ms"),
        blockers,
    )
    transcript_ids = {
        item["segment_id"]
        for item in transcript_doc.get("segments") or []
        if isinstance(item, dict) and isinstance(item.get("segment_id"), str)
    }
    latest_visual = observations.latest_observations(visual_doc)
    if not enabled:
        if latest_visual or markdown_media or visual_doc.get("status") != "skipped":
            blockers.append("visuals were explicitly skipped; visual evidence or media must not be fabricated")
        if not transcript_ids:
            blockers.append("skipped visuals require actual subtitle/audio evidence")
    elif not transcript_ids and (transcript_doc.get("source", {}).get("kind") != "no_audio" or manifest.get("asr", {}).get("state") != "skipped_no_audio"):
        blockers.append("empty transcript must have a verified no-audio source state")
    if enabled:
        from .vision import all_frames, dispatch_input
        for summary in manifest.get("batches", []):
            batch = state.read_json(task_dir / summary["state_path"])
            expected_frames = {f["frame_id"] for f in batch.get("first_pass", {}).get("frames", [])}
            expected_frames.update(f["frame_id"] for r in batch.get("resampling_rounds", []) for f in r.get("frames", []))
            seen_frames: set[str] = set()
            for dispatch in visual_state.get("dispatches", []):
                if dispatch["batch_id"] != summary["batch_id"] or dispatch["purpose"] != "observe" or dispatch["status"] != "complete":
                    continue
                try:
                    _, payload = dispatch_input(task_dir, manifest, dispatch["dispatch_id"])
                    seen_frames.update(f["frame_id"] for f in payload["frames"])
                except (ValueError, OSError) as exc:
                    blockers.append("vision dispatch integrity check failed: " + str(exc))
            if expected_frames - seen_frames:
                undispatched_batches.add(summary["batch_id"])
                gaps.append("batch has original/supplement frames without completed vision dispatches: " + summary["batch_id"])
        for relative in markdown_media:
            if relative.lower().endswith(".gif"):
                continue
            path = _safe_relative(task_dir, relative)
            frames = [f for b in manifest.get("batches", []) for f in all_frames(state.read_json(task_dir / b["state_path"]))]
            frame = next((f for f in frames if Path(f["path"]).resolve() == path), None)
            if path and path.suffix.lower() != ".png":
                blockers.append("final screenshot must be an original-resolution PNG: " + relative)
            if frame is None or frame.get("crop") or not frame.get("pts_verified"):
                blockers.append("final screenshot lacks verified original-frame PTS: " + relative)
            check = next((r for r in visual_state.get("screenshot_reviews", []) if r["path"] == relative and path and r["sha256"] == state.sha256_file(path)[0]), None)
            if check is None:
                blockers.append("final screenshot has not been reviewed with the selected vision route: " + relative)
            else:
                dispatch = next((d for d in visual_state.get("dispatches", []) if d["dispatch_id"] == check["dispatch_id"]), None)
                if dispatch is None or dispatch["purpose"] != "screenshots" or dispatch["status"] != "complete":
                    blockers.append("screenshot review lacks a completed screenshot dispatch: " + relative)
                else:
                    try:
                        _, payload = dispatch_input(task_dir, manifest, dispatch["dispatch_id"])
                        result_path = task_dir / dispatch["result"]
                        if state.sha256_file(result_path)[0] != dispatch["result_sha256"]:
                            raise ReviewError("screenshot result changed")
                        checked = state.read_json(result_path)["checks"]
                        if not any(c["frame_id"] == check["frame_id"] and all(c[k] == check[k] for k in ("readable", "same_content", "reason")) for c in checked):
                            raise ReviewError("screenshot receipt disagrees with the model result")
                    except (ValueError, OSError, KeyError) as exc:
                        blockers.append("screenshot review integrity failed: " + type(exc).__name__)
                if not check["readable"] or not check["same_content"]:
                    gaps.append("final screenshot is not readable or does not preserve content: " + relative)
    observation_ids = {
        item["observation_id"]
        for item in latest_visual
        if isinstance(item.get("observation_id"), str)
    }
    for item in latest_visual:
        if item.get("confidence") == "uncertain" or item.get("needs_resample") is True:
            gaps.append("visual observation remains unresolved: %s" % item.get("observation_id"))

    if review_doc.get("schema_version") != 1:
        blockers.append("review-report.json must use schema_version 1")
    if review_doc.get("document") != document_name:
        blockers.append("review report document does not match manifest")
    intervals = review_doc.get("intervals")
    if not isinstance(intervals, list) or not intervals:
        blockers.append("review report must contain coverage intervals")
        intervals = []
    expected_start = 0
    consumed_transcript = set()
    consumed_observations = set()
    duration = manifest.get("source", {}).get("duration_ms")
    for index, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            blockers.append("review interval %d is not an object" % index)
            continue
        start_ms = interval.get("start_ms")
        end_ms = interval.get("end_ms")
        classification = interval.get("classification")
        if type(start_ms) is not int or type(end_ms) is not int or start_ms >= end_ms:
            blockers.append("review interval %d has invalid times" % index)
            continue
        if start_ms != expected_start:
            blockers.append(
                "review timeline gap or overlap before interval %d: expected %s, got %s"
                % (index, expected_start, start_ms)
            )
        expected_start = end_ms
        if type(duration) is int and end_ms > duration:
            blockers.append("review interval %d exceeds video duration" % index)
        if classification not in CLASSIFICATIONS:
            blockers.append("review interval %d has an unknown classification" % index)
            continue
        cited_observations = interval.get("observation_ids") or []
        cited_transcript = interval.get("transcript_segment_ids") or []
        if not isinstance(cited_observations, list) or not isinstance(cited_transcript, list):
            blockers.append("review interval %d evidence IDs must be arrays" % index)
            continue
        unknown_observations = set(cited_observations) - observation_ids
        unknown_transcript = set(cited_transcript) - transcript_ids
        if unknown_observations:
            blockers.append("review interval %d cites unknown observations" % index)
        if unknown_transcript:
            blockers.append("review interval %d cites unknown transcript segments" % index)
        consumed_observations.update(cited_observations)
        consumed_transcript.update(cited_transcript)
        reason = interval.get("reason")
        anchor = interval.get("document_anchor")
        interval_media = interval.get("media") or []
        if classification == "unresolved":
            gaps.append("timed unresolved interval: %s..%s" % (start_ms, end_ms))
            if not isinstance(reason, str) or not reason.strip() or reason not in markdown or media.format_timecode(start_ms) not in markdown:
                blockers.append("unresolved interval requires its exact timestamp and reason in the draft Markdown")
        if classification in {"deliberately_skipped", "unresolved"}:
            if not isinstance(reason, str) or not reason.strip():
                blockers.append("skipped interval %d requires a concrete reason" % index)
        else:
            if not isinstance(anchor, str) or anchor not in anchors:
                blockers.append("represented interval %d cites a missing document anchor" % index)
            if not cited_observations and not cited_transcript:
                blockers.append("represented interval %d cites no source evidence" % index)
        if classification in {"represented_by_screenshot", "represented_by_gif"}:
            if not isinstance(interval_media, list) or not interval_media:
                blockers.append("media interval %d cites no media" % index)
            for value in interval_media:
                if not isinstance(value, str):
                    blockers.append("media interval %d contains a non-string path" % index)
                    continue
                path = _safe_relative(task_dir, value)
                if path is None or not path.is_file():
                    blockers.append("review media is missing or unsafe: %s" % value)
                    continue
                if value not in markdown_media:
                    blockers.append("review media is not embedded in Markdown: %s" % value)
                suffix = path.suffix.lower()
                if classification == "represented_by_gif" and suffix != ".gif":
                    blockers.append("GIF interval cites non-GIF media: %s" % value)
                if classification == "represented_by_screenshot" and suffix not in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                }:
                    blockers.append("screenshot interval cites non-image media: %s" % value)
    if type(duration) is int and expected_start != duration:
        blockers.append(
            "review timeline ends at %s ms but source duration is %s ms"
            % (expected_start, duration)
        )
    missing_transcript = transcript_ids - consumed_transcript
    missing_observations = observation_ids - consumed_observations
    if missing_transcript:
        blockers.append(
            "unexplained transcript segments: %s" % ", ".join(sorted(missing_transcript))
        )
    if missing_observations:
        blockers.append(
            "unexplained visual observations: %s" % ", ".join(sorted(missing_observations))
        )

    draft_ready = False
    if allow_draft and gaps:
        if "待复核草稿" not in markdown and "DRAFT" not in markdown:
            blockers.append("draft Markdown needs a visible 待复核草稿 or DRAFT label")
        unresolved_intervals = [i for i in intervals if isinstance(i, dict) and i.get("classification") == "unresolved"]
        if not unresolved_intervals:
            blockers.append("draft requires timed unresolved coverage intervals")
        for obs in latest_visual:
            if obs.get("confidence") == "uncertain" or obs.get("needs_resample"):
                if not any(obs["observation_id"] in i.get("observation_ids", []) and i["start_ms"] <= obs["start_ms"] and i["end_ms"] >= obs["end_ms"] for i in unresolved_intervals):
                    blockers.append("unresolved observation must be represented as a timed gap: " + obs["observation_id"])
        limit = visual_state.get("call_limit")
        call_budget_exhausted = limit is not None and visual_state.get("model_calls", 0) >= limit
        for interval in unresolved_intervals:
            issues = [visual_state.get("issues", {}).get(key, {}) for key in interval.get("issue_ids", [])]
            covered_to = interval["start_ms"]
            for issue in sorted((i for i in issues if i.get("rounds", 0) >= i.get("max_rounds", 2)), key=lambda i: i.get("start_ms", -1)):
                if issue.get("start_ms", -1) <= covered_to:
                    covered_to = max(covered_to, issue.get("end_ms", -1))
            bounded = call_budget_exhausted or covered_to >= interval["end_ms"]
            if not bounded:
                blockers.append("each draft gap needs its own saved exhausted issue or model-call budget")
        for batch in manifest.get("batches", []):
            unobserved = batch["batch_id"] in undispatched_batches or not any(o.get("batch_id") == batch["batch_id"] for o in latest_visual)
            if unobserved and not any(i["start_ms"] <= batch["start_ms"] and i["end_ms"] >= batch["end_ms"] for i in unresolved_intervals):
                blockers.append("unobserved batch must be covered by a timed unresolved interval: " + batch["batch_id"])
        draft_ready = not blockers

    result = {
        "complete": not blockers and not gaps,
        "draft_ready": draft_ready,
        "gaps": gaps,
        "checked_at": state.utc_now(),
        "blockers": blockers + ([] if draft_ready else gaps),
        "document_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if markdown
        else None,
    }
    manifest["validation"] = result
    if result["complete"] or manifest.get("stage") == "complete":
        manifest["completed_once"] = True
    manifest["stage"] = "validated" if result["complete"] else "draft_validated" if draft_ready else "blocked_validation"
    state.save_manifest(task_dir, manifest)
    return result
