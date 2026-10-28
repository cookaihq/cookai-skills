# 项目配置协议

official 的项目流程使用 `--project <绝对路径>`。所有生命周期命令保持项目路径和 `--home` 一致；`--home` 默认是运行用户的 `~/.config/frpc-launch`，提供共享默认与二进制。项目身份由规范化绝对路径生成，symlink 同一实体得到同一身份；另一个 clone/worktree 是另一项目。移动项目之前先停止原位置的进程，再核对固定域名登记，不把目录移动当作运行实例迁移。

## 连接字段与来源报告

| 字段 | 用途 | 必填 / 敏感 / 默认 |
| --- | --- | --- |
| `FRPC_LAUNCH_SERVER_ADDR` | frpc 实际连接的 FRPS 主机 | 必填、非 Secret、无默认 |
| `FRPC_LAUNCH_SERVER_PORT` | FRPS 控制连接端口 | 必填、非 Secret、1–65535、无默认；不猜为网站端口 |
| `FRPC_LAUNCH_TOKEN` | 此 FRPS 的 token 认证凭证 | 必填、Secret、无默认；与前两项同源 |
| `FRPC_LAUNCH_DOMAIN_SUFFIX` | 首次派生项目域名的后缀 | 可选、非 Secret、无默认；也可明确给完整域名 |
| `FRPC_LAUNCH_FRPC` | 已有官方 frpc 可执行文件 | 可选、非 Secret；缺省使用受管客户端或已验证的 PATH 客户端 |
| `FRPC_LAUNCH_MODE` | `official` / `sakura` | 可选；两套配置同时存在时不任选 |
| `FRPC_LAUNCH_SAKURA_KEY` / `FRPC_LAUNCH_SAKURA_TUNNELS` | 樱花访问密钥 / 隧道 ID | 樱花模式必填，前者 Secret；不参与 official 连接组 |

本次输入地址、端口与**仅进程来源**的 `FRPC_LAUNCH_INIT_TOKEN` 构成一个完整请求组。它先于环境变量中的三字段组；之后依次读取项目 `.env.frpc-launch`、`.env.local`、`.env`、个人 `.env`。每层出现任一非空连接字段，就必须在该层补齐整组，否则返回 `incomplete_connection`，不向下一层拼接。全部没有组时，才从个人 `frpc.toml` 的 `serverAddr/serverPort/auth.token` 整组读取。

`.env` 是极简文本，不执行 shell、变量展开或命令替换。同名字段取最后一条；普通标量取首个非空来源。存在但不可读取的项目连接文件报错，不能悄悄换用别的账号。

`project-info` 与 `project-config` 的 `connection.fields` 输出每项 `source/configured/sensitive`，不含连接值。错误输出给 `result/source/missing/exit_code`，不引用 Secret 所在行。可交给 secret-book 按上述真实 key、同组字段和作用域管理；业务运行之后直接读取本机。

## 保存连接与路由

调用前已取得项目保存授权时，以进程环境注入 `FRPC_LAUNCH_INIT_TOKEN`，不要将真实值放进命令行、示例或日志：

```text
uv run --frozen --project <skill> <skill>/scripts/frpc_launch.py --project <项目> --json project-config --server-addr <实际主机> --server-port <实际连接端口>
```

此调用不要求本地端口，不启动 frpc。新请求或完整进程连接组写 `.env.local`，保留其他行并消除同 key 的旧重复项；回读验证后报告实际来源。高优先级 `.env.frpc-launch` 遮蔽不同连接时返回 `save_shadowed`，应修复其实际来源。已有项目或全局来源只被复用，不自动搬运 Token。

写项目 Secret 前确认目标是项目内普通文件，并通过 Git 的“未跟踪且已忽略”检查。Git 状态无法确认也拒绝写入；不能通过原生入口的旧绕过参数避开项目流程保护。`.frpc-launch/project.toml` 存 schema、固定域名和 `client` 原生客户端设置，连接认证字段留在 `.env.local`。帮助器不在此文件生成 Secret；已有敏感高级选项应先明确其保存方式。

配置示例（非 Secret）：

```toml
schema = 1
domain = "my-app.dev.example.com"

[[client.proxies]]
name = "project-unique-id-web"
type = "http"
localIP = "127.0.0.1"
localPort = 3000
customDomains = ["my-app.dev.example.com"]
locations = ["/api/"]
```

新建网站代理用 `--local-port`，可选 `--local-ip`、`--proxy-name`、`--proxy-type` 与可重复的 `--location`。默认代理名包含项目身份，避免同一 FRPS 上不同项目都注册为 `web`。第一次须明确 `--route-mode customDomains` 或 `subdomain`；后者须有已核实的 `--subdomain-host`，且与固定域名的父域匹配。前置 HTTPS 终止到 HTTP 时保持 `--proxy-type http`。

后续只传 `--local-port` 会保留原代理的域名、类型、路径和高级字段。`--proxy` 简写支持 `name/type/localIP/localPort/remotePort/customDomains/locations`，重复参数用于多条代理；按名称合并，不删除其他代理，不自动改变已有代理类型。原生 `client` 配置可保留简写之外的高级字段；`includes` 不自动加载，避免把别的项目代理引入当前进程。

## 固定域名与本机冲突

首次从本机后缀生成域名：项目目录名小写、空白和标点规范化为 `-`，无法得到 ASCII 名时使用稳定派生名。已明确的 `--domain` 可指定完整主机名。首次候选与另一项目冲突时追加项目身份短后缀；登记位于 `--home` 下 `domains.json`，与项目配置写入都有操作锁。

已保存的域名优先于后续后缀设置。显式换域名须 `--domain <新域名> --replace-domain`；对应原域名的路由随之定点修改，其他域名保留。subdomain 变更须重新核对 `--subdomain-host`。本机登记损坏或固定域名冲突时停止，不抢占、不清空登记。

本机登记只能发现这一受管目录已知的项目；远端注册冲突必须根据代理日志定位。需要换域名时取得对新映射的明确决定，核对 DNS/TLS/Nginx 后再保存；不把随机改名当成公网修复。共享同一域名的不同路径需核对实际设计，不擅自将既有路径改为 `/`。

## 运行状态、恢复与验证

每项目使用 `<home>/projects/<项目标识>/run/`，独立 `official.pid`、`official.log` 和环境模板 `official.toml`；樱花使用同目录的独立模式文件。全局 TOML 的其他代理不复制到项目；只复用连接及必要的客户端连接选项。项目的其他高级设置保留，日志输出定向当前项目的脱敏收集进程。

有效配置在进程内生成，通过 `FRPC_LAUNCH_RUNTIME_CONFIG` 交给 frpc 官方环境模板。磁盘模板只有环境引用；完整连接不另存到全局运行目录。真实 frpc 的 `verify` 也使用同一环境。机制依据：[frp v0.70.1 配置加载源码](https://github.com/fatedier/frp/blob/v0.70.1/pkg/config/load.go) 中 `RenderWithTemplate` / `Values.Envs`。

后台日志收集进程与 frpc 在独立进程组中，输出在写盘前脱敏。PID 记录包含项目身份、精确参数和进程创建时间；停止先核验，记录串到别的项目或 PID 被复用时不发信号。新进程在等待启动日志前就记录身份，因此调用中断后可重新检查或停止。失败清理本次进程，超时保留记录；不批量杀进程。

配置写入逐文件原子替换，普通写入异常会恢复此前文件；异常或中断后先 `project-info/status` 核对实际状态，不宣称多文件写入具备跨进程崩溃事务保证。运行中的 frpc 保留启动时环境快照，新的配置只会在明确停止并重启后使用。

`status` 区分运行时 `config_source`、当前来源和 `config_changed`；`verified` 是历史启动日志。每条代理注册、完整公网 URL 的正常 TLS 响应及预期业务内容仍须逐层验证。单次查询成功不能证明整个应用目标完成。

## 保留的完整原生接口

已有调用可以继续省略 `--project`，以 `FRPC_LAUNCH_CONFIG` 指向完整 TOML，以 `--home` 指向原受管目录。原来的 `start/status/logs/stop` 及 `<home>/run/<mode>.pid/.log` 保持可用。该路径不获得项目域名和自动隔离能力；不要把多个项目指向同一活动目录，也不把此路径当作新项目默认流程。

`guide-init --scope project|global --source frps|baota` 是完整配置生成器，使用原 `FRPC_LAUNCH_INIT_TOKEN` 和 `--server-addr/--server-port/--proxy`。它会覆盖目标原生 TOML；仅在明确创建完整配置时使用。已有配置应定点修改并执行 `frpc verify`，保留其他代理和高级字段。继续此接口不授权迁移或复制任何实际凭证。
