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

## Start, check, stop

Use the pinned runtime and the actual invocation project, not the Skill installation directory:

```text
uv run --frozen --project <skill-dir> <skill-dir>/scripts/workflow.py config-ui start --project <absolute-project> --host <actual-host> [--capabilities <current-session-file>] [--use-local-key]
```

`status` and `stop` replace `start` with the same project, host, capability evidence, global flag and optional `--runtime-dir`. These inputs identify the instance. `start` reuses a matching live instance or opens a new one and returns the current authorized URL; `status` verifies its ownership/lifetime. Never revive a cached URL without a live check. An absent project/evidence or mismatched owner is an error, not permission to reuse another project. The package includes its fixed UI runtime; users do not install the development UI Skill.

The page shows all four methods, real capability/model evidence, effective source, project/global scopes, independent file drafts, and a per-new-task ask switch. Low-frequency ASR/output/source controls are collapsed. Save once performs candidate validation, atomic write and readback; there is no autosave or second confirmation. Refresh retains editing drafts for conflict repair while hiding revealed stored secrets. Saving a connection does not call a model. Tracked credential targets are refused; new private writes prepare narrow Git ignores.

Choose the save scope above the vision form. If that file has no vision group, the form shows the effective inherited method, model and ask switch as read-only, with the source identified. Clear “沿用其他来源的图像识别配置” to start an independent unsaved group; enter its complete connection in that file. Inherited connection values and keys are not copied into the new group. Checking it again previews removal through the real loader and writes only on Save. The form distinguishes inherited, saved and draft values; a lower-priority saved group may remain overridden by the source shown in the effective banner. Switching scope retains separate drafts and never saves them.

Listen only on loopback with a random port and private fragment-token authentication, CSRF/Origin checks and an activity-based 60-minute idle expiry. Runtime records live under the actual user's `.cache/video-to-markdown/ui/` (or the explicitly selected runtime directory). Authorizing global config requires reopening with `--use-local-key`; a disabled layer remains uninspected and read-only.

## Delivering a page link

Return the verified live URL and actual remaining idle expiry. On first delivery, fill one reusable request with the actual absolute project/Worktree path: “请打开项目【实际路径】中 video-to-markdown 的配置页，给我新的访问链接。” Add only: “手机访问：告诉 Agent‘我要用手机打开这个页面。’” Repeated delivery of the same page in this conversation needs only the URL and actual expiry.

Configure and verify cross-device access only when requested. For testing, stop synthetic pages rather than delivering them as real user configuration.

If an authorized video task is waiting, check that the saved scope/project applies and resume that same task. After a finished session, supply a concrete continuation request with its task path. A save receipt alone is not a completed conversion.
