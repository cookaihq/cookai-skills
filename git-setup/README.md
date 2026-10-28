# git-setup

帮你为项目设置 Git，并把“这个项目怎样用 Git”写进项目说明、从 Agent 规则中引用。后续 Agent 会按说明定位仓库、读取凭证并执行获授权的操作。

当前版本 **1.0.1**。需要 Node.js 20+、Git 2.29+；没有额外 npm/Python 包或本地模型。普通终端、IDE、CI 使用既有鉴权，不会自动读取 .credentials/repositories。

## 安装与第一次使用

复制给你的 Agent：

> 请从 https://github.com/cookaihq/cookai-skills 的 git-setup 目录安装 Skill，核对当前客户端实际支持的安装入口，保留已有配置，验证能发现并调用它。网络失败时可检查镜像 https://cnb.cool/zhidateam/tannt/tannt-skills 。

Claude Code 用户目录为 ~/.claude/skills/git-setup，Codex 为 ~/.agents/skills/git-setup；Git 检出可分别用每个 Skill 的符号链接挂载。WorkBuddy 桌面按其实际安装器使用 ~/.workbuddy/skills/git-setup，项目入口可为 .codebuddy/skills/git-setup；不能假定其 symlink 支持或混用桌面与内嵌 CLI 的验收结果。三者使用同一份 Skill 源文件。

打开项目文件夹后说：

> 使用 git-setup，检查这个项目的 Git 设置和实际 Agent 规则，给出需要修正的地方。

Claude Code 可用 /git-setup，Codex 可用 $git-setup，WorkBuddy 在已加载 Skill 的对话中点名。预期先得到 Git 根、remote、分支与凭证来源的无值报告；仅检查不改项目。

## 给项目接入远端

> 使用 git-setup，为当前项目接入 https://cnb.cool/<组织>/<仓库>，按项目规则完成设置，并留下后续 Agent 能使用的 Git 说明。

已有 remote 时先明确替换哪个或以什么名称新增。没有既有文档约定时，说明放在 docs/agent/git.md；已有路径就更新原文档。实际 Agent 规则入口引用同一正文，保留链接与原有规则。

如果项目的 `origin` 指向 CNB 等非 GitHub 仓库，同时已有将提交同步到 GitHub 的机制，Agent 会展示两个仓库及同步依据，询问是否把 `origin` 改指 GitHub。**只有项目的 `origin` 指向 GitHub 仓库，Orca Workflow 的任务中才能显示当前项目的 Issue。** 你可以保留现状；选择切换时，Agent 会先说明如何保留原仓库和同步关系，再按已接受的方案调整。仅有 GitHub remote 不证明已配置同步，Git 配置完成也不代表 Orca 界面已经验证。

可以说：“我在 Orca 中使用这个项目，请用 git-setup 检查 origin 和同步到 GitHub 的机制，说明 Workflow 的 Issue 显示条件，先让我选择是否调整 origin。”

GitHub 默认使用项目为该仓库配置的 Token，并验证本次能否使用；已有 Token 时不会再让你二选一。全局 gh 已登录时，Agent 可以告诉你这个情况；只有你明确说“本次使用全局 gh 的某账号”，或此前已确认的选择仍适用于这个目标和范围，才会使用该账号。当前活动账号不会自动成为本项目的选择。

项目 Token 缺失、失效或权限不足时，Agent 会说明具体来源和问题，按你的决定处理，不自动换用全局 gh。新文件的准备与保存仍按你明确的授权执行。CNB 文件保存一行带用户名和 Token 的 URL：

    https://cnb:<TOKEN>@cnb.cool/<完整仓库路径>

用户名是 cnb，Token 中的 URL 特殊字符须编码。需要新文件时，Agent 会先准备空文件、提供完整可点击路径和格式，等你回复“已填写”再验证；项目已有凭证直接读取并做本次验证。不要把 Token 发到对话里。文件须未跟踪且被 Git 忽略；macOS/Linux 目录 700、文件 600，Windows 核对私有 ACL。真实 remote 始终保存不含用户名和 Token 的地址。

需要记住机器自己的账号或自定义文件时，可在主检出的 .credentials/repositories/sources.local.md 记非敏感来源；普通路径已能从项目说明确定时不额外记录。同一文件内换 Token，下次直接验证当前值，无需重新绑定。缺失或失效时修复原来源，不自动换账号。

设置仅完成 Git 配置和远端读取，回复会注明“本次暂未提交和推送。”。不会因此提交、推送、创建线上事项或改 CI。读取成功不证明有推送权限。

## 后续使用与排障

- “按项目 Git 说明，把已批准的提交推到这个远端，先核对具体引用。”
- “把来源远端已发布的 main 和指定标签单向同步到目标，保留目标独有引用。”
- “按项目现有规则新建 Issue，告诉我保存路径或线上链接。”
- “检查当前操作实际使用哪个凭证文件，只显示来源，不显示值。”
- “这次操作中断了，先核对真实结果，再继续确认未完成的步骤。”

发布、同步和 GitHub Issue 同样默认使用各目标的项目 Token，全局 gh 的明确选择要求相同。项目已有发布、PR 或 Release 脚本会在说明中引用，是否执行仍由具体请求决定。普通本地 Issue 沿用项目 tracker；GitHub Issue 支持原操作回读与恢复。网络读取设超时并有限重试；写入不明时先回读，避免重复。

## 配置位置与更新

工作文件夹为本次明确选中的项目根；Worktree 的凭证使用已核验主检出，子仓只有在管理关系明确时才使用管理仓主检出的目录。每个完整目标独立选择来源。项目文档或本次选择优先，可选本机来源记录只记账号/文件，不含 Token。

只有明确选择 GitHub environment 方式才按组读取：进程环境 → 当前鉴权项目根 .env.git-setup → .env.local → .env → 本次显式启用的个人 .config/git-setup/.env。不查父目录或其他 Skill。macOS/Linux 使用当前用户目录；Windows 原生使用 Node 识别的用户目录下 .config\git-setup\.env；WSL 使用自己的 Linux 用户目录，不自动读 Windows 文件。初次保存按用户明确作用域，修复沿用实际来源。

自动检查更新默认开启，失败不阻塞业务；实际更新逐次确认。可以说“关闭 git-setup 的自动检查更新”，它只设置个人 .config/git-setup/.env 中的 AUTO_UPDATE_CHECK=0。

**1.0.0 为不兼容升级。** 原持久 helper、鉴权绑定和生成运行包接口已删除。已有安装先清点并迁移实际调用方，验证后删除旧入口和无人引用的旧运行包；不会自动改真实凭证或删除其他 helper。可以说“将这个项目迁移到 Git 文档方案，先列出调用方与影响，再在我已授权范围内迁移并删除旧实现”。

## 支持状态

1.0.1 的三种离线来源场景已在 Windows 原生 Codex 0.165.0（workspace-write 沙箱）和 Claude Code 2.1.312 上验证：已有项目 Token 直接选用、缺失时不回退全局 gh、明确选择时使用指定 gh 账号。两端均检查了本次 1.0.1 源码；Claude Code 的同名个人安装可能优先加载，验收时须核对实际 Skill 路径与版本。WorkBuddy 5.8.3 启动后未提供可操作窗口，本次尚未完成该端调用验证。离线来源检查不证明线上凭证有效或写入权限。

已合入 0.5.0 的 GitHub origin 与 Orca Issue 提醒。该提醒的三个客户端实际会话及 Orca Workflow 界面尚未验证。

Claude Code、Codex、WorkBuddy 是必需目标，入口说明共用一份源文件。1.0.0 已在 Windows 原生合成仓库中验证 Codex 0.165.0 的设置和文档交付（保留 workspace-write、使用原生人工审批），以及 Claude Code 2.1.293 的凭证确认、跨会话续作和文档交付；Claude 文档示例经审阅修正后通过。未安装 Skill 的独立 Codex、Claude Code 2.1.312 会话已仅按项目文档完成鉴权读取。

WorkBuddy 桌面因本机连接不可用，尚未完成 1.0.0 的实际调用验收；完整三宿主矩阵、Windows 真实凭证私有 ACL 和默认 Schannel 路径仍待验证。本机自动化检查不能代替客户端验证。0.2–0.4 的历史结果仅属于原实现，尤其普通 Git 自动鉴权和移走 Skill 后运行包仍可用，已不属于当前承诺。Windows 原生、WSL、macOS/Linux 分别按实际证据记录，不能互相替代。
