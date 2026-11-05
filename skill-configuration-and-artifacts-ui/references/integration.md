# 共用代码接入

以下命令由 Agent 执行，路径先在本机解析并按实际 Shell 引用。示例中的 `<本Skill>`、`<目标>`、`<项目>` 为实际绝对路径；业务目标的运行环境由其入口负责。

## 代码边界

- `assets/runtime/skill_ui/`：无第三方依赖的 Python 服务与原生 JS/CSS 页面。Python 3.10+ 语法；本开发 Skill 固定 Python 3.13/uv 环境。
- `examples/studio_adapter.py` 与 `receipt-view.js`：合成数据示例，包含配置、六类产物、只读/依赖/高级字段及定制详情。它们不代表真实业务接入。
- `scripts/vendor.py`：检查并复制核心，生成 `.vendor.json` 的版本与逐文件 SHA-256。默认只检查；`--apply` 才应用，冲突退出 2。迁移暂存必须指定同一文件系统的已忽略临时目录，成功保留旧核心以便恢复。
- `scripts/ui.py`：本开发 Skill 预览入口，先进入自身锁定环境。业务发布包直接使用其已有运行环境调用 vendored `skill_ui`；不依赖本开发 Skill 或其安装路径。

在目标发布根选择独立核心目录，例如 `shared/skill_ui/`，每个 Plugin 一份。业务适配器、定制视图在 `ui/` 或目标已有正式目录维护，用户配置和记录留在原位置。

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
| `config()` | `{revision, fields, validation?, note?, task?}`；返回真实生效值与编辑语义。敏感字段用 `sensitive: true`，核心在普通响应中替换为掩码 |
| `save(changes, revision)` | 变更为 `{field_id: {op: "set", value: ...} 或 {op: "unset"}}`；校验旧 revision、只读/依赖/类型/必需项后按原协议原子保存，返回新 config。校验失败抛安全的 `UIError` |
| `artifacts()` | `{items, note?}`，将真实记录映射成可显示条目。集合条目仅 `id/title` 必需，其余按实际业务提供 |
| `resource(id)` | 返回 `Resource(Path或bytes, mime, name)`；只允许已归属本项目且允许用户取用的成果。ID 查白名单，不把请求 ID 拼成文件路径 |
| `extensions()` | 可选 `{文件名: 已打包文件Path}`，仅登记开发者可信 JS/CSS。JS 导出 `register(add)`，调用 `add(renderer名称, (container,data,helpers)=>...)`；示例展示回执 |

`JsonConfig` 仅供明确采用单 JSON 文件协议的新目标/合成示例复用；已有 dotenv、全局/项目分层或多文件事务通过目标原加载器实现，不迁入示例存储。它保留未知 key，以 HMAC 修订号检测变化，OS 锁协调同协议写入，临时文件加原子替换；不能防止不参与锁的外部写入者在最终检查后并发修改。多来源 revision 必须覆盖所有影响实际值的输入；多文件保存必须先确认原子/回滚方案。

字段的 `type` 为 `text/number/boolean/select/list/json`，枚举 `choices` 为字符串列表。`group/advanced/readonly/required/sensitive/help` 控制展示，`default/explicit` 保留默认与显式语义；`depends_on` 是字段 ID 到值的等值依赖，复杂联动可扩展业务控件及服务端校验。`scope/path/effect/clear_help` 说明固定写入位置和实际效果。敏感字段的帮助、元数据、任务、异常及默认文本不得带原值。眼睛读取与保存均复核 revision；掩码永远不是保存值。

`task` 可包含安全的 `id/binding/continuation`。保存后服务写入独立实例的 `saved.json`，`status` 返回当前实例回执。等待中的 Agent 核对原任务、项目、授权和输入绑定，且配置满足后继续；已结束会话用 continuation 提示词。核心不执行业务请求。

产物条目按需提供 `kind/task/summary/status/metadata/resource/url/copy_text/preview`。`url` 仅允许 HTTP(S)，不提供失效链接。内置预览为 text、markdown（安全基础文本排版）、image、audio、video、table、comparison；复杂 Markdown/PDF/交互视图采用按目标确认的渲染器。`custom` 使用 `renderer/data`；定制视图通过 DOM textContent 插入不可信数据。下载采用分块和单 Range；超大集合或重型文档需适配器分页、摘要或定制视图，不能把全库塞入浏览器。

Windows 私有运行目录和保存文件仅向执行账号及所选项目所有者授予访问，不改变已有业务父目录的 ACL。`JsonConfig(..., owner_path=project)` 可明确所有者基准（尤其配置位于新建子目录时）；默认使用配置父目录。由不同 Windows 沙箱账号运行的服务须从同一原生入口核验归属与权限，不能通过放开整个项目权限让测试通过。

## 运行示例与业务入口

先创建一个已忽略的合成项目目录。分别启动两个页面：

```text
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page configuration --runtime-dir <演示项目>/tmp/ui
uv run --frozen --project <本Skill> python <本Skill>/scripts/ui.py start --adapter <本Skill>/examples/studio_adapter.py --project <演示项目> --page artifacts --runtime-dir <演示项目>/tmp/ui
```

必要配置入口在返回 URL 的 fragment 前加 `?mode=required`，主动管理用 `?mode=all`（默认）。`status`、`stop` 使用相同 adapter/project/page/runtime-dir 参数；每个页面独立停止。`--idle-seconds` 默认 3600，短期限仅用于验收。

业务入口先通过自身 bootstrap 固定环境，再将 `<发布根>/shared` 加入 `sys.path`，调用 `skill_ui.__main__.main()`（参数协议相同）。Python 子进程使用当前解释器、固定包目录；无 GUI 窗口。配置命令仅传本地路径和任务标识，不把凭证放参数。运行目录位于已确认的忽略位置，`server.json` 包含私有访问 token；`service.log` 轮转为 1 MiB × 3，记录事件、错误类型和栈位置，不记录请求体及密钥。`launcher.log` 仅含启动诊断。

跨设备先按 [访问规则](access-and-lifetime.md) 准备实际通道，再用 `--public-origin https://实际入口` 重开指定页面。默认监听 loopback，代理应转发正确 Host，不任意信任 forwarded headers。不要只换链接域名绕过 origin 检查。安装后从发布包运行验证，开发源能运行不等于目标自包含。
