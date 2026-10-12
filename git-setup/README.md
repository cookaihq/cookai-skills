# git-setup

帮助你给新项目设置 Git，为已有项目添加远端仓库，并把主要远端已经发布的分支或标签同步到其他仓库。创建 Issue 时先读取项目约定：项目要求本地保存，就写本地文件并明确告诉你；项目使用 GitHub，就写到配置指定的仓库。

当前开发版本为 **0.2.0，尚未发布**；下面的常规安装来源目前提供 0.1.0，新增 URL 默认流程须使用包含本次改动的版本。需要 Node.js 20 或更新版本及 Git 2.29 或更新版本；Agent 会先检查环境。没有额外 npm 包、Python 环境或本地模型。

## 安装与第一次使用

把这段话复制给 Claude Code、Codex 或 WorkBuddy：

> 请安装 git-setup Skill，优先从 https://github.com/cookaihq/cookai-skills 安装，遇到网络故障时自动尝试 https://cnb.cool/zhidateam/tannt/tannt-skills ，仓库内目录是 git-setup。保留已有配置，核对当前客户端的 Skill 安装入口，并验证可以发现和调用它。

三宿主共用一份 Skill 源文件。Claude Code 的用户安装入口为 `~/.claude/skills/git-setup`，Codex 为 `~/.agents/skills/git-setup`；Git 检出可分别以每个 Skill 的符号链接挂载。WorkBuddy 桌面端按[腾讯官方安装器](https://github.com/Tencent/BrowserSkill/blob/main/crates/bsk-cli/src/skill_install/harness.rs)使用 `~/.workbuddy/skills/git-setup`，不假定支持符号链接；复制品由同一源版本更新，不独立维护。[CodeBuddy 官方文档](https://cnb.cool/codebuddy/codebuddy-code/-/git/raw/main/docs/skills.md)还提供项目目录 `.codebuddy/skills/git-setup`。本次 WorkBuddy 桌面实测加载的是项目副本，全局目录单独安装尚未验收；内嵌 CLI 与桌面的结果分别记录。

打开你的项目文件夹后，先说：

> 使用 git-setup，检查这个项目的 Git 状态、远端关系和 Issue 保存规则，先给检查结果。

预期会得到实际 Git 根目录、当前状态、远端和配置来源。没有足够配置时会指出缺项。Claude Code 可以显式使用 `/git-setup`，Codex 可以使用 `$git-setup`；WorkBuddy 在对话中点名 Skill，项目副本的加载及本次 URL 流程已验收，具体覆盖范围见下方支持状态。

## 常用请求

在 Agent 中打开要接入的项目后，可以只提供仓库地址，例如在 Codex 中输入：

> $git-setup https://cnb.cool/zhidateam/han/obvault_tannt

Claude Code 使用 `/git-setup <仓库地址>`；WorkBuddy 在对话中点名 git-setup 并附上地址。示例仅说明输入方式，不代表该 CNB 仓库已验证可访问或为空。

Agent 会读取当前项目状态和项目规则，查询远端，再按实际状态设置 Git。项目已有任何远端时，会列出全部远端，让你选择替换哪个地址，或以哪个新名称添加；相同地址也遵守这项选择。你已作出的明确选择会沿用。没有远端时使用 `origin`；这次自动采用的名称不算你已选择添加或替换，完成后再次只发 URL 仍会询问。

当前目录真正为空且远端有内容时，克隆直接落在这个目录。克隆后 Agent 会先读取取得的项目规则，再继续共享设置。当前目录已有文件而远端也有历史时，先与你明确文件保留及迁入方式。初始化缺少分支或作者时，只补问这些实际缺项。

默认终点是 Git 设置及远端读取验证。完成回复会明确写出 **“本次暂未提交和推送。”**，不会把接入成功说成代码已上传。已有 Issue 设置继续使用；没有设置时会说明，但不因此要求你新增 Issue 配置。替换会影响同名远端被分支、其他 Worktree、同步和 Issue 使用的目标，Agent 会展示具体影响；涉及同步或 Issue 目标变化时先取得你的选择。

| 你可以这样说 | 会得到什么 |
| --- | --- |
| 只发送 git-setup 和一个仓库 URL | 为当前项目接入该远端；已有远端先选择替换或添加，完成后明确暂未提交和推送 |
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

Git 设置需要跨轮确认时，Agent 会在已有工作区被忽略的 `data/git-setup/` 或 `tmp/git-setup/` 保存计划、已选目标与执行状态，告诉你实际位置；不把计划文件先放进待克隆的空目录。中断后让 Agent 读取原记录并重新检查实际 Git 状态，补齐未完成步骤。记录保存不代表操作已经完成；克隆失败不会自动删除文件或直接重跑。远端替换会保留原远端跟踪引用，本次不刷新引用或证明两边历史关系。

设置阶段可以把 Issue 入口写入项目实际使用的 Agent 规则文件，已有内容保留。Agent 应从三个客户端分别开启新会话验证“创建 Issue”能正确读取项目配置。未接入规则的外部工具不在保证范围。

创建操作的无凭证记录保存在目标项目的 `data/git-setup/`，该目录必须未被 Git 跟踪且实际被忽略。原始事项正文按你指定的本地目录保存，或发送到明确的 GitHub 仓库；操作记录保留目标、摘要和状态，便于跨会话核验。保存本地 Issue 不会提交或推送 Git。

新建 Issue 遇到网络中断后，把原操作标识交给 Agent 恢复。首版不提供评论的自动跨会话恢复；评论结果不明时核对原 Issue，不能盲目重发。查询不到不能证明此前没有创建，因此不会自动重发。更新已有 GitHub Issue 或添加回复需要明确原链接；“参考这个 Issue 新建”会保留参考事项。

遇到问题，可以说：

- “检查 git-setup 实际读取哪个项目、哪些字段来自哪个文件，不显示凭证值。”
- “说明这次同步每个目标和引用的状态，只重试确认未完成的部分。”
- “检查这个子仓为何继承外仓设置，列出登记依据和生效来源。”

## 更新与支持状态

自动检查更新默认开启，检查失败仍可继续任务；实际替换版本需你确认。Git 检出随包提供 Bash 和 PowerShell 检查入口；复制安装用 `update.json` 确定稳定版本及 commit SHA，不假定宿主有未经核实的更新 API。可以说“关闭 git-setup 的自动检查更新”，它只修改个人 `.config/git-setup/.env` 的 `AUTO_UPDATE_CHECK=0`。这个开关独立于业务配置读取顺序。

| 客户端 | 本次实际入口 | 0.2.0 URL 默认流程实测 |
| --- | --- | --- |
| Claude Code 2.1.296 | 项目 Skill 链接；`/git-setup <URL>` | 未初始化目录接入空远端、已有多远端先询问、选择替换及添加、空目录原地克隆通过；完成回复包含规定提示 |
| Codex 0.162.1 | 项目 Skill 链接；`$git-setup <URL>`；workspace-write 沙箱 | 同上，原有文件、暂存区和未选中的远端保留；完成回复包含规定提示 |
| WorkBuddy 5.7.7 桌面 | 已选项目的 `.codebuddy/skills/git-setup` 副本；对话点名并提供 URL | 无远端的已有 Git 项目接入、相同 URL 已有远端先弹出选择、选择替换后完成核验通过；完成回复包含规定提示 |

以上均在 macOS 26.3 上使用隔离本地 bare 远端，未访问用户示例 CNB 仓库。87 项自动化测试通过，覆盖原有同步/Issue 能力与新增 URL 设置、配置变化拦截、计划保存及失败恢复。WorkBuddy 复跑曾误把自动选择当成用户授权，补清 Skill 与脚本提示后已重新观察到实际选择框；不把计划文件作为用户批准。

0.1.0 的历史验收仅证明 Claude Code 与 Codex 的本地 Issue 创建、回读和恢复；WorkBuddy 本地 Issue 尚未验收。0.2.0 的 WorkBuddy 初始化、克隆和跨会话中断恢复，全局目录单独安装、内嵌 CLI（2.156.0 仍缺独立登录验收），三客户端的实际同步、线上仓库/Issue 写入、Windows 原生、WSL 及稳定版本更新仍未验收。不能把本次 URL 入口覆盖扩写为全部能力已通过。
