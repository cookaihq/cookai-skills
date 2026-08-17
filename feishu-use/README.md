# feishu-use

让 Agent 安全地使用飞书官方 [`lark-cli`](https://github.com/larksuite/cli) 的通用入口 Skill。

它在实际操作飞书 Base、文档、日历、消息等资源前完成：

- 检查 `lark-cli` 是否安装且可执行
- 使用 CLI 自带命令检查当前版和最新版
- 在安装或更新前征求用户同意
- 区分用户身份与应用身份
- 核对目标 Profile 和飞书账户
- 使用链接 + 二维码完成分轮 Device Flow 登录
- 检查并增量申请最小 scopes
- 识别外部凭证提供方和 `bot-only` 策略，避免错误发起登录
- 保留 `lark-cli` 的高风险操作确认门禁

## 安装

使用 Skills CLI：

```bash
npx skills add cookaihq/cookai-skills --skill feishu-use -g -y
```

本 Skill 调用官方 `lark-cli`。若尚未安装，它会先询问用户，再使用官方入口：

```bash
npx @larksuite/cli@latest install
```

## 触发示例

```text
用飞书 CLI 读取这个多维表格。
```

```text
检查 lark-cli 是否需要更新，然后用 Alice 的账户查看今天的日程。
```

```text
帮我重新登录飞书，并只授权读取 Wiki 和 Base 的权限。
```

## 工作流程

```text
安装检查
  → 最新版本检查与用户选择
  → 应用配置检查
  → user / bot 身份判断
  → 账户强匹配或人工确认
  → 最小 scope 检查与分轮授权
  → 调用对应 lark-cli 业务命令
```

`feishu-use` 不重复维护 Base、文档、日历等领域参数。门禁通过后，应继续使用官方 `lark-base`、`lark-doc`、`lark-calendar` 等 Skill；没有对应 Skill 时读取具体命令的 `--help`。

## 只读预检

Skill 内置一个无第三方 Python 依赖的只读检查器：

```bash
python3 scripts/preflight.py \
  --identity user \
  --expected-name "Alice" \
  --scope "wiki:node:retrieve base:record:read"
```

它输出结构化 JSON，但不会安装、更新、登录、切换 Profile 或修改凭证。所有写操作仍由 Agent 向用户确认后执行。

## 账户安全

Open ID 是强匹配；显示名可能重复，只能作为弱匹配并要求人工确认。

当前官方 CLI 在新用户登录成功后会替换当前 Profile 的登录用户，并清理旧用户 Token。因此 `feishu-use` 在账户不一致时会先说明影响，不会静默登录另一个账户。需要长期保留多个账户时，应使用用户明确创建的独立 Profile。

## 测试

```bash
python3 -m pytest -q feishu-use/tests/test_preflight.py
```

测试全部使用伪造 CLI 响应，不访问飞书、不读取真实凭证，也不修改本机 `lark-cli` 配置。
