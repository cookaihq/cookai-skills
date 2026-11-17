# Transcription and recovery

The two providers share the same video, transcript, visual-observation and Markdown pipeline. A valid supplied SRT/VTT/timestamped text bypasses cloud transcription. A missing/invalid supplied file remains selected on resume. Never substitute ASR for it without the user's changed request.

## Provider selection

New tasks use `--provider`, then `VIDEO_TO_MARKDOWN_ASR_PROVIDER`, then `aihub-audio`. Resume uses the saved task choice. `--only-provider` preserves a user's prohibition against another platform. Tell the user which provider will receive the extracted audio, including automatic fallback.

Use the same business working directory throughout. Project configuration is read from that directory, not from the Skill or output folder. `--use-local-key` explicitly enables the independent Skill's personal global file; this choice survives resume. `--no-global-config` disables global business reads for both the Skill and AIhub dependency throughout the task.

## AIhub Audio

1. `load_aihub_audio`: discover and load the installed `aihub-audio` using this host's actual full identifier and verified source. Claude Code, Codex and WorkBuddy do not share an invented Skill-call API. The adapter never searches another checkout for credentials or silently reinstalls a dependency.
2. Resume with `--aihub-plugin-dir <actual-aihub-studio-root> --request <user-original-request>`. The request is saved privately. If the host has verified that the dependency is unavailable, use `--aihub-unavailable`; omitting the path alone does not declare it missing. Reviewed contracts are AIhub Studio 1.x from 1.3.0 (checked at commit `70fb0807c9f6d448314988271873f18c1e59e617`), 6.5.x from 6.5.1 / Audio 6.4.1, and 6.6.x from 6.6.0 / Audio 6.5.0. Other version families, including 6.7+ and 7.x, require contract review before support. When a supported 6.5/6.6 release installs in a new directory, explicitly pass that root on resume; the adapter accepts the same or a newer version within the local-transcription contract and continues the saved run. A downgrade, missing prior version or cross-contract replacement requires restoring the original dependency. Existing confirmation, upload uncertainty and operation receipts remain authoritative.
3. The adapter invokes public commands with caller `aihub-audio`, original cwd and global configuration choice. Windows uses the dependency's `scripts/aihub.ps1`, macOS/Linux its Node entry. For 6.5/6.6, `doctor` → `plan` → `run` passes the complete local MP3 path directly in `inputs.audios`; AIhub owns snapshots, any required segmentation/upload and its model-specific limits. The 1.x contract retains `doctor` → `upload` → `plan` → `run`. An existing 1.x upload intent/receipt remains authoritative even when loading 6.5/6.6: an unknown upload cannot be bypassed by starting a local-input run. Credentials, model lists and internal fallback policy remain owned by AIhub Audio. This adapter does not call AIhub HTTP endpoints.
4. `continue_aihub_audio`: use this task's `resume`; it continues the saved run. `confirm_aihub_audio`: load the dependency and explain its saved exact model/parameters before seeking confirmation. Only that explicit decision authorizes its `continue --confirm`; never run an unreviewed `next_action` containing a token. Then resume video-to-markdown.
5. `review_aihub_audio`: follow the dependency's result-check flow using actual media evidence and its `review`/`review-submit`. Do not substitute a text summary, a mock report or an automatic disable. After completing the check, resume this task. A mismatched/unavailable check is preserved for user action; it does not cause regeneration or a provider switch. Relay the returned `user_notice` and any dependency feedback from the private receipt without exposing signed URLs.
6. For 6.5/6.6, a saved run's `persistence_failed` uses `continue_aihub_audio`: repair storage and resume the same Video task. Its dependency `continue` adopts a known fragment task ID before advancing and keeps the original run/result check. For the 1.x contract, `recover_aihub_known_task` instead returns a public `task --task-id` command; preserve the receipt and query only that known task after repairing storage. Joining a separately recovered child back into the original 1.3 run/result-check remains unverified. Never edit dependency records to bypass identity/confirmation checks or create a replacement run.

### Delivered transcription with a new review credential

Studio 6.6.0 / Audio 6.5.0 adds explicit `review-recover` for this case. Load that installed Audio Skill and its `references/audio-transcription.md` and `references/result-checks.md` before proceeding. In the original business cwd, use the normal configuration loader and the user's explicit choice of review credential. Run the dependency's `review-recover --skill aihub-audio --record <original-run.json> --reason <actual-authorization-reason>`; only `review_recovery_ready` authorizes local review recovery. This offline authorization preserves the ASR credential, run/plan, source and output hashes. It does not grant remote `task/resume` access or permit ASR confirmation/submission.

Only when the user explicitly requests a new check and the previous check is definitively finished, use dependency `review --skill aihub-audio --record <original-run.json> --provider aihub --recheck`. This may upload the source and incur review costs; it archives the old check. For `checking`, omit `--recheck` and query the same check. Pending/unknown submissions, unfinished checks and changed/missing files must be resolved through the dependency's protocol before changing credentials. Do not edit private state to bypass a refusal.

Then use Video `resume --task-dir <original-task> --aihub-plugin-dir <actual-6.6-plugin-root>`. A verified delivered result still requires `matched` or the user's explicit check-disable choice before adoption. `mismatched`, `inconclusive` and `unavailable` remain `review_aihub_audio`; neither recovery authorization nor a completed review makes them successful transcription evidence. Video never automatically issues `review-recover` or `--recheck`.

### Saved records and transcript

Current runs use timestamp/title directories; legacy runs use `aihub-run-*`. A lost wrapper response is recovered only from an unambiguous, identity-checked `run.json` inside this task's dependency root. An absent or ambiguous record preserves submission uncertainty. `upload_unknown` also remains uncertain. Preprocessing/registry/merge errors and stopped modern plans preserve the original run and provider, including any completed fragments; repair and continue it rather than submitting through another provider. Doctor failures retain the private `dependency_receipt`, including the original tool error; `EPERM` does not by itself establish missing FFmpeg.

Complete saved JSON artifacts are normalized, including all result files. For 6.5/6.6 Paraformer, the delivered `transcript.json` already contains global segment/word times in seconds; do not add fragment offsets again. Its accompanying TXT remains a raw artifact, not a second transcript. Preserve fragment-local speaker IDs and available word boundaries. Missing/invalid timing, differing full text or `text_alignment=unverified` stops adoption without discarding the complete raw result. The dependency's top-level `text` can contain only the first result and must not be used as the transcript. Keep raw artifacts and dependency records under `.state/`.

Compatible Video patch releases resume existing schema-4 tasks through the same `resume --task-dir` entry. They preserve the task UUID and manifest `skill_version` recorded at creation; there is no public command that upgrades that version field. A caller that freezes the converter's executable version must handle its own explicit compatible-version binding through its supported interface. Do not hand-edit the converter manifest, frozen caller state or private dependency records to make a version check pass.

## Feishu Minutes

The checked contract is official Lark CLI 1.0.98, commit `a92db8779c0959bc14beb69d82fb262123cfb79b`. The adapter requires `>=1.0.98,<2`, records the detected version and uses `--profile <name>` on every call. It never switches the global active profile.

The atomic identity group is:

- `VIDEO_TO_MARKDOWN_FEISHU_PROFILE`
- `VIDEO_TO_MARKDOWN_FEISHU_APP_ID`
- `VIDEO_TO_MARKDOWN_FEISHU_USER_OPEN_ID`

Never fill a missing identity field from a different configuration layer. The app/user fixed values are captured locally, not copied from user-entered IDs. `select_feishu_profile`: have the user choose a local profile, then resume with `--feishu-profile <name>`. `login_feishu`: let the user authorize login for that profile through the installed Lark CLI's actual login flow; do not automate their consent. Resume after login. Local validity is not proof of server authorization.

`confirm_feishu_identity` returns captured identity, a token and a proposed save target. Show the user these facts. After confirmation, resume with `--confirm-feishu-identity <token>`. Add `--save-feishu-identity` only if the displayed persistence target was explicitly authorized; otherwise the binding is retained just for this task. The identity and save scope are rechecked at confirmation. Full task identity is fixed once a remote operation has been attempted; restore the original identity for recovery.

`identity_save.effective=false` means task binding succeeded but persistent configuration did not. Report this separately. First persistent save defaults to the personal global file and requires `--use-local-key` to take effect. Existing identity configuration is repaired in its actual source. An environment override cannot be fixed by writing another file.

The execution is local identity checks → user cloud-drive upload → Minutes creation → detail query → timestamped SRT export. The user needs drive upload and Minutes upload/read/export permissions, including `minutes:minutes.upload:write`. The audio and resulting cloud objects remain in the user's account; the Skill does not delete them. Minute creation and uploads are non-idempotent; there is no automatic replay of uncertain writes.

## Actions and choices

| Result | Agent action |
|---|---|
| `feishu_quota_choice` | Explain exhausted quota. Offer `--choice aihub-once`, `--choice aihub-default`, or `--choice pause`. The default-save option is a separate explicit user decision. |
| `default_save.effective=false` | Explain that this task switched but the persistent default did not take effect; report `reason` and target. Never claim a successful default change. |
| Login/profile/identity or known permission failure | Show the returned differences/candidates, guide repair. A known rejected write can be retried with `--retry-provider` after the fix. This flag never repeats an uncertain write. |
| `wait_feishu` / `retry_feishu_query` | Resume to query the same minute. No new upload or creation. |
| `retry_feishu_export` | Repair download/storage and resume to export the same minute. |
| `submission_unknown` | Stop submissions and provider switches; inspect private receipts and known remote IDs. Do not delete the intent journal to force a retry. A separate `paused: true` preserves a user's pause even while this unresolved action remains visible. |
| `paused` | Preserve task. `--choice continue` resumes; a safe advertised `aihub-once` choice can leave a cancelled login flow. |
| `both_providers_failed` | Report both reasons and pause. Never loop back to an already failed provider. |
| `repair_transcript_timing` | Keep original results; explain incomplete/invalid timing. Do not invent word or character times or mark full evidence complete. |

Known AIhub dependency/configuration failures before a run, or definite unsuccessful legacy transcription, automatically switch only this task to Feishu once. This does not change the persistent default. Saved modern plans, processing, result checks, confirmation, cancellation, query/download/save errors and uncertain writes preserve the original operation. Local source/audio corruption is repaired before provider selection.

After the user explicitly leaves Feishu for AIhub, an AIhub failure preserves that choice instead of automatically returning to Feishu. If Feishu had already failed, report both failures; otherwise report the AIhub repair action without mislabeling the cancelled Feishu flow as a service failure.

## Limits, time and privacy

The first audio track is encoded in full as mono MP3, 24 kHz, 64 kbit/s. Its original stream index, source/video offset, encoded duration/format, hash and size are recorded. Video does not split or truncate it, run a local ASR model or use an alternative AIhub client. The legacy 1.x upload is capped at 20 MiB; 6.5/6.6 receives the local path and applies its own model/transport limits and bounded preprocessing. Feishu allows at most 6 hours and 6 GB; FLAC is not used. Actual provider/model constraints can be stricter.

Feishu SRT supplies segment times, not proven character boundaries. AIhub word timing is retained when supplied and each boundary is checked; other results remain segment/missing/mixed. Display precision is not recognition accuracy. Missing, reversed or out-of-video times stop adoption. Preserve the unmodified raw files so an alignment issue can be investigated.

Read requests have a shared 60-second invocation budget and at most three attempts; transient classification uses Lark's typed `retryable`/`rate_limit` contract, with bounded exponential backoff or `retry_after_seconds` (capped at 60 seconds per workspace policy). Cooldowns and retry metadata persist. Writes execute once per saved intent; AIhub owns its own internal retries. Each dependency process has a hard timeout, including its children on Windows.

Only public paths/status/timing evidence enter the document. Raw responses, signed URLs, dependency run files and confirmation evidence are private task data. Share the final Markdown and referenced assets, not `.state/`.
