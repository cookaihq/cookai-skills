# Pressure Test Scenarios — multimodal-ask

1. **pytest 单元测试**（`test_*.py`）：纯逻辑 + 注入式 transport/sleep 桩（无网络）。
   跑（在 `multimodal-ask/` 下）：`uv run --project . --with pytest python -m pytest tests -q`
   —— 解释器由 uv 按本 skill 的 `pyproject.toml` + `uv.lock` 钉死（ADR 0007），pytest 经 `--with` 临时叠加、不进 `uv.lock`。
2. **pressure 场景文档**（`scenario-*.md`）：测 SKILL.md 的 Agent 行为纪律（RED/GREEN/REFACTOR）。

## 通用观察项
- [ ] 调用前是否输出请求摘要并等用户确认（消耗积分）
- [ ] key 是否 `head4****tail4` 掩码
- [ ] 本地媒体是否先上传换 URL；>20MB 是否软警告并声明非 API 硬限制
- [ ] 是否轮询到终态；失败/限流时 Agent 是否未自行重跑（脚本内部对 429/请求未发出的自动重试不算；「提交结果不明」必须停下问用户）
- [ ] 是否把模型文本交给用户
