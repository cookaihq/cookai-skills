# 独立 Skill 更新

Git 检出使用随包 `scripts/check_update.ps1` / `check_update.sh`。自动检查读取个人同名目录 `.env` 的 `AUTO_UPDATE_CHECK`；`0` 关闭。检查节流为 6 小时，网络检查仅一次、5 秒预算，失败继续当前业务。更新报告包括本 Skill 和同仓其他改动；pull 单位是整个仓库。

只有用户同意当前报告，才用同一脚本 `-Pull` / `--pull`。要求 main、已跟踪文件干净、可快进、版本元数据有效。拒绝或失败后保留当前版本，继续任务；不得 stash/reset 等绕过。

复制安装读取 `update.json`，只查询 `agent-instructions-update/v` 前缀的稳定 tag。返回远端版本、固定 tag 和 commit SHA；401/403 为 `auth-required`，超时/连接/5xx 为 `unknown-network`，元数据无效为 `invalid-metadata`。没有可验证的查询能力时报告 `not-applicable`。这些状态都不阻断规则维护。

替换前展示本地/远端版本、仓库、tag、commit SHA 和安装实体目录。用户确认后按这个 SHA 下载到同文件系统暂存位置，核对 Skill 名称、版本与简介前缀，再整体替换并保留旧目录以便恢复；替换后再次回读元数据和下载来源记录。不能重新解析浮动 tag，不能从版本文字推断下载 commit。

Claude Code、Codex 使用其实际 Skill 安装目录；WorkBuddy 复制安装仅在其安装器支持固定 commit 和可验证替换时应用，不假造未公开 API。安装形态不明确先检查原生安装记录。更新不删除个人设置或历史，不以“重新安装最新版”代替已确认的固定目标。
