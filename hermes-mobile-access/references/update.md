# 固定版本更新

Git 检出使用随包 `check_update.ps1` / `check_update.sh`，检查与应用分开。复制安装先读取 `update.json` 的仓库、目录与 tag 前缀，只查询 `hermes-mobile-access/v` 的稳定 SemVer tag，预算 5 秒、单次请求，失败继续当前业务。

`AUTO_UPDATE_CHECK` 仅从用户主目录 `.config/hermes-mobile-access/.env` 读取；0 关闭，其余值开启。检查失败或无 Git 信息不能称为最新。固定解析 annotated tag 到实际 commit，向用户报告本地/远端版本和 commit；同意后才按该 SHA 下载整个 Skill。

在同一文件系统的已忽略任务目录准备副本，核对名称、版本、描述前缀、来源 commit 和完整包。通过后替换并保留恢复目录，再回读目标；不修改用户连接、凭证、Hermes 或 FRP。当前源码未发布时报告尚无可取得版本，不把 main 或多 Skill 仓库的 latest Release 当成本 Skill 的稳定版。

没有固定 SHA 下载和可恢复替换能力时报告 not-applicable，不臆造宿主 API。查询状态区分 latest、update-available、unknown-network、auth-required、invalid-metadata；替换状态区分 applied、apply-failed，均如实保留当前可用业务状态。
