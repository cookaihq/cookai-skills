---
name: video-to-markdown
metadata:
  version: "2.0.0"
description: v2.0.0｜Turn one local or directly downloadable video into source-faithful Markdown with timestamped speech, screenshots, short GIFs and resumable coverage review. Transcription uses AIhub Audio or Feishu Minutes; supplied subtitles bypass both. Use for 视频转文档、带截图的视频笔记 or video to Markdown. Not for pure ASR, one-off video Q&A, downloading/editing video or automatic publishing.
compatibility: Claude Code, Codex and WorkBuddy are required targets; new host workflows need separate acceptance. Requires uv >= 0.8, ffmpeg, ffprobe and host image inspection. Cloud transcription uses installed AIhub Audio (aihub-studio >=1.3.0,<2) or user-authorized Lark CLI >=1.0.98,<2. Native Windows uses PowerShell. See README for tested scope.
---

# video-to-markdown

Convert one video into a source-faithful GFM document. The workflow owns files, timestamps, ffmpeg operations, transcription coordination, recovery and validation. You inspect frames, record supported observations, request denser evidence where needed, and write the document and review report. A pending action is not a completed transcription or document.

Read [references/workflow.md](references/workflow.md) before the first run and [references/schemas.md](references/schemas.md) for observation/review JSON. When transcription is needed, follow [references/transcription.md](references/transcription.md), including dependency loading, identity confirmation, failure choices and interrupted-write handling.

## Step 0: Check For Updates

For a Git installation, run `<skill-dir>/scripts/check_update.ps1` in native Windows PowerShell, or `scripts/check_update.sh` in Bash on macOS/Linux. Do not invoke Windows' WSL `bash.exe` as a native adapter.

- Exit `0`: continue without relaying its output.
- Exit `10`: relay the report verbatim and ask whether to update now. After approval use `-Pull` in PowerShell or `--pull` in Bash; continue with the available version if it fails.
- If the user declines or does not answer, continue without asking again in this run.
- To disable this check, write `AUTO_UPDATE_CHECK=0` to `~/.config/video-to-markdown/.env` without changing other lines.

The update check never blocks video processing.

For a copy installation, read `update.json`, find the newest stable tag under its prefix and resolve its commit SHA. Show the local/remote versions, repository, exact tag/SHA and destination before asking to update. Only after confirmation download that SHA to a temporary directory on the same filesystem, validate the Skill, atomically replace the installation and read it back. Do not infer WorkBuddy update APIs; unavailable discovery/apply capabilities report `not-applicable`/`apply-failed` and continue the task.

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
- `--provider aihub-audio|feishu-minutes` selects this task; `--only-provider` forbids switching to the other platform.
- `--no-global-config` disables global business configuration for this task and the AIhub dependency. It is preserved on resume.

Configuration is resolved from the process environment, `$PWD/.env.video-to-markdown`, `$PWD/.env.local`, `$PWD/.env`, then the explicitly enabled home file. Never search parents. `VIDEO_TO_MARKDOWN_ASR_PROVIDER` defaults to `aihub-audio`. The Feishu profile/app/user group comes from a single layer. AIhub credentials/models belong to AIhub Audio. An old `VIDEO_TO_MARKDOWN_ASR_MODEL` is a migration error; explain its migration instead of silently changing the model.

To continue an existing task:

```bash
uv run --project <skill-dir> <skill-dir>/scripts/workflow.py resume --task-dir <task-directory>
```

Resume checks source/audio identity and keeps the saved provider. A task lock prevents concurrent resume. Preserve receipts and known IDs; stop on `submission_unknown`. Do not delete state or start again to escape an uncertain write. Version 1 task schemas are preserved but rejected by v2; no old task is automatically resubmitted.

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

Do not reconstruct SVG animation.

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
- Direct file URLs only; no platform page downloader.
- Use supplied SRT, VTT, or timestamped transcript instead of ASR.
- Never upload the full video for transcription; upload only the prepared MP3. It preserves the first audio track with a recorded video offset, without truncation or automatic splitting.
- Do not download, install, start, or call a local model. ffmpeg and host image understanding are allowed.
- Static evidence is PNG/JPEG; motion evidence is GIF; no SVG reconstruction.
- Use actual decoded-frame PTS for frame/GIF timestamps and preserve upstream ASR timestamp precision.
- AIhub Audio owns model selection and its confirmation policy. Known AIhub failure may switch this task to Feishu once; processing, confirmation, query/download failure or unknown writes cannot trigger that switch.
- Keep `.state/` private. Never paste raw receipts, signed URLs, login tokens or dependency records into public documents. Provide local recovery paths and explain actual status.
- This Skill has no per-request paid authorization gate. A failed request may still require user involvement when its outcome is ambiguous.
