# 项目 Git 使用说明

默认新增 docs/agent/git.md；已有项目规定路径则复用原文档。Claude Code、Codex、WorkBuddy 实际规则入口均引用同一正文，保留已有 symlink。引用应明确触发条件：“执行本项目 Git 操作前，必须阅读并遵循[项目 Git 使用说明](相对路径)。”

## 交付内容

Agent 根据当前项目填写下列内容，不把模板占位符当成完成结果：

1. 当前仓、主检出与 Worktree 的关系；子模块或独立仓的登记依据；每次明确选择操作检出。
2. remote 名称、无凭证完整 URL、允许的引用与同步方向；分支、提交身份和创建/提交/推送的授权规则。
3. 每个完整目标默认复用的项目 Token 来源及主检出内实际相对路径；全局 gh 仅在用户明确选择（含可核验的同范围既有选择）时使用指定账号，发现登录只作信息提示，不要求重新二选一或推荐 gh。存在 sources.local.md 时写明本机记录位置与读取规则，不将个人绝对路径写进共享正文。
4. CNB 单链接文件格式、GitHub Token/gh 的区别、未跟踪且实际忽略、Unix 权限/Windows ACL、禁止回显。不要把示例中的 Token 替换成真实值。
5. 按本次进程环境传递凭证的具体操作方法：读取当前值；CNB URL 必须含固定 cnb 用户和相同完整仓库；清空 helper 链，启用 useHttpPath，用仅响应 get 的短期 helper 精确匹配本次 URL，store/erase 不写；关提示与重定向。helper 忽略 `capability[]` / `wwwauth[]` 等合法多值元数据，仅对身份字段检查重复与匹配。环境结束即失效。来源缺失或鉴权失败停止，不能借用其他来源。完整契约从 [鉴权文档](authentication.md) 合并到项目正文，不能只引用本 Skill 安装路径。
6. 本次需要的 read/publish/sync 和 GitHub Issue 流程沿用相同来源规则；项目 Token 缺失、失效或权限不足时报告原来源并按用户明确决定处理，不回退全局 gh。说明推送前检查、逐 ref 结果回读、写入不明时只读核对的恢复方式。已有 PR/Release 脚本引用真实项目路径、输入与结果，不自动更换。
7. 普通终端、IDE、CI 使用既有鉴权；需要它们接入自定义文件时另有明确配置需求，不能承诺设置后自动生效。

## 可选文档写入工具

node <skill>/scripts/git-setup/cli.mjs document-plan --project <根> --input -

输入 {path:"docs/agent/git.md",content:"经当前项目核对的完整说明",ruleFiles:["AGENTS.md","CLAUDE.md", "<已核实的WorkBuddy规则入口>"]}。实际入口按客户端检查结果给出，不凭示例创建无效文件。document-write 使用同一输入、--apply 和 --expected-fingerprint。

工具保留规则原文，在 git-setup:documentation 标记内替换引用；指向同一实体的规则只编辑一次。写入前核对原文和链接，写后回读；部分失败报告已完成文件，不回滚覆盖后续用户内容。不保存例行事务、生成脚本、manifest 或运行包。脚本不理解正文语义，Agent 必须审阅内容和所有引用。

项目已有 docs-anchor 等映射时，沿其正式规则维护真实文档和现有引用。该工具拒绝经任意 symlink 路径写入，不可为绕过检查另建平行目录。

## 1.0.0 迁移：调用方迁移后删除旧实现

不再支持 0.4.x/0.5.x 的 harness-plan/apply/recover、auth-prepare/verify/cleanup、runtime-info、生成 run.mjs、gitsetup.auth-*.binding 或指纹运行包。没有转发别名、旧格式双读和回退。未发布的 CNB 多行方案也已撤回。

升级已有项目时先只读清点：实际配置中的 helper 引用及 reset 项、本机绑定键、规则中的旧入口引用、安装目录、所有脚本/Hook/CI 调用方和用户维护文件。旧安装检查只报告路径/键名，不输出鉴权值。按当前授权迁移调用方到项目文档或明确业务脚本，保留所需发布/同步/PR/Release 行为；验证之后，才删除确认属于旧实现的入口、绑定键及无人引用运行包。仅删除旧 helper 对应的精确配置项；没有证据不能删除其他 helper、全局配置或所有 .git/git-setup。

真实凭证不随源码升级转换、复制或清除。没有已授权真实项目迁移时只说明现状与待迁移对象；调用方无法迁移就报告具体阻塞，不静默保留兼容层。
