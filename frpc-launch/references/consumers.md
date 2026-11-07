# 消费者独立连接接口 v1

`frpc-launch` 1.6.0 起，调用方可保留原 `--project` / `--home` 配置来源，另外提供 `--consumer` 和 `--instance` 管理独立进程。普通项目入口继续管理原项目的全部代理；消费者入口只管理本实例的一条路由。当前接口支持 official HTTP/HTTPS 代理、loopback 本地目标，TLS 终止方式由实际入口决定。

统一入口：`uv run --frozen --project <frpc-launch> <frpc-launch>/scripts/frpc_launch.py --project <原项目绝对路径> --home <原FRP个人目录> --json ...`。消费者标识与实例 ID 不是秘密；Token 留在 FRP 原来源，不能经业务配置或 argv 中转。

## 查询上下文

`context-info` 不启动进程、安装客户端或修改配置。返回：

- `interface_version: 1`、`project`、`home`。
- `capabilities` 声明可用的 `owner_watch` 和 `require_tls`。
- `domain_suffix` 的规范父域、来源与状态。已有 `FRPC_LAUNCH_DOMAIN_SUFFIX` 时沿用（`basis=domain_suffix_setting`）；未设置时使用同一生效完整连接组的域名主机（`basis=server_addr`）。IP 地址或 URL 不能用作父域；无效后缀、不可读来源或高层半组不能靠回退掩盖。独立的明确后缀即使连接组缺项也返回。
- `connection` 的完整性、字段是否存在及来源，缺项时返回结构化错误；不返回 Token。
- `saved_project_domain` 是原项目保存的完整域名，与父域不同。
- `revision` 是不含原值的上下文修订；`source_binding` 标记本机来源组合，不能代替真实远端账号身份验证。

环境、原项目 `.env.frpc-launch` / `.env.local` / `.env`、同一 home `.env` 的顺序沿用原加载器。完整连接组最后可从该 home `frpc.toml` 读取。半组不补齐；完整原生 `FRPC_LAUNCH_CONFIG` 仍须明确迁移。

父域是生成服务域名的配置依据，标记 `verification=configuration_only`。例如仅有 `serverAddr=devfrp.example.com` 的完整连接即可生成 `hermes-mobile.devfrp.example.com`，无需另填可选后缀。该命名约定不证明 DNS、TLS、远端域名占用或 FRPS `subDomainHost` 已配置；消费者路由使用完整 `customDomains`。`serverPort` 仅用于控制连接，不作为网站端口。此行为属于消费者接口，不改变普通项目入口的固定域名协议。

## 独立生命周期

在上述全局参数后，加 `--consumer hermes-mobile-access --instance <稳定连接ID>`，对所有实例命令保持一致：

1. 调用 `context-info`，取得修订及父域。
2. `instance-config --context-revision <修订> --domain-prefix hermes-mobile --local-port <实际端口> [--proxy-type http|https] [--local-ip 127.0.0.1|::1] [--location /path/]`，仅保存本实例路由。
3. `verify --mode official` 离线验证；`start --mode official --wait 15` 启动。
4. `status`、`logs -n 50`、`stop` 使用相同的原项目/home/consumer/instance。停止在凭证丢失、父域变化或原项目目录消失后仍可运行，但必须保留原路径参数以核对身份。

需要后台所有者退出保护时，启动传 `--owner-pid <当前后台PID>`。runner 固定该进程创建时间，每秒检查一次；失去所有者就停止自己的 frpc 子进程。此参数只用于消费者，不能传 Agent 会话或短期 CLI 的 PID。`instance-config --require-tls` 要求 FRPC—FRPS TLS，已有来源显式禁用时拒绝，而不是覆盖该选择。它们不修改 FRP 主配置。

所有者退出后再次 `stop`，`stale_pid_cleaned` 的 `owned_process_exited=true` 表示原归属进程已确认退出；身份不匹配的活动 PID 不带这一证明，调用方不能将其解释为已安全停止。

实例文件位于 `<home>/consumers/<consumer>/<instance>/`。代理名由完整本机所有权派生；配置、操作锁、PID、脱敏日志复用既有 runner 和进程身份验证。原项目代理、visitor 和固定域名登记不被导入或改写。受支持的全局客户端选项继承原来源，证书相对路径在组合前相对原文件/项目解析。

运行中的路由改变返回 `running`，不修改旧路由；来源变化返回 `context_changed`，不自动启动或重命名。调用方先做业务预检，确认可用后停止旧实例，保存新路由，再启动；失败恢复由拥有业务条件的调用方组织。纯名称修改不调用本接口。

`verified` 仅为启动日志证据，`public_endpoint=not_checked`。此接口没有跨用户远端域名空闲证明，也不提供远端预留。调用方不能把本机登记、DNS/HTTP、未看到条目或试注册代理当作空闲证据。认证、DNS/TLS、代理注册及手机业务分别验收。

## 当前验证边界

本批次已通过 Windows 合成客户端的同源、多实例、凭证缺失停止、PID 归属、所有者退出隔离及 TLS 要求回归。Hermes 消费者在 Windows 使用官方 frpc 0.72.0 已验证真实 FRPS 注册、HTTPS/REST/WS 与停止/重连；macOS 和手机业务另行验收。
