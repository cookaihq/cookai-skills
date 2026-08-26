# Download Retrospective

This document retains the technical conclusions from development without
recording any real video identifier, title, signed URL, file size, or digest.

## Browser Extraction Findings

1. Selecting an inline script by numeric index is unstable because Bilibili can
   change page composition between loads. Locate the exact
   `window.__playinfo__=` marker instead.
2. Reading or exporting `SESSDATA` is unnecessary. The logged-in page already
   contains the play response needed by this workflow.
3. Returning the parsed play response to the model exposes signed media URLs.
   Build the manifest inside page JavaScript and have the browser tool write or
   download it directly to a temporary file.
4. Passing media URLs to `curl` or another command exposes signed query values
   in the process list. The downloader must read them from the mode-`0600`
   manifest and keep them in process memory.
5. Matching page and play-response durations is only a consistency check. If
   both surfaces report the same preview duration, the comparison cannot prove
   full playback or download permission; explicit user confirmation remains
   required.

## Download Integrity Findings

1. A single long transfer can stall even when the CDN continues to serve valid
   data. Split each resource into exact HTTP byte ranges and resume completed
   ranges independently.
2. Never append a new response to an interrupted partial range. The resulting
   file can have the expected byte count while containing invalid media data.
   Write each attempt to a private partial file and publish the range atomically
   only after one response supplies the complete requested interval.
3. Container duration is not sufficient proof of integrity. A damaged MP4 can
   still declare the expected duration while later packets and frames are
   unreadable.
4. Before publishing success, compare packet counts and packet payload bytes
   across the downloaded streams and the stream-copy mux, scan the complete
   MP4, and decode frames at multiple positions including near the end.
5. Write the final MP4 to a temporary file and atomically replace the target
   only after mux succeeds. Preserve the private work directory after failure;
   delete only that directory after verification succeeds.

## Synthetic Regression Test

The automated test uses the synthetic BVID `BV1TEST00000`, media generated
locally by `ffmpeg`, and a local HTTP server with byte-range support. It covers:

- fallback from an unavailable primary URL to a working backup;
- an interrupted range followed by a clean whole-range retry;
- stream-copy mux and `ffprobe` verification;
- removal of the consumed manifest;
- rejection of a deliberately corrupted existing output; and
- absence of local media URLs from downloader output.

No Bilibili login, Cookie, real title, signed URL, or production media is used
by this test.

## Structural Rules

- Separate browser authorization from deterministic downloading with a
  mode-`0600` manifest.
- Keep signed URLs inside the protected manifest and downloader memory.
- Probe `Content-Range`, verify exact per-range and assembled byte totals, and
  restart incomplete ranges instead of appending.
- Require `ffprobe`, full demux scanning, packet preservation, and multi-position
  frame decoding before reporting success.
- If signed URLs expire, refresh the manifest once from the same authorized
  browser page and reuse already verified ranges.
- Never replace a failed stage with a lower verification standard.

## Diagnostic Order

Locate the first failed stage in this order:

1. User authorization, browser login, and confirmed complete playback to near
   the end.
2. Embedded play response and stream selection.
3. Protected manifest transfer, permissions, and schema.
4. CDN range support and exact `Content-Range`.
5. Per-range byte totals and assembled resource size.
6. `ffmpeg` stream-copy mux.
7. Final stream metadata, packet preservation, total duration, full demux scan,
   and frame decoding.

## Future Performance Work

More byte ranges than concurrent workers can reduce the amount of data delayed
by a slow response while allowing other workers to continue. Any such change
must preserve exact range validation, atomic part publication, resume behavior,
and packet-integrity checks.
