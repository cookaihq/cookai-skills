# Git 接入、初始化、克隆、发布与多远端同步

`scripts/git-setup/git.mjs` 只使用 Node.js 内置模块和 Git；需要 Node.js 20+、Git 2.29+。运行时会真实执行 Git 并检查版本。缺少系统程序时报告安装方式，不自动安装。

## 操作顺序与授权

Agent 先读取项目规则，用 `inspect(project)` 识别实际根目录、外仓/子模块/Worktree、提交作者、暂存区、未提交文件、远端和 upstream。提交作者与访问远端的账号分别核对。

设置 Git 不等于提交全部文件。首次提交前，Agent 列明将纳入的文件、已有忽略规则、可能的秘密与大文件，取得本次操作已经需要的用户选择。既有暂存区不得自动清空；按明确文件清单执行标准 Git 暂存与提交工具，不使用未经检查的 `git add .`。检查范围必须说明，不能承诺识别了所有秘密。初始化不会创建占位文件、空提交或自动推送。

公开或首次向新远端发布前还要检查将发布的历史；`.gitignore` 不会清除已经提交的秘密。发现远端独立历史、分叉、同名不同对象的标签，先说明实际差异和保留方案，不自动强推或删除。受到保护的分支被拒绝时，使用项目已有 PR 流程或明确报告阻塞；本模块不重设保护规则。

## GitHub origin 与 Orca Issue 提醒

这是 Agent 在 Skill 初检及本次配置同步后执行的检查，Claude Code、Codex、WorkBuddy 共用；Orca 是使用环境，不替代实际 Agent 宿主的判断。

1. 核对当前选中仓库的 `origin` fetch/push 实际地址，以及同步配置、项目脚本、hooks 或 CI 中将该仓提交送到 GitHub 的有效入口、目标和触发方式。标准 Harness 的已登记同步入口和项目自有同步机制都纳入检查，不要求一定自动触发。展示具体来源；仅有 GitHub remote、未接入的候选脚本或相似仓库名不算依据。地址按实际主机及完整仓库路径判断，覆盖 HTTPS 和 SSH；有 URL 重写、SSH 别名、fetch/push 分离或多地址时先核实，不按 remote 名称猜测。无法确定的部分标为未知，不宣称满足条件或 Orca 可识别。
2. `origin` 已确认指向非 GitHub 仓库，且已有上述 GitHub 同步机制时，展示脱敏的原地址、GitHub 完整目标及同步依据，询问“保留现有 origin，还是把 origin 指向这个 GitHub 仓库”。多个 GitHub 同步目标先让用户选定；不能自行选第一个。已有添加/替换选择问题时合并询问。`origin` 缺失、已经指向 GitHub，或未发现有效 GitHub 同步机制时，不凭此规则建议切换。
3. 提醒中必须明确：**只有项目的 `origin` 指向 GitHub 仓库，Orca Workflow 的任务中才能显示当前项目的 Issue。** 用户已说明使用 Orca，或当前会话有可靠的 Orca 上下文时，直接解释这项影响；其他环境使用“如果使用 Orca”的条件表达，不为了此提醒额外要求安装或检测 Orca。当前范围是 GitHub.com，不据此承诺 GitHub Enterprise 可被 Orca 识别。改绑是显示的必要条件，不证明账号权限、Issue 列表加载或界面验证已通过。
4. 用户选择保留或尚未答复时保持绑定；事实和范围未变的明确选择继续沿用，不反复劝改。选择切换后，先按现有设置流程列出原仓库保留方式、分支跟踪/推送设置、同仓 Worktree，以及 Harness、脚本、hooks、CI 的具体影响，再执行用户已明确接受的方案。例如 `origin(CNB) → github(GitHub)` 不能只替换 origin 后变成 GitHub 向自身同步：须明确原 CNB 地址由哪个名称保留、同步来源/目标如何对应，并同步维护实际调用配置，不能擅自反转同步方向或丢弃原平台目标。远端名称及配置变化继续遵守项目规则和既有授权；暂未明确的部分先保留。
5. 替换继续使用已有 `setup` 计划、鉴权、指纹及影响核验流程，不把提醒当作改绑、推送或同步授权。完成后回读实际 fetch/push 地址及受影响的同步入口；没有实际检查 Orca Workflow 界面时，只报告 Git 配置结果。此提醒不迁移 Issue，不从 `origin` 推断 Issue 写入目标，也不让普通 Git 设置依赖 Issue 模板。

## 模块接口

所有接口为异步函数。前置条件失败抛出含 `code` 的错误。发布和同步返回逐远端、逐引用结果，不能仅凭命令退出或某一个目标成功宣布全部完成。

| 接口 | 输入与行为 |
| --- | --- |
| `inspect(project, options)` | 只读检查。返回实际根目录、仓库类型、HEAD、分支、upstream、作者、远端 fetch/push URL 和工作区状态；URL 凭证及常见 GitHub Token 脱敏 |
| `inspectRemote(project, url, options)` | 实际执行 ls-remote，返回可见引用、SHA、默认 HEAD 与 empty；不安装 remote、不 fetch，不将失败或仅缺 HEAD 视为空仓 |
| `setup(project, options)`（setup.mjs） | 读取本地 Git、远端及实际 Harness 策略并计划初始化/克隆/接入；apply 必须提供本次 expectedFingerprint。默认不提交或推送 |
| `init(project, {branch, authorName, authorEmail})` | 三个字段必须明确；只初始化无 Git 的目录，并写仓库级作者。目录已属于任何 Git 仓库时拒绝，避免误建嵌套仓库 |
| `addRemote(project, {name, url})` | 相同名称与相同 fetch/push 地址时不修改；地址冲突时停止。不替换 origin、不改变 upstream、不修改全局 Git 配置 |
| `replaceRemote(project, options)` | 只由已选 replace 计划调用，核对 expectedFetch、expectedPush、expectedState；在 Git config.lock 内准备后原子替换选中远端 url/已有 pushurl。配置来自 include/全局/Worktree 独立文件时停止，其他设置保持原样 |
| `cloneInto(project, options)` | 核对目录完全为空，用已存在的明确分支原地 clone，核对 HEAD 为计划 SHA；不递归子模块，跳过 LFS 下载，只执行一次。失败保留实际状态，不清理目录 |
| `publish(project, {remote, refs, ...options})` | 将完整本地引用清单的对象推送到指定远端；不提交、不设置 upstream、不推送额外标签、不强推。每次写入后回读 SHA |
| `sync(project, config, options)` | 从来源远端实际已发布引用取得快照，再逐目标复制约定引用。不同目标独立报告；重新调用时读取真实状态，只补齐缺项 |

配置中 `sync` 为 `{source: 'origin', targets: ['backup'], refs: ['refs/heads/main']}`。分支、标签必须使用完整引用，不接受通配符或隐式“所有”。`remotes` 为可选的 `{origin: '<URL>', backup: '<URL>'}` 声明；提供时必须与实际 Git 地址一致。一个 remote 多个读取/推送 URL、mirror 配置、带凭证 URL 或不支持的 remote helper 都被拒绝。

CLI 的发布入口先执行完整历史内容检查，再将检查结果的引用快照作为 `options.expectedRefs` 传给 `publish`。模块在网络操作前核对所有引用仍与该快照一致；发生变化时拒绝发布，并要求重新检查。推送使用已核对的对象 ID，不在执行时重新解析移动中的本地分支。

实际 `sync` 在取得来源远端的精确对象后，同样通过 `auditSnapshot(project, snapshot)` 检查这些对象及其全部可达历史，任何目标 push 前必须完成。来源在私有远端已发布不能豁免检查；不会用本地同名分支代替来源对象。发现已知秘密、大文件、浅历史或检查预算不足时返回 `audit_blocked` 或明确检查错误，所有目标均不写入；本次临时引用仍被清理。审查读取禁用 Git replace 和 grafts，确保检查原始对象；总检查预算为 120 秒。

`options.dryRun: true` 只用于 `sync`，读取来源和目标引用，使用已有本地对象比较历史；缺少对象时返回 `needs_history_fetch`，不 fetch、不 push、不更新本地引用。`audit.status` 为 `not_performed`，该计划未执行来源历史秘密/大文件审查，也没有完成 LFS/子模块内容检查，不能报告审查通过或项目同步成功。

## 鉴权与超时

GitHub HTTPS 与 CNB HTTPS 的实际网络入口调用 `auth.mjs`，按完整远端读取已选来源，缺少选择或凭证时拒绝访问；首次流程见 [仓库鉴权](authentication.md)。CNB SSH 明确拒绝。其他平台与 GitHub SSH 保留已有项目鉴权。

`options.authForUrl(url)` 供已经核验计划的调用方复用同一选择，返回 `{env, metadata}`。每次远端读取、fetch 和 push 前重新读取并检查来源；单次传输与持久设置共用 credential helper 协议。凭证仅通过环境交给该次 Git/helper 进程，不进入命令参数或 Git 配置；helper 校验主机和完整路径。`setup` 安装仓库级持续 helper，原生 Git 后续也读取同一来源，来源失效时拒绝回退。

本地 Git 命令最多 15 秒，远端引用读取每次最多 60 秒，传输每次最多 300 秒。只读网络操作只对明确瞬时错误尝试最多三次，退避为 1 秒和 2 秒，并记录脱敏重试原因。确定性鉴权、权限和仓库不存在错误不重试。脚本每项只调用一次 git push；Git/libcurl 在完全未收到 HTTP 响应时可能在该调用内重发传输，不能把脚本单次调用当成网络层只发送一次。即使超时或命令失败，也先回读实际引用。无法回读或结果不一致时报告 `uncertain`/`failed`，不重复写入。

## 项目 Harness

公开 CLI 与项目入口的 check/read/publish/sync 接受 `--repository <登记路径>` 和可选 `--checkout <该仓检出>`；公开 CLI 的 `--project` 指安装 Harness 的管理仓。预览列出安装位置、实际检出、登记依据、远端、完整 refs、鉴权来源和指纹。每仓读取自己的策略，未选择时只操作入口所属项目；错误选择不回退外仓。详见 [选仓流程](harness.md#从一份-harness-选择一个仓库)。

仅在 Agent 明确调用 Skill 时，按 [标准 Harness](harness.md) 先读取实际宿主规则和调用链、比较标准。采用后使用项目规则登记的实际 `run.mjs`；位置由计划指定，不预设目录。发布/同步先预览，再以同参数和指纹执行，项目扩展使用稳定 operation-id。公开 CLI 的 publish/sync 使用已登记位置，也可显式传 `--harness-directory`；日常入口只核对必要运行文件、目标和来源，不调查无关说明。普通 URL 设置不读取 Issue 文档或旧项目 JSON，不维护远端地址副本；Harness 同步缺策略或损坏时停止。

## 同步数据流与完整性

脚本读取主要远端的显式引用及 SHA；获取对象时只写本次 `refs/git-setup/<随机操作标识>/` 临时引用，不修改用户分支、标签、remote-tracking refs 或 `FETCH_HEAD`，结束时删除本次临时引用。用户工作树和暂存区不参与同步。未发布的本地分支/标签不会进入来源快照。

每个目标先读取实际 SHA。同名标签对象不一致时拒绝覆盖；已有分支必须能快进到来源提交。push 显式使用 `--no-force`，且关闭隐式跟随标签和子模块递归推送。写入后再次读取目标 SHA，全部目标结束后再次读取来源；来源发生变化或无法回读时，旧快照不能被报告为当前来源已全部同步。

`results` 中 `verified` 表示该引用回读值正确；`conflict` 表示历史或标签冲突；`failed` 表示操作失败；`uncertain` 表示写入结果无法可靠确定。`refsComplete` 仅描述约定 Git 引用。检测到浅克隆、LFS 规则/指针、子模块时，`contentWarnings` 列明未覆盖内容，`complete` 保持 `false`。本模块不传输 LFS 对象或子模块仓库，也不复制 Issue、PR、Release 附件、平台配置或部署状态。

发布和同步还检查约定引用的历史中已删除的 LFS 规则/指针与子模块 gitlink。历史检查同样受超时和输出预算限制；无法完成时输出 `content_history_unverified`，不报告项目内容完整。普通 `inspect` 仅检查当前 HEAD 的内容，不替代发布前完整历史检查。

调用方应把本次计划、结果和需要继续的目标保存到项目允许的运行数据目录，并在恢复时重新查询真实状态。CI 通过与 Git 引用同步成功分别核验和汇报。

## 验证

`tests/git.test.mjs` 使用临时本地工作仓和 bare 远端，覆盖初始化边界、remote 幂等、来源引用范围、注解标签、FETCH_HEAD/本地引用/暂存区保留、只读计划、目标部分失败与恢复、分叉和标签冲突、显式发布、LFS 未覆盖状态及配置漂移。测试仅在被忽略的 `tmp/git-setup/` 下创建夹具；通过 `GIT_SETUP_TEST_ROOT` 可明确指定当前工作区的该目录。

这些测试不证明 GitHub/CNB 线上权限、实际分支保护或三宿主会话已经通过；相关结果由交付验证记录分别说明。

## 仅 URL 入口、确认与恢复

Agent 使用 `setup --url <URL>` 取得 `needs_input` 或 `planned`。已有任意 remote 时先要求 `--action add|replace --name <名称>`，即使已有 URL 相同。只有用户明确作出且范围未变的选择，或明确继续中断的同一次设置，才可复用参数；自动采用 origin、selection 字段、计划记录和成功结果均不等于用户选择。没有 remote 默认 origin；完成后再次仅发送 URL 且从未明确选择时仍须询问。`--apply --expected-fingerprint <本次指纹>` 才执行写入，不继承旧计划的状态假设。

替换保留名称，计划包含当前 fetch/push、所有分支 remote/merge/pushRemote 设置、remote.pushDefault、本仓和已验证管理仓中受影响的 Harness 目标及 sync、Git common config 对应 Worktree。相关 Harness 策略或其他 Worktree 受影响时需要 `--accept-effects`。该开关不授权同步、Issue 迁移或推送。旧 refs/remotes 和 remote HEAD 原样保留，本次不 fetch，不宣称历史关系已验证。多地址、mirror、非仓库本地的 URL 来源不能通过本入口替换。

“空目录”按 `readdir` 的全部条目判定，隐藏文件也算内容。查询成功且没有任何可见引用才是 empty；HEAD-only、自定义引用、只有标签都不是空仓。clone 需要远端声明且实际存在的默认分支，或明确的 `--branch`。原地克隆后返回 `needs_project_rules`（退出 2，尚未完成设置）；Agent 先读新取得的项目规则，再沿用 `add` 和原名称重新计划，合并远端信息并完成回读。此阶段不增加用户确认，只读取并执行适用规则。

独立文件鉴权在无 Git 项目中先准备 `.git` 和受保护凭证，因此由本机 `gitsetup.initialization` 记录区分这种未完成初始化。`setup` 只排除本次 Git 元数据及确切凭证文件检查其他内容：有其他文件且远端有历史时仍停下；只有准备文件时获取远端分支/标签并安全检出，返回同一个 `needs_project_rules`。检出拒绝凭证路径冲突，使用 `--no-overwrite-ignore`；远端为空则明确作者和分支后完成初始化。该分支不调用要求空目录的 `git clone ... .`，不制造本地提交。记录、HEAD、索引、目标及来源参与恢复校验，设置成功后清除初始化记录。

`--plan-file` 可将不含凭证的 request/result 写入已有调用工作区中已忽略且未跟踪的 `data/git-setup/<任务>.json` 或 `tmp/git-setup/<任务>.json`。等待选择前保存 `needs_input`；具体方案保存 `planned`；首次变更前保存 `applying`；最后保存 `configured`、`needs_project_rules` 或 `incomplete`。成功 configured 后，同一文件只保留目标、完成步骤、指纹和完成时间；等待和中断仍保留恢复所需完整状态。没有跨轮需要时不传 plan-file，也不预建目录。文件写入使用锁和原子替换，读取回验；不自动改 .gitignore。真正空的克隆目标不能用于保存计划。Agent 的实际用户授权另行记录，`planned` 本身不证明批准。

状态记录用于查证，不自动重放。异常返回 failedStep、completedSteps 与本地回读；进程被终止时可能停在 applying，须重新读取真实状态。Git config.lock 残留时先查正在运行的操作，禁止自动删除锁。已 init/已配置远端时保留用户选择，生成新指纹补齐余下步骤；克隆中断须核对 HEAD、索引与检出文件，不能把存在 .git 视为完整成功，不能自动删除或盲重试。

configured 证明本地设置和远端读取；支持的 HTTPS 目标还包含已选来源及持久 helper 核验。GitHub 账号以 `/user` 结果为准，CNB 仅验证 Git 读取。推送预检与实际推送均未执行，不暂存、提交或推送。Agent 的实际完成回复必须含“本次暂未提交和推送。”，包括同地址核验和续作。无关 Issue 文档缺失、不可读或变化均不阻塞普通 Git setup；旧 JSON 损坏和旧写锁不读取或删除；LFS/子模块内容限制继续适用。`tests/setup.test.mjs` 从 CLI 入口核验这些行为；客户端最终回复另作实际验收。
