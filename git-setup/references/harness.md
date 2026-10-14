# 标准 Harness 与项目日常入口

只有 Agent 明确调用 git-setup Skill，才执行完整规则调查、实现比较和采用选择。调用可来自用户点名或 Agent 根据明确任务调用，不限于手工斜杠命令。先读当前实际宿主适用的全局、项目和目录规则及其引用，再运行公开 `check` 收集事实；普通 Git、直接业务 CLI 和生成入口不启动这套完整流程。

## 检查与比较

`node <skill>/scripts/git-setup/cli.mjs check --project <项目>`（无命令时同义）只读报告真实 Git 根、作者及 scope/origin、远端、分支跟踪、推送设置、本机鉴权来源和 Harness。helper 和鉴权头隐藏值；配置存在不表示已选择账号，先前身份核验不是本次联网验证。GitHub HTTPS 首次仍只选择本机 gh 账号或独立 Token，已有显式 legacy 绑定按[鉴权规则](authentication.md)核对。

`harness.candidates` 来自常见脚本、hooks、CI 和规则路径，只是调查线索。Agent 沿真实调用链读取调用方与被调用方，用 `evidenceFiles` 补入其他项目文件；说明谁在什么条件下触发、输入、写入、频率及失败处理。脚本不能理解自然语言，也不能仅因未发现候选就宣称符合标准。全局规则由实际宿主的 Agent 读取，不自动扫描其他宿主的 home 配置。

分别取得“采用标准或保留现有实现”与“哪些独有功能要重新实现”的选择。保留时不替换文件；事实和标准未变时复用原选择。规则实体、已检查文件、安装位置、相关 Git 事实和标准指纹变化会使原选择或计划失效。hooksPath 仅影响本仓 hooks，不说明远端 CI 是否启用；本机工作流文件不证明平台已运行成功。

## 位置与生成计划

Agent 依据实际项目规则确定 `directory`（项目内安装目录）和 `ruleFiles`（登记入口的实际项目规则文件）。没有既定位置时提出具体位置并纳入计划；没有产品默认脚本目录。以下例子只适用于项目规则已选择 `tools/project-git` 和 `AGENTS.md`、且明确需要该同步范围的项目：

```json
{
  "choice": "standard",
  "directory": "tools/project-git",
  "ruleFiles": ["AGENTS.md"],
  "sync": {"source": "origin", "targets": ["backup"], "refs": ["refs/heads/main"]},
  "evidenceFiles": [],
  "comparison": [],
  "extensions": [],
  "edits": []
}
```

`directory` 与 `ruleFiles` 首次采用必须明确；以后省略时只复用本检出的本机选择，不猜测目录。已登记位置改变时停止，旧入口实际迁移需另行计划。不需要同步用 `sync: null`。保留用 `choice: "keep"`，不能修改 sync、repositories、extensions、edits 或刷新目标身份。生成和同步均不读取 `.git-setup/project.json`，缺少策略不回退；原文件不自动删除。Issue 只使用现有文档与本次完整输入；旧配置退出不迁移或删除使用者文件。

comparison 每项包含 id、summary、trigger、inputs、writes、failure、evidence 和 classification。分类为 standard、configuration、unique、conflict、unknown；采用前解决 conflict/unknown，unique 明确 decision 为 retain/drop。retain 的 extension 必须对应已选择扩展 ID。计划不是授权。

edits 只改已检查的项目内入口，每项填写 path 和完整 content。规则登记交给 ruleFiles：保留原文，只增改 `git-setup:harness` 标记块；明确指定的缺失规则文件可创建。项目内文件符号链接写入其原实体，多个别名去重；不替换链接，不写项目外实体，也不通过链接目录写入。前后指纹及链接关系变化时停止。

```text
node <skill>/scripts/git-setup/cli.mjs harness-plan --project <项目> --input <选择文件>
node <skill>/scripts/git-setup/cli.mjs harness-plan --project <项目> --input <选择文件> --plan-file <已忽略记录>
node <skill>/scripts/git-setup/cli.mjs harness-apply --project <项目> --input <同一选择文件> --plan-file <同一记录> --expected-fingerprint <指纹> --apply
```

不带 plan-file 的预览不写文件；显式保存仅写指定、未跟踪且已忽略的 `data/git-setup/*.json` 或 `tmp/git-setup/*.json`。输入文件与记录分开。已有生成锁或未完成事务时，保存新预览会被拒绝，不能覆盖原阶段记录。计划分别列出 generated（生成）、preserved（原样保留）、modified（明确修改）文件；应用取得本仓锁，再核对原文件、权限、链接实体、目标和标准，在同一阶段目录中生成运行包并复制项目文件，按计划替换并回读。没有自动暂存、提交、推送或安装 hooks。

## 独立运行与日常校验

所选目录包含 `run.mjs`、`config.json`、`manifest.json`、`README.md` 和 `runtime/`。入口从自身位置加载相邻配置和完整 Node 运行包，通过生成时记录的相对关系验证 Git 根；不依赖调用者 CWD、固定层数、开发电脑路径或 Skill 安装位置。需要 Node >=20、Git >=2.29；gh 来源另需 GitHub CLI。新 clone 可直接调用已登记的生成入口；公开 CLI 可用 `--harness-directory <实际项目相对目录>` 指定位置，不扫描目录猜测。

| 入口 | 校验与结果 |
| --- | --- |
| 生成入口无参数/check | 只核对本项目运行文件、配置、已确认目标和本机鉴权来源；不联网或修改文件 |
| read --remote | 使用该目标来源读取真实远端引用 |
| publish --remote --ref | 审查指定本地完整 refs 的历史，预览目标，逐引用传输并回读 |
| sync | 使用相邻 config.json 的来源、目标和完整 refs，传输来源远端已发布对象并回读 |
| 普通 Git | 只执行 Git 自身操作及绑定 helper 鉴权，不自动加入 Harness 历史审查、发布计划或同步 |

从项目外调用使用入口的实际路径，例如 `node <项目>/<已选目录>/run.mjs read --remote origin`。发布、同步先预览，再以同参数加 `--apply --expected-fingerprint <指纹>`。两者分别调用，不自动互相触发。Skill 的 publish/sync 在已登记 Harness 存在时使用同一实现；直接业务 CLI 不附带完整 Skill entryCheck。日常操作不读取 Issue 规则、不比较 Skill 新版本；无关说明变化不使计划失效，必要运行文件、配置、目标或来源损坏仍停止。入口在导入业务模块前校验运行文件，不回退 Skill。项目说明 README 不属于日常必需运行文件。

config.json 为 `schemaVersion: 1`、`targets`、`sync`、`extensions`，可选 `repositories` 保存各登记子仓的独立策略。targets 保存每个 remote 已确认的 fetch/push 目标身份，实际地址来自 Git 配置。sync 为 null 或 `{source, targets, refs}`；完整 refs 必须明确列出，不允许通配符、强推、镜像或删除。所有必需目标经核对后才写入。read/publish 仅核对所选远端；sync 核对策略涉及的远端。实际 Git 可能触发已有 hooks，远端可能触发已有 CI；不能宣称 hooks/CI 验收已完成。

项目入口的 auth-info/auth-prepare/auth-verify 使用自带运行包和相同鉴权流程。helper 的完整运行包位于实际 Git common directory 的 `git-setup/<源码指纹>/`，同仓 Worktree 共用；不把凭证复制进运行包。新 clone 仍需建立自己的本机鉴权绑定。移走 Skill 后普通 Git 仍使用该 helper；来源失效停止，不回退其他账号。

## 中断与扩展保护

生成记录保存原计划、文件内容与摘要、选择、锁持有进程、阶段和备份位置，不含凭证。发生中断保留锁、完整或部分临时目录、原目录备份和已完成步骤，不宣称回滚。新进程先检查：

```text
node <skill>/scripts/git-setup/cli.mjs harness-recover --project <项目> --plan-file <原记录>
node <skill>/scripts/git-setup/cli.mjs harness-recover --project <项目> --plan-file <原记录> --expected-fingerprint <恢复检查指纹> --apply
```

首次只读核对真实文件、原选择、标准、目标、锁和进程状态。原进程仍存在、锁无法归属、用户文件或部分运行包不符合原计划时停止，保留现场供人工核查，不盲删残留。现场匹配才返回 recovery_ready；明确继续原范围时使用该指纹完成剩余写入。旧成功记录只报告 historical_receipt_only，不充当本轮验证。恢复不改变原选择或扩展范围，失效计划需重新审阅。未开始写入的计划仍可用原 input 重新计划；没有原选择依据时不能从记录猜授权。

扩展使用项目内 `.mjs`，推荐放在所选 Harness 的 `extensions/` 下；目录外的旧路径仍支持，不自动搬迁。Harness 内其他位置不能作为扩展。扩展包含唯一 id、path、sha256、event（after-publish/after-sync）、description、timeoutMs（1000–300000）。仅在对应 Git 操作全部成功后执行。stdin 为 `{schemaVersion,event,project,operationId,git}`，退出 0 表示扩展自身完成，业务回读由扩展负责；框架不记录 stdout/stderr。有扩展时预览和执行传同一 `--operation-id`，恢复记录不安全则在 Git 写入前停止。中断或失败不自动重发；历史收据不替代远端核验。

hooks 的适用/启用、主检出迁移、全局安装更新和三宿主真实入口验收均留待后续。脚本、生成入口、普通 Git、三客户端会话及真实平台账号/权限分别记录，不能互相替代。

## 升级时保留项目文件

生成器维护 `runtime/`、`run.mjs`、`README.md` 和 `manifest.json`；项目维护 `config.json`、可选 `repos.conf` 及 `extensions/` 中的文件。新清单使用 `schemaVersion: 2`，`files` 只记录生成器维护的文件摘要；旧 `schemaVersion: 1` 清单也能检查和升级。未知或被修改的运行文件仍拒绝覆盖。首次安装目录可为空，或预先仅包含 `extensions/`；无扩展不创建空目录。

升级输入省略 `extensions`、`sync`、`repositories` 时保留已有策略。`extensions: []` 只停用扩展，仍保留脚本和资源；`sync: null` 清除同步策略；`repositories: {}` 明确移除全部子仓策略。已选扩展指纹未变时不要求再次选择，新增、改选或内容改变时仍须比较实际行为并确认新指纹。省略比较不代表可以跳过新发现的候选入口。

原配置语义未变时保留原字节、格式及普通文件权限，对象键序不改变语义。`repos.conf`、扩展脚本和二进制资源同样保留；其新增、删除、内容或权限变化会拒绝旧安装计划。所选 `.mjs` 扩展脚本在业务执行前另有 256 KiB 上限；安装复制检查要求普通文件、禁止符号链接、单文件不超过 2 MiB、整包不超过 300 文件。资源文件可使用复制检查的 2 MiB 上限。

根仓远端 targets 默认保持原先确认值。实际远端改变时，日常入口仍会拒绝；Agent 核对新目标和鉴权并取得同范围授权后，在采用输入中明确传 `refreshTargets: true`，才能从根仓实际 Git 配置刷新 targets。该字段只影响根仓，不刷新子仓或绑定凭证；子仓目标仍通过明确传入对应 `repositories` 策略更新。

恢复时，内部扩展可能已经随原目录移入备份；程序分别核对当前、阶段和备份目录的完整内容及项目文件权限，不因原路径暂时缺失就认定扩展丢失。任一现场不符都保留现场并停止，不覆盖后续修改。阶段目录部分写入尚不完整时不会自动补写或清除。

## 仓库关系

已确认的非子模块独立仓在实际 Harness 目录的 `repos.conf` 登记，一行一个相对管理仓根的路径。没有独立仓时不生成空文件；子模块由 `.gitmodules`、gitlink 与实际 Git 元数据共同证明，不重复写入清单。鉴权、setup 和 Issue 共用解析，仓库登记不选择 Issue 继承。`check/auth/setup --workspace <管理仓>` 使用该关系；必要时加 `--harness-directory <管理仓内实际目录>`。绑定只保存已选关系，不能绕过登记变化。外仓入口使用下面的明确选仓流程。


## 从一份 Harness 选择一个仓库

公开 CLI 的 `check/read/publish/sync` 与生成入口都接受 `--repository <登记路径>`，外仓自身用 `.`；省略时只操作入口所属项目。`--checkout <路径>` 必须与 repository 一起使用，相对路径以管理仓为基准。先证明检出与登记仓有相同 Git common directory 和主检出；同 URL 的独立 clone 会被拒绝。公开 CLI 的 `--project` 是安装 Harness 的管理仓，不能另传不同的 workspace 重定向操作。

采用计划用 `repositories` 按登记路径明确选择每仓策略，例如 `{"repositories":{"api":{"sync":{"source":"api-origin","targets":["api-backup"],"refs":["refs/heads/main"]}}}}`。登记 api 后，生成器从 api 的实际 Git 配置取得已确认 targets；生成 config.json 的 repositories.api 包含其 targets 和 sync。首次独立仓先安装 Harness、在实际目录登记，再计划各仓策略；子模块直接使用 Git 登记。未传 repositories 时保留已安装的策略并重验登记；传空对象表示明确移除全部子仓策略。缺少所选仓策略直接停止，不套用外仓策略。子仓扩展维护不在本阶段范围。

```text
node <管理仓>/<安装目录>/run.mjs check --repository api --checkout <api 的已验证 Worktree>
node <Skill>/scripts/git-setup/cli.mjs publish --project <管理仓> --repository api --remote api-origin --ref refs/heads/main
node <管理仓>/<安装目录>/run.mjs publish --repository api --remote api-origin --ref refs/heads/main --apply --expected-fingerprint <同一计划指纹>
node <管理仓>/<安装目录>/run.mjs sync --repository api
```

预览的 entryCheck 分别列出 installation、repository、project、relationship（主检出、登记依据和指纹）、本仓远端/作者/分支、来源与策略；preview 列出完整 refs 及实际 SHA。执行前再次检查相同对象，登记、检出、策略、remote、凭证来源或 ref 变化使旧计划失效。生成恢复也重验所涉及子仓的登记和目标。auth-info/auth-prepare/auth-verify 同样接受 repository/checkout，使用管理仓主检出的独立凭证文件；不改变兄弟仓绑定。

多目标同步逐项返回实际 SHA 和 verified/failed/uncertain；成功目标不会掩盖失败。源引用改变后停止后续目标，已完成写入照实保留。传输响应丢失先回读目标；SHA 符合计划可报告 verified，并用 writeReportedSuccess=false 说明传输没有返回成功。脚本每项只调用一次 git push；Git/libcurl 的底层 HTTP 传输行为另计，完全未收到响应时可能在该调用内重发。后续继续先重新读取真实状态、形成新计划，已符合的引用返回 unchanged，不从历史 receipt 推断成功。
