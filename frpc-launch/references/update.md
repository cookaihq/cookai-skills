# 复制安装的自动检查更新

Git 检出按 SKILL.md 第 0 步执行随包适配器；本文件只用于复制安装。

1. 从本地 `SKILL.md` 读取 `name`、字符串 `metadata.version` 和 description 前缀，三者合法且一致才能继续。读取 `update.json` 的公开仓库、`skill_path`、`channel`、`tag_prefix` 与 `ref_type`，不读取业务凭证。
2. `AUTO_UPDATE_CHECK` 只读用户个人目录下 `~/.config/frpc-launch/.env`；值为 `0` 时不检查。每 6 小时最多检查一次，查询预算 5 秒、单次尝试，失败继续当前业务。不能把失败或非 Git 检出的退出码 0 解释为“已是最新”。
3. 只查询 `frpc-launch/v` 前缀的稳定 SemVer tag，取得该 tag 实际指向的 commit SHA（annotated tag 要解析到 commit），不使用多 Skill 仓的仓库级 latest release。
4. 有更新时展示本地/远端版本、仓库、tag、commit SHA 和安装目录。用户确认后才按该 SHA 下载 `frpc-launch/` 到同一文件系统的任务临时目录，不能下载时重新解析浮动 tag。
5. 核对下载来源记录中的 SHA，以及临时目录的 Skill 名称、版本和 description 前缀。通过后用目录改名替换，失败保留或恢复原目录；替换后再次读取目标 `SKILL.md` 验证。不能从 SKILL.md 推断下载的 SHA，也不修改使用者配置或全局挂载。

状态使用 `latest`、`update-available`、`unknown-network`、`auth-required`（401/403）、`invalid-metadata`、`not-applicable`、`applied` 或 `apply-failed`。所有状态都允许继续当前任务。Claude Code、Codex、WorkBuddy 按实际可用的安装能力执行；没有可核实的查询、固定 SHA 下载或原子替换能力时报告 `not-applicable`，不得臆造 WorkBuddy 更新 API。
