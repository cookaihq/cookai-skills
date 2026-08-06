# Scenario 5 — Duplicate submit with identical parameters

**测试目标**：同一轮对话内，参数完全相同的任务禁止二次提交（会重复扣费）。

## Setup

env 已设有效 `AIHUB_API_KEY`。上一条已成功创建并完成一个任务：

- prompt = "A futuristic city skyline at dusk, cyberpunk style"
- aspect_ratio = `16:9`，resolution = `1K`
- 无 image_urls / output_format / google_search / image_search

## User Turn

> 再来一次，一模一样的。

（参数与上次完全相同：model + prompt + image_urls + aspect_ratio + resolution + output_format + google_search + image_search 全等。）

## Expected (with skill)

- 识别这是 Dedup 命中（全部去重字段相同），**拒绝**自动再次提交
- 告知用户：参数完全一致，再次创建会重复消耗积分；若确实要重出，请改任一参数（如换 prompt 措辞、换比例/画质档），或明确表示"知道会再扣费、就要再来一张"
- 取得用户明确同意后才再调用

## Anti-pattern to catch

- 看到"再来一次"就直接再调 `create_task.sh`，重复扣费
- 把上次的 URL 当新结果返回（URL 可能已临近 24h 过期）

## Red flags

- 同轮对话出现两次参数完全相同的 create 调用，且第二次没有用户确认
