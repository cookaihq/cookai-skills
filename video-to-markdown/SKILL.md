---
name: video-to-markdown
metadata:
  version: "4.0.0"
description: v4.0.0｜Turn one local or directly downloadable video into source-faithful Markdown with timestamped speech, screenshots, short GIFs and resumable coverage review. Transcription uses AIhub Audio or Feishu Minutes; supplied subtitles bypass both. Use for 视频转文档、带截图的视频笔记 or video to Markdown. Not for pure ASR, one-off video Q&A, downloading/editing video or automatic publishing.
compatibility: Claude Code, Codex and WorkBuddy are required targets; native model routing depends on actual session capabilities. Requires uv >= 0.8, ffmpeg, ffprobe and a usable vision route unless visuals are explicitly skipped. Cloud transcription uses installed AIhub Audio or user-authorized Lark CLI >=1.0.98,<2. Native Windows uses PowerShell. See README for tested scope.
---

# video-to-markdown

Convert one video into a source-faithful GFM document. The main Agent orchestrates and writes; the selected vision route performs all image judgments, including supplements, second-pass review and final screenshots. ASR remains independent. A pending action is not a completed transcription or document.

Read [workflow.md](references/workflow.md) before the first run and [schemas.md](references/schemas.md) before writing results. For enabled visuals, read [vision-routing.md](references/vision-routing.md) before selecting or executing a route. For configuration/UI requests, read [configuration.md](references/configuration.md). When transcription is needed, follow [transcription.md](references/transcription.md), including dependency loading, identity confirmation and interrupted-write handling.

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
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py start \
  --video <local-path-or-direct-http-url> [--subtitle <srt-vtt-or-timestamped-text>] \
  --host <codex|claude-code|workbuddy> [--capabilities <current-session-evidence.json>]
```

Useful options:

- `--output-parent <directory>` uses that complete output parent instead of the default `<output-base>/data/video-to-markdown/`.
- `--copy-dir <directory>` chooses where to place an additional portable Markdown copy and its referenced images/GIFs after finalization. For local inputs it defaults to the original video's directory, even when the source is copied into the task. URL inputs have no default copy directory.
- `--project-root <directory>` overrides only default output placement. It does not change where configuration is read.
- `--use-local-key` authorizes this run to read `~/.config/video-to-markdown/.env`.
- `--language <code>` records the requested source language hint. Translation is not implied.
- `--provider aihub-audio|feishu-minutes` selects this task; `--only-provider` forbids switching to the other platform.
- `--no-global-config` disables global business configuration for this task and the AIhub dependency. It is preserved on resume.
- `--skip-visuals <user-reason>` implements an explicit statement that the picture is irrelevant. Skip model choice, frame extraction, visual Agents, screenshots/GIFs and visual review; use valid subtitles or speech. Do not sample to second-guess that instruction. Without either source, report missing content.
- `--vision-method` / `--vision-model` or `--vision-config <private-route.json>` selects only this task. Without a fixed choice, show all four methods and obtainable models together in one question; then collect missing connection details. No mode-first question. Silence is not a model choice.

Configuration is resolved from the process environment, `$PWD/.env.video-to-markdown`, `$PWD/.env.local`, `$PWD/.env`, then the explicitly enabled home file. Never search parents. `VIDEO_TO_MARKDOWN_ASR_PROVIDER` defaults to `aihub-audio`. Feishu identity and each vision connection resolve as atomic groups. AIhub ASR credentials/models belong to AIhub Audio. An old `VIDEO_TO_MARKDOWN_ASR_MODEL` is a migration error; explain its migration instead of silently changing the model. A verified no-audio video can use visuals alone without ASR.

To continue an existing task:

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py resume --task-dir <task-directory>
```

Resume checks source/audio identity and retains the saved ASR provider and vision route, regardless of changed defaults. A task lock prevents concurrent operations, including final renaming. Preserve receipts and known IDs; stop on `submission_unknown`. V4 supports task schema 4 only: preserve unsupported tasks and report the format without migration, recreation or resubmission. A requested route change uses `vision-switch` with the first affected batch and a reason.

## Inspect Every Batch

Each parent batch spans at most five minutes. First-pass candidates combine 5-second intervals (about 61 per full batch), boundaries, FFmpeg scene scores and local-region change signals. These are screening signals, not proof of no information changes. Capacity splits preserve every original; there is no 40-frame thinning. One batch Agent task handles multiple images and supplements; Agent tasks, images and model requests are different counts.

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py batch \
  --task-dir <task-directory> [--batch-id batch-0001]
```

Execute every returned dispatch through `vision-run`, following [the route-specific loop](references/vision-routing.md). When another model was selected, the main Agent uses returned evidence for writing and sends further image judgments back through that route. The selected viewer checks contact sheets for context and originals for details; it returns supported observations, unreadable regions and precise supplement requests. A contact sheet alone does not prove small text or motion.

## Perform Adaptive Resampling

Every run requires a second-pass decision. Request more evidence for fast changes, unreadable details, transcript/visual mismatches, contradictory observations, or missing action boundaries:

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py resample \
  --task-dir <task-directory> --batch-id batch-0001 \
  --start 00:01:12.000 --end 00:01:20.000 \
  --issue-id chart-transition \
  --reason "The chart values change between the sampled frames"
```

Use a stable `--issue-id` for the same question, including expansion and recovery. Each issue allows two automatic resampling/expansion rounds, separately from network retries. Use `--interval-ms` for density and `--crop x:y:w:h` for a target region. Dispatch supplements to the same batch context and route. For blurred originals, use the FFmpeg neighbor/semantic confirmation loop in [vision-routing.md](references/vision-routing.md); the same loop applies to the second pass and final screenshots.

After bounded effort, unresolved facts may remain only in a visibly marked, timed draft. Preserve their evidence and reasons; use `adaptive-review --draft`, `validate --draft` and `finalize --draft`. Complete delivery remains strict. An Agent path without actual request metering cannot enforce an exact model-request cap.

If all first-pass evidence is sufficient, persist that second-pass decision instead of silently skipping it:

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py adaptive-review \
  --task-dir <task-directory> \
  --reason "All transcript-linked visual changes are legible in the first-pass evidence"
```

For information that depends on motion rather than a static state, generate a short GIF:

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py make-gif \
  --task-dir <task-directory> --start 00:02:10.000 --end 00:02:16.000 \
  --purpose "Shows the animated transition between the two states"
```

Do not reconstruct SVG animation.

## Write And Validate

Write to the `document` path returned by `start`/`resume`, initially `<task-dir>/<complete-original-video-filename>.md`, in the video's source language unless the user explicitly requested translation. Preserve video order and content; remove fillers and meaningless repetition, but do not replace the source with a summary. Put a timestamped screenshot beside static visual information and a GIF beside motion-dependent information. Use inline image links to relative files under `assets/`.

Create `<task-dir>/review-report.json` using [schemas.md](references/schemas.md). Classify the complete timeline as body, screenshot, GIF, deliberate skip, or (draft only) an unresolved timed gap. Every transcript segment and visual observation must be accounted for. Before validation, run `screenshot-review`, execute its dispatches through the selected route, then `screenshot-review --accept`; repair unreadable references through the neighbor loop.

Run:

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py validate --task-dir <task-directory>
```

Do not call the task complete if validation reports a gap, missing media, out-of-range timestamp, unexplained evidence, source mismatch, or unresolved uncertainty.

After validation succeeds, decide whether the original filename describes the video. Hashes, UUIDs, random IDs and bare timestamps are opaque; a descriptive title remains meaningful even if it includes an ID. Use the actual transcript and inspected frames to choose a concise source-language title only for an opaque name. Leave the title absent while conversion is in progress. Apply this naming decision automatically through `finalize` (no rename questions):

```bash
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py finalize \
  --task-dir <task-directory> --title <content-derived-title>
```

For a meaningful filename, replace `--title` with `--keep-name`. Final names are `YYYYMMDDHHMMSS-<complete-original-filename>[-<title>]/<complete-original-filename>[-<title>].md`; the timestamp has exactly 14 digits. Preserve the original video extension. Omit both the separator and title when absent.

Finalization revalidates, repairs internal paths after renaming, and copies the Markdown plus referenced media to the saved copy directory. An existing different file is never overwritten. Retry `finalize --task-dir <current-directory>` without a naming option to reuse the saved decision; use `--copy-dir` to resolve a destination conflict. After a rename interruption, use the final directory printed before the move if it already exists. See [workflow.md](references/workflow.md) for recovery details.

Report full completion only for `stage: complete`. For `draft_delivered`, explicitly call the result a 待复核草稿, summarize timed gaps and give the saved task's recovery path. Relay document/task/copy paths and explain that `.assets` must accompany the shared copy.

## Hard Boundaries

- One video per task.
- Direct file URLs only; no platform page downloader.
- Use supplied SRT, VTT, or timestamped transcript instead of ASR.
- Never upload the full video for transcription; upload only the prepared MP3. It preserves the first audio track with a recorded video offset, without truncation or automatic splitting.
- Do not download, install, start, or call a local model. ffmpeg and host image understanding are allowed.
- Final static evidence is original-resolution PNG; contact sheets may be JPEG; motion evidence is GIF.
- Use actual decoded-frame PTS for frame/GIF timestamps and preserve upstream ASR timestamp precision.
- AIhub Audio owns model selection and its confirmation policy. Known AIhub failure may switch this task to Feishu once; processing, confirmation, query/download failure or unknown writes cannot trigger that switch.
- Keep `.state/` private. Never paste raw receipts, signed URLs, login tokens or dependency records into public documents. Provide local recovery paths and explain actual status.
- This Skill has no per-request paid authorization gate. A failed request may still require user involvement when its outcome is ambiguous.
