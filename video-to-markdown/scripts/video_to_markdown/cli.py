from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from . import asr, clarity, config, delivery, media, observations, paths, review, sampling, state, transcript, vision


class WorkflowError(ValueError):
    pass


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _runtime_config(args) -> tuple[Path, config.ResolvedConfig]:
    cwd = Path.cwd().resolve()
    resolved = config.resolve(cwd=cwd, use_home=bool(getattr(args, "use_local_key", False)) and not getattr(args, "no_global_config", False), read_vision=not bool(getattr(args, "skip_visuals", False)))
    return cwd, resolved


def _prepare_transcript(
    *,
    task_dir: Path,
    manifest: dict,
    private: dict,
    resolved: config.ResolvedConfig,
    source_path: Path,
    subtitle: str | None,
    language: str | None,
    options: dict | None = None,
) -> dict | None:
    if subtitle:
        subtitle_path = Path(subtitle).expanduser().resolve()
        manifest["transcript_source"] = {"kind": "supplied", "path": str(subtitle_path)}
        state.save_manifest(task_dir, manifest)
        value = transcript.parse_subtitle(
            subtitle_path,
            language=language,
            duration_ms=manifest.get("source", {}).get("duration_ms"),
        )
        transcript.write_transcript(task_dir, value)
        manifest["asr"]["state"] = "skipped_supplied_transcript"
        manifest["transcript_source"] = {
            "kind": value["source"]["kind"],
            "path": str(subtitle_path),
        }
        state.save_manifest(task_dir, manifest)
        return None
    if not any(s.get("codec_type") == "audio" for s in manifest.get("source", {}).get("streams", [])):
        if not manifest["vision"]["enabled"]:
            raise WorkflowError("no usable subtitles or audio track, and visuals were explicitly skipped; no source content is available")
        transcript.write_transcript(task_dir, {"schema_version": 1, "source": {"kind": "no_audio"}, "language": language, "segments": []})
        manifest["asr"]["state"] = "skipped_no_audio"
        state.save_manifest(task_dir, manifest)
        return None
    audio_path = task_dir / "source" / "audio-for-asr.mp3"
    if manifest.get("audio_for_asr"):
        audio_path = media.verify_audio(task_dir, manifest["audio_for_asr"])
    elif audio_path.exists():
        raise WorkflowError("prepared audio has no saved identity; refusing to reuse it")
    else:
        audio_record = media.extract_audio(source_path, audio_path, resolved.ffmpeg_bin, ffprobe_bin=resolved.ffprobe_bin, source_record=manifest["source"])
        manifest["audio_for_asr"] = {
            **audio_record, "path": "source/audio-for-asr.mp3",
        }
        state.save_manifest(task_dir, manifest)
    forwarded = {key: value for key, value in (options or {}).items() if key not in {"language", "task_dir"}}
    return asr.advance(task_dir, manifest, private, resolved=resolved, audio_path=audio_path, language=language, **forwarded)


def _finish_preparation(
    task_dir: Path,
    manifest: dict,
    resolved: config.ResolvedConfig,
    source_path: Path,
) -> None:
    manifest["stage"] = "transcript_ready"
    state.save_manifest(task_dir, manifest)
    state.create_empty_artifacts(task_dir, manifest["artifacts"]["document"])
    if not manifest["vision"]["enabled"]:
        manifest["stage"] = "writing_document"
        state.atomic_write_json(task_dir / "visual-observations.json", {"schema_version": 1, "revisions": [],
                                "status": "skipped", "reason": manifest["vision"]["reason"]})
        state.save_manifest(task_dir, manifest)
        return
    sampling.prepare_first_pass(
        task_dir=task_dir,
        manifest=manifest,
        source=source_path,
        ffmpeg_bin=resolved.ffmpeg_bin,
        ffprobe_bin=resolved.ffprobe_bin,
    )
    manifest["stage"] = "observation_in_progress"
    state.save_manifest(task_dir, manifest)


def _choose_vision(args, task_dir, manifest, resolved):
    if manifest["vision"].get("route") and (getattr(args, "vision_method", None) or getattr(args, "vision_config", None) or getattr(args, "vision_model", None)):
        raise WorkflowError("this task already has a saved vision route; use vision-switch with an explicit first affected batch and reason")
    if not manifest["vision"]["enabled"] or manifest["vision"].get("route"):
        return None
    host = getattr(args, "host", None) or manifest["settings"].get("host", "codex")
    caps = vision.capabilities(getattr(args, "capabilities", None), host=host)
    route, ref = None, None
    if getattr(args, "vision_config", None):
        ref = str(Path(args.vision_config).expanduser().resolve())
        route = state.read_json(Path(ref))
    elif getattr(args, "vision_method", None):
        route = {"method": args.vision_method}
        if args.vision_model:
            route["model"] = args.vision_model
        if args.accept_unverified_model:
            route["accept_unverified_model"] = True
    pending = vision.select(task_dir, manifest, resolved, host=host, caps=caps, override=route, credential_ref=ref)
    return pending if pending["next_action"] == "choose_vision_once" else None


def command_start(args) -> int:
    cwd, resolved = _runtime_config(args)
    if args.provider:
        resolved = replace(resolved, asr_provider=args.provider, sources={**resolved.sources, "asr_provider": "task option"})
    source_policy = paths.parse_source_policy(resolved.source_video_dir)
    versions = media.preflight(resolved.ffmpeg_bin, resolved.ffprobe_bin)
    project_root = paths.detect_project_root(cwd, args.project_root)
    output_parent = paths.resolve_output_parent(
        cwd=cwd,
        project_root=project_root,
        configured_base=resolved.output_base,
        explicit_parent=args.output_parent,
    )
    name = paths.task_dir_name(args.video)
    _, document_name = paths.output_names(name.split("-", 1)[0], paths.source_filename(args.video))
    copy_directory = paths.resolve_copy_directory(args.video, cwd=cwd, explicit=args.copy_dir)
    task_dir = paths.create_task_directory(output_parent, name)
    manifest, private = state.initialize_task(
        task_dir,
        document_name=document_name,
        settings_snapshot={
            **resolved.public_snapshot(),
            "invocation_cwd": str(cwd),
            "project_root": str(project_root),
            "output_parent": str(output_parent),
            "source_policy": source_policy.mode,
            "runtime_versions": versions,
            "language": args.language,
            "only_provider": args.only_provider,
            "no_global_config": args.no_global_config,
        },
    )
    manifest["delivery"] = delivery.initialize(args.video, name, copy_directory)
    manifest["settings"]["host"] = args.host or "codex"
    if args.skip_visuals:
        manifest["vision"].update(enabled=False, status="skipped", reason=args.skip_visuals, content_source="subtitles_or_audio")
    if args.subtitle:
        manifest["transcript_source"] = {"kind": "supplied", "path": str(Path(args.subtitle).expanduser().resolve())}
    state.save_manifest(task_dir, manifest)
    with state.task_lock(task_dir):
        return _start_prepared(args, task_dir, manifest, private, resolved, source_policy, name)


def _start_prepared(args, task_dir, manifest, private, resolved, source_policy, name) -> int:
    if paths.is_http_url(args.video):
        private["source_origin"] = args.video
        state.save_private(task_dir, private)
    source_path, source_record = media.prepare_source(
        args.video,
        task_dir=task_dir,
        task_name=name,
        policy=source_policy,
    )
    probe = media.probe_video(source_path, resolved.ffprobe_bin)
    source_record.update(probe)
    manifest["source"] = source_record
    manifest["stage"] = "source_ready"
    state.save_manifest(task_dir, manifest)
    pending_vision = _choose_vision(args, task_dir, manifest, resolved)
    if pending_vision:
        _print(pending_vision)
        return 0
    pending_asr = _prepare_transcript(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        resolved=resolved,
        source_path=source_path,
        subtitle=args.subtitle,
        language=args.language,
        options=vars(args),
    )
    if pending_asr:
        _print(pending_asr)
        return 0
    _finish_preparation(task_dir, manifest, resolved, source_path)
    pending = [item["batch_id"] for item in manifest["batches"] if item["status"] == "ready_for_observation"]
    _print(
        {
            "task_dir": str(task_dir),
            "document": str(task_dir / manifest["artifacts"]["document"]),
            "copy_directory": manifest["delivery"]["copy_directory"],
            "stage": manifest["stage"],
            "next_action": "inspect_batch" if manifest["vision"]["enabled"] else "write_document",
            "next_batch_id": pending[0] if pending else None,
            **asr.public_status(private),
        }
    )
    return 0


def _resolved_from_manifest(manifest: dict, *, use_home: bool) -> config.ResolvedConfig:
    invocation = manifest.get("settings", {}).get("invocation_cwd")
    if not isinstance(invocation, str):
        raise WorkflowError("manifest does not contain the invocation directory")
    return config.resolve(cwd=invocation, use_home=use_home, read_vision=manifest.get("vision", {}).get("enabled", True) and not bool(manifest.get("vision", {}).get("route")))


def command_resume(args) -> int:
    task_dir, manifest, private = state.load_task(args.task_dir)
    vision.finish_switch(task_dir, manifest)
    if args.use_local_key:
        manifest["settings"]["use_home"] = True
    if args.no_global_config:
        manifest["settings"]["no_global_config"] = True
        manifest["settings"]["use_home"] = False
    if args.use_local_key or args.no_global_config:
        state.save_manifest(task_dir, manifest)
    resolved = _resolved_from_manifest(manifest, use_home=(args.use_local_key or manifest["settings"].get("use_home", False)) and not (args.no_global_config or manifest["settings"].get("no_global_config")))
    media.preflight(resolved.ffmpeg_bin, resolved.ffprobe_bin)
    source_path = media.verify_source(manifest.get("source") or {})
    if manifest.get("audio_for_asr"):
        media.verify_audio(task_dir, manifest["audio_for_asr"])
    pending_vision = _choose_vision(args, task_dir, manifest, resolved)
    if pending_vision:
        _print(pending_vision)
        return 0
    if manifest.get("draft_delivered_once"):
        validation = review.validate_task(task_dir, manifest, allow_draft=True)
        manifest["stage"] = "draft_delivered" if validation.get("draft_ready") and delivery.copy_is_current(task_dir, manifest) else "draft_validated" if validation.get("draft_ready") else manifest["stage"]
        state.save_manifest(task_dir, manifest)
        _print({"task_dir": str(task_dir), "stage": manifest["stage"], "next_action": "draft_delivered" if manifest["stage"] == "draft_delivered" else "repair_artifacts", "validation": validation})
        return 0 if validation.get("draft_ready") else 1
    if manifest.get("stage") == "complete" or manifest.get("completed_once"):
        manifest["completed_once"] = True
        validation = review.validate_task(task_dir, manifest)
        copied = validation["complete"] and delivery.copy_is_current(task_dir, manifest)
        if copied:
            manifest["stage"] = "complete"
            state.save_manifest(task_dir, manifest)
        _print(
            {
                "task_dir": str(task_dir),
                "stage": manifest["stage"],
                "next_action": "complete" if copied else "finalize" if validation["complete"] else "repair_artifacts",
                "validation": validation,
                "document": str(task_dir / manifest["artifacts"]["document"]),
                "copy_directory": manifest["delivery"]["copy_directory"],
                "asr_task_id": manifest.get("asr", {}).get("task_id"),
                **asr.public_status(private),
            }
        )
        return 0 if validation["complete"] else 1
    if not (task_dir / "transcript.json").exists():
        pending_asr = _prepare_transcript(
            task_dir=task_dir,
            manifest=manifest,
            private=private,
            resolved=resolved,
            source_path=source_path,
            subtitle=(manifest.get("transcript_source") or {}).get("path"),
            language=manifest.get("settings", {}).get("language"),
            options=vars(args),
        )
        if pending_asr:
            _print(pending_asr)
            return 0
    _finish_preparation(task_dir, manifest, resolved, source_path)
    pending = [
        item["batch_id"]
        for item in manifest.get("batches") or []
        if item.get("status") in {"ready_for_observation", "needs_resample"}
    ]
    _print(
        {
            "task_dir": str(task_dir),
            "stage": manifest["stage"],
            "document": str(task_dir / manifest["artifacts"]["document"]),
            "next_action": "write_document" if not manifest["vision"]["enabled"] else "inspect_batch" if pending else "adaptive_review",
            "next_batch_id": pending[0] if pending else None,
            "asr_task_id": manifest.get("asr", {}).get("task_id"),
            **asr.public_status(private),
        }
    )
    return 0


def command_batch(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    summary, batch_state = sampling.load_batch(task_dir, manifest, args.batch_id)
    transcript_doc = state.read_json(task_dir / "transcript.json")
    relevant = []
    for segment in transcript_doc.get("segments") or []:
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if start_ms is None or end_ms is None or (
            start_ms < summary["end_ms"] and end_ms >= summary["start_ms"]
        ):
            relevant.append(segment)
    _print(
        {
            "task_dir": str(task_dir),
            "batch": batch_state,
            "transcript_segments": relevant,
            "dispatches": vision.prepare_dispatch(task_dir, manifest, summary["batch_id"]),
            "next_action": "execute each saved vision dispatch with the selected method; reuse one Agent context for this batch",
        }
    )
    return 0


def command_resample(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    resolved = _resolved_from_manifest(manifest, use_home=False)
    source_path = media.verify_source(manifest["source"])
    result = sampling.resample(
        task_dir=task_dir,
        manifest=manifest,
        source=source_path,
        batch_id=args.batch_id,
        start_ms=media.parse_timecode(args.start),
        end_ms=media.parse_timecode(args.end),
        reason=args.reason,
        interval_ms=args.interval_ms,
        crop=sampling.parse_crop(args.crop),
        ffmpeg_bin=resolved.ffmpeg_bin,
        ffprobe_bin=resolved.ffprobe_bin,
        issue_id=args.issue_id,
    )
    _print(result)
    return 0


def command_adaptive_review(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    if not args.reason.strip():
        raise WorkflowError("adaptive review reason is required")
    if not manifest["vision"]["enabled"]:
        raise WorkflowError("visual review is skipped for this task; write the subtitle/audio coverage report directly")
    unresolved = [
        item["batch_id"]
        for item in manifest.get("batches") or []
        if item.get("status") != "observed"
    ]
    if unresolved and not args.draft:
        raise WorkflowError(
            "all batches must be observed before closing adaptive review: %s"
            % ", ".join(unresolved)
        )
    manifest["adaptive_review_performed"] = True
    manifest["adaptive_review"] = {
        "decision": "bounded_unresolved_draft" if unresolved else "no_additional_sampling_needed",
        "reason": args.reason.strip(),
        "recorded_at": state.utc_now(),
    }
    state.save_manifest(task_dir, manifest)
    _print(manifest["adaptive_review"])
    return 0


def command_make_gif(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    resolved = _resolved_from_manifest(manifest, use_home=False)
    source_path = media.verify_source(manifest["source"])
    result = sampling.make_gif(
        task_dir=task_dir,
        manifest=manifest,
        source=source_path,
        start_ms=media.parse_timecode(args.start),
        end_ms=media.parse_timecode(args.end),
        purpose=args.purpose,
        ffmpeg_bin=resolved.ffmpeg_bin,
        ffprobe_bin=resolved.ffprobe_bin,
    )
    _print(result)
    return 0


def command_validate(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    result = review.validate_task(task_dir, manifest, allow_draft=args.draft)
    _print(result)
    return 0 if result["complete"] or result.get("draft_ready") else 1


def command_finalize(args) -> int:
    _print(delivery.finalize(Path(args.task_dir), title=args.title,
                             keep_name=args.keep_name, copy_directory=args.copy_dir, draft=args.draft))
    return 0


def command_vision(args) -> int:
    if args.command == "vision-options":
        _print(vision.options(vision.capabilities(args.capabilities, host=args.host or "codex")))
        return 0
    if args.command == "vision-models":
        _print(vision.model_catalog(state.read_json(Path(args.vision_config).resolve())))
        return 0
    task_dir, manifest, _ = state.load_task(args.task_dir)
    if args.command == "vision-switch":
        resolved = _resolved_from_manifest(manifest, use_home=manifest["settings"].get("use_home", False))
        host = args.host or manifest["vision"].get("host", "codex")
        caps = vision.capabilities(args.capabilities, host=host)
        route = state.read_json(Path(args.vision_config).resolve()) if args.vision_config else {"method": args.vision_method, **({"model": args.vision_model} if args.vision_model else {}), **({"accept_unverified_model": True} if args.accept_unverified_model else {})}
        result = vision.switch_route(task_dir, manifest, resolved, host=host, caps=caps, route=route,
                                     credential_ref=str(Path(args.vision_config).resolve()) if args.vision_config else None,
                                     from_batch=args.from_batch, reason=args.reason)
    elif args.command == "vision-extend":
        result = vision.extend_issue(task_dir, manifest, args.issue_id, rounds=args.rounds, reason=args.reason)
    elif args.command == "vision-limit":
        result = vision.set_call_limit(task_dir, manifest, limit=None if args.unlimited else args.limit, reason=args.reason)
    elif args.command == "vision-reconcile":
        record, _ = vision.dispatch_input(task_dir, manifest, args.dispatch_id)
        received = state.read_json(task_dir / (".state/vision/" + args.dispatch_id + "-received.json"))
        result = vision.record_result(task_dir, manifest, args.dispatch_id, received["result"],
                                      actual_model=received["actual_model"],
                                      model_calls=received.get("usage", {}).get("model_calls", 1), already_reserved=True)
    elif args.command == "vision-run":
        result = vision.execute(task_dir, manifest, args.dispatch_id)
    elif args.command == "vision-begin":
        record, _ = vision.dispatch_input(task_dir, manifest, args.dispatch_id)
        result = vision.reserve_call(task_dir, manifest, record)
    elif args.command == "vision-record":
        record, _ = vision.dispatch_input(task_dir, manifest, args.dispatch_id)
        if record["status"] not in {"running", "complete", "model_unverified", "response_invalid"}:
            raise WorkflowError("use vision-begin before invoking host Agent tools; reconcile unknown requests instead of starting them twice")
        result = vision.record_result(task_dir, manifest, args.dispatch_id, state.read_json(Path(args.input).resolve()),
                                      actual_model=args.actual_model, agent_id=args.agent_id,
                                      model_calls=None if args.model_calls_unknown else args.model_calls, already_reserved=True)
    elif args.command == "clarity-search":
        resolved = _resolved_from_manifest(manifest, use_home=False)
        result = clarity.search(task_dir, manifest, batch_id=args.batch_id, frame_id=args.frame_id, issue_id=args.issue_id,
                                original_checked=args.original_checked, reason=args.reason, radius_ms=args.radius_ms,
                                interval_ms=args.interval_ms, crop=sampling.parse_crop(args.crop),
                                ffmpeg_bin=resolved.ffmpeg_bin, ffprobe_bin=resolved.ffprobe_bin)
    elif args.command == "clarity-apply":
        result = clarity.apply(task_dir, manifest, batch_id=args.batch_id, issue_id=args.issue_id)
    elif args.command == "screenshot-review":
        result = clarity.accept_screenshots(task_dir, manifest) if args.accept else {"dispatches": clarity.screenshot_dispatches(task_dir, manifest)}
    else:
        raise WorkflowError("unknown vision operation")
    _print(result)
    return 0


def command_config_ui(args) -> int:
    from .config_ui import run
    return run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="video-to-markdown deterministic workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--video", required=True)
    start.add_argument("--subtitle")
    start.add_argument("--language")
    start.add_argument("--output-parent")
    start.add_argument("--copy-dir", help="portable Markdown copy directory; defaults to the original local video's directory")
    start.add_argument("--project-root")
    start.add_argument("--use-local-key", action="store_true")
    start.add_argument("--skip-visuals", metavar="REASON", help="user explicitly says visual content is irrelevant; skip the entire visual pipeline")
    start.set_defaults(handler=command_start)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--task-dir", required=True)
    resume.add_argument("--use-local-key", action="store_true")
    resume.set_defaults(handler=command_resume)

    start.add_argument("--provider", choices=config.PROVIDERS)
    start.add_argument("--only-provider", action="store_true")
    for operation in (start, resume):
        operation.add_argument("--host", choices=vision.HOSTS)
        operation.add_argument("--capabilities", help="actual current-session capability evidence JSON")
        operation.add_argument("--vision-method", choices=vision.METHODS)
        operation.add_argument("--vision-model")
        operation.add_argument("--vision-config", help="private task-only route JSON; kept by reference, no Key copied")
        operation.add_argument("--accept-unverified-model", action="store_true")
        operation.add_argument("--aihub-unavailable", action="store_true", help="host verified that AIhub Audio is unavailable")
        operation.add_argument("--aihub-plugin-dir", help="actual installed Plugin root, after the host loads AIhub Audio")
        operation.add_argument("--no-global-config", action="store_true")
        operation.add_argument("--choice", choices=("aihub-once", "aihub-default", "pause", "continue"))
        operation.add_argument("--feishu-profile")
        operation.add_argument("--confirm-feishu-identity")
        operation.add_argument("--save-feishu-identity", action="store_true")
        operation.add_argument("--retry-provider", action="store_true", help="retry a known rejected operation after repairing it; never retries unknown writes")
        operation.add_argument("--request", help="the user's original task request, retained privately for AIhub Audio result checking")

    batch = subparsers.add_parser("batch")
    batch.add_argument("--task-dir", required=True)
    batch.add_argument("--batch-id")
    batch.set_defaults(handler=command_batch)

    resample = subparsers.add_parser("resample")
    resample.add_argument("--task-dir", required=True)
    resample.add_argument("--batch-id", required=True)
    resample.add_argument("--start", required=True)
    resample.add_argument("--end", required=True)
    resample.add_argument("--reason", required=True)
    resample.add_argument("--interval-ms", type=int)
    resample.add_argument("--crop")
    resample.add_argument("--issue-id", required=True)
    resample.set_defaults(handler=command_resample)

    adaptive = subparsers.add_parser("adaptive-review")
    adaptive.add_argument("--task-dir", required=True)
    adaptive.add_argument("--reason", required=True)
    adaptive.add_argument("--draft", action="store_true", help="record bounded unresolved review without asserting complete success")
    adaptive.set_defaults(handler=command_adaptive_review)

    gif = subparsers.add_parser("make-gif")
    gif.add_argument("--task-dir", required=True)
    gif.add_argument("--start", required=True)
    gif.add_argument("--end", required=True)
    gif.add_argument("--purpose", required=True)
    gif.set_defaults(handler=command_make_gif)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--task-dir", required=True)
    validate.add_argument("--draft", action="store_true")
    validate.set_defaults(handler=command_validate)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--task-dir", required=True)
    finalize.add_argument("--draft", action="store_true")
    naming = finalize.add_mutually_exclusive_group()
    naming.add_argument("--title", help="content-derived title for an opaque original filename")
    naming.add_argument("--keep-name", action="store_true", help="the original filename is meaningful")
    finalize.add_argument("--copy-dir", help="override the saved copy destination, including when retrying delivery")
    finalize.set_defaults(handler=command_finalize)

    opts = subparsers.add_parser("vision-options")
    opts.add_argument("--host", choices=vision.HOSTS)
    opts.add_argument("--capabilities")
    opts.set_defaults(handler=command_vision)
    catalog = subparsers.add_parser("vision-models")
    catalog.add_argument("--vision-config", required=True)
    catalog.set_defaults(handler=command_vision)
    switch = subparsers.add_parser("vision-switch")
    switch.add_argument("--task-dir", required=True)
    switch.add_argument("--from-batch", required=True)
    switch.add_argument("--reason", required=True)
    switch.add_argument("--host", choices=vision.HOSTS)
    switch.add_argument("--capabilities")
    switch.add_argument("--vision-config")
    switch.add_argument("--vision-method", choices=vision.METHODS)
    switch.add_argument("--vision-model")
    switch.add_argument("--accept-unverified-model", action="store_true")
    switch.set_defaults(handler=command_vision)
    extend = subparsers.add_parser("vision-extend")
    extend.add_argument("--task-dir", required=True)
    extend.add_argument("--issue-id", required=True)
    extend.add_argument("--rounds", required=True, type=int)
    extend.add_argument("--reason", required=True)
    extend.set_defaults(handler=command_vision)
    limit = subparsers.add_parser("vision-limit")
    limit.add_argument("--task-dir", required=True)
    budget = limit.add_mutually_exclusive_group(required=True)
    budget.add_argument("--limit", type=int)
    budget.add_argument("--unlimited", action="store_true")
    limit.add_argument("--reason", required=True)
    limit.set_defaults(handler=command_vision)
    for name in ("vision-run", "vision-begin", "vision-record", "vision-reconcile", "clarity-search", "clarity-apply", "screenshot-review"):
        op = subparsers.add_parser(name)
        op.add_argument("--task-dir", required=True)
        op.set_defaults(handler=command_vision)
        if name.startswith("vision-"):
            op.add_argument("--dispatch-id", required=True)
        if name == "vision-record":
            op.add_argument("--input", required=True)
            op.add_argument("--actual-model")
            op.add_argument("--agent-id")
            accounting = op.add_mutually_exclusive_group()
            accounting.add_argument("--model-calls", type=int)
            accounting.add_argument("--model-calls-unknown", action="store_true")
        if name in {"clarity-search", "clarity-apply"}:
            op.add_argument("--batch-id", required=True)
            op.add_argument("--issue-id", required=True)
        if name == "clarity-search":
            op.add_argument("--frame-id", required=True)
            op.add_argument("--original-checked", action="store_true")
            op.add_argument("--reason", required=True)
            op.add_argument("--radius-ms", type=int)
            op.add_argument("--interval-ms", type=int, default=125)
            op.add_argument("--crop")
        if name == "screenshot-review":
            op.add_argument("--accept", action="store_true")
    ui = subparsers.add_parser("config-ui")
    ui.add_argument("action", choices=("start", "status", "stop"))
    ui.add_argument("--project", default=str(Path.cwd()))
    ui.add_argument("--host", choices=vision.HOSTS, default="codex")
    ui.add_argument("--capabilities")
    ui.add_argument("--use-local-key", action="store_true")
    ui.add_argument("--runtime-dir")
    ui.set_defaults(handler=command_config_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "task_dir", None):
            with state.task_lock(Path(args.task_dir).expanduser().resolve()):
                args.task_dir = str(delivery.recover_rename(Path(args.task_dir)))
                return int(args.handler(args))
        return int(args.handler(args))
    except (
        OSError,
        WorkflowError,
        config.ConfigError,
        paths.PathError,
        state.StateError,
        media.MediaError,
        transcript.TranscriptError,
        sampling.SamplingError,
        observations.ObservationError,
        review.ReviewError,
        delivery.DeliveryError,
        vision.VisionError,
    ) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2 if isinstance(exc, (config.ConfigError, paths.PathError)) else 1
