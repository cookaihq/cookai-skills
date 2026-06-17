# Scenario 3 — 跨模型 flag 被拒绝

**输入**：对 `paraformer-v2` 传了 scribe 专属的 `--keyterm`。

```bash
python3 scripts/transcribe.py --model paraformer-v2 --audio-url "https://x/a.mp3" --keyterm foo
```

**期望**：`validate.build_payload` 抛 `ValidationError`，stderr 打印
`Error: --keyterms is not supported by paraformer-v2`，进程**退出码 2**（参数错误），**不发起任何网络请求**（不扣积分）。

同类应拒绝的例子：
- `paraformer-8k-v2` + `--language-hint zh`（8k 仅中文，禁传 language_hints）。
- `scribe-v2` + 两个 `--audio-url`（scribe 仅接受 1 个）。
- `cohere-transcribe` + `--language ru`（不在 14 种白名单）。
- `--speaker-count 3` 但未 `--diarize`。

对应自动化覆盖见 `test_validate.py`。
