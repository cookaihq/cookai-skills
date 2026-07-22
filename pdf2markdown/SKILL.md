---
name: pdf2markdown
description: Create and manage a private, recoverable PDF-to-Markdown work bundle with snapshotted interaction and publication settings. Use when a user asks to begin a verifiable workflow from one local PDF or public HTTPS PDF URL, configure confirm or auto behavior, inspect saved state, or resume without inheriting later configuration drift.
---

# PDF to Markdown

Establish a durable work bundle before performing any later PDF conversion work. Accept one local PDF or one unauthenticated public HTTPS PDF URL. Treat the bundled `source.pdf` and its SHA-256 identity as the source of truth.

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

Resolve the output root in this order:

1. `--output-dir`
2. `PDF2MARKDOWN_OUTPUT_DIR`
3. `$PWD/pdf2markdown-output`

Read the single JSON object from stdout. Preserve `work_bundle`, `generation`, and `evidence_hash` for subsequent commands. The manifest freezes effective settings, their sources, the canonical invocation cwd identity, and the persistent settings content hash. Treat stderr as diagnostic logging only.

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
  [--use-local-key]
```

Resume without an explicit setting override from the saved work-bundle snapshot; ignore later environment, dotenv, home, and persistent-settings drift. A valid explicit override creates the next generation and appends its evidence without changing the old history. `--use-local-key` authorizes only the current invocation and does not re-resolve snapshotted non-secret settings.

Handle `generation_conflict` by inspecting again before retrying. Handle `bundle_locked` by waiting for the active writer to finish. A resume without overrides returns `outcome: no_progress`; it does not begin preflight or conversion.

## Interpret Results

- Exit `0`: the command completed; inspect `outcome` for `settings_initialized`, `settings_unchanged`, `settings_status`, `settings_updated`, `created`, `inspected`, `settings_overridden`, or `no_progress`.
- Exit `2`: correct the command arguments.
- Exit `3`: provide a parseable local PDF or an unauthenticated public HTTPS PDF that satisfies the source safety contract.
- Exit `4`: stop and repair or restore the work bundle; do not bypass integrity or schema failures.
- Exit `5`: resolve a stale generation or concurrent writer before retrying.
- Exit `6`: repair invalid persistent settings or correct an invalid explicit override.

Expect exactly one versioned JSON object on stdout for every supported command and structured entries in `errors` on failure. Never infer success from stderr text.

## Scope Boundary

Use these commands only to manage settings and create or validate the frozen source work bundle. Do not claim that Markdown was generated. This implementation does not yet support PDF preflight, AIHub or Doc2X calls, result archives, content review, publication plans, or image publication. Do not invoke `pdf2md_docx`, `upload-for-url`, or `s3-upload` as a substitute inside this workflow.
