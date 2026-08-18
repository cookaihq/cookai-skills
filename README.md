# awesome-skills

cookaihq 维护的 Agent Skill 集合 —— 每个 skill 是一份遵循 [agentskills.io](https://agentskills.io) 开放标准的 `SKILL.md` + 配套脚本，可在 Claude Code 等 Agent 平台即装即用。

## Skills

| Skill | 功能 | 文档 |
|---|---|---|
| [feishu-use](feishu-use/) | 安全调用飞书官方 `lark-cli` 的通用入口：检查安装与最新版、确认更新选择、核对用户/应用身份和目标账户、引导链接 + 二维码登录、增量授权后再执行 Base / 文档 / 日历等操作 | [README](feishu-use/README.md) · [SKILL.md](feishu-use/SKILL.md) |
| [image-2](image-2/) | `gpt-image-2` 生成图片：文生图 / 图生图、11 种预设比例 + 自定义分辨率（最高 4K）、任务完成自动下载到工作区 | [README](image-2/README.md) · [SKILL.md](image-2/SKILL.md) |
| [banana-2](banana-2/) | Nano Banana 2（`gemini-3.1-flash-image-preview`）生成 / 编辑图片：文生图 / 图生图 / 图像编辑、15 种宽高比 + `512`~`4K` 画质档、可选联网搜索 / 图片搜索增强、任务完成自动下载到工作区 | [README](banana-2/README.md) · [SKILL.md](banana-2/SKILL.md) |
| [memoji-sticker-pack](memoji-sticker-pack/) | 从一张人物照片生成一套 Apple Memoji 风格（拟我表情）表情贴纸包：先出基准头像锁定长相，再并发生成 N 个表情（默认 16），绿幕出图 + 自动抠成透明底 PNG，产出画廊 `index.html`（调用 `image-2` 生成，内置参考图上传） | [README](memoji-sticker-pack/README.md) · [SKILL.md](memoji-sticker-pack/SKILL.md) |
| [multimodal-ask](multimodal-ask/) | 指定模型做多模态理解与文本生成：分析 / 转写音视频、让指定模型读取本地或远程图片 / PDF、对混合媒体一次性推理（异步） | [README](multimodal-ask/README.md) · [SKILL.md](multimodal-ask/SKILL.md) |
| [pdf2md_docx](pdf2md_docx/) | PDF 转 Markdown / LaTeX / DOCX：公式识别、跨页表格合并，返回 ZIP 自动解压到带时间戳的目录 | [README](pdf2md_docx/README.md) · [SKILL.md](pdf2md_docx/SKILL.md) |
| [pdf2markdown](pdf2markdown/) | 把一个 PDF（本地文件或公开 HTTPS 链接）转成可复核的 Markdown 工作目录：preflight 门禁、一次一个 Doc2X 转换任务（创建 + 轮询 + 原子采纳结果 ZIP 为不可变原始产物）、逐处证据绑定的人工复核与修正、原始产物全程保留，中断后可从任意阶段续跑；付费重试必须显式确认 | [README](pdf2markdown/README.md) · [SKILL.md](pdf2markdown/SKILL.md) |
| [s3-upload](s3-upload/) | 把本地文件持久写入用户自己的 S3 兼容 bucket（AWS S3 / Cloudflare R2 / custom，OSS / COS experimental）：严格 JSON Object Reference + public/presigned URL、`--collision reject` 原子 no-overwrite 与同内容采纳、17 键闭合 result 合同与 `--result-out` 跨进程 handoff、checkpoint 只读对账恢复 | [README](s3-upload/README.md) · [SKILL.md](s3-upload/SKILL.md) |
| [preview-share](preview-share/) | 把本地 HTML 页面（或任意本地文件）传到线上预览并拿到可分享 URL，自动识别并一并上传相对路径引用的图片 / CSS / JS | [README](preview-share/README.md) · [SKILL.md](preview-share/SKILL.md) |
| [template-preview](template-preview/) | 把「一批图片 + 文案」的文件夹渲染成模仿知名 App UI 的展示页（v1 内置小红书个人主页模板），产出自包含的 `index.html` + assets，可交给 preview-share 上传 | [README](template-preview/README.md) · [SKILL.md](template-preview/SKILL.md) |
| [exit-ip](exit-ip/) | 查询运行本 Agent / Claude Agent SDK 环境的出口（公网）IP 与归属地 / 运营商，直接读取 `ipinfo.io` | [SKILL.md](exit-ip/SKILL.md) |
| [frpc-launch](frpc-launch/) | 本地一键启动/管理 frpc 内网穿透：双模式（自部署 FRPS / 宝塔 frps 用官方 frpc，樱花FRP 用其定制客户端）、无配置时引导配置、二进制自动下载校验、后台常驻 + status/stop/logs、凭证全程掩码 | [README](frpc-launch/README.md) · [SKILL.md](frpc-launch/SKILL.md) |

## 安装

以 `image-2` 为例，四种方式任选其一（其他 skill 如 `banana-2` 把命令里的 `image-2` 换成对应目录名即可）。

让 Agent 自己装（推荐，最省事 —— 把下面这段提示词丢给 Claude Code 或 Codex）：

```text
请把 https://github.com/cookaihq/cookai-skills 仓库里的 image-2 skill 安装给你自己用：
1. 克隆仓库到本地（已克隆则更新）
2. 把其中的 image-2 目录链接或复制到你当前平台的 skills 目录
   （Claude Code 用 ~/.claude/skills/image-2，其他平台用各自约定的位置）
3. 读 image-2/SKILL.md 确认能被识别，然后告诉我怎么触发它
```

如果你非常清楚 Skills 的安装方式，可以使用以下方法。

项目级 symlink（只在当前项目生效）：

```bash
git clone https://github.com/cookaihq/cookai-skills.git
mkdir -p ./.claude/skills
ln -s "$(pwd)/cookai-skills/image-2" ./.claude/skills/image-2
```

用户级 symlink（所有项目可触发）：

```bash
ln -s "$(pwd)/cookai-skills/image-2" ~/.claude/skills/image-2
```

skills CLI：

```bash
npx skills add cookaihq/cookai-skills --skill image-2 -a claude-code
```

> Codex / Gemini 用户把 `-a claude-code` 换成对应平台标识。

## 运行环境

含 Python 脚本的 skill 统一用 [uv](https://docs.astral.sh/uv/)（>= 0.8）管理运行时：每个 skill 自带 `pyproject.toml` + `uv.lock`，调用示例一律 `uv run --project <skill目录> ...`，首次运行会自动在 `<skill>/.venv` 建好环境；入口脚本内置兜底，即使误用系统 `python3` 直跑也会自动切回正确环境。未安装 uv 时按报错提示执行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 即可。

各 skill 的详细用法、配置与排错见其目录下的 README。

## License

[MIT](LICENSE)
