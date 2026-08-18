---
name: feishu-use
version: 1.1.0
description: >-
  v1.1.0｜Use when the user wants an agent to operate Feishu/Lark through the official
  lark-cli, or asks to install, update, configure, log in, re-authorize,
  verify, or switch the Feishu account used by lark-cli. This is the shared
  gateway before lark-base, lark-doc, lark-calendar, lark-im, and other domain
  operations: it checks that lark-cli is installed and current, asks before
  installation or update, verifies the required user/bot identity and
  requested account, guides device-flow login with a displayed URL and QR
  code, checks scopes, then hands the request to the relevant lark-cli
  command. Do not use for browser-only Feishu UI instructions or when the user
  explicitly requires a different integration instead of lark-cli.
---

# feishu-use

`feishu-use` 是所有 `lark-cli` 操作之前的通用门禁。它只负责运行环境、版本、Profile、身份、账户、授权和安全确认；Base、文档、日历等业务参数继续交给对应的 `lark-*` Skill 或 `lark-cli <service> --help`。

## 执行原则

- 先完成门禁，再执行业务命令；不要把“CLI 已配置”误当成“用户已登录”。
- 安装、更新、配置绑定、换账号和授权都需要用户确认，不能静默修改环境。
- 用户身份和应用身份必须显式选定为 `user` 或 `bot`，不要依赖 `auto` 猜测。
- 只申请本次操作需要的最小 scopes；不要默认申请 `all`。
- 同一任务中门禁通过后无需重复执行，除非 Profile、目标账户、身份或 CLI 状态发生变化。

## 1. 收集门禁输入

从用户请求和领域 Skill 中确定：

1. **目标操作**：后续要运行的 `lark-cli` 命令。
2. **身份**：个人 Wiki、云盘、日历、邮箱以及“用我的账户”通常是 `user`；明确以应用/机器人执行才是 `bot`。不确定时先问。
3. **目标账户**：优先使用用户给出的 `ou_...` Open ID；只有显示名时属于弱匹配；未指定账户时可以使用当前 Profile 的唯一登录用户，并在执行摘要中说明。
4. **Profile**：只有用户指定或当前环境存在多个 Profile 且结果有歧义时才传 `--profile`；不要擅自切换默认 Profile。
5. **scopes**：已知时传给预检；未知时先完成身份门禁，再由实际命令的结构化错误增量补权。

邮箱、手机号和显示名不能直接当 Open ID。若无法可靠解析，只能在登录后展示实际账户名称和脱敏 Open ID，让用户确认。

## 2. 运行只读预检

先定位本 Skill 目录，再运行：

```bash
uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/preflight.py" \
  --identity user \
  --expected-open-id "ou_xxx" \
  --scope "wiki:node:retrieve base:record:read"
```

按实际情况省略 `--expected-open-id`、`--expected-name`、`--profile` 和 `--scope`。脚本只执行只读探测，不安装、不更新、不登录、不写配置，并输出稳定 JSON。

**必须用 `uv run --project` 起，禁止裸 `python3`**：解释器由本 Skill 的 `pyproject.toml` + `uv.lock` 钉死，venv 落在 `$SKILL_DIR/.venv`，首次运行会自动创建。万一被裸 `python3` 起了，脚本自带的 bootstrap 也会把进程 exec 回该 venv。

若环境没有 `uv`，脚本会报错并给出安装命令（`curl -LsSf https://astral.sh/uv/install.sh | sh`）——不要静默替用户安装；也可以跳过脚本，直接按本节状态机依次运行 `command -v lark-cli`、`lark-cli --version`、`lark-cli update --check --json` 和对应认证命令。

**网络抖动**：脚本内部对五个只读探测命令统一做瞬时失败重试（超时、连接重置、DNS 失败、CLI 报的 5xx/429 → 至多 3 次尝试，退避 1s、2s，重试过程打到 stderr）；鉴权失败、参数非法等确定性错误不重试，直接落到对应终态 stage。Agent 拿到终态后不要再自己包一层重试。

用户明确选择跳过更新后，重新运行时加：

```bash
--allow-outdated
```

版本检查因网络失败且用户明确同意继续后，加：

```bash
--allow-unknown-version
```

仅凭显示名匹配且用户确认后，加：

```bash
--accept-name-match
```

这些 flag 只代表**本次对话中已获得对应确认**，不能预先添加，也不能持久化为默认值。

## 3. 按预检状态处理

| `stage` | 处理 |
|---|---|
| `cli_missing` | 告知未安装，询问是否使用官方安装方式；同意后安装并重跑预检 |
| `cli_broken` | 报告可执行文件路径和版本读取失败，询问是否重装 |
| `update_available` | 展示当前/最新版本，让用户选择更新、使用当前版本继续或取消 |
| `update_check_failed` | 说明无法确认最新版，让用户选择重试、在未知版本状态下继续或取消 |
| `config_required` | 引导创建应用配置或绑定既有 Agent 配置 |
| `profile_required` | 指定 Profile 不存在；列出现有 Profile 让用户选择，不要新建或切换默认 Profile |
| `login_required` | 按 Device Flow 登录，详见 `references/authentication-flow.md` |
| `account_confirmation_required` | 展示名称与脱敏 Open ID；用户确认后用 `--accept-name-match` 重跑 |
| `account_mismatch` | 先说明换账号的替换影响，再让用户选择登录目标账户、继续当前账户或取消 |
| `scope_required` | 只对 `missing_scopes` 做增量授权 |
| `identity_unavailable` | 检查 strict-mode、身份策略或 Bot 凭证；外部凭证提供方管理的身份回到提供方修复，不运行 `auth login` |
| `auth_check_failed` / `scope_check_failed` | 转述结构化错误；不要把 Keychain、网络或配置故障一律当成未登录 |
| `ready` | 执行业务命令 |

完整账户判定规则见 `references/account-matching.md`。

## 4. 安装与更新

### 未安装

先告诉用户安装会修改本机 CLI 环境，并询问是否继续。宿主有受控 CLI 安装器时优先使用；否则使用官方入口：

```bash
npx @larksuite/cli@latest install
```

需要 Node.js/npm。安装后必须重新验证：

```bash
command -v lark-cli
lark-cli --version
```

### 有新版本

最新版只以以下命令的结果为准，不自行查询或硬编码版本：

```bash
lark-cli update --check --json
```

向用户提供：

1. 更新到最新版并继续（推荐）
2. 使用当前版本继续
3. 取消

用户同意更新后执行：

```bash
lark-cli update --json
```

更新后重新运行 `lark-cli --version` 和预检。若当前版本缺少本次必需命令，则不能提供“旧版本继续”作为可执行选项。CLI 更新可能同步官方 Skills；完成当前任务后提醒用户重启 Agent 以加载新 Skill 内容。

## 5. 配置与登录

没有应用配置时，先让用户选择：

- 本地独立配置：`lark-cli config init --new`
- 使用已有 App ID/Secret：`lark-cli config init --app-id ... --app-secret-stdin`
- OpenClaw、Hermes 或 Lark Channel 环境：确认身份策略后使用 `lark-cli config bind`

禁止把 App Secret 放进命令参数、日志或回复。Agent 环境中的 `config bind` 可能改变应用和身份策略，必须先确认。

内置凭证模式下的用户登录或增量授权统一采用分轮 Device Flow。开始前必须阅读并执行 `references/authentication-flow.md`。

**重要：当前官方 CLI 在一次用户登录成功后，会把当前 Profile 的用户列表替换为新用户，并清理旧用户 Token。** 因此账户不一致时，必须在发起登录前明确说明这个影响并取得同意；不要称为无副作用的“临时切换”。保留两个账户需要用户明确创建和选择独立 Profile，不能自动创建。

## 6. Scope 增量授权

用户身份可先检查：

```bash
lark-cli auth check --scope "<required scopes>" --json
```

只对返回的 `missing` 发起新一轮 Device Flow：

```bash
lark-cli auth login --scope "<missing scopes>" --no-wait --json
```

如果错误含 `console_url`，说明应用后台可能尚未开通 scope。原样展示该 URL，让用户先完成后台授权，再发起**全新的** Device Flow；不要重复轮询旧 code。

Bot scope 由应用后台权限决定，不能用用户 `auth login` 修复。Bot 权限错误应展示 `console_url` 或缺失 scope，并引导用户配置应用。

外部凭证提供方管理的 User 身份不能通过本机 Keychain 的 `auth check` 或 `auth login` 修复。预检会把 scope 检查标为 `delegated_to_external_provider`；继续执行实际业务命令，并按提供方返回的结构化权限错误处理。

## 7. 执行业务命令

`ready` 后：

1. 优先使用对应官方领域 Skill 选择命令和构造参数。
2. 没有领域 Skill 时先读 `lark-cli <service> --help` 和具体命令 `--help`。
3. 用户提供 URL 时优先使用对应 `+url-resolve`，不要把 Wiki token、完整 URL或其他 token 猜成资源 ID。
4. 使用 `--profile` 和明确的 `--as user|bot` 保持与预检一致。
5. 命令返回缺失 scope 时回到增量授权，不要申请全域权限。

## 8. 高风险操作

- 写入或删除前确认用户意图；支持 `--dry-run` 时先预览。
- 高风险命令首次不要加 `--yes`。
- 若退出码为 `10` 且结构化错误为 `confirmation_required`，展示 `error.risk.action` 和关键参数，等待明确同意；同意后只在原 argv 末尾追加 `--yes`。
- 不得把退出码 `10` 当网络错误，也不得静默重试。
- 用户参数通过 argv 传递，不用 `sh -c` 拼接。
- 不输出 App Secret、Access Token、Refresh Token、device code；Open ID 对用户展示时使用脱敏形式。

若命令输出 `_notice.update`，不要忽略。当前任务若已按用户选择使用旧版，可在完成后再次提醒；否则先处理更新选择再继续。
