---
name: pdf2markdown
description: Create a private, recoverable work bundle from one local PDF and inspect or safely resume its saved state. Use when a user asks to begin a verifiable PDF-to-Markdown workflow from a local PDF, preserve the source bytes for later conversion, inspect an existing pdf2markdown work bundle, or resume one without repeating completed work.
---

# PDF to Markdown

Establish a durable work bundle before performing any later PDF conversion work. Treat the bundled `source.pdf` and its SHA-256 identity as the source of truth.

## Start A Work Bundle

Run:

```bash
python3 scripts/workflow.py start --source <local-pdf> [--output-dir <directory>]
```

Use a local regular file. Do not pass a symlink, directory, special file, or URL.

Resolve the output root in this order:

1. `--output-dir`
2. `PDF2MARKDOWN_OUTPUT_DIR`
3. `$PWD/pdf2markdown-output`

Read the single JSON object from stdout. Preserve `work_bundle`, `generation`, and `evidence_hash` for subsequent commands. Treat stderr as diagnostic logging only.

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
  --expected-generation <generation>
```

Handle `generation_conflict` by inspecting again before retrying. Handle `bundle_locked` by waiting for the active writer to finish. In the current implementation, a valid resume returns `outcome: no_progress`; it does not begin preflight or conversion.

## Interpret Results

- Exit `0`: the command completed; inspect `outcome` for `created`, `inspected`, or `no_progress`.
- Exit `2`: correct the command arguments.
- Exit `3`: provide a readable, regular local PDF with PDF bytes.
- Exit `4`: stop and repair or restore the work bundle; do not bypass integrity or schema failures.
- Exit `5`: resolve a stale generation or concurrent writer before retrying.

Expect exactly one versioned JSON object on stdout for every supported command and structured entries in `errors` on failure. Never infer success from stderr text.

## Scope Boundary

Use these commands only to create and validate the local source work bundle. Do not claim that Markdown was generated. This implementation does not yet support URL sources, PDF preflight, AIHub or Doc2X calls, result archives, content review, image publication, or settings management. Do not invoke `pdf2md_docx`, `upload-for-url`, or `s3-upload` as a substitute inside this workflow.
