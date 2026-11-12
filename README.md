# awesome-skills

cookaihq 维护的 Agent Skill 集合 —— 每个 skill 是一份遵循 [agentskills.io](https://agentskills.io) 开放标准的 `SKILL.md` + 配套脚本，可在 Claude Code 等 Agent 平台即装即用。

## Skills

| Skill | 功能 | 文档 |
|---|---|---|
| [agent-instructions-update](agent-instructions-update/) | 检测五种 Agent 的规则入口，通过专用工作台审阅、编辑、同步和恢复全局及项目规则 | [README](agent-instructions-update/README.md) · [SKILL.md](agent-instructions-update/SKILL.md) |
| [as](as/) | 显式调用，在 Orca 当前目录和分支按默认值或跨 Agent 预设新建会话，可自动发送首条消息 | [README](as/README.md) · [SKILL.md](as/SKILL.md) |
| [bilibili-charge-download](bilibili-charge-download/) | 使用用户已登录且有完整播放及保存权限的 B 站账号下载充电专属视频，将 DASH 音视频封装并完整性验证为 MP4；不读取 Cookie，不绕过付费或访问限制 | [README](bilibili-charge-download/README.md) · [SKILL.md](bilibili-charge-download/SKILL.md) |
| [git-setup](git-setup/) | 仅提供 URL 即按当前项目状态接入远端；支持明确请求的发布、同步与本地/GitHub Issue | [README](git-setup/README.md) · [SKILL.md](git-setup/SKILL.md) |
| [video-to-markdown](video-to-markdown/) | 把本地或可直接下载的视频转成可复核、可续跑的 Markdown 文档：结合带时间戳的语音转写、关键帧截图、短 GIF、自适应补采样和全时间线覆盖检查，保留语音与画面共同承载的内容 | [README](video-to-markdown/README.md) · [SKILL.md](video-to-markdown/SKILL.md) |
| [pdf2markdown](pdf2markdown/) | 通过 AIHub Doc2X 转换本地 PDF 或公开 HTTPS PDF，由 Agent 逐页核验、修订和复核，在本地交付 Markdown、图片、核验报告与三栏预览；支持同任务恢复和已有 ZIP 重新核验 | [README](pdf2markdown/README.md) · [SKILL.md](pdf2markdown/SKILL.md) |
| [pdf-to-markdown-vision](pdf-to-markdown-vision/) | 使用宿主原生视觉将 PDF 转为忠实 Markdown 与真实图片，保留表格条件和公式，支持清洗、续跑及结果追溯 | [README](pdf-to-markdown-vision/README.md) · [SKILL.md](pdf-to-markdown-vision/SKILL.md) |
| [s3-upload](s3-upload/) | 把本地文件持久写入用户自己的 S3 兼容 bucket（AWS S3 / Cloudflare R2 / custom，OSS / COS experimental）：严格 JSON Object Reference + public/presigned URL、`--collision reject` 原子 no-overwrite 与同内容采纳、17 键闭合 result 合同与 `--result-out` 跨进程 handoff、checkpoint 只读对账恢复 | [README](s3-upload/README.md) · [SKILL.md](s3-upload/SKILL.md) |
| [exit-ip](exit-ip/) | 查询运行本 Agent / Claude Agent SDK 环境的出口（公网）IP 与归属地 / 运营商，直接读取 `ipinfo.io` | [SKILL.md](exit-ip/SKILL.md) |
| [frpc-launch](frpc-launch/) | 使用已有自部署/宝塔或樱花 FRP；按项目复用连接、固定域名并隔离代理、进程和日志，支持检查与独立停止 | [README](frpc-launch/README.md) · [SKILL.md](frpc-launch/SKILL.md) |
| [hermes-mobile-access](hermes-mobile-access/) | 复用 Hermes Desktop 和已有 FRP，为 Android 手机配置 HTTPS 接入、独立密钥与首选 profile，检查连接和恢复状态 | [README](hermes-mobile-access/README.md) · [SKILL.md](hermes-mobile-access/SKILL.md) |
| [redskill-download](redskill-download/) | 下载小红书 RedSkill 分享的技能发布包，保存原始 ZIP、解压文件和校验记录，检查引用缺件；下载目录可配置 | [README](redskill-download/README.md) · [SKILL.md](redskill-download/SKILL.md) |
| [skill-configuration-and-artifacts-ui](skill-configuration-and-artifacts-ui/) | 为新建或已有 Skill / Plugin 评估、预览和接入配置中心与产物集合；携带共用规则、运行代码和改造工具 | [README](skill-configuration-and-artifacts-ui/README.md) · [SKILL.md](skill-configuration-and-artifacts-ui/SKILL.md) |

## 版本与 Release

各目标的当前版本与对应 Release 地址（表格由 `harness/repo-harness/release-on-push.sh --table` 生成，升版本的提交须同步更新，约定见外层仓 `docs/adr/0009`）：

表格随源码版本生成；对应版本完成发布后 Release 链接才可用。

<!-- release-table:begin -->
| 目标 | 版本 | Release |
|---|---|---|
| agent-instructions-update | 0.1.1 | [agent-instructions-update/v0.1.1](https://github.com/cookaihq/cookai-skills/releases/tag/agent-instructions-update%2Fv0.1.1) |
| as | 1.1.0 | [as/v1.1.0](https://github.com/cookaihq/cookai-skills/releases/tag/as%2Fv1.1.0) |
| bilibili-charge-download | 1.0.0 | [bilibili-charge-download/v1.0.0](https://github.com/cookaihq/cookai-skills/releases/tag/bilibili-charge-download%2Fv1.0.0) |
| exit-ip | 1.1.0 | [exit-ip/v1.1.0](https://github.com/cookaihq/cookai-skills/releases/tag/exit-ip%2Fv1.1.0) |
| frpc-launch | 1.6.0 | [frpc-launch/v1.6.0](https://github.com/cookaihq/cookai-skills/releases/tag/frpc-launch%2Fv1.6.0) |
| git-setup | 1.0.2 | [git-setup/v1.0.2](https://github.com/cookaihq/cookai-skills/releases/tag/git-setup%2Fv1.0.2) |
| hermes-mobile-access | 0.2.0 | [hermes-mobile-access/v0.2.0](https://github.com/cookaihq/cookai-skills/releases/tag/hermes-mobile-access%2Fv0.2.0) |
| pdf-to-markdown-vision | 1.0.1 | [pdf-to-markdown-vision/v1.0.1](https://github.com/cookaihq/cookai-skills/releases/tag/pdf-to-markdown-vision%2Fv1.0.1) |
| pdf2markdown | 2.0.0 | [pdf2markdown/v2.0.0](https://github.com/cookaihq/cookai-skills/releases/tag/pdf2markdown%2Fv2.0.0) |
| redskill-download | 1.0.0 | [redskill-download/v1.0.0](https://github.com/cookaihq/cookai-skills/releases/tag/redskill-download%2Fv1.0.0) |
| s3-upload | 1.3.0 | [s3-upload/v1.3.0](https://github.com/cookaihq/cookai-skills/releases/tag/s3-upload%2Fv1.3.0) |
| skill-configuration-and-artifacts-ui | 1.2.0 | [skill-configuration-and-artifacts-ui/v1.2.0](https://github.com/cookaihq/cookai-skills/releases/tag/skill-configuration-and-artifacts-ui%2Fv1.2.0) |
| video-to-markdown | 3.0.0 | [video-to-markdown/v3.0.0](https://github.com/cookaihq/cookai-skills/releases/tag/video-to-markdown%2Fv3.0.0) |
<!-- release-table:end -->

Hermes 手机接入 0.1.0 与 FRP Launch 1.6.0 的已验范围及待验事项见各自 README；Windows/Android 基础文字收发已验证，macOS 等其余场景按说明继续验收。

## 安装

以 `frpc-launch` 为例，四种方式任选其一（其他 skill 如 `bilibili-charge-download` 把命令里的 `frpc-launch` 换成对应目录名即可）。

让 Agent 自己装（推荐，最省事 —— 把下面这段提示词丢给 Claude Code 或 Codex）：

```text
请把 https://github.com/cookaihq/cookai-skills 仓库里的 frpc-launch skill 安装给你自己用：
1. 克隆仓库到本地（已克隆则更新）
2. 把其中的 frpc-launch 目录链接或复制到你当前平台的 skills 目录
   （Claude Code 用 ~/.claude/skills/frpc-launch，其他平台用各自约定的位置）
3. 读 frpc-launch/SKILL.md 确认能被识别，然后告诉我怎么触发它
```

如果你非常清楚 Skills 的安装方式，可以使用以下方法。

项目级 symlink（只在当前项目生效）：

```bash
git clone https://github.com/cookaihq/cookai-skills.git
mkdir -p ./.claude/skills
ln -s "$(pwd)/cookai-skills/frpc-launch" ./.claude/skills/frpc-launch
```

用户级 symlink（所有项目可触发）：

```bash
ln -s "$(pwd)/cookai-skills/frpc-launch" ~/.claude/skills/frpc-launch
```

skills CLI：

```bash
npx skills add cookaihq/cookai-skills --skill frpc-launch -a claude-code
```

> Codex / Gemini 用户把 `-a claude-code` 换成对应平台标识。

## 运行环境

含 Python 脚本的 skill 统一用 [uv](https://docs.astral.sh/uv/)（>= 0.8）管理运行时：每个 skill 自带 `pyproject.toml` + `uv.lock`，调用示例一律 `uv run --project <skill目录> ...`，首次运行会自动在 `<skill>/.venv` 建好环境；入口脚本内置兜底，即使误用系统 `python3` 直跑也会自动切回正确环境。未安装 uv 时按报错提示执行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 即可。

各 skill 的详细用法、配置与排错见其目录下的 README。

## 自动检查更新

本仓 Skill 自带 `scripts/check_update.sh`，按各自 `SKILL.md` 在运行前检查更新：Git 安装按 skill 所在的检出比对 `origin/main`，落后就把版本变化和提交摘要打印出来，由 Agent 转述给你并询问是否拉取；只有你同意后才会执行 `check_update.sh --pull`（仅当分支为 `main`、跟踪文件无未提交改动、且可 fast-forward 时执行 `git merge --ff-only`，脚本从不 commit / push / reset / checkout）。`redskill-download` 在 Windows 原生使用随包的 `check_update.ps1`；复制安装的检查和更新方式见其 README。

它只做检查和提醒，不会自动改你本机的文件。检查每 6 小时最多一次，`git fetch` 单次尝试、5 秒超时，网络失败、非 git 检出（如手工拷贝安装）一律静默跳过——更新检查任何情况下都不会挡住 skill 干正事。

关掉某个 skill 的检查：在 `~/.config/<skill 目录名>/.env` 里写一行 `AUTO_UPDATE_CHECK=0`（值要裸写，行尾不要跟注释）。重新开启：改成 `AUTO_UPDATE_CHECK=1` 或删掉这一行。这个开关只从该文件读，按 skill 各自隔离。

## License

[MIT](LICENSE)
