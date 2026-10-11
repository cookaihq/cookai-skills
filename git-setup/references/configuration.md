# 配置与 Issue 目标解析

共享配置的固定位置是所选项目 Git 根目录下的 `.git-setup/project.json`。项目尚未初始化 Git 时，使用显式指定的项目目录；如果目录已经属于父仓，先识别父仓并让用户决定本次作用对象，不能把子目录当作未初始化项目直接写入。配置文件不包含凭证，供协作者随代码取得；生成文件不自动暂存、提交或推送。

文件采用 `schemaVersion: 1`。下面展示一个外仓登记两个独立子仓的例子；`app-a` 自己维护 Issue 设置，`app-b` 继承外仓的本地事项设置。代码同步和 Issue 归属分开处理，子仓不会继承外仓的 Git remote。

```json
{
  "schemaVersion": 1,
  "repository": { "host": "github.com", "repository": "example/project" },
  "remotes": { "origin": "https://github.com/example/project.git" },
  "sync": {
    "source": "origin",
    "targets": ["backup"],
    "refs": ["refs/heads/main", "refs/tags/v1.0.0"]
  },
  "issues": {
    "backend": "local",
    "target": "outer",
    "local": {
      "directory": "tickets",
      "template": "docs/issue-template.md",
      "filename": "{number}-{slug}.md",
      "numbering": { "start": 1, "width": 2 }
    }
  },
  "workspace": {
    "repositories": [
      { "path": "app-a", "issues": "independent" },
      { "path": "app-b", "issues": "inherit" }
    ],
    "issuePolicy": { "allowIndependent": true }
  }
}
```

`repository`、`remotes`、`sync`、`issues` 和 `workspace` 均可省略；相应操作必须具备所需配置才可执行。没有 `issues` 时，Agent 可在读取已有项目规则后传入完整规则记录，或向用户取得必要选择。示例的 `backup` 必须对应实际配置的 remote，示例标签必须存在；解析配置不代表这些同步条件已经成立。

## Issue 记录

| 字段 | 约束 |
| --- | --- |
| `backend` | `local` 或 `github`；其他在线平台拒绝执行，不修改原配置 |
| `target` | `current`、`outer`、`specified` |
| `github` | `{ "host": "github.com", "repository": "owner/name" }`；不允许 URL、Token 或带密码地址 |
| `local.directory` | 相对最终本地目标检出根的事项目录；禁止 `..` 越界；允许 `.`，不预设 `.scratch/` |
| `local.checkout` | 可选，相对**配置所属项目根**的明确 Git 检出路径；允许 `../issues` 指定已检出的兄弟仓，不允许绝对路径，不自动 clone |
| `local.template` | 可选，相对**配置所属项目根**的模板文件；解析后交给创建模块的是绝对路径和内容指纹 |
| `local.filename` | 可选 Markdown 文件名，支持 `{number}`、`{slug}` 占位符；未配置时由实际创建调用明确指定文件名，不发明编号约定 |
| `local.numbering` | 可选 `{ "start": 1, "width": 2 }`；必须配合含 `{number}` 的文件名，正整数宽度最大 12 |

GitHub 记录不得包含 `local`；本地记录不得包含 `github`。整个 Issue 记录完整校验，不从其他配置来源拼接缺失的目标字段。

- `current` 始终指发起请求的实际仓库。子仓继承外仓默认时，切换读取配置的目录不会改变“当前仓库”的含义。
- `outer` 必须有已验证的外仓关系。没有外仓时拒绝创建；从外仓自身发起可在本次明确使用 `current`，不把缺失关系静默改写。
- `specified` + `github` 必须给出完整 `github` 身份，不需要该远端仓库已在本地检出。
- `specified` + `local` 必须给出 `local.checkout`，并验证该目录本身是已经存在的 Git 根目录。仓库子目录、缺失检出都不能作为替代位置。
- `current` / `outer` 配置了 `local.checkout` 时，该检出必须与目标仓库拥有相同 Git common directory，允许项目明确选择同一仓库的其他 Worktree。

当 GitHub 目标没有显式 `github` 身份时，读取目标仓库共享配置中的 `repository`。仍缺少时，仅在目标仓库所有 GitHub.com fetch/push 地址归属于同一个仓库时推导。多个不同 GitHub 仓库、SSH 主机别名或尚未明确的企业主机都需要显式配置，不默认选择 `origin`。

## 外仓登记和项目限制

Git 子模块通过 Git 的 superproject 事实及 `.gitmodules` 识别。普通目录嵌套和多个 remote 都不是外仓关系。多个外层管理仓库存在时要求显式选择，不能按目录距离猜测。

独立子仓通过外仓 `workspace.repositories` 登记。运行时显式传入 workspace 路径；Agent 在配置阶段将解析入口及 `--workspace` 参数登记进项目实际使用的 Agent 规则。独立 clone 无法仅靠自己的文件推导外仓在本机哪里，规则必须由 Agent 按本机已存在检出定位；不能将原电脑的绝对路径写入共享配置。工作目录移动后重新定位并核验登记。登记的 `path` 相对外仓根目录，Worktree 可通过共同的 Git common directory 匹配同一已登记仓库。

`issues: independent` 表示该子仓 `.git-setup/project.json` 中必须具有完整 `issues`。缺失、不可读、非法或半组配置均报错，不退回外仓，不从外仓补字段。`inherit` 使用外仓完整默认；单独在子仓写一个未登记的配置文件不会改变登记关系。

可选 `workspace.issuePolicy` 表达已确认的项目限制：

- `allowIndependent: false` 禁止子仓独立配置，发生冲突时不创建。
- `backend: "local"` 或 `"github"` 固定允许的记录方式。本机覆盖和本次请求仍须满足此限制。

Agent 还须读取项目现有规则。脚本不会把自然语言规则猜译成配置；只有明确取得的规则才可登记或作为本次输入，不能把既有硬限制当普通默认值覆盖。

## 本机覆盖与凭证

共享默认值之上，`GIT_SETUP_ISSUE_JSON` 可以选择**完整 Issue JSON 记录**。该变量按以下顺序取首个非空值：

1. 本次进程环境。
2. 配置所属项目根的 `.env.git-setup`。
3. 同一目录的 `.env.local`。
4. 同一目录的 `.env`。
5. 仅在本次显式启用全局读取时，个人目录 `~/.config/git-setup/.env`。

没有任何上述覆盖时使用共享 `issues`。全局文件仍低于项目环境文件；不会因为称为“个人覆盖”就提升优先级。不向父目录递归、不扫描其他 Skill 的 `.env.*`。从子仓继承外仓时，所选读取目录是已验证的外仓；独立配置时是该子仓。Windows 原生使用当前 Node 进程的用户目录，WSL 使用 WSL 用户目录，不跨环境查找配置。

环境文件只是数据：支持 `KEY=value`、单引号或双引号、空行、注释行，同名变量最后一次生效。`$()`、反引号、`${VAR}` 都不执行也不展开。加载器只识别下表字段，日志只输出来源和是否存在，不输出凭证值。

| 字段 | 用途 |
| --- | --- |
| `GIT_SETUP_ISSUE_JSON` | 完整本次机器覆盖记录，不能只写 backend 或半个目标 |
| `GIT_SETUP_GITHUB_AUTH` | `token` 或 `gh`；同组存在 TOKEN 时可以省略并按 `token` 解释 |
| `GIT_SETUP_GITHUB_TOKEN` | GitHub Token；仅内部鉴权使用，不能写入共享 JSON |
| `GIT_SETUP_GITHUB_HOST` | 该凭证允许访问的 GitHub 主机，不能含协议或路径 |
| `GIT_SETUP_GITHUB_REPOSITORY` | 该凭证绑定的 `owner/name` |

四个鉴权字段作为完整组，从首个含非空鉴权字段的配置层取得，不与较低层拼接。Token 模式必须在同一层具备 TOKEN、HOST、REPOSITORY；不允许只用进程中的新 Token 配上文件中的旧身份。`gh` 模式不能在同组同时放 TOKEN；实际调用仍须核对所选主机、账号和目标。最终创建模块必须比较 Token 绑定与已解析目标，匹配后才请求。只读 Issue 解析与本地 Issue 创建不要求 GitHub 凭证有效。

保存凭证仍须取得用户明确的持久化位置授权，默认读取不授权写入。项目内 Secret 文件必须未被 Git 跟踪且实际被忽略；个人覆盖也不随代码提交。读取失败需要定位原文件，不悄悄换来源。

## 模块接口与结果复核

`scripts/git-setup/config.mjs` 只使用 Node 内置模块，导出以下异步接口：

- `inspectProject(project)`：返回真实 Git 根、Git common directory、Worktree 状态、superproject 与已脱敏 remote 地址，不写入项目。
- `loadProject(project)`：只读取和验证共享 `.git-setup/project.json`，返回 `config`、`configPath`、`exists`、`fingerprint` 和 `project` 检查结果。
- `writeProject(project, input)`：验证完整输入，通过互斥锁和原子替换写入共享文件；拒绝 Secret、未知字段和符号链接写入，不暂存或提交。明确传入完整有效配置可修复损坏的旧 JSON。并发写入或遗留写锁报错，由 Agent 核查原操作，不能盲删其他操作的锁。
- `loadEnvironment(project, options)`：内部接口，返回 `{ values, sources }`。`values` 可能含 Token，绝不能直接透传到用户输出。`useGlobalConfig` 默认关闭；`env` 允许调用方显式提供本轮进程环境。`issueOnly: true` 只读取 Issue 覆盖，不验证无关鉴权字段。
- `resolveIssue(project, options)`：可用 `workspace`、`useGlobalConfig`、`env`、`issueOverride`、`projectRuleIssue`。后两项是 Agent 根据明确用户要求或项目规则提供的完整对象；规则记录只在没有默认值时使用，本次覆盖不写回持久配置。

解析结果分别保存 `initiatingProject`、`configOwner` 和 `targetProject`；还包含 `backend`、`github`、`local`、`issue`、`source`、`relationship` 和 `fingerprint`。`local.root` 是实际目标检出根，`local.directory` 是相对该根的目录，`local.template` 已解析为绝对路径。指定远端 GitHub 仓库没有本地检出时，`targetProject` 只用配置所属项目承载本次运行记录；**GitHub 写入目标只由 `github.host` 和 `github.repository` 决定**，不得从该路径再次推导。

Agent 把 `issue-resolve` 返回的 `fingerprint` 传入 `issue-create --expected-fingerprint`；CLI 拒绝与先前展示目标不一致的配置，在真正写入前还会再次调用 `resolveIssue` 并比对 `fingerprint`。直接单次调用 CLI 时该参数可省略，但不能把省略参数的调用说成已验证此前计划。写入模块只消费完整解析结果，并再次检查本地模板摘要。指纹包含有效记录、配置来源、外仓登记、限制、仓库身份、remote 和模板内容摘要；变化时不得继续旧计划。恢复记录不得保存 Token。所有公开错误带 `code`，错误文本不包含 JSON 原文或凭证。

本文件描述实现契约，不代表三宿主的真实调用已经通过。测试中的 Git 仓库、子模块及 Worktree 都是隔离夹具，不修改实际业务仓库。
