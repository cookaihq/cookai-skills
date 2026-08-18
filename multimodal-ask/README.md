# multimodal-ask

用**用户点名的模型**，通过 aihubmax.com 的 `llm-custom` 异步端点处理文本 / 图 / 音 / 视频 / 文档 / 混合媒体，返回模型的文本回答。

```bash
SKILL_DIR=/path/to/github-cookai-skills/multimodal-ask
AIHUB_API_KEY='sk-xxx' uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/ask.py" --model gemini-3.5-flash --video ./clip.mp4 --prompt "这段视频讲了什么"
```

- 本地媒体自动经 aihubmax 上传换 72h URL（内置 `upload_helper`，无外部 skill 依赖）
- 提交前能力预校验（模型是否可用 / 是否支持该媒体类型，不消耗积分）
- 异步：提交 → 轮询到终态 → 取 `choices[0].message.content`
- 鉴权 / key 分层详见 [SKILL.md](SKILL.md) 与 [references/api-guide.md](references/api-guide.md)

## 运行时环境

纯 Python 标准库，无第三方运行时依赖；解释器由 uv 按 `pyproject.toml` + `uv.lock` 钉死（ADR 0007），venv 落 `<skill>/.venv`。

- 调用一律走 `uv run --project <skill目录> <skill目录>/scripts/ask.py ...`，**不要裸 `python3`**。
- 手工重建环境：`rm -rf <skill>/.venv && uv sync --project <skill>`；`ask.py` 被裸 `python3` 直接执行时也会自动拉回该 venv（缺失则按 `uv.lock` 重建）。
- 跑测试：`uv run --project . --with pytest python -m pytest tests -q`（pytest 经 `--with` 临时叠加，不进 `uv.lock`）。

## 网络抖动处理（ADR 0006）

- 超时分级：元数据 / 提交 / 轮询 60s，本地媒体上传 300s。
- 幂等读（模型清单、任务轮询）：429 / 5xx / 网络异常自动重试 3 次 + 指数退避（1s、2s），429 优先按 `Retry-After` 等待；轮询的单次抖动只消耗内层重试，不击穿总轮询预算。
- 计费写（提交任务）与上传：只在「确认未受理」时重试（429、请求未发出）；请求已发出但响应丢失时不重试，stderr 明确报「结果不明」，由用户决定是否重发。
