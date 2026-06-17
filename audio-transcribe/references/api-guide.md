# audio-transcribe — API 指南

封装 foxapi.cc 的录音文件识别（STT）模型。三模型共享异步机制：

```
POST /v1/audios/generations            → { id, status, ... }
GET  /v1/tasks/{id}?sync_upstream=true → 轮询至 status ∈ {completed, failed}
```

鉴权：`Authorization: Bearer <X_API_KEY>`。三模型都只接受**公网 HTTP/HTTPS 音频 URL**，不接受本地文件直传（本地文件先经 `upload-for-url` skill 转 72h 短链）。

> ⚠️ **平台可用性（2026-06-18 实测）**：截至本文撰写，foxapi `/v1/models` 仅提供 `cohere-transcribe`。
> `scribe-v2` 返回 `422 no_compatible_channel`（目录中不存在），`paraformer-v2` / `paraformer-8k-v2` 返回
> `422 pricing_not_configured`（平台未配置计费）。详见 [../PHASE0-BLOCKER.md](../PHASE0-BLOCKER.md)。
> 本指南的输入字段表来自设计规格 §2；输出 schema 仅 `cohere-transcribe` 为实测，其余待模型恢复后补测。

---

## 1. 输入字段（按模型）

### Paraformer V2 / 8k-V2（上游：阿里 DashScope）

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | str | `paraformer-v2` 或 `paraformer-8k-v2`（必填） |
| `file_urls` | array[str] | **1–100 个**公网 URL（必填） |
| `language_hints` | array[str] | `zh/en/ja/yue/ko/de/fr/ru`；**仅 `paraformer-v2`**，8k 传则报错 |
| `diarization.enabled` | bool | 说话人分离 |
| `diarization.speaker_count` | int 2–100 | 说话人数提示（仅单声道），需配合 `enabled` |
| `recognition.disfluency_removal_enabled` | bool | 语气词过滤 |
| `recognition.timestamp_alignment_enabled` | bool | 时间戳校准 |
| `recognition.vocabulary_id` | str | 热词表 ID |
| `recognition.special_word_filter` | JSON | 敏感词规则（须合法 JSON） |
| `channel_id` | array[int] | 多音轨，索引从 0，各轨独立计费 |

### Scribe V2（上游：ElevenLabs）

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | str | `scribe-v2`（必填） |
| `audio_url` | str | 单个公网 URL（必填） |
| `language_code` | str | ISO-639-1/3；不传自动检测 |
| `diarize` | bool | **上游默认 true** |
| `tag_audio_events` | bool | **上游默认 true**，标注笑声/掌声等 |
| `keyterms` | array[str] | ≤100 项，每项 ≤50 字 |

### Cohere Transcribe（上游：Cohere）

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | str | `cohere-transcribe`（必填） |
| `audio_url` | str | 单个公网 URL（必填） |
| `language` | str | 固定 14 种 `en/fr/de/it/es/pt/el/nl/pl/zh/ja/ko/vi/ar`；不传默认 `en` |
| `punctuation` | bool | 上游默认 true（标点 + 大小写） |
| `max_new_tokens` | int ≥1 | 分块上限，默认 256 |

**参数归属约束**（`validate.py` 据此拒绝跨模型误用）：`keyterms` 仅 Scribe；`vocabulary_id`/`diarization`/`recognition`/`channel_id`/`language_hints` 仅 Paraformer；`punctuation`/`max_new_tokens` 仅 Cohere；`tag_audio_events` 仅 Scribe；`paraformer-8k-v2` 禁传 `language_hints`。

---

## 2. 实测输出 schema (Phase 0, 2026-06-18)

STT 任务 `type=audio`，**结果不含 url**，文本直接在 `results[]` 里。

### cohere-transcribe — ✅ 实测（`tests/fixtures/cohere.json`）

```json
"results": [{ "text": " ", "timings": { "download": 2.746, "inference": 0.885 } }]
```

- **时间戳**：无。`results[]` 只有 `text` + `timings`，且 `timings` 是**下载/推理耗时（性能指标），不是音频时间轴** → 该模型 **.srt/.vtt 一律跳过**，.md 省略时长行。
- **speaker**：无。
- **批量**：不支持（单 `audio_url`）。
- 备注：本次样本（3s MP3，默认 `language=en`）`text` 返回为空白 `" "`，属样本内容/语种问题，非 schema 问题。
- 适配：`adapters.adapt_cohere` → `words=[]`、`segments=[]`、`has_timestamps=False`。

### scribe-v2 — ⏳ 官方文档 schema，未实测（模型当前不可用）

官方 `get-task-detail` 规范（设计规格 §3）：

```json
"results": [{
  "text": "Hello world.",
  "language_code": "en",
  "language_probability": 0.98,
  "words": [{ "text": "Hello", "start": 0.0, "end": 0.42, "type": "word" }]
}]
```

- **时间戳**：`words[].start` / `words[].end`，单位**秒**（float）。
- **speaker**：官方注明「`words` 的具体键由上游 ASR 决定」；`adapters.adapt_scribe` 同时探测 `speaker_id` / `speaker`。
- `adapter` 已按此 schema 实现，但**待模型恢复后用真实 fixture 复核**（尤其 speaker 键名与 diarize 开启时的落点）。

### paraformer-v2 / paraformer-8k-v2 — ❌ 未捕获（BLOCKED）

模型在 foxapi 平台 `pricing_not_configured`，Phase 0 无法捕获。`adapters.adapt_paraformer` 暂 `raise NotImplementedError`，**禁止凭上游原生文档臆测**（DashScope 网关会归一化字段，如 `begin_time`/`end_time` ms vs `start`/`end` s、`speaker_id` 落点、批量 `results[]` 基数均需实测）。模型恢复后按计划 Task 1 Step 4–5 补测并实现。

---

## 3. 错误码

创建接口**不自动重试**（唯一例外：HTTP 401 触发 key 链 fallback，401 不消耗积分）。

| 状态 | 含义 | 处理 |
|---|---|---|
| 200 | 任务已创建/查询成功 | 继续轮询 |
| 401 | 鉴权失败 | 换下一个 key 重试；全部 401 → 退出 1 |
| 402 | 余额不足 | 不重试，如实回传 |
| 422 | 参数错误 / 模型不可用（`no_compatible_channel` / `pricing_not_configured`） | 不重试，如实回传交用户 |
| 429 | 频率超限 | 不自动重试 |
| 5xx | 服务端错误 | 不 fallback，如实回传 |

任务终态 `failed` 时回传 `error` 对象（`{message,type,code}`），脚本退出码 1。

## 4. 任务生命周期

`pending → processing → completed | failed`。轮询 `GET /v1/tasks/{id}?sync_upstream=true`；`completed` 且 `results` 非空才算成功。超时（默认 10s×180≈30min）不报错，打印 `task_id` + 复查命令，退出码 3。
