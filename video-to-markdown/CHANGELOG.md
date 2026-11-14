# Changelog

## 4.0.0

- Select vision once per task from four choices presented together: main Agent, native host/model, independent Codex Agent with a custom connection, or a Chat Completions image API. Writing and ASR remain independent. Native model catalogs/capabilities come from actual session evidence; unavailable and unverified paths are shown honestly, and model mismatches never trigger silent fallback.
- Add a local configuration page with real dotenv preview, project/global source display, private credentials, atomic save/readback, CSRF protection and 60-minute idle expiry. Fixed host-specific routes or per-new-task questions share the CLI's loader.
- Sample 5-second periodic candidates plus scene/local-region changes, retaining every original through capacity splits. Preserve parent batch context, immutable input hashes, model receipts, request accounting and route-change invalidation.
- Verify original-resolution PNG against decoded PTS, including long GOP, fractional FPS and nonzero starts. Rank dense neighbors with FFmpeg blurdetect; the selected viewer must confirm readable matching content before replacing evidence or final screenshots.
- Support explicit visual skipping, verified no-audio visual-only tasks, persistent two-round per-issue budgets and timed unresolved drafts distinct from complete delivery. Preserve raw API responses and ambiguous outcomes without blind replay.
- **Breaking:** task schema 4 rejects schema 3 and older without migration, recreation or ASR resubmission; `vision-record`/route adapters replace the former `record-observations` CLI. Previously completed documents remain intact. Output naming/copy rules and ASR provider policy continue unchanged.
- Permit supplemental observations to combine new frames with previously inspected same-batch evidence, rechecking saved input and image hashes; prepared or foreign-batch work does not count as inspected evidence.
- Windows: 137 tests, mypy, official/workspace Skill validation, real configuration-page save/readback and desktop/mobile layout checks passed. Actual Codex CLI 0.167.2 completed main-Agent vision with supplied subtitles, dense supplementation, final screenshot checks, portable delivery and completed-task resume. The existing sandbox image-path restriction required presenting the same image bytes through the host's image output tool; permissions were unchanged and the temporary Skill mount was restored. Mac 待验; other native hosts/models and real external model/transcription services remain untested.

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
