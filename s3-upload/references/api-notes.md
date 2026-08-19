# API and result notes

## Output

`--json` prints one closed result object with every key present. The v1 schema is extended in place to seventeen keys: the original thirteen keep their names and meaning, and `remote`, `checkpoint`, `next_action` and `retry_safety` close the caller contract. A value that does not apply is an explicit `null`, never an omitted key.

```json
{
  "schema_version": 1,
  "operation": "upload",
  "status": "ok",
  "object_written": true,
  "object_reference": {},
  "url": "https://...",
  "url_kind": "presigned",
  "expires_at": "2026-07-27T13:00:00Z",
  "retention": {"mode": "retain", "days": null, "enforcement": "external-unverified"},
  "delete_scope": null,
  "deleted_version_id": null,
  "checkpoint_id": null,
  "plan": null,
  "remote": {"key": "objects/report.bin", "size": 7, "sha256": "ed7002b4..."},
  "checkpoint": null,
  "next_action": null,
  "retry_safety": null
}
```

Statuses are `ok`, `adopted`, `dry_run`, `partial_success`, `not_started`, `collision`, `deleted`, `not_deleted`, `aborted`, and `ambiguous`. A failure with no durable outcome leaves stdout empty, including JSON mode.

The nine-field caller contract reads the result as `status`, `object_written`, `url`, `url_kind`, `expires_at`, `retention`, the remote identity, `checkpoint`, and the `next_action` + `retry_safety` pair:

- `remote` is the closed `{key, size, sha256}` container. `key` always equals `object_reference.location.key` (both null when no reference exists, e.g. `ambiguous`); `size`/`sha256` carry the checkpointed source snapshot on terminal upload results and on a converged reconcile, and require a non-null `key`.
- `checkpoint` mirrors `checkpoint_id`; the two are always equal.
- `next_action` is `"reconcile"` exactly when a checkpoint is retained, otherwise `null`.
- `retry_safety` is derived from the status alone: `null` for `ok`/`adopted`/`dry_run`/`deleted`/`aborted` (a retry is not a meaningful question), `"safe"` for `not_started`/`collision`/`not_deleted` (provably nothing was written), `"unsafe"` for `ambiguous`/`partial_success` (a blind re-run could double a write).

The three derived fields are computed at construction and validated against the rest of the result, so a printed result can never carry a contradictory combination (for example `status="ok"` with `object_written=false` is rejected before output).

Without `--json`:

- successful upload/url/resume prints exactly one URL to stdout;
- delete/abort and dry-run keep stdout empty and report status/plan on stderr;
- ambiguous/partial outcomes keep or report their recovery checkpoint and never print logs to stdout.

## Atomic no-overwrite and adoption (`--collision reject`)

On the `aws-s3` and `cloudflare-r2` baselines, `upload --collision reject` (or a Target with `"collision": "reject"`) sends the single PutObject with `If-None-Match: *` inside the signed header set, so no-overwrite is decided atomically by the provider. A remote 412 means the key already holds an object; the CLI then issues exactly one presigned full-body GET and compares the remote bytes against the planned source:

- size and SHA-256 both match: the existing object is adopted as the outcome. `status="adopted"`, `object_written=false`, exit 0, the result carries the usual Object Reference, URL and `remote` identity, and no second Put is sent.
- anything short of that proof (different bytes, different length, or a body that could not be read in full): `status="collision"`, `object_written=false`, exit 4, and the local source is not re-sent.

HEAD or ETag comparison is never used as adoption evidence. `--collision` on the CLI overrides the Target policy for one invocation and is visible in the dry-run `plan.collision`.

## Result handoff file (`--result-out <path>`)

`upload --result-out <path>` atomically writes the result JSON (temp file + fsync + rename, mode 0600) to a caller-chosen file whose bytes equal the stdout `--json` line exactly. The destination goes through the same preflight discipline as `--reference-out` (protected namespaces, unsafe parents, source aliasing; an existing file must be a prior result JSON); a preflight failure rejects the upload with `config_error`, exit 2, before any remote request is issued. The file is written for confirmed success, for a failure with a durable result (`ambiguous`/`partial_success`/`collision`), for `--dry-run`, and for a plan blocked before any request (a `not_started` result whose inapplicable fields are explicit null, exit stays 2). A write failure after the operation is loud: stderr gets `[s3-upload] result_error: ...`, an exit code of 0 becomes 1, and a non-zero exit code is preserved.

Three properties make the file readable by a separate process that never sees this command's exit code, in every case where the file can be written at all:

- **The file always belongs to the current run.** As soon as the preflight passes and before the first remote request, a `not_started` placeholder is written; every later write replaces it. A run that dies without a durable result therefore leaves this run's own state, never the previous run's `ok`. If that placeholder cannot be written, the upload stops at exit 1 with `result_error` and issues no remote request.
- **A `not_started` left standing over a request that went out has exactly two causes.** The placeholder claims no object was written and that retrying is safe, and the claim only holds until the operation announces its first remote request (the `checkpoint_id=` line on stderr). Any exit after that point which does not produce its own terminal result — including an unexpected raise between a successful Put and the terminal result — rewrites the file as `ambiguous` with that checkpoint, `next_action="reconcile"` and `retry_safety="unsafe"`, and repeats the `[s3-upload] ambiguous checkpoint_id=...` line on stderr. The two post-request exits that keep `not_started` are (a) a definitive 4xx (`DefinitiveNoWrite`, for example a 403), which proves no object was written, and (b) a refused terminal write (`result_error`, exit ≥ 1) — the destination cannot be updated at all, so the placeholder stands and stderr plus the exit code are the only truthful channels for that run.
- **The destination is re-checked at write time, not only at preflight.** The preflight snapshot (parent device/inode/owner/mode, and the destination file's own identity and digest) is verified again immediately before each write, with the publish itself done through the pinned parent descriptor. A destination or parent directory swapped during the upload is refused with `result_error` instead of being overwritten, and an absent destination is created with `O_EXCL`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | confirmed success, adopted existing object, or executable dry-run |
| 1 | runtime/remote failure, partial result, ambiguous result, not-deleted observation |
| 2 | CLI/config/reference error or formed-but-blocked dry-run |
| 3 | local source error before durable remote state |
| 4 | verified atomic collision |

## Recovery

Every mutating Put/Delete/multipart operation persists a Secret-free checkpoint before its first request. A lost/unknown response is never treated as “not written” and the whole mutation is not automatically repeated.

- `ambiguous`: `object_written=null`, non-null `checkpoint_id` (mirrored in `checkpoint`, with `next_action="reconcile"`); use read-only reconcile.
- `partial_success`: a confirmed object/reference or resumable multipart state exists; do not restart the original operation.
- `not_started`: proves no object was written. That is weaker than "no request was issued" — a definitive 4xx (a 403, say) is a request that went out and still proves nothing was written.
- terminal checkpoint replay rebuilds output without another mutation, then cleans up after output flush.

`reconcile --checkpoint <id>` settles a `put_unknown` checkpoint (a `put_in_flight` one is folded into it) with exactly one presigned full-body GET compared against the checkpointed source size and SHA-256. It issues zero write requests and never re-sends the Put. The verifying read requires an executable `PresignGetObject` capability (present on every normal baseline); without it the reconcile stays `ambiguous` without issuing any read:

- both match: the result converges to `status="ok"` with `object_written=null` (presence and content are proven, writer identity is not claimed), the `remote` identity is filled from the checkpoint source snapshot, and the checkpoint is removed; exit 0.
- a definitive 404: converges to `not_started` (`object_written=false`, `retry_safety="safe"`), checkpoint removed; exit 1.
- anything else (unreadable, denied, truncated, or different bytes): stays `ambiguous`, the checkpoint is retained and `next_action` remains `"reconcile"`; exit 1.

Conditional write (`ConditionalPutObject`) is enabled on the `aws-s3` and `cloudflare-r2` baselines; it stays disabled for `custom` and the OSS/COS experimental presets. All normal baselines still lack Delete and multipart capabilities, so those command paths remain blocked before checkpoint/network. Parser presence is a stable interface, not a support claim.

## Object Reference and URL

Object Reference snapshots provider, endpoint, addressing, region, bucket, actual key, optional version id, Access and Retention. Before signing a later command, a current Target with the same location fingerprint must authorize credentials.

All public and presigned URLs address the current Object Key. A captured version id is never added to URL query; it only chooses exact-version delete scope when that capability is available. Replacing the same key can therefore make an older Object Reference URL serve newer content.

Public Base URL is a user declaration and is not probed. Private presigned expiry is capped by temporary credential expiry minus a 60-second safety margin.

## Dry-run plan

Dry-run performs configuration, source/reference and capability validation but creates no checkpoint, signs no request and performs no network I/O. Its strict `plan` includes resolved Target provenance, exact Contract Key, ordered remote operations, required capability states/evidence, headers, Access/Retention, collision policy and blocker codes.

Capability states are:

- `enabled`: executable and backed by the reviewed evidence id;
- `experimental`: executable, based on official provider constraints plus offline vectors, but not backed by complete reviewed release evidence for the exact declared contract; bounded non-release observations may exist;
- `test-only`: executable only through the exact maintainer live-test interlock/evidence path;
- `disabled`: deliberately unavailable;
- `unknown`: no matching exact contract entry.

Entering maintainer test-only mode still requires the process-only exact Target interlock for `experimental` rows. This prevents a normal capability from weakening live-test authorization.

## Secret boundaries

Credential values are used only for signing. Object References, plans, checkpoints, evidence summaries and stderr never contain Secret Access Key, Session Token, Authorization headers or signed URL query values outside the result's designated `url` field.
