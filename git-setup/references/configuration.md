# 配置来源与本次 Issue 输入

Git 远端来自所选仓库的实际 Git 配置。Harness 策略来自已登记的实际安装目录 `config.json`；外仓自身使用顶层 `targets/sync`，已登记子仓使用 `repositories[登记路径]`，缺策略不回退外仓。详见 [Harness](harness.md)。

仓库关系由 `repository-relationship.mjs` 共用解析：子模块联合核对 `.gitmodules`、gitlink 和 Git 元数据；非子模块独立仓读取管理仓实际 Harness 的 `repos.conf`。Worktree 用 Git common directory 核对归属，同 URL 的独立 clone 不是同一仓库。登记证明管理关系，不决定 Issue 归属或账号共享。

`.git-setup/project.json`、`.git-setup/issue-routing.md`、`GIT_SETUP_ISSUE_JSON` 已退出正常读写；`config-check`、`config-write`、`install-rules` 明确拒绝。旧文件原样保留，不兼容、不自动迁移或删除。普通 Git setup 不读取 Issue 规则，不维护旧 JSON 中的 remote 副本。

## 从文档到本次操作

Agent 在明确的发起项目读取 `docs/agents/issue-tracker.md`，沿引用读取模板、限制与状态约定。有项目声明的 Worktree 文档映射时访问同一实体。Agent 解释任意 Markdown；脚本不推断文档语义，也不把文档改为固定格式。

Agent 为本次创建、更新或回复准备一份完整 JSON，保存在按项目规则允许的位置。需要跨会话恢复时保留原输入及其实际路径，目录须按项目规则忽略。输入可能包含事项正文，应按同一内容的保存权限保护；无凭证操作记录只保存摘要。输入仅供本次操作及恢复，不能作为未来新建的长期默认值。

```text
node <skill>/scripts/git-setup/cli.mjs issue-resolve --project <发起项目> --input <本次输入.json>
node <skill>/scripts/git-setup/cli.mjs issue-create --project <同一项目> --input <同一输入.json> --apply --expected-fingerprint <计划指纹>
node <skill>/scripts/git-setup/cli.mjs issue-recover --project <同一项目> --input <原输入.json>
```

更新和回复分别使用 `issue-update`、`issue-comment`，同样先预览，再带指纹执行。恢复不带 `--apply`，只回读实际事项并更新本机恢复记录。直接调用 CLI 不证明 Agent 已完成规则调查和标准比较。

## 输入契约

下例展示当前仓本地创建的字段；路径和摘要由 Agent 读取实际项目后填写，不原样使用占位内容。

```json
{
  "schemaVersion": 1,
  "action": "create",
  "operationId": "issue-request-001",
  "initiatingProject": "<发起项目绝对Git根>",
  "relationship": { "workspace": "<明确管理仓或自身的绝对Git根>", "kind": "self", "path": "." },
  "rules": {
    "project": "<规则所属项目绝对Git根>",
    "path": "docs/agents/issue-tracker.md",
    "sha256": "<已读取规则的64位SHA-256>",
    "references": [],
    "initialization": { "mode": "initialize-if-missing" }
  },
  "target": {
    "scope": "current",
    "project": "<目标检出绝对Git根>",
    "backend": "local",
    "local": {
      "directory": "tickets",
      "filename": "{number}-{slug}.md",
      "numbering": { "start": 1, "width": 2 },
      "template": { "path": "docs/agents/issue-template.md", "sha256": "<已读取模板的64位SHA-256>" },
      "commentsHeading": "## Comments"
    }
  },
  "decision": { "basis": "规则要求当前仓保存到 tickets，使用两位编号和指定模板。", "override": null },
  "content": { "title": "事项标题", "body": "本次事项正文" }
}
```

| 字段 | 调用方必须明确的内容 |
| --- | --- |
| `action` / `operationId` | create、update 或 comment；稳定的 8–80 位字母、数字、下划线或连字符标识，同一次恢复不换标识 |
| `initiatingProject` | 实际发起检出根；必须与 `--project` 一致 |
| `relationship` | `workspace` 为明确管理仓或自身；`kind/path` 与实际解析的 self / independent / submodule 和登记路径匹配；可选 `harnessDirectory` 为管理仓实际安装位置 |
| `rules.project` | 规则 owner，必须属于发起仓、已验证管理仓或明确目标仓 |
| `rules.path/sha256/references` | 主规则路径与内容 SHA-256；references 为已读取引用的 `{path, sha256}` 列表，不由脚本猜引用 |
| `rules.initialization` | 一般项目为 initialize-if-missing；禁止重跑脚手架时用 repair-existing 并提供 `evidence: {path, sha256}` 指向限制依据 |
| `target.scope/project` | current、outer 或 specified；project 是已存在的实际 Git 检出根；current/outer 允许同仓的其他 Worktree，outer 必须有真实外仓关系 |
| `target.backend` | local 或 github，必须提供对应完整字段，不允许同时携带两组设置 |
| `decision` | basis 记录规则解释依据；无本次覆盖用 `override: null`，有覆盖用 `{reason, request}` 记录依据及用户原请求 |
| `reference` | 仅参考新建时可选的来源字符串，不决定新建目标，也不修改参考事项 |

本地 `directory` 相对目标检出根，不接受越界、符号链接或 Git 元数据路径。`filename` 是单个跨平台 `.md` 名称，可用 `{number}`、`{slug}`；包含编号时显式提供正整数 start、1–12 的 width。固定文件名也可使用，不另设 filename 命令行覆盖。

本地创建有两种输入：`content: {title, document}` 提交 Agent 按规则准备的完整 Markdown，document 原样写入，不替换其中的占位文字；title 用于命名。此方式不要求项目另建模板文件，已有引用仍放入 rules.references 校验。若已有模板支持渲染，可沿用上例的 `content: {title, body}` 和 local.template：模板从规则 owner 读取，必须含 `{title}`、`{body}`，还支持 `{number}`、`{slug}`、`{date}`。两种 content 不混用；不能为适配渲染器改写项目既有模板。显式提供 local.template 时，无论动作均验证其实际文件和指纹。

GitHub 改为 `backend: "github"` 与 `github: {"host":"github.com","repository":"owner/name"}`，移除 local。所有范围都必须给出完整在线目标，不从 remote、CWD 或活动账号推导。`target.project` 承载该目标的已选鉴权及操作记录，不要求在线目标必须有代码检出，也不用于推导在线地址。

本地 update/comment 不要求创建模板，省略不参与本次操作的 local.template。update 使用 `existing: {path, sha256}` 指向目标目录内已读取的原事项，content 为 `{document: "完整更新后原文"}`。comment 使用同样 existing、`content: {body}`，在规则指定的 `commentsHeading` 下追加并保留原文；该标题必须是最后一个同层或上层章节，否则改为明确的完整更新。恢复回读写入后的摘要，后续用户修改不能被旧记录覆盖。

GitHub update/comment 使用 `existing: {url: "https://github.com/owner/name/issues/123"}`；URL 必须属于本次明确目标。update 的 content 可含 title、body、labels、assignees、state，至少一项，state 为 open/closed；comment 只有非空 body。创建可附已有明确 labels/assignees，不自动选择负责人。

## Worktree 文档映射与缺失规则

可选 `rules.mapping` 格式为 `{from: "docs-anchor", to: "docs", declaration: {path: "AGENTS.md", sha256: "..."}}`。from 是检出内实际符号链接，必须指向同仓主检出的 docs 实体；declaration 是该检出内已读取的普通文件。主规则改用 `docs-anchor/agents/issue-tracker.md`，模板和引用可使用同一前缀。脚本验证链接、目标实体、声明与各文档指纹；不按目录名猜映射。

主规则真正缺失时，一般项目提示 `/setup-matt-pocock-skills`，不自动运行。已有禁止重跑依据时只提示修复原文档或映射。本工作区按 ADR 0004 使用 repair-existing，不建议解除禁令。不可读、模板缺失或内容变化时报告实际问题，不创建另一份默认设置；无关 Issue 文档不阻塞普通 Git 操作。

## 鉴权环境配置

新 GitHub/CNB 鉴权沿用 [仓库鉴权](authentication.md) 的独立文件或 gh 账号绑定。已有效绑定的目标不被其他环境 Token 替换。已有显式配置继续按以下顺序读取：进程环境 → 所选鉴权项目根 `.env.git-setup` → `.env.local` → `.env` → 本次 `--use-global-config` 显式启用的个人 `.config/git-setup/.env`。

不扫描父目录、其他 Skill 或另一操作系统环境。macOS/Linux 使用当前 Node 用户目录；Windows 原生由 os.homedir() 定位用户目录下 `.config\git-setup\.env`；WSL 使用其 Linux 用户目录，不自动读取 Windows 文件。文件内容仅按 KEY=value 读取，支持引号，不执行 `$()`、反引号或变量展开。

只识别四个鉴权字段：`GIT_SETUP_GITHUB_AUTH`（token/gh）、`GIT_SETUP_GITHUB_TOKEN`、`GIT_SETUP_GITHUB_HOST`、`GIT_SETUP_GITHUB_REPOSITORY`。首个含非空鉴权字段的层提供完整组，不从较低层补齐。Token 模式必须同层提供 TOKEN/HOST/REPOSITORY；gh 模式不能同时包含 TOKEN。本地 Issue 不加载无关 GitHub 鉴权。`auth-info` 只报告来源和是否填写，不输出值。读取不授权保存或迁移凭证。

## 校验和恢复责任

CLI 调用 `issue-rules.mjs` 验证输入、实际仓库和路径、规则/引用/模板；`issue-operation.mjs` 取得目标已选来源，把来源指纹纳入计划。写入前重新核对完整输入和证据；远端预读之后仍核对一次，再调用 `issues.mjs` 的实际写入。解析不保存长期 Issue 默认值。

本地文件独占创建，原文更新按 SHA 验证；GitHub 写入只调用一次。`data/git-setup/<operationId>.json` 保存操作类型、完整目标、规则和输入指纹、内容摘要及状态，不含正文和凭证。恢复重新校验同一输入、规则、模板、关系、目标和鉴权，并回读真实结果；旧 verified 记录不代替本轮核验。详见 [写入与恢复](github.md)。

若已经取得写入或恢复结果，但返回前规则、关系或鉴权复核失败，返回 `review_required`、退出码 2。`knownResult` 保留实际结果，operationId 和已知路径/链接仍可见；finalCheck 只报告失败类别，禁止重新写入。先核实变化及原操作，恢复仍需满足原输入和依据。

这些是脚本契约；三宿主的自然语言解释、选择和跨会话恢复须在完整实现后分别验收。
