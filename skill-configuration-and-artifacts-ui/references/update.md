# 复制安装更新

Git 检出用随包检查脚本。复制安装读取 `update.json` 与本地 SKILL 元数据，按该 Skill 的 `tag_prefix` 查询 stable tag 和其 commit SHA，不能使用多 Skill 仓的 latest release。

查询失败分清 `unknown-network`、`auth-required`、`invalid-metadata`，继续当前任务。发现新版本先展示本地/远端版本、仓库、tag、commit SHA 和安装目标，明确同意后才下载替换。缺少按固定 commit 下载及可恢复替换能力时报告 `not-applicable`，继续任务。

下载到同一文件系统的任务临时目录，校验名称、版本和 description 前缀；按确认的 commit SHA 下载，不重新解析 tag。目录改名整体替换前保留旧目录，替换后再次校验并记录下载 commit。失败恢复旧目录，报告 `apply-failed`。WorkBuddy 使用实际支持的安装器，不能猜测其更新 API。开关只读用户主目录 `.config/skill-configuration-and-artifacts-ui/.env` 的 `AUTO_UPDATE_CHECK`，不从进程或项目同名 key 读取。
