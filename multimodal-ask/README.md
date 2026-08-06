# multimodal-ask

用**用户点名的模型**，通过 aihubmax.com 的 `llm-custom` 异步端点处理文本 / 图 / 音 / 视频 / 文档 / 混合媒体，返回模型的文本回答。

```bash
AIHUB_API_KEY='sk-xxx' python3 scripts/ask.py --model gemini-3.5-flash --video ./clip.mp4 --prompt "这段视频讲了什么"
```

- 本地媒体自动经 aihubmax 上传换 72h URL（内置 `upload_helper`，无外部 skill 依赖）
- 提交前能力预校验（模型是否可用 / 是否支持该媒体类型，不消耗积分）
- 异步：提交 → 轮询到终态 → 取 `choices[0].message.content`
- 鉴权 / key 分层详见 [SKILL.md](SKILL.md) 与 [references/api-guide.md](references/api-guide.md)
