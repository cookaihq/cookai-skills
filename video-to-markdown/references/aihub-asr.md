# AIHub ASR Facts And Local Policy

Verified against the official AIHub documentation on 2026-08-26.

## Endpoints

- Upload a local audio file: `POST https://api.aihubmax.com/v1/files/upload/stream`
- Create an ASR task: `POST https://api.aihubmax.com/v1/audios/generations`
- Query a task: `GET https://api.aihubmax.com/v1/tasks/{task_id}?sync_upstream=true`
- Authentication: `Authorization: Bearer <token>`

Official references:

- [File stream upload](https://docs.aihubmax.com/pages/zh/api-manual/file-management/upload-stream.md)
- [Paraformer V2](https://docs.aihubmax.com/pages/zh/api-manual/audio-series/paraformer/paraformer-v2)
- [Scribe V2](https://docs.aihubmax.com/pages/en/api-manual/audio-series/elevenlabs/scribe-v2)
- [Cohere Transcribe](https://docs.aihubmax.com/pages/zh/api-manual/audio-series/cohere-transcribe/cohere-transcribe.md)
- [Task detail](https://docs.aihubmax.com/pages/zh/api-manual/task-management/get-task-detail.md)

## Request Shape

Paraformer:

```json
{"model": "paraformer-v2", "file_urls": ["https://example/audio.flac"]}
```

The same shape applies to `paraformer-8k-v2`. `paraformer-v2` supports optional `language_hints`; do not send optional recognition, diarization, or channel fields unless the user explicitly supplied a corresponding value. Official limits are 1-100 URLs, 2GB per file, and 12 hours per file. This Skill sends exactly one URL and rejects an over-limit source rather than creating multiple paid tasks.

`paraformer-v2` language hints are limited to `zh`, `en`, `ja`, `yue`, `ko`, `de`, `fr`, and `ru`. `paraformer-8k-v2` is Chinese-only and does not accept `language_hints`.

Scribe:

```json
{"model": "scribe-v2", "audio_url": "https://example/audio.flac"}
```

When supplied, `language_code` must be an ISO-639-1 or ISO-639-3 code.

Cohere:

```json
{"model": "cohere-transcribe", "audio_url": "https://example/audio.flac"}
```

When supplied, `language` must be one of the 14 documented ISO-639-1 codes: `en`, `fr`, `de`, `it`, `es`, `pt`, `el`, `nl`, `pl`, `zh`, `ja`, `ko`, `vi`, or `ar`.

The Skill uses the minimal request and leaves optional model behavior at upstream defaults.

## Responses And Retention

Successful upload returns an object with `id`, `filename`, `url`, and `size`; uploaded files expire after 72 hours. Store `url` only in `.state/private.json`. The public manifest stores the file ID, time, and expiry notice.

Successful task creation returns an object with an `id`, `status`, `progress`, `model`, and task type. Persist the task ID before polling. Task states are `pending`, `processing`, `completed`, and `failed`.

Scribe task details return text plus optional language and word timing fields. Paraformer task details return a temporary `transcription_url`; download that JSON immediately and use its `transcripts[].sentences[]` millisecond timestamps. Persist the complete task and transcription responses in `.state/private.json` before normalization so `resume` can continue without another AIHub request. The public `transcript.json` preserves the response structure but redacts every temporary URL value. Normalize only fields that are actually present; never derive word or segment timestamps from total duration.

## Retry Policy

- Upload and task creation have no idempotency key.
- HTTP 401 is a deterministic authentication failure for the single credential selected by the ADR 0003 configuration order. Stop immediately and ask the user to correct that credential; do not search for or try another key.
- HTTP 429 is documented as rate-limit rejection and may retry with `Retry-After`, capped at 60 seconds.
- DNS resolution failure and connection refusal prove that the request did not reach the service and may retry.
- Timeouts, connection resets after sending, invalid success responses, and 5xx responses leave a write result ambiguous. Persist `ambiguous_upload` or `ambiguous_asr_create` and do not resubmit automatically.
- Task polling is a read of the same task ID. Retry transient failures up to three attempts per poll, bounded by a wall-clock deadline.
