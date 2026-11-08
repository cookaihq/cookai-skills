# Workflow Reference

## Output Resolution

The invocation directory, project root, and Skill directory are separate facts:

1. Capture `$PWD` when the process starts. Read only its `.env.video-to-markdown`, `.env.local` and `.env`.
2. Resolve the project root from `--project-root`, otherwise `git rev-parse --show-toplevel`, otherwise `$PWD`.
3. Resolve the output base from `VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR`, otherwise the project root.
4. Unless `--output-parent` was supplied, append `data/video-to-markdown/`.
5. Create `YYYYMMDDHHMMSS-<complete-original-video-filename>/` without overwriting an existing path. The timestamp is local time to seconds. A same-second collision fails explicitly; use another output parent or start after the next second, never overwrite a task.
6. Set the initial document to `<complete-original-video-filename>.md`, preserving the video extension. Resolve `--copy-dir` relative to the invocation directory. Without it, use the original local video's parent; URL inputs have no extra copy. This destination is independent of source storage policy and `--output-parent`.

`VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR` is independent:

- unset: keep a local input in place; download a URL input to `<task-dir>/source/`;
- `project`: copy/download to `<task-dir>/source/`;
- absolute path: copy/download to `<value>/<task-dir-name>/`;
- any other relative value: reject before copying or downloading.

## Agent Loop

1. Run `start` or `resume` and inspect its JSON result.
   If it returns a transcription action, follow [transcription.md](transcription.md) before continuing. Exit code zero alone does not mean transcription or the document is complete. Always relay default/identity save results and dependency result-check notices, including after a successful resume.
2. Run `batch` for the next batch whose status is `ready_for_observation`.
3. Inspect every contact sheet. Open original frames for text and small details.
4. Record observations. A batch is not complete merely because its contact sheet was opened.
5. After all first-pass batches, run at least one second-pass decision over the observation set. Use `resample` where evidence is incomplete; otherwise persist the no-resampling decision with `adaptive-review --reason ...`.
6. Generate GIFs only where motion is information.
7. Write the returned `document` path, then `review-report.json` with that document's filename.
8. Run `validate`. Resolve every reported blocker and rerun.
9. Follow the naming decision in SKILL.md, then run `finalize --title <title>` or `finalize --keep-name`. It applies the final names and produces the requested copy. Return the paths from the command, including the optional copy.

## Finalization and Recovery

Validation certifies the document and evidence; `stage: validated` still requires finalization. A naming decision is saved before renaming. The directory and document change together, including managed-source, frame, contact-sheet and dependency paths. Recovery replays an unfinished rename under the task lock, whose identity survives directory renaming. V3 does not resume older task schemas.

The copy is `<copy-directory>/<final-document-filename>`. Its PNG/JPEG/GIF references point into the adjacent `<final-document-name-without-.md>.assets/`; only referenced media is copied. Relative URLs are encoded for spaces, Unicode and punctuation. Keep the Markdown and that directory together when moving or sharing them. Private state, audio, source video and unused frames are excluded.

Every copy target is checked before writing. Existing identical bytes can be reused; different bytes fail without overwrite. Each file is published atomically. If delivery is interrupted, run `finalize --task-dir <current-directory>` again; missing files are completed and the saved title is reused. A conflict can be resolved using `--copy-dir <another-directory>`. A new naming decision is not accepted after naming has begun.

If the process stops during renaming, use the printed final directory if it exists, otherwise the original directory. `resume`, `validate` and `finalize` repair saved pending path changes before using the task. No transcription is repeated for a task that already reached validation. `resume` rechecks the document and copy; a missing or changed copy returns `next_action: finalize`, not a successful delivery claim.

## Observation Rules

- Describe visible facts, not likely intent.
- Bind each fact to one or more `frame_id` values.
- Keep OCR text as observed; mark illegible parts uncertain.
- A frame before and after an action does not prove the intermediate motion.
- When transcript and image conflict, record the conflict and resample before writing.
- A static screenshot may represent a stable state. Use a GIF when direction, order, transformation, or interaction is the content.

## Document Rules

- Preserve chronological order.
- Keep source detail. Compression is allowed only for fillers, false starts, and exact repetition.
- Give images useful alt text and include `HH:MM:SS.mmm` in the alt text or nearby prose.
- Use inline image links with paths relative to the document, such as `assets/frames/...`.
- Do not claim millisecond ASR accuracy when the source transcript only supplies seconds.
- Use `start_ms` and `end_ms` to place transcript content on the video timeline. Treat `raw_start` and `raw_end` as audit evidence; when `timing_adjustments` exists, do not restore the out-of-range raw value into review intervals.
- Surface uncertainty in the prose instead of choosing an unsupported interpretation.
