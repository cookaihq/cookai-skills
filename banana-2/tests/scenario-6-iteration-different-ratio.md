# Scenario 6 — Iteration: a changed parameter allows a new task

**测试目标**：Dedup 只拦"完全相同"。任一参数变化（如换宽高比）即视为新任务，应正常创建，不要被上一条的 dedup 记忆误拦。

## Setup

env 已设有效 `AIHUB_API_KEY`。上一条已成功创建：

- prompt = "A futuristic city skyline at dusk, cyberpunk style"
- aspect_ratio = `16:9`，resolution = `1K`

## User Turn

> 同样的画面，给我出一张竖屏 9:16 的。

（prompt 不变，但 `aspect_ratio` 从 `16:9` 变成 `9:16`。）

## Expected (with skill)

- 识别 `aspect_ratio` 已变化 → 不属于 Dedup 命中，**允许新建**
- 输出请求摘要（aspect_ratio=9:16，resolution=1K），确认后调用 `--aspect-ratio 9:16`
- 正常轮询到终态并下载

## Anti-pattern to catch

- 误把它当成"刚才那张"的重复而拒绝创建
- 仍用 `16:9` 提交（没把变更落到参数上）
- 不输出摘要直接调用

## Red flags

- payload 里 `aspect_ratio` 仍是 `16:9`
- 因 dedup 误判而拒绝了一个本应允许的新任务
