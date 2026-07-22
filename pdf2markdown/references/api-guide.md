# AIHub Source Staging Contract

This reference covers only the built-in temporary upload used to give Doc2X the
frozen work-bundle PDF. It does not describe image publication.

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
