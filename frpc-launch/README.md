# frpc-launch

本地一键启动/管理 [frp](https://github.com/fatedier/frp) 客户端（frpc）的 Agent Skill：检测不到配置时引导你完成配置，frpc 二进制自动下载校验，后台常驻运行，并提供 status / stop / logs 管理。

## 功能

- **双模式**：
  - `official`——连接自部署 FRPS 或宝塔面板安装的 frps（标准 frps 同构），使用官方 frpc + 原生 `frpc.toml`
  - `sakura`——连接樱花FRP（natfrp.com），使用其官方定制 frpc，配置只需访问密钥 + 隧道 ID 两个变量
- **二进制自动获取**：official 走 GitHub release API 并比对官方 `frp_sha256_checksums.txt`；sakura 走其免鉴权客户端清单 API 并校验 size/hash。均原子安装、`*.meta.json` 锁定版本，`start` 永不自动升级（升级用 `update`）
- **后台常驻**：`start` 后进程脱离会话存活；启动成功以 frpc 日志中的真实登录/代理证据判定，失败时报告真实原因
- **进程管理**：`status`（运行状态/配置来源/版本/日志尾部）、`stop`（SIGTERM→SIGKILL，stale pid 只清文件不误杀无关进程）、`logs`
- **凭证安全**：token/密钥在一切输出中掩码为 `head4****tail4`；sakura 密钥经环境变量注入，不出现在进程参数里

## 环境要求

macOS（主线，已真实验证）或 Linux（同一 POSIX 路径，尽力支持）；Python ≥ 3.9，仅标准库，无第三方依赖。Windows 暂不支持（目录已预留）。

## 快速开始

```bash
# 首次（无配置）会返回退出码 4，按 SKILL.md 引导流程配置；配置好后：
python3 scripts/frpc_launch.py start          # 后台启动（自动下载 frpc）
python3 scripts/frpc_launch.py status         # 查看状态
python3 scripts/frpc_launch.py logs -n 50     # 查看日志（已掩码）
python3 scripts/frpc_launch.py stop           # 停止
```

## 配置分层

每个变量独立按以下顺序取首个非空来源：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 进程环境变量 | 本次运行显式注入 |
| 2 | `$PWD/.env.local` | 项目级，不向上递归 |
| 3 | `$PWD/.env` | 项目级，不向上递归 |
| 4 | `~/.config/frpc-launch/` | 全局（official 为 `frpc.toml`，sakura 为 `.env`）；使用时会在输出中标明 |

**首次引导默认写项目级 `$PWD/.env.local`**（official 另生成项目级 `frpc.toml`）；只有你明确要求「全局 / 长期保存 / 多项目共用」时，才写入 `~/.config/frpc-launch/`。

变量：`FRPC_LAUNCH_MODE`（official/sakura）、`FRPC_LAUNCH_CONFIG`（项目级 frpc.toml 路径）、`FRPC_LAUNCH_FRPC`（自备官方 frpc 路径）、`FRPC_LAUNCH_SAKURA_KEY` / `FRPC_LAUNCH_SAKURA_TUNNELS` / `FRPC_LAUNCH_SAKURA_FRPC`。

## 受管目录布局

```
~/.config/frpc-launch/
├── frpc.toml                # official 全局配置（0600）
├── .env                     # sakura 全局配置（0600）
├── bin/
│   ├── macos/  frpc / frpc.meta.json / frpc-sakura / frpc-sakura.meta.json
│   ├── linux/               # 同结构
│   └── windows/             # 预留
└── run/  {official,sakura}.pid / .log
```

## 安全说明

- 下载完整性校验：official 比对官方发布的 SHA-256，sakura 比对其 API 返回的 size/hash——这只能发现传输损坏，**不等于发布者签名验证**（两家上游当前均未提供独立签名）
- 校验不一致/下载失败：删除临时文件并如实报错，不自动切换第三方镜像
- Secret 文件（`frpc.toml` 含 token、`.env`）权限 0600；项目作用域写入前做 git 忽略检查，防止凭证被提交
