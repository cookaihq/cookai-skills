---
name: banana-2
description: Use when the user asks to generate or edit an image with Nano Banana 2 / Banana 2 / nano banana / 香蕉 / gemini flash image / gemini-3.1-flash-image-preview, or when they size an image by ASPECT RATIO (16:9, 9:16, 21:9, match_input_image) or by QUALITY TIER (512 / 0.5K / 1K / 2K / 4K), or want web-grounded ("联网搜索") / image-search-assisted generation. For generic "生成图片" with pixel resolutions like 1024x1024 use the image-2 skill instead. Do NOT use for video generation, OCR, or non-generative editing (crop, compress, watermark).
---

# banana-2

## Overview

通过 [aihubmax.com](https://docs.aihubmax.com/pages/zh/api-manual/image-series/nanobanana/gemini-3.1-flash-image-preview) 的 **Nano Banana 2**（模型 `gemini-3.1-flash-image-preview`）接口创建异步图片生成任务，创建成功后必须继续查询直到任务终态（`completed` 或 `failed`）。鉴权采用 `Authorization: Bearer <key>`。

支持文生图、图生图、图像编辑。尺寸由 **`aspect_ratio`（宽高比）+ `resolution`（画质档，非像素）** 两个字段共同决定。

完整字段说明、错误码、响应结构见 [references/api-guide.md](references/api-guide.md)。

## When to Use

- 用户明确点名 Nano Banana 2 / Banana 2 / nano banana / 香蕉 / gemini flash image / `gemini-3.1-flash-image-preview`
- 用户用**宽高比**描述尺寸（`16:9`、`9:16`、`21:9`、`match_input_image` 等），而非具体像素
- 用户用**画质档**描述清晰度（`512`、`0.5K`、`1K`、`2K`、`4K`）
- 用户想要**联网搜索实时信息**辅助生成（`google_search`）或**图像搜索**辅助生成（`image_search`，本模型独有）
- 用户提供参考图，希望基于参考图编辑/重绘（图生图，传 `image_urls`）

## When NOT to Use

- 泛化的「生成图片 / 海报图 / 封面图」且用**像素分辨率**（如 `1024x1024`、`1920x1080`）描述 → 用 **image-2** skill
- 视频生成、视频处理、音频处理
- OCR、文档解析、表格识别
- 非生成式编辑（裁剪、压缩、加水印、加边框）

## CRITICAL

- 调用前必须完成参数校验，缺必填先补齐再调用 API
- 不得回显完整 `Authorization` token，日志只允许 `head4****tail4` 掩码
- **同参数任务在同一轮对话内禁止二次提交**（参数任意变化即视为新任务，可创建）
- 禁止自动循环重试创建接口；重试需先告知会再次消耗积分并取得用户同意。**唯一例外**：HTTP 401 触发的 key 链 fallback——它换的是 key 不是同请求，且 401 不消耗积分，详见下方「401 自动 fallback」
- 创建接口返回的是异步任务，必须接着轮询查询接口直到终态
- 生成的图片 URL 24 小时后失效，需要长期保留请下载或转存
- **默认会把图片下载到当前工作区根目录**，文件名 `{YYYYMMDD-HHMMSS}-{≤10字标签}.{ext}`；优先级：`--filename` > `--label` > 自动从 prompt 前 10 字提取。环境变量 `BANANA_2_OUTPUT_DIR` 或 `--output-dir` 可改保存目录
- 本模型每次只产 1 张图，没有 `num_outputs`；也没有 `quality`/`background`/`mask_url` 字段，不要臆造

## Auth & Key Handling

**读取优先级（从高到低）**：

1. 本轮对话显式提供的 key（通过 `AIHUB_API_KEY=...` 注入给脚本）
2. 环境变量 `AIHUB_API_KEY`
3. `$PWD/.env.local` 中的 `AIHUB_API_KEY=...`（**自动读取**，无需 flag）
4. `$PWD/.env` 中的 `AIHUB_API_KEY=...`（**自动读取**，无需 flag）
5. `~/.config/banana-2/.env`（仅 `--use-local-key` 时读）

环境变量名 `AIHUB_API_KEY` 与 image-2 一致（都是 aihubmax.com 的 key，可共用同一个）；值通过 `Authorization: Bearer <key>` 提交给 aihubmax.com。

**旧变量名 `X_API_KEY` 仍然可用**：上面每一层都先找 `AIHUB_API_KEY`，找不到再找 `X_API_KEY`；命中旧名时会打一行废弃提示。已有配置无需立即改动，新配置请一律用 `AIHUB_API_KEY`。

**401 自动 fallback**：

- 如果某一层的 key 调用 `POST /v1/images/generations` 返回 HTTP 401（`authentication_error`），脚本会自动尝试链条中的下一个 key。**401 不消耗积分**，所以这种 fallback 安全。
- 其他错误码（402 余额不足 / 422 参数非法 / 429 限流 / 5xx 服务错误）以及网络错误**不触发 fallback**，立即返回让用户决定。
- 同一个 key 值在多个来源重复出现时只会试一次（按值去重）。
- 一旦某层 key 通过 create 调用成功，后续轮询查询接口都用同一个 key。

**作用域约束**：

- 第 3、4 层只读 `$PWD` 下的文件，**不向上递归**，避免在 Agent 任意 cwd 下误读父项目的 key。
- 想用 `~/.config/banana-2/.env` 必须显式 `--use-local-key`，避免 Agent 在用户不知情时静默用持久化 key 扣费。
- key 在日志中始终掩码为 `head4****tail4`，完整值只出现在 `Authorization` header 内。

**首次配置 key（可选）**：

```bash
./scripts/set_key.sh                  # 交互式输入，存到 ~/.config/banana-2/.env
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
| `model` | 是 | 固定 `gemini-3.1-flash-image-preview`（脚本默认，唯一合法值） |
| `prompt` | 是 | 提示词；图像编辑时描述「对参考图做什么改变」 |
| `aspect_ratio` | 否 | 输出宽高比，默认 `1:1`，详见下方 |
| `resolution` | 否 | 输出画质档（**非像素**），默认 `1K`，详见下方 |
| `image_urls` | 否 | 参考图 URL 数组；传入即图生图/图像编辑模式 |
| `output_format` | 否 | `jpg`/`png`/`webp`，**非必须不要传** |
| `google_search` | 否 | 布尔，启用联网搜索获取实时信息辅助生成，**非必须不要传** |
| `image_search` | 否 | 布尔，启用图像搜索辅助生成（**本模型独有**），**非必须不要传** |

### aspect_ratio 取值（15 种）

`1:1` `1:4` `1:8` `2:3` `3:2` `3:4` `4:1` `4:3` `4:5` `5:4` `8:1` `9:16` `16:9` `21:9` `match_input_image`

- `match_input_image`：尽量沿用输入参考图的宽高比（仅在传了 `image_urls` 时有意义）。

### resolution 取值（5 档画质）

`512` `0.5K` `1K` `2K` `4K`

- `512` 与 `0.5K`：half-size 输出
- `1K`：约 1MP；`2K`：约 4MP；`4K`：约 16MP
- 这是**画质档位**，不是像素宽高；具体像素由 `aspect_ratio` × 档位共同决定

## Workflow

1. **收集与校验**：补齐 `prompt`；如果用户描述涉及"基于这几张图"/"参考这张图"/"把背景换成…"等编辑意图，需要 `image_urls`；按上表校验 `aspect_ratio`（15 值）、`resolution`（5 档）、`output_format`（如传）。
2. **预览摘要**：向用户输出请求摘要（模型、模式、aspect_ratio、resolution、是否有参考图、是否开 google_search/image_search），明确"将消耗积分"，**等待用户确认后再调用脚本**。
3. **调用脚本**：用 `scripts/create_task.sh` 提交。脚本本身不再二次确认，直接发起 create 并轮询。
4. **查询直到终态**：脚本会以默认 8 秒间隔轮询 `/v1/tasks/{id}?sync_upstream=true`，直到 `status=completed` 或 `failed`。
5. **返回结果 & 自动保存**：成功时输出 `results[].url`，并把图片下载到本地（详见下方 [Output & Save](#output--save)）。失败时输出 `error.code` / `error.message`，不下载。

## Output & Save

任务终态为 `completed` 后，脚本会自动下载 `results[].url` 中的图片到本地。**Agent 必须把这一步当成主流程**，不要只把 URL 抛给用户——24 小时后 URL 失效。

### 默认行为

- **保存目录**：`$PWD`（脚本被调用时的工作目录 = 当前工作区根目录）
- **文件名**：`{YYYYMMDD-HHMMSS}-{标签}.{ext}`
  - 时间戳取**脚本启动时刻**
  - 标签 = `prompt` 前 10 个 Unicode 字符，去除 `/\:*?"<>|` 等文件系统不安全字符，空白折叠为 `_`
  - **扩展名推断**（本模型 `results[]` 通常只有 `url`、无 `content_type`）：`--output-format`（最可靠，用户显式意图）→ 响应 `content_type`（偶有）→ URL 路径尾缀（png/jpg/jpeg/webp）→ 默认 `png`
- **文件冲突**：同名文件存在时追加 `-2`、`-3`...
- 本模型恒 1 张输出，不会出现 `-01/-02` 多图编号

### 覆盖优先级

| 项 | 覆盖来源（高 → 低） | 说明 |
|---|---|---|
| 保存目录 | `--output-dir DIR` → env `BANANA_2_OUTPUT_DIR` → `$PWD` | 目录不存在会自动 `mkdir -p` |
| 标签部分 | `--label TEXT` → prompt 前 10 字自动提取 | 仅影响默认文件名中的标签段，时间戳照常 |
| 完整文件名 | `--filename NAME` | 整段替换 `{时间戳}-{标签}` 部分（仍自动加扩展名） |
| 关闭下载 | `--no-save` | 跳过下载，只回显 URL |

### Agent 解析用户意图

| 用户话语 | Agent 应传递的参数 |
|---|---|
| 「生成一张……」（未提保存位置） | 不传 `--output-dir` 也不传 `--filename`，走默认 |
| 「16:9 横屏」 | `--aspect-ratio 16:9` |
| 「竖屏封面」 | `--aspect-ratio 9:16` |
| 「要 4K 高清」 | `--resolution 4K` |
| 「按参考图的比例」 | `--aspect-ratio match_input_image`（须有 `--image-url`） |
| 「把这张图的背景换成海滩」 | `--image-url <URL>` + 编辑型 prompt |
| 「需要参考实时/网络信息」 | `--google-search` |
| 「参考网上的图片风格」 | `--image-search` |
| 「保存到 `~/Desktop`」 | `--output-dir ~/Desktop` |
| 「文件叫 `banner`」 | `--filename banner` |
| 「不要保存到本地，只给我链接」 | `--no-save` |

```bash
# 文生图（默认 1:1 / 1K）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "A futuristic city skyline at dusk, cyberpunk style" \
  --aspect-ratio 16:9 --resolution 1K

# 图像编辑（传参考图 + match_input_image + 图像搜索辅助）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "Replace the background with a tropical beach" \
  --image-url 'https://example.com/photo.jpg' \
  --aspect-ratio match_input_image --resolution 2K \
  --image-search

# 多张参考图（重复 --image-url）
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "把这两张图融合成一张极简海报" \
  --image-url 'https://example.com/a.png' \
  --image-url 'https://example.com/b.png' \
  --aspect-ratio 4:5 --resolution 2K

# 联网搜索辅助 + 指定输出格式
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "今天的科技新闻头条做成信息图" \
  --aspect-ratio 3:4 --resolution 2K \
  --google-search --output-format webp

# 保存到指定目录 + 指定文件名
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "Logo 设计" --aspect-ratio 1:1 --resolution 1K \
  --output-dir ~/Desktop --filename brand-logo

# 只要 URL，不下载
AIHUB_API_KEY='sk-xxx' ./scripts/create_task.sh \
  --prompt "..." --aspect-ratio 1:1 --resolution 1K --no-save
```

## Error Handling

- 缺 `prompt`：提示用户补全提示词
- 缺 `image_urls` 但 prompt 暗示需要参考图（"基于这张图"/"把…换成…"）：先和用户确认是否提供参考图，再选择 text2img 或图像编辑
- `aspect_ratio` 非法：返回 15 种合法值清单，要求重选
- `resolution` 非法：返回 5 档合法值（`512`/`0.5K`/`1K`/`2K`/`4K`），要求重选
- `output_format` 非法：只支持 `jpg`/`png`/`webp`
- HTTP 401 `authentication_error`：key 无效/过期/权限不足，去 [aihubmax.com](https://aihubmax.com) 检查
- HTTP 402 `insufficient_quota`：余额不足，请用户充值后再试
- HTTP 422 `validation_error`：根据 `error.message` 调整参数
- HTTP 429 `rate_limit_error`：限流，**禁止自动重试**，告知用户稍后再试
- HTTP 5xx：服务异常，建议用户确认后由用户决定是否重试
- 轮询超时：明确告知任务可能仍在运行，给出 `task_id` 让用户后续手动查询

## Dedup Rule

同一轮对话内，若 `model + prompt + image_urls + aspect_ratio + resolution + output_format + google_search + image_search` 全部相同，禁止再次提交；任何一项变化都允许新建。

## Pre-Response Checklist

最终响应前自检：

- 是否完成必填与字段约束校验（aspect_ratio 15 值、resolution 5 档）
- 是否对 key 做了 `head4****tail4` 掩码
- 创建成功后是否输出了 `task_id`
- 是否轮询到 `completed` / `failed` 才结束
- 是否提示了图片 URL 24 小时过期
- 是否输出了本地保存路径（除非显式 `--no-save`）
- 用户在上下文里指定了保存位置/文件名时，是否被正确传给 `--output-dir` / `--filename`

## Directory

- `SKILL.md`
- `scripts/set_key.sh`、`scripts/create_task.sh`
- `references/api-guide.md`
- `tests/`（pressure 场景文档）
