---
name: pdf2md_docx
description: Use when the user wants to convert a PDF into Markdown / LaTeX / DOCX — phrases like "把这个 PDF 转成 markdown"、"PDF 转 md/docx"、"提取 PDF 里的公式和表格"、"PDF 转可编辑文档"、"pdf2md"、"doc2x". Wraps aihubmax.com Doc2X V3; handles formula recognition and cross-page table merging, returns a ZIP that is auto-extracted into a date-time-prefixed folder. Do NOT use for plain text extraction of a single short page (read it directly), image generation, OCR of photos, or non-PDF documents.
---

# pdf2md_docx

## Overview

通过 [aihubmax.com](https://docs.aihubmax.com/pages/zh/api-manual/document-processing/doc2x-v3) 的 `doc2x-v3` 接口把 **PDF 转换为 Markdown / LaTeX / DOCX**，支持公式识别与跨页表格合并。这是**异步任务**：创建后必须轮询查询直到终态（`completed` / `failed`）。结果是一个 **ZIP 压缩包**（含转换文档 + 图片资源），脚本会自动下载并**解压到带日期时间前缀的文件夹**。鉴权 `Authorization: Bearer <key>`。

完整字段、错误码、响应结构见 [references/api-guide.md](references/api-guide.md)。

## When to Use

- 用户想把 PDF 转成 Markdown / LaTeX / DOCX 等可编辑格式
- 用户关注 PDF 里的**公式**、**跨页表格**的还原
- 用户说 "doc2x"、"pdf2md"、"PDF 转 md/docx"

## When NOT to Use

- 只想读取某页纯文本 / 问 PDF 内容 → 直接读，不必转换
- 图片生成 / 图生图 → `image-2` / `banana-2`
- 照片 OCR、视频 / 音频处理
- 只是想把本地文件换成 URL → `upload-for-url`

## CRITICAL

- **每次调用只产出一种格式**（`convert_mode` 是单选 `md|tex|docx`）。要 md 又要 docx＝两次调用，**会各扣一次积分**——多格式前先告知用户成本并取得同意。
- 创建接口返回的是**异步任务**，必须接着轮询 `/v1/tasks/{id}?sync_upstream=true` 直到 `completed` / `failed`。
- **结果 ZIP 链接 24 小时后失效**，脚本默认会立即下载并解压，不要只把 URL 抛给用户。
- API 需要 `pdf_url`（公网可下载地址）+ `page_count`（必填，用于预扣费校验）。传**本地 PDF** 时脚本会自动：统计页数 → 上传换取 URL（该上传 URL 72h 过期）→ 提交任务。
- 不得回显完整 `Authorization` token，日志只允许 `head4****tail4` 掩码。
- 禁止自动循环重试创建接口（会重复扣费）；**唯一例外**是 HTTP 401 触发的 key 链 fallback——换的是 key 不是请求，且 401 不消耗积分。
- 默认把结果解压到 `{YYYYMMDD-HHMMSS}-{标签}/` 文件夹（标签默认取 PDF 文件名前 40 字）。

## Auth & Key Handling

**读取优先级（从高到低）**，按值去重，逐个尝试：

1. 进程 env `AIHUB_API_KEY`（本轮显式注入 `AIHUB_API_KEY=... python3 ...`）
2. `$PWD/.env.local` 中的 `AIHUB_API_KEY=...`（**自动读取**，不向上递归）
3. `$PWD/.env` 中的 `AIHUB_API_KEY=...`（**自动读取**，不向上递归）
4. `~/.config/pdf2md_docx/.env`（**仅 `--use-local-key`** 时读）

**401 自动 fallback**：某层 key 调用返回 HTTP 401（`authentication_error`）时自动尝试下一层。401 不消耗积分，安全。其他错误码（402/422/429/5xx）和网络错误**不**触发 fallback，立即返回交用户决定。

`.env` / `.env.local` 解析极简、非 shell：支持 `KEY=value` / `KEY="value"` / `KEY='value'`、等号两侧空白、`#` 起首注释、空行；同名取最后一次；**不支持** `${X}` / `$(...)` / 续行符。只识别 `AIHUB_API_KEY`（旧名 `X_API_KEY` 仍兼容）。key 日志一律 `head4****tail4` 掩码。

首次配置（可选）：`./scripts/set_key.sh`（或 `echo 'sk-xxx' | ./scripts/set_key.sh --stdin`）。

## Parameters

| 参数 | 必填 | 说明 |
|------|------|------|
| `--pdf` 或 `--pdf-url` | 是（二选一） | 本地 PDF 路径 / 已公开的 PDF URL |
| `--page-count` | 本地可省略 | PDF 页数；本地 PDF 自动统计，`--pdf-url` 时**必填** |
| `--convert-mode` | 否 | `md`（默认）/ `tex` / `docx`，每次一种 |
| `--formula-mode` | 否 | `normal`（默认）/ `dollar` |
| `--merge-cross-page-forms` | 否 | 合并跨页表格（默认关） |
| `--filename` | 否 | ZIP 内输出文档名（不含扩展名，上游超 50 字截断） |
| `--output-dir` | 否 | 解压根目录；默认 env `PDF2MD_DOCX_OUTPUT_DIR` 或 `$PWD` |
| `--label` | 否 | 文件夹标签段；默认取 PDF 文件名前 40 字 |
| `--keep-zip` | 否 | 在输出文件夹内保留原始 ZIP |
| `--no-extract` | 否 | 只下载 ZIP 不解压（存到输出根目录） |
| `--use-local-key` | 否 | 允许读取 `~/.config/pdf2md_docx/.env` |

## Workflow

1. **收集与校验**：确认输入是 PDF；确定 `--convert-mode`（用户要多格式则说明会多次扣费并确认）。
2. **预览摘要**：脚本会先打印模型、convert_mode、page_count、掩码 key —— 默认无需二次确认即继续（与 image-2 一致，调用脚本即视为已确认会消耗积分）。如不确定用户是否愿意扣费，先问。
3. **调用脚本**：见 Usage。脚本自动完成「（本地）统计页数 → 上传换 URL → 创建任务 → 轮询 → 下载 ZIP → 解压到日期文件夹」。
4. **轮询直到终态**：默认 8 秒间隔轮询 `/v1/tasks/{id}?sync_upstream=true`。`completed` 但 `results` 为空时视为上游竞态，继续轮询。
5. **返回结果**：成功时 **stdout 打印解压后的文件夹路径**，stderr 打印文件清单 + 24h 过期提醒。失败时打印 `error.code` / `error.message`。
6. **询问是否重命名主文档**：解压后转换出的文档默认叫 `output.md`（或 `--filename` 指定值）这种通用名，不够语义化。**解压成功后，主动告知用户主文档当前文件名，并询问是否要改名**（建议依据 PDF 标题/内容给一个候选名）。用户给出新名后用 `mv` 改名即可——文档内图片用相对路径 `images/...` 引用，与文档文件名无关，改名不会破坏图片链接。用户说不用改则保持原名。这一步对 `--no-extract`（只下 ZIP 未解压）不适用。

## Usage

```bash
# 本地 PDF → Markdown（默认），自动统计页数 + 上传 + 解压到 {时间戳}-{标签}/
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py --pdf ./report.pdf

# 本地 PDF → DOCX，合并跨页表格
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py \
  --pdf ./report.pdf --convert-mode docx --merge-cross-page-forms

# 已有公开 URL（远程无法自动统计页数，必须传 --page-count）
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py \
  --pdf-url 'https://example.com/a.pdf' --page-count 12 --convert-mode md

# 指定输出根目录 + 文件夹标签 + 保留 ZIP
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py \
  --pdf ./paper.pdf --convert-mode tex --formula-mode dollar \
  --output-dir ~/Documents/converted --label 论文 --keep-zip

# 只下载 ZIP，不解压
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py --pdf ./a.pdf --no-extract
```

**输出约定**：成功时 **stdout 只打印一行结果路径**（解压文件夹，或 `--no-extract` 时的 ZIP 路径），便于其他脚本解析；所有日志（含掩码 key、轮询进度、文件清单、过期提醒）走 stderr。

## Error Handling

| 退出码 | 含义 |
|---|---|
| 0 | 成功，stdout 为输出路径 |
| 1 | 上传 / 创建 / 下载 / 解压失败、本地 PDF 不可读、网络错误（stderr 有原因） |
| 2 | 未找到 AIHUB_API_KEY；或任务 `failed`；或缺 `--page-count`（远程 URL） |
| 3 | 轮询超时（任务可能仍在运行，stderr 给出 task_id 供手动查询） |

HTTP 语义：401 鉴权失败（触发 key fallback）｜402 余额不足｜422 参数校验失败｜429 限流（**不自动重试**）｜5xx 服务异常。

## Pre-Response Checklist

- 是否对 key 做了 `head4****tail4` 掩码
- 是否轮询到 `completed` / `failed` 才结束
- 成功后是否输出了**解压文件夹路径**并提示结果 ZIP URL 24h 过期
- 解压后是否**询问用户是否重命名主文档**（`output.md` 等通用名），并按用户意愿处理
- 多格式需求是否事先告知会多次扣费并取得同意
- 本地 PDF 是否走了「统计页数 → 上传换 URL」，远程 URL 是否要求了 `--page-count`

## Directory

- `SKILL.md`
- `scripts/convert.py`（主流程）、`scripts/set_key.sh`
- `scripts/config.py`、`scripts/client.py`、`scripts/upload_helper.py`（与 image-2 / multimodal-ask 同源的 provider 层）
- `references/api-guide.md`
