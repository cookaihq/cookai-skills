---
name: frpc-launch
metadata:
  version: "1.3.1"
description: v1.3.1｜本地启动、检查或停止 frpc，将本地服务通过既有 FRPS 公网入口转发；支持自部署/宝塔 frps 和樱花 FRP。适用于“启动 frpc”“把本地端口通过这个域名访问”“FRP 连不上”。先核对已有入口、域名/路径与本地端口，再配置和验证。不负责部署 frps、修改服务器/Nginx/DNS/证书或注册开机自启；Windows 原生运行未实现。
compatibility: Claude Code, Codex or WorkBuddy with a local shell; macOS/Linux, uv >= 0.8. Native Windows is not supported; host acceptance status is listed in README.md.
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

**配置分层**（每变量独立取首个非空来源）：进程环境变量 → `$PWD/.env.frpc-launch` → `$PWD/.env.local` → `$PWD/.env` → 全局受管目录。`guide-init` 的 `FRPC_LAUNCH_INIT_TOKEN`、`FRPC_LAUNCH_SAKURA_KEY` 与 `FRPC_LAUNCH_SAKURA_TUNNELS` 也按相同顺序读取，写入仍由显式 `--scope` 决定。`FRPC_LAUNCH_CONFIG` 可指向项目级 frpc.toml；`FRPC_LAUNCH_MODE=official|sakura` 显式指定模式。**使用全局配置启动时必须向用户报告「使用了全局配置」及具体来源**（脚本输出已含 `config_source`，转述给用户）。

## 第 0 步：检查更新（每次运行都做）

Git 检出先在本 Skill 目录下运行 Bash 适配器（PowerShell 宿主用随包 `scripts/check_update.ps1`，不表示业务脚本支持 Windows）：

```bash
scripts/check_update.sh
```

- 退出码 `0`：直接进入下一步，**不要**向用户复述脚本输出。
- 退出码 `10`：把脚本打印的报告**原样转述给用户**，并询问是否现在拉取。
  - 用户同意 → 运行 `scripts/check_update.sh --pull`，成功后按新版本继续；失败时把脚本给出的拒绝原因转述给用户，然后**按当前版本继续本次任务**，不要卡在更新上。
  - 用户拒绝或不理会 → 按当前版本继续，本次任务内不再提更新。
- 用户说「关掉自动检查更新」→ 往 `~/.config/frpc-launch/.env` 写 `AUTO_UPDATE_CHECK=0`（该文件已存在则只改 / 追加这一行，不动其他行）。

**更新检查永远不是任务的阻塞项**：检查失败、拉取失败、用户不理会，一律落到「按当前版本继续干活」。

复制安装按 [更新协议](references/update.md) 读取 `update.json`，查询本 Skill 稳定 tag 的版本和精确 commit SHA，确认后才替换。非 Git 检出的退出码 0 不表示已是最新。WorkBuddy 未提供可核实的安装/替换能力时报告 `not-applicable`，继续本次任务。

## 第 1 步：确定已有入口与本地目标

用户说“使用这个 FRP 域名”时，先核对现有配置和当前进程；不能直接按新建服务器部署处理。HTTP/HTTPS、自定义域名或按路径转发时，读取 [入口与路由核对](references/http-routing.md)。

1. 保留用户指定的协议、域名、端口和路径，列出每个公网入口对应的代理名、`type`、`customDomains`/`subdomain`、`locations`、`localIP:localPort` 及配置来源。未知项标明待核对，不拿管理面板站点名替换访问域名。
2. 只读核对工作文件夹、实际使用的配置文件、受管目录和本地服务响应。`status` 必须使用该进程原来的 `--home`；没有 PID 记录只表示此目录没有受管进程，不证明整台机器没有 frpc。沿项目记录核对其他已知实例，不重复启动、不按进程名批量停止。
3. 对于已配置的公网 HTTPS → Nginx → frps HTTP 入口 → frpc → 本地 HTTP 服务，保留 `type=http`。`serverPort` 是 frpc 连接 frps 的端口；`vhostHTTPPort` 是 HTTP 代理入口，可能只由前置 Nginx 使用；公网 URL 端口由真正的监听入口决定。不能因 URL 是 HTTPS 就改用 `type=https` 或把 vhost 端口拼进 URL。
4. 已有 DNS、TLS、Nginx 和 FRPS 路由满足目标时，在授权范围内补齐本地服务与 frpc。确有服务器端缺项才说明具体字段、证据、影响与所需授权；本 Skill 不执行服务器改动。日志策略等独立业务要求单独报告，不能描述成 FRP 转发尚未配置，也不能把已约定的业务验收要求直接删掉。

## 第 2 步：执行本地操作并逐层验证

- 已授权启动且配置已确认时执行 `start`；已在运行先检查，只有配置需要变更才按退出码 5 的流程处理。应用未启动或仍用旧配置时报告具体应用状态，不重启无关隧道。
- `start` 的 `ok` / `already_running` 与 `status.verified=true` **只表示启动日志证据**。输出明确给出 `verification_scope=startup_log`、`public_endpoint=not_checked`；其中 `verified` 是 PID 记录中的历史值，不是当前连通性探测。旧记录没有该字段时为 `null`，不可当作通过。官方模式目前检查登录成功，不能据此推断每条代理注册成功。
- 核对每条目标代理的最新日志：登录成功之后，还须有该代理的 `start proxy success`，且没有更晚的失败。多代理只成功一条不能替其他代理验收。日志不足时报告未验证，不从进程存活反推成功。
- 在授权允许请求该入口时，以有超时的只读请求检查**用户给定的完整公网 URL**，保留正常证书校验；对照相同路径的本地响应和预期内容。HTTP 200 可能是准备页，应用返回的 4xx 可能证明到达应用；状态码本身不能证明业务完成。每个域名/路径分别核对，校验文件按预期内容或哈希核对。仅为定位问题改变 Host、SNI、证书校验或绕过代理时标记“辅助诊断”，不替代原入口验收。
- 最后先给用户实际可访问的 URL，再分别报告本地服务、FRP 登录/各代理、公网响应、业务目标；每项只写已验证结果。需要重启应用加载配置、或业务目标尚未完成时明确指出，不能只交付 PID。

## 子命令速查

统一入口 `uv run --project <skill> <skill>/scripts/frpc_launch.py [--home DIR] [--json] <子命令>`；Agent 一律加 `--json` 解析结果。

**必须用 `uv run --project` 起，禁止裸 `python3`**：解释器由 `<skill>/pyproject.toml` + `uv.lock` 钉死，venv 落 `<skill>/.venv`（首次运行自动创建，需要 `uv` ≥ 0.8；缺 uv 时脚本报错并给安装命令）。被裸 `python3` 起时脚本自带的 bootstrap 会把进程 exec 回该 venv。

**网络抖动**：所有出网请求（GitHub Releases 元数据、natfrp 客户端清单、二进制下载）都是幂等 GET，统一带 30s 超时并对瞬时失败（连接/读取超时、连接重置、DNS 失败、HTTP 5xx/429/408）重试至多 3 次（退避 1s、2s；429 遵循 `Retry-After`，上限 60s），重试过程打到 stderr；确定性 4xx（404 版本不存在等）立即报错不重试。Agent 不需要自己再包一层重试。

| 子命令 | 说明 | 常用参数 |
|---|---|---|
| `start` | 后台启动 + 有界启动验证（默认 15s） | `--mode`、`--wait N` |
| `stop` | 优雅停止（SIGTERM→SIGKILL），stale pid 只清文件不误杀 | `--mode` |
| `status` | 分模式报告进程状态、历史启动日志证据/配置来源/版本；不请求公网入口 | `--mode` |
| `logs` | 输出受管日志尾部（已掩码） | `--mode`、`-n N` |
| `install` | 安装受管二进制（已装则不重复下载） | `--mode`、`--version X` |
| `update` | 显式升级二进制（唯一会替换二进制的入口） | `--mode`、`--version X` |
| `guide-init` | 引导流程落盘配置（见下节） | `--scope`、`--source`、`--proxy` 等 |

**退出码语义**：`0` 当前子命令完成（start 只确认启动日志）；`1` 失败（含启动验证失败与 `running_unverified`）；`2` 用法错误；`3` 模式歧义（两模式配置同存，须询问用户）；`4` 未配置（进入引导流程）；`5` 配置已变更（须确认 stop 后再 start）；`6` Git 安全拒绝（Secret 目标被跟踪或未忽略，先解决实际目标文件的安全条件，不使用 `--allow-tracked` 绕过）。

## 引导流程编排（start 退出码 4 时，Agent 按序执行）

1. **确定作用域（默认项目级，不询问）**：
   - **默认**：写当前项目 `$PWD/.env.local`（official 另生成项目级 frpc.toml 并以 `FRPC_LAUNCH_CONFIG` 指向），即 `guide-init --scope project`。用户没有明确要求全局时一律走这条，不要把作用域问题抛给用户
   - **仅当用户在本轮明确要求「全局」「长期保存」「多项目共用」时**，才用 `--scope global` 写 `~/.config/frpc-launch/`
   - 落盘后必须告知用户实际写入的作用域与文件路径
   - **注意**：项目在 git 工作树内时，须先把 `frpc.toml` 与 `.env.local` 加入 `.gitignore`，否则 guide-init 会以退出码 6 拒绝写入 Secret——这是防止凭证被提交的保护，引导用户补 `.gitignore` 后重试
2. **询问是否需要配置帮助**：
   - 不需要 → 告知所选作用域的文件位置与最小模板（official：`serverAddr`/`serverPort`/`auth.token` + `[[proxies]]`；sakura：`FRPC_LAUNCH_SAKURA_KEY` + `FRPC_LAUNCH_SAKURA_TUNNELS`），等用户自行配置后重试 start
3. **需要帮助 → 三来源分支**：
   - **自部署 FRPS**：先读已有项目记录和获授权的配置；仅补问缺失的 serverAddr、serverPort、认证方式及代理需求。客户端 `serverPort` 对应实际可达的服务端连接入口（直接连接时为 `bindPort`）；不能从公网网站端口推导。
   - **宝塔 frps**：指引查看已安装 FRP 服务端插件的配置，核对 `bindPort`、认证、`vhostHTTPPort`/`vhostHTTPSPort`；同时核对现有反向代理是否已经终止 TLS 并保留 Host。面板路径随插件版本变化，不假定必须新增站点或逐个添加子域名。具体值只取自实际配置。
   - **樱花FRP**：指引用户到樱花管理面板取访问密钥（用户中心）与隧道 ID（隧道列表，逗号分隔多条）。如实说明：macOS 官方更推荐其图形启动器，本 Skill 直接托管定制 frpc 属高级用法
4. **调用 guide-init 落盘**（token/key 一律经环境变量注入，不进命令行参数）：

   ```bash
   # official（frps/baota）：
   FRPC_LAUNCH_INIT_TOKEN=<token> uv run --project <skill> <skill>/scripts/frpc_launch.py --json \
     guide-init --scope project --source frps \
     --server-addr <host> --server-port <port> \
     --proxy "name=web;type=http;localPort=8080;customDomains=a.example.com"
   # sakura：
   FRPC_LAUNCH_SAKURA_KEY=<密钥> FRPC_LAUNCH_SAKURA_TUNNELS=<id,id> \
     uv run --project <skill> <skill>/scripts/frpc_launch.py --json guide-init --scope project --source sakura
   ```

   （只有用户明确要求全局时才把 `--scope project` 换成 `--scope global`）

5. `guide-init` 是完整配置生成器，会覆盖目标 `frpc.toml`，不是追加代理接口。已有配置应保留其他代理、认证及高级字段，在获授权后定点修改；不要为了新增路径调用它重建整份文件。支持字段为 `name/type/localIP/localPort/remotePort/customDomains/locations`，未知、重复或类型不适用字段直接失败；`locations` 只支持 HTTP。高级原生配置使用原 TOML 并执行 `frpc verify`，不扩展简写来猜参数。
6. 已明确要求启动时继续 `start` 与第 2 步验证；只要求生成配置时交付文件与检查结果，不自动公开本地服务。`frpc verify` 只校验配置，不能替代公网验证。

## 掩码规则

- token / 访问密钥回显一律 `head4****tail4`（如 `tok_****1234`）；**禁止在对话、命令行参数或日志中出现完整值**
- `guide-init` / `start` / `status` / `logs` 的输出已内置掩码；Agent 转述时不得从别处拼回完整值

## CRITICAL

- **start 永不自动升级二进制**；升级只能走显式 `update`
- **两模式配置同存且未指定时（退出码 3）必须询问用户**，不得默认选择
- **配置变更（退出码 5）必须向用户展示差异并确认 stop 后再 start**，不得静默替换运行中的进程
- 下载失败/校验不一致：如实报错，**不自动切换第三方镜像**；可提示用户自行配置代理
- 启动日志、各代理注册、公网响应与业务目标分别验证；`verified` 不代表公网或业务通过
- 完整性校验（官方 SHA-256 / 樱花 size+hash）只保证下载未损坏，**不等于发布者签名验证**
