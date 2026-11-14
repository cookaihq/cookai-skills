"""FFmpeg neighbor screening followed by the task's selected visual judge."""
from __future__ import annotations

import json
from pathlib import Path

from . import media, observations, sampling, state, vision


def search(task_dir: Path, manifest: dict, *, batch_id: str, frame_id: str, issue_id: str,
           ffmpeg_bin: str, ffprobe_bin: str, original_checked: bool, reason: str,
           radius_ms: int | None = None, interval_ms: int = 125, crop=None) -> dict:
    if not original_checked:
        raise vision.VisionError("inspect the original-resolution image with the selected vision route before declaring source blur")
    summary, batch = sampling.load_batch(task_dir, manifest, batch_id)
    reference = next((f for f in vision.all_frames(batch) if f["frame_id"] == frame_id), None)
    if reference is None:
        raise vision.VisionError("unknown reference frame")
    if interval_ms < 100 or interval_ms > 200:
        raise vision.VisionError("clarity interval must be between 100 and 200 ms")
    existing = manifest["vision"]["issues"].get(issue_id, {})
    radius = radius_ms or (1000 if not existing.get("rounds") else 2000)
    if radius < 100 or radius > 4000:
        raise vision.VisionError("clarity search radius must be 100..4000 ms")
    start = max(0, reference["pts_ms"] - radius)
    end = min(manifest["source"]["duration_ms"] - 1, reference["pts_ms"] + radius)
    issue = vision.begin_issue(task_dir, manifest, issue_id, start_ms=start, end_ms=end, reason=reason)
    source = media.verify_source(manifest["source"])
    frames = []
    seen = set()
    for requested in [reference["pts_ms"], *range(start, end + 1, interval_ms)]:
        frame = media.extract_frame(source, requested_ms=requested, output_dir=task_dir / "assets" / "frames",
                                    ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
        if frame["frame_id"] in seen:
            continue
        seen.add(frame["frame_id"])
        score = media.blur_score(Path(frame["path"]), ffmpeg_bin, crop)
        distance = abs(frame["pts_ms"] - reference["pts_ms"])
        frame.update(blur_score=score, distance_ms=distance,
                     rank_score=round(score + distance / 1000 * 0.25, 6))
        frames.append(frame)
    ranked = sorted(frames, key=lambda f: (f["rank_score"], f["distance_ms"]))
    record = {"issue_id": issue_id, "round": issue["rounds"], "reference_frame_id": frame_id,
              "start_ms": start, "end_ms": end, "interval_ms": interval_ms,
              "ranking": "lower blurdetect score + 0.25 per second of time distance; semantic identity is checked separately",
              "crop": list(crop) if crop else None, "frames": frames,
              "shortlist": [f["frame_id"] for f in ranked[:4]], "status": "awaiting_model_confirmation"}
    batch.setdefault("clarity_searches", []).append(record)
    state.atomic_write_json(task_dir / summary["state_path"], batch)
    dispatches = vision.prepare_dispatch(task_dir, manifest, batch_id, purpose="clarity", frames=ranked[:4],
                                         reference_frame_id=frame_id, context={"issue_id": issue_id, "reason": reason})
    record["dispatch_ids"] = [d["dispatch_id"] for d in dispatches]
    state.atomic_write_json(task_dir / summary["state_path"], batch)
    state.save_manifest(task_dir, manifest)
    return record


def apply(task_dir: Path, manifest: dict, *, batch_id: str, issue_id: str) -> dict:
    summary, batch = sampling.load_batch(task_dir, manifest, batch_id)
    records = [r for r in batch.get("clarity_searches", []) if r["issue_id"] == issue_id]
    if not records:
        raise vision.VisionError("no saved clarity search for this issue")
    search_record = records[-1]
    if search_record.get("status") == "resolved":
        return search_record
    checks: dict[str, list[dict]] = {}
    for dispatch_id in search_record["dispatch_ids"]:
        dispatch, _ = vision.dispatch_input(task_dir, manifest, dispatch_id)
        if dispatch["status"] != "complete":
            raise vision.VisionError("clarity model confirmation is incomplete")
        for check in state.read_json(task_dir / dispatch["result"])["checks"]:
            checks.setdefault(check["frame_id"], []).append(check)
    ranked = sorted(search_record["frames"], key=lambda f: (f["rank_score"], f["distance_ms"]))
    selected = next((f for f in ranked if f["frame_id"] in checks and all(c["readable"] and c["same_content"] for c in checks[f["frame_id"]])), None)
    if selected is None:
        search_record["status"] = "unresolved"
        state.atomic_write_json(task_dir / summary["state_path"], batch)
        issue = manifest["vision"]["issues"][issue_id]
        return {"status": "unresolved", "issue_id": issue_id, "remaining_rounds": max(0, issue.get("max_rounds", 2) - issue["rounds"])}
    reference = next(f for f in vision.all_frames(batch) if f["frame_id"] == search_record["reference_frame_id"])
    # Save a replacement intent first; recovery can repeat the idempotent edits.
    replacement = {"old_frame_id": reference["frame_id"], "new_frame_id": selected["frame_id"],
                   "old_pts_ms": reference["pts_ms"], "pts_ms": selected["pts_ms"],
                   "reason": checks[selected["frame_id"]][0]["reason"], "issue_id": issue_id}
    search_record.update(status="applying", replacement=replacement)
    state.atomic_write_json(task_dir / summary["state_path"], batch)
    visual_path = task_dir / "visual-observations.json"
    document = state.read_json(visual_path)
    latest = observations.latest_observations(document)
    changed = []
    for obs in latest:
        if reference["frame_id"] in obs.get("evidence_frame_ids", []):
            updated = {**obs, "evidence_frame_ids": [selected["frame_id"] if v == reference["frame_id"] else v for v in obs["evidence_frame_ids"]],
                       "revision": int(obs.get("revision", 0)) + 1, "recorded_at": state.utc_now(),
                       "replacement": replacement}
            # Content identity was confirmed, not every unresolved fact in the observation.
            changed.append(updated)
    if changed:
        document["revisions"].append({"batch_id": batch_id, "kind": "clarity_replacement", "observations": changed})
        document["latest"] = observations.latest_observations(document)
        state.atomic_write_json(visual_path, document)
    old_relative = str(Path(reference["path"]).relative_to(task_dir)).replace("\\", "/")
    new_relative = str(Path(selected["path"]).relative_to(task_dir)).replace("\\", "/")
    markdown_path = task_dir / manifest["artifacts"]["document"]
    markdown = markdown_path.read_text(encoding="utf-8")
    from .review import IMAGE_LINK
    from urllib.parse import quote, unquote
    def replace_image(match):
        if unquote(match.group("target").strip("<>")) != old_relative:
            return match.group(0)
        updated = match.group(0).replace(match.group("target"), quote(new_relative, safe="/-._~"))
        old_time, new_time = media.format_timecode(reference["pts_ms"]), media.format_timecode(selected["pts_ms"])
        caption, rest = updated.split("](", 1)
        caption = caption.replace(old_time, new_time)
        if new_time not in caption:
            caption += " · " + new_time
        return caption + "](" + rest
    state.atomic_write_bytes(markdown_path, IMAGE_LINK.sub(replace_image, markdown).encode("utf-8"), mode=0o644)
    report_path = task_dir / "review-report.json"
    if report_path.exists():
        report = state.read_json(report_path)
        for interval in report.get("intervals", []):
            interval["media"] = [new_relative if p == old_relative else p for p in interval.get("media", [])]
        replacements = report.setdefault("frame_replacements", [])
        if replacement not in replacements:
            replacements.append(replacement)
        state.atomic_write_json(report_path, report)
    search_record["status"] = "resolved"
    state.atomic_write_json(task_dir / summary["state_path"], batch)
    manifest["vision"]["issues"][issue_id].update(status="resolved", replacement=replacement)
    # Replacing a previously reviewed screenshot invalidates that screenshot review.
    manifest["vision"]["screenshot_reviews"] = [r for r in manifest["vision"]["screenshot_reviews"] if r["path"] not in {old_relative, new_relative}]
    state.save_manifest(task_dir, manifest)
    return search_record


def screenshot_dispatches(task_dir: Path, manifest: dict) -> list[dict]:
    from .review import IMAGE_LINK
    from urllib.parse import unquote
    markdown = (task_dir / manifest["artifacts"]["document"]).read_text(encoding="utf-8")
    wanted = {unquote(m.group("target").strip("<>")) for m in IMAGE_LINK.finditer(markdown)}
    groups: dict[str, list[dict]] = {}
    for summary in manifest["batches"]:
        batch = state.read_json(task_dir / summary["state_path"])
        for f in vision.all_frames(batch):
            relative = Path(f["path"]).relative_to(task_dir).as_posix()
            if relative in wanted and Path(f["path"]).suffix.lower() != ".gif":
                groups.setdefault(summary["batch_id"], []).append(f)
                wanted.remove(relative)
    unknown = [p for p in wanted if not p.lower().endswith(".gif")]
    if unknown:
        raise vision.VisionError("final screenshots must be known original-resolution PNG evidence: " + ", ".join(unknown))
    return [d for batch_id, frames in groups.items() for d in vision.prepare_dispatch(task_dir, manifest, batch_id, purpose="screenshots", frames=frames)]


def accept_screenshots(task_dir: Path, manifest: dict) -> dict:
    reviews = []
    for dispatch in screenshot_dispatches(task_dir, manifest):
        if dispatch["status"] != "complete":
            raise vision.VisionError("final screenshot review is not complete")
        _, payload = vision.dispatch_input(task_dir, manifest, dispatch["dispatch_id"])
        frames = {f["frame_id"]: f for f in payload["frames"]}
        for check in state.read_json(task_dir / dispatch["result"])["checks"]:
            frame = frames[check["frame_id"]]
            reviews.append({"path": Path(frame["path"]).relative_to(task_dir).as_posix(), "sha256": frame["sha256"],
                            "pts_ms": frame["pts_ms"], "dispatch_id": dispatch["dispatch_id"], **check})
    manifest["vision"]["screenshot_reviews"] = reviews
    state.save_manifest(task_dir, manifest)
    return {"reviewed": len(reviews), "unreadable": [r for r in reviews if not r["readable"] or not r["same_content"]]}
