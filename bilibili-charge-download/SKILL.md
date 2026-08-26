---
name: bilibili-charge-download
version: 1.0.0
description: v1.0.0｜Download and verify a Bilibili charge-only video through a logged-in browser session when the user is authorized to watch, download, and retain that content. Use for B站充电视频、充电专属视频、已充电视频 downloads. Do not use to bypass payment, login, preview limits, or any restriction on downloading or saving the content.
compatibility: >-
  Requires uv >= 0.8, Python 3.13, ffmpeg, ffprobe, network access to
  Bilibili and its media CDN hosts, and a browser-control tool attached to a
  logged-in Bilibili session that can execute page JavaScript and write a
  caller-selected temporary path without returning the file's contents
  to the model.
---

# Bilibili Charge Download

Produce a complete, verified MP4 only when the user explicitly says they are
allowed to download and retain the target content.

## Step 0: Check For Updates

Before collecting input, run:

```bash
bash "<skill directory>/scripts/check_update.sh"
```

- Exit code `0`: continue without repeating the script output to the user.
- Exit code `10`: relay the report exactly and ask whether to pull now.
  - If the user agrees, run
    `bash "<skill directory>/scripts/check_update.sh" --pull`. If it succeeds,
    continue with the updated version. If it fails, relay the refusal or failure
    reason and continue with the current version.
  - If the user declines or does not answer, continue with the current version
    and do not mention the update again during this task.
- If the user asks to disable update checks, set `AUTO_UPDATE_CHECK=0` in
  `~/.config/bilibili-charge-download/.env`, preserving every other line.

An update-check failure must never block the download workflow.

## Authorization Boundary

Before accessing the play response, confirm all of the following:

1. The user requested this specific download.
2. The selected browser is logged in to an account that the user says can play
   the complete target video, including seeking or playing to near its end.
3. The user says they have the right to download and retain this content.

Do not infer any of these from a URL, a successful play response, matching
durations, or account login alone. Do not automate login, solve CAPTCHAs,
purchase access, read cookies, or use a preview stream as the final result.

## Workflow

1. Use the browser surface selected by the user and its existing login state.
   Before reading the play response, read that browser integration's complete
   capability documentation. Confirm a documented operation can write bytes to
   a caller-selected local path without serializing those bytes into the tool
   result. If this is not documented, stop before extracting media URLs.
2. Create the manifest destination before browser extraction:

   ```bash
   umask 077
   TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bilibili-charge-download.XXXXXX")"
   chmod 0700 "$TMP_DIR"
   MANIFEST="$TMP_DIR/manifest.json"
   ```

   The browser may write only to `$MANIFEST`. Do not use a default Downloads
   directory or an evaluate API that returns the manifest body.
3. Read [browser-extraction.md](references/browser-extraction.md). Use page
   JavaScript to locate and parse `window.__playinfo__` without reading cookies,
   local storage, browser profiles, passwords, or request headers.
4. Require all of these observable conditions:
   - The page shows a logged-in account and no visible preview restriction.
   - The user has confirmed complete playback, including seeking or playing to
     near the end.
   - The `<video>` duration and `data.timelength` are finite, positive, and
     differ by no more than two seconds. This is an internal consistency check,
     not independent proof of full-viewing or download permission.
   - The play response has `code === 0` and contains both DASH video and audio.
5. Build the manifest inside the browser execution context. The documented
   browser operation must create `$MANIFEST` directly, preferably atomically
   with mode `0600`; the enclosing `0700` directory protects the file even if
   the tool initially uses a broader file mode. Tool output may contain only
   the path and a safe summary, never the manifest body, `url`, or
   `backup_urls` values.
6. Immediately set the manifest to mode `0600`; stop if `chmod` fails. The
   downloader independently rejects group- or world-accessible manifests.
   Then run it from any working directory:

   ```bash
   SKILL_DIR="<absolute path to this skill directory>"
   chmod 0600 "$MANIFEST"
   uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/download_media.py" \
     --manifest "$MANIFEST" \
     --output-dir "$PWD/downloads" \
     --consume-manifest
   ```

7. Treat the final JSON result as success only when `status` is `downloaded` or
   `existing_verified`. Report the final path, stream metadata, duration, size,
   and SHA-256. Never report a partial file as successful.

## Retry Boundary

- The downloader resumes completed byte ranges from its private work directory.
  It restarts an interrupted range instead of appending unverified bytes.
- If every signed media URL has expired, repeat browser extraction once from
  the still-authorized page and rerun the downloader with the new manifest.
- If the refreshed manifest also fails, stop and report the exact failed stage:
  browser authorization, play response, range download, byte validation, mux,
  or media verification.
- Never switch to a preview stream, lower the verification standard, or expose
  signed media URLs while diagnosing a failure.

## Defaults And Diagnostics

- Unless the user requests another resolution or codec, select the highest
  available resolution and prefer AVC at that resolution for compatibility.
- Default output is `downloads/<BVID>/<BVID>-<height>p.mp4`.
- Do not overwrite an existing target. The downloader returns
  `existing_verified` only after the existing file passes verification.
- For a changed Bilibili response shape or repeated failure, read
  [retrospective.md](references/retrospective.md) before changing the workflow.
