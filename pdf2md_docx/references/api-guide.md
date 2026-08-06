# Doc2X V3 API Guide

来源：aihubmax.com 统一网关，`openapi/zh/doc2x-v3.json`。本文件是 skill 自带的离线速查；权威定义以该 OpenAPI 为准。

## 接口

- **创建任务**：`POST https://api.aihubmax.com/v1/run/generations`
- **查询任务**：`GET https://api.aihubmax.com/v1/tasks/{task_id}?sync_upstream=true`
- **鉴权**：`Authorization: Bearer <AIHUB_API_KEY>`

异步模型：创建返回 `id`（task_id），随后轮询查询接口直到 `status` 为 `completed` 或 `failed`。

## 创建请求体（`Doc2xRequest`）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `model` | string | 是 | `doc2x-v3` | 固定 `doc2x-v3` |
| `pdf_url` | string | 是 | — | PDF 文件下载地址（公网可访问） |
| `page_count` | integer ≥1 | 是 | — | PDF 页数，用于预扣费与执行时校验 |
| `convert_mode` | enum | 否 | `md` | 输出格式：`md` / `tex` / `docx`（**每次一种**） |
| `formula_mode` | enum | 否 | `normal` | 公式处理：`normal` / `dollar` |
| `filename` | string\|null | 否 | null | 输出文件名（不含扩展名，超 50 字执行时截断，maxLength 200） |
| `merge_cross_page_forms` | boolean | 否 | false | 是否合并跨页表格 |

请求示例：

```json
{
  "model": "doc2x-v3",
  "pdf_url": "https://example.com/document.pdf",
  "page_count": 10,
  "convert_mode": "md",
  "formula_mode": "normal",
  "filename": "output",
  "merge_cross_page_forms": false
}
```

## 创建响应（`TaskResponse`）

```json
{
  "created": 1757165031,
  "id": "task-unified-1757165031-uyujaw3d",
  "model": "doc2x-v3",
  "object": "document.generation.task",
  "progress": 0,
  "status": "pending",
  "task_info": { "can_cancel": true, "estimated_time": 45 },
  "type": "document"
}
```

- `status` 取值：`pending` / `processing` / `completed` / `failed`
- `progress`：0–100

## 查询响应（终态）

`completed` 时 `results[0].url` 指向一个**可下载的 ZIP 压缩包**，内含转换后的文档（`.md` / `.tex` / `.docx`）及图片资源。

```json
{
  "status": "completed",
  "progress": 100,
  "results": [ { "url": "https://.../result.zip" } ]
}
```

注意点：
- **ZIP 链接 24 小时后失效**，需尽快下载保存。
- 偶发上游竞态：`status` 已置 `completed` 但 `results` 仍为空 —— 应继续轮询直到 `results` 非空（脚本已处理）。
- `results[0]` 通常只有 `url`，一般无 `content_type`（结果固定是 ZIP）。

## 错误码

| HTTP | type | 含义 | 处理 |
|------|------|------|------|
| 400 | `invalid_request_error` | 请求格式错误 | 修正请求体 |
| 401 | `authentication_error` | API 密钥无效 | 触发 key 链 fallback；都失败则去 aihubmax.com 检查 key |
| 402 | `insufficient_quota` | 账户余额不足 | 充值后重试 |
| 422 | `validation_error` | 参数校验失败 | 按 `error.message` 调整（如 page_count 与实际不符） |
| 429 | `rate_limit_error` | 请求频率超限 | **不自动重试**，稍后再试 |
| 500 | `server_error` | 服务器内部错误 | 由用户决定是否重试 |
| 503 | `service_unavailable` | 服务暂不可用 | 稍后重试 |

错误体结构：`{ "error": { "message": "...", "type": "..." } }`。

## 本地 PDF → URL 的衔接

`pdf_url` 必须是公网可下载地址。本地 PDF 由 `upload_helper.upload_local_file` 经
`POST /v1/files/upload/stream`（multipart，字段 `file` + `auto_cleanup=true`）上传，返回
`{ "url": "..." }`。该上传 URL **72 小时**有效，足够本次转换任务下载。
