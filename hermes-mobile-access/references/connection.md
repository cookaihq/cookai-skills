# 实际接入前的证据

受查 Mobile 源码是 v2.9.6，commit `4d7f680c18eb7989ac97616841f6376547104127`；设备若不是该版本，应先核对实际实现。首版目标为 Windows 与 macOS，先验证当前 Windows；当前尚无 Android 完整接入或 macOS 实测通过记录。

1. 在目标 Windows 或 macOS 电脑发现 Desktop 当前使用的进程、启动时间、监听和健康协议，并核验实际 Hermes home/profile。分别使用实际平台的进程、权限和生命周期机制；Windows 结果不能代替 macOS。发现指纹是运行代际，不是永久逻辑身份；不要另起后端。
2. 用 FRP Launch `context-info` 接口 v1 获取父域与原来源。手机 Server URL 是 HTTPS 完整基础 URL，末尾 `/`；只新增一个服务子域名。源站随机端口、`/api/ws` 与完整 Server URL 分开。
3. 按用户选择的访问规则检查占用：无凭证 GET 返回 2xx/3xx 判为占用，4xx/5xx、DNS/连接/TLS 失败或超时判为未占用。记录响应码、失败类别和时间；不要求 FRPS 管理 API、宝塔或 SSH。预检不预留域名，实际 FRP 注册仍可能冲突，失败按既有流程恢复旧连接。
4. 先用隔离探针验证 HTTPS、入站认证、FRPC–FRPS 传输和源站防绕过，再绑定真实后端。覆盖首页、token 引导、API、WS 和媒体；匿名、错误凭证及旁路须拒绝。Desktop 的 loopback 豁免可能被 frpc 触发。
5. 沿用已确认的独立接入密钥：Mobile Token 模式，在 Custom Headers 中填写 `X-Hermes-Mobile-Key`。先配置此头，再探测服务器，以取得适配器首页的会话标记。标记填入 Mobile Token 字段只用于协议衔接，密钥不填入 URL 或 Token 字段。实际手机需核验其自动探测/刷新行为；不能把源码和本机探针当作 Android 通过。外部浏览器不继承此头，匿名打开会被拒绝，不改用带密钥链接绕过。
6. 在手机选中并核对首选 profile。REST query 和 WS params 的作用域由手机表达，之后用户可以切换；不是服务端单 profile 沙箱。聊天 WS 可能携带管理能力，保留原有工具审批。
7. 真实测试使用明确的独立会话、profile、消息和次数；核验请求作用域、后端归属、真实 WS 回复及 Android 结果，不信模型自述。

当前原生 profile 适配器要求 Gateway 控制协议 v1：Windows 使用同用户命名管道，macOS 使用私有 Unix socket，并核对内核提供的服务端 PID、进程创建时间和 identify 回执。每个 REST 查询单独申请 30 秒一次性 `native-http` 票据，不保存或返回票据。只核验 `/api/profiles` 和 `/api/profiles/active`；旧式无此协议的后端明确返回不可用，不自动启用旧网页 token 方案。当前 Windows Gateway 已通过，macOS 分支尚未实机验收。

profile HTTP 探针和正式接入层共用已建立 TCP 连接的归属检查；确认对端属于目标进程后才发送一次性票据，监听端口存在不能代替此检查。`backend_user_mismatch` 与进程变化分别报告：本机 Codex CLI 0.166.0 的默认 Windows 沙箱以另一系统账号运行，合成回归通过但真实 profile 查询被拒绝。不得通过移除同用户检查、修改已有 ACL 或更换更宽松沙箱来把原验收记为通过。

本机原生票据不能交给远端 Mobile。当前 Gateway 首页不提供旧式 session token，正式 `access_bridge.py` 用 aiohttp 3.14.5 做 loopback 转换：每次请求验证独立密钥，将 REST 转为原生头票据、WS 转为原生票据子协议。建立上游 TCP 后、发送任何凭证或数据前，通过 OS 四元组归属检查确认对端仍为已识别进程。禁用环境代理、Cookie 继承和重定向，精确核对 Host，拒绝 Origin，不转发客户端的认证或 forwarding 头。

`probe-access` 只建立临时 loopback 接入：匿名/错误密钥/Host/Origin、真实 profile、WS ready/ping、撤销后流关闭和新请求拒绝，结束即停止。已在 Windows Hermes 0.22.5 验证。正式适配器另以合成文件通过下载、媒体 Range 和显式 profile 读取；2026-11-07 用户确认 Windows 后端与 Android 基础文字消息往返通过。实际上传、语音、外部浏览器和重连历史同步尚待验；先于会话恢复的 `session.events.since` 仍可能返回 `not_found`。探针不自动保存生产密钥、不启动 FRP、不发模型请求。

HTTP 仅发布代码 allowlist 内的基本状态、profile、模型信息、会话读取、文件和语音请求；会话 ID 可包含斜线。文件下载/播放允许且移除本适配器的 query 标记，仍须独立密钥；保留 Range/Content-Range。密钥持有人具有 Hermes 主机所有者的 WS 能力，首选 profile 与 HTTP 路径裁剪不是权限沙箱；保留原审批，不让手机声明 `desktop_local=true`。配置或身份失效撤销两侧活动流，每 2 秒启动检查，检查自身有预算，不能称为 2 秒内必定撤销。

换域名前先重新核对来源、身份、占用、TLS 与认证。可用才停止本连接，核对停止后更新路由并启动；竞态失败先清理本次资源，条件仍有效且配置未被外部修改时尝试恢复旧连接，恢复失败如实报告断开。

`probe-domain [--domain-prefix <单个子域名>]` 从 FRP 上下文派生完整 HTTPS 地址，不需要先保存 HMA 连接。仅访问一次，不跟随重定向、不读取正文、不携带密钥或 Cookie、不使用环境代理；请求总预算 8 秒、连接/读取各最多 3 秒，外层子进程 12 秒。结果 `occupied/unoccupied` 仅表达本规则，`registration=not_checked`。程序故障或非法配置单独报错。`plan/doctor/connect` 复用同一预检，不再列出缺少服务端查询接口；实际接入仍须满足剩余条件。

`connect` 启动独立控制器，固定连接 ID、来源、绑定摘要和 FRP 上下文修订。先仅发布带认证的随机引导标记；公网 HTTPS、匿名/错误密钥拒绝和实例对应通过后，才开放 Gateway REST/WS，验证真实 profile、`gateway.ping` 和聊天读取方法后报告 `awaiting_mobile`。聊天方法预检使用随机不存在的会话 ID，要求用量与上下文查询准确返回 `not_found`，不读取已有聊天、不创建会话。FRP 要求 `owner_watch` 与 `require_tls` 能力；只创建当前连接路由，显式禁用 FRP TLS 的来源会被拒绝。

Mobile v2.9.6 打开聊天会调用 `session.usage`，当前原生 Hermes 0.22.5 的等价请求为 `session.stats`。手机序列化会省略默认 `jsonrpc` 字段；接入层和方法预检均覆盖此实际格式。用量请求及对应成功响应按真实统计转换，保持 ID 和后端错误，缺失的费用/用量不补零；`session.usage` 推送与通知不改写。每条 WS 最多跟踪 256 个未回复请求，重复 ID 或超限关闭该连接。目标 Gateway 须提供 `session.stats` 与 `session.context_breakdown`；预检失败返回 `chat_usage_method_unavailable` 或 `chat_context_method_unavailable`，不自动升级 Hermes 或重放请求。`probe-access` 同样执行此预检。

手机创建/恢复会话会显式选择 profile，部分随后发出的会话请求却省略它。接入层根据同一 WS 上成功的 `session.create/resume` 回复记录会话所属 profile，为省略选择器的会话请求补齐；显式选择器始终保留，不使用“最近一次选择”覆盖其他会话。绑定最多 512 个会话标识，超限关闭该 WS；同一标识成功归属多个 profile 时要求显式选择，返回 `ambiguous_profile`。失败回复不建立绑定，重连后重新由成功回复确认。诊断日志只含固定方法/原因及每条 WS 随机加盐的关联标记，不记录请求/会话原始 ID、profile 名、参数、聊天或后端错误正文。Windows 已用两个 profile 的隐藏空会话验证省略 `jsonrpc/profile` 的用量、上下文和会话状态读取及来回切换，随后删除测试会话；Android 界面和消息往返仍须手机确认。

Mobile v2.9.6 的 `prompt.submit` 没有原生 Gateway 必需的 `submission_id/input_id`。接入层仅在两字段均省略且请求 ID 非空值时补 `submission_id`，保留显式字段（包括无效值，由后端拒绝）、正文、排队和审批参数。同一 WS 的相同类型/值 RPC ID 保持同一提交标识；数值 1 与 1.0 视为同一 ID，字符串 "1" 则不同。每次 WS 使用独立随机命名空间，不根据聊天正文生成标识，不保存正文，不承诺跨重连去重。受查手机只重排未被发送队列接受的请求；已发送但回执丢失时，接入层不重放，先确认实际结果。成功提交日志仅记录白名单回执状态和脱敏关联标记，不代表模型已回复。

适配器每 2 秒检查配置及原生身份；控制器每 2 秒检查失效状态、每 10 秒核对 FRP 来源/进程、每 60 秒复查公网准入，各检查另有墙钟预算。身份失败先关闭活动流，再停专属 FRP。开启恢复时按 2、4、8、16、30 秒上限退避重新发现同一逻辑 home/profile并验证新实例，绝不重放聊天。FRP 来源或保存身份变化须重新核对；关闭恢复仍执行停转发。Windows 已验证接入进程退出后专属 FRP 在约 1 秒停止；此为本次观察，不是跨平台 SLA。Desktop 重启、端口变化、睡眠及网络切换仍需实机验收。

状态与日志在 `<home>/runtime/connections/<稳定ID>/`，日志 `service.log` 为 1 MiB × 3，仅记录阶段与脱敏原因。`status` 保留证据时间，失去进程或超过 30 秒未更新时不提供当前生效 URL。连接锁和同一 OS 用户的 Desktop 协调锁防止重复绑定。`disconnect --connection` 使用启动记录的原 FRP 来源，即使配置后来损坏也可停止；不按模糊进程名操作。

运行时在配置页更换域名：查占用、HTTPS 前提和 Desktop 身份后才停旧连接，保存新配置并启动验证。失败先清理新资源；来源及保存修订仍匹配时恢复旧配置和连接，冲突或恢复失败明确返回断开原因。清除密钥则断开并保存撤销，不自动恢复旧密钥。初次未连接时保存草稿仍不启动公网。
