# 仓库鉴权选择、文件准备和后续 Git

进入 GitHub HTTPS / CNB Git 设置、发布、同步或 GitHub API 操作前读取本文件。以下脚本均位于当前 Skill 的 `scripts/git-setup/cli.mjs`；选择由 Agent 在会话中取得，CLI 不提供终端菜单。所有路径以脚本实际解析结果为准。

## 选择与作用范围

首次 GitHub HTTPS 设置必须在会话中给出两个选项：

1. 使用本机 gh 已登录账号。
2. 使用这个仓库的独立 GitHub Token（保存于 `<实际完整凭证文件路径>`）。

先执行 `setup --project <项目> --url <URL>` 或 `auth-info --project <项目> --url <URL>`，从 `authentication` / 返回记录取得路径；展示时原样使用该完整路径，不能自行改成另一个系统的盘符或分隔符。宿主有会话选择组件就使用，否则列出文字选项。公开远端读取成功、系统 keychain、当前活动 gh 账号以及提交作者都不构成用户选择。已有相同仓库、目标、账号与来源的有效本机绑定可以复用；不能从外仓的 gh 选择推导子仓的选择。

将脚本的 `choices[].label` 原样用于选择组件，不能缩短成“独立 Token”而丢失完整路径。组件的短标题有长度限制时，完整标签放入该选项的说明或问题正文，确保用户作出选择前能实际看到目标文件路径。仅 URL 流程在发送组件前的正文也写出“本次暂未提交和推送。”；组件不代替必要的会话说明。

CNB 只使用 HTTPS 文件方式。不得尝试 SSH，也不扩展 CNB Issue 或仓库管理 API。GitHub SSH 与其他平台保留已有项目鉴权；SSH 不代表 GitHub API 已有身份。

## 文件位置

`repository-relationship.mjs` 供鉴权、设置和 Issue 共用；它读取 Git 主检出、子模块登记与实际 Git 元数据，独立仓读取管理仓 Harness 内的 `repos.conf`，不沿物理父目录查找凭证：

- 单仓：主检出根的 `.credentials/repositories/`。
- 外仓及其已登记子仓：明确统筹外仓的主检出根 `.credentials/repositories/`。多候选外仓须在会话选择后传 `--credential-workspace`，`--workspace` 指定同一个管理关系；同时传入时必须一致，不能分别指向不同管理仓。没有本机安装位置记录时同时传 `--harness-directory <管理仓内目录>`。
- 同一 Git 仓库的 Worktree 读取同一实体。外仓 Worktree 内独立初始化的子模块可能使用不同 Git 元数据，但经实际 `.gitmodules` 登记核验后，凭证仍位于外仓主检出。
- 独立 clone 没有外仓绑定时按单仓。已保存的主检出、外仓或 Git common directory 不可用时报告原位置，不创建替代来源。

独立仓只在管理仓实际 Harness 的 `repos.conf` 中登记，每行一个相对管理仓根的路径；不重复子模块，没有独立仓时不创建空清单。子模块必须同时通过 `.gitmodules`、gitlink 和实际 Git 元数据验证。同 URL 的独立 clone 不是同一仓库。旧 `workspace.repositories` 和 `gitsetup.repository` 不再提供登记依据；已有绑定只保存已选管理仓和位置，每次使用仍检查当前登记。失效、重复、缺失和歧义关系停止，不自动添加或迁移凭证。

每个完整远端一个文件：GitHub 为 `<owner>--<repo>.github-token`；CNB 使用完整组织/子组织/仓库路径，去末尾 `.git`，各级用 `--` 连接，再加 `.cnb-url`。例如 `zhidateam/han/obvault_tannt` 对应 `zhidateam--han--obvault_tannt.cnb-url`。相同子仓的 GitHub 和 CNB 分开保存。文件名碰撞会报错；不会因新规范改名或迁移旧文件。

## 独立文件准备与填写

取得用户的文件方式选择后，Agent 调用：

```text
node <skill>/scripts/git-setup/cli.mjs auth-prepare --project <项目> --url <URL> --auth-method github-token --apply
node <skill>/scripts/git-setup/cli.mjs auth-prepare --project <项目> --url <URL> --auth-method cnb-url --apply
```

沿用明确外仓时加 `--credential-workspace <外仓>`。沿用已登记旧文件时可传 `--credential-file <完整路径>`，这不授权新建其他位置或迁移文件。已有绑定不同须先明确替换决定，再传 `--replace-auth`；旧文件保留。

脚本在凭证实体所属仓库检查未跟踪、实际忽略及实体路径，拒绝符号链接和已跟踪文件。文件准备授权已包含忽略设置时可加 `--allow-ignore`，脚本只在该仓本机 `.git/info/exclude` 增加该文件规则；否则返回缺失的忽略条件。新目录在 macOS/Linux 为 700，新文件为 600，以排他方式创建为空；已有文件不覆盖，权限不符合时报出实际路径。Windows 原生使用同一路径规则，不把 POSIX mode 当作 Windows ACL 验收；该环境须单独验证。

首次创建后，以及跨会话恢复时重新请用户填写的**同一条会话回复**，都必须包含：

- 可点击的完整实际文件路径；使用返回的 `credentialLink`。恢复检查只返回 `credentialFile` 时，用该原始绝对路径生成 Markdown 链接；不以代码块、路径缩写或“复制路径打开”替代链接；
- 完整目标仓库及本次用途；
- GitHub：单行 Token 原文，不加引号、变量名或空格；
- CNB：单行 `https://cnb:<访问令牌>@cnb.cool/<完整仓库路径>`，用户名必须为 `cnb`，不加引号或变量名；
- 按下节列出本次所需权限；
- 请用户在本机填写，完成后回复“已填写”。不得要求把 Token 发到聊天。

在用户回复前停在 `awaiting_credentials`；文件非空不等于已确认。收到回复后才使用 `--credentials-filled` 继续计划和验证；已完成绑定以后不重复要求填写。`pending` 与来源信息保存在本机 Git 配置，换会话后通过 `auth-info --url` 恢复。

报告准备结果时区分三个状态：凭证文件是否新建或保留；本机 Git 配置是否已保存文件目标预留与待确认来源；远端设置、持久 helper 和网络核验是否已执行。`auth-prepare --apply` 成功会保存前两项，本阶段尚未完成后续接入；不能把“尚未添加远端”写成“未写任何 Git 配置”。恢复时先回读这些事实，不从文件非空推断用户已确认。

尚无 Git 的项目选择独立文件时，文件准备包含建立 Git 元数据及本机忽略规则。在本次已授权的准备范围内使用 `auth-prepare --allow-ignore --apply`：脚本先排他创建 `.git`，在本机 Git config 记录目标和未完成的初始化状态，再验证忽略并创建受保护空文件。此时没有连接远端，没有确定最终检出分支，也不算设置完成。只做计划不创建 `.git` 或凭证文件。

用户回复“已填写”后继续 `setup --credentials-filled`。远端为空时沿项目规则明确分支及作者并完成初始化；远端有历史且项目只有本次 Git 元数据和凭证文件时，使用选定来源获取真实分支及标签，再检出远端分支到当前目录。检出前拒绝远端占用凭证路径，禁止覆盖忽略文件，不创建额外提交。存在其他文件时返回 `local-files`，由用户明确保留及迁入方式。检出后仍返回 `needs_project_rules`，Agent 读取项目规则再完成 helper 和配置核验。

初始化记录与目标、凭证来源一起参与计划校验。网络中断后保留对象和状态，重新核对后恢复；已经有 HEAD、暂存文件或不匹配的记录时不会被当作普通接入绕过检查。完成设置后才清除该记录。空目录的 gh 分支仍可先读取远端并使用原生原地 clone；不能把已放凭证的目录直接交给 `git clone ... .`。

## gh 账号

选择 gh 后传 `--auth-method gh`。脚本真实执行 `gh --version` 和 `gh auth status --hostname github.com --json hosts`，不显示 Token。只有一个保存账号时使用该账号；存在多个账号时 Agent 展示实际账号，让用户明确选择，然后传 `--gh-user <login>`。不能默认采用 active 账号。

后续通过 `gh auth token --hostname github.com --user <已选账号>` 在进程内取得凭证，API 和 Git 使用同一账号来源。子进程排除 `GH_TOKEN` / `GITHUB_TOKEN` 覆盖，不执行 `gh auth switch`，不将 gh Token 保存到项目。账号不可用时报告选定账号，不回退其他账号。

## 按任务说明权限

GitHub fine-grained Token 选择实际资源所有者和目标仓库，按本次动作申请：代码读取需要 Contents: read；代码推送需要 Contents: write；推送涉及工作流文件时另核对 Workflows: write。Metadata 的读取权限由 GitHub 随仓库选择提供。Issue 查询使用 Issues: read，创建、更新和回复使用 Issues: write。创建仓库的权限见 [GitHub API](github.md)，不能为普通 Git 设置顺便申请创建仓库权限。组织审批和策略以 GitHub 页面为准。

CNB 令牌须覆盖完整目标仓库：读取需要代码只读权限，推送需要代码写权限。部署令牌仅适用于读取，不能用它完成推送。此处不要求创建 CNB 仓库或 Issue 的权限。

设置成功分别报告账号核验、远端读取、持久 helper 验证、推送预检及实际推送。当前 `setup` 不执行 push dry-run 或实际推送，二者均明确未执行；`sync` 的引用比较也不等于推送权限预检。GitHub 通过 `/user` 核验账号；CNB 只报告 Git 读取验证，不编造账号核验结果。

## 本机绑定与实际调用

`auth.mjs` 将完整目标、已选方式、实际主检出/文件路径、gh 账号、验证后的身份和非明文校验值保存到当前仓库的本机 Git 配置。Token 不进入该配置、remote、argv、共享 JSON 或计划。文件名预留记录保存在凭证外仓的本机 Git 配置，防止不同子仓将碰撞路径绑定给不同目标。

`setup` 在生成计划前检查选择与身份，计划指纹包含来源。执行时重新核对，安装持久仓库 credential helper，并通过原生 `git credential fill` 在进程内比对实际取得的凭证，不把其 stdout 交给 Agent。helper 使用明确的 Node 路径和本仓 Git 实际 common directory 下 `git-setup/<源码指纹>/` 的完整运行包；同仓 Worktree 共用，移除 Skill 后仍可使用。新 clone 需重新建立绑定；仓库、Node 或绑定路径变化时修复原来源。运行包无凭证，不自动迁移或删除旧文件。GitHub/CNB 主机的 helper 链先清空再设置；带完整仓库路径的请求才能取得相应文件或 gh 账号，未知目标、文件缺失、来源轮换或脚本失效会终止认证，不能退回 keychain、其他 helper 或 ASKPASS。已有额外 HTTP 鉴权头冲突时报告错误。

`publish`、`sync` 的每个实际远端和 GitHub API 最终目标分别查自己的绑定。API 先核验账号，再使用固定 `api.github.com` 接口；不会借用发起子仓或外仓账号。凭证轮换只修改原文件，再运行 `auth-verify --url <目标>` 取得新指纹，按已有授权加 `--apply --expected-fingerprint <指纹>` 更新校验结果。验证失败不切换来源，也不授权重发结果不明的业务写入。

只做 GitHub Issue/API 操作，或目标仓库尚未创建时，使用 `auth-verify --api-only`（首次填写加 `--credentials-filled`），再以相同选项和返回指纹执行 `--apply`。这会核验 `/user` 身份、保存选择并验证本机 helper，不要求 Contents 读取权限，也不尝试读取尚不存在的 Git 远端；报告 `remoteRead: not_performed`、`apiPermissions: not_verified`。具体 Issue/创建仓库权限由相应 API 实际请求验证，身份核验成功不代表该业务权限已具备。

旧环境/dotenv 配置仍按 [配置规则](configuration.md) 的分层加载器读取。显式 `--github-auth` 保留既有 Git 调用兼容；API 在没有本机绑定时仍可使用原本完整、匹配目标的 Token 配置。已有绑定优先决定精确来源，不会被另一份 Token 自动替代。进程 Token 可用于显式本轮操作，但不能作为持久设置完成的依据；不自动保存到新文件。旧 gh 配置需要补全明确账号。新文件默认流程不改成 `.env.local`。

生成项目入口的 auth-info/auth-prepare/auth-verify 接受 `--repository <登记路径>` 及可选 `--checkout`。入口固定管理仓与安装位置，先核验所选检出的归属，再交给相同鉴权模块；凭证仍按目标独立，不因共用入口而共用账号。

## 官方依据

- [Git credential helper 协议](https://git-scm.com/docs/gitcredentials)：空 helper 重置列表、useHttpPath、quit 阻止回退。实现核对 Git 2.29 文档。
- [gh auth status](https://cli.github.com/manual/gh_auth_status) 与 [gh auth token](https://cli.github.com/manual/gh_auth_token)：账号列表与 `--user`。
- [CNB Git 认证](https://docs.cnb.cool/zh/guide/git-access.md)、[访问令牌](https://docs.cnb.cool/zh/guide/access-token.md)、[部署令牌](https://docs.cnb.cool/zh/guide/deploy-key.md)：2026-10-13 查证。

## 项目环境工具的本机检查

项目自有环境导出器可以从已验证的生成运行包调用 `inspectAuth(project, url, {metadataOnly: true})`，只核对已选绑定和文件元数据，返回 `credentialContent: not_read`。这不能证明文件内容仍有效，不能替代 `authSelection`、`auth-verify` 或权限验证。导出器先明确拒绝 gh/legacy 等不支持的来源，再读取同一文件绑定，避免在拒绝前先取出系统登录凭证。普通 `auth-info` 保持原来的完整本机来源检查。
