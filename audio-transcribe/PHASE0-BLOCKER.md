# Phase 0 BLOCKED — 2026-06-18

Phase 0（实测三模型输出 schema）无法完成：foxapi 平台当前只提供了三个目标模型中的一个。
**未伪造任何数据，未提交，未 push。**

## 探测结果（样本短链：upload-for-url 上传的 audio-transcribe-sample-3s.mp3 → OSS 72h URL）

| 模型 | 创建任务结果 | 终态 | 结论 |
|---|---|---|---|
| `cohere-transcribe` | HTTP 200 | `completed` | ✅ 可用，已落 `tests/fixtures/cohere.json` |
| `scribe-v2` | HTTP 422 `service_unavailable_error` / `no_compatible_channel`：「Model 'scribe-v2' is temporarily unavailable. Please try again later.」 | — | ❌ 连试 3 次均失败；`/v1/models` 目录中**不存在**该模型 |
| `paraformer-v2` | HTTP 422 `invalid_request_error` / `pricing_not_configured`：「pricing not configured. Please contact admin to set ModelPrice or ModelRatio.」 | — | ❌ 平台未配置计费，需管理员介入；目录中不存在 |
| `paraformer-8k-v2` | HTTP 422 `pricing_not_configured`（同上） | — | ❌ 同上 |

request_ids（部分）：scribe `57b82926-...` / `fac57528-...` / `704f4e7b-...`；paraformer-v2 `1e98638f-...`；paraformer-8k-v2 `61b99f14-...`。

## `/v1/models` 目录（audio 相关）

实际返回包含：`cohere-transcribe`、`whisper-1`、`FunAudioLLM/SenseVoiceSmall`、`FunAudioLLM/CosyVoice2-0.5B`、各类 veo/kling 视频音轨模型。
**`scribe-v2`、`paraformer-v2`、`paraformer-8k-v2` 均不在目录中。**

## 已捕获的 cohere 输出 schema（仅供参考，未写入 api-guide）

```json
"results": [{ "text": " ", "timings": { "download": 2.746, "inference": 0.885 } }]
```

- cohere 的 `results[]` **无逐词/句级时间戳**，只有 `text` + `timings`（`timings` 是下载/推理耗时，非音频时间轴）→ 该模型 .srt/.vtt 应跳过。
- 本次样本 `text` 仅为 `" "`（空白）。样本是合法 MP3（64kbps/44.1kHz/stereo/3s），但 cohere 默认 `language=en`，样本内容是否为英文语音未知，故文本可能本就近空。

## 阻塞性质

- `pricing_not_configured` 是 foxapi 平台管理员侧配置动作，**不会因重试或等待自动恢复**。
- `scribe-v2` 不在模型目录中，平台侧不可用。
- 因此完成条件 #1（四份 completed fixture + 三模型 schema）与 #3（scribe-v2 E2E）**当前客观不可达**，与本 skill 代码无关。

## 追加发现（E2E 阶段，2026-06-18）

除模型不可用外，foxapi **账号文件存储处于满载降级态**，叠加阻塞 E2E：

- `upload-for-url` 上传**间歇/持续 403「存储空间不足」**（连试 5 次全 403；偶有窗口能成功）。
- 即使上传返回了 OSS 短链，模型创建任务后下载该链接仍报
  `task_error / upstream_error`：「Failed to download the file ... (file_download_error)」——
  文件在上传与上游抓取之间被 eviction 清掉了。
- 因此**任何需要上传样本的真实 E2E（含 cohere 替代）当前都无法跑出 `completed`**。

**但管线代码已被真实 API 证明正确**：每次 E2E 都正常走通 `create→200→task_id→轮询→
检测到 status=failed→打印 error→退出码 1`，失败点全在平台侧（模型不可用 / 存储满 / 文件被清）。

## 解除阻塞后如何续跑

1. 在 foxapi 平台为 `paraformer-v2` / `paraformer-8k-v2` 配置 ModelPrice/ModelRatio，并恢复 `scribe-v2` 的可用渠道（`/v1/models` 能列出三者即可）。
2. 重跑 Phase 0 探测（计划 Task 1 Step 2–4），生成 `scribe_v2.json` / `paraformer_v2.json` / `paraformer_batch.json`。
3. 据真实 schema 续做 Task 2–10。
