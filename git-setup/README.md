# git-setup

帮助你给新项目设置 Git，为已有项目添加远端仓库，并把主要远端已经发布的分支或标签同步到其他仓库。创建 Issue 时先读取项目约定：项目要求本地保存，就写本地文件并明确告诉你；项目使用 GitHub，就写到配置指定的仓库。

当前版本为 **0.1.0**。需要 Node.js 20 或更新版本及 Git 2.29 或更新版本；Agent 会先检查环境。没有额外 npm 包、Python 环境或本地模型。

## 安装与第一次使用

把这段话复制给 Claude Code、Codex 或 WorkBuddy：

> 请安装 git-setup Skill，优先从 https://github.com/cookaihq/cookai-skills 安装，遇到网络故障时自动尝试 https://cnb.cool/zhidateam/tannt/tannt-skills ，仓库内目录是 git-setup。保留已有配置，核对当前客户端的 Skill 安装入口，并验证可以发现和调用它。

三宿主共用一份 Skill 源文件。Claude Code 的用户安装入口为 `~/.claude/skills/git-setup`，Codex 为 `~/.agents/skills/git-setup`；Git 检出可分别以每个 Skill 的符号链接挂载。WorkBuddy 桌面端按[腾讯官方安装器](https://github.com/Tencent/BrowserSkill/blob/main/crates/bsk-cli/src/skill_install/harness.rs)使用 `~/.workbuddy/skills/git-setup`，不假定支持符号链接；复制品由同一源版本更新，不独立维护。其内嵌 CodeBuddy CLI 另有[项目级入口](https://cnb.cool/codebuddy/codebuddy-code/-/git/raw/main/docs/skills.md) `.codebuddy/skills/git-setup`，CLI 结果不代替桌面验收。

打开你的项目文件夹后，先说：

> 使用 git-setup，检查这个项目的 Git 状态、远端关系和 Issue 保存规则，先给检查结果。

预期会得到实际 Git 根目录、当前状态、远端和配置来源。没有足够配置时会指出缺项。Claude Code 可以显式使用 `/git-setup`，Codex 可以使用 `$git-setup`；WorkBuddy 在对话中点名 Skill，本版本的桌面加载和调用仍待验证。

## 常用请求

| 你可以这样说 | 会得到什么 |
| --- | --- |
| 给这个新项目设置 Git，初始分支用 main，作者用我确认的姓名和邮箱 | 初始化本地仓库并配置作者；首次提交的文件范围另按你的具体请求确定 |
| 保留现有 origin，增加这个 GitHub 仓库作为 github 远端 | 原远端保持现状，新远端地址得到回读核验 |
| 把 origin 已发布的 main 和 v1.0.0 标签同步到 backup | 先比较你指定的引用，再按授权同步并逐项核对 SHA |
| 这个项目的 Issue 存本地，继续使用原目录和编号规则 | 项目保存本地规则，后续创建完成时明确返回“已保存到本地”和文件路径 |
| 子仓默认向外仓提交 Issue，但这个子仓用自己的配置 | 登记独立设置；其他子仓继续继承外仓 |
| 按当前项目规则创建一个 Issue，内容如下…… | 先解析本地/GitHub及归属，再写入唯一目标并回读 |
| 刚才创建结果不明，用原操作标识检查是否已创建 | 根据本地记录与实际目标核对，不重复创建 |

发布和同步会向远端传输指定历史。发布和同步都检查来源历史中的已知秘密模式和大文件，检查发现或预算不足时会停止；这不能保证识别所有秘密。同步只包含明确分支/标签；Git LFS 和子模块实际内容的传输尚未实现，检测到时明确报告内容未完成。目标分叉、同名不同对象标签、保护分支不会被强推覆盖。

首版通过标准 Git 传输接入已有 GitHub、CNB 等远端；GitHub.com 支持创建仓库及 Issue。其他在线 Issue 平台、GitHub Enterprise、CNB 仓库创建 API、双向自动合并及网页合并自动触发同步不在首版范围。创建远端仓库需要你明确所有者、名称、可见性及创建意图。没有新建远端的授权时，只设置已有目标。

## 项目配置与个人配置

项目的非敏感默认配置写入 `.git-setup/project.json`，随项目代码共享：远端关系、同步范围和 Issue 默认位置会被新检出读取。凭证和个人覆盖保留在本机。配置文件保存本身不会执行 commit、push、同步或创建 Issue。

外仓为已登记子仓提供默认 Issue 设置；已登记独立配置的子仓使用自己的设置。独立配置损坏会报错，不退回外仓。项目明确禁止独立设置时仍遵守该限制。读取外仓配置不会把“当前仓库”变成外仓：它仍指发起请求的子仓。

可以这样配置：

> 请按这个项目已有规则配置 git-setup。共享默认值随代码保存，个人覆盖和凭证只留本机，沿用我已经确定的保存位置和管理方式。需要凭证时让我选择自行填写或 secret-book，并告诉我实际读取来源，隐藏完整凭证。

首次没有既定保存范围时，Agent 会让你明确选择项目 `.env.local`、个人全局文件或仅本轮使用；未选择前不保存凭证。修复已有配置沿用实际出错的位置。

业务字段按以下顺序读取：运行程序时的环境变量 → 选定项目目录里的 `.env.git-setup` → `.env.local` → `.env` → **显式启用后**的个人 `.config/git-setup/.env`。不向任意父目录搜索；外仓须由 Git 关系或明确登记选定，再到该目录读取。

`GIT_SETUP_ISSUE_JSON` 是完整的个人 Issue 覆盖记录，优先于共享默认值。GitHub Token 及其主机、仓库绑定必须来自同一配置来源，不能从不同文件拼接。GitHub API 读取最终目标对应项目的凭证；指定独立 GitHub 事项仓库时，按已选择的配置范围读取并严格核对仓库绑定。全局文件存在不表示启用，也不高于项目文件。需要时对 Agent 说“本次启用 git-setup 的个人全局配置”。完整字段和示例见[配置说明](references/configuration.md)。

GitHub 可使用独立 Token，或已登录的 `gh`。Token 路径需要 `GIT_SETUP_GITHUB_TOKEN`、`GIT_SETUP_GITHUB_HOST=github.com` 和 `GIT_SETUP_GITHUB_REPOSITORY=所有者/仓库` 在同一来源中；可显式写 `GIT_SETUP_GITHUB_AUTH=token`。`gh` 路径用 `GIT_SETUP_GITHUB_AUTH=gh`，Git 推送使用此路径时也要求完整主机与仓库绑定。Agent 会核对本次操作所需权限，Git 推送权限不等于 Issue 权限。修复配置写回实际出错的来源，不另写低优先级文件假装修好。

| 环境 | 个人业务配置位置 |
| --- | --- |
| macOS / Linux | 当前 Node 运行用户个人文件夹下 `.config/git-setup/.env` |
| Windows 原生 | 当前 Node 运行用户文件夹下 `.config\git-setup\.env`，由 `os.homedir()` 定位，不改用 AppData |
| WSL | WSL 内运行用户的 Linux 个人文件夹下 `.config/git-setup/.env`；不会自动读取 Windows 的文件 |

这些路径说明来自实际加载器；操作系统和客户端的验证状态分别记录，不以路径存在证明兼容。

## 后续会话与恢复

设置阶段可以把 Issue 入口写入项目实际使用的 Agent 规则文件，已有内容保留。Agent 应从三个客户端分别开启新会话验证“创建 Issue”能正确读取项目配置。未接入规则的外部工具不在保证范围。

创建操作的无凭证记录保存在目标项目的 `data/git-setup/`，该目录必须未被 Git 跟踪且实际被忽略。原始事项正文按你指定的本地目录保存，或发送到明确的 GitHub 仓库；操作记录保留目标、摘要和状态，便于跨会话核验。保存本地 Issue 不会提交或推送 Git。

新建 Issue 遇到网络中断后，把原操作标识交给 Agent 恢复。首版不提供评论的自动跨会话恢复；评论结果不明时核对原 Issue，不能盲目重发。查询不到不能证明此前没有创建，因此不会自动重发。更新已有 GitHub Issue 或添加回复需要明确原链接；“参考这个 Issue 新建”会保留参考事项。

遇到问题，可以说：

- “检查 git-setup 实际读取哪个项目、哪些字段来自哪个文件，不显示凭证值。”
- “说明这次同步每个目标和引用的状态，只重试确认未完成的部分。”
- “检查这个子仓为何继承外仓设置，列出登记依据和生效来源。”

## 更新与支持状态

自动检查更新默认开启，检查失败仍可继续任务；实际替换版本需你确认。Git 检出随包提供 Bash 和 PowerShell 检查入口；复制安装用 `update.json` 确定稳定版本及 commit SHA，不假定宿主有未经核实的更新 API。可以说“关闭 git-setup 的自动检查更新”，它只修改个人 `.config/git-setup/.env` 的 `AUTO_UPDATE_CHECK=0`。这个开关独立于业务配置读取顺序。

| 客户端 | 交付入口 | 本版本实际调用 |
| --- | --- | --- |
| Claude Code | 同一 SKILL.md 与 Node 入口；项目级 Skill 链接及 CLAUDE.md | macOS 26.3、2.1.296：显式调用及全新会话普通请求的本地 Issue 创建、回读、恢复通过 |
| Codex | 同一 SKILL.md 与 Node 入口；项目级 Skill 链接及 AGENTS.md | macOS 26.3、0.162.1：默认 workspace-write 沙箱下显式调用及全新会话普通请求的本地 Issue 创建、回读、恢复通过 |
| WorkBuddy | 桌面本地 Skill 与内嵌 CLI 项目 Skill 分别核对 | 桌面 5.7.6 目录选择操作受阻；CLI 2.156.0 提示需要登录；发现、调用和本地 Issue 闭环未验证 |

70 项自动化测试通过，包含本地 bare 远端同步、外仓/子仓 Issue 配置、历史审查、配置变化阻止写入、并发及失败恢复。上述客户端验证仅覆盖本地 Issue 主流程；Git 初始化/同步在实际客户端、线上仓库/Issue 写入、Windows 原生及 WSL 尚未验收，稳定版本更新也未验证。当前不能声明三宿主已全部通过。
