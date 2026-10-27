# 复制安装更新协议

在 Git 适配器报告非 Git 检出时读取。三宿主共用本协议，不能假定 WorkBuddy 有未验证的更新 API。

1. 读取 `~/.config/pdf2markdown/.env` 的 `AUTO_UPDATE_CHECK`；仅值 `0` 关闭。该开关无需业务凭证 flag；其他全局字段不因此启用。检查失败不中断业务。
2. 从本地 `SKILL.md` 读取 `name`、字符串 `metadata.version` 与同号 description 前缀。读取随包 `update.json`，只查 `pdf2markdown/v` 前缀的稳定 SemVer 标签，取得精确 tag/version 及其 commit SHA；不能使用多 Skill 仓的 latest release。检查总预算 5 秒、一次尝试，不在启动路径重试。
3. 返回 `latest`、`update-available`、`unknown-network`（超时/DNS/5xx）、`auth-required`（401/403）、`invalid-metadata` 或 `not-applicable`。查询条件不足时继续当前任务；不声称已是最新。
4. 有更新时展示本地与远端版本、仓库 URL、tag、commit SHA 和安装目标目录；未经用户确认不下载、不替换。2.0.0 升级需先说明 README 的 1.x 任务处理方式。
5. 用户确认后，让实际宿主支持的安装器按**该 commit SHA**下载到目标目录同一文件系统的临时目录，校验 Skill 名称、版本和前缀，再通过目录改名整体替换。保留原目录以便失败恢复。不得在应用阶段重新解析标签改变目标。
6. 替换后重新读取目标元数据，并用安装器的下载来源记录证明 commit 身份。缺少按 commit 下载、来源记录或可靠替换能力时返回 `not-applicable`；失败保留/恢复原目录并返回 `apply-failed`，成功为 `applied`。继续本次业务，不自动更改用户配置。

临时更新材料按调用项目的临时文件规则组织，不把凭证或用户任务放入安装包。Git 检出继续使用随包原样 Bash/PowerShell 适配器，拉取单位是整个仓库；脚本不执行提交、推送、reset、checkout 或 stash。
