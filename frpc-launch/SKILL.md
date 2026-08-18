---
name: frpc-launch
version: 1.0.0
description: v1.0.0｜Use when the user wants to 本地启动 frpc / 做内网穿透 / 打通 frp 隧道 / 把本地端口暴露到公网 / 连接自部署 FRPS 或宝塔面板的 frps / 使用樱花FRP（natfrp）隧道 —— phrases like "启动 frpc"、"起个内网穿透"、"frp 隧道连一下"、"把本地 8080 暴露出去"、"frps 连不上帮我看看"、"用樱花FRP 启动隧道"。检测不到配置时先引导用户配置（作用域选择 + 三来源帮助），frpc 二进制自动下载，后台常驻并提供 status/stop/logs。Do NOT use for 部署/管理 frps 服务端、注册系统服务（launchd/systemd 开机自启）、Windows 平台（v1 未实现）。
---

# frpc-launch

## Overview

本地一键启动/管理 frpc，双模式覆盖三类接入来源：

| 模式 | 覆盖来源 | 客户端 | 配置载体 | 启动方式 |
|---|---|---|---|---|
| `official` | 自部署 FRPS、宝塔面板安装的 frps（同构） | 官方 fatedier/frp 的 frpc（自动下载 + SHA-256 校验） | 原生 `frpc.toml` | `frpc -c <toml>` |
| `sakura` | 樱花FRP（natfrp.com） | 樱花定制 frpc（官方免鉴权 API 自动下载 + size/hash 校验） | `.env` 三变量 | 环境变量 `NATFRP_TOKEN`/`NATFRP_TARGET` 注入 |

受管目录 `~/.config/frpc-launch/`：

```
frpc.toml            # official 全局配置（原生格式，可手改，frpc verify 可校验）
.env                 # sakura 全局配置（FRPC_LAUNCH_SAKURA_KEY / _TUNNELS / 可选 _FRPC）
bin/{macos,linux,windows}/   # 受管二进制按 OS 分层（windows 预留），旁存 *.meta.json 版本锁定
run/                 # {official,sakura}.pid / .log
```

**配置分层**（每变量独立取首个非空来源）：进程环境变量 → `$PWD/.env.local` → `$PWD/.env` → 全局受管目录。`FRPC_LAUNCH_CONFIG` 可指向项目级 frpc.toml；`FRPC_LAUNCH_MODE=official|sakura` 显式指定模式。**使用全局配置启动时必须向用户报告「使用了全局配置」及具体来源**（脚本输出已含 `config_source`，转述给用户）。

## 子命令速查

统一入口 `python3 <skill>/scripts/frpc_launch.py [--home DIR] [--json] <子命令>`；Agent 一律加 `--json` 解析结果。

| 子命令 | 说明 | 常用参数 |
|---|---|---|
| `start` | 后台启动 + 有界启动验证（默认 15s） | `--mode`、`--wait N` |
| `stop` | 优雅停止（SIGTERM→SIGKILL），stale pid 只清文件不误杀 | `--mode` |
| `status` | 分模式报告运行状态/配置来源/二进制版本/日志尾部 | `--mode` |
| `logs` | 输出受管日志尾部（已掩码） | `--mode`、`-n N` |
| `install` | 安装受管二进制（已装则不重复下载） | `--mode`、`--version X` |
| `update` | 显式升级二进制（唯一会替换二进制的入口） | `--mode`、`--version X` |
| `guide-init` | 引导流程落盘配置（见下节） | `--scope`、`--source`、`--proxy` 等 |

**退出码语义**：`0` 成功/已在运行；`1` 失败（含启动验证失败与 `running_unverified`——进程存活但从未见登录成功证据，log_tail 有真实原因）；`2` 用法错误；`3` 模式歧义（两模式配置同存，须询问用户）；`4` 未配置（进入引导流程，含显式指定模式但该模式无配置）；`5` 配置已变更（须用户确认 stop 后再 start）；`6` git 安全拒绝（Secret 目标未被忽略——引导用户先把该文件加入 `.gitignore` 再重试，或经用户明确同意后加 `--allow-tracked`）。

## 引导流程编排（start 退出码 4 时，Agent 按序执行）

1. **确定作用域（默认项目级，不询问）**：
   - **默认**：写当前项目 `$PWD/.env.local`（official 另生成项目级 frpc.toml 并以 `FRPC_LAUNCH_CONFIG` 指向），即 `guide-init --scope project`。用户没有明确要求全局时一律走这条，不要把作用域问题抛给用户
   - **仅当用户在本轮明确要求「全局」「长期保存」「多项目共用」时**，才用 `--scope global` 写 `~/.config/frpc-launch/`
   - 落盘后必须告知用户实际写入的作用域与文件路径
   - **注意**：项目在 git 工作树内时，须先把 `frpc.toml` 与 `.env.local` 加入 `.gitignore`，否则 guide-init 会以退出码 6 拒绝写入 Secret——这是防止凭证被提交的保护，引导用户补 `.gitignore` 后重试
2. **询问是否需要配置帮助**：
   - 不需要 → 告知所选作用域的文件位置与最小模板（official：`serverAddr`/`serverPort`/`auth.token` + `[[proxies]]`；sakura：`FRPC_LAUNCH_SAKURA_KEY` + `FRPC_LAUNCH_SAKURA_TUNNELS`），等用户自行配置后重试 start
3. **需要帮助 → 三来源分支**：
   - **自部署 FRPS**：逐项询问 serverAddr、serverPort、auth.token、代理需求（type；localPort；tcp/udp 要 remotePort，http/https 要 customDomains）
   - **宝塔 frps**：同上参数，但指引用户到「宝塔面板 → 软件商店 → FRP 服务端插件 → 配置（frps.toml）」查看：`bindPort` 即 serverPort、`auth.token` 即 token、http 代理需服务端 `vhostHTTPPort`（访问时用该端口）。只指位置，不猜具体值
   - **樱花FRP**：指引用户到樱花管理面板取访问密钥（用户中心）与隧道 ID（隧道列表，逗号分隔多条）。如实说明：macOS 官方更推荐其图形启动器，本 Skill 直接托管定制 frpc 属高级用法
4. **调用 guide-init 落盘**（token/key 一律经环境变量注入，不进命令行参数）：

   ```bash
   # official（frps/baota）：
   FRPC_LAUNCH_INIT_TOKEN=<token> python3 <skill>/scripts/frpc_launch.py --json \
     guide-init --scope project --source frps \
     --server-addr <host> --server-port <port> \
     --proxy "name=web;type=http;localPort=8080;customDomains=a.example.com"
   # sakura：
   FRPC_LAUNCH_SAKURA_KEY=<密钥> FRPC_LAUNCH_SAKURA_TUNNELS=<id,id> \
     python3 <skill>/scripts/frpc_launch.py --json guide-init --scope project --source sakura
   ```

   （只有用户明确要求全局时才把 `--scope project` 换成 `--scope global`）

5. 生成后询问用户是否立即 `start` 做真实连通验证（official 启动前会自动 `frpc verify` 预检）。

## 掩码规则

- token / 访问密钥回显一律 `head4****tail4`（如 `tok_****1234`）；**禁止在对话、命令行参数或日志中出现完整值**
- `guide-init` / `start` / `status` / `logs` 的输出已内置掩码；Agent 转述时不得从别处拼回完整值

## CRITICAL

- **start 永不自动升级二进制**；升级只能走显式 `update`
- **两模式配置同存且未指定时（退出码 3）必须询问用户**，不得默认选择
- **配置变更（退出码 5）必须向用户展示差异并确认 stop 后再 start**，不得静默替换运行中的进程
- 下载失败/校验不一致：如实报错，**不自动切换第三方镜像**；可提示用户自行配置代理
- 启动成功以 frpc 日志真实登录/代理证据为准（进程拉起 ≠ 成功）；失败时把 log_tail 的真实原因转述给用户
- 完整性校验（官方 SHA-256 / 樱花 size+hash）只保证下载未损坏，**不等于发布者签名验证**
