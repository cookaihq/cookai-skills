# Workflow Reference

## Output Resolution

The invocation directory, project root, and Skill directory are separate facts:

1. Capture `$PWD` when the process starts. Read only its `.env.local` and `.env`.
2. Resolve the project root from `--project-root`, otherwise `git rev-parse --show-toplevel`, otherwise `$PWD`.
3. Resolve the output base from `VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR`, otherwise the project root.
4. Unless `--output-parent` was supplied, append `video-to-markdown-output/`.
5. Create `YYYYMMDDHHMMSSffffff-<video-stem>/` without overwriting an existing path.

`VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR` is independent:

- unset: keep a local input in place; download a URL input to `<task-dir>/source/`;
- `project`: copy/download to `<task-dir>/source/`;
- absolute path: copy/download to `<value>/<task-dir-name>/`;
- any other relative value: reject before copying or downloading.

## Agent Loop

1. Run `start` or `resume` and inspect its JSON result.
2. Run `batch` for the next batch whose status is `ready_for_observation`.
3. Inspect every contact sheet. Open original frames for text and small details.
4. Record observations. A batch is not complete merely because its contact sheet was opened.
5. After all first-pass batches, run at least one second-pass decision over the observation set. Use `resample` where evidence is incomplete; otherwise persist the no-resampling decision with `adaptive-review --reason ...`.
6. Generate GIFs only where motion is information.
7. Write `document.md`, then `review-report.json`.
8. Run `validate`. Resolve every reported blocker and rerun.
9. Ask the two rename questions only after validation succeeds.

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
- Use paths relative to `document.md`, such as `assets/frames/...`.
- Do not claim millisecond ASR accuracy when the source transcript only supplies seconds.
- Use `start_ms` and `end_ms` to place transcript content on the video timeline. Treat `raw_start` and `raw_end` as audit evidence; when `timing_adjustments` exists, do not restore the out-of-range raw value into review intervals.
- Surface uncertainty in the prose instead of choosing an unsupported interpretation.
