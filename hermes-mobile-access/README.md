# Hermes 手机接入

目标是让 Android Hermes Mobile 连接当前 Windows 或 macOS 电脑上的 Hermes Desktop，通过已有 FRP 使用完整 HTTPS 地址。

**0.1.0 已通过 Windows 后端与 Android 的基础文字消息收发。** 已实现发现、命名连接/UI、独立密钥认证、域名预检、后台连接/恢复及独立 FRP。Windows 另已验证公网 HTTPS、认证拒绝、真实 profile REST、WS 探活、进程退出后 FRP 停止及重连。macOS、重连历史同步、Desktop 重启/睡眠恢复及其余手机场景仍待实测；首版不表示这些场景已全部验收。

## 安装

将下面的请求交给你正在使用的 Codex、Claude Code 或 WorkBuddy：

> 请从 https://github.com/cookaihq/cookai-skills 安装 hermes-mobile-access v0.1.0，并准备同仓的 frpc-launch v1.6.0，保留我的已有配置，验证当前 Agent 能发现和调用。

固定版本入口为 [hermes-mobile-access/v0.1.0](https://github.com/cookaihq/cookai-skills/releases/tag/hermes-mobile-access/v0.1.0)，安装其中的 `hermes-mobile-access/`；FRP Launch 需要 1.6.0 起提供的消费者接口 v1。宿主使用同一份 Skill；Codex 通常使用 `~/.agents/skills/`，Claude Code 使用 `~/.claude/skills/`，WorkBuddy 使用 `~/.workbuddy/skills/`。首版已完成 Codex 安装与合成回归，其他两者保留兼容入口但未实测。

## 配置与使用

> 使用 hermes-mobile-access 检查当前 Desktop 后端，打开配置页面。新连接保存个人配置，已有连接按实际来源修改；保留 FRP Launch 的原配置，只让我修改新增的服务子域名。

页面最前面依次显示 Server URL 和手机接入密钥。Server URL 整体显示完整地址，默认服务子域名为 `hermes-mobile`。协议、FRP 父域和 `/` 固定，复制得到完整地址。首选 profile 使用平铺选项，来自当前后端的真实列表；保存前重新核验，仅允许选择该后端正在提供的 profile。保存不会切换电脑当前 profile，接入时仍须在手机核对。读取失败时显示具体原因，不列合成 profile 供真实连接选择。

入口采用独立接入密钥。在配置页生成、保存和复制；平时脱敏，可用眼睛按钮查看、替换或清除。手机选择 Token 模式，在 Custom Headers 中添加 `X-Hermes-Mobile-Key`，值为配置页的密钥；再探测服务器，让手机获取会话标记。密钥与手机 Token 字段中的会话标记不同，密钥不放进 URL。外部浏览器不继承自定义头，相关跳转功能尚未验证。

此密钥允许使用 Hermes 主机所有者的聊天能力；首选 profile 不限制其他 profile 的权限。替换或清除密钥会在接入层检查到变更时关闭旧连接；不会关闭 Desktop。可以先让 Agent 只验证本机认证：

> 使用已保存的连接运行本机认证探针，检查密钥准入、profile、WebSocket、聊天用量和上下文接口，以及撤销后的连接关闭。结束后停止测试监听，不启动 FRP，不发送聊天，也不把密钥贴到回复中。

父域从 FRP Launch 当前生效配置读取：有明确命名后缀时沿用，没有时使用有效连接中的域名主机。例如 FRP 连接主机为 `devfrp.example.com`，页面显示 `https://hermes-mobile.devfrp.example.com/`，只让你编辑 `hermes-mobile`；FRP 控制端口不进入这个地址。完整连接已经存在时，无需为缺少可选后缀重新配置。地址生成与公网连通、认证通过分别显示。

域名占用按访问结果判断：正常访问（HTTP 2xx/3xx）为占用，其他状态或访问失败为未占用。不需要提供 FRPS 管理入口。切换时若 FRP 注册仍然失败，尝试恢复原连接。

> 使用 hermes-mobile-access 访问准备使用的域名，告诉我是否占用及具体响应。只检查，不启动 FRP，不修改配置。

配置就绪后，连接会启动本机接入进程及专属 FRP，并在经过认证的 HTTPS 地址提供手机入口：

> 连接这条已配置的 Hermes 手机接入，核对 HTTPS、认证、REST 和 WebSocket。把手机填写的信息给我，密钥只在本机配置页复制；等待我反馈手机聊天结果。

新连接默认保存到个人配置，由 Agent 显式启用个人来源；已有配置修复实际来源。读取顺序为：运行程序时的环境变量 → 当前工作文件夹的 `.env.hermes-mobile-access` → `.env.local` → `.env` → 已启用的个人配置。不向父目录查找；工作文件夹由调用项目决定，与 Skill 安装目录分开。高层连接集合整套覆盖低层，不拼接连接或凭证；空字符串继续回退，合法空集合遮蔽低层，来源不可读或格式损坏时提示修复。

| 环境 | 个人配置位置与验证范围 |
| --- | --- |
| Windows 原生 | 当前 Windows 用户文件夹下 `.config\hermes-mobile-access\.env`，即 `%USERPROFILE%\.config\hermes-mobile-access\.env`；同用户环境已验证配置、发现、认证、真实 FRP/HTTPS/REST/WS、媒体读取及连接停止/重连 |
| macOS | 当前用户主目录下 `~/.config/hermes-mobile-access/.env`；属于首版目标，尚待实机验收 |
| WSL | 若在 WSL 调用，使用 Linux 用户主目录的 `~/.config/hermes-mobile-access/.env`，不默认共用 Windows 配置；不属于首版已验证环境 |

配置缺项也可打开页面、保存草稿。配置页仅本机可访问，60 分钟无操作到期；**📱 跨设备访问需要单独配置 FRP / Tailscale**。

> 检查这条 Hermes 手机连接的缺项、配置来源、FRP 进程和实际端口绑定，区分本机发现、入口认证、WebSocket 与 Android 是否真的验证通过。

> 把这条连接改名，保留连接 ID、默认项和运行身份。

> 断开所选 Hermes 手机连接，只停止它的受管 FRP；保留 Desktop、其他代理、聊天和凭证，并核对入口是否关闭。

连接成功后默认持续检测并自动恢复，Agent 退出后仍运行；断开时停止，不开启登录自启。关闭自动恢复仍会停止身份失效的转发。正在连接时更换域名会先检查占用，再停旧启新；失败尝试恢复旧连接，配置冲突或恢复失败会明确报告。运行状态显示最近检查时间，不能把历史验证当作当前结果。

## 更新与排障

> 检查 hermes-mobile-access 是否有更新，告诉我固定版本与来源；我同意后再更新，保留本机配置。

> 关闭 hermes-mobile-access 的自动检查更新。

更新检查只管理本 Skill，不升级 Hermes、FRP 或手机 App。配置冲突时重新载入核对草稿；环境来源无法直接从页面写回，须修复实际注入位置。秘密留在本地配置入口，不贴到聊天。

Windows Codex 已实际加载安装副本并执行合成回归、配置及缺项诊断。当前电脑的默认 Codex 沙箱使用独立系统账号，真实 profile 查询返回 `backend_user_mismatch`；接入组件需要由 Hermes 所属账号运行，不能仅靠修改用户目录或放宽文件权限解决。普通同用户环境的真实 FRP、HTTPS、原生 profile 与 WebSocket 探活已通过，但不能代替该沙箱中的真实业务验收。

手机由你自行填写即可，Agent 在接通后提供完整地址、认证方式、自定义头、首选 profile 和本地密钥复制入口。Windows 与 Android 基础文字收发已由用户实测确认；手机重连时的历史同步仍待验，当前后端在先于恢复会话的历史事件查询中可能返回 `not_found`。上传、语音、外部浏览器跳转、Windows Desktop 重启/睡眠恢复、macOS 与默认 Codex 沙箱的真实接入仍需分别验收。
