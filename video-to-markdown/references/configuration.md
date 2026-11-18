# Configuration and local page

## 配置页面快捷入口

纯页面请求直接执行下方 `config-ui start`，传真实项目和当前宿主；保留已提供的 capability evidence、全局启用选择与运行目录。本次禁用全局时省略 `--use-local-key`；页面入口不接受业务命令的 `--no-global-config`。没有本次能力证据时可省略 `--capabilities`，不为打开页面先运行看图或转写探测。页面自身负责展示已知能力与缺项，业务 Key、视频和转写身份不作为启动前提。

入口已实现探活、同归属复用和失效重启。成功后交付回执 URL 和实际剩余空闲期限并结束；启动未确定才用相同上下文 `status`，不循环重启。仅打开页面不进入视频转换，也不因保存自动创建任务。

Read this when opening, saving or repairing configuration. The same dotenv loader drives the CLI, preview and saved readback. UI choices are not evidence of successful authentication or image capability.

## Sources and saved choices

Read process environment, invocation directory `.env.video-to-markdown`, `.env.local`, `.env`, then `~/.config/video-to-markdown/.env` only with `--use-local-key`. `--no-global-config` overrides that enablement. Never search parent directories or read a disabled global layer. Task overrides do not authorize persistent saves. For a new persistent configuration the established default scope is personal global; the approved page also offers an explicit project target. Explain the actual effective source and save only the user's chosen target.

`VIDEO_TO_MARKDOWN_VISION` is one JSON dotenv value. Its connection group never borrows Base URL, Key or model from another layer:

```dotenv
VIDEO_TO_MARKDOWN_VISION='{"ask_each_task":false,"routes":{"codex":{"method":"main-agent"}},"call_limit":null}'
```

`routes` can contain `codex`, `claude-code`, `workbuddy`, and `any`. Host-specific entries take precedence within the chosen group; native `host-agent` cannot use `any`. Four `method` values and remote fields are described in [vision-routing.md](vision-routing.md). Optional capacity fields are `max_images`, `max_request_bytes`, `max_context_chars`. `ask_each_task: true` retains suggestions but asks every new task. The whole group resolves from one source; project `ask_each_task` therefore overrides a global fixed route.

Task state saves only the safe route and credential source reference. A task-only remote route file must remain private and available for resume. Repair its Key in the same source; changed public connection/model fields require `vision-switch`. Persistent config changes affect new tasks, not existing task selections.

`VIDEO_TO_MARKDOWN_ASR_PROVIDER` and Feishu identity retain the transcription rules. AIhub ASR credentials stay with AIhub Audio. The vision connection is independent of ASR.

### 独立 Agent 启动与恢复

下列变量按进程环境、调用目录的 `.env.video-to-markdown`、`.env.local`、`.env`、已启用的个人全局配置逐项读取。可以通过 `vision-options` 核对配置来源。

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `VIDEO_TO_MARKDOWN_CODEX_EXECUTABLE` | 自动选择 | 依次采用显式路径、经过核验的当前 Codex 会话路径或 PATH 中的 `codex`。 |
| `VIDEO_TO_MARKDOWN_REQUEST_MAX_RETRIES` | 5 | 明确未发送或明确限流拒绝后的重试次数，0–100。 |
| `VIDEO_TO_MARKDOWN_STREAM_MAX_RETRIES` | 5 | 首次断流后接续原响应的次数，0–100。 |
| `VIDEO_TO_MARKDOWN_RETRY_BACKOFF_BASE_MS` | 1000 | 指数退避的起始等待，单位毫秒。 |
| `VIDEO_TO_MARKDOWN_RETRY_BACKOFF_MAX_MS` | 16000 | 指数退避上限，单位毫秒。服务冷却时间可超过此值。 |
| `VIDEO_TO_MARKDOWN_RESPONSES_BACKGROUND` | `false` | 服务支持且用户允许保留响应时设为 `true`，启用 Responses 后台存储。 |

调度从任务原始调用目录读取这些设置，单次调度共用 600 秒预算。断流后通过已有响应 ID 和游标接续。服务无法检索原响应时，任务记录保留响应 ID 和冷却截止时间。

## Start, check, stop

Use the pinned runtime and the actual invocation project, not the Skill installation directory:

```text
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py config-ui start --project <absolute-project> --host <actual-host> [--capabilities <current-session-file>] [--use-local-key]
```

`status` and `stop` replace `start` with the same project, host, capability evidence, global flag and optional `--runtime-dir`. These inputs identify the instance. `start` reuses a matching live instance or opens a new one and returns the current authorized URL; `status` verifies its ownership/lifetime. Never revive a cached URL without a live check. An absent project/evidence or mismatched owner is an error, not permission to reuse another project. The package includes its fixed UI runtime; users do not install the development UI Skill.

The page starts with three explicit strategies: inherit, ask for every new task, or fix one method. Fixed mode exposes all four methods and their connection fields; ask mode keeps optional suggestions in a disclosure and never presents them as an automatic route. Low-frequency ASR/output/source controls are collapsed. Save performs candidate validation, atomic write and readback. Saving a connection does not call a model. Tracked credential targets are refused; new private writes prepare narrow Git ignores.

Choose the file above the vision form. Inherit shows the actual fallback summary and its source; saving it removes this file's entire vision group. Switching to inherit parks independent edits instead of deleting them, and only submitted edits are consumed after a successful save. Independent groups never copy another layer's connection. The adapter also preserves complete same-file `routes.any` connections when editing the host-specific route. The saved/effective banner and candidate preview show a lower-priority save that remains overridden. Scope navigation retains separate drafts, and the save button applies only the current file. Returning to the baseline clears the corresponding modification.

An empty Key input is a placeholder or a cancelled replacement. Only the separate delete action submits `unset`; a fixed remote connection without its Key fails validation, while an incomplete ask-mode suggestion can be saved. Restore inheritance, delete Key and discard draft are separate actions. Unavailable routes are disabled by default; explicit future-use intent allows preconfiguration without claiming current execution capability. Unknown capability is not treated as unavailable. UI CLI capability evidence is reused for up to 30 seconds when runtime configuration is unchanged; changed runtime settings invalidate it, and task execution retains its live checks.

External revisions preserve drafts and require reload/review before submission. A save request carries a random session-local `request_id`, which is never written to dotenv. If the response is lost, disable applying changes and read state first: an exact receipt plus matching current revision confirms the submitted file, consuming only that draft. An absent/mismatched receipt retains the draft for explicit review. This receipt is not a durable transaction or permission to retry automatically. Reload also hides revealed stored secrets. Stop/expiry retain visible input for copying and disable editing, scope/route changes, deletion, reveal and save; reopen through the public entry.

Listen only on loopback with a random port and private fragment-token authentication, CSRF/Origin checks and an activity-based 60-minute idle expiry. Runtime records live under the actual user's `.cache/video-to-markdown/ui/` (or the explicitly selected runtime directory). Authorizing global config requires reopening with `--use-local-key`; a disabled layer remains uninspected and read-only.

## Delivering a page link

Return the verified live URL and actual remaining idle expiry. On first delivery, fill one reusable request with the actual absolute project/Worktree path: “请打开项目【实际路径】中 video-to-markdown 的配置页，给我新的访问链接。” Add only: “手机访问：告诉 Agent‘我要用手机打开这个页面。’” Repeated delivery of the same page in this conversation needs only the URL and actual expiry.

Configure and verify cross-device access only when requested. For testing, stop synthetic pages rather than delivering them as real user configuration.

If an authorized video task is waiting, check that the saved scope/project applies and resume that same task. After a finished session, supply a concrete continuation request with its task path. A save receipt alone is not a completed conversion.
