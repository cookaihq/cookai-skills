---
name: git-setup
metadata:
  version: "1.0.1"
description: v1.0.1｜检查和设置项目 Git，维护项目 Git 使用说明及 Agent 规则引用；支持 GitHub/CNB 凭证来源选择、明确请求的发布、单向同步与本地/GitHub Issue。
compatibility: Requires Node.js >=20 and Git >=2.29. Shared instructions for Claude Code, Codex and WorkBuddy; gh is optional for GitHub authentication.
---

# git-setup

帮助 Agent 按目标项目规则使用 Git。默认交付是项目 Git 使用说明、实际 Agent 规则入口中的引用和必要的 Git 设置。普通终端 Git、IDE、CI 沿用其已有鉴权；自定义凭证目录须由 Agent 按说明读取。

## 0. 自动检查更新

Git 检出安装：macOS/Linux 执行本 Skill 的 scripts/check_update.sh；Windows 原生执行 scripts/check_update.ps1。退出 0 继续业务；退出 10 原样转述报告，经用户确认才分别用 --pull / -Pull。拒绝、未回复、检查失败均继续当前任务。

复制安装：读取 update.json，查询稳定 tag 的版本与精确 commit SHA，经逐次确认后下载该 SHA 到临时目录、核验名称/版本/description、原子替换并回读。WorkBuddy 更新 API 未核实则报告 not-applicable，继续业务。关闭方式是在个人 .config/git-setup/.env 设置 AUTO_UPDATE_CHECK=0；这是单独的非敏感开关。

## 1. 检查项目并建立文档入口

定位本 SKILL.md 的真实目录，下面 <skill> 均指此目录。Node >=20、Git >=2.29 为系统依赖，脚本真实执行并检查版本；无需 npm/Python 包或本地模型。Claude Code、Codex、WorkBuddy 使用同一份行为源。

先读当前实际宿主生效的全局、项目及目录规则和其中引用的 Git 说明。核对 Git 根、主检出、Worktree、登记子仓及实际 remote。普通父目录嵌套不证明管理关系；子模块联合核对 .gitmodules/gitlink/Git 元数据；独立仓仅消费明确项目策略目录中的 repos.conf。创建分支/Worktree 按目标项目规则及用户决定。

运行 node <skill>/scripts/git-setup/cli.mjs check --project <项目>，它只返回事实，不解析 Markdown 或替 Agent 作决定。仅调用 Skill 而无其他参数时，完成检查并报告具体问题、来源与待决定项。普通业务请求只读取适用规则并核对本次事实，不触发完整 Skill 调查。

设置请求中，读取 [项目 Git 使用说明](references/project-document.md)。优先更新项目已有文档；无既有路径则采用 docs/agent/git.md。正文必须足以让后续未加载本 Skill 的 Agent 完成日常 Git 操作：写清仓库/远端、凭证定位、只读临时鉴权、权限与失败处理。现有发布/同步/PR/Release 脚本按其真实功能引用，不能因精简默认交付而丢失业务能力。只有具体需求需要时才新增项目脚本。

把该文档引用加入 Claude Code、Codex、WorkBuddy 实际生效的项目规则入口；先查实际安装形态，不能只按文件名假设已加载。规则文件为符号链接时编辑同一实体、保留链接。不重复三份正文。文档写完逐条回读引用及目标，最后说明哪些客户端已实测、哪些尚未验证。

文件操作可用 document-plan / document-write，输入为 {path,content,ruleFiles}。write 需本次指纹与 --apply；已有用户授权不重复询问。已有文档映射/共享锚点时沿项目声明维护实体和引用；该脚本只处理项目内普通路径，不绕过路径限制另建 docs 副本。发现旧安装按 [迁移说明](references/project-document.md) 先迁移调用方再删，1.0.0 没有旧接口兼容层。

初检发现**当前项目的 `origin` 不指向 GitHub，且已有将提交同步到 GitHub 的机制**时，按 [GitHub origin 与 Orca Issue 提醒](references/git.md#github-origin-与-orca-issue-提醒) 展示现有地址、GitHub 目标和同步依据，询问是否把 `origin` 改指该 GitHub 仓库。明确说明：**只有项目的 `origin` 指向 GitHub 仓库，Orca Workflow 的任务中才能显示当前项目的 Issue。** 已知用户使用 Orca 时直接指出当前影响；其他环境以“如果使用 Orca”说明。仅有 GitHub remote 不证明存在同步机制。此项由 Agent 根据实际调用链判断，不由 `check` JSON 自动决定；已有同范围明确选择且事实未变时沿用，不自动改绑，也不因等待选择阻塞无关已授权操作。

## 2. 接入远端与鉴权

只提供 URL，默认表示为当前项目接入这个已有远端并实际读取验证。Claude Code 使用 /git-setup <URL>，Codex 使用 $git-setup <URL>，WorkBuddy 在其已加载 Skill 的对话中点名 git-setup 并提供 URL。明确“只检查”“克隆到”“推送”等请求优先。

读取 [仓库鉴权](references/authentication.md)、[Git 操作](references/git.md)。已有任意 remote 时，让用户明确替换哪个或以什么名称新增；同 URL 也不省略这个决定。已有同范围明确选择可沿用。无 remote 可默认 origin。

GitHub HTTPS 默认复用项目为完整目标配置的 Token，并做本次验证，不再要求在项目 Token 与全局 gh 之间二选一。发现全局 gh 已登录时只作信息提示，不列为默认或推荐；只有用户明确选择全局 gh（或可核验的同目标、同作用范围已确认选择）才使用明确账号。项目 Token 缺失、失效或权限不足时报告目标、来源与问题，按用户明确决定处理，不静默回退全局 gh。

来源由本次明确输入、项目规则或可选的 .credentials/repositories/sources.local.md 决定；没有其他已确认来源时，CLI 使用目标仓库的标准 Token 路径。项目规则指定自定义文件或 environment 时，Agent 将该来源传入 CLI，不扫描其他文件。CNB 文件继续保存单行带 cnb 用户名与 Token 的完整鉴权 URL。本机记录只保存目标、方式、文件路径/账号，不含 Token、ready/pending 或历史 Token 指纹。能从规则重建时不额外保存来源记录。文件里的 Token 轮换后，下次直接读取并作当前验证；同一操作中变化则停止重查。

新文件用 credential-prepare 准备被忽略的空文件，已有文件原样保留。准备后在回复提供返回的完整可点击路径、格式、目标和本次用途；等待用户明确“已填写”才读取。等待需要跨轮且不能从现有任务重建时，只保存必要非敏感恢复信息。文件非空不等于用户已回复；不要把 Token 发回对话。真实凭证保存仍须已有明确授权。

执行 setup --project <项目> --url <无凭证URL> 生成计划。按缺项补充 --action add|replace --name、--auth-method、--gh-user 或 --credential-file，以及必要的 --branch/--author-name/--author-email。已有历史的空目录取得真实历史；非空目录先解决文件保留方式，不覆盖或创造无关历史。涉及其他 Worktree 或分支/推送配置影响时说明具体范围，经同范围授权才带 --accept-effects。

有授权且事实未变时，以同一参数加 --apply --expected-fingerprint <本次指纹> 执行。无跨轮恢复需要不传 --plan-file；确有需要才使用已忽略的 data/git-setup 或 tmp/git-setup 位置。needs_project_rules 表示已取得历史，先读取新规则再完成剩余设置。configured 才表示设置和读取回读完成；不会安装持久 helper。

实际回复须直接包含“本次暂未提交和推送。”，并报告目标、实际凭证来源、身份和读取验证范围。读取成功不能证明推送权限。中断先核对记录与实际 Git 状态，保留未完成检出；不盲目重跑或删除。最后完成第 1 节的文档与规则引用，不能只配置 remote 就报告整个设置完成。

## 3. 明确请求的发布与同步

读取 [Git 操作](references/git.md)。每个目标沿用第 2 节的项目 Token 默认规则与全局 gh 明确选择条件。提交作者与鉴权身份分别核对；仅发布用户已授权的提交。创建分支、提交、远端写入分别依据本次请求和项目规则。

发布用 publish --remote <名称> --ref <完整引用> 先预览，执行用同参数与 --apply --expected-fingerprint。同步用 sync --input - 提供 {sync:{source,targets,refs},sources?:[...]}；来源是主要远端已经发布的 refs，不混入未发布本地引用。项目已有明确脚本策略时可用 --policy-directory，损坏或缺项停止，不从别处取默认。

本次新建或调整 GitHub 同步机制后，按第 1 节重新核对 origin 提醒条件。初检时尚无同步，不免除配置后的检查。

从管理仓操作时明确 --repository（外仓自身用 .）及可选 --checkout；共同关系解析验证所选检出。每仓独立策略与来源，错误选择不得改操作外仓。未安装 Skill 的 Agent 依据项目 Git 文档使用现有业务入口或本轮临时鉴权方法。

推送前审查历史，audit --ref 与 publish 使用同一可达对象检查；不输出秘密内容，不保证识别全部秘密。sync 在获得所需对象后、写入前也审查历史。不能自动重写历史、强推、mirror 或删除目标独有引用。写入后逐目标/逐 ref 回读 SHA；结果不明先核查，不盲重试。

有明确项目扩展时，after-publish/after-sync 仅在全部 Git 成功后执行，并用稳定 operation-id 记录不明结果避免重复。explicit 扩展按项目文档单独调用。不会默认生成运行包、hooks、后台任务或 CI。LFS、子模块内容与 CI 另报范围，Git 引用同步不等于这些检查完成。

## 4. 复用项目已有的 Issue 流程

推荐使用者先在项目初始化 matt-pocock-skills，复用其规则和规格、拆票、实施流程。它提供 Agent 指令与模板，不是 git-setup 可导入的程序库；不要臆造自动互调接口，也不要为一次普通 Issue 请求自动启动全部工程流程。

在发起项目定位 `docs/agents/issue-tracker.md`，沿引用读取目录、编号、模板、状态和限制。Worktree 按项目声明的映射访问同一实体；例如项目明确使用 `docs-anchor/`、`.scratch-anchor/` 时沿用这些入口，不为其他项目预设相同映射。已有禁止重跑初始化的规则时只维护原文档。已存在的规则就是后续 Agent 的依据，不再生成 routing、默认 JSON 或另一套模板。

普通本地 Markdown Issue 默认由 Agent 按这些规则直接创建、更新、追加回复并回读。沿用现有 matt 流程及其任务正文格式；需要规格、拆票或实施时，读取实际已安装的相应 Skill，并按宿主支持的入口和用户授权执行。只有缺少必要目标、编号规则或正文时才补问。新建前检查下一个可用编号，独占创建，冲突重新读取；更新前回读原文件，保留其他修改，无法核实并发变化时停止。不为此准备输入 JSON、operationId 或 `data/git-setup/` 记录。

更新和回复沿用原事项的位置；参考新建按本次规则确定新目标。保存后回读并明确说“已按当前项目规则将 Issue 保存到本地”，给实际路径；保存不等于提交或推送。中断后核对原文件与本次任务，不因未看到完成回复就重复创建，不覆盖部分写入或用户后续修改。

项目已采用其他有效 Issue 工具时沿用。项目只做 Git 接入，不强制安装 matt-pocock-skills，也不因 Issue 文档缺失阻塞。确实要用其工程流程但规则缺失时，按实际安装入口提示初始化；已有禁止重跑依据则修复现有实体或映射，不自动运行脚手架。

## 5. 需要脚本校验或 GitHub 写入时

GitHub Issue 写入继续使用 git-setup 的鉴权与恢复实现。本地只有明确选择脚本校验、并发编号或操作恢复能力时才使用同一低层 CLI，不能把它作为所有本地 Issue 的必经步骤。先读 [输入契约](references/configuration.md)和[写入与恢复](references/github.md)。

1. Agent 读取现有规则，明确发起仓、规则 owner、关系、最终目标及本次内容。在线目标必须完整指定 `github.com/owner/repo`，鉴权按第 2 节默认复用该目标的项目 Token；全局 gh 仍须明确选择。不从当前 remote、活动账号或 matt 的通用模板推断目标或凭证。
2. 准备稳定 operationId 和完整本次输入。默认用 `--input -` 从 stdin 传递 JSON，不创建临时参数文件。需要跨会话恢复且无法从已有任务材料完整重建输入时，才按项目规则保存一份原输入，并告知路径与用途；其中可能含正文。操作记录只有指纹和摘要，不能单凭它重建原输入。
3. `issue-resolve --project <发起Git根> --input -` 核对事实；同一输入交给 `issue-create`、`issue-update` 或 `issue-comment`，在已有具体授权内加 `--apply --expected-fingerprint <指纹>`。脚本写前重验规则、关系、目标和来源；变化拒绝旧计划。
4. 成功回读后给实际路径或 GitHub 链接。结果不明时使用原输入和 operationId 调用 `issue-recover --input -`，只核对原目标，不换编号重发。若原输入不能重建，保留记录并报告缺项；不把一次空查询、同标题或旧 verified 状态当作未写入/本轮成功的证据。
5. `review_required` 先报告 knownResult、原 operationId 和已知路径/链接，再说明复核失败，不能表述为尚未写入。恢复记录位于 target.project 已忽略且未跟踪的 `data/git-setup/`，不含正文或凭证。GitHub 创建/回复的唯一标记、分页预算及结果不明不重发继续有效。配置修复不授权重发。

本地普通事项只有正式 Markdown；选择上述低层流程才产生恢复记录。不能把“目录有位置规范”理解为每次必须创建目录或文件。其他文件生命周期及 helper 显式清理见 [项目策略](references/project-document.md)与[鉴权](references/authentication.md)。

## 完成条件

报告实际改动文件、配置来源、远端/本地事项位置、验证通过范围及未完成项。脚本通过不等于线上鉴权、真实 GitHub 写入或三宿主调用通过。当前分发与验证状态见 [README](README.md)，使用者项目中的能力须从其实际入口验证。
