# upload-for-url

把本地文件 / 远程 URL / base64 通过 aihubmax 文件接口托管成一个 **72 小时公网 URL**，供只接受 URL 的 AI 接口消费。默认网关为 `https://api.aihubmax.com`。

```bash
AIHUB_API_KEY='sk-xxx' python3 scripts/upload.py --file ./clip.mp4   # → stdout 打印一行 URL
```

- 三种来源：`--file`（本地，multipart）/ `--base64`（base64 或 data URL）/ `--url`（远程转存）
- 输出：stdout 纯 URL；stderr 摘要 + 72h 过期提醒（key 掩码）
- 请求默认使用浏览器 User-Agent，兼容 Cloudflare 文件上传网关
- 鉴权与 key 分层读取见 `SKILL.md`；字段/错误码见 `references/api-guide.md`

详见 [SKILL.md](SKILL.md)。
