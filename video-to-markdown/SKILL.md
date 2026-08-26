---
name: video-to-markdown
version: 1.0.0
description: v1.0.0｜Turn one local or directly downloadable video into a source-faithful GitHub Flavored Markdown document with timestamped transcript evidence, keyframe screenshots, short GIFs for motion-dependent information, adaptive visual resampling, and a resumable coverage review. Use for 视频转文档、带截图的视频笔记、video to Markdown, or preserving spoken and visual content together. Do not use for pure ASR, one-off video Q&A, downloading/editing/compressing video, video generation, or automatic publishing.
compatibility: Requires Python managed by uv >= 0.8, ffmpeg, ffprobe, and a host Agent that can inspect images. AIHub ASR requires network access and AIHUB_API_KEY when no subtitle is supplied. Supports Codex and Claude Code.
---

# video-to-markdown

Convert one video into a source-faithful GFM document. The deterministic workflow owns files, timestamps, ffmpeg operations, AIHub ASR, recovery, and final validation. You own visual understanding and writing: inspect each persisted batch, record only what its frames support, request denser evidence where needed, then write the document and review report.

Read [references/workflow.md](references/workflow.md) before the first run. Read [references/schemas.md](references/schemas.md) when constructing observation or review JSON. Read [references/aihub-asr.md](references/aihub-asr.md) only when ASR is needed or an AIHub request fails.

## Step 0: Check For Updates

Run `<skill-dir>/scripts/check_update.sh` on every invocation.

- Exit `0`: continue without relaying its output.
- Exit `10`: relay the report verbatim and ask whether to pull now. If approved, run the same script with `--pull`; whether pulling succeeds or fails, continue this task with the available version.
- If the user declines or does not answer, continue without asking again in this run.
- To disable this check, write `AUTO_UPDATE_CHECK=0` to `~/.config/video-to-markdown/.env` without changing other lines.

The update check never blocks video processing.

## Start Or Resume

Always use the pinned runtime:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py start \
  --video <local-path-or-direct-http-url> [--subtitle <srt-vtt-or-timestamped-text>]
```

Useful options:

- `--output-parent <directory>` uses that complete output parent instead of the default `<output-base>/video-to-markdown-output/`.
- `--project-root <directory>` overrides only default output placement. It does not change where configuration is read.
- `--use-local-key` authorizes this run to read `~/.config/video-to-markdown/.env`.
- `--language <code>` records the requested source language hint. Translation is not implied.

Configuration is resolved per variable from the process environment, then `$PWD/.env.local`, then `$PWD/.env`, then the authorized home file. Never search parent directories. `VIDEO_TO_MARKDOWN_ASR_MODEL` defaults to `paraformer-v2`; unknown models fail before any paid request.

To continue an existing task:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py resume --task-dir <task-directory>
```

Resume must validate the source SHA-256. If an upload or ASR task ID already exists, continue that operation; never create a duplicate. Stop on `ambiguous_upload` or `ambiguous_asr_create` and explain that the request may have been accepted but no usable response was received.

## Inspect Every Batch

The first pass combines ffmpeg scene changes with periodic fallback frames. Each batch covers at most five minutes and is persisted before the next begins.

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py batch \
  --task-dir <task-directory> [--batch-id batch-0001]
```

Open every returned contact sheet and any full-resolution frame needed to verify small text, charts, code, or UI state. Do not infer events between sampled frames.

Write an observation input that follows [references/schemas.md](references/schemas.md), then record it through the workflow boundary:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py record-observations \
  --task-dir <task-directory> --batch-id batch-0001 --input <observations.json>
```

## Perform Adaptive Resampling

Every run requires a second-pass decision. Request more evidence for fast changes, unreadable details, transcript/visual mismatches, contradictory observations, or missing action boundaries:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py resample \
  --task-dir <task-directory> --batch-id batch-0001 \
  --start 00:01:12.000 --end 00:01:20.000 \
  --reason "The chart values change between the sampled frames"
```

Use `--interval-ms` to control density and `--crop x:y:w:h` only when the full frame cannot make the evidence legible. Inspect the new contact sheet and record a new observation revision. Do not enter document writing while any observation remains `uncertain` or `needs_resample: true`.

If all first-pass evidence is sufficient, persist that second-pass decision instead of silently skipping it:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py adaptive-review \
  --task-dir <task-directory> \
  --reason "All transcript-linked visual changes are legible in the first-pass evidence"
```

For information that depends on motion rather than a static state, generate a short GIF:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py make-gif \
  --task-dir <task-directory> --start 00:02:10.000 --end 00:02:16.000 \
  --purpose "Shows the animated transition between the two states"
```

Do not reconstruct SVG animation in v1.

## Write And Validate

Write `<task-dir>/document.md` in the video's source language unless the user explicitly requested translation. Preserve video order and content; remove fillers and meaningless repetition, but do not replace the source with a summary. Put a timestamped screenshot beside static visual information and a GIF beside motion-dependent information. Use only relative media paths.

Create `<task-dir>/review-report.json` using [references/schemas.md](references/schemas.md). Its intervals must classify the complete timeline as body, screenshot, GIF, or deliberate skip with a reason. Every transcript segment and visual observation must be consumed or explicitly skipped.

Run:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py validate --task-dir <task-directory>
```

Do not call the task complete if validation reports a gap, missing media, out-of-range timestamp, unexplained evidence, source mismatch, or unresolved uncertainty.

After validation succeeds, ask the user first whether to rename `document.md`, then whether to rename the task directory. Apply approved names only through:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py rename \
  --task-dir <task-directory> [--document-name <name.md>] [--task-name <directory-name>]
```

Relay the final document path and task directory. If a rename occurs, relay the new paths returned by the command.

## Hard Boundaries

- One video per task.
- Direct file URLs only; no platform page downloader in v1.
- Use supplied SRT, VTT, or timestamped transcript instead of ASR.
- Never upload the full video for ASR; upload only `source/audio-for-asr.flac`.
- Do not download, install, start, or call a local model. ffmpeg and host image understanding are allowed.
- Static evidence is PNG/JPEG; motion evidence is GIF; no SVG reconstruction.
- Use actual decoded-frame PTS for frame/GIF timestamps and preserve upstream ASR timestamp precision.
- No automatic model fallback and no multi-task splitting beyond an ASR model's documented single-file duration.
- This Skill has no per-request paid authorization gate. A failed request may still require user involvement when its outcome is ambiguous.
