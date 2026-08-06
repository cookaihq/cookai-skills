# multimodal-ask — API 参考

Server: `https://api.aihubmax.com` ｜ Auth: `Authorization: Bearer <AIHUB_API_KEY>`（`sk-...`）。统一 `llm-custom` 协议，异步：提交 → 轮询任务。

## 1. 模型清单（能力预校验，不消耗积分）

`GET /v1/configs/llm_generations_models` → `{ "object": "list", "data": [ { "id", "object": "model", "capabilities": [...] } ] }`

- `capabilities` 取值（顺序固定）：`text` → `vision` → `video` → `audio` → `file`（所有可见渠道的并集）
- 空分组返回 `200 { "object":"list", "data":[] }`（非 404）
- **内容块类型 → capability 映射**（注意 image 对应 `vision`）：`image_url→vision`、`video_url→video`、`audio_url→audio`、`file_url→file`、text→`text`

## 2. 提交任务

`POST /v1/llm/generations`，body：

```json
{
  "model": "claude-opus-4-7",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": [
      {"type": "text", "text": "describe"},
      {"type": "image_url", "image_url": {"url": "https://.../a.png"}},
      {"type": "video_url", "video_url": {"url": "https://.../v.mp4"}},
      {"type": "audio_url", "audio_url": {"url": "https://.../u.mp3"}},
      {"type": "file_url",  "file_url":  {"url": "https://.../d.pdf"}}
    ]}
  ],
  "stream": false,
  "max_tokens": 1024
}
```

- `content` 块类型 ∈ `text` / `image_url` / `video_url` / `audio_url` / `file_url`；`image_url` 亦接受 base64 data URL
- `max_tokens` 家族约束：`claude-*` **必填**（脚本默认补 1024）；`gpt-*` 通常 ≥16；`gemini-*` 可选
- `reasoning`（可选，opt-in）：llm-custom schema 未列，靠 `additionalProperties:true` 透传；效果未验证
- 成功 200：`{ "id": "task-llm-...", "object":"llm.generation.task", "type":"llm", "model", "status":"pending", "progress":0, "created", "stream":null, "results":null, "error":null }`

### 视频源约束（据 llm-video 文档）

- 直链扩展名：`.mp4/.mpeg/.mpg/.mov/.webm`
- YouTube：仅 **Gemini 家族**支持 `watch?v=<id>` / `youtu.be/<id>`；**不支持 Shorts**（脚本自动改写为 `watch?v=`）

## 3. 轮询结果

`GET /v1/tasks/{task_id}?sync_upstream=true` → 状态机 `pending` / `processing` / `completed` / `failed`。

- `completed`：`results[0]` 为 OpenAI `ChatCompletion`（`object:"chat.completion"`）；文本在 `results[0].choices[0].message.content`
- `failed`：`error { code, message, type }`
- `usage.credits_reserved`：本次预扣额度
- **已知限制**：思考模型（如 kimi-k2.6）的 `reasoning_content` 不累积进 `content`，可能为空串（完整思考仅 SSE 可见）

## 4. 错误码（提交）

| 码 | code / type | 说明 |
|---|---|---|
| 401 | `authentication_error` | token 无效（脚本触发 key 链 fallback） |
| 422 | `no_available_model` | 模型未配置 / 不可用 |
| 422 | `model_not_support_capability` | 模型不支持本次类型组合（如 text+video） |
| 422 | `model_rule_violation` | 违反模型规则（含 `gemini_video_size_exceeded`、单视频模型收到多个等） |
| 422 | `invalid_param` | 参数非法（含 max_tokens 家族约束违反） |
| 429 | `rate_limit_error` | 限流，不自动重试 |
| 500 / 503 | `internal_error` / `service_unavailable` / `all_platforms_exhausted` | 服务 / 上游异常 |

> 来源：`openapi/zh/llm-custom.json`、`configs-llm-generations-models.json`、`get-task-detail.json`、`llm-video.json`（本地 Mintlify 仓）。
