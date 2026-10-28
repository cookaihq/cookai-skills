---
name: frpc-launch
metadata:
  version: "1.4.0"
description: v1.4.0｜通过已有 FRPS 入口启动、检查或停止本地 frpc。支持自部署/宝塔和樱花 FRP；按项目复用连接、固定域名并隔离代理与进程。适用于“把这个项目的 3000 端口透传出去”“启动 frpc”“FRP 连不上”。先核对已有域名、路由与本地服务。不修改远端服务器、DNS、证书或开机自启；Windows 原生仅支持本地配置检查与准备。
compatibility: Claude Code, Codex or WorkBuddy with a local shell and uv >= 0.8. Process management requires macOS/Linux; native Windows supports project configuration only. Host acceptance status is in README.md.
---

# frpc-launch

使用已有 FRP 服务，管理当前项目的本地 frpc。`official` 用于自部署或宝塔安装的标准 frps；`sakura` 使用樱花定制客户端及面板隧道。三宿主共用此流程，保留自然语言隐式触发。

## 0. 检查更新

每次使用先按安装形态检查，失败不阻断当前任务：

- Git 检出：运行本 Skill 的 `scripts/check_update.sh`，PowerShell 用 `scripts/check_update.ps1`。退出 `0` 直接继续，不复述输出；退出 `10` 原样转述报告并询问是否更新。用户同意后 Bash 用 `--pull`，PowerShell 用 `-Pull`；拒绝、不答复或更新失败均继续当前版本，同一任务不再追问。仅在 main、工作树干净且可 fast-forward 时应用更新。
- 复制安装：读取 [更新协议](references/update.md) 和 `update.json`，确定稳定 tag 对应的精确 commit SHA。用户确认后才下载、校验、原子替换并回读。WorkBuddy 没有已核实的替换入口时报告 `not-applicable`，继续业务，不臆测更新 API。
- 关闭自动检查更新只修改 `~/.config/frpc-launch/.env` 的 `AUTO_UPDATE_CHECK=0`，保留其他设置。该开关不读取项目文件。

## 1. 确定项目和连接

先确认调用工作文件夹，后续始终显式使用同一个 `--project <绝对路径>`。它不是 Skill 安装目录，也不向父目录查配置。统一入口，Agent 一律加 `--json`：

```text
uv run --frozen --project <skill> <skill>/scripts/frpc_launch.py --project <项目> --json <子命令>
```

先运行 `project-info` 和 `status`。前者只读报告实际来源、字段是否配置、固定域名、代理和运行目录，不回显连接值、不联网。Windows 原生可做配置准备；进程管理须使用已支持的 macOS/Linux 环境，不自动跨到 WSL 或读取另一环境的配置。

official 连接整组读取：**本次完整输入 → 进程环境 → 项目 `.env.frpc-launch` → `.env.local` → `.env` → 全局 `.env` → 全局 `frpc.toml`**。地址、端口、Token 必须同源完整；高层半组配置返回 `incomplete_connection`，不得从另一层补 Token。使用全局只读回退时明确告诉用户具体来源。

用户提供新连接并要求保存时，用 `project-config` 保存到当前项目 `.env.local`；地址、端口通过参数，Token 只经进程环境 `FRPC_LAUNCH_INIT_TOKEN` 注入。只给部分新连接时补齐确认，不默认沿用另一套 Token。执行前读取 [项目配置协议](references/projects.md)，核对字段、保存和原生接口边界。

- 首次新增沿用已确认的项目保存选择，实际写入仍须有保存授权。Git 内的 `.env.local` 必须未跟踪且被忽略；不满足时先修正安全条件，不绕过、不改索引，也不同时改写全局默认。
- 首次缺项或有证据的错误，沿用已有管理方式；否则提供“修改本机配置 / 从 secret-book 选择并保存”。真实字段与关联组见项目配置协议。修复实际来源，高层遮蔽时不能另写低层文件假装修复。
- **仅保存连接不需要本地服务端口，也不启动隧道。** 安装和首次配置时不强求代理。
- 现有 `FRPC_LAUNCH_CONFIG` 整份原生配置不会自动合入项目。先核对现有调用方式；继续使用原生接口时按项目配置协议操作，迁移不自动复制或删除凭证。

## 2. 固定域名，核对路由和服务

HTTP/HTTPS、域名或路径转发，先读 [入口与路由核对](references/http-routing.md)。保留用户指定的协议、域名、端口和路径，逐条对应代理名、`type`、`customDomains/subdomain`、`locations` 和本地目标。

首次使用本机 `FRPC_LAUNCH_DOMAIN_SUFFIX` 与规范化项目目录名生成域名；明确的 `--domain` 优先。确定后保存到 `.frpc-launch/project.toml`，以后复用，不因全局后缀变化重命名。本机已知重名会分配稳定后缀；固定域名冲突则报告具体项目，不静默换名。本地登记不证明远端未占用或 DNS/TLS/Nginx 已就绪。

本地地址、端口取用户本次输入或项目实际运行信息。已核实 customDomains 路由时，例如：

```text
uv run --frozen --project <skill> <skill>/scripts/frpc_launch.py --project <项目> --json project-config --local-port 3000 --route-mode customDomains
```

使用 `subdomain` 必须同时提供从实际 FRPS 核实的 `--subdomain-host`，不能从域名猜测 `subDomainHost` 已配置。同名代理只改本次提供的字段，保留其他代理、路径和高级字段；之后只更新本地端口不必重填域名。

HTTPS 经 Nginx 解密后转 frps HTTP 入口时保留 `type=http`。`serverPort` 是控制连接端口，网站端口取真正的公网监听。前置路由满足目标时继续本地操作；未知项明确标出。Skill 不执行服务端改动，也不把独立业务要求省略成“已可用”。

## 3. 执行并分层验证

- 已授权启动、配置确认后执行 `start`。二进制不存在时自动下载校验；已有二进制不自动升级。两模式同时可用返回 `3`，使用用户已明确的选择，否则询问。
- 启动、检查、日志和停止使用同一项目与同一 `--home`。项目流程的 `--home` 提供共享默认及二进制；实际运行文件在其中的 `projects/<项目标识>/run/`。停止 A 不操作 B，不按进程名批量停止。
- 配置变化返回 `5/config_changed`，原进程继续使用原配置。说明当前项目的变化和影响，按已有重启授权或新确认执行 `stop` 再 `start`，不静默替换。
- 调用中断后重新 `status`。等待启动前已保存 PID、进程创建时间、参数和项目身份；`timeout/running_unverified` 不等于成功。停止不依赖当前连接配置完整。
- `ok/already_running`、`verified=true` 只代表历史启动日志证据；`verification_scope=startup_log`、`public_endpoint=not_checked` 不是公网探测。每条代理还须有最新 `start proxy success` 且没有更晚失败。
- 获授权访问公网时，请求**用户给定的完整 URL**，与本地同路径和预期内容对照。保留 TLS 校验，设置单次超时和总预算；只对瞬时错误最多尝试 3 次。HTTP 200 可能是准备页，应用 4xx 可能证明到达应用，均不自动等于业务完成。改变 Host/SNI、直连 IP 或跳过证书校验只算辅助诊断。

最后交付完整 URL，以及本地服务、FRP 登录、各代理、公网响应和业务目标的分别结果。入口尚未确认时说明缺项，不编造可用 URL。应用需加载配置或业务未就绪时具体指出。

## 子命令与边界

| 子命令 | 用途 |
| --- | --- |
| `project-info` | 只读来源、固定域名与路由 |
| `project-config` | 保存新连接或定点更新项目路由；不启动 |
| `start --mode official/sakura --wait N` | 有界启动验证，默认 15 秒 |
| `status` / `logs -n N` / `stop` | 检查、脱敏日志、停止当前项目 |
| `install` / `update --version X` | 安装或明确升级二进制；樱花不接受指定版本 |
| `guide-init --source sakura --scope project` | 保存樱花 key 与隧道 ID，按项目隔离运行 |

退出码：`0` 子命令完成；`1` 失败或启动未确认；`2` 用法错误；`3` 模式歧义；`4` 缺配置/代理/路由；`5` 运行配置变化；`6` Secret 写入安全拒绝。

运行时由 `pyproject.toml`、`uv.lock`、`.python-version` 与入口 bootstrap 钉定。环境损坏按锁修复；缺系统 uv 时给安装办法。下载统一 30 秒超时、瞬时错误至多 3 次尝试和指数退避，确定性 4xx 立即失败。完整性校验不等于发布者签名验证，不自动换第三方镜像。

Token 不进入命令行。项目连接只存 `.env.local`，有效 frpc 配置经子进程环境交给模板，不再复制进全局运行文件。项目日志写入时脱敏，之后换 Token 也不会使旧日志回显完整值。对话与诊断不得重组完整凭证。
