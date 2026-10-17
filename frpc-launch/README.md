# frpc-launch

让 Agent 使用已有的 [frp](https://github.com/fatedier/frp) 服务，把本地网站或端口通过指定公网入口访问，并检查、停止对应的本地 frpc 客户端进程。已有域名和配置时先核对，缺少配置时协助填写；不部署服务器，也不自动修改 Nginx、DNS、证书或开机自启设置。

## 功能

- **双模式**：
  - `official`——连接自部署 FRPS 或宝塔面板安装的 frps（标准 frps 同构），使用官方 frpc + 原生 `frpc.toml`
  - `sakura`——连接樱花FRP（natfrp.com），使用其官方定制 frpc，配置只需访问密钥 + 隧道 ID 两个变量
- **二进制自动获取**：official 走 GitHub release API 并比对官方 `frp_sha256_checksums.txt`；sakura 走其免鉴权客户端清单 API 并校验 size/hash。均原子安装、`*.meta.json` 锁定版本，`start` 永不自动升级（升级用 `update`）
- **后台常驻**：启动后进程脱离会话存活；脚本报告启动日志证据，Agent 再分别核对各条代理和完整公网 URL。进程启动不等于公网或业务验证通过
- **进程管理**：检查进程、配置来源、版本和日志，或停止已核对身份的受管进程。状态查询本身不请求公网；旧记录缺少验证结果时显示未知
- **域名与路径转发**：同一域名可按路径转到不同本地地址和端口。新增路由时保留已有代理；完整配置生成器会覆盖目标文件，不能当作追加接口
- **凭证安全**：token/密钥在一切输出中掩码为 `head4****tail4`；sakura 密钥经环境变量注入，不出现在进程参数里

## 环境要求

需要已有的 FRP 服务，以及要公开的本地服务地址、端口和公网入口。macOS 有现有运行实现，1.3.1 通过隔离脚本测试及真实 frpc 离线配置校验；Linux 使用同一 POSIX 实现，本版未做实机验证。Windows 原生暂不支持；WSL 使用其 Linux 用户目录和程序，本版未实测，不默认读取 Windows 配置。

运行需要 [`uv`](https://docs.astral.sh/uv/) ≥ 0.8，由 Agent 检查。Skill 自带 Python 版本与依赖锁文件，首次运行可能下载运行环境及 frpc；后续启动不会自动升级 frpc。

## 让 Agent 安装

在 Claude Code、Codex 或 WorkBuddy 对话中发送：

> 请从 https://github.com/cookaihq/cookai-skills 安装其中的 frpc-launch Skill，保留已有配置，并验证当前客户端能否发现和调用它。

| 宿主 | 本地入口与本版验证状态 |
| --- | --- |
| Claude Code | `~/.claude/skills/frpc-launch/`，可用 `/frpc-launch` 点名；本版客户端发现、触发和业务闭环未实测 |
| Codex | `~/.agents/skills/frpc-launch/`，可用 `$frpc-launch` 点名；本版重新加载、触发及默认沙箱闭环未实测 |
| WorkBuddy | `~/.workbuddy/skills/frpc-launch/`，通过自然语言点名并确认实际加载；本版客户端未实测，不假定支持 symlink 或其他宿主的命令语法 |

三宿主使用同一份 Skill 源文件；脚本测试不能代替客户端验收。

## 第一次使用

启动会把所选本地服务通过公网入口公开，并留下后台进程与日志。先启动本地应用，再将示例域名、端口换成实际目标发送给 Agent：

> 使用 frpc-launch，把本地 http://127.0.0.1:8080 通过我已有的 https://app.example.com/ 访问。先核对已有配置和进程，补齐缺失的本地 FRP，验证这个完整公网 URL，并给我可以打开的地址和各层结果。

预期得到完整 URL，以及本地服务、FRP 登录、每条代理、公网响应和业务状态的分别说明。公网返回 200 可能只是应用准备页；预期应用错误可以说明请求到达应用，但不能证明业务成功。HTTPS 在 Nginx 处理后转给 frps HTTP 入口时，本地代理仍可使用 HTTP；不因公网 HTTPS 就更换代理类型。

## 常用请求

- “把同一域名的 `/api/` 转到本地 8081，保留其他路径。”——先检查已有配置，定点修改并逐条验证。
- “检查这个项目使用的 FRP 进程和完整公网 URL。”——进程状态与公网结果分别报告，给出实际配置和运行目录。
- “公网显示准备页，检查应用是否加载了配置。”——对照原入口与本地同一路径的响应，说明具体未就绪项。
- “停止刚才启动的 FRP。”——核对对应的受管目录与进程身份后停止，不停止本地应用。

## 配置分层

每个变量独立按以下顺序取首个非空来源：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 进程环境变量 | 本次运行显式注入 |
| 2 | `$PWD/.env.frpc-launch` | 项目级，不向上递归 |
| 3 | `$PWD/.env.local` | 项目级，不向上递归 |
| 4 | `$PWD/.env` | 项目级，不向上递归 |
| 5 | `~/.config/frpc-launch/` | 全局（official 为 `frpc.toml`，sakura 为 `.env`）；使用时会在输出中标明 |

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

同一受管目录内，每种模式共用一份 PID 与日志；项目配置分开并不自动隔离进程。多项目同时运行时，让 Agent 明确各自的受管目录，并在启动、检查和停止时使用同一个目录。配置变化需要先说明影响，再按授权停止并重启。

## 安全说明

- 下载完整性校验：official 比对官方发布的 SHA-256，sakura 比对其 API 返回的 size/hash——这只能发现传输损坏，**不等于发布者签名验证**（两家上游当前均未提供独立签名）
- 校验不一致/下载失败：删除临时文件并如实报错，不自动切换第三方镜像
- Secret 文件（`frpc.toml` 含 token、`.env`）权限 0600；项目作用域写入前做 git 忽略检查，防止凭证被提交

## 排查与更新

> 请检查 frpc-launch 实际加载的版本、工作文件夹和配置来源，隐藏密钥；沿我的完整公网 URL 核对代理和本地响应，分别报告已验证结果与辅助诊断。

Skill 默认自动检查更新，只提醒，实际更新需要确认。Git 检出使用随包适配器；复制安装按 `update.json` 查询本 Skill 的稳定 tag 和精确提交后再确认替换。宿主缺少可核实的更新能力时报告未能执行更新，继续当前任务；检查失败不等于已是最新。

可以说“关闭 frpc-launch 的自动检查更新”。开关 `AUTO_UPDATE_CHECK=0` 只保存在当前用户的 `~/.config/frpc-launch/.env`，不读取工作文件夹配置。更新 Skill 与升级 frpc 二进制是两项操作，启动不会顺带升级 frpc。
