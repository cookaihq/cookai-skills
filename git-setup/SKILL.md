---
name: git-setup
metadata:
  version: "0.1.0"
description: v0.1.0｜为新项目初始化 Git、接入远端并配置多个仓库的单向同步；按项目规则选择本地或 GitHub Issue 保存位置，识别外仓与子仓的独立配置及继承关系，并核验写入和中断恢复结果。
compatibility: Requires Node.js >=20 and Git >=2.29. Shared instructions for Claude Code, Codex and WorkBuddy; gh is optional for GitHub authentication.
---

# git-setup

帮助用户设置项目的 Git，并让以后创建 Issue 的会话使用同一项目配置。根据本次请求只执行需要的分支。用户已经授权的操作继续执行；缺少目标、身份或必要内容时先查询现有项目，再补问缺项。

## 0. 自动检查更新

Git 检出安装：macOS/Linux 执行本 Skill 的 `scripts/check_update.sh`；Windows 原生执行 `scripts/check_update.ps1`。退出 0 继续业务，不复述输出；退出 10 原样转述报告，用户确认后才分别用 `--pull` / `-Pull`。拒绝、未回复、检查或更新失败都按当前版本继续。

复制安装：读取 `update.json`，按 `git-setup/v` 稳定 tag 查询版本及确切 commit SHA；展示本地/远端版本、来源、tag、SHA、安装目录，经确认才下载该 SHA 到同一文件系统临时目录，校验 Skill 名、版本、description 后原子替换并回读。无法核实宿主安装器或 WorkBuddy 更新 API 时报告 `not-applicable` 并继续，不编造更新接口。关闭检查只修改个人目录 `.config/git-setup/.env` 的 `AUTO_UPDATE_CHECK=0`，保留其他字段。

## 1. 入口与初检

先定位此 `SKILL.md` 的真实目录。以下 `<skill>` 均指该目录，不能按使用者项目目录寻找脚本。

```text
node <skill>/scripts/git-setup/cli.mjs help
node <skill>/scripts/git-setup/cli.mjs inspect --project <用户项目>
```

入口从 `package.json` 校验 Node 版本，Git 模块真实执行 Git 并要求版本不低于 2.29。系统程序缺失时给安装办法，按已有授权处理安装；没有 Python、npm 包或本地模型依赖。三宿主运行同一份脚本。

读取项目适用的 Agent 规则、Git 配置、已有 Issue 工具和模板。区分无 Git、空仓库、有历史、父仓目录、Worktree、子模块；嵌套目录或多 remote 不证明外仓关系。不要给父仓的普通目录自动嵌套初始化。新分支/Worktree按使用者项目规则和本次授权决定。

所有业务命令输出 JSON。写入命令要求 `--apply`；这只是执行开关，Agent 应依据用户本次具体请求传入，不把它变成重复确认。状态不明、部分完成和检查未完成不能报告成功。配置来源使用 `config-check` / `auth-info`，后者仅输出字段是否填写及来源。

## 2. 设置 Git、发布与同步

进入此分支时读取 [Git 操作](references/git.md) 和 [配置规则](references/configuration.md)。

1. 明确本次是初始化、仅增加远端、发布已有提交或配置同步。分别核对提交作者与推送身份；作者正确不证明访问账号正确。沿用项目明确作者，否则展示生效作者并取得选择。暂存区和其他未提交文件保持现状。
2. 无 Git 时明确初始分支和作者，调用 `init`。不制造占位文件或空提交。需要首次提交时，检查忽略规则、用户批准的文件范围与暂存区，使用 Git 的显式文件清单提交；不以“设置 Git”为理由自动 `git add -A`。空目录仅完成初始化并报告尚无提交。
3. 目标已存在时核对完整地址，调用 `remote-add`；不替换已有同名远端。创建 GitHub 仓库另读 [GitHub](references/github.md)，所有者、名称、可见性明确且用户委托创建后才调用 `repo-create`。CNB 等平台首版接入已有 Git 仓库，不调用其仓库管理 API。
4. 将非敏感默认设置写入 `.git-setup/project.json`。配置可包含 remote 映射和同步的 source、targets、完整 refs；`config-write`只写共享文件，不自动配置 remote、提交或同步。实际 Git config 用 `remote-add` 独立设置并回读。只在明确项目设置范围内为 `data/git-setup/` 添加忽略规则；已被跟踪的秘密不能自动移出索引。
5. 首次发布前检查将提交文件及历史。`audit --ref refs/heads/<分支>` 会扫描所有可达 blob 的已知秘密模式及大文件；`publish`内置同一检查，检查发现或预算不完整会停止。发现只报告对象 SHA 和类别，不输出秘密。此检查不保证识别所有秘密。修复历史需单独明确处理决定，不能自行重写。
6. `publish --remote <主要远端> --ref <完整引用> --apply` 发布指定本地引用；只有已批准提交可发布。GitHub token/gh 路径用 `--github-auth`，仅向绑定的 HTTPS 仓库临时注入凭证，其他地址保持其已验证的 Git/SSH 鉴权。参考配置文档，绝不把 Token 放进 URL、argv 或 Git config。
7. `sync` 读取共享配置，先不带 `--apply` 比较来源和各目标；实际同步加 `--apply`，脚本在读取来源对象后、任何目标写入前执行同一历史检查。来源是主要远端已经发布的引用，未发布本地分支/标签不加入。目标独有引用保留，不 force、mirror、prune；分叉、标签冲突、保护分支按实际结果报告，必要时沿项目既有 PR 流程处理。
8. 检查每个目标、每个引用的实际 SHA 和来源是否变化。LFS、子模块内容未传输时报告引用与内容范围，不能宣称完整项目同步。推送验证与 CI 结果分开；没有查询 CI 时明确未验证 CI。复跑先比较真实状态再补缺项。

运行恢复不安装后台服务、hooks 或 CI。用户要求网页合并自动同步或双向合并时，说明首版边界并另行确定方案。

## 3. 设置 Issue 默认规则及后续会话入口

读取 [配置规则](references/configuration.md)。先确定记录方式（local/github），再确定 current/outer/specified 和该方式必需字段。沿用项目已有目录、编号、模板、状态约定；复杂已有工具优先接入它，必须把解析出的最终目标显式传给该工具。

项目默认设置随代码共享；凭证和个人覆盖留在本机，不自动迁移、复制或保存秘密。已登记独立设置的子仓使用自身 Issue 配置，缺失/损坏就报错；未独立配置才继承外仓。项目硬性限制继续适用。发起仓库、配置所属仓库和目标仓库分别记录，读取外仓配置不改变“当前仓库”的含义。

设置请求覆盖后续会话接入时，先核对当前项目各宿主实际加载的规则文件，使用 `install-rules --rule-file <实际文件>` 预览目标列表，然后在已有设置授权内加 `--apply`。只追加/替换本工具标记块，保留其他内容，符号链接实体去重。非子模块的外仓关系通过明确登记及 `--workspace <外仓>` 接入。不要因为某目录存在就声称该宿主已加载规则。用全新 Claude Code、Codex、WorkBuddy 会话分别验证普通自然语言 Issue 请求。

## 4. 创建、更新与恢复 Issue

1. 区分新建、更新和回复。“参考 #123 新建”不修改 #123；明确更新/回复原事项才沿原链接或本地路径执行。只有 GitHub.com 在线 Issue 受支持，其他平台不改投本地或 GitHub。
2. 新建前调用 `issue-resolve --project <发起项目>`，需要已登记外仓时加 `--workspace`。没有配置但项目规则明确时，Agent 将实际规则转成完整 JSON 文件，经 `--project-rule` 传入。用户本次明确覆盖才传 `--override`；仅改目标时先保留原记录方式再形成完整记录，不改持久默认值。缺项或歧义先取得必要输入。
3. 检查结果中的来源、backend、目标、目录与模板。GitHub 凭证必须匹配最终 host/repository，不借用发起子仓凭证。创建正文遵从最终目标模板，保留实际涉及的仓库、路径、提交、事实及预期；不编造复现，标签/负责人只用已有明确值。
4. 为这一次创建生成并记录稳定 `--operation-id`（8–80位字母/数字/连字符/下划线），准备 UTF-8 正文文件。调用 `issue-create --title <标题> --body-file <文件> --operation-id <标识> --expected-fingerprint <第2步返回的fingerprint> --apply`，保留解析时的所有选择参数。脚本先验证此前解析结果的 fingerprint，创建前再次解析配置并验证 fingerprint，再执行本地独占写入或一次 GitHub 请求。无可用命名模板时按项目规则明确 `--filename`，不自定编号。
5. 成功后回读。本地回复必须写“已按当前项目配置将 Issue 保存到本地”并给实际路径；GitHub返回真实 Issue URL。只保存本地文件不表示已 commit/push。
6. 写入结果不明或换会话继续时，用同一选择参数和 `issue-recover --operation-id <原标识>` 查证 `data/git-setup/` 中操作记录及实际事项。GitHub恢复使用专属标记及内容校验，不能凭同标题认领；查询为空仍不能自动重新创建。不改配置绕过恢复失败，不换 operation-id 重发同一未知请求。
7. 明确更新 GitHub Issue 用 `issue-update --issue-url <原URL>`；回复用 `issue-comment --issue-url <原URL>`。本地已有事项通过项目原工具编辑，先读原文再改指定内容并回读；不进入新建默认选择。评论结果不明保留错误并核对原事项，不盲重发。

运行日志只保存无凭证的操作元数据和摘要。首次缺少运行目录忽略规则会明确报错，普通创建请求不自动改项目设置。认证错误按实际来源修复；首次凭证配置沿用既定管理方式，缺项时提供自行填写/secret-book选择，手填不依赖secret-book。配置修复不授权重发结果不明的请求。

## 完成条件

报告实际改动文件、配置来源、远端/本地事项位置、验证通过范围及未完成项。脚本通过不等于线上鉴权、真实 GitHub 写入或三宿主调用通过。当前分发与验证状态见 [README](README.md)，使用者项目中的能力须从其实际入口验证。
