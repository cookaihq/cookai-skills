# Browser Extraction

Use this procedure only after the user requests the specific download, confirms
that the selected account can play the complete target video (including seeking
or playing to near its end), and confirms that they have the right to download
and retain the content.

## Browser Tool Capability

The browser tool must provide all of these capabilities:

1. Operate the user's selected, already logged-in browser session.
2. Execute JavaScript in the target Bilibili page.
3. Document an operation that writes bytes produced by that JavaScript to a
   caller-selected local path without returning those bytes to the model.
4. Return only the temporary path and a safe summary that contains no signed
   media URL.

Read the selected browser integration's complete capability documentation before
opening the play response. If it does not explicitly guarantee all four
conditions, stop. Do not return the manifest to the model, use an evaluate API
whose result serializes the manifest, paste it into a shell command, write to a
default Downloads directory, read cookies, or use another browser profile
without the user's direction.

Before browser extraction, create the destination from the local shell:

```bash
umask 077
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bilibili-charge-download.XXXXXX")"
chmod 0700 "$TMP_DIR"
MANIFEST="$TMP_DIR/manifest.json"
```

Pass only `$MANIFEST` to the documented host-side write operation. The browser
must not choose a different directory.

## Observable Checks

1. Open the exact `bilibili.com/video/<BVID>` page in the selected browser.
2. Read a DOM snapshot. Confirm a logged-in account is visible and note any
   visible preview or access restriction.
3. Require the user's confirmation that the complete video is playable,
   including seeking or playing to near its end. DOM state alone is not
   sufficient.
4. Read the `<video>` element's `duration`; require a finite positive value.
5. Locate an inline `<script>` by the exact `window.__playinfo__=` marker, not by
   a numeric script index. Parse the assignment as JSON inside page JavaScript.
6. Require `code === 0`, `data.timelength > 0`, at least one
   `data.dash.video` entry, and at least one `data.dash.audio` entry.
7. Compare `data.timelength / 1000` with the `<video>` duration and reject a
   difference greater than two seconds. A match only shows that the two runtime
   values agree; it does not independently prove full access or permission to
   download and save the content.

Do not inspect cookies, local storage, browser profiles, passwords, or request
headers. Read only the play response already embedded in the authorized page.

## Stream Selection

Unless the user requests a resolution or codec:

1. Find the maximum numeric video `id` in `data.dash.video`.
2. Restrict candidates to that `id`.
3. Prefer `avc1`; if absent, use `hev1` or `hvc1`, then `av01`.
4. Select the audio entry with the greatest numeric `bandwidth`.

Each selected stream may expose `baseUrl` or `base_url`, and `backupUrl` or
`backup_url`. Preserve the backup list inside the manifest only.

## Manifest Schema

Build this object inside the page execution context:

```json
{
  "schema_version": 1,
  "source_url": "https://www.bilibili.com/video/BV1TEST00000/",
  "bvid": "BV1TEST00000",
  "title": "Synthetic example",
  "duration_ms": 120000,
  "video": {
    "url": "https://example.bilivideo.com/video.m4s?signed=...",
    "backup_urls": [],
    "id": 80,
    "codecs": "avc1.640032",
    "width": 1920,
    "height": 1080,
    "bandwidth": 1178145
  },
  "audio": {
    "url": "https://example.bilivideo.com/audio.m4s?signed=...",
    "backup_urls": [],
    "id": 30280,
    "codecs": "mp4a.40.2",
    "bandwidth": 122100
  }
}
```

The example identifiers and values are synthetic. Never substitute a real
manifest into model-visible output.

## Protected Transfer And Invocation

1. Have the documented browser operation create `$MANIFEST` directly inside the
   pre-created `0700` directory, preferably atomically with mode `0600`. The
   evaluation result must not include the JSON, `url`, or `backup_urls`.
2. Accept only confirmation that the exact preselected path was written, plus a
   safe summary containing BVID, duration, selected resolution, and codecs.
3. In the local shell, immediately set the file mode; stop if `chmod` fails.
   The downloader independently enforces the same permission boundary. Then
   invoke it:

   ```bash
   SKILL_DIR="<absolute path to this skill directory>"
   chmod 0600 "$MANIFEST"
   uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/download_media.py" \
     --manifest "$MANIFEST" \
     --output-dir "$PWD/downloads" \
     --consume-manifest
   ```

Do not pass signed media URLs as command-line arguments or print them during
diagnosis.

## Failure Rules

- No logged-in account: ask the user to sign in in the selected browser and
  stop at that page.
- User cannot confirm complete playback to near the end or the right to save:
  stop before extracting media URLs.
- No `window.__playinfo__` script: reload the same tab once, then inspect the
  current page structure. Do not guess a Bilibili API response.
- Duration mismatch: reject the response as internally inconsistent; do not
  claim that a duration match independently proves authorization.
- Signed URLs expire during download: repeat extraction once from the same
  authorized page and rerun the downloader. Verified range files can resume.
