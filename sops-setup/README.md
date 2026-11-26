## SOPS Setup

`sops-setup` 帮助你用 SOPS 和 age 在 Git 中管理加密配置。Git 记录公开的接收者和加密后的配置值，能解密的私钥由你已有的密钥管理工具保存。Skill 可以检查已接入 SOPS 的仓库，也可以为新仓库建立开发与生产配置并验证访问范围。

### 让 Agent 安装

本机开发可安装当前检出中的 `sops-setup/` 目录。使用已发布版本时，在 Claude Code、Codex 或 WorkBuddy 的对话中粘贴下面这段话。Agent 会按当前宿主的 Skill 入口安装、检查工具和验证识别状态。

> 请把 https://github.com/cookaihq/cookai-skills 仓库里的 `sops-setup/` 安装为当前 Agent 的 Skill。GitHub 遇到网络故障时尝试同一版本的 CNB 仓库 https://cnb.cool/zhidateam/tannt/tannt-skills。检查当前宿主能否发现并调用它，再告诉我是否需要安装 SOPS 或 age。

Claude Code 可在新会话中输入 `/sops-setup`，Codex 可用 `$sops-setup`。WorkBuddy 在当前客户端的 Skill 管理入口安装或导入目录，再从对话点名 `sops-setup`。

### 第一次使用

已有 SOPS 配置时，在目标仓库的会话中发送下面的请求。这项检查只返回路径、接收者和状态。

> 用 sops-setup 检查当前仓库的 SOPS 配置和开发环境能否解密，不显示明文。

新仓库可以发送下面的请求。Agent 会先核对项目布局和需要使用的接收者，再写 `.sops.yaml` 与加密文件。

> 用 sops-setup 为当前仓库初始化开发和生产的加密配置。用合成数据验证两个环境的密钥隔离，并告诉我生成了哪些文件。

### 密钥从哪里来

Skill 不需要自己的业务配置。公开接收者在项目的 `.sops.yaml` 及密文元数据中。解密由项目的包装命令或 SOPS 读取授权的私钥来源。沿用项目已配置的凭证库、Secret 注入或 SOPS 密钥文件。要新增身份时，说明它要用于哪个仓库和环境、谁应当能解密，以及你准备保存到的具体位置。来源、验证方式以及不同系统的 SOPS 密钥路径见 [操作指引](references/setup.md#检查已有文件)。

### 接收者变更与排查

- 确认新接收者后，可以说「用 sops-setup 为开发配置接入新接收者，验证新身份能解密。」接收者变更会同时核对规则和对应的加密文件。
- 遇到解密失败时，可以说「检查哪个加密文件无法解密、它的接收者是什么、当前进程配置了哪些密钥来源。隐藏密钥和明文。」常见原因是当前身份不在文件的接收者中，或终端运行的 SOPS 版本和密钥路径与预期不同。

### 更新

可以让 Agent 检查 `sops-setup` 更新。Git 安装使用随包的 Bash 或 PowerShell 检查入口，复制安装按已发布的固定 tag 和提交核对。更新由你确认，检查失败时可以继续使用现有版本。要关闭检查，可以对 Agent 说「关闭 sops-setup 的自动检查更新」。

### 验证范围

`1.0.0` 已在 Windows 原生环境的 Codex CLI 0.172.0 验证 Skill 发现、调用和静默解密。随包的初始化和接收者轮换示例使用 SOPS 3.13.3、age 1.3.2 验证。macOS 实机验收待完成。
