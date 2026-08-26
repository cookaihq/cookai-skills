# Agent-Written JSON Contracts

The runtime performs strict structural and referential validation. Times are integer milliseconds from the beginning of the source video.

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

## Observation Input

Submit one object per `record-observations` call:

```json
{
  "schema_version": 1,
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

The runtime adds `batch_id`, `revision`, `recorded_at`, and disposition fields.

## Review Report

Write `<task-dir>/review-report.json`:

```json
{
  "schema_version": 1,
  "document": "document.md",
  "intervals": [
    {
      "start_ms": 0,
      "end_ms": 4200,
      "classification": "represented_by_screenshot",
      "document_anchor": "#opening-title",
      "media": ["assets/frames/frame-000000000000.jpg"],
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

Intervals are half-open `[start_ms, end_ms)`, sorted, adjacent, non-overlapping, and together cover `[0, duration_ms)`. A represented interval must cite a document anchor and at least one observation or transcript segment. Screenshot/GIF classifications must also cite existing media of the matching type. A deliberately skipped interval requires a concrete reason.

Every final visual observation and every transcript segment must be referenced by at least one interval. Unresolved observations (`uncertain` or `needs_resample`) always block completion.

## Timestamped Plain Text

When a supplied transcript is neither SRT nor VTT, each non-empty line must use one of:

```text
[HH:MM:SS.mmm --> HH:MM:SS.mmm] text
HH:MM:SS.mmm --> HH:MM:SS.mmm text
```

Seconds may omit `.mmm`; the parser records the source precision and does not invent finer accuracy.
