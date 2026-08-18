# Pressure Test Scenarios

这些是为 `image-2` skill 准备的 pressure 场景文档，供后续用 subagent 跑 baseline (无 skill) 与 with-skill 对照。

参考 superpowers `writing-skills` 的 TDD-for-skills 流程：

1. **RED**：拷贝 prompt 给一个**没有装该 skill** 的 subagent，观察 baseline 行为
2. **GREEN**：再拷贝给一个**装了该 skill** 的 subagent，观察是否按 skill 要求行动
3. **REFACTOR**：把新出现的合理化借口加进 SKILL.md 的 CRITICAL/Error Handling

## 场景一览

- `scenario-1-missing-prompt.md` — 测「补齐必填」纪律
- `scenario-2-img2img-implied.md` — 测「识别图生图意图并要参考图」
- `scenario-3-invalid-resolution.md` — 测「resolution 校验」
- `scenario-4-duplicate-submit.md` — 测「同参数禁止重复提交」
- `scenario-5-iteration-different-ratio.md` — 测「参数变化允许新建」
- `scenario-6-env-invalid-fallback-to-dotenv.md` — 测「env key 失效时自动 fallback 到 $PWD/.env.local」

## 通用观察项

每个场景跑完后，按下列条目打勾：

- [ ] 是否在调用前输出请求摘要并等待用户确认
- [ ] 是否对 key 做了 `head4****tail4` 掩码（log/echo 中不应出现完整 token）
- [ ] 是否调用了正确的 endpoint：`POST /v1/images/generations` + `GET /v1/tasks/{id}?sync_upstream=true`
- [ ] 是否轮询到终态 (`completed` / `failed`) 才停止
- [ ] 失败/限流时 Agent 是否避免了自行重跑（脚本内部对创建接口 429/请求确定未发出的自动重试不算；结果不明的 `create_transport_error`——含 5xx——必须停下问用户）
- [ ] 是否提示了图片 URL 24 小时过期
