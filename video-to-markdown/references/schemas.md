# Agent-Written JSON Contracts

The runtime performs strict structural and referential validation. Times are integer milliseconds from the beginning of the source video.

The manifest and private task state use schema version 4. Transcript/observation/review artifacts retain schema version 1. Unsupported task state is preserved and rejected without migration or resubmission. Private state includes model inputs/responses, credential references, operation intents and provider receipts; publish only the document and referenced media.

## Transcript Timing

The workflow writes `transcript.json`; do not edit it by hand. Each segment contains:

```json
{
  "segment_id": "segment-000001",
  "raw_start": 0,
  "raw_end": 5700,
  "start_ms": 0,
  "end_ms": 5687,
  "timestamp_precision_ms": 1,
  "timing_adjustments": [
    {
      "field": "end_ms",
      "from_ms": 5700,
      "to_ms": 5687,
      "reason": "after_video_end"
    }
  ],
  "text": "Hello world."
}
```

`raw_start` and `raw_end` preserve the upstream values and units. `start_ms` and `end_ms` are the normalized values used for batch matching and review; timed segments must satisfy `0 <= start_ms <= end_ms <= duration_ms`. `timing_adjustments` is omitted when no boundary adjustment was needed. Its `reason` is `before_video_start` or `after_video_end`.

The example above is a supplied subtitle with a recorded boundary adjustment. New provider transcripts instead reject missing, reversed or out-of-video intervals. `time_mapping.offset_ms` maps the extracted audio's start to the video timeline; raw times remain unchanged. `source.timing_granularity` is `segment`, `word`, `missing` or `mixed`; source files are referenced under `.state/` instead of embedding raw provider metadata or temporary URLs in the public artifact. Real word times may appear under each segment's `words`. Do not split a word or segment into invented per-character intervals.

## Observation Input

Submit a selected-viewer result through `vision-record` (host/main) or the executable route adapter:

```json
{
  "schema_version": 1,
  "checks": [],
  "observations": [
    {
      "observation_id": "batch-0001-title-card",
      "start_ms": 0,
      "end_ms": 4200,
      "description": "A title card displays the session name.",
      "evidence_frame_ids": ["frame-000000000000"],
      "motion_dependent": false,
      "confidence": "certain",
      "needs_resample": false,
      "resample_reason": null
    }
  ]
}
```

Rules:

- `observation_id` is stable across revisions and unique within the task.
- `start_ms <= end_ms`, both inside the batch and source duration.
- Every `evidence_frame_ids` item must exist in that batch or one of its resampling rounds.
- `confidence` is `certain` or `uncertain`.
- `needs_resample: true` requires a non-empty `resample_reason`.
- `uncertain` cannot be silently changed to `certain`; record a new revision with new evidence.

The runtime adds `batch_id`, `revision`, `recorded_at`, and receipt fields. Supplement or clarity frames may support a new revision; use the same fact's existing ID. Route switches invalidate affected revisions rather than deleting history.

For clarity and final screenshot dispatches use checks only:

```json
{
  "schema_version": 1,
  "observations": [],
  "checks": [
    {"frame_id": "frame-000000000000", "readable": true, "same_content": true,
     "reason": "The title and labels are readable and the operation state matches the reference."}
  ]
}
```

Every supplied original, including a reference, needs exactly one check. `same_content` refers to the provided reference for clarity work and the cited fact/page for final screenshots. A negative result must state the actual issue. Observation dispatches contain no checks; check dispatches contain no observations.

## Review Report

Write `<task-dir>/review-report.json`:

```json
{
  "schema_version": 1,
  "document": "example.mp4.md",
  "intervals": [
    {
      "start_ms": 0,
      "end_ms": 4200,
      "classification": "represented_by_screenshot",
      "document_anchor": "#opening-title",
      "media": ["assets/frames/frame-000000000000.png"],
      "observation_ids": ["batch-0001-title-card"],
      "transcript_segment_ids": [],
      "reason": null
    },
    {
      "start_ms": 4200,
      "end_ms": 5100,
      "classification": "deliberately_skipped",
      "document_anchor": null,
      "media": [],
      "observation_ids": [],
      "transcript_segment_ids": [],
      "reason": "Silent transition with no new spoken or visual information."
    }
  ]
}
```

Allowed classifications:

- `represented_in_body`
- `represented_by_screenshot`
- `represented_by_gif`
- `deliberately_skipped`
- `unresolved` (draft only)

Intervals are half-open `[start_ms, end_ms)`, sorted, adjacent, non-overlapping, and together cover `[0, duration_ms)`. A represented interval must cite a document anchor and at least one observation or transcript segment. Screenshot/GIF classifications must also cite existing media of the matching type. A deliberately skipped interval requires a concrete reason.

Every final visual observation and transcript segment must be referenced. Unresolved observations (`uncertain` or `needs_resample`) block full completion. In a draft, give each an `unresolved` interval containing its full time range, exact start time and reason in the Markdown, and `issue_ids` referencing exhausted two-round issues covering that gap (or a genuinely exhausted saved model-call cap). Mark the document visibly `待复核草稿` or `DRAFT`. Missing observation dispatches require an unresolved interval covering the unobserved batch; a localized unreadable detail does not invalidate the already observed remainder. `deliberately_skipped` cannot conceal unreadable content.

When the user explicitly skips visuals, `visual-observations.json` has `status: skipped`, the saved reason and no revisions; no visual artifacts/review records are fabricated. Coverage then cites actual subtitles/speech. A verified no-audio visual task has `transcript.source.kind: no_audio` and `segments: []`.

## Timestamped Plain Text

When a supplied transcript is neither SRT nor VTT, each non-empty line must use one of:

```text
[HH:MM:SS.mmm --> HH:MM:SS.mmm] text
HH:MM:SS.mmm --> HH:MM:SS.mmm text
```

Seconds may omit `.mmm`; the parser records the source precision and does not invent finer accuracy.
