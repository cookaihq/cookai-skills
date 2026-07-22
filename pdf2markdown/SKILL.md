---
name: pdf2markdown
description: Create, preflight, and temporarily stage a private, recoverable PDF-to-Markdown work bundle with frozen settings, a source inventory, and full-page reference images. Use when a user asks to begin or resume a verifiable workflow from one local PDF or public HTTPS PDF URL, inspect every source page before conversion, or prepare the frozen PDF for Doc2X without an external uploader.
---

# PDF to Markdown

Establish a durable work bundle, complete its preflight gate, and use the built-in AIHub source-staging upload before any paid conversion. Accept one local PDF or one unauthenticated public HTTPS PDF URL. Treat the bundled `source.pdf` and its SHA-256 identity as the source of truth.

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

`source_upload_ready` keeps the temporary URL and locally derived 72-hour expiry only in `.state/private.json`. Before expiry, later `advance` or `resume` calls reuse it without reading a key or sending another POST. Expiry preserves the old attempt and URL evidence. Confirm mode returns a bound retry action; auto mode appends a new attempt and performs one replacement upload without an intermediate question.

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

Resume without an explicit setting override from the saved work-bundle snapshot; ignore later non-secret environment, dotenv, home, and persistent-settings drift. A valid explicit override creates the next generation and appends its evidence without changing the old history. `--use-local-key` authorizes only the current invocation. Source staging resolves a key only when a new attempt is about to start; ready and unknown recovery never silently chooses another key.

Handle `generation_conflict` by inspecting again before retrying. Handle `bundle_locked` by waiting for the active writer to finish. Before preflight is ready, a resume without overrides returns `outcome: no_progress`. Supplying `--visual-capability` makes `resume` use the same deterministic preflight progression as `advance`; once preflight is ready, resume can stage or recover the source without rerunning visual work.

## Interpret Results

- Exit `0`: the command completed; inspect `outcome`, `conversion_state`, `action_required`, and `errors` to distinguish creation, dependency recovery, pending preflight, warning, blocking, and readiness.
- Exit `2`: correct the command arguments.
- Exit `3`: provide a parseable local PDF or an unauthenticated public HTTPS PDF that satisfies the source safety contract.
- Exit `4`: stop and repair or restore the work bundle; do not bypass integrity or schema failures.
- Exit `5`: resolve a stale generation or concurrent writer before retrying.
- Exit `6`: repair invalid persistent settings or correct an invalid explicit override.

Expect exactly one versioned JSON object on stdout for every supported command and structured entries in `errors` on failure. Never infer success from stderr text.

## Scope Boundary

Use these commands to manage settings, freeze the source PDF, generate the page baseline, complete the preflight gate, and temporarily stage the frozen source for Doc2X. Do not claim that Markdown was generated. This implementation does not yet create or poll Doc2X tasks, download result archives, perform content review, create publication plans, or publish images. Do not invoke `pdf2md_docx`, `upload-for-url`, or `s3-upload` as a substitute inside this workflow.
