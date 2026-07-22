---
name: pdf2markdown
description: Create, preflight, convert, and safely adopt an immutable raw Markdown work bundle from one PDF. Use when a user asks to begin or resume a verifiable PDF-to-Markdown workflow from a local PDF or public HTTPS PDF URL without an external source uploader.
---

# PDF to Markdown

Establish a durable work bundle, complete its preflight gate, use the built-in AIHub source-staging upload, create and poll one Doc2X conversion attempt at a time, and atomically adopt each validated ZIP as an immutable raw conversion. A new paid attempt requires an explicit bound decision whenever the preceding attempt has an uncertain submission, explicit failure, result-count error, or unusable result layout. Accept one local PDF or one unauthenticated public HTTPS PDF URL. Treat the bundled `source.pdf` and its SHA-256 identity as the source of truth.

## Manage Settings

Initialize, inspect, or update the non-secret persistent settings file:

```bash
python3 scripts/workflow.py settings init
python3 scripts/workflow.py settings status
python3 scripts/workflow.py settings set-mode confirm
python3 scripts/workflow.py settings set-mode auto
python3 scripts/workflow.py settings set-publish-mode skip
python3 scripts/workflow.py settings set-publish-mode upload
```

Use `--interaction-mode confirm|auto`, `--publish-mode skip|upload`, `--publish-with <skill:name|tool:name>`, and `--publish-target <opaque-name>` with `settings status` to inspect one-call overrides without persisting them. Add `--use-local-key` only when the current invocation may read `~/.config/pdf2markdown/.env`; this permission is never saved or inherited by a work bundle.

Resolve each non-secret value independently in this order: command option, process environment, `$PWD/.env.local`, `$PWD/.env`, explicitly authorized home `.env`, `settings.json`, built-in default. Use `PDF2MARKDOWN_INTERACTION_MODE`, `PDF2MARKDOWN_PUBLISH_MODE`, `PDF2MARKDOWN_UPLOADER`, and `PDF2MARKDOWN_UPLOAD_TARGET` in environment or dotenv layers. Treat empty values as absent. Defaults are `interaction_mode=confirm` and `publishing.mode=skip`.

Do not put API keys, credentials, signed URLs, or bearer URLs in `settings.json`. Treat `publishing.mode=upload` without a complete publisher binding as blocked; the current implementation never plans or performs publication.

## Start A Work Bundle

Run:

```bash
python3 scripts/workflow.py start \
  --source <local-pdf-or-public-https-url> \
  [--output-dir <directory>] \
  [--interaction-mode confirm|auto] \
  [--publish-mode skip|upload] \
  [--publish-with <skill:name|tool:name>] \
  [--publish-target <opaque-name>] \
  [--use-local-key]
```

For a local source, use a readable regular file. Do not pass a symlink, directory, or special file. For a URL source, use an unauthenticated public HTTPS URL; do not add userinfo, cookies, browser state, or request headers.

Treat every URL query and fragment as sensitive. The workflow sends the query only to the validated HTTPS target, never sends the fragment, and persists only query-free URLs plus the SHA-256 of the complete original input URL. Do not put the complete URL in logs or user-facing reports.

The workflow validates every redirect, all resolved A/AAAA endpoints, the connected TLS peer, response limits, `application/pdf` content type, the `%PDF-` signature, and PyMuPDF parser identity before committing the work bundle. It never sends `AIHUB_API_KEY`, Cookie, proxy credentials, or browser authentication to a source URL. Read [references/security-limits.md](references/security-limits.md) when diagnosing a rejected URL or reviewing the source-download boundary.

Initial source acceptance therefore requires PyMuPDF so `start` can reject non-PDF bytes before committing them as a source PDF. The recoverable dependency gate below applies once a valid work bundle already exists and a required preflight dependency is later missing or API-incompatible.

Resolve the output root in this order:

1. `--output-dir`
2. `PDF2MARKDOWN_OUTPUT_DIR`
3. `$PWD/pdf2markdown-output`

Read the single JSON object from stdout. Preserve `work_bundle`, `generation`, and `evidence_hash` for subsequent commands. The manifest freezes effective settings, their sources, the canonical invocation cwd identity, and the persistent settings content hash. Treat stderr as diagnostic logging only.

## Build The Page Baseline

Before calling `advance`, confirm that the current Agent host can actually inspect local PNG images. Declare that capability explicitly; do not infer it from the PDF or send page images to another service:

```bash
python3 scripts/workflow.py advance \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --visual-capability available \
  [--render-dpi <72-600>]
```

The default is 300 DPI. `advance` checks the required PyMuPDF, Pandoc, and BeautifulSoup4 APIs plus the explicit host visual declaration before rendering. It never installs a missing dependency. A missing or API-incompatible dependency is saved as `recoverable_error / dependency_missing`; after restoring it, run the same command with the newly returned generation.

A successful baseline creates `01-source/source-inventory.json` and one lossless full-page PNG per source page under `02-pages/`. Preserve the returned `generation`, one-time `action_id`, and `evidence_hash`. Read [references/preflight-contract.md](references/preflight-contract.md) for dependency purposes, limits, evidence fields, risk codes, and the record schema.

## Record Preflight

Inspect every page reference image and use the source inventory for non-visual evidence such as link targets, form values, rotation, image occurrences, and annotations. Submit one conclusion for every page through the workflow; never edit authoritative JSON directly:

```bash
python3 scripts/workflow.py record preflight \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --action-id <action-id> \
  --evidence-hash <sha256-evidence> \
  --input <preflight-record.json>
```

The workflow derives the overall result again from the page conclusions. All unqualified `content` pages produce `pass`; any `blank` or `risk` page produces `warning`; any `unreadable` page or an all-blank document produces `blocked`. Deterministic password, damage, render, numbering, and resource failures are blocked before an Agent action is offered.

In `confirm` mode, a warning returns a new decision action. Apply it with:

```bash
python3 scripts/workflow.py record decision \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --action-id <action-id> \
  --evidence-hash <sha256-evidence> \
  --decision accept|decline \
  --basis <non-empty-reason>
```

In `auto` mode, a warning records `interaction_mode_auto` acceptance and advances without a decision action. A block never advances in either mode. A successful preflight reaches `ready_to_submit`; run `advance` or `resume` again to perform source staging.

## Stage The Frozen Source

Configure `AIHUB_API_KEY` for the staging invocation. Resolve exactly one non-empty value in this order: process environment, `$PWD/.env.local`, `$PWD/.env`, then `~/.config/pdf2markdown/.env` only with `--use-local-key`. Dotenv values are literal and are never shell-expanded. The selected attempt records a stable non-secret source locator and key fingerprint; it never records the key and never falls back to a lower source after 401.

Run either command after preflight reaches `ready_to_submit`:

```bash
python3 scripts/workflow.py advance \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --visual-capability available \
  [--use-local-key]

python3 scripts/workflow.py resume \
  --work-bundle <directory> \
  --expected-generation <generation> \
  [--use-local-key]
```

Staging sends one non-retried, non-redirected POST to the fixed AIHub stream endpoint. It streams the same `source.pdf` bytes, sends `auto_cleanup=false`, and never calls `upload-for-url`, `s3-upload`, or another uploader. Read [references/api-guide.md](references/api-guide.md) for the verified upstream contract and conservative result classification.

`source_upload_ready` keeps the temporary URL and locally derived 72-hour expiry only in `.state/private.json`. Before expiry, the next `advance` or `resume` reuses it without sending another upload POST. Conversion starts only when the staging attempt's exact credential locator still yields the same fingerprint. Expiry preserves the old attempt and URL evidence. Confirm mode returns a bound retry action; auto mode appends a new attempt and performs one replacement upload without an intermediate question.

HTTP 403 is `source_upload_rejected` for this fixed `auto_cleanup=false` request. Repair storage capacity, then apply the returned retry action. Network interruption, redirects, 401, 413, 429, 5xx, abnormal 2xx, and invalid URLs are `source_upload_unknown` and are never replayed automatically.

In confirm mode, resolve a returned staging action through the same workflow boundary:

```bash
python3 scripts/workflow.py record source-staging \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --action-id <action-id> \
  --evidence-hash <sha256-evidence> \
  --decision retry|wait \
  --basis <non-empty-reason>
```

Use `retry` only after accepting the disclosed possibility that an unknown attempt left a remote file, or after repairing a rejected/expired attempt. It appends a new attempt; it never overwrites history. `wait` is valid only for unknown results. It waits through a conservative 72-hour window, does not claim remote deletion, and issues a fresh bound decision action when the window elapses. Auto mode keeps unknown and rejected results stopped.

## Create And Resume The Doc2X Attempt

After `source_upload_ready`, run `resume` with the latest generation and the exact `AIHUB_API_KEY` source used by staging:

```bash
python3 scripts/workflow.py resume \
  --work-bundle <directory> \
  --expected-generation <generation> \
  [--use-local-key]
```

The create request is fixed to `doc2x-v3`, `convert_mode=md`, `formula_mode=dollar`, the locally verified page count, and `document-{source-sha256-8}`. `merge_cross_page_forms` is true only when the saved preflight contains `cross_page_table` evidence. The workflow durably appends a `submitting` attempt before its single non-retried, non-redirected POST. Only HTTP 200 with a strict JSON object and a bounded safe `id` becomes `submitted`; every response or interruption without such an ID becomes `submission_unknown`.

Confirm mode returns `resolve_submission_unknown` after an unknown create result, or `resolve_task_failed` after an explicit upstream failure. Accept the possible duplicate conversion charge by applying the bound action:

```bash
python3 scripts/workflow.py record conversion \
  --work-bundle <directory> \
  --expected-generation <generation> \
  --action-id <action-id> \
  --evidence-hash <sha256-evidence> \
  --decision retry \
  --basis <non-empty-reason>
```

This command appends a new `not_started` attempt and leaves every older attempt unchanged. A later `resume` sends that new attempt once. Auto mode keeps `submission_unknown` and failed tasks stopped without an action or an implicit second charge.

Once submitted, each `resume` rereads only the recorded credential locator and polls only the recorded task ID at the fixed AIHub endpoint. It never searches lower-priority keys or another account. Missing and changed credential sources, poll 401, poll 404, and transient HTTP 403, network, 429, 5xx, or JSON failures have distinct recoverable evidence. Poll 404 and transient failures persist an exponential retry schedule starting at 8 seconds; a `resume` before `next_poll_at` performs no request. Pending and processing tasks use a persisted 720-second polling window; a completed task with no results uses a separate persisted 720-second result window. A later command may continue querying the same task after either timeout.

`completed` is usable only when `results` contains exactly one distinct absolute HTTPS URL. Exact duplicate values are deduplicated. A missing or empty URL entry, malformed URL, or multiple distinct URLs stops without guessing. The complete result URL is stored only in `.state/private.json`; stdout, `manifest.json`, and history contain its SHA-256 and non-secret timing evidence. The upstream says result URLs last 24 hours but does not define the start instant, so the workflow records `expires_at: null` rather than inventing a deadline.

`outcome: result_ready` with `conversion_state: result_downloading` is an intermediate handoff. `inspect` reports that state without consuming the URL. The next `resume` durably records one raw-adoption reservation and its identity-bound intent before downloading the same private result reference.

## Adopt The Raw Conversion

Run `resume` with the `result_ready` generation. The result download uses the same public-HTTPS protections as source download: every redirect and all resolved addresses are validated, the connected TLS peer must match the selected public endpoint, and the body is streamed in 64 KiB chunks with fixed time and byte limits. It sends no AIHub Authorization header, Cookie, Referer, proxy credentials, browser state, or ambient authentication. The signed URL remains only in `.state/private.json`; the intent binds its SHA-256, task ID, attempt ID, unique staging identity, and all active limits before `GET`.

The downloaded archive is saved as an exclusive `0600` `result.zip`. Only ordinary files and directories using ZIP stored or deflated compression are accepted. Encrypted entries, symlinks, devices, special members, absolute or escaping paths, backslashes, drive paths, NUL names, duplicates, Unicode/casefold collisions, and file/directory prefix conflicts are rejected. The workflow enforces archive, member-count, member-size, raw and canonical path/component, path-depth, total-path-component, total compressed, total uncompressed, and staging-disk limits before and while extracting. It never calls `extractall`; directories and files are created through no-follow directory descriptors with modes `0700` and `0600`, then CRC, declared size, EOF, file hashes, and the complete tree hash are verified.

Before any result `GET`, the operation verifies that its random staging name, sibling owner-marker name, and final attempt name are absent, then durably records a reservation. It exclusively creates a `0600` owner marker containing the reservation hash and fsyncs it and the attempts parent before exclusively creating and fsyncing the `0700` staging directory. The identity-bound intent is made durable before the marker is removed and the parent is fsynced again. Reservation-only recovery may recreate the marker when both marker and staging are still absent; once staging exists, the exact marker must match and the directory must be empty. An unreserved orphan, foreign payload, preseeded final path, changed marker, or replaced identity is never adopted.

The operation prepares both ZIP and raw-tree evidence under that reservation. It fsyncs `result.zip`, every extracted file, every explicit and implicit directory, the raw root, and the attempt root before writing the prepared record. It then renames the whole directory exactly once to `03-converted/attempts/conversion-attempt-NNNN/`, fsyncs the attempts parent again before manifest/private commit, and finally records the committed event. `converted` means this immutable raw baseline exists; it does not mean the later content-semantic review is complete.

Main Markdown selection is deterministic and case-sensitive. Prefer exactly one recursive member whose basename is `<request-filename>.md`. If none matches exactly, accept only when the entire tree contains exactly one lowercase `.md` file. Zero, multiple, or multiple exact matches produce `unexpected_result_layout`: the already paid-for, safely validated ZIP and raw tree are still adopted, but no `raw_markdown` artifact is claimed. Confirm mode returns `resolve_unexpected_result_layout`; only a matching `record conversion --decision retry` may authorize a new paid attempt. Auto mode stops without an action or another charge.

Recovery never creates another Doc2X task implicitly. A complete local ZIP is revalidated and locally re-extracted without network access, including when the URL is no longer usable. If no complete ZIP exists and result `GET` returns 401, 403, or 404, the failed raw operation is durably closed as `result_url_unavailable`; the next `resume` rereads the attempt's exact credential locator and polls the same task ID for a replacement result URL. The replacement URL remains private, and a new raw operation adopts it without creating a conversion attempt. Missing/changed credentials, poll 401/404, and transient refresh failures remain recoverable on the same task.

A prepared operation accepts exactly one matching part or final directory; missing, duplicate, replaced, type-drifted, or hash-drifted paths are integrity failures. A crash after rename, parent fsync, private-state write, manifest write, or before the final history event only completes the same adoption. Deterministic unsafe-archive rejection is saved as `terminal_error` with its precise reason and is not replayed by later `inspect` or `resume`. Every conversion attempt and every result-URL refresh has an append-only raw operation record; earlier ZIPs and trees remain at their attempt paths when a later explicitly authorized attempt succeeds.

Read [references/security-limits.md](references/security-limits.md) for the fixed local limits and [references/api-guide.md](references/api-guide.md) for the boundary between verified upstream facts and local conservative policy.

## Inspect Saved State

Run a read-only inspection when reporting status or checking a work bundle:

```bash
python3 scripts/workflow.py inspect --work-bundle <directory>
```

Use the returned `conversion_state`, `publication_state`, `outcome`, `artifacts`, and `errors`. Do not edit `manifest.json`, `.state/private.json`, or `.state/history.ndjson` directly.

## Resume Safely

Pass the generation most recently returned by `start` or `inspect`:

```bash
python3 scripts/workflow.py resume \
  --work-bundle <directory> \
  --expected-generation <generation> \
  [--interaction-mode confirm|auto] \
  [--publish-mode skip|upload] \
  [--publish-with <skill:name|tool:name>] \
  [--publish-target <opaque-name>] \
  [--visual-capability available|unavailable] \
  [--render-dpi <72-600>] \
  [--use-local-key]
```

Resume without an explicit setting override from the saved work-bundle snapshot; ignore later non-secret environment, dotenv, home, and persistent-settings drift. A valid explicit override creates the next generation and appends its evidence without changing the old history. `--use-local-key` authorizes only the current invocation. Source staging resolves a key only when a new upload attempt is about to start. Conversion creation and polling reread the exact saved locator and fingerprint; they never silently choose another key.

Handle `generation_conflict` by inspecting again before retrying. Handle `bundle_locked` by waiting for the active writer to finish. Before preflight is ready, a resume without overrides returns `outcome: no_progress`. Supplying `--visual-capability` makes `resume` use the same deterministic preflight progression as `advance`; once preflight is ready, resume can stage or recover the source without rerunning visual work.

## Interpret Results

- Exit `0`: the command completed; inspect `outcome`, `conversion_state`, `conversion_attempt_state`, `raw_conversion_state`, `action_required`, and `errors` to distinguish creation, preflight, source staging, submission, polling, result readiness, raw adoption, layout failure, and durable rejection.
- Exit `2`: correct the command arguments.
- Exit `3`: provide a parseable local PDF or an unauthenticated public HTTPS PDF that satisfies the source safety contract.
- Exit `4`: stop and repair or restore the work bundle; do not bypass integrity or schema failures.
- Exit `5`: resolve a stale generation or concurrent writer before retrying.
- Exit `6`: repair invalid persistent settings or correct an invalid explicit override.

Expect exactly one versioned JSON object on stdout for every supported command and structured entries in `errors` on failure. Never infer success from stderr text.

## Scope Boundary

Use these commands to manage settings, freeze the source PDF, generate the page baseline, complete the preflight gate, temporarily stage the frozen source, create one-at-a-time Doc2X conversion attempts, and atomically adopt an immutable raw conversion for each attempt that returns a usable ZIP. Do not claim content-semantic fidelity or a reviewed local final Markdown yet. This implementation does not yet perform content review, create a corrected Markdown, select a reviewed local final pointer, create publication plans, or publish images. Do not invoke `pdf2md_docx`, `upload-for-url`, or `s3-upload` as a substitute inside this workflow.
