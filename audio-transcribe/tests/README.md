# audio-transcribe tests

```bash
cd audio-transcribe && python3 -m pytest -q
```

- `test_config.py` — key 链 / dotenv 解析 / 掩码。
- `test_client.py` — 浏览器 UA 注入、401-only key fallback。
- `test_validate.py` — 跨模型 flag 拒绝、cohere 语言白名单、keyterm/speaker-count 边界、audio-url 计数按模型、payload 组装。
- `test_adapters.py` — cohere 用真实 fixture（无时间戳）；scribe 用官方文档 schema 的合成输入；**paraformer skip**（模型不可用，Phase 0 未捕获）。
- `test_formats.py` — sanitize、cue 断句、txt/md/srt/vtt。
- `test_transcribe.py` — 纯 helper（任务/子文件夹命名、write_outputs 落盘）。

`fixtures/cohere.json` 是 Phase 0 真实捕获的终态响应。`scribe_v2.json` / `paraformer_v2.json` / `paraformer_batch.json` **待对应模型在 foxapi 恢复后补测**（见 `../PHASE0-BLOCKER.md`）。

场景文档（人工对照）：`scenario-1-model-match.md` / `scenario-2-local-upload.md` / `scenario-3-cross-model-flag-rejected.md`。
