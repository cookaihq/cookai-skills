# 宿主机制与证据边界

每次报告区分安装记录/文件、规则入口存在、实体解析、实际会话加载、行为验证。版本未知、JSONC/动态表达式不能解析、账户目录未选定时标待核实；文件存在不意味着宿主读取。

| 产品 | 已核实机制与来源 | 本工具边界 |
| --- | --- | --- |
| Codex | [官方 AGENTS 指南](https://developers.openai.com/codex/guides/agents-md/)，2026-11-07 读取：`CODEX_HOME` 默认 `.codex`；全局/项目每层首个非空 override、AGENTS、配置回退；根至 cwd，受字节预算限制 | 检测多个 PATH/已知桌面实例、配置 Profile 和默认模型；额外 fallback 名、实际启动覆盖和加载需继续核对 |
| Claude Code | [Memory / AGENTS](https://code.claude.com/docs/en/memory#agents-md)；本机 2.1.312 既有调查：原生 AGENTS 从 2.1.277 起，受项目 CLAUDE 文件及 Project instructions 模式影响 | `.claude/CLAUDE.md`、rules、项目/子目录入口；托管策略、imports、排除和条件路径需会话验证。2026-11-07 文档端请求 403，未把它当作最新独立核验 |
| WorkBuddy | 5.9.3 桌面内 CLI 2.165.0 的 `MemoryLoader.loadUserMemories` 加载 CODEBUDDY 与 rules；`main/authority-snapshot.js` 的 SOUL 模板说明 `AI persona`，读取 IDENTITY/USER/SOUL | 配置目录可被环境覆盖，账户 `user-*` 目录作为候选；SOUL/IDENTITY/USER 身份文件单列，MEMORY 单列；其他版本/实际活跃账户待验证 |
| Hermes | [固定源码 prompt_builder.py](https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/agent/prompt_builder.py)及同提交 `hermes_constants.py`、`hermes_cli/profiles.py` | Windows 默认 LOCALAPPDATA/hermes，其他原生系统 `.hermes`，支持 suffix/home/Profiles；active_profile 是配置证据，不代表当前会话。SOUL 是身份，无虚构的全局工作 AGENTS。项目优先 Hermes 文件，再 AGENTS 链，再 cwd CLAUDE，再 Cursor |
| OpenCode | [1.19.5 instruction.ts](https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/session/instruction.ts)，2026-11-07 读取 | XDG 配置 AGENTS 存在即优先；否则可回退 `.claude/CLAUDE.md`，禁用 Claude prompt 时不回退。新建专用 AGENTS 会改变回退。instructions 的 URL、glob、JSONC 及项目禁用开关需继续核对 |

宿主设置仅读取已知模型/路径元数据；不要读取或输出登录凭证。配置默认模型、可证明的本会话模型、手工审阅目标各有来源。CLI/桌面、Windows/macOS、不同安装/Profile 的验证不能相互代替。

产品目录另列 Gemini CLI、GitHub Copilot、Cursor、Windsurf、Cline、OpenClaw 为“尚未支持”，不判断它们是否已安装。

项目索引提供根、子目录及显式引用的范围证据。自由文本只能作为读取建议线索，宿主是否实际读取以该版本的原生上下文查看或最小会话行为验证。对身份文件可提出专门编辑计划；整篇工作规则同步不包含身份或记忆文件。
