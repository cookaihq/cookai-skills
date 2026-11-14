---
name: hermes-mobile-access
description: v0.3.0｜将 Android Hermes Mobile 接入当前 Windows 或 macOS 上的 Hermes Desktop 后端，复用已有 FRP，检查认证、恢复端口变化并管理连接。适用于手机接入、连接排障和停用。
metadata:
  version: "0.3.0"
compatibility: Claude Code, Codex or WorkBuddy with a local shell and uv >= 0.8. Requires frpc-launch >= 1.6.0. Windows with Android basic text chat is tested; macOS, reconnect history and Desktop restart/sleep recovery remain unverified. The access process must run as the Hermes owner.
---

# Hermes 手机接入

## 配置页面快捷入口

先分流：显式调用本 Skill 后，内容去掉首尾空白后恰为 `配置`、`配置页面`、`打开配置`、`打开配置页面`、`设置`、`设置页面`、`打开设置`、`打开设置页面` 中任一项时，直接按[配置协议的快捷分支](references/configuration.md#配置页面快捷入口)打开现有配置页，返回有效链接与实际期限后结束。本分支先于更新检查和后端发现；保留真实连接选择及同用户校验，不探测域名、不启动 FRP 或发送聊天。

仅这些完整短语进入快捷分支；带业务正文、预设、参数或字面 `--` 的请求继续原流程。

**0.1.0 已验证 Windows 后端与 Android 的基础文字消息往返。** Windows 已通过公网 HTTPS、独立密钥准入、真实 profile REST、WS 探活、停止及进程退出后 FRP 清理/重连；Codex 安装及合成回归通过。macOS、重连历史同步、Desktop 重启/睡眠恢复及其余手机场景仍待验，不能报告全部场景通过。Windows 默认 Codex 沙箱使用另一系统账号时，真实接入仍返回 `backend_user_mismatch`，不绕过同用户校验。

用于把用户当前 Desktop 后端接到手机，或管理这条连接。单纯解释 FRP、打开 Desktop 或泛化网络咨询不启动连接流程。复用当前后端，不另起、更新或重启 Hermes；首选 profile 不代表服务端权限隔离，也不立即改变手机当前会话。

0.2.0 在配置页提供每条连接的 12 组 HTTP / WebSocket 独立勾选及 WebSocket 总开关，默认全部开启，未设置项也开启。用户可关闭不需要的手机直接接口；WS 同时限制 RPC、实时事件和重连回放。开关不替代 Hermes 的 Agent 工具审批或操作系统隔离，通用命令归入“其他接口与通用命令”。范围和错误含义见[接入与验收](references/connection.md)。

## 调用入口

每次调用先执行 `scripts/check_update.ps1`（Windows PowerShell）或 `scripts/check_update.sh`（macOS/Bash）。检查失败继续当前业务；返回 10 时展示实际更新信息，用户同意才用同一脚本 `-Pull` / `--pull`。复制安装按 [更新协议](references/update.md) 查询固定 tag/commit。

解析本 Skill 实体目录为 `<skill>`，全部业务调用使用：

```text
uv run --frozen --project <skill> python -B <skill>/scripts/hermes_mobile_access.py --project <调用项目绝对路径> --use-local-key <命令>
```

新建默认个人保存已确认，后续入口显式传 `--use-local-key`，不重复询问。用户选择项目保存时加 `--scope project`；已有配置写回实际来源。`--home`、`--frp-home` 仅在明确的另一个上下文或隔离验收中覆盖。FRP 安装不在相邻目录时显式传 `--frp-skill`；该参数指向安装，不改变 FRP 配置来源。

## 按请求工作

- 发现/检查：运行 `discover`，必要时用用户明确的 ledger 或候选 ID。多候选不选第一个。发现只证明进程、监听和健康协议；不读取 `/api/status` 冒充无写入探针。
- 核验 profile：运行 `profiles`，已选候选可传 `--selected <候选ID>`。使用同用户原生控制通道及一次性票据，只读取列表与当前项。`backend_user_mismatch` 表示执行账号与 Hermes 不同，须使用 Hermes 所属账号运行接入组件；不要把 `USERPROFILE` 路径当作执行身份或绕过检查。协议不支持、身份不符或无法读取时明确阻断，不从目录名合成可选列表。
- 配置：先 `connections list`。有多个连接或默认失效时让用户明确目标 ID。`configure start` 打开页面；未配置也能打开。使用已确认的完整 Server URL 控件，只编辑新增的单个子域名，父域取 FRP Launch；profile 平铺。接入时在手机核对首选 profile。
- 访问范围：配置页在连接信息下提供 HTTP / WebSocket 分组勾选，总开关关闭时保留 WS 分组选择；保存才生效。运行中变更先核验，再关闭本连接旧流并重新连接，失败按修订条件恢复。HTTP 返回 `403/access_policy_denied`、WS RPC 返回 `4001/access_policy_denied` 表示当前连接的主动限制；探针按保存策略核对拒绝，不误报为后端损坏。用户允许手机接口不等于授权 Agent 执行真实任务、管理写入或聊天。
- 本机认证验证：独立接入密钥方案已确认，配置页管理密钥，手机使用 Token 模式和 `X-Hermes-Mobile-Key`。已有保存授权和完整绑定时运行 `probe-access`；它临时启动 loopback 适配器，验证匿名拒绝、profile REST、WS 探活、用量/上下文方法及撤销后停止，不启动 FRP 或发送聊天。方法探针只查询随机不存在的会话。失败按结构化原因处理，不能为验证把密钥传入 argv 或回复。
- 创建/改名/删除/默认：使用 `connections new --name <显示名称>` 或对应 `rename/delete/default`；已有连接通过全局 `--connection <稳定ID>` 选定。默认失效不自动取第一条；删除活动连接先断开并核验。名称支持中文和空格，改名不重启。
- 域名检查：`probe-domain` 检查已保存子域名，无连接时默认 `hermes-mobile`；`--domain-prefix` 只覆盖本次检查。沿用用户规则：HTTP 2xx/3xx 为占用，其他 HTTP 状态或网络失败为未占用。保留具体结果，不再索取 FRPS 管理查询入口。实际注册冲突仍按切换失败处理。
- 连接/排障：`plan` 报告缺项和域名访问结果，`doctor` 核对运行状态与 FRP 来源。目标、密钥保存及接入范围已获授权后执行 `connect`；先验证隔离的公网准入，再开放后端并核验 REST/WS。`awaiting_mobile` 表示等待手机验收，失败会清理本实例。所需证据与恢复边界见 [接入与验收](references/connection.md)。
- 状态/断开：`status` / `disconnect` 仅针对所选稳定 ID。停止成功只表明本实例进程停止，仍需核对公网入口及已有流关闭。保持 Desktop 和其他 FRP 项目运行。

配置协议、来源错误、保存冲突和运行绑定见 [配置协议](references/configuration.md)。配置页面只监听 loopback；交付采用本次有效链接和实际剩余空闲期限。首次附一条填好实际项目路径的重开请求：“请打开项目【实际路径】中 hermes-mobile-access 的配置页，给我新的访问链接。”以及“手机访问：告诉 Agent‘我要用手机打开这个页面。’”同会话重发同页面只给链接与期限。页面使用后可 `configure stop`；默认 60 分钟无操作到期，不随业务连接公开页面。

## 结果与接续

分别报告配置保存、本机后端、FRP 进程、注册、TLS、入站认证、REST、WS 和 Android 实测，不把 HTTP 200 或启动日志合成“手机已可用”。缺凭证时在本地页面补齐，不索要或展示秘密。普通模型调用无新增逐次付费门；真实聊天验收仍明确目标会话、消息内容和次数，结果不明不盲重放。

手机由用户自行填写和验收时，交付实际可用的完整 Server URL、Token 模式、自定义头名称、首选 profile 和本地密钥复制入口。尚未接通的地址须标明未启用；不要求提供可控手机，不把已交付填写信息记为 Android 通过。

连接期间控制器独立于 Agent 运行，断开时停止，不注册登录自启。关闭自动恢复仍会关闭失效路由；修改身份、密钥或 FRP 来源后旧证据失效。保持已有授权范围、认证、Host/Origin 校验和工具审批。
