# Transcription and recovery

The two providers share the same video, transcript, visual-observation and Markdown pipeline. A valid supplied SRT/VTT/timestamped text bypasses cloud transcription. A missing/invalid supplied file remains selected on resume. Never substitute ASR for it without the user's changed request.

## Provider selection

New tasks use `--provider`, then `VIDEO_TO_MARKDOWN_ASR_PROVIDER`, then `aihub-audio`. Resume uses the saved task choice. `--only-provider` preserves a user's prohibition against another platform. Tell the user which provider will receive the extracted audio, including automatic fallback.

Use the same business working directory throughout. Project configuration is read from that directory, not from the Skill or output folder. `--use-local-key` explicitly enables the independent Skill's personal global file; this choice survives resume. `--no-global-config` disables global business reads for both the Skill and AIhub dependency throughout the task.

## AIhub Audio

1. `load_aihub_audio`: discover and load the installed `aihub-audio` using this host's actual full identifier and verified source. Claude Code, Codex and WorkBuddy do not share an invented Skill-call API. The adapter never searches another checkout for credentials or silently reinstalls a dependency.
2. Resume with `--aihub-plugin-dir <actual-aihub-studio-root> --request <user-original-request>`. The request is saved privately. If the host has verified that the dependency is unavailable, use `--aihub-unavailable`; omitting the path alone does not declare it missing. The supported interface is AIhub Studio 1.x from 1.3.0; the contract was checked at 1.3.0, commit `70fb0807c9f6d448314988271873f18c1e59e617`.
3. The adapter invokes the public `doctor`, `upload`, `plan`, `run` and `continue` commands with caller `aihub-audio`, original cwd and global configuration choice. Windows uses the dependency's `scripts/aihub.ps1`, macOS/Linux its Node entry. Credentials, model lists and internal fallback policy remain owned by that Skill. This adapter does not call AIhub HTTP endpoints.
4. `continue_aihub_audio`: use this task's `resume`; it continues the saved run. `confirm_aihub_audio`: load the dependency and explain its saved exact model/parameters before seeking confirmation. Only that explicit decision authorizes its `continue --confirm`; never run an unreviewed `next_action` containing a token. Then resume video-to-markdown.
5. `review_aihub_audio`: follow the dependency's result-check flow using actual media evidence and its `review`/`review-submit`. Do not substitute a text summary, a mock report or an automatic disable. After completing the check, resume this task. A mismatched/unavailable check is preserved for user action; it does not cause regeneration or a provider switch. Relay the returned `user_notice` and any dependency feedback from the private receipt without exposing signed URLs.
6. `recover_aihub_known_task`: the task ID is known but a dependency record could not be persisted. Preserve the receipt and use the displayed public `task --task-id` command after repairing storage. It only queries the known task. Do not blindly continue the stale run or create another run. Joining a separately recovered child record back into the original run/result-check is not verified in dependency 1.3.0: report this recovery limitation rather than editing dependency records to bypass identity/confirmation checks.

Complete saved JSON artifacts are normalized, including all result files. The dependency's top-level `text` can contain only the first result and must not be used as the transcript. Keep raw artifacts and dependency records under `.state/`.

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

Known AIhub dependency/configuration failures or definite unsuccessful transcription automatically switch only this task to Feishu once. This does not change the persistent default. Processing, result checks, confirmation, cancellation, query/download/save errors and uncertain writes preserve the original operation. Local source/audio corruption is repaired before provider selection.

After the user explicitly leaves Feishu for AIhub, an AIhub failure preserves that choice instead of automatically returning to Feishu. If Feishu had already failed, report both failures; otherwise report the AIhub repair action without mislabeling the cancelled Feishu flow as a service failure.

## Limits, time and privacy

The first audio track is encoded in full as mono MP3, 24 kHz, 64 kbit/s. Its original stream index, source/video offset, encoded duration/format, hash and size are recorded. No automatic splitting, truncation, local ASR model or alternative direct AIhub client is used. AIhub local upload is capped at 20 MiB; exceeding it can route to Feishu. Feishu allows at most 6 hours and 6 GB; FLAC is not used. Actual provider/model constraints can be stricter.

Feishu SRT supplies segment times, not proven character boundaries. AIhub word timing is retained when supplied and each boundary is checked; other results remain segment/missing/mixed. Display precision is not recognition accuracy. Missing, reversed or out-of-video times stop adoption. Preserve the unmodified raw files so an alignment issue can be investigated.

Read requests have a shared 60-second invocation budget and at most three attempts; transient classification uses Lark's typed `retryable`/`rate_limit` contract, with bounded exponential backoff or `retry_after_seconds` (capped at 60 seconds per workspace policy). Cooldowns and retry metadata persist. Writes execute once per saved intent; AIhub owns its own internal retries. Each dependency process has a hard timeout, including its children on Windows.

Only public paths/status/timing evidence enter the document. Raw responses, signed URLs, dependency run files and confirmation evidence are private task data. Share the final Markdown and referenced assets, not `.state/`.
