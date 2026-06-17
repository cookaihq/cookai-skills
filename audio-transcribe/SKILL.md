---
name: audio-transcribe
description: Use when the user wants speech-to-text / 录音转写 — 语音识别、语音转写、听写、转录、会议记录转文字、生成字幕(srt/vtt)、transcribe audio、speech-to-text、STT. Wraps foxapi 的三个录音识别模型 (paraformer-v2/-8k-v2、scribe-v2、cohere-transcribe)，自动匹配模型并把结果按规范落盘。NOT for TTS/语音合成/音乐生成/声音克隆，也不做音频内容问答(那是 multimodal-ask)。
---

# audio-transcribe

把音频转成文字 + 字幕。封装 foxapi 三个 STT 模型，**根据用户需求自动推荐模型 → 用户确认 → 调用 `transcribe.py`** 创建任务、轮询、落盘。

> ⚠️ **平台可用性（2026-06-18 实测）**：foxapi 当前仅 `cohere-transcribe` 可用；`scribe-v2`（`no_compatible_channel`）与 `paraformer-v2/-8k-v2`（`pricing_not_configured`）暂不可用。推荐这两者前先确认平台已恢复，否则创建任务会返回 422。详见 `references/api-guide.md` 与 `PHASE0-BLOCKER.md`。

## 概览

- 异步：`POST /v1/audios/generations` → 轮询 `GET /v1/tasks/{id}?sync_upstream=true` 至终态。
- 结果是**文本**（不含 URL），直接在 `results[]` 里。
- 三模型都只吃**公网 URL**；本地文件先用兄弟 skill `upload-for-url` 转 72h 短链。

## 模型匹配（先推荐 + 说明理由，确认后再跑）

| 用户需求 | 推荐模型 |
|---|---|
| 中文/粤语、电话 8k 录音、批量(≤100 文件)、热词/敏感词/多音轨 | **Paraformer V2**（8k 录音用 `paraformer-8k-v2`） |
| 标注笑声/掌声等音频事件、自动语种检测、keyterm 偏置、开箱即用说话人分离 | **Scribe V2** |
| 希腊/荷兰/波兰/越南/阿拉伯/意/西/葡 等语种、只要纯文本+标点开关 | **Cohere Transcribe** |
| **需要 .srt/.vtt 字幕** | 优先 **Scribe V2**（唯一有官方逐词时间戳）；其余模型在实测支持时才保证 |

需求未明默认：偏中文/批量 → `paraformer-v2`；要事件/自动检测 → `scribe-v2`；上述特定语种 → `cohere-transcribe`。

## 输入处理（本地文件自动上传）

1. 入参是公网 URL → 直接 `--audio-url <url>` 透传。
2. 入参是本地路径 → **先**运行兄弟 skill：
   ```bash
   python3 ../upload-for-url/scripts/upload.py --file <本地路径>   # stdout 一行 URL，72h 有效
   ```
   再把返回的 URL 传给 `--audio-url`，并把**原文件名 stem** 传给 `--audio-label`（上传后的 URL 已丢原名）。**必须向用户复述「源音频上传链接 72h 后过期」。**
3. Paraformer 批量：每个本地文件各上传一次，`--audio-url` 可重复（≤100）；scribe/cohere 仅接受 1 个。

> 依赖：本地文件路径需要兄弟 skill `upload-for-url`（同在 github-cookaihq，一起安装）。

## CLI

```bash
python3 scripts/transcribe.py --model <M> --audio-url <URL> [选项...]
```

**布尔三态**：`--x` / `--no-x`，两者都不传 → 不发送该字段（用上游默认）。**列表参数**用可重复单数 flag。**互斥校验**：不属于当前 `--model` 的专属 flag 一律报错。

通用：`--model`（必填，∈ paraformer-v2/paraformer-8k-v2/scribe-v2/cohere-transcribe）、`--audio-url`（可重复，仅 paraformer 支持 >1）、`--audio-label`（可重复，与 url 配对，用作批量子文件夹名）。

- Paraformer 专属：`--language-hint`(可重复)、`--diarize`/`--no-diarize`、`--speaker-count N`(2–100)、`--disfluency-removal`、`--timestamp-alignment`、`--vocabulary-id`、`--special-word-filter JSON`、`--channel-id`(可重复)。
- Scribe 专属：`--language-code`、`--diarize`/`--no-diarize`、`--tag-audio-events`/`--no-tag-audio-events`、`--keyterm`(可重复，≤100/≤50 字)。
- Cohere 专属：`--language`(14 种白名单)、`--punctuation`/`--no-punctuation`、`--max-new-tokens N`。

输出/运行时：`--formats srt,vtt,md`（txt+json 永远产出）、`--output-dir`、`--label`(≤10 字)、`--no-save`、`--poll-interval 10`、`--max-attempts 180`、`--base-url`、`--use-local-key`。

退出码：`0` 成功；`1` 任务 failed / 落盘失败；`2` 无 key / 参数错误；`3` 轮询超时（已打印 task_id + 复查命令）。

## 输出（每任务一个子文件夹）

输出根目录优先级：`--output-dir` > 环境变量 `AUDIO_TRANSCRIBE_OUTPUT_DIR` > `$PWD`。
任务文件夹：`{YYYYMMDD-HHMMSS}-{≤10字标签}/`（标签优先级 `--label` > audio-label > 模型名）。

- `result.json`（完整终态响应）、`transcript.txt`（各 result 文本以空行连接）**始终产出**。
- `transcript.md`：元信息头 + 正文（有说话人字段时按说话人分段）。
- `transcript.srt`/`transcript.vtt`：从逐词时间戳生成；**无时间戳的模型跳过**并在 stderr 说明，禁止编造时间。
- 源文件 >1（paraformer 批量）：任务文件夹内每个源文件一个子文件夹。

## 鉴权 / 安全

- Key 优先级（首个非空，逐变量独立）：进程 env `X_API_KEY` → `$PWD/.env.local` → `$PWD/.env` → `~/.config/audio-transcribe/.env`（仅 `--use-local-key`）。
- key 日志一律 `head4****tail4` 掩码，完整值只进 `Authorization`。
- 创建接口**不自动重试**；唯一例外是 HTTP 401 → key 链 fallback（401 不扣积分）。402/422/429/5xx 与网络错误不 fallback，如实回传交用户决定。

## Pre-Response Checklist

- [ ] key 已掩码，未回显完整值？
- [ ] 已向用户推荐模型并说明理由、得到确认？（注意当前平台仅 cohere 可用）
- [ ] 本地文件已经 `upload-for-url` 转 URL，并复述「72h 过期」？
- [ ] 结果已落盘并把输出路径告知用户？
- [ ] 模型无时间戳导致字幕跳过时，已把 stderr 提示转达用户？
