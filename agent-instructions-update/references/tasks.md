# 维护任务接口

Agent 使用安装实体的入口；项目是用户所选项目，默认为调用目录。运行入口会进入自己的锁定环境。请求用 UTF-8 JSON 文件或 stdin 传入，避免把正文拼进 Shell。

```text
uv run --frozen --project <Skill实体目录> python <Skill实体目录>/scripts/agent_rules.py --project <项目绝对路径> call <action> --input <请求JSON路径>
```

`--personal-dir` 默认当前用户 `.config/agent-instructions-update/`；隔离验收可指定测试目录。`--no-personal` 仅关闭本次个人业务配置，不改变独立更新开关或历史目录。输入相对文件路径应先按用户意图解析成绝对路径，不能让安装目录决定规则位置。

成功返回 JSON、退出 0；失败返回 `error/message`、退出 1。`task.apply` 中途失败也会返回一个成功读出的任务 JSON，必须检查其 `status` 和 `receipt`，不能只看进程退出码。每次交互都调用同一接口；持久任务在重启后仍可读取。

## 只读发现

| action | 请求 |
| --- | --- |
| `discover.scan` | `{}` 扫描当前用户原生环境；`custom` 可列出 `{product, installation?, home?, profile?, paths?, role?}`。产品标识 `codex/claude-code/workbuddy/hermes/opencode`；`paths` 为规则路径，未知职责保持 `unknown` |
| `index.read` | 可传 `paths` 补充需要跟随引用的入口，返回项目根、子目录及引用文件；引用不是自动加载证据 |
| `file.read` | `{path}` 返回当前正文、实体身份、入口链接、哈希和目录信息 |
| `workspace.read` | `{scope:"global"|"project"}` 返回启动项目上下文、主规则与嵌套场景、真实目录和读取关系；全局可指定 `rules_root`，引用基准使用 `reference_bases` |
| `scenario.title` | `{content}` 返回正文首个一级标题；无有效标题返回 `title:null`，跳过文首元数据、围栏代码及注释 |
| `config.read` | 可传 `scope: "global"`；默认当前项目，返回值、来源、保存位置及 `revision` |

`discover.scan` 的 `home` 和 `probe_system:false` 用于明确隔离的测试环境；它们不证明另一设备或平台可用。检测不运行发现的 Agent 程序，不读登录凭证；安装文件存在对应 `runnable:unverified`。实际 CLI、桌面及会话验证须另取证。

Markdown 相对链接按物理正式源文档目录解析。反引号路径或 `@import` 有明确基准声明时，Agent 在 `index.read`、`scenario.plan` 或 `layout.plan` 中传 `reference_bases:[{path:"主规则绝对路径",prefix:".agents/",base:"home"}]`；`base` 也可用已核实的项目或其他基准目录绝对路径。此声明仅作用于该文件中匹配前缀的路径引用，不改变 Markdown 链接含义。未提供依据的 `.agents/`、`.codex/`、`.claude/` 路径引用标为 `basis:unresolved, target:null`，相关改名、删除或迁移先停止；不能把未知基准猜成正式源目录。声明随引用索引绑定当前计划；重写会保留可解析的声明基准和目录入口，新位置无法保持此关系时须单独制定引用方案。

## 建立任务

以下请求均可带 `scope:"global"`；省略则绑定当前项目。`known_paths` 提供已发现的别名及相关入口，供展示共享影响和写前复核。文件系统不能枚举所有未知硬链接，Agent 应提供此次发现的全部相关入口。

编辑、同步和审阅均记录 `intent` 请求意图、完整 `binding`、绑定来源和模型事实。Agent 应传入用户本次请求的准确意图；省略时仅记录所选操作的通用意图并标明 `intent_source:operation`。绑定沿用所选范围的完整偏好，不跨层补齐；显式传半组对象（包括 `{}`）会报错。`binding:null` 表示本次按显式路径建立绑定：范围、项目、路径与同步权威按请求记录，产品、安装、Profile、布局保持 null，不借保存的偏好推定。绑定必须覆盖任务输入路径，范围及同步权威须一致；变更选择时提供完整新绑定。任务创建不保存设置。

```json
{"kind":"sync","authority":"/absolute/A.md","targets":[{"path":"/absolute/B.md","strategy":"merge"},{"path":"/absolute/C.md","strategy":"empty"},{"path":"/absolute/D.md","strategy":"overwrite"}],"known_paths":[]}
```

传给 `task.create`。仅 A+B 是合并输入。无合并时直接形成候选；存在合并时返回 `waiting_agent`。任何目标在生成候选前都不会写入。

已核实的文件职责可用 `classifications:[{path,role:"work"|"identity"|"memory"|"unknown",evidence:"可回查依据"}]` 传入；分类绑定实际实体及入口快照。同一实体的冲突分类报错，身份、记忆及未知职责不参加工作规则同步。逻辑路径、物理路径或已知共享入口出现身份文件名时，须先核实职责；确认是工作规则才传有依据的 `work` 分类，不能只改入口名称绕过检查。

```json
{"kind":"review","intent":"核对命令执行要求的触发条件，保留用户偏好","paths":["/absolute/AGENTS.md"],"models":{"configured":{"id":null,"source":"unknown"},"current":{"id":null,"source":"unknown"},"targets":["model-id"]},"evidence":[],"binding":null}
```

模型 ID 需注明来源，不能根据当前配置推定本会话模型。用户手动审阅目标可以有多个。未显式传入的审阅目标和绑定读取所选范围的已保存偏好；本次显式值覆盖这些偏好，任务创建不保存设置。

`task.create(kind=edit)` 的 `changes` 为有序列表：

| 操作 | change |
| --- | --- |
| 写正文 | `{path, content}` |
| 删除入口 | `{path, op:"delete"}`；目录必须为空 |
| 建目录 | `{path, op:"mkdir"}`；位置须不存在 |
| 建或改链接 | `{path, op:"link", target}`；目标须存在或在该计划更早创建。非空旧目录保留到计划列明的相邻恢复目录，不递归删除 |

恢复用的内部 `restore` 操作由 `history.restore` 生成，不手造快照。编辑同一实体的多项内容先合并成唯一候选。

## Agent 回传与确认

- `task.get` / `task.prompt` 请求 `{task_id}`；prompt 可复制给 Agent。来自 UI 和聊天的任务完全相同。
- 同步 `task.result` 请求 `{task_id,input_revision,content}`。
- 审阅 `task.result` 请求 `{task_id,input_revision,suggestions:[{id,path,line,evidence,impact,recommendation,verification,content}]}`。`content` 为该文件完整候选；`line` 为正整数；`evidence` 为可回查依据的非空文本列表。`verification` 为 `pending/text/behavior`，文本或行为已验证结论不得没有证据；`pending` 可明确缺少依据。工具校验格式不证明证据真实，Agent 仍须查证。
- `task.select` 请求 `{task_id,suggestions:["建议id"]}`，生成同一任务的具体计划。多个建议冲突到同一实体时先让 Agent 合成候选。
- `task.confirm` 请求 `{task_id,plan_revision}`；只在已经取得对当前计划的确认时使用。
- `task.apply` 请求 `{task_id}`；重复完成请求只读回既有结果，不再次写入。
- `task.get` 会检查未执行候选是否过期并保留候选；已完成回执保持原事实。
- `task.refresh` 请求 `{task_id}`；重读现场、保留旧候选修订并清除旧确认。审阅及合并需按新 `input_revision` 回传。编辑计划展示新现场与原候选的差异。场景重命名/删除须重新 `scenario.plan`，让新的引用集合进入计划。

原“空目标”已有外部正文或目标变成目录时，刷新报告 `strategy_changed` 并保留原任务；重新读取、明确选择覆盖或合并后建立新同步任务，不把旧空目标选择当作覆盖授权。

语义结果只允许修改原任务输入文件。需要新增其他目标时先建立范围明确的新任务，不能用建议携带任意副作用。

## 场景和布局

`scenario.plan`：

- `operation:add`：`path/content/entry/trigger`；正文须含一级标题，在主入口增加以该标题命名的“触发时读取并遵守”链接。可提供 `title` 将缺标题的正文补为 `# title`，它不作为独立显示别名保存。
- `operation:edit`：`path/content`。
- `operation:rename`：`path/destination`；同步修改已知 Markdown、反引号路径及 @import 引用，重新计算迁移后正文的相对链接。
- `operation:delete`：`path/reference_changes:[{path,content}]`；必须修复所有已知引用实体，候选不可仍指向删除对象。同一实体的多个入口可只提供一份候选，也可提供相同正文；冲突候选会被拒绝。被删除文件内部的自引用无需另行修改。
- 全局场景另传 `scope:"global", rules_root`；额外宿主入口用 `entrances` 明确补充。无法解析的自由文本、动态路径和宿主配置表达式由 Agent 查证后作为显式修改加入计划；静态索引不代替这个核对。

改名时已知文件符号链接入口与新正文一起重接；删除时这些入口列入同一待确认删除计划，不留下已知断链。共享正文联动与共享入口关系变化分别展示。已知硬链接或目录映射不能按文件入口安全改接时停止自动方案，由 Agent 明确逐项处理；不猜测删除或复制共享实体。

`layout.plan` 请求 `{root,source_main,source_scenarios?,entrances:[主规则入口],scenario_entrances?:[场景目录入口]}`。`root` 由用户选择；输出 `AGENTS.md` 与 `scenarios-agent-instructions/`，先复制已选正文并重算引用，再接入入口。目标已有不同内容时先完成审阅合并。嵌套链接或重叠迁移目录需单独核对，工具拒绝猜测。空目录可能作为运行创建的父目录保留，回执以逐项实际操作为准。

## 历史和恢复

`history.list` 可带 `scope`，返回当前项目与全局任务摘要。`history.restore {task_id}` 根据实际执行日志与当前现场生成新任务；展示原正文、入口类型及恢复位置，确认后执行。历史版本不是自动覆盖授权。

恢复链接正文与重新连接入口会分别列入计划、检查现场并记录回执；只恢复入口时预览当前目标内容，不回写其历史正文。刷新包含正文恢复的计划会保留将写入的候选。当前位置已被替换为其他实体目录时停止并报告冲突，保留现场；Windows junction 与符号链接都按入口处理，不把其目标当作可递归清理的备份。

`applying` 任务在进程锁释放后再被读取时会进入 `recovery_required`，未能记录完成的项标 `uncertain`。这时不能重放旧确认。先核对现场及备份，通过恢复计划处理。多文件部分失败停止后续修改，能证实安全时恢复本批已改内容；他人的后续修改保留。

`history.prune {keep:正整数, scope?:范围}` 清理超出数量的完成任务；默认 30。返回 `removed` 与 `retained`。完成任务的目录备份只在位置、身份和内容仍与该任务一致时逐项清理；备份变化或清理失败会保留任务及剩余备份，并返回原因，数量可能暂时超出设定值。历史清理失败不将已完成的规则写入误报为失败，检查 `history_warning`。未完成与待恢复任务不自动裁剪。不补造工具启用前的历史，不跨设备同步。

## 配置页面快捷入口

纯配置请求直接使用下方 `ui start`，保留真实项目、`--personal-dir`、`--no-personal` 和运行目录。入口已内建探活、同归属服务复用及失效重启。启动未确定时才用同参数 `ui status` 核查。

工作台已经支持 `view=settings`：在本次返回 URL 的 `#` 之前加入 `?view=settings`，保留原始授权 fragment，直接定位同一工作台的设置页。不要新建配置服务或预先扫描安装、索引规则、创建任务。交付有效链接及实际剩余空闲期限后结束；打开设置不授权规则迁移或同步。

## 工作台入口

```text
uv run --frozen --project <Skill实体目录> python <Skill实体目录>/scripts/agent_rules.py --project <项目绝对路径> ui start
```

`ui status` 和 `ui stop` 使用相同的项目及 `--personal-dir`（若曾指定）。默认运行目录在个人维护目录的 `ui/` 下；可显式传 `ui start --runtime-dir <已获准的私有运行目录>`，后续状态/停止也传同一路径。入口复用固定版本 UI 核心的会话、CSRF、Host/Origin 与生命周期；业务动作通过独立 `/api/rules` 调用维护接口，不借配置保存执行规则写入。

默认仅监听本机、空闲 3600 秒到期，后台读取不续期；实际交互续期。工作台同时包含配置，因此不提供常驻。`--idle-seconds` 可用于隔离验收；`--public-origin` 只传已验证代理入口的精确 origin。

启动目录向父级定位最近的项目标志（Git 或主规则）；无主规则时项目选项显示原因并置灰，仍可选择其他项目或预览建立规则的计划。项目目录选择后，文件、草稿、任务、绑定及历史各归对应范围。CLI 配置仍只读取显式 `--project` 的目录；页面选定的项目是后续操作的配置工作目录。

场景树按真实路径分组，主显示正文首个一级标题，副显示文件名；标题草稿同步更新名称，文件改名单独计划。设置集中在一个页面，一次保存显示实际位置和覆盖来源；配置冲突后可“重读来源并保留编辑”，核对最新来源后再保存。

交付本次有效链接与实际剩余空闲期限。首次附一条填好实际项目绝对路径的通用重开请求：“请打开项目【实际路径】中 agent-instructions-update 的规则维护工作台，给我新的访问链接。”以及“手机访问：告诉 Agent‘我要用手机打开这个页面。’”同会话重发同页面只给链接与期限。

重开先核对项目、个人目录和运行目录的服务归属，再复用或启动；停止与到期不删除任务。收到跨设备请求后再配置、验证实际通道与 origin。
