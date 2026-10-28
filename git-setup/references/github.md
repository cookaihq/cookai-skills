# GitHub API 与事项写入

本文件供 Agent 和维护者读取。用户入口、安装与常用请求见上层 README。首版在线 Issue 及创建远端仓库接口只支持 `github.com`；Git 传输另由 Git 模块负责，不因为此限制禁止其他平台的 Git remote。

## 调用关系

普通本地 Markdown 沿已有 matt 工程流程直接写入和回读，不进入本文件的操作记录协议。只有 GitHub 写入或明确选择低层 CLI 时，Agent 读取现有 tracker 文档、引用及模板，按 [输入契约](configuration.md) 准备完整本次输入。CLI 的 `issue-resolve/create/update/comment/recover --input` 先经 `issue-rules.mjs` 验证文件与仓库事实，`issue-operation.mjs` 再选择最终目标的鉴权。计划指纹包含输入、规则、模板、关系和来源；实际写入前再次核对，变化则停止。

GitHub 目标始终是输入的 `target.github`，不从 CWD、origin、GH_REPO 或活动账号推断。`target.project` 为已存在的实际 Git 根，承载该目标已选鉴权及操作记录。本地 `target.local.directory` 相对目标检出根；模板从规则 owner 或其声明的同仓文档映射读取。更新与回复的 existing 指向原事项，reference 只用于参考新建。

## 身份与权限

GitHub API 的最终 host/repository 交给 `auth.mjs` 的 `boundGitHubClient`。它读取该目标已经选定的独立文件或 gh 账号，通过 `/user` 核验身份，再构造只访问 `api.github.com` 的客户端。Git 传输使用同一来源；gh 使用明确账号取得的进程内凭证，不依赖活动账号、不导出到项目，也不受 GH_TOKEN / GITHUB_TOKEN 覆盖。

首次会话选择、受保护空文件、等待“已填写”、真实路径、外仓与 Worktree 规则见 [仓库鉴权](authentication.md)。新 GitHub 文件为主检出或明确统筹外仓 `.credentials/repositories/<owner>--<repo>.github-token`，内容为单行 Token 原文。不得把已确认流程替换成 `.env.local`。只有明确选择 environment 才读取 dotenv / 进程配置；不自动回退。

Token 和 gh 均不能从提交作者推出。SSH Git 鉴权不代表 Issue API 权限。不同目标分别选择来源；缺失、来源不符或账号变化时停止，不寻找其他 Token。配置修复不授权重发结果不明的写入。

GitHub Settings → Developer settings → Personal access tokens 中创建 Token 时，选择正确的资源所有者及目标仓库，设置有效期。创建或修改 Issue 需要该目标的 **Issues: write**；只读核对使用 Issues: read。对组织创建仓库，官方文档列出 Administration: write 或 Repository creation: write 权限方案，还受组织策略约束。仅配置已有仓库不需要为了方便申请创建仓库权限。权限字段、组织审批与可选资源范围以实际 GitHub 页面和下面的官方文档为准，不使用虚构的 Token 前缀或固定长度校验。

## 真实接口与访问限制

| 动作 | API |
| --- | --- |
| 核对仓库 | `GET /repos/{owner}/{repo}` |
| 列出仓库事项 | `GET /repos/{owner}/{repo}/issues`，`state=all`，每页最多 100 |
| 创建事项 | `POST /repos/{owner}/{repo}/issues` |
| 回读 / 更新事项 | `GET` / `PATCH /repos/{owner}/{repo}/issues/{issue_number}` |
| 列出回复 | `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`，page/since，每页最多 100 |
| 回复 / 回读回复 | `POST /repos/{owner}/{repo}/issues/{issue_number}/comments`；`GET /repos/{owner}/{repo}/issues/comments/{comment_id}` |
| 当前个人账号创建仓库 | `POST /user/repos`，先核对 `/user` 的 login 与指定 owner |
| 组织创建仓库 | `POST /orgs/{org}/repos`，先核对 `/users/{owner}` 的账号类型 |

API 版本为 `2022-11-28`。接口可能返回 PR，读取时用 `pull_request` 字段排除；不能把 PR 当普通 Issue 修改。成功结果必须回读，检查完整仓库标识、事项编号、实际链接和要求写入的字段。标签或负责人被平台忽略时返回核验失败，不能只依据 HTTP 成功报告完成。

读取仓库得到 404 可能表示没有访问权限，不证明仓库不存在。用户已经明确要求创建指定 owner/name 和可见性时，创建入口可调用只负责新建的 API；它不会更新已有仓库。目标已可读且存在时直接报告已有，不改变可见性。401/403、账户不匹配等情况停止。

## 可选 CLI 的本地写入

本地事项必须遵守解析结果中的目录、文件名和模板规则。`filename` 支持 `{number}`、`{slug}`；`numbering` 可指定正整数 `start` 和 1–12 的 `width`。filename 必须在本次输入中明确；固定的 `.md` 文件名也可使用，脚本不发明编号规则。

Agent 可按现有自然语言规则直接提交 `content: {title, document}`，完整 document 原样保存，不要求额外模板文件。若选择按已有模板渲染，则提供 `content: {title, body}` 与 local.template，模板须含 `{title}`、`{body}`，还可用 `{number}`、`{slug}`、`{date}`。显式模板从规则 owner 或已声明映射读取，验证普通文件及指纹；任意格式的参考模板可以放入 rules.references，由 Agent 解释后准备 document，不改写项目模板来适配脚本。update/comment 不要求创建模板。内容均依据用户事实、复现和期望，不捏造结果。

路径不能逃出配置的根目录，目录链和模板不能通过符号链接跳转；拒绝平台保留文件名。编号分配有互斥锁，新文件用独占创建，绝不覆盖已有事项。并发调用如遇编号锁返回 `operation-locked`，重新读取后重试；同机持锁进程已结束时通过独占后继锁恢复，不能先删旧锁再竞争。损坏锁、其他主机或无法确认的进程需要先核实。

完整写入、刷新并回读后返回 `已按当前项目规则将 Issue 保存到本地` 及实际路径。此动作不执行 `git add`、commit 或 push。

## 写入中断与恢复

使用本文件的 CLI 创建、更新和回复时必须提供操作标识 `operationId`。Agent 在执行前生成稳定标识，放入本次完整 JSON，默认经 `--input -` 传入 stdin；只有确有跨会话需要才保存原输入文件，CLI 不自行生成；后续恢复复用同一个标识，不能用新标识掩盖结果不明的旧操作。

这些低层操作由 `issues.mjs` 在目标项目 `data/git-setup/<operationId>.json` 保存操作类型、目标、内容哈希、时间和状态，不保存 Token 或正文。本地记录包含实际文件路径；文件名使用标题时，路径也会包含该标题片段。首次写入前验证该目录未被 Git 跟踪且实际被忽略；缺少忽略规则时返回具体说明，由已有授权决定是否修改项目忽略规则，写入模块不会自行改文件。

本地更新接收原路径、已读 SHA 和完整新原文；回复按明确标题追加。临时结果写完后再次核对原内容及本次依据，再替换原文件。本地恢复按操作记录的路径和完整内容哈希回读；部分写入、冲突或后续编辑报告未完成，不覆盖文件或自动换名创建。

GitHub Issue 正文追加 HTML 注释，包含唯一 operationId 和内容哈希。GitHub 没有在此接口提供已验证的幂等键，这个注释仅用于查询恢复。发生超时、连接中断、429 或 5xx 时，写请求只执行一次，状态为 `ambiguous`。恢复优先使用已记录编号，否则分页查询同一仓库近期创建/更新的事项，排除 PR，核对唯一注释和正文等字段哈希。默认恢复总预算 120 秒、最多 20 页，预算约束每次请求及退避。分页未结束、没有找到、找到多个或内容已改变，都不能自动重发。恢复还会重新校验本次输入和依据，旧成功记录不能替代真实回读。

创建仓库同样记录操作。已取得仓库 ID 时可以按 ID 与可见性核验恢复；请求结果不明且只有同名仓库存在时，无法证明就是本次创建，报告 `ambiguous`，不重发。

更新已有 Issue 与回复始终指向用户明确提供的原仓库和编号，写失败不重发。更新记录期望字段摘要，恢复只 GET 原编号并核对；回复追加唯一操作标记，响应丢失后分页查询原 Issue 的评论，确认唯一结果并 GET 回读 URL、issue_url、编号和正文。回复恢复同样有 120 秒/20 页预算，未扫描完整时保持 ambiguous。参考一个旧 Issue 新建时，旧链接只是参考，不调用更新接口。

仓库名称比较沿用 GitHub 大小写不敏感规则，原 Issue 编号和回复 ID 仍须准确匹配。写入或恢复结果取得后，还会复核规则、关系与鉴权。此时失败返回 `review_required`，保留 operationId、已知路径/链接和 knownResult，退出码 2；不能把已写入表述为未执行，也不能因复核失败重新创建。核对实际变化后，仍按原输入和依据恢复。

## 网络行为与验证边界

每次 HTTP 或 gh API 调用默认超时 30 秒，读取瞬时网络错误、429、5xx 最多尝试 3 次，指数退避 1、2 秒。403 仅在有 Retry-After 或剩余额度为零等明确限流响应头时按限流处理；其他 403 按权限错误停止。限流的 Retry-After 支持秒数或日期，上限 60 秒；主限流无 Retry-After 时可使用 reset 时间，同样限制上限。非法值回落指数退避。确定性 4xx 不重试。日志只含尝试次数、等待时间和状态码，不透传响应正文、命令 stderr 或 Token。

当前 CLI 的文件与 gh 账号分支均通过相同 HTTP 客户端请求 GitHub，禁止自动跟随重定向。仓库迁移或改名需重新核对完整目标。gh 只负责读取明确账号的已保存凭证，不切换活动账号。

公开 CLI 测试在新进程使用本机 HTTP API 服务，仅在测试进程重定向 api.github.com；内部分类测试另用注入传输。测试使用隔离 Git 仓库和测试凭证，覆盖来源选择、权限拒绝、超时、限流、无重复创建、并发与路径冲突。模拟测试通过不表示真实 Token、gh 登录或线上仓库写入已验收，更不表示 Claude Code、Codex、WorkBuddy 的实际会话已全部验收。线上创建需要当前任务对具体目标与内容的已有授权；本 Skill 的开发授权不构成线上写入授权。

## 官方依据

以下页面在实现期间于 2026-10-11 读取，API 行为与权限以页面当前内容为准：

- [GitHub Issues REST API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)：创建、读取、更新，PR 混合返回，Issue 权限与分页。
- [Issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28)：回复、分页查询与回读；分页参数另于 2026-10-14 核对。
- [Repositories REST API](https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28)：个人/组织创建仓库、所有者、private、auto_init 与权限。
- [gh api](https://cli.github.com/manual/gh_api)：`--hostname`、`--method`、`--include`、`--input -`。
- [REST API troubleshooting](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api?apiVersion=2022-11-28)：私有资源的 404、鉴权和限流。
