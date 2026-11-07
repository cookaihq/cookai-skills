# 设置与项目绑定

入口显式启用用户已选择的个人设置，默认保存到当前用户 `~/.config/agent-instructions-update/.env`；明确选择当前项目时只写 `<project>/.env.local`。配置 UI 与对话均使用 `config.read/config.save`。不用敏感业务配置，不添加凭证管理流程。

普通配置逐字段读取首个非空值：进程环境 → 所选工作目录 `.env.agent-instructions-update` → `.env.local` → `.env` → 已启用的个人 `.env`。只查这个目录，不向父级搜索。文件存在但无法读取时报错。只读不会补写默认值。

| 字段 | 实际 key | 默认/格式 |
| --- | --- | --- |
| `history_keep` | `AGENT_RULES_HISTORY_KEEP` | 30，正整数 |
| `binding` | `AGENT_RULES_BINDINGS` | JSON 对象，key 为 `global` 或项目规范绝对路径；每个值为一个完整绑定 |
| `review_models` | `AGENT_RULES_REVIEW_MODELS` | 按同一范围 key 保存模型 ID 数组；未选择复用时仅存任务 |

每个绑定须整组包含 `scope`（global/project）、`project`（项目绝对路径或 null）、`product`、`installation`（路径或 null）、`profile`、`paths` 数组、`authority`（路径或 null）、`layout`。不完整的高层记录报错，不跨层拼接。项目路径相对于绑定项目；全局相对路径以个人配置目录为基准，推荐保存绝对路径。绑定只保存选择，检测事实通过新检测取得。

`product` 使用五种支持产品的标识，未核实时为 null；安装、Profile 和布局同样允许 null，不能假造默认实例。全局绑定的 `project` 必须为 null，项目绑定必须与所选项目一致；非空权威路径必须包含在 `paths` 中。任务显式绑定按相同规则校验，并须覆盖本次输入。任务中传 `binding:null` 只表示本次按明确文件路径建立绑定，不清除已保存偏好。

`config.save` 请求含 `revision`、`save_scope:personal|project`、`values`（仅变动字段），可指定 `scope:global`，省略为当前项目。`values.binding:null` 显式取消该范围的绑定，不影响其他范围。返回实际保存位置、生效来源及 `overridden` 字段；高优先级设置仍生效时必须解释，不声称刚写入的值已生效。写入保留无关字段，旧修订失败后刷新，不盲目重试。保存偏好不搬规则、改规则、写宿主模型配置。

dotenv 支持等号两侧空白、成对单/双引号、整行注释、空行；同名取最后一个；`${X}`、`$(...)`、反引号和反斜杠按字面量，不执行 Shell。

`AUTO_UPDATE_CHECK` 不属于上述业务层级，只从个人同名目录 `.env` 读取，缺失默认开启，字面值 `0` 关闭。`config.read` 返回布尔字段 `auto_update` 及其独立来源；保存时只接受个人范围的布尔值，可与个人普通偏好一次保存。项目保存会拒绝此字段，个人业务配置关闭不影响独立更新读取。
