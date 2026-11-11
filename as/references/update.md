# 复制安装的更新

Git 安装使用随包更新脚本。复制安装读取 `update.json`，只查询 `as/v` 前缀的 stable tag 和对应 commit SHA，不能使用多 Skill 仓的 latest Release。

5 秒内未能确定更新时报告 `unknown-network` 并继续业务；401/403 为 `auth-required`，元数据错误为 `invalid-metadata`。查询结果可用时，展示本地版本、远端版本、仓库、精确 tag、commit 和安装目录，用户确认后才下载与替换。

按已确认 SHA 下载到同文件系统的项目忽略临时目录，验证 `name: as`、`metadata.version` 与 description 版本前缀，再原子替换完整 Skill。保留原目录以便失败恢复，不删除安装目录之外的 `.config/as/`。替换后重新核验版本及下载来源 SHA；宿主不能提供固定下载或恢复时报告 `not-applicable`，不声称更新成功。
