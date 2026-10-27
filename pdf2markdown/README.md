# pdf2markdown

把一份 PDF 转为可继续处理的 Markdown：AIHub Doc2X 完成云端转换，Claude Code、Codex 或 WorkBuddy 对照全部页图核验内容、修订错误，最后在本地交付 Markdown、图片、核验报告和三栏对照预览。

适合需要保留原稿、检查表格和公式、追踪修订或中途续跑的文档处理。每次处理一份 PDF；不自动删除品牌、公司介绍、联系方式、二维码或页眉页脚，也不接入飞书任务表、OSS 发布和通知。

## 让 Agent 帮你安装

在 Claude Code、Codex 或 WorkBuddy 中复制这段话：

> 请安装 cookaihq/cookai-skills 仓库中的独立 Skill pdf2markdown。优先从 https://github.com/cookaihq/cookai-skills 安装，遇到网络故障时自动尝试同项目的 https://cnb.cool/zhidateam/tannt/tannt-skills，仓库内目录都是 pdf2markdown；保留已有设置，并验证当前 Agent 能发现和调用它。

本页对应 **2.0.0**，固定版本使用标签 `pdf2markdown/v2.0.0`。从 1.x 升级前先阅读下方迁移说明；已有任务继续使用原版本完成，或提供已有 Doc2X ZIP 在新版重新核验。

三宿主共用一份 Skill。Claude Code 的本地入口是 `~/.claude/skills/pdf2markdown`，Codex 是 `~/.agents/skills/pdf2markdown`，两者可按技能链接到检出目录；WorkBuddy 本地安装使用 `~/.workbuddy/skills/pdf2markdown`，从同一来源生成安装副本，不要求符号链接。Codex 可用 `$pdf2markdown` 显式调用，Claude Code 可用 `/pdf2markdown`，WorkBuddy 可在新会话点名使用 `pdf2markdown`。自然语言请求也属于预定触发范围。

Agent 需要能够读取本地 PNG。运行工具为 uv（0.8 或更高）和 Pandoc **3.10.1**；Python 与依赖按随包锁文件准备，不下载本地模型。Pandoc 缺失或版本不符时，Agent 会说明需要安装的版本与位置，确认后再安装系统工具。

本次验证状态：macOS 的 110 项自动测试、本地合成文档核验闭环和浏览器三栏预览已通过。Claude Code 技能列表与 Codex 原生加载接口能发现 2.0.0；各客户端的完整业务流程、WorkBuddy 发现、真实 Doc2X 请求和 Windows 原生环境仍待验收。Windows 的运行时路径、锁及文件替换适配已加入，不能据此视为实机通过。

## 完成首次配置

需要 AIHub 账号的 `AIHUB_API_KEY`，用于上传 PDF、创建 Doc2X 转换任务及查询结果。接口固定为 `https://api.aihubmax.com`，无需飞书账号或 OSS 配置。已有同一 PDF 的 Doc2X ZIP 时，可以跳过上传和转换，无需 Key，仍进行逐页核验。

> 请帮我配置 pdf2markdown，沿用已有选择，或让我选择 secret-book / 自填 .env。首次新增默认保存在个人全局，已有错误修复实际生效的原文件；选择 secret-book 时按它的流程取值保存。告诉我写入位置和实际来源，隐藏密钥值。

手填时由 Agent 定位文件，你在本机填写，不必把完整密钥发到聊天里。新增全局配置通常保存在下表位置，须经你授权才写入。选择项目保存时使用工作文件夹的 `.env.local`，先检查文件未被 Git 跟踪且已忽略；不会同时复制到两处。

| 运行环境 | 全局配置文件 |
| --- | --- |
| macOS / Linux | 当前用户个人文件夹下的 `.config/pdf2markdown/.env`，如 `/Users/你的用户名/.config/pdf2markdown/.env` |
| Windows 原生 | Python `Path.home()` 指向的用户文件夹下 `.config\pdf2markdown\.env`，通常为 `C:\Users\你的用户名\.config\pdf2markdown\.env`；由 Agent 在实际环境确认 |
| WSL | WSL 用户个人文件夹下 `.config/pdf2markdown/.env`，如 `/home/你的用户名/.config/pdf2markdown/.env`，与 Windows 配置分开 |

读取顺序为：运行程序时的环境变量 → 工作文件夹中的 `.env.pdf2markdown` → `.env.local` → `.env` → **明确启用的**个人全局 `.env`。每项独立取首个非空值；不向父目录寻找，不扫描其他 Skill 的文件。工作文件夹是发起转换的项目目录，和 Skill 安装目录不同。

使用全局配置时，对 Agent 说“本次使用 pdf2markdown 的个人全局配置”，Agent 会在本次及续跑命令中显式启用。全局默认保存不改变上述优先级。任务保存所用凭证的来源和哈希，恢复时继续使用同一来源；密钥变更时会停下说明，不换账号继续请求。

可选的 `PDF2MARKDOWN_OUTPUT_DIR` 按相同顺序设置输出根目录；本次明确指定的目录优先。诊断请求：

> 请检查 pdf2markdown 的实际工作文件夹、AIHUB_API_KEY 是否已填写及来自哪里；不要显示密钥，也不要为检查配置创建转换任务。

保存成功只说明本机配置已写入，不等于线上鉴权或转换已成功。secret-book 可用于配置，不是日常转换的依赖；表中改值不会自动同步本机。

## 第一次使用

PDF 会上传到 AIHub，转换可能消耗账号额度；页图核验由宿主 Agent 按其数据处理方式执行。结果保存在本地，不代表 PDF 内容全程不离开电脑。请求转换后，流程不增加每次计费前的额外询问。

将 PDF 提供给 Agent，继续对话：

> 使用 pdf2markdown 转换这份 PDF，使用我已配置的个人全局 Key，对照每一页核验文字、表格、公式和图片。结果放到我指定的“文档转换结果”文件夹，保留原稿，并说明无法确认的内容。

支持本地 PDF 和无需登录、Cookie 的公开 HTTPS PDF 链接。Agent 返回独立任务目录，以及其中 `result/` 下的：

| 文件 | 用途 |
| --- | --- |
| `source.pdf` | 原 PDF 副本，输入文件不被修改 |
| `direct.md` | 直转稿，图片引用改为本地相对路径 |
| `final.md` | 应用有原文证据的修订后的 Markdown |
| `images/` | 两份 Markdown 所需图片 |
| `index.html` | 同时查看 PDF、直转稿、最终稿；可隐藏或重新展开各栏 |
| `review.json` | 每轮核验、修订及未解决问题 |
| `manifest.json` | 文件清单与哈希 |

没有指定目录时，结果根目录为工作文件夹下的 `pdf2markdown-output/`；每份 PDF 使用一个 `document-*` 任务目录。整个任务目录保留来源、页图、原始 ZIP 和恢复状态，不自动删除。分享时复制整个 `result/`，不要只拿走 Markdown，也不要分享含临时 URL 的 `.state/`。

预览无需 CDN；浏览器限制本地 PDF 嵌入时，可使用预览中的 PDF 下载链接。文件已经生成与内容全部核实是两种结果：有不可辨认内容、疑似漏图、未解决的页级问题或终轮再次修订时，Agent 会明确列出仍需核实的事项。

## 常用请求

- “继续上次的 pdf2markdown 任务，目录是……”：从原检查点恢复，同一个转换任务继续查询；核验过的页不从头再做。
- “用这份 PDF 和它已有的 Doc2X ZIP 重新核验，不再次转换”：跳过上传与计费创建。程序不能独立证明 ZIP 与 PDF 配对，由你提供正确组合。
- “查看这个任务目前到哪一步”：只检查状态和已保存文件，不创建转换任务。

每页至少核验一次，首轮修改过的页再复核一次。页与 Markdown 块的对应是启发式候选，Agent 仍需看整页及相邻内容。完全缺失且没有可替换块的段落、无法安全修复的图片等问题会进入报告，不凭空补写。

## 遇到问题

- **网络中断或轮询窗口耗尽**：让 Agent 继续原任务，仍使用已经创建的任务 ID。查询与下载有次数、时长上限。
- **上传或创建结果不明**：让 Agent 核对上游状态；恢复不会重发该创建请求，也不能把“再新建一个”当作自动修复。
- **Key 缺失、变更或鉴权拒绝**：让 Agent 报告实际来源，选择修复本机配置或通过 secret-book 保存。网络、余额和权限错误不直接当作 Key 错误。
- **依赖问题**：让 Agent 检查冻结运行环境及 Pandoc 3.10.1。Skill 自有 Python 环境可按锁文件修复，系统工具安装另行确认。
- **文件被改动或旧任务格式**：保留现状，让 Agent 检查；不要手动修改检查点、页图或原始解压文件来跳过校验。

## 从 1.x 升级

2.0.0 改用 `run / resume / inspect / record-review`，不再使用旧的 `start/advance/record preflight`、代次参数、`settings.json` 和发布设置。已有 `AIHUB_API_KEY`、项目配置层和显式全局配置继续有效。

旧任务不自动迁移。对 Agent 说：

> 这是一份未完成的 pdf2markdown 1.x 任务，请从同一仓库的固定标签 pdf2markdown/v1.3.0 获取独立安装，用它完成原任务，保留现有安装和任务文件。若已有 Doc2X ZIP，也可以用 2.0.0 新建核验任务，避免再次转换。

已发布的 [1.3.0](https://github.com/cookaihq/cookai-skills/releases/tag/pdf2markdown%2Fv1.3.0) 是旧流程的获取入口；不能用新版直接改写旧检查点。

## 更新

每次调用先自动检查更新，检查失败可继续当前任务，安装更新须你逐次确认。Git 检出使用系统对应的 Bash / PowerShell 适配器；WorkBuddy 等复制安装使用随包 `update.json` 定位此 Skill 的稳定标签和固定提交，缺少可靠更新能力时报告原因并继续工作。

“关闭 pdf2markdown 的自动检查更新”会只修改全局文件中的 `AUTO_UPDATE_CHECK=0`。这个非敏感开关只读取该全局文件，不走业务配置的项目层，也不受全局 Key 启用选项控制。
