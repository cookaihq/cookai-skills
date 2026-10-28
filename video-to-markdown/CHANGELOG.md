# Changelog

## 2.0.0

- Transcription uses installed AIhub Audio or user-authorized Feishu Minutes. AIhub Audio is the default; per-task overrides and configured defaults are supported.
- Definite AIhub failure can switch this task to Feishu once. Feishu quota exhaustion offers an explicit per-task/default choice. Confirmation, processing, uncertain writes and result-download failures retain their original operation.
- Persist provider selection, Feishu identity confirmation, operation intents, raw receipts and retry cooldowns; prevent concurrent task resume.
- Preserve complete raw transcripts, real timing granularity, audio hashes/encoding/track offsets, and block invalid transcription timing.
- Repair native Windows private persistence and runtime bootstrap, preserve supplied-subtitle selection, verify prepared audio and revalidate completed artifacts.
- **Breaking migration:** remove direct AIhub HTTP implementation and credential aliases. Retire `VIDEO_TO_MARKDOWN_ASR_MODEL`; do not copy credentials or silently change its model intent. Preserve but reject old task schemas without resubmission.
- Add user-facing three-host instructions and platform-specific update guidance. New real-service and host acceptance remain separately unverified; see README for recovery and existing video-evidence limitations.
