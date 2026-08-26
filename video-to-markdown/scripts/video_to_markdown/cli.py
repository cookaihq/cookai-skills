from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import aihub, config, media, observations, paths, review, sampling, state, transcript


class WorkflowError(ValueError):
    pass


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _runtime_config(args) -> tuple[Path, config.ResolvedConfig]:
    cwd = Path.cwd().resolve()
    resolved = config.resolve(cwd=cwd, use_home=bool(getattr(args, "use_local_key", False)))
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
) -> None:
    if subtitle:
        subtitle_path = Path(subtitle).expanduser().resolve()
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
        return
    audio_path = task_dir / "source" / "audio-for-asr.flac"
    if not audio_path.exists():
        audio_record = media.extract_audio(source_path, audio_path, resolved.ffmpeg_bin)
        manifest["audio_for_asr"] = {
            "path": "source/audio-for-asr.flac",
            "sha256": audio_record["sha256"],
            "size_bytes": audio_record["size_bytes"],
        }
        state.save_manifest(task_dir, manifest)
    aihub.run_asr(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        audio_path=audio_path,
        api_key=resolved.api_key,
        language=language,
    )


def _finish_preparation(
    task_dir: Path,
    manifest: dict,
    resolved: config.ResolvedConfig,
    source_path: Path,
) -> None:
    manifest["stage"] = "transcript_ready"
    state.save_manifest(task_dir, manifest)
    state.create_empty_artifacts(task_dir)
    sampling.prepare_first_pass(
        task_dir=task_dir,
        manifest=manifest,
        source=source_path,
        ffmpeg_bin=resolved.ffmpeg_bin,
        ffprobe_bin=resolved.ffprobe_bin,
    )
    manifest["stage"] = "observation_in_progress"
    state.save_manifest(task_dir, manifest)


def command_start(args) -> int:
    cwd, resolved = _runtime_config(args)
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
    task_dir = paths.create_task_directory(output_parent, name)
    manifest, private = state.initialize_task(
        task_dir,
        settings_snapshot={
            **resolved.public_snapshot(),
            "invocation_cwd": str(cwd),
            "project_root": str(project_root),
            "output_parent": str(output_parent),
            "source_policy": source_policy.mode,
            "runtime_versions": versions,
            "language": args.language,
        },
    )
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
    _prepare_transcript(
        task_dir=task_dir,
        manifest=manifest,
        private=private,
        resolved=resolved,
        source_path=source_path,
        subtitle=args.subtitle,
        language=args.language,
    )
    _finish_preparation(task_dir, manifest, resolved, source_path)
    pending = [item["batch_id"] for item in manifest["batches"] if item["status"] == "ready_for_observation"]
    _print(
        {
            "task_dir": str(task_dir),
            "document": str(task_dir / "document.md"),
            "stage": manifest["stage"],
            "next_action": "inspect_batch",
            "next_batch_id": pending[0] if pending else None,
            "legacy_key_notice": "X_API_KEY is deprecated; use AIHUB_API_KEY."
            if resolved.api_key_name == config.LEGACY_KEY
            else None,
        }
    )
    return 0


def _resolved_from_manifest(manifest: dict, *, use_home: bool) -> config.ResolvedConfig:
    invocation = manifest.get("settings", {}).get("invocation_cwd")
    if not isinstance(invocation, str):
        raise WorkflowError("manifest does not contain the invocation directory")
    return config.resolve(cwd=invocation, use_home=use_home)


def command_resume(args) -> int:
    task_dir, manifest, private = state.load_task(args.task_dir)
    resolved = _resolved_from_manifest(manifest, use_home=args.use_local_key)
    media.preflight(resolved.ffmpeg_bin, resolved.ffprobe_bin)
    source_path = media.verify_source(manifest.get("source") or {})
    if manifest.get("stage") == "complete" and manifest.get("validation", {}).get("complete"):
        _print(
            {
                "task_dir": str(task_dir),
                "stage": "complete",
                "next_action": "ask_rename_questions",
                "document": str(task_dir / manifest["artifacts"]["document"]),
                "asr_task_id": manifest.get("asr", {}).get("task_id"),
            }
        )
        return 0
    if manifest.get("asr", {}).get("state") in {"ambiguous_upload", "ambiguous_asr_create"}:
        raise aihub.AmbiguousWrite(
            "task is in %s; resume will not duplicate the remote write"
            % manifest["asr"]["state"]
        )
    if not (task_dir / "transcript.json").exists():
        _prepare_transcript(
            task_dir=task_dir,
            manifest=manifest,
            private=private,
            resolved=resolved,
            source_path=source_path,
            subtitle=None,
            language=manifest.get("settings", {}).get("language"),
        )
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
            "next_action": "inspect_batch" if pending else "adaptive_review",
            "next_batch_id": pending[0] if pending else None,
            "asr_task_id": manifest.get("asr", {}).get("task_id"),
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
            "next_action": "inspect contact sheets, then record-observations",
        }
    )
    return 0


def command_record_observations(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    input_document = state.read_json(Path(args.input).expanduser().resolve())
    result = observations.record(
        task_dir=task_dir,
        manifest=manifest,
        batch_id=args.batch_id,
        input_document=input_document,
    )
    _print(result)
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
    )
    _print(result)
    return 0


def command_adaptive_review(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    if not args.reason.strip():
        raise WorkflowError("adaptive review reason is required")
    unresolved = [
        item["batch_id"]
        for item in manifest.get("batches") or []
        if item.get("status") != "observed"
    ]
    if unresolved:
        raise WorkflowError(
            "all batches must be observed before closing adaptive review: %s"
            % ", ".join(unresolved)
        )
    manifest["adaptive_review_performed"] = True
    manifest["adaptive_review"] = {
        "decision": "no_additional_sampling_needed",
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
    result = review.validate_task(task_dir, manifest)
    _print(result)
    return 0 if result["complete"] else 1


def command_rename(args) -> int:
    task_dir, manifest, _ = state.load_task(args.task_dir)
    if not manifest.get("validation", {}).get("complete"):
        raise WorkflowError("rename is allowed only after validation succeeds")
    if not args.document_name and not args.task_name:
        raise WorkflowError("provide --document-name and/or --task-name")
    document_name = None
    document_source = None
    document_destination = None
    task_destination = None
    if args.document_name:
        document_name = paths.safe_leaf(args.document_name, require_markdown=True)
        old_name = manifest["artifacts"]["document"]
        document_source = task_dir / old_name
        document_destination = task_dir / document_name
        if document_destination.exists() and document_destination != document_source:
            raise WorkflowError(
                "document rename target already exists: %s" % document_destination
            )
    if args.task_name:
        new_task_name = paths.safe_leaf(args.task_name)
        task_destination = task_dir.parent / new_task_name
        if task_destination.exists() and task_destination != task_dir:
            raise WorkflowError("task rename target already exists: %s" % task_destination)

    if document_name and document_source and document_destination:
        document_source.rename(document_destination)
        manifest["artifacts"]["document"] = document_name
        report_path = task_dir / "review-report.json"
        report_document = state.read_json(report_path)
        report_document["document"] = document_name
        state.atomic_write_json(report_path, report_document)
        state.save_manifest(task_dir, manifest)
    if args.task_name and task_destination and task_destination != task_dir:
        task_dir.rename(task_destination)
        task_dir = task_destination.resolve()
        manifest["task_dir_name"] = new_task_name
        state.save_manifest(task_dir, manifest)
    result = review.validate_task(task_dir, manifest)
    if not result["complete"]:
        raise WorkflowError("renamed task failed validation: %s" % "; ".join(result["blockers"]))
    _print(
        {
            "task_dir": str(task_dir),
            "document": str(task_dir / manifest["artifacts"]["document"]),
            "validation": result,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="video-to-markdown deterministic workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--video", required=True)
    start.add_argument("--subtitle")
    start.add_argument("--language")
    start.add_argument("--output-parent")
    start.add_argument("--project-root")
    start.add_argument("--use-local-key", action="store_true")
    start.set_defaults(handler=command_start)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--task-dir", required=True)
    resume.add_argument("--use-local-key", action="store_true")
    resume.set_defaults(handler=command_resume)

    batch = subparsers.add_parser("batch")
    batch.add_argument("--task-dir", required=True)
    batch.add_argument("--batch-id")
    batch.set_defaults(handler=command_batch)

    record = subparsers.add_parser("record-observations")
    record.add_argument("--task-dir", required=True)
    record.add_argument("--batch-id", required=True)
    record.add_argument("--input", required=True)
    record.set_defaults(handler=command_record_observations)

    resample = subparsers.add_parser("resample")
    resample.add_argument("--task-dir", required=True)
    resample.add_argument("--batch-id", required=True)
    resample.add_argument("--start", required=True)
    resample.add_argument("--end", required=True)
    resample.add_argument("--reason", required=True)
    resample.add_argument("--interval-ms", type=int)
    resample.add_argument("--crop")
    resample.set_defaults(handler=command_resample)

    adaptive = subparsers.add_parser("adaptive-review")
    adaptive.add_argument("--task-dir", required=True)
    adaptive.add_argument("--reason", required=True)
    adaptive.set_defaults(handler=command_adaptive_review)

    gif = subparsers.add_parser("make-gif")
    gif.add_argument("--task-dir", required=True)
    gif.add_argument("--start", required=True)
    gif.add_argument("--end", required=True)
    gif.add_argument("--purpose", required=True)
    gif.set_defaults(handler=command_make_gif)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--task-dir", required=True)
    validate.set_defaults(handler=command_validate)

    rename = subparsers.add_parser("rename")
    rename.add_argument("--task-dir", required=True)
    rename.add_argument("--document-name")
    rename.add_argument("--task-name")
    rename.set_defaults(handler=command_rename)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        WorkflowError,
        config.ConfigError,
        paths.PathError,
        state.StateError,
        media.MediaError,
        transcript.TranscriptError,
        sampling.SamplingError,
        observations.ObservationError,
        aihub.AIHubError,
        review.ReviewError,
    ) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2 if isinstance(exc, (config.ConfigError, paths.PathError)) else 1
