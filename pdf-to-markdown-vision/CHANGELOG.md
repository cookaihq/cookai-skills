# Changelog

## 1.0.2 — 2026-11-14

- 相邻独立列表保留语义边界，在 Markdown 渲染后分别成组并留白；同一列表跨普通留白、装饰底色和条目换行仍保持连续，沿用原有标题分隔。
- 保持 reading-order 规则 ID、CLI、页 JSON 和清洗接口；未清洗及清洗结果均保留列表标记和来源映射。
- Windows 原生 Codex CLI 0.169.0 已在 workspace-write 默认沙箱中完成真实 MXO 3 页面及合成边界验收；CommonMark 和当前审阅页已用真实浏览器核验。macOS、WSL、Codex GUI、Claude Code、WorkBuddy 和下游检索未实测。

## 1.0.1 — 2026-11-13

- 简短图内文字进入图片替代文本，密集标注按对象归组；保留原图注、独立正文和表格，避免重复转写邻近已有文字。
- 图片区域允许空正文，替代文本保留字面特殊字符，输出行号与来源映射保持一致。
- 保持既有调用和清洗接口；旧任务升级后保留任务 ID、原件、规则快照和历史结果。
- Windows 原生 Codex CLI 0.167.2 已在 workspace-write 默认沙箱中完成受影响场景验收；macOS、WSL、Codex GUI、Claude Code 和 WorkBuddy 未实测。知识库需另验图片替代文本是否进入下游索引。

## 1.0.0 — 2026-11-11

- 使用宿主原生视觉转换 PDF，保留真实裁图、图内文字、表格条件和 LaTeX 公式。
- 保存原件、规则和清洗快照、区域关联、追加事件及可移动结果版本。
- 支持项目清洗方案、续跑、纠错、升级重跑、失败记录和结果回退。
- Windows 原生 Codex CLI 0.167.1 已在默认 workspace-write 沙箱中完成路径调用、真实样本、项目默认清洗、纠错、升级与同版本接续验收。
- 本机已验收，另一平台待验：macOS、WSL、Codex GUI 交互待验；Claude Code、WorkBuddy 尚未业务实测。
