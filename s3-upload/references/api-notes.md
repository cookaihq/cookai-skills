# API and result notes

## Output

`--json` prints one closed result object with every key present:

```json
{
  "schema_version": 1,
  "operation": "upload",
  "status": "ok",
  "object_written": true,
  "object_reference": {},
  "url": "https://...",
  "url_kind": "presigned",
  "expires_at": "2026-07-22T13:00:00Z",
  "retention": {"mode": "retain", "days": null, "enforcement": "external-unverified"},
  "delete_scope": null,
  "deleted_version_id": null,
  "checkpoint_id": null,
  "plan": null
}
```

Statuses are `ok`, `dry_run`, `partial_success`, `not_started`, `collision`, `deleted`, `not_deleted`, `aborted`, and `ambiguous`. A failure with no durable outcome leaves stdout empty, including JSON mode.

Without `--json`:

- successful upload/url/resume prints exactly one URL to stdout;
- delete/abort and dry-run keep stdout empty and report status/plan on stderr;
- ambiguous/partial outcomes keep or report their recovery checkpoint and never print logs to stdout.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | confirmed success or executable dry-run |
| 1 | runtime/remote failure, partial result, ambiguous result, not-deleted observation |
| 2 | CLI/config/reference error or formed-but-blocked dry-run |
| 3 | local source error before durable remote state |
| 4 | verified atomic collision |

## Recovery

Every mutating Put/Delete/multipart operation persists a Secret-free checkpoint before its first request. A lost/unknown response is never treated as “not written” and the whole mutation is not automatically repeated.

- `ambiguous`: `object_written=null`, non-null `checkpoint_id`; use read-only reconcile when the exact contract permits it.
- `partial_success`: a confirmed object/reference or resumable multipart state exists; do not restart the original operation.
- `not_started`: checkpoint proves no request was issued.
- terminal checkpoint replay rebuilds output without another mutation, then cleans up after output flush.

All normal baselines currently lack Delete, conditional write, multipart and HEAD reconciliation capabilities, so those command paths remain blocked before checkpoint/network. Their parser presence is a stable interface, not a support claim.

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
