# AIHub Source Staging And Doc2X Contract

This reference covers the built-in temporary upload and the Doc2X create/poll
contract used by the core conversion workflow. It does not describe image
publication.

## Verified Upstream Contract

- Source: `https://docs.aihubmax.com/openapi/zh/upload-stream.json`
- OpenAPI version: `1.0`
- Retrieved: `2026-07-22`
- Server: `https://api.aihubmax.com`
- Operation: `POST /v1/files/upload/stream`
- Authentication: global Bearer authentication
- Request: `multipart/form-data`; `file` is required, while `file_name` and the
  boolean `auto_cleanup` are optional.
- Cleanup: `auto_cleanup` defaults to `true`; when it is `false`, insufficient
  storage returns HTTP 403 instead of deleting an older upload.
- Success schema: HTTP 200 may contain `id`, `filename`, `url`, `size`, and
  `created`. The schema does not mark those response properties as required.
- Documented response statuses include 400, 401, 403, 413, 429, and 500.

The upstream contract does not define a file-size limit, transport timeout,
multipart framing implementation, redirect behavior, retry behavior, or a
required error-body shape. Do not invent values or infer whether a remote file
was written from an undocumented response body.

## pdf2markdown Interpretation

The workflow always sends the already frozen `01-source/source.pdf` as the
`file` part with filename `source.pdf`, plus `auto_cleanup=false`. It uses only
the fixed server and operation above.

`source_upload_ready` requires all of the following:

- exactly one HTTP 200 response to the POST;
- a strict JSON object response with no duplicate keys;
- a non-empty absolute HTTPS `url` without URL userinfo; and
- confirmation that the streamed file bytes hash to the work-bundle source
  SHA-256.

The workflow records `expires_at` as 72 hours after the local time at which it
validated the ready response. It does not depend on optional upstream
`created`, and expiry never proves that the remote object was deleted.

For this fixed request, HTTP 403 is the only current rejected allowlist entry:
the official operation describes it as insufficient storage when cleanup is
disabled. HTTP 400, 401, 413, 429, 500, other 5xx responses, redirects,
unexpected 2xx responses, malformed responses, and transport interruption are
`source_upload_unknown` because the current contract does not prove that no
remote write occurred.

The POST has no automatic redirect, transport retry, or key fallback. Each
durably recorded staging intent can send at most one POST.

## Verified Doc2X Contract

- Source: `https://docs.aihubmax.com/openapi/zh/doc2x-v3.json`
- Retrieved: `2026-07-22`
- Server: `https://api.aihubmax.com`
- Create: `POST /v1/run/generations` with global Bearer authentication.
- Required create fields: `model`, `pdf_url`, and `page_count`.
- Query: `GET /v1/tasks/{task_id}?sync_upstream=true` with the same Bearer
  authentication.
- Query statuses: `pending`, `processing`, `completed`, and `failed`.
- Document results are ZIP references represented as `results[].url`.
- Result URLs are documented as lasting 24 hours, but the contract does not
  define when that window starts.

The generic task OpenAPI schema does not fully describe the document-task
shape, and its fields are not consistently marked required. The workflow
therefore parses only `status`, `results[].url`, and `error`, while ignoring
unknown additional fields. This does not relax the result URL rules below.

## pdf2markdown Doc2X Interpretation

The normalized create body always uses:

```json
{
  "model": "doc2x-v3",
  "pdf_url": "<private staged URL>",
  "page_count": 1,
  "filename": "document-<source-sha256-8>",
  "convert_mode": "md",
  "formula_mode": "dollar",
  "merge_cross_page_forms": false
}
```

`page_count` is the locally verified value, not a caller guess. The merge flag
is true only for saved `cross_page_table` preflight evidence. The filename is
stable and remains below the upstream 50-character bound.

Only HTTP 200 with a strict JSON object and an `id` matching
`[A-Za-z0-9][A-Za-z0-9._:-]{0,255}` becomes `submitted`. Unexpected 2xx
responses, all non-200 responses, malformed or duplicate-key JSON, unsafe IDs,
and network interruption are `submission_unknown`. The create POST has no
redirect, transport retry, or key fallback, and each durably recorded
conversion attempt can send it at most once.

Polling rereads the exact credential locator and fingerprint saved by create.
It never reruns first-found-wins or searches another account. Local source
missing, local fingerprint drift, HTTP 401, and HTTP 404 are separate
recoverable reasons. Network errors, 429, 5xx, invalid JSON, and unknown task
shapes are `poll_transient`; a later command can only poll the same task.

The upstream guidance recommends exponential backoff but does not prescribe a
local interval, retry count, or deadline. After `task_unavailable` or
`poll_transient`, this workflow persists the consecutive transient count and
`next_poll_at`, starting at 8 seconds and doubling without an additional local
cap; the current polling deadline remains the upper bound. A command before
`next_poll_at` performs no request and keeps the same task.

Pending and processing results use a persisted 720-second local polling
window. A completed task whose `results` is null or an empty list uses a
separate 720-second result-pending window. The 8-second base and 720-second
windows are local, pending-validation precedent derived from the verified
legacy `pdf2md_docx` defaults of an 8-second interval and 90 attempts. They are
not upstream service guarantees. Reaching either deadline stops the current
window without claiming the task or result is permanently unavailable; a
later invocation can continue querying the same task.

For `completed`, every present array item must contain a non-empty absolute
HTTPS `url` without userinfo. Missing, empty, non-string, malformed, or non-HTTPS
entries are `unsafe_result_url`. Exact duplicate full URL strings are
deduplicated. Zero URLs are result-pending, one distinct URL is `result_ready`,
and multiple distinct URLs are `unexpected_result_count`.

The full staged URL and result URL exist only in `.state/private.json` with mode
`0600`. Public state and history keep hashes and non-secret identity evidence.
For result URLs, `observed_at` and `validity_window_hours: 24` are recorded, but
`expires_at` stays null because the upstream validity-window start is undefined.
