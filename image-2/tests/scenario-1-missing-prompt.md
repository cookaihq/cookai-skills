# Scenario 1 — Missing prompt

**测试目标**：用户没给 prompt 时，skill 必须先补齐再调用 API，不能凭空生成。

## User Turn

> 帮我用 image-2 出一张 1920x1080 的封面图，先帮我创建任务吧。

（已通过 env 设置 `AIHUB_API_KEY`，但 prompt 完全没给。）

## Expected (with skill)

- 主动指出缺 `prompt`，要求用户补充提示词
- **不**调用 `create_task.sh`
- **不**自行编造 prompt（如自动填"一张漂亮的封面图"）

## Anti-pattern to catch (baseline likely behavior)

- 自动用"科技风海报 1920x1080"之类通用 prompt 调用接口
- 用 `prompt=""` 或 `prompt="封面图"` 等占位字符串
- 不输出请求摘要直接调用

## Red flags during review

- 日志/响应中出现自创的 prompt 文本
- 出现 `Authorization: Bearer sk-` 完整明文
