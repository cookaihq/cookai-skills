# GitHub API 与事项写入

本文件供 Agent 和维护者读取。用户入口、安装与常用请求见上层 README。首版在线 Issue 及创建远端仓库接口只支持 `github.com`；Git 传输另由 Git 模块负责，不因为此限制禁止其他平台的 Git remote。

## 调用关系

CLI 先调用 `config.mjs` 的 `resolveIssue`，取得发起项目、配置所属项目、最终目标和 `fingerprint`。Agent 将该值传入 `issue-create --expected-fingerprint`，CLI 先比较此前计划，执行写入前再重新解析并比较 fingerprint，再将完整结果传给 `issues.mjs`。写入模块不从当前工作目录、`origin`、`GH_REPO` 或活动账号推断目标仓库。

GitHub 目标使用 `resolution.github = { host: "github.com", repository: "owner/repository" }`。本地目标使用 `resolution.local.root` 指定实际检出根，`directory` 指定根内事项目录。`targetProject` 指定操作记录所属项目；GitHub 指定仓库没有本地检出时，它可以是配置所属项目，不能据此改选 GitHub 目标。

## 身份与权限

`loadEnvironment` 按配置规范提供 `values` 和无值来源报告。CLI 调用 `authFromEnvironment(values, resolution.github)`，验证身份绑定后构造 `createGitHubClient`。配置加载不执行 shell 文件。

| 模式 | 同一配置来源内的字段 | 行为 |
| --- | --- | --- |
| 独立 Token | `GIT_SETUP_GITHUB_AUTH=token`、`GIT_SETUP_GITHUB_TOKEN`、`GIT_SETUP_GITHUB_HOST=github.com`、`GIT_SETUP_GITHUB_REPOSITORY=owner/repository` | Token 仅放入固定 `https://api.github.com` 请求的认证头，不进入参数、日志或操作记录；必须匹配最终目标 |
| GitHub CLI | `GIT_SETUP_GITHUB_AUTH=gh`，可选 HOST / REPOSITORY 限定 | 真实执行 `gh --version`，调用 `gh api --hostname github.com`，使用 gh 已保存的 github.com 登录态；子进程清除 GH_TOKEN / GITHUB_TOKEN 临时覆盖，不允许同时传入 Skill Token |

Token 和 gh 均不能从 Git 提交作者推出。SSH 的 Git push 鉴权也不表示具有 Issue API 权限。不同目标仓库使用对应配置；配置不完整或绑定不匹配时停止，不自动寻找其他 Token。

凭证首次保存沿用用户明确选择的位置。项目保存只写已证明未被跟踪、被忽略的 `.env.local`；个人全局按普通 Skill 规范只在显式启用后读取 `~/.config/git-setup/.env`。Token 值不可写入共享项目配置。本轮使用不等于允许长期保存。

GitHub Settings → Developer settings → Personal access tokens 中创建 Token 时，选择正确的资源所有者及目标仓库，设置有效期。创建或修改 Issue 需要该目标的 **Issues: write**；只读核对使用 Issues: read。对组织创建仓库，官方文档列出 Administration: write 或 Repository creation: write 权限方案，还受组织策略约束。仅配置已有仓库不需要为了方便申请创建仓库权限。权限字段、组织审批与可选资源范围以实际 GitHub 页面和下面的官方文档为准，不使用虚构的 Token 前缀或固定长度校验。

## 真实接口与访问限制

| 动作 | API |
| --- | --- |
| 核对仓库 | `GET /repos/{owner}/{repo}` |
| 列出仓库事项 | `GET /repos/{owner}/{repo}/issues`，`state=all`，每页最多 100 |
| 创建事项 | `POST /repos/{owner}/{repo}/issues` |
| 回读 / 更新事项 | `GET` / `PATCH /repos/{owner}/{repo}/issues/{issue_number}` |
| 回复 / 回读回复 | `POST /repos/{owner}/{repo}/issues/{issue_number}/comments`；`GET /repos/{owner}/{repo}/issues/comments/{comment_id}` |
| 当前个人账号创建仓库 | `POST /user/repos`，先核对 `/user` 的 login 与指定 owner |
| 组织创建仓库 | `POST /orgs/{org}/repos`，先核对 `/users/{owner}` 的账号类型 |

API 版本为 `2022-11-28`。接口可能返回 PR，读取时用 `pull_request` 字段排除；不能把 PR 当普通 Issue 修改。成功结果必须回读，检查完整仓库标识、事项编号、实际链接和要求写入的字段。标签或负责人被平台忽略时返回核验失败，不能只依据 HTTP 成功报告完成。

读取仓库得到 404 可能表示没有访问权限，不证明仓库不存在。用户已经明确要求创建指定 owner/name 和可见性时，创建入口可调用只负责新建的 API；它不会更新已有仓库。目标已可读且存在时直接报告已有，不改变可见性。401/403、账户不匹配等情况停止。

## 本地写入

本地事项必须遵守解析结果中的目录、文件名和模板规则。`filename` 支持 `{number}`、`{slug}`；`numbering` 可指定正整数 `start` 和 1–12 的 `width`。项目未配置 filename 时，调用方须提供明确的单个 `.md` 文件名，脚本不发明编号规则。

`template` 从配置所属项目读取，必须是该项目内普通文件。模板使用 `{title}`、`{body}`、`{number}`、`{slug}`、`{date}`，其中 title/body 必须存在，避免丢失用户内容；没有模板时使用标题加正文。既有项目使用其他模板格式时，由 Agent 先按已有规则整理正文，并明确设置本次文件名，不擅自修改项目约定。正文由 Agent 根据用户提供的事实、复现和期望填写，不捏造结果。

路径不能逃出配置的根目录，目录链和模板不能通过符号链接跳转；拒绝平台保留文件名。编号分配有互斥锁，新文件用独占创建，绝不覆盖已有事项。并发调用如遇编号锁返回 `operation-locked`，重新读取后重试；持锁进程已结束时允许移除可验证的遗留锁。损坏锁或无法确认的进程需要先核实。

完整写入、刷新并回读后返回 `已按当前项目配置将 Issue 保存到本地` 及实际路径。此动作不执行 `git add`、commit 或 push。

## 写入中断与恢复

每次新建必须使用操作标识 `operationId`。Agent 在执行前生成并展示稳定标识，通过 `--operation-id` 明确传入，CLI 不自行生成；后续恢复复用同一个标识，不能用新标识掩盖结果不明的旧操作。

`issues.mjs` 在目标项目 `data/git-setup/<operationId>.json` 保存操作类型、目标、内容哈希、时间和状态，不保存 Token 或正文。本地记录包含实际文件路径；文件名使用标题时，路径也会包含该标题片段。首次写入前验证该目录未被 Git 跟踪且实际被忽略；缺少忽略规则时返回具体说明，由已有授权决定是否修改项目忽略规则，写入模块不会自行改文件。

本地恢复按操作记录的路径和完整内容哈希回读。遇到部分写入、冲突或后续编辑时报告具体状态，不覆盖文件或自动换名创建。

GitHub Issue 正文追加 HTML 注释，包含唯一 operationId 和内容哈希。GitHub 没有在此接口提供已验证的幂等键，这个注释仅用于查询恢复。发生超时、连接中断、429 或 5xx 时，写请求只执行一次，状态为 `ambiguous`。恢复优先使用已记录编号，否则分页查询同一仓库近期创建/更新的事项，排除 PR，核对唯一注释和正文等字段哈希。默认恢复总预算 120 秒、最多 20 页，预算约束每次请求及退避。没有找到、找到多个或内容已改变，都不能自动重发。

创建仓库同样记录操作。已取得仓库 ID 时可以按 ID 与可见性核验恢复；请求结果不明且只有同名仓库存在时，无法证明就是本次创建，报告 `ambiguous`，不重发。

更新已有 Issue 与回复始终指向用户明确提供的原仓库和编号，不采用新建默认目标。写失败同样不盲重试。更新可回读原字段核对；回复发生不明状态时需人工核对原 Issue 评论，首版不提供评论的自动跨会话恢复。参考一个旧 Issue 新建时，旧链接只是参考，不调用更新接口。

## 网络行为与验证边界

每次 HTTP 或 gh API 调用默认超时 30 秒，读取瞬时网络错误、429、5xx 最多尝试 3 次，指数退避 1、2 秒。403 仅在有 Retry-After 或剩余额度为零等明确限流响应头时按限流处理；其他 403 按权限错误停止。限流的 Retry-After 支持秒数或日期，上限 60 秒；主限流无 Retry-After 时可使用 reset 时间，同样限制上限。非法值回落指数退避。确定性 4xx 不重试。日志只含尝试次数、等待时间和状态码，不透传响应正文、命令 stderr 或 Token。

Token HTTP 请求禁止自动跟随重定向，避免将凭证发给其他主机。仓库迁移或改名需更新配置。gh 使用固定主机及结构化参数，正文通过 stdin 传入；底层 gh 的网络行为仍由已安装的 GitHub CLI 实现，Skill 不把它当作 SSH 鉴权。

测试使用注入传输和本地临时仓库，覆盖绑定、权限拒绝、超时、限流、无重复创建、并发与路径冲突。模拟测试通过不表示真实 Token、gh 登录或线上仓库写入已验收，更不表示 Claude Code、Codex、WorkBuddy 的实际会话已全部验收。线上创建需要当前任务对具体目标与内容的已有授权；本 Skill 的开发授权不构成线上写入授权。

## 官方依据

以下页面在实现期间于 2026-10-11 读取，API 行为与权限以页面当前内容为准：

- [GitHub Issues REST API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28)：创建、读取、更新，PR 混合返回，Issue 权限与分页。
- [Issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28)：回复与回读。
- [Repositories REST API](https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28)：个人/组织创建仓库、所有者、private、auto_init 与权限。
- [gh api](https://cli.github.com/manual/gh_api)：`--hostname`、`--method`、`--include`、`--input -`。
- [REST API troubleshooting](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api?apiVersion=2022-11-28)：私有资源的 404、鉴权和限流。
