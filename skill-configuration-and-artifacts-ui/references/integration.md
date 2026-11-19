# 共用代码接入

本文件仅用于选用共用核心的接入分支。保留/改造已有界面或完全定制可不复制核心、不实现下述适配器；按目标原入口实现，并遵守实际能力对应的配置、产物和访问规则。局部复用只引入所需代码及其依赖，记录来源和定制边界。所有实现均须随目标发布并能独立启动，最终使用者无需安装本开发 Skill。

以下命令由 Agent 执行，路径先在本机解析并按实际 Shell 引用。示例中的 `<本Skill>`、`<目标>`、`<项目>` 为实际绝对路径；业务目标的运行环境由其入口负责。

## 代码边界

- `assets/runtime/skill_ui/`：无第三方依赖的 Python 服务与原生 JS/CSS 页面。Python 3.10+ 语法；本开发 Skill 固定 Python 3.13/uv 环境。
- `examples/studio_adapter.py` 与 `receipt-view.js`：单文件配置和六类产物示例，含只读/依赖/高级字段及定制详情。`examples/layers_adapter.py` 提供十二层合成 JSON 加载器，包含 Plugin 项目的 local/普通共享来源，也保留 Skill 专用来源；项目仅覆盖 Key、全局提供其余基础项，所有文件均在演示项目内。示例不代表真实业务接入，层数及顺序也不是共用核心内置规则；目标的退出来源政策由业务适配器落实。
- `scripts/vendor.py`：检查并复制核心，生成 `.vendor.json` 的版本与逐文件 SHA-256。默认只检查；`--apply` 才应用，冲突退出 2。迁移暂存必须指定同一文件系统的已忽略临时目录，成功保留旧核心以便恢复。
- `scripts/ui.py`：本开发 Skill 预览入口，先进入自身锁定环境。业务发布包直接使用其已有运行环境调用 vendored `skill_ui`；不依赖本开发 Skill 或其安装路径。

在目标发布根选择独立核心目录，例如 `shared/skill_ui/`，每个 Plugin 一份。业务适配器、定制视图在 `ui/` 或目标已有正式目录维护，用户配置和记录留在原位置。配置入口与调用方按 [配置方式与存量改造](configuration.md#配置方式与存量改造) 接续，清理已退出的路径；复用真实加载器不意味着保留旧的取值或配置引导流程。

```text
uv run --frozen --project <本Skill> python <本Skill>/scripts/vendor.py --target <目标>/shared/skill_ui
uv run --frozen --project <本Skill> python <本Skill>/scripts/vendor.py --target <目标>/shared/skill_ui --apply --staging-dir <目标项目>/tmp/skill-ui-vendor
```

重复接入先检查清单及目标定制。`current` 不写文件；`conflict` 列出修改、缺失或未知文件，先对比并提出处理方案。脚本不合并业务定制，不更新原配置。应用采用目录改名；目标内的自定义文件应先按确认方案迁到核心外。此操作须单独串行执行；目标构建器若并发升级，应使用自己的任务锁。保留的恢复目录须按目标清理规则处理。

## 适配器协议

入口是可信 Python 文件中的 `build(project: Path) -> Adapter`。构造器只加载 UI 所需本地定义和记录，不能要求业务密钥齐备或发起业务请求。服务固定该项目；共享核心不扫描项目或磁盘。

接口定义在 `assets/runtime/skill_ui/contracts.py`。实现所选页面的方法即可：

| 方法 | 输入/输出与职责 |
| --- | --- |
| `config()` | `{configuration_protocol:"field-scopes-v1", revision, layers, fields, default_targets?, presentation?, readiness?, validation_errors?, validation?, note?, task?}`；返回真实加载器计算的生效结果、每层状态及允许的保存目标。敏感字段用 `sensitive: true`，核心对生效值和所有层的值脱敏 |
| `preview(changes, revision)` | `changes` 为 `{files, switches?, file_edits?, directory_revision?}`，具体结构见下文。校验 revision 与逐字段目标，在内存中应用完整提交，用原加载器解析；返回候选 config、安全的 `validation_errors` 和 `scope_plans`。不写文件、忽略规则或回执 |
| `save(changes, revision)` | 复核同一结构、revision、类型、允许目标与关联约束，原子提交本次依赖组的所有文件与必要高层清理，回读验证来源后返回 config。不得信任先前预览；失败抛安全 `UIError`，该组不部分保存；HTTP 批次由核心按真实依赖拆分 |
| `artifacts()` | `{items, note?}`，将真实记录映射成可显示条目。集合条目仅 `id/title` 必需，其余按实际业务提供 |
| `resource(id)` | 返回 `Resource(Path或bytes, mime, name)`；只允许已归属本项目且允许用户取用的成果。ID 查白名单，不把请求 ID 拼成文件路径 |
| `extensions()` | 可选 `{文件名: 已打包文件Path}`，仅登记开发者可信 JS/CSS。JS 导出 `register(add)`，调用 `add(renderer名称, (container,data,helpers)=>...)`；示例展示回执 |

`JsonConfig` 仅供明确采用单 JSON 文件协议的新目标/合成示例复用；已有 dotenv、全局/项目分层或多文件事务通过目标原加载器实现，不迁入示例存储。它保留未知 key，以 HMAC 修订号检测变化，OS 锁协调同协议写入，临时文件加原子替换；不能防止不参与锁的外部写入者在最终检查后并发修改。多来源 revision 必须覆盖所有影响实际值的输入；多文件保存必须先确认原子/回滚方案。

`JsonConfig.save` 自动准备配置、锁和随机临时文件的 Git 忽略保护。自有保存协议可复用 `storage.prepare_private_write(path, owner_path=project)`：在每次实际写入前调用，拒绝跟踪文件，必要时在文件所在目录追加精确 `.gitignore` 规则，并复核 Git 结果。`file_lock(..., protect_git=True, owner_path=project)` 和 `atomic_json(..., protect_git=True, owner_path=project)` 接入相同检查；通用运行状态写入仍由调用方保证运行文件受到必要保护，位于仓内时已忽略，不把配置保存行为扩展到只读启动或预览。

自有原子写入的随机临时文件先调用 `prepare_private_write(temporary, temporary_for=target, owner_path=project)`，名字需为 `<原文件名>.<随机部分>.tmp`，生成一条稳定的临时文件模式。事务的 journal 写入前调用 `prepare_private_write(journal, private_directory=recovery_dir, owner_path=project)`，保护整个专用恢复目录并拒绝其中的已跟踪内容；恢复目录宜使用用户本地状态位置；位于仓内时不能是仓根。该辅助不提供多文件事务或回滚，不代替业务 ACL、revision、目标白名单和原有事务机制。预览不调用它；保存先验证输入，再准备保护，锁内重查 revision 和实际写入条件。调用失败抛安全 `UIError`，页面草稿保留；忽略规则可保留供重试。

`layers` 按加载器的真实高到低顺序返回；每层为 `{id, name, scope, scope_label, path, readonly, enabled, exists?}`。`scope` 用 `project/global/process` 或业务自定义值分类，`scope_label` 说明共享影响。`path` 是准确位置或只读注入来源说明。全局关闭时可展示已声明层及 `enabled:false`，不得为展示扫描加载器未知的目录。客户端提交层 ID，不提交任意路径；适配器用自己的白名单映射保存文件。

字段的 `type` 为 `text/number/boolean/select/list/json`，`choices` 是字符串列表，`choice_labels` 可提供安全显示名；`group/advanced/readonly/required/sensitive/help/effect` 控制展示。`active` 是加载器按实际依赖计算的布尔值，`depends_on` 可描述等值依赖；`dependencies` 列出其他真实依赖字段，`atomic_group` 标识不可拆分记录，但前端不自行解析层级。绑定记录以一个 `json` 字段提交整个对象，由业务校验完整性。

字段 `id` 是编辑、草稿、依赖与 API 提交使用的内部身份；`config_key` 是不含值的真实配置字段名或路径，例如 `{id:"credential_control", config_key:"SERVICE_API_KEY"}` 或 `{id:"routing", config_key:"routing.default"}`。业务适配器声明并维护实际映射，写入仍按原协议映射；核心只透传及转义显示，不把 `config_key` 当作磁盘路径或写入选择器。没有声明时不猜测 ID 就是真实 Key，接入验收须补齐实际映射。`JsonConfig` 和分层 JSON 示例按其真实顶层 JSON key 生成 `$["api_key"]` 等路径，不能把这类路径直接用于 dotenv。敏感字段的 `config_key` 可公开，原值只通过既有显隐接口取得。

每个字段必须提供：

- `effective: {source, status, value, empty, message?}`：`source` 为真实层 ID、`"default"` 或 `null`；`status` 为 `set/default/missing/optional/error/invalid`，不能把读取失败报成正常继承。
- `rows: [{layer, present, status, value, message?}]`：逐层显式值与状态；`status` 为 `effective/shadowed/empty/missing/skipped/error`。缺项与空值分开，不由 UI 猜测。
- `targets: [层ID]`：该字段允许写入的文件；只读字段为空列表。环境层只读。新增密钥的文件限制须同时在此处和保存方法落实。

`default_targets` 映射范围到默认保存层，例如 `{"project":"project_local","global":"plugin_env"}`；不表示读取优先级。单 JSON 目标同样返回一层及上述字段结构，无需伪造全局层。缺少 `layers` 或 `configuration_protocol:"field-scopes-v1"` 的旧适配器明确报协议迁移错误，不静默计算继承。

`presentation` 可提供安全的 `title/description/focus/initial_target`，其中 `focus` 为本次优先展示的字段 ID 列表，`initial_target` 为初始写入层 ID。Agent 在每次打开时可通过启动参数 `--fields api_key,format --target project_local` 覆盖默认展示；只传 ID，不传值或密钥。未知 ID 被忽略，URL 不增加写权限。明确给空 `--fields ""` 仅先显示必要缺项；未指定焦点且没有缺项时，模板展示前三个非高级适用项作为兜底，业务应声明合适的默认焦点。页面把当前必要缺项并入默认字段，后续保持这些字段的位置；依赖新激活的缺项也会加入。

逐字段初始目标依次采用本项已有可写来源、有效的本次 `target`／适配器 `initial_target`、允许的全局默认文件、其余允许文件。入口指定的默认位置只影响没有可写保存来源的项。无有效文件来源且 `effective.empty:true` 时突出默认保存位置，不打生效勾。后续清空输入不改已有草稿目标；只查看范围沿用上次查看的文件，不能移动草稿。字段 `targets` 和只读限制仍逐项检查，不允许写入的字段提供可用位置或只读说明。默认文件须由开发者声明，不能由读取优先级代替。

`readiness: {ready: boolean, missing: [字段ID]}` 描述业务是否已具备必要配置，不作为保存按钮的阻塞条件。核心在脱敏前补算 active + required 的空值或错误状态，保留适配器声明的业务缺项。`validation_errors` 仅放本次保存阻塞错误，不能把所有未填写必要项一律塞入。`storage.missing_required` 与 `validation_errors` 分别提供示例计算；后者保留已修改必需项、关联依赖和原子记录约束。真实业务的就绪条件与保存约束由原加载器定义，格式检查不代替业务鉴权。

普通状态、预览和保存响应中的敏感 `value` 由核心替换为 `mask/configured`；嵌套记录必须整体标为敏感或由适配器拆成安全展示，不向通用元数据塞原值。眼睛接口 `/api/reveal` 接收 `{id, revision, target}`，`target:null` 查看当前生效值，层 ID 查看该层值；均复核 revision。掩码永远不是保存值。帮助、错误、任务与来源文字也不得含凭证。

`/api/preview` 接收 `{changes, revision}`，`/api/save` 另接收唯一 `request_id`；出现旧的顶层 `target` 直接返回 422，不自动兼容。`/api/reveal` 的 `target` 含义不变。新结构：

```json
{
  "files": {"project_local": {"model": {"op": "set", "value": "new-model"}}},
  "switches": {"model": {"target": "plugin_env", "mode": "input"}},
  "file_edits": {}
}
```

`files` 保存各真实目标的 `set/unset` 草稿。`switches` 仅包含用户明确提出的逐项切换；`mode` 为 `input`（转存当前内容）或 `existing`（未修改时用目标原值）。显式范围选择使用 `choose_content:true`；目标不同值时必须传回本次预览的 `choice_token` 与 `choice:"input"|"target"`，否则计划为 `choice`，不可保存。Token 用服务端私有 key 绑定输入、目标、模式及本项 edit_revision；不能包含明文密钥。`file_edits` 为 `{层ID:[字段ID]}`，标记明确删除／清空等文件操作，防止它们被普通“改回基线”过滤；与同项切换组合时拒绝，先保存或撤销原文件操作。

适配器可复用 `scopes.annotate_snapshot(snapshot, secret, empty=业务判断)`：添加协议标记、字段 `edit_revision`（已保存值/来源/目标的 HMAC）和 `effective.empty`。默认只将 null 与空白字符串判空，JSON `[]`、false、0 保留；dotenv 列表编码等由业务显式提供判空方法。随后用 `ScopePlanner(snapshot, secret, project).plan(changes, revision)` 生成切换；`project(files)` 必须把本次全部文件一起投影并通过原加载器计算，返回 `(candidate_snapshot, opaque_modifications)`。Planner 返回同一元组，合入逐项 `scope_plans`、`unchanged_fields` 及验证错误；无写入时 modifications 为 `{}`。业务保存先拒绝所有错误，再在原事务锁内重核、提交、真实回读，不把预览对象直接当保存证明。

`scope_plans` 公开 `target/state/cleanup/cleanup_token/writes_target/conflict/choice_token/input_changed/uses_target/message`；`state` 为 `ready/choice/blocked`。层和字段白名单必须在任何写入前验证。更高只读／不可读来源阻断切换，目标值相同仍可能需要清理。修订号覆盖所有参与来源、进程注入与开关；单项依赖不能借用本次未提交字段。

共用前端扩展使用 `draftValue(id)`、`stage(id,change)`、异步 `requestSwitch(id,target,mode)`、`selectionHTML(id)`、`subscribe(listener)`。选择器与主表共享草稿；选择完成只关闭面板并提示底部保存。`requestSwitch` 在用户确认后返回 true，取消返回 false，不写盘；调用方不得在取消前破坏原草稿。扩展自己的按钮须订阅 `busy/expired/uncertain/needsReview`，缓存刷新不改配置。

3.0.0 不再支持扩展 `save(id或ID数组)` 立即保存。调用方须迁移到 `stage` 和异步切换；底部表单独占持久化入口。HTTP 保存现在可能返回 200 加逐项失败，必须读取 `save_status` 和 `field_results`，不能把 HTTP 成功当成所有字段成功。这些是本次 MAJOR 的调用契约变化；固定旧版核心的自定义消费者应修复自己的保存入口，不能盲换核心。

核心 `batches.run` 以 `dependencies/depends_on/atomic_group` 构造依赖组，各组调用适配器的原子 preview/save。客户端 `changes.field_revisions` 提交编辑时的逐字段 HMAC，`invalid_fields` 标记无法编码的草稿；服务端仍独立校验所有值。`field_results` 按字段返回 `ready/saved/failed/uncertain`、安全 code/message 和组信息。保存的 `save_status` 为 `saved/partial/failed/uncertain`，只消费 saved；未确认结果停止后续组。清理必须带本次 `cleanup_token`，不相关字段变化不撤销确认。

每次保存生成 `request_id`。服务在返回前记录不含值的逐项结果，响应丢失通过 `/api/save-result` 查询同一 ID；同 ID 不执行第二次写入，不同内容复用 ID 返回冲突。未知或恢复受阻时保留草稿并停止相关重试；不能仅按 HTTP 状态判断已写入。实例结束后内存回执不能恢复，须核对事务与实际文件，不能自动重放。

扩展通过 `stage` 写入时须立即同步主表可见输入，不等预览成功；否则到期或错误时会显示旧值而隐藏最新草稿。主表或其他入口保存成功后，扩展同步消费对应字段已经完成的转存标记；后续只改收藏等其他项时，不得因残留标记再次提交已保存字段。未消费的选择与目标仍须保留并按新快照核对。

局部成功只消费本次字段，其余草稿保留并比较 `edit_revision`；已保存基线变了要求人工核对，相关项旧 choice/cleanup token 作废，无关项仍有效。HTTP 409 保留草稿；保存结果未知先回读，不自动重发。JSON 分层合成示例使用 `transactions.JsonTransaction` 的私有 journal、文件白名单、失败精确回滚和中断恢复；这是新 JSON 存储可选示例，不代替已有业务事务。目录示例传入可选 `commit(..., check=复核函数)`，在事务捕获 before 之后、写 journal 之前复核当前和候选根的所有读取输入，逐文件替换前仍检查 before。锁只协调同协议写入者，不提供任意外部读取者的跨文件瞬时隔离。

`task` 可包含安全的 `id/binding/continuation`。保存后服务写入独立实例的 `saved.json`，`status` 返回当前实例回执。页面留在原处并提示用户发送“配置已保存，请继续。”；Agent 收到后核对原任务、项目、授权、输入绑定和配置是否满足，再接续。核心不执行业务请求。

产物条目按需提供 `kind/task/summary/status/metadata/resource/url/copy_text/preview`。`url` 仅允许 HTTP(S)，不提供失效链接。内置预览为 text、markdown（安全基础文本排版）、image、audio、video、table、comparison；复杂 Markdown/PDF/交互视图采用按目标确认的渲染器。`custom` 使用 `renderer/data`；定制视图通过 DOM textContent 插入不可信数据。下载采用分块和单 Range；超大集合或重型文档需适配器分页、摘要或定制视图，不能把全库塞入浏览器。

所有目录沿用操作系统和父目录默认权限，不主动收紧 ACL 或 chmod；Windows 私密文件仍向执行账号及所选项目所有者授予必要访问。`JsonConfig(..., owner_path=project)` 可明确所有者基准（尤其配置位于新建子目录时）；默认使用配置父目录。由不同 Windows 沙箱账号运行的服务须从同一原生入口核验归属与权限，不能通过放开整个项目权限让测试通过。

## 运行示例与业务入口

### 目录字段与组合投影

2.1.0 在 `field-scopes-v1` 上增加可选目录字段；没有声明的适配器继续使用原表单。目录字段声明 `directory_scope:"global"|"project"`、`type:"text"`、真实 `config_key`、`default`（完整默认根）、`anchor`（固定保存文件）和只含该锚点的 `targets`。它的 effective/rows 同样由真实加载器计算。共用 UI 据此显示顶部收起面板，空值或“恢复默认”提交该项的 unset，共享原 `files/switches/file_edits` 草稿与底部保存，不接受任意浏览路径命令。

适配器先用 `directories.selector_edits(snapshot, request)` 提取目录项，在内存中投影固定锚点后调用原加载器取得候选根与允许目标。再用 `retarget_request(current, prospective, request, destination)` 重映射明确业务编辑；`destination(field, old_layer)` 由业务适配器返回新层 ID，负责相对文件、settings.json、退出来源和路径政策。函数校验原／新白名单及固定目录目标，移除等于原基线的普通编辑，保留与新根值相同但原来有修改的意图；不读取磁盘、不创建文件、不定义业务目录。目录项不允许范围切换。

将重映射后的 envelope 交给 `ScopePlanner`，规划快照使用 `planning_snapshot(current, prospective)`：保留原有效值作为当前输入和差异比较基线，文件行、白名单和修订仍来自候选根。这样未编辑的“保存当前内容”不会误转存新根值，`existing` 模式仍选择目标已有内容。原组合投影与事务同时提交固定锚点及候选根业务文件。返回 `directory_targets:{字段ID:实际完整目标文件}`，只含本次编辑／切换的准确位置；公开投影仅保留已声明字段的路径，前端据此展示目标，并从候选 `layers` 解释新来源。

HTTP 目录预览返回 `preview_tokens`，按目录依赖组绑定当前与候选根中受影响的字段（包括未提交但会改变实际来源的字段）。客户端随本次 changes 原样带回；缺失或相关来源变化只使该组失败。核心在调用单组适配器前生成其 `directory_revision`；适配器仍在事务锁内复核候选来源。无关项并发变化合并保留，不借用未提交目录草稿，不能先存锚点再另存业务项。

默认目录、Key 名、环境优先、普通 Skill 回退是否退出、空值语义、写入权限及事务均属业务适配器，核心没有 AIhub/Tikin 默认值。参考 `examples/directories_adapter.py`：默认 JSON 文件保存目录项，先解析根再读取业务与全局 settings.json；所有自定义根限制在所选演示项目的 `demo-directories/` 中，不读取真实 home。将下方配置示例命令的 adapter 换成此文件即可体验。该限制仅保护合成演示，不能照搬成真实业务路径政策。

2.0.0 是配置协议的不兼容升级：1.4.0 支持的 `preview/save(changes,revision,target)` 改为二参数 envelope，HTTP preview/save 不再接收旧顶层 `target`，返回 422。原配置适配器与扩展调用方不能只替换核心；须添加 `field-scopes-v1`、逐项注解、组合投影与原事务接续，并将业务选择器接入共享草稿。共用配置模板、包内示例、Studio 和本批获准接入的 Tikin 已迁移。其他业务固定副本保持原版本，只有在目标自身获准升级、适配器与调用方迁移及验收后才替换。产物适配器协议不变。

先创建一个已忽略的合成项目目录。以下是两个可独立选用的模板示例命令；只启动已选择的页面，两个都选才分别启动。共用核心当前按 `--page` 提供独立页面，不代表定制统一界面也须采用此结构：

```text
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page configuration --runtime-dir <演示项目>/tmp/ui
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page artifacts --runtime-dir <演示项目>/tmp/ui
```

每次 `start` 可传 `--fields api_key,format` 与 `--target project_local`，返回 URL 会在 fragment 前携带对应 `fields/target` 查询参数；复用同一服务也能返回不同焦点链接。首次补填可传空字段列表；主动查看或修改由 Agent 根据上下文选 ID，无需用户选择场景。旧 `mode/advanced` 查询不再控制新表单，升级业务入口时一并移除。`status`、`stop` 使用相同 adapter/project/page/runtime-dir 参数；每个页面独立停止。`--idle-seconds` 默认 3600，短期限仅用于验收。

审阅分层交互时，将配置示例的 adapter 换为 `<本Skill>/examples/layers_adapter.py`。它在演示项目的 `demo-layers/` 创建合成 JSON 文件，首次为项目本地文件写入假 Key，为全局共享文件写入其余基础项；不读写真实全局目录。它与单文件示例共用同一页面和接口，实际业务仍使用自己的配置协议。

业务入口先通过自身 bootstrap 固定环境，再将 `<发布根>/shared` 加入 `sys.path`，调用 `skill_ui.__main__.main()`（参数协议相同）。Python 子进程使用当前解释器、固定包目录；无 GUI 窗口。配置命令仅传本地路径和任务标识，不把凭证放参数。运行目录使用用户可写本地状态位置（可用 `storage.user_state_directory`），保留真实 project 身份；协调锁按规范化目标路径跨项目复用。仓内运行目录须位于已确认的忽略位置，`server.json` 包含私有访问 token；`service.log` 轮转为 1 MiB × 3，记录事件、错误类型和栈位置，不记录请求体及密钥。`launcher.log` 仅含启动诊断。

跨设备先按 [访问规则](access-and-lifetime.md) 准备实际通道，再用 `--public-origin https://实际入口` 重开指定页面。默认监听 loopback，代理应转发正确 Host，不任意信任 forwarded headers。不要只换链接域名绕过 origin 检查。安装后从发布包运行验证，开发源能运行不等于目标自包含；在目标项目说明中接入 [重开入口](access-and-lifetime.md#重开入口)，使新会话能找到业务入口和实际参数。
