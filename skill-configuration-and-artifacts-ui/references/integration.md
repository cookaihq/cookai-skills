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
| `config()` | `{revision, layers, fields, default_targets?, presentation?, readiness?, validation_errors?, validation?, note?, task?}`；返回真实加载器计算的生效结果、每层状态及允许的保存目标。敏感字段用 `sensitive: true`，核心对生效值和所有层的值脱敏 |
| `preview(changes, revision, target)` | 变更为 `{field_id: {op: "set", value: ...} 或 {op: "unset"}}`，`target` 是层 ID；校验修订号和目标，内存应用所选文件的修改，再用同一加载逻辑解析。返回候选 config，`validation_errors` 为安全消息列表；不写文件、不写保存回执 |
| `save(changes, revision, target)` | 再次校验输入、目标写权限、revision、类型、枚举、依赖及关联完整性，原子保存所选文件，重新加载后返回 config。不相关的必要缺项不阻止有效修改；不能信任先前预览；失败抛安全的 `UIError`，不部分保存 |
| `artifacts()` | `{items, note?}`，将真实记录映射成可显示条目。集合条目仅 `id/title` 必需，其余按实际业务提供 |
| `resource(id)` | 返回 `Resource(Path或bytes, mime, name)`；只允许已归属本项目且允许用户取用的成果。ID 查白名单，不把请求 ID 拼成文件路径 |
| `extensions()` | 可选 `{文件名: 已打包文件Path}`，仅登记开发者可信 JS/CSS。JS 导出 `register(add)`，调用 `add(renderer名称, (container,data,helpers)=>...)`；示例展示回执 |

`JsonConfig` 仅供明确采用单 JSON 文件协议的新目标/合成示例复用；已有 dotenv、全局/项目分层或多文件事务通过目标原加载器实现，不迁入示例存储。它保留未知 key，以 HMAC 修订号检测变化，OS 锁协调同协议写入，临时文件加原子替换；不能防止不参与锁的外部写入者在最终检查后并发修改。多来源 revision 必须覆盖所有影响实际值的输入；多文件保存必须先确认原子/回滚方案。

`JsonConfig.save` 自动准备配置、锁和随机临时文件的 Git 忽略保护。自有保存协议可复用 `storage.prepare_private_write(path, owner_path=project)`：在每次实际写入前调用，拒绝跟踪文件，必要时在文件所在目录追加精确 `.gitignore` 规则，并复核 Git 结果。`file_lock(..., protect_git=True, owner_path=project)` 和 `atomic_json(..., protect_git=True, owner_path=project)` 接入相同检查；通用运行状态写入仍由调用方保证运行目录私有且被忽略，不把配置保存行为扩展到只读启动或预览。

自有原子写入的随机临时文件先调用 `prepare_private_write(temporary, temporary_for=target, owner_path=project)`，名字需为 `<原文件名>.<随机部分>.tmp`，生成一条稳定的临时文件模式。事务的 journal 写入前调用 `prepare_private_write(journal, private_directory=recovery_dir, owner_path=project)`，保护整个专用恢复目录并拒绝其中的已跟踪内容；恢复目录须在仓内且不是仓根。该辅助不提供多文件事务或回滚，不代替业务 ACL、revision、目标白名单和原有事务机制。预览不调用它；保存先验证输入，再准备保护，锁内重查 revision 和实际写入条件。调用失败抛安全 `UIError`，页面草稿保留；忽略规则可保留供重试。

`layers` 按加载器的真实高到低顺序返回；每层为 `{id, name, scope, scope_label, path, readonly, enabled}`。`scope` 用 `project/global/process` 或业务自定义值分类，`scope_label` 说明共享影响。`path` 是准确位置或只读注入来源说明。全局关闭时可展示已声明层及 `enabled:false`，不得为展示扫描加载器未知的目录。客户端提交层 ID，不提交任意路径；适配器用自己的白名单映射保存文件。

字段的 `type` 为 `text/number/boolean/select/list/json`，`choices` 是字符串列表；`group/advanced/readonly/required/sensitive/help/effect` 控制展示。`active` 是加载器按实际依赖计算的布尔值，`depends_on` 可描述等值依赖，但前端不自行解析层级。绑定记录以一个 `json` 字段提交整个对象，由业务校验完整性。

字段 `id` 是编辑、草稿、依赖与 API 提交使用的内部身份；`config_key` 是不含值的真实配置字段名或路径，例如 `{id:"credential_control", config_key:"SERVICE_API_KEY"}` 或 `{id:"routing", config_key:"routing.default"}`。业务适配器声明并维护实际映射，写入仍按原协议映射；核心只透传及转义显示，不把 `config_key` 当作磁盘路径或写入选择器。没有声明时不猜测 ID 就是真实 Key，接入验收须补齐实际映射。`JsonConfig` 和分层 JSON 示例按其真实顶层 JSON key 生成 `$["api_key"]` 等路径，不能把这类路径直接用于 dotenv。敏感字段的 `config_key` 可公开，原值只通过既有显隐接口取得。

每个字段必须提供：

- `effective: {source, status, value, message?}`：`source` 为真实层 ID、`"default"` 或 `null`；`status` 为 `set/default/missing/optional/error/invalid`，不能把读取失败报成正常继承。
- `rows: [{layer, present, status, value, message?}]`：逐层显式值与状态；`status` 为 `effective/shadowed/empty/missing/skipped/error`。缺项与空值分开，不由 UI 猜测。
- `targets: [层ID]`：该字段允许写入的文件；只读字段为空列表。环境层只读。新增密钥的文件限制须同时在此处和保存方法落实。

`default_targets` 映射范围到默认保存层，例如 `{"project":"project_local","global":"plugin_env"}`；不表示读取优先级。单 JSON 目标同样返回一层及上述字段结构，无需伪造全局层。缺少 `layers` 的旧适配器明确报协议迁移错误，不静默计算继承。

`presentation` 可提供安全的 `title/description/focus/initial_target`，其中 `focus` 为本次优先展示的字段 ID 列表，`initial_target` 为初始写入层 ID。Agent 在每次打开时可通过启动参数 `--fields api_key,format --target project_local` 覆盖默认展示；只传 ID，不传值或密钥。未知 ID 被忽略，URL 不增加写权限。明确给空 `--fields ""` 仅先显示必要缺项；未指定焦点且没有缺项时，模板展示前三个非高级适用项作为兜底，业务应声明合适的默认焦点。页面把当前必要缺项并入默认字段，后续保持这些字段的位置；依赖新激活的缺项也会加入。

初始保存目标依次采用有效的本次 `target`、适配器 `initial_target`、相关字段已有的可写来源、声明的个人通用默认文件、项目默认文件。切换范围沿用该范围的上次选择或相关已有来源。字段 `targets` 和只读限制仍逐项检查，不允许写入的字段提供可用位置或只读说明。默认文件须由开发者声明，不能由读取优先级代替。

`readiness: {ready: boolean, missing: [字段ID]}` 描述业务是否已具备必要配置，不作为保存按钮的阻塞条件。核心在脱敏前补算 active + required 的空值或错误状态，保留适配器声明的业务缺项。`validation_errors` 仅放本次保存阻塞错误，不能把所有未填写必要项一律塞入。`storage.missing_required` 与 `validation_errors` 分别提供示例计算；后者保留已修改必需项、关联依赖和原子记录约束。真实业务的就绪条件与保存约束由原加载器定义，格式检查不代替业务鉴权。

普通状态、预览和保存响应中的敏感 `value` 由核心替换为 `mask/configured`；嵌套记录必须整体标为敏感或由适配器拆成安全展示，不向通用元数据塞原值。眼睛接口 `/api/reveal` 接收 `{id, revision, target}`，`target:null` 查看当前生效值，层 ID 查看该层值；均复核 revision。掩码永远不是保存值。帮助、错误、任务与来源文字也不得含凭证。

`/api/preview` 与 `/api/save` 接收同样的 `{changes, revision, target}`。修订号覆盖所有影响生效结果的文件、进程注入和启用开关；预览无副作用。不同文件的草稿彼此独立，保存一个文件后仍保留其他草稿，并以新快照重新预览。外部更改冲突返回 409，用户重载核对前不写入。

`task` 可包含安全的 `id/binding/continuation`。保存后服务写入独立实例的 `saved.json`，`status` 返回当前实例回执。页面留在原处并提示用户发送“配置已保存，请继续。”；Agent 收到后核对原任务、项目、授权、输入绑定和配置是否满足，再接续。核心不执行业务请求。

产物条目按需提供 `kind/task/summary/status/metadata/resource/url/copy_text/preview`。`url` 仅允许 HTTP(S)，不提供失效链接。内置预览为 text、markdown（安全基础文本排版）、image、audio、video、table、comparison；复杂 Markdown/PDF/交互视图采用按目标确认的渲染器。`custom` 使用 `renderer/data`；定制视图通过 DOM textContent 插入不可信数据。下载采用分块和单 Range；超大集合或重型文档需适配器分页、摘要或定制视图，不能把全库塞入浏览器。

Windows 私有运行目录和保存文件仅向执行账号及所选项目所有者授予访问，不改变已有业务父目录的 ACL。`JsonConfig(..., owner_path=project)` 可明确所有者基准（尤其配置位于新建子目录时）；默认使用配置父目录。由不同 Windows 沙箱账号运行的服务须从同一原生入口核验归属与权限，不能通过放开整个项目权限让测试通过。

## 运行示例与业务入口

从旧核心升级时，配置适配器须一并迁移：旧 `value/default/explicit/scope/path` 字段改为 `effective/rows/targets`，补齐层清单、无写入的预览方法和显式 `target` 保存参数。包内单文件与分层示例均使用新协议；已发布业务中的固定核心副本不会因开发 Skill 更新自动改变。升级目标应在其原加载器上实现上述方法，通过原命令和新 UI 验收后再带入新核心；仅使用产物接口的适配器方法不变。核心不保留旧配置协议的推测回退分支。

先创建一个已忽略的合成项目目录。以下是两个可独立选用的模板示例命令；只启动已选择的页面，两个都选才分别启动。共用核心当前按 `--page` 提供独立页面，不代表定制统一界面也须采用此结构：

```text
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page configuration --runtime-dir <演示项目>/tmp/ui
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page artifacts --runtime-dir <演示项目>/tmp/ui
```

每次 `start` 可传 `--fields api_key,format` 与 `--target project_local`，返回 URL 会在 fragment 前携带对应 `fields/target` 查询参数；复用同一服务也能返回不同焦点链接。首次补填可传空字段列表；主动查看或修改由 Agent 根据上下文选 ID，无需用户选择场景。旧 `mode/advanced` 查询不再控制新表单，升级业务入口时一并移除。`status`、`stop` 使用相同 adapter/project/page/runtime-dir 参数；每个页面独立停止。`--idle-seconds` 默认 3600，短期限仅用于验收。

审阅分层交互时，将配置示例的 adapter 换为 `<本Skill>/examples/layers_adapter.py`。它在演示项目的 `demo-layers/` 创建合成 JSON 文件，首次为项目本地文件写入假 Key，为全局共享文件写入其余基础项；不读写真实全局目录。它与单文件示例共用同一页面和接口，实际业务仍使用自己的配置协议。

业务入口先通过自身 bootstrap 固定环境，再将 `<发布根>/shared` 加入 `sys.path`，调用 `skill_ui.__main__.main()`（参数协议相同）。Python 子进程使用当前解释器、固定包目录；无 GUI 窗口。配置命令仅传本地路径和任务标识，不把凭证放参数。运行目录位于已确认的忽略位置，`server.json` 包含私有访问 token；`service.log` 轮转为 1 MiB × 3，记录事件、错误类型和栈位置，不记录请求体及密钥。`launcher.log` 仅含启动诊断。

跨设备先按 [访问规则](access-and-lifetime.md) 准备实际通道，再用 `--public-origin https://实际入口` 重开指定页面。默认监听 loopback，代理应转发正确 Host，不任意信任 forwarded headers。不要只换链接域名绕过 origin 检查。安装后从发布包运行验证，开发源能运行不等于目标自包含；在目标项目说明中接入 [重开入口](access-and-lifetime.md#重开入口)，使新会话能找到业务入口和实际参数。
