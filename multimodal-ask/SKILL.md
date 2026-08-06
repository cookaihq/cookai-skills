---
name: multimodal-ask
description: Use when the user names a specific model and wants it to generate text or understand media — analyze/transcribe/summarize an AUDIO or VIDEO (modalities the agent can't process itself), have a NAMED model describe an image, read a PDF/document, or reason over MIXED media at once. Phrases like "用 gemini-3.5-flash 看这段视频"、"让 claude-opus-4-7 读这个 PDF"、"转写这段音频"、"用 X 模型分析这些图+视频". Drives aihubmax.com llm-custom (async). Do NOT use for IMAGE GENERATION (use image-2 / banana-2), OCR-only, or plain text the agent can answer itself without a named model.
---

# multimodal-ask

## Overview

通过 [aihubmax.com](https://docs.aihubmax.com/pages/zh/api-manual/text-series/llm-async/llm-custom) 的统一 `llm-custom` 异步端点（`POST /v1/llm/generations`），用**用户点名的模型**处理文本 / 图片 / 音频 / 视频 / 文档 / 混合媒体，返回模型的**文本**回答。鉴权 `Authorization: Bearer <key>`。

字段、错误码、轮询契约见 [references/api-guide.md](references/api-guide.md)。

## When to Use

- 用户**点名某模型**生成文本，或让它理解图片
- 用户给**音频 / 视频**要理解、转写、总结（Agent 自身无法处理的模态）
- 用户给**文档**（PDF/docx）要理解
- 用户**同时给多种媒体**要一并理解

## When NOT to Use

- **图片生成 / 文生图 / 图生图** → `image-2` / `banana-2`（本 skill 只做理解/分析与文本生成，不生成图片）
- 纯文本问题、没点名模型、Agent 自己能答 → 不必用
- 纯 OCR、非生成式处理

## CRITICAL

- 创建任务**消耗积分**：提交前向用户输出请求摘要（模型、媒体清单、max_tokens、本地媒体 >20MB 的软警告），**等用户确认再调脚本**；脚本本身不二次确认（注：>20MB 软警告——Agent 应在构造摘要前自行检查本地媒体大小，例如 `os.path.getsize`；脚本运行时也会把该警告输出到 stderr，但那发生在调用之后）
- **同参数同轮禁止二次提交**（dedup 维度按用户输入的媒体来源，不是上传后的 URL）
- **禁止自动重试创建**；重试需先告知会再扣分并取得同意。唯一例外：401 key 链 fallback（不消耗积分）
- 创建返回异步任务，必须轮询 `GET /v1/tasks/{id}?sync_upstream=true` 到终态（`completed`/`failed`）
- 不得回显完整 token，日志 `head4****tail4` 掩码
- **本地文件先经上传换 URL**（脚本内置）；上游只收 URL / data URI
- 大小软提醒（默认 20MB，可配 `MULTIMODAL_ASK_WARN_BYTES`）是**经验提醒、非 API 硬限制**；真实上限由 413 / `model_rule_violation` 反应式裁决

## Auth & Key Handling

与 `upload-for-url` 一致：`AIHUB_API_KEY` 分层读取（进程 env → `$PWD/.env.local` → `$PWD/.env` → `~/.config/multimodal-ask/.env` 仅 `--use-local-key`）；401 触发 key 链 fallback；key 一律掩码。每一层都先找 `AIHUB_API_KEY`，找不到再找旧名 `X_API_KEY`（仍兼容，命中会打废弃提示）。首次配置：`./scripts/set_key.sh`。

## Usage

```bash
# 纯文本，指定模型
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gpt-5.5 --prompt "用一句话解释相对论"

# 视频理解（本地文件，自动上传换 URL）
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gemini-3.5-flash --video ./clip.mp4 --prompt "这段视频讲了什么"

# 音频转写
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gemini-3.5-flash --audio ./talk.mp3 --prompt "转写并总结"

# YouTube（仅 Gemini 家族；Shorts 会自动改写为 watch?v=）
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gemini-3.5-flash --video 'https://youtu.be/abc123' --prompt "概述"

# 混合媒体（图 + 视频 + 文档）单次提问
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gemini-3.5-flash \
  --image ./a.png --video ./b.mp4 --file ./c.pdf --prompt "汇总这些素材的核心信息"

# 文档理解 + claude（claude 家族 max_tokens 必填，脚本会自动补 1024）
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model claude-opus-4-7 --file ./report.pdf --prompt "提炼要点"
```

**输出约定**：成功时 **stdout 打印模型的文本回答**，stderr 打印完成摘要（model / task_id / 掩码 key）+ 任何软警告。失败时 stderr 打印原因，退出码非 0。

## Error Handling

| 退出码 | 含义 |
|---|---|
| 0 | 成功，stdout 为模型文本 |
| 1 | 提交/轮询/上传失败、任务 failed、或网络/超时（stderr 有原因 + task_id） |
| 2 | 未找到 AIHUB_API_KEY，或未提供 --prompt 及任何媒体（缺必需输入） |
| 3 | 能力预校验失败（模型不可用或不支持所需媒体类型；stderr 列出可用/支持模型） |

HTTP/任务语义：401 鉴权（key fallback）｜422 `no_available_model`/`model_not_support_capability`/`model_rule_violation`/`invalid_param`｜429 限流（不自动重试）｜5xx 服务/上游异常｜任务 `failed` 回传 `error`。思考模型的 `reasoning_content` 不累积进结果，content 可能为空串（会如实说明）。

## Pre-Response Checklist

- 调用前是否输出请求摘要并等用户确认（消耗积分）
- 是否对 key 掩码
- 本地媒体是否先上传换 URL；>20MB 是否给了软警告（并说明非 API 硬限制）
- 是否轮询到终态才结束；失败是否如实回传、未自动重试
- 是否把模型文本输出给用户；content 为空时是否说明了思考模型限制
