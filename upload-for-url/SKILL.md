---
name: upload-for-url
description: Use when the user wants to turn a LOCAL file (image/audio/video/document) into a public URL that an AI API can consume — phrases like "把这个文件传上去拿个链接"、"上传文件换个 URL"、"这个视频/音频/PDF 传成在线地址给模型用". Also re-hosts a remote URL into an aihubmax 72h short link. Returns a public URL valid for 72 hours. Do NOT use for HTML page preview (that's preview-share), production deploys, npm publish, or git push.
---

# upload-for-url

## Overview

通过 [aihubmax.com](https://docs.aihubmax.com/pages/zh/api-manual/file-management/upload-stream) 的文件上传接口，把本地文件 / 远程 URL / base64 托管成一个**公网可访问的 URL**，常用于喂给只接受 URL 的 AI 接口（如 multimodal-ask 的 `image_url`/`video_url`/`audio_url`/`file_url`）。默认网关为 `https://api.aihubmax.com`，鉴权 `Authorization: Bearer <key>`。

完整字段、错误码见 [references/api-guide.md](references/api-guide.md)。

## When to Use

- 把**本地文件**（图/音/视频/文档）变成公网 URL，喂给只收 URL 的 API
- 把一个远程 URL **转存**成 aihubmax 72h 短链

## When NOT to Use

- HTML 页面在线预览 → 用 `preview-share`
- 正式部署 / npm publish / git push

## CRITICAL

- **上传的 URL 72 小时后过期**——输出时必须告知用户；需长期保留要转存
- 不得回显完整 `Authorization` token，日志只允许 `head4****tail4` 掩码
- HTTP 401 触发 key 链 fallback（401 不消耗积分，安全）；其他错误码不 fallback，直接返回交用户决定
- 上传请求默认发送浏览器 User-Agent，避免 Cloudflare `403 / error code: 1010`；调用方显式传入的 User-Agent 保持不变
- **不预设文件大小上限**：文档未给出具体字节数，413 `file_too_large_error` 由 API 反应式裁决，禁止硬编码 MB 数值

## Auth & Key Handling

读取优先级（从高到低）：1) 进程 env `AIHUB_API_KEY` 2) `$PWD/.env.local` 3) `$PWD/.env`（均自动读、不向上递归）4) `~/.config/upload-for-url/.env`（仅 `--use-local-key`）。`.env` 解析极简、非 shell（`KEY=value`/引号/`#`注释/空行，同名取最后，不展开 `${X}`/`$(...)`）。key 日志一律 `head4****tail4` 掩码。每一层都先找 `AIHUB_API_KEY`，找不到再找旧名 `X_API_KEY`（仍兼容，命中会打废弃提示）。

首次配置（可选）：`./scripts/set_key.sh`（或 `echo 'sk-xxx' | ./scripts/set_key.sh --stdin`）。

## Usage

```bash
# 本地文件直传（主路径）
AIHUB_API_KEY='sk-xxx' python3 scripts/upload.py --file ./clip.mp4

# base64 / data URL
AIHUB_API_KEY='sk-xxx' python3 scripts/upload.py --base64 'data:image/png;base64,iVBOR...'

# 远程 URL 转存成 72h 短链
AIHUB_API_KEY='sk-xxx' python3 scripts/upload.py --url 'https://example.com/a.pdf'

# 自定义存储文件名 / 关闭自动清理（空间不足直接 403）
AIHUB_API_KEY='sk-xxx' python3 scripts/upload.py --file ./a.png --file-name cover.png --no-auto-cleanup
```

**输出约定**：成功时 **stdout 只打印一行 URL**（便于其他脚本解析），stderr 打印 `id/size` 摘要 + 72h 过期提醒 + 掩码 key。失败时 stderr 打印 `[HTTP <code>] <提示>`，退出码非 0。

## Error Handling

| 退出码 | 含义 |
|---|---|
| 0 | 成功，stdout 为 URL |
| 1 | 上传失败（HTTP 4xx/5xx、网络错误，或本地文件不可读），stderr 有原因 |
| 2 | 未找到 AIHUB_API_KEY（或命令行参数错误，argparse 用法退出码也是 2） |

HTTP 语义：401 鉴权失败（触发 key fallback）｜403 存储空间不足；纯文本 `error code: 1010` 表示 Cloudflare 拒绝 User-Agent｜413 文件过大（不编造上限）｜429 限流（不自动重试）｜5xx 服务异常。

## Pre-Response Checklist

- 是否对 key 做了 `head4****tail4` 掩码
- 成功时是否提示了 **URL 72 小时过期**
- 失败时是否如实回传 HTTP code 与原因、未编造大小阈值、未自动重试
