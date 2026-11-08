# Changelog

## 3.0.0

- **Breaking output contract:** default to `data/video-to-markdown/YYYYMMDDHHMMSS-<complete-video-filename>/`, with the complete video extension retained in the Markdown filename. Task timestamps now use seconds.
- After content review, append a content-derived title only for opaque filenames. Replace manual rename questions and the old `rename` command with resumable `finalize`.
- Add `--copy-dir` for a portable Markdown copy and its referenced images/GIFs. Default to the original local video's directory; URL inputs require an explicit copy directory. Refuse conflicting files without overwriting them.
- Repair managed-source, frame, contact-sheet and dependency paths when the task directory changes. Preserve rename intent for crash recovery, keep the task lock across renames, and verify copies on resume.
- Task schema 3 rejects older tasks without migration or resubmission. Transcription provider behavior is unchanged.
- Windows: 96 script tests passed, plus actual Codex CLI 0.166.0 supplied-subtitle conversion through visual review, finalization, portable delivery and completed-task resume. Verified temporary runtime copies were used without changing sandbox settings. macOS acceptance is pending; real transcription services were not retested.

## 2.0.0

- Transcription uses installed AIhub Audio or user-authorized Feishu Minutes. AIhub Audio is the default; per-task overrides and configured defaults are supported.
- Definite AIhub failure can switch this task to Feishu once. Feishu quota exhaustion offers an explicit per-task/default choice. Confirmation, processing, uncertain writes and result-download failures retain their original operation.
- Persist provider selection, Feishu identity confirmation, operation intents, raw receipts and retry cooldowns; prevent concurrent task resume.
- Preserve complete raw transcripts, real timing granularity, audio hashes/encoding/track offsets, and block invalid transcription timing.
- Repair native Windows private persistence and runtime bootstrap, preserve supplied-subtitle selection, verify prepared audio and revalidate completed artifacts.
- **Breaking migration:** remove direct AIhub HTTP implementation and credential aliases. Retire `VIDEO_TO_MARKDOWN_ASR_MODEL`; do not copy credentials or silently change its model intent. Preserve but reject old task schemas without resubmission.
- Add user-facing three-host instructions and platform-specific update guidance. New real-service and host acceptance remain separately unverified; see README for recovery and existing video-evidence limitations.
