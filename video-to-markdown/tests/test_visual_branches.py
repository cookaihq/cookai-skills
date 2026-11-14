import json
import subprocess
from pathlib import Path

import pytest

from video_to_markdown import clarity, cli, delivery, media, review, sampling, state, vision
from test_review import fixture_task


def subtitle(tmp_path):
    path = tmp_path / "speech.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nOnly spoken content.\n", encoding="utf-8")
    return path


def silent_video(tmp_path):
    path = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=10:duration=2", "-c:v", "libx264", str(path)], check=True, capture_output=True)
    return path


def test_explicit_visual_skip_does_not_select_sample_or_call_visual_services(tmp_path, monkeypatch, capsys):
    video = silent_video(tmp_path)
    speech = subtitle(tmp_path)
    monkeypatch.chdir(tmp_path)
    def forbidden(*a, **k):
        raise AssertionError("visual pipeline must be bypassed")
    monkeypatch.setattr(vision, "select", forbidden)
    monkeypatch.setattr(sampling, "prepare_first_pass", forbidden)
    args = ["start", "--video", str(video), "--subtitle", str(speech), "--project-root", str(tmp_path), "--skip-visuals", "user says video pictures are irrelevant"]
    assert cli.main(args) == 0
    started = json.loads(capsys.readouterr().out)
    task = Path(started["task_dir"])
    _, manifest, _ = state.load_task(task)
    assert manifest["vision"]["status"] == "skipped" and manifest["batches"] == []
    assert manifest["asr"]["state"] == "skipped_supplied_transcript"
    assert not list((task / "assets" / "frames").iterdir())
    (task / manifest["artifacts"]["document"]).write_text("# Spoken\n\nOnly spoken content.\n", encoding="utf-8")
    state.atomic_write_json(task / "review-report.json", {"schema_version": 1, "document": manifest["artifacts"]["document"],
        "intervals": [{"start_ms": 0, "end_ms": manifest["source"]["duration_ms"], "classification": "represented_in_body", "document_anchor": "#spoken",
                       "transcript_segment_ids": ["segment-000001"], "observation_ids": [], "media": [], "reason": None}]})
    assert review.validate_task(task, manifest)["complete"]
    assert cli.main(["resume", "--task-dir", str(task)]) == 0
    assert json.loads(capsys.readouterr().out)["next_action"] == "finalize"


def test_silent_video_skips_asr_and_missing_both_content_sources_is_an_error(tmp_path, monkeypatch, capsys):
    video = silent_video(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.asr, "advance", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audio means no ASR")))
    caps = tmp_path / "caps.json"
    state.atomic_write_json(caps, {"schema_version": 1, "host": "codex", "source": "test host", "main_vision": True, "models": []})
    assert cli.main(["start", "--video", str(video), "--project-root", str(tmp_path), "--vision-method", "main-agent", "--capabilities", str(caps)]) == 0
    task = Path(json.loads(capsys.readouterr().out)["task_dir"])
    _, manifest, _ = state.load_task(task)
    assert manifest["asr"]["state"] == "skipped_no_audio"
    assert state.read_json(task / "transcript.json")["segments"] == []
    assert manifest["batches"][0]["frame_count"] > 0
    # A second, distinct input avoids task-name collisions within one second.
    other = tmp_path / "other-silent.mp4"
    other.write_bytes(video.read_bytes())
    assert cli.main(["start", "--video", str(other), "--project-root", str(tmp_path), "--skip-visuals", "user request"]) == 1
    assert "no source content" in capsys.readouterr().err


def test_unresolved_draft_is_deliverable_but_never_complete(tmp_path):
    task, manifest = fixture_task(tmp_path)
    visual = state.read_json(task / "visual-observations.json")
    visual["revisions"][0]["observations"][0].update(confidence="uncertain", needs_resample=True)
    state.atomic_write_json(task / "visual-observations.json", visual)
    manifest["vision"]["issues"]["blurred-title"] = {"rounds": 2, "start_ms": 0, "end_ms": 1000, "status": "unresolved"}
    manifest["delivery"] = delivery.initialize("sample.mp4", "20261114000000-sample.mp4", None)
    (task / "document.md").write_text("# Opening\n\n> 待复核草稿\n\n00:00:00.000 标题无法辨认。\n", encoding="utf-8")
    report = state.read_json(task / "review-report.json")
    report["intervals"][0].update(classification="unresolved", reason="标题无法辨认。", media=[], issue_ids=["blurred-title"])
    state.atomic_write_json(task / "review-report.json", report)
    assert review.validate_task(task, manifest)["complete"] is False
    value = review.validate_task(task, manifest, allow_draft=True)
    assert not value["complete"] and value["draft_ready"]
    final = delivery.finalize(task, title=None, keep_name=True, copy_directory=None, draft=True)
    assert final["stage"] == "draft_delivered" and not final["validation"]["complete"]
    _, restored, _ = state.load_task(final["task_dir"])
    assert restored.get("completed_once") is not True


def test_unrelated_exhausted_issue_cannot_validate_another_draft_gap(tmp_path):
    task, manifest = fixture_task(tmp_path)
    manifest["vision"]["issues"]["elsewhere"] = {"rounds": 2, "start_ms": 5000, "end_ms": 6000}
    (task / "document.md").write_text("# Opening\n\nDRAFT\n00:00:00.000 unreadable\n", encoding="utf-8")
    report = state.read_json(task / "review-report.json")
    report["intervals"][0].update(classification="unresolved", media=[], reason="unreadable", issue_ids=["elsewhere"])
    state.atomic_write_json(task / "review-report.json", report)
    value = review.validate_task(task, manifest, allow_draft=True)
    assert not value["draft_ready"]


def test_partial_blur_gap_does_not_discard_the_observed_rest_of_the_batch(tmp_path):
    task, manifest = fixture_task(tmp_path)
    manifest["batches"][0]["status"] = "needs_resample"
    visual = state.read_json(task / "visual-observations.json")
    visual["revisions"][0]["observations"][0].update(end_ms=200, confidence="uncertain", needs_resample=True)
    state.atomic_write_json(task / "visual-observations.json", visual)
    manifest["vision"]["issues"]["title"] = {"rounds": 2, "start_ms": 0, "end_ms": 200}
    (task / "document.md").write_text("# Opening\n\nDRAFT\n00:00:00.000 unreadable title\n\nKnown speech remains.\n", encoding="utf-8")
    report = state.read_json(task / "review-report.json")
    report["intervals"] = [
        {"start_ms": 0, "end_ms": 200, "classification": "unresolved", "document_anchor": None, "media": [],
         "observation_ids": ["visual-1"], "transcript_segment_ids": [], "reason": "unreadable title", "issue_ids": ["title"]},
        {"start_ms": 200, "end_ms": 1000, "classification": "represented_in_body", "document_anchor": "#opening", "media": [],
         "observation_ids": [], "transcript_segment_ids": ["segment-000001"], "reason": None}]
    state.atomic_write_json(task / "review-report.json", report)
    assert review.validate_task(task, manifest, allow_draft=True)["draft_ready"]
    # An exhausted 200 ms issue cannot justify declaring a full second unknown.
    report["intervals"] = [{**report["intervals"][0], "end_ms": 1000, "transcript_segment_ids": ["segment-000001"]}]
    state.atomic_write_json(task / "review-report.json", report)
    assert not review.validate_task(task, manifest, allow_draft=True)["draft_ready"]


def test_neighbor_ranking_rejects_clear_changed_page_and_updates_references(tmp_path, monkeypatch):
    task, manifest = fixture_task(tmp_path)
    summary, batch = sampling.load_batch(task, manifest, "batch-0001")
    reference = batch["first_pass"]["frames"][0]
    candidate = {**reference, "frame_id": "clear-same-page", "pts_ms": 500, "source_pts_ms": 500,
                 "path": str(task / "assets/frames/clear-same-page.png"), "blur_score": 2, "distance_ms": 500, "rank_score": 2.125}
    changed = {**reference, "frame_id": "clear-different-page", "pts_ms": 900,
               "path": str(task / "assets/frames/clear-different-page.png"), "blur_score": 1, "distance_ms": 900, "rank_score": 1.225}
    for f in (candidate, changed):
        Path(f["path"]).write_bytes(b"frame")
    record = {"issue_id": "blur", "round": 1, "reference_frame_id": reference["frame_id"], "frames": [candidate, changed],
              "shortlist": [changed["frame_id"], candidate["frame_id"]], "status": "awaiting_model_confirmation"}
    batch["clarity_searches"] = [record]
    state.atomic_write_json(task / summary["state_path"], batch)
    manifest["vision"]["issues"]["blur"] = {"rounds": 1, "start_ms": 0, "end_ms": 1000, "status": "unresolved"}
    dispatch = vision.prepare_dispatch(task, manifest, "batch-0001", purpose="clarity", frames=[changed, candidate], reference_frame_id=reference["frame_id"])[0]
    record["dispatch_ids"] = [dispatch["dispatch_id"]]
    state.atomic_write_json(task / summary["state_path"], batch)
    _, payload = vision.dispatch_input(task, manifest, dispatch["dispatch_id"])
    checks = [{"frame_id": f["frame_id"], "readable": f["frame_id"] != reference["frame_id"],
               "same_content": f["frame_id"] != changed["frame_id"], "reason": "same page" if f["frame_id"] != changed["frame_id"] else "page changed"} for f in payload["frames"]]
    vision.record_result(task, manifest, dispatch["dispatch_id"], {"schema_version": 1, "observations": [], "checks": checks}, actual_model=None)
    applied = clarity.apply(task, manifest, batch_id="batch-0001", issue_id="blur")
    assert applied["replacement"]["new_frame_id"] == candidate["frame_id"]
    assert "clear-same-page.png" in (task / "document.md").read_text()
    assert "clear-same-page.png" in json.dumps(state.read_json(task / "review-report.json"))
    assert state.read_json(task / "visual-observations.json")["latest"][0]["evidence_frame_ids"] == [candidate["frame_id"]]
    assert manifest["vision"]["screenshot_reviews"] == []
