---
name: image-2
version: 1.2.0
description: v1.2.0｜Use when the user asks to generate, render, or recreate an image — phrases like "生成图片"、"图生图"、"海报图"、"封面图"，or when they specify an output resolution such as 1024x1024 / 1920x1080 / 1080x1920. Do NOT use for video generation, OCR, or non-generative image editing (crop, compress, watermark).
---

# image-2

## Overview

通过 [aihubmax.com](https://docs.aihubmax.com/pages/zh/api-manual/image-series/gpt-image-2/gpt-image-2) 的 `gpt-image-2` 接口创建异步图片生成任务，创建成功后必须继续查询直到任务终态（`completed` 或 `failed`）。鉴权采用 `Authorization: Bearer <key>`。

完整字段说明、错误码、响应结构见 [references/api-guide.md](references/api-guide.md)。

## When to Use

- 用户想用 prompt 生成图片（文生图）
- 用户提供参考图，希望基于参考图生成新图（图生图，传 `image_urls`）
- 用户指定输出像素分辨率（如 `1024x1024`、`1920x1080`、`1080x1920`）

## When NOT to Use

- 视频生成、视频处理、音频处理
- OCR、文档解析、表格识别
- 非生成式编辑（裁剪、压缩、加水印、加边框）

## CRITICAL

- 调用前必须完成参数校验，缺必填先补齐再调用 API
- 不得回显完整 `Authorization` token，日志只允许 `head4****tail4` 掩码
- **同参数任务在同一轮对话内禁止二次提交**（参数任意变化即视为新任务，可创建）
- 禁止 Agent 自行重跑创建接口；重跑需先告知会再次消耗积分并取得用户同意。脚本内部只在**确认未受理**的两种情况下自动重试同一个创建请求（都不会重复扣费，ADR 0006 规则 4）：HTTP 429（服务端明确限流、本次未受理，循 `Retry-After`）、连接阶段失败（DNS 解析失败 / 连接被拒，请求根本没发出去）。**HTTP 5xx 与「请求已发出但响应丢失」都不重试**：这两种情况下服务端都收到了请求，任务是否已创建并计费无法从本地判定，一律落 `create_transport_error` 并在 message 里写明「任务可能已创建也可能没有」，由用户到控制台确认后再决定。另一个不消耗积分的例外仍在：HTTP 401 触发的 key 链 fallback——它换的是 key 不是同请求，详见下方「401 自动 fallback」
- 创建接口返回的是异步任务，必须接着轮询查询接口直到终态
- 生成的图片 URL 24 小时后失效，需要长期保留请下载或转存
- **默认会把图片下载到当前工作区根目录**，文件名 `{YYYYMMDD-HHMMSS}-{≤10字标签}.{ext}`；优先级：`--filename` > `--label` > 自动从 prompt 前 10 字提取。环境变量 `IMAGE_2_OUTPUT_DIR` 或 `--output-dir` 可改保存目录
- 项目存在 `image-2 -> Upload Target` 映射时也不得自动上传；只有用户明确要求持久 URL/Object Reference 才进入独立的 `s3-upload` 第二阶段

## Auth & Key Handling

**读取优先级（从高到低）**：

1. 本轮对话显式提供的 key（通过 `AIHUB_API_KEY=...` 注入给脚本）
2. 环境变量 `AIHUB_API_KEY`
3. `$PWD/.env.local` 中的 `AIHUB_API_KEY=...`（**自动读取**，无需 flag）
4. `$PWD/.env` 中的 `AIHUB_API_KEY=...`（**自动读取**，无需 flag）
5. `~/.config/image-2/.env`（仅 `--use-local-key` 时读）

值通过 `Authorization: Bearer <key>` 提交给 aihubmax.com。

**旧变量名 `X_API_KEY` 仍然可用**：上面每一层都先找 `AIHUB_API_KEY`，找不到再找 `X_API_KEY`；命中旧名时会在日志打一行废弃提示。已有配置无需立即改动，但新配置请一律用 `AIHUB_API_KEY`。

**401 自动 fallback**：

- 如果某一层的 key 调用 `POST /v1/images/generations` 返回 HTTP 401（`authentication_error`），脚本会自动尝试链条中的下一个 key。**401 不消耗积分**，所以这种 fallback 安全。
- 其他错误码（402 余额不足 / 422 参数非法 / 429 限流 / 5xx 服务错误）以及网络错误**不触发 fallback**（换 key 解决不了这些问题）；创建接口上只有 429 与「请求确定未发出」这两类会先按 ADR 0006 用同一个 key 重试 3 次（指数退避 1s、2s，429 循 `Retry-After`），仍失败才返回让用户决定。轮询与下载是幂等 GET，5xx 在那里照常重试。
- 同一个 key 值在多个来源重复出现时只会试一次（按值去重）。
- 一旦某层 key 通过 create 调用成功，后续轮询查询接口都用同一个 key。

**作用域约束**：

- 第 3、4 层只读 `$PWD` 下的文件，**不向上递归**，避免在 Agent 任意 cwd 下误读父项目的 key。
- 想用 `~/.config/image-2/.env` 必须显式 `--use-local-key`，避免 Agent 在用户不知情时静默用持久化 key 扣费。
- key 在日志中始终掩码为 `head4****tail4`，完整值只出现在 `Authorization` header 内。

**首次配置 key（可选）**：

```bash
./scripts/set_key.sh                  # 交互式输入，存到 ~/.config/image-2/.env
echo 'sk-xxx' | ./scripts/set_key.sh --stdin
```

**会话级使用（推荐）**：

```bash
export AIHUB_API_KEY='sk-xxx'
```

**项目级使用**：在项目根目录建 `.env` 或 `.env.local`：

```ini
# .env.local（不入版本控制）
AIHUB_API_KEY=sk-xxx
```

`.env` / `.env.local` 解析规则（极简，不等同于 shell）：

- 支持：`KEY=value` / `KEY="value"` / `KEY='value'`、等号两侧空白、`#` 起首的注释行、空行
- 文件中 `AIHUB_API_KEY` 出现多次时取**最后一次**
- **不支持** shell 展开（`${OTHER}` / `$OTHER`）、命令替换（`$(...)` / 反引号）、续行符 `\` ——这些都会被当作字面字符串
- 只识别变量名 `AIHUB_API_KEY`，不识别 `OPENAI_API_KEY` / `AIHUBMAX_API_KEY` 等其他命名（旧名 `X_API_KEY` 仍兼容）

## Required & Optional Parameters

| 参数 | 必填 | 说明 |
|------|------|------|
| `model` | 是 | `gpt-image-2`（完整版，默认）或 `gpt-image-2-limit`（精简版） |
| `prompt` | 是 | 提示词；图生图时描述「在参考图基础上做什么改变」 |
| `image_urls` | 否 | 参考图 URL 数组；传入即图生图模式 |
| `resolution` | 否 | 预设字符串或 `{width, height}` 对象，默认 `1024x1024`，详见下方 |
| `num_outputs` | 否 | 完整版 1-10，精简版只能 1，默认 1 |
| `quality` | 否 | `low/medium/high`，默认 `high`，**精简版不支持** |
| `output_format` | 否 | `png/jpeg/webp`，默认 `png`，**精简版不支持** |
| `background` | 否 | `auto/opaque`，与 `mask_url` 互斥，**精简版不支持** |
| `mask_url` | 否 | 遮罩图（白色区域可编辑），与 `background` 互斥，**精简版不支持** |

### Resolution 取值

**完整版预设**（11 种）：`1024x768` `768x1024` `1024x1024` `1536x1024` `1024x1536` `1920x1080` `1080x1920` `2560x1440` `1440x2560` `3840x2160` `2160x3840`

**精简版预设**（3 种）：`1024x1024` `1024x1536` `1536x1024`

**完整版自定义**（仅完整版）：`{"width": W, "height": H}`，W/H 均为 16 的倍数，256≤W,H≤3840，长短边比 ≤ 3:1，总像素 655,360..8,294,400。

## 第 0 步：检查更新（每次运行都做）

先在本 skill 目录下运行：

```bash
scripts/check_update.sh
```

- 退出码 `0`：直接进入下一步，**不要**向用户复述脚本输出。
- 退出码 `10`：把脚本打印的报告**原样转述给用户**，并询问是否现在拉取。
  - 用户同意 → 运行 `scripts/check_update.sh --pull`，成功后按新版本继续；失败时把脚本给出的拒绝原因转述给用户，然后**按当前版本继续本次任务**，不要卡在更新上。
  - 用户拒绝或不理会 → 按当前版本继续，本次任务内不再提更新。
- 用户说「关掉自动检查更新」→ 往 `~/.config/image-2/.env` 写 `AUTO_UPDATE_CHECK=0`（该文件已存在则只改 / 追加这一行，不动其他行）。

**更新检查永远不是任务的阻塞项**：检查失败、拉取失败、用户不理会，一律落到「按当前版本继续干活」。

## Workflow

1. **收集与校验**：补齐 `prompt`；如果用户描述涉及"基于这几张图"/"参考这张图"等，需要 `image_urls`；按上表校验 `resolution`、`num_outputs`、精简版禁用字段。
2. **预览摘要**：向用户输出请求摘要（模型、模式、resolution、num_outputs、是否有参考图、掩码 key），明确"将消耗积分"，**等待用户确认后再调用脚本**。
3. **调用脚本**：用 `scripts/create_task.sh` 提交。脚本本身不再二次确认，直接发起 create 并轮询。
4. **查询直到终态**：脚本会以默认 8 秒间隔轮询 `/v1/tasks/{id}?sync_upstream=true`，直到 `status=completed` 或 `failed`。
5. **返回结果 & 自动保存**：成功时输出 `results[].url`，并把图片下载到本地（详见下方 [Output & Save](#output--save)）。失败时输出 `error.code` / `error.message`，不下载。

## Output & Save

任务终态为 `completed` 后，脚本会自动下载 `results[].url` 中的图片到本地。**Agent 必须把这一步当成主流程**，不要只把 URL 抛给用户——24 小时后 URL 失效。

### 默认行为

- **保存目录**：`$PWD`（脚本被调用时的工作目录 = 当前工作区根目录）
- **文件名**：`{YYYYMMDD-HHMMSS}-{标签}.{ext}`
  - 时间戳取**脚本启动时刻**，所有相关任务共享一致前缀
  - 标签 = `prompt` 前 10 个 Unicode 字符，去除 `/\:*?"<>|` 等文件系统不安全字符，空白折叠为 `_`
  - 扩展名按响应的 `content_type` 推断：`image/png` → `png`、`image/jpeg` → `jpg`、`image/webp` → `webp`
- **多输出**（`num_outputs > 1`）：自动加 `-01`、`-02` 后缀
- **文件冲突**：同名文件存在时追加 `-2`、`-3`...

### 覆盖优先级

| 项 | 覆盖来源（高 → 低） | 说明 |
|---|---|---|
| 保存目录 | `--output-dir DIR` → env `IMAGE_2_OUTPUT_DIR` → `$PWD` | 目录不存在会自动 `mkdir -p` |
| 标签部分 | `--label TEXT` → prompt 前 10 字自动提取 | 仅影响默认文件名中的标签段，时间戳照常 |
| 完整文件名 | `--filename NAME` | 整段替换 `{时间戳}-{标签}` 部分（仍自动加扩展名 & 多输出索引） |
| 关闭下载 | `--no-save` | 跳过下载，只回显 URL |

### Agent 解析用户意图

| 用户话语 | Agent 应传递的参数 |
|---|---|
| 「生成一张……」（未提保存位置） | 不传 `--output-dir` 也不传 `--filename`，走默认 |
| 「保存到 `~/Desktop`」 | `--output-dir ~/Desktop` |
| 「文件叫 `banner`」 | `--filename banner` |
| 「叫 `banner.jpg`」 | `--filename banner`（扩展名由 content-type 决定，无需传） |
| 「设置环境变量 `IMAGE_2_OUTPUT_DIR=/foo` 后再跑」 | 由用户自己 `export`；脚本会自动读取，Agent 无需再传 |
| 「不要保存到本地，只给我链接」 | `--no-save` |



```bash
# 文生图（默认 1024x1024）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "生成科技风产品海报" \
  --resolution 1920x1080

# 图生图（多张参考图重复传 --image-url，最多按 aihubmax.com 实际上限）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "基于参考图重绘成极简海报" \
  --image-url 'https://example.com/a.png' \
  --image-url 'https://example.com/b.png' \
  --resolution 1024x1536

# 精简版（仅 3 种预设、num_outputs 必须 1）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --model gpt-image-2-limit \
  --prompt "极简产品图" \
  --resolution 1536x1024

# 自定义分辨率（仅完整版）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "宽屏 banner" \
  --resolution 2400x800

# 保存到指定目录 + 指定文件名（用户在上下文中明确要求时）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "Logo 设计" \
  --resolution 1024x1024 \
  --output-dir ~/Desktop \
  --filename brand-logo

# 用环境变量统一指定保存目录（适合反复生成场景）
IMAGE_2_OUTPUT_DIR=~/Pictures/image-2 \
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "宣传图" --resolution 1920x1080

# 只要 URL，不下载
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "..." --resolution 1024x1024 --no-save
```

## Persistent Upload Handoff

用户明确要求把结果持久保存到自己的对象存储时，Agent 使用结构化模式完成两个独立阶段。Mapping 只选择目的地，不构成上传授权。

1. 在原项目 cwd 调用 image-2，加入 `--json`，不要 `--no-save`。
2. 只读取 `status=saved` 且具有非空绝对 `local_path` 的 outputs；生成失败、下载失败或未保存项不进入上传。
3. 保持同一个原项目 cwd，逐项调用 `s3-upload upload --json --caller-skill image-2 --file <absolute-local-path>`。
4. 关联每个 image output 与对应上传结果。上传失败、partial 或 ambiguous 不重新生成图片、不删除本地文件，也不盲目重放对象写入。

```bash
# 阶段一：结构化生成与原子本地保存
./scripts/create_task.sh --json \
  --prompt "产品封面" --resolution 1024x1024

# 阶段二：仅在明确 Persistent Upload Request 后，由 Agent 对 saved output 调用
# （跨 skill 调用同样用 uv run --project 钉死对方 skill 的解释器，ADR 0007）
uv run --project /absolute/s3-upload /absolute/s3-upload/scripts/upload.py upload \
  --file /absolute/output/cover.png \
  --caller-skill image-2 \
  --json
```

如果用户显式选择了 Target，第二阶段可加 `--target project:name|global:name`；否则先读取项目环境和 `.s3-upload/config.json` 中的 `image-2` mapping/default。Global Target 的间接 mapping 仍需 `--use-local-key`。

## Error Handling

- 缺 `prompt`：提示用户补全提示词
- 缺 `image_urls` 但 prompt 暗示需要参考图：先和用户确认是否提供参考图，再选择 text2img 或 img2img
- `resolution` 非法：返回模型对应的预设清单，要求重选；若自定义需提示 16 的倍数/范围/总像素约束
- 精简版用了禁用字段（`quality`、`output_format`、`background`、`mask_url`、`num_outputs>1`）：明确告知精简版限制
- `mask_url` 与 `background` 同时传：二选一
- HTTP 401 `authentication_error`：key 无效/过期/权限不足，去 [aihubmax.com](https://aihubmax.com) 检查
- HTTP 402 `insufficient_quota`：余额不足，请用户充值后再试
- HTTP 422 `validation_error`：根据 `error.message` 调整参数
- HTTP 429 `rate_limit_error`：限流，脚本已按 `Retry-After` / 指数退避自动重试 3 次；仍报错说明限流持续，告知用户稍后再试，**Agent 不要立刻重跑**
- HTTP 5xx：**创建接口上不重试**（服务端已收到请求，任务是否已创建无法确认），直接落 `create_transport_error`；轮询与下载上会自动重试 3 次，仍报错则由用户确认后决定是否重跑
- `create_transport_error`：结果不明——请求可能已到达上游、任务可能已创建并计费。**禁止自动重发**；先让用户到 aihubmax 控制台确认任务列表，再决定是否重新生成。`[net]` 日志写明本次是「请求未发出」「响应丢失」还是「服务端以 5xx 作答」
- 轮询超时：明确告知任务可能仍在运行，给出 `task_id` 让用户后续手动查询

**网络抖动处理（ADR 0006）**：每次调用都有超时（API 60s、下载 60s）；瞬时错误自动重试 3 次 + 指数退避（1s、2s）并打 `[net]` 日志；确定性 4xx 立即报错不重试。可重试面按操作分：幂等 GET（轮询、下载）重试 429 / 5xx / 连接抖动；计费写（创建任务）只重试 429 与「请求确定未发出」。轮询除 `--max-attempts` 次数预算外还压一条墙钟预算（`--max-attempts` × `--poll-interval`，默认 720s），单次抖动只消耗**内层重试**，不击穿轮询总预算。

**`[net]` 日志去向**：`--json` 模式下所有进度日志（含 `[net]`）走 **stderr**，stdout 只留一份纯 JSON；非 `--json` 模式下进度日志走 **stdout**，只有结果不明这类错误行始终走 stderr。

## Dedup Rule

同一轮对话内，若 `model + prompt + image_urls + resolution + num_outputs + quality + mask_url + output_format + background` 全部相同，禁止再次提交；任何一项变化都允许新建。

## Pre-Response Checklist

最终响应前自检：

- 是否完成必填与字段约束校验
- 是否对 key 做了 `head4****tail4` 掩码
- 创建成功后是否输出了 `task_id`
- 是否轮询到 `completed` / `failed` 才结束
- 是否提示了图片 URL 24 小时过期
- 是否输出了本地保存路径（除非显式 `--no-save`）
- 用户在上下文里指定了保存位置/文件名时，是否被正确传给 `--output-dir` / `--filename`

## Directory

- `SKILL.md`
- `scripts/set_key.sh`、`scripts/create_task.sh`、`scripts/image_task.py`
- `references/api-guide.md`
- `tests/`（pressure 场景、handoff contract、runtime tests）
