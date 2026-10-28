# video-to-markdown

把一个视频整理成保留原始内容的 Markdown 文档：结合语音转写、带时间的截图、必要的短 GIF 和覆盖复核，适合课程、演示和操作说明。可以处理本地视频或能直接下载的视频链接，不负责下载视频网站页面，也不自动发布文档。

## 安装与准备

把下面这段话复制给正在使用的 Claude Code、Codex 或 WorkBuddy：

> 请从 https://github.com/cookaihq/cookai-skills 安装 video-to-markdown，保留已有配置，检查当前宿主能否发现和调用它，并告诉我实际安装的版本。

Agent 会检查 uv、ffmpeg、ffprobe。Claude Code 的独立 Skill 入口是 `/video-to-markdown`，Codex 是 `$video-to-markdown`；也可以说明“用 video-to-markdown 整理这个视频”。WorkBuddy 应从本机实际支持的本地 Skill 入口安装并点名调用；其本地安装、Plugin 加载与开放平台上架是不同入口，不需要为了使用本 Skill 上架。

三宿主都是支持目标。2.0.0 的 Windows 原生脚本、模拟服务恢复和离线文档流程已通过本地测试。Claude Code 和 Codex 已检查实际发现入口，但本机默认读取权限、只读沙箱和 uv 启动限制使最小闭环尚未通过；WorkBuddy 实际调用仍待验收。三端的依赖调用、登录交互与跨会话完整闭环也仍待分别验收。macOS/Linux/WSL 的本次改动尚未实机复测。

## 转写方式与配置

有现成 SRT、VTT 或带起止时间的文字稿时直接使用，不上传音频。没有字幕时，两个选项并行提供：

| 选项 | 用途和准备 |
|---|---|
| AIhub Audio（内置默认） | 安装 AIhub Studio 中的 AIhub Audio Skill，由它管理 AIhub 凭证、模型和结果检查。 |
| 飞书妙记 | 安装官方 Lark CLI，选择飞书账号并由本人完成登录授权；需要云盘上传、妙记创建和导出权限及可用额度。 |

AIhub Audio 缺失、缺配置或明确失败时，会说明原因并尝试将**本次任务**转到飞书。首次飞书授权会暂停等待你完成。它不会因此修改以后任务的默认选项。AIhub 已提交但正在等待、下载失败、等待结果检查或提交结果不明时，继续保留原任务。

可以这样配置：

> 请将 video-to-markdown 的默认转写方式设为飞书妙记。帮我选择正确账号，展示应用和用户身份让我确认。默认保存到个人全局，并为任务显式启用读取；保留其他配置。

本 Skill 的默认保存位置是个人全局 `~/.config/video-to-markdown/.env`，只有任务显式启用个人配置（由 Agent 使用 `--use-local-key`）才读取。项目中已有设置时，Agent 会解释实际生效来源。飞书身份可以只绑定本次任务；长期保存身份也需要你确认位置。登录令牌仍由 Lark CLI 管理。

配置读取顺序：进程环境变量 → 你发起任务的工作文件夹中的 `.env.video-to-markdown` → `.env.local` → `.env` → 显式启用的个人全局文件。不搜索父文件夹，也不从安装目录或结果目录寻找配置。`VIDEO_TO_MARKDOWN_ASR_PROVIDER` 的值为 `aihub-audio` 或 `feishu-minutes`；当前请求的明确选择优先，已经开始的任务继续使用保存的选择。

| 运行环境 | 本 Skill 的个人全局位置 |
|---|---|
| macOS / Linux | 用户个人文件夹中的 `.config/video-to-markdown/.env` |
| Windows 原生 | 例如 `C:\Users\你的用户名\.config\video-to-markdown\.env`，按实际 Python 用户目录定位 |
| WSL | WSL 用户自己的 `~/.config/video-to-markdown/.env`，不自动读取 Windows 那一份 |

AIhub Audio 独立读取自己的配置。它依次读取环境变量、当前工作文件夹的 `.env.aihub-audio` / `.env.local` / `.env`，然后个人目录中的 `~/.config/aihub-studio/aihub-audio/.env.local`、同目录 `.env`、Plugin 根的 `.env.aihub-audio` / `.env.local` / `.env`，最后补齐 `~/.config/aihub-audio/.env`。Plugin 全局读取默认启用；Windows 使用其实际用户个人目录，WSL 与 Windows 分离。对话中说“这次禁用全局配置”可同时关闭两者的全局业务读取。

首次 AIhub 凭证配置可以对 Agent 说：“请沿用我已有的配置方式；没有的话，让我选择手填本机配置或从 secret-book 选择并保存。”本 Skill 不复制、迁移或保存 AIhub 密钥；缺项由 AIhub Audio 的配置流程处理。

## 第一次使用

云端转写会上传提取后的音频，可能消耗 AIhub 或飞书额度。飞书会创建云盘文件和妙记；本 Skill 不自动删除这些云端对象。默认没有逐请求的付费确认门。可以先提供字幕，验证本地视频整理流程。

> 请用 video-to-markdown 整理我提供的这个视频和 SRT 字幕，保留原语言与内容顺序，结合截图和必要的 GIF，完成覆盖复核后把文档路径告诉我。

完成后通常得到 `video-to-markdown-output/时间编号-视频名/` 下的 `document.md` 和引用的图片/GIF；Agent 会返回实际路径。转写完成后还需要 Agent 查看画面、补采样、写正文并复核，只有任务 ID 或字幕不算整份文档完成。

## 常用请求

- “这次优先使用飞书妙记，不修改以后默认。”只改变新任务的本次通道。
- “这次只能使用 AIhub Audio，不要向飞书上传。”禁止自动跨平台切换。
- “飞书额度用完后，这次切换并把以后默认设为 AIhub Audio。”Agent 会区分本次切换与默认保存是否实际生效。
- “继续这个任务文件夹。”恢复保存的输入、账号和任务。不要通过重新开始来重试结果不明的上传。
- “先暂停，我稍后登录。”保存任务；稍后继续原任务或选择当时提供的安全切换选项。

## 限制与排障

音频使用完整的第一条音轨，转换为单声道 MP3；记录音轨、时长、哈希及视频时间偏移，不自动截断或拆分收费任务。AIhub 本地上传上限为 20 MiB；飞书上限为 6 小时、6 GB，实际账号或模型可能更严格。无音轨的视频需要你提供适合的带时间文字材料。

飞书导出的是句段时间，不能承诺每个字出现的准确时刻。AIhub 实际返回词时间时会保留；缺失、倒序或越界时间会阻止完整证据交付。显示到毫秒不代表识别准确到毫秒。

遇到问题可对 Agent 说：“检查这个任务的下一动作、配置来源和已有远端 ID，先说明可恢复到哪一步。”登录、权限、额度、网络和结果不明分别处理。结果下载失败应补下载原结果。AIhub 出现已知任务 ID 但依赖落盘失败时可以按该 ID 查询；把另行恢复的子任务接回原运行及结果检查的公共接口尚未核实，Agent 会报告这一限制，不能擅自重新转写。

分享时只提供正文和引用素材，不分享 `.state/`：其中包含私有恢复记录、原始响应和临时链接。脚本校验主要检查结构、引用与时间范围，语义内容仍须实际复核。长 GOP 视频的截图/GIF 时间定位、任务目录改名后的路径和语义覆盖检查仍有独立待修问题，不属于本次转写改造已解决的范围。

## 从 1.x 升级

2.0.0 删除了 Skill 自己的 AIhub HTTP 客户端和凭证别名，不自动搬动真实密钥。旧 `VIDEO_TO_MARKDOWN_ASR_MODEL` 必须由 Agent 核对意图后迁移到 AIhub Audio 的模型配置；不能悄悄改用默认型号。旧任务文件保留，但其旧状态格式不能由 v2 直接续跑，也不会被自动重新提交。升级前请保留需要查询的原任务材料。

## 更新

本 Skill 默认自动检查更新，更新本身需要你确认。Git 安装使用系统对应的随包检查脚本；复制安装按 `update.json` 的稳定版本及固定提交检查。WorkBuddy 不具备已核实的更新入口时会说明未完成并继续当前任务。

可以说：“检查 video-to-markdown 是否有新版本，先告诉我变化。”也可以说：“关闭它的自动检查更新。”这个开关只保存在个人全局文件中的 `AUTO_UPDATE_CHECK`，独立于业务配置的读取优先级；检查失败不会阻止处理视频。

详细 Agent 流程见 [SKILL.md](SKILL.md)，升级变化见 [CHANGELOG.md](CHANGELOG.md)。
