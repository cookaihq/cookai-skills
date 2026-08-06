# upload-for-url — API 参考

Server: `https://api.aihubmax.com` ｜ Auth: `Authorization: Bearer <AIHUB_API_KEY>`（`sk-...`）

3 个上传端点，**统一返回一个 72 小时后过期的公网 URL**；存储空间不足时默认自动淘汰最早上传的文件（`auto_cleanup=true`），设 `false` 则空间不足直接 403。

## 端点

| 端点 | Content-Type | 必填 | 可选 |
|---|---|---|---|
| `POST /v1/files/upload/stream` | `multipart/form-data` | `file`（文件部分） | `file_name`、`auto_cleanup` |
| `POST /v1/files/upload/base64` | `application/json` | `file_data`（纯 base64 或 data URL） | `file_name`、`auto_cleanup` |
| `POST /v1/files/upload/url` | `application/json` | `url`（远程地址） | `file_name`、`auto_cleanup` |

## 成功响应 200

```json
{ "id": "f_xxx", "filename": "clip.mp4", "url": "https://.../clip.mp4", "size": 1048576, "created": 1776874481 }
```

- `url`：下游可消费的公网地址（72h 有效）
- `size`：字节数

## 错误码

| 码 | type | 说明 |
|---|---|---|
| 400 | — | 请求格式错误 |
| 401 | `authentication_error` | token 无效 / 缺失（脚本触发 key 链 fallback） |
| 403 | — | 存储空间不足（多见于 `auto_cleanup=false`）；纯文本 `error code: 1010` 则是 Cloudflare 拒绝 User-Agent |
| 413 | `file_too_large_error` | 文件过大。**文档未给出具体字节上限**，不要硬编码 MB 数值 |
| 429 | `rate_limit_error` | 限流，不自动重试 |
| 500 | — | 服务器内部错误 |

> 字段与路由来源：`openapi/zh/upload-stream.json` / `upload-base64.json` / `upload-url.json`（本地 Mintlify 仓）。运行网关 `api.aihubmax.com` 的三个端点已验证可达。
