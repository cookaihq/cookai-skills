# Image 2

通过 [aihubmax.com](https://aihubmax.com) 的 `gpt-image-2` 接口，让 AI Agent 自动调用、生成图片并保存到当前工作区。

支持文生图、图生图、11 种预设比例与自定义像素分辨率（最高 4K），任务完成后图片自动下载到工作区根目录，文件名按 `时间戳 + prompt 标签` 自动命名。

---

## What It Does

- **文生图**：一句 prompt → 一张图（默认 `1024x1024`）
- **图生图**：传 1-N 张参考图 URL，让 AI 在参考图基础上重绘
- **完整版 vs 精简版**：
  - `gpt-image-2`：11 种预设 + 自定义像素分辨率（256-3840、16 倍数）、`num_outputs` 1-10、`quality`/`output_format`/`background`/`mask_url` 控制
  - `gpt-image-2-limit`：3 种预设（`1024x1024` / `1024x1536` / `1536x1024`）、单图、无高级参数
- **自动下载到工作区**：任务终态 `completed` 后自动 `curl` 下载，命名 `{YYYYMMDD-HHMMSS}-{≤10 字标签}.{ext}`，多输出加 `-01/-02` 后缀
- **API Key 多层兜底**：环境变量 → `.env.local` → `.env` → 用户级配置文件，HTTP 401 自动 fallback 到下一层

## Supported Platforms

任何遵循 [agentskills.io](https://agentskills.io) 开放标准的 Agent 平台都可使用：

- ✅ **Claude Code**（已端到端实测）
- ⚠️ **Codex CLI / Gemini CLI / Cursor / VS Code / GitHub Copilot** 等：本 Skill 是一份标准 `SKILL.md` + 共享 `scripts/`，理论上即装即用，未逐个实测

依赖：

- macOS / Linux shell（`bash`、`python3`、`curl`、`grep`、`sed`）
- 互联网，可访问 `api.aihubmax.com` 与阿里云 OSS（图片 URL host）

## Installation

### 方式 1：项目级 symlink（推荐）

把 skill 链到当前项目，只在这个项目生效：

```bash
git clone https://github.com/<your-org>/<your-repo>.git
mkdir -p ./.claude/skills
ln -s "$(pwd)/<your-repo>/image-2" ./.claude/skills/image-2
```

### 方式 2：用户级 symlink

装到 `~/.claude/skills/`，所有项目都可触发：

```bash
ln -s "$(pwd)/<your-repo>/image-2" ~/.claude/skills/image-2
```

### 方式 3：vercel-labs/skills CLI

```bash
npx skills add <your-org>/<your-repo> --skill image-2 -a claude-code
```

> Codex / Gemini 用户请把 `-a claude-code` 换成对应平台标识。

## Quick Start

```bash
# 1. 在当前项目根目录配置 API Key
echo 'X_API_KEY=sk-xxx' >> .env

# 2. 启动 Claude Code，发起对话
你：帮我生成一张 16:9 的科技感产品海报，prompt 是「未来感无线耳机，霓虹光效，电影构图」
```

Agent 自动识别意图 → 调用脚本 → 轮询任务 → 下载到 `./20260527-183934-未来感无线耳.png`。

获取 API Key：去 [aihubmax.com](https://aihubmax.com) 注册账号，控制台生成。

## Configuration

### API Key

环境变量名固定为 **`X_API_KEY`**（值会作为 `Authorization: Bearer <key>` 提交）。

读取优先级（高 → 低）：

| 优先级 | 来源 | 触发方式 |
|---|---|---|
| 1 | shell 环境变量 `X_API_KEY` | 已 `export` |
| 2 | `$PWD/.env.local` 中的 `X_API_KEY=...` | 自动 |
| 3 | `$PWD/.env` 中的 `X_API_KEY=...` | 自动 |
| 4 | `~/.config/image-2/.env` | 加 `--use-local-key` 启用 |

**HTTP 401 自动 fallback**：如果上一层 key 调用 API 返回 401（认证失败），会自动尝试下一层；其他错误（402/422/429/5xx）立即停止。

持久化全局 key（可选）：

```bash
./scripts/set_key.sh          # 交互式输入
echo 'sk-xxx' | ./scripts/set_key.sh --stdin
```

### 输出位置

| 配置 | 优先级 | 说明 |
|---|---|---|
| `--output-dir DIR` | 高 | 单次调用指定目录 |
| env `IMAGE_2_OUTPUT_DIR` | 中 | 全局默认目录 |
| 无配置 | 默认 | 落到 `$PWD`（当前工作区根目录） |

文件名：

| 配置 | 效果 |
|---|---|
| 默认 | `{YYYYMMDD-HHMMSS}-{prompt 前 10 字}.{ext}` |
| `--label TEXT` | 替换标签段：`{时间戳}-{TEXT}.{ext}` |
| `--filename NAME` | 整段替换：`{NAME}.{ext}` |
| `--no-save` | 不下载，只输出 URL |

多输出（`num_outputs > 1`）自动加 `-01`、`-02` 后缀。同名冲突追加 `-2`、`-3` 数字。

## Usage Examples

直接和 Agent 对话即可：

| 你说 | Agent 行为 |
|---|---|
| 「帮我生成一张产品海报」 | 文生图，默认 `1024x1024`，存到工作区根目录 |
| 「按这 2 张参考图重画一张」 | 图生图，传 `image_urls` |
| 「9:16 竖屏封面」 | `resolution=1080x1920` |
| 「生成 3 张供选」 | `num_outputs=3`，文件名加 `-01/-02/-03` |
| 「保存到 ~/Desktop」 | `--output-dir ~/Desktop` |
| 「文件叫 banner」 | `--filename banner` |
| 「只要 URL 不下载」 | `--no-save` |

直接 CLI 调用（脚本模式）：

```bash
# 最小调用：文生图默认参数
X_API_KEY=sk-xxx ./scripts/create_task.sh \
  --prompt "未来城市夜景海报" --resolution 1920x1080

# 图生图 + 指定保存位置
X_API_KEY=sk-xxx ./scripts/create_task.sh \
  --prompt "极简风格重绘" \
  --image-url https://example.com/ref.png \
  --resolution 1024x1536 \
  --output-dir ~/Pictures
```

完整参数：`./scripts/create_task.sh --help`。

## 持久上传（显式 opt-in）

`--json` 会输出固定 schema 的任务结果；每个成功保存项具有绝对 `local_path`。当用户明确要求长期 URL/Object Reference 时，Agent 才把这些 `saved` 文件交给独立的 `s3-upload` 阶段：

```bash
# 阶段一：生成并保存本地文件
./scripts/create_task.sh --json \
  --prompt "产品封面" --resolution 1024x1024

# 阶段二：保持原项目 cwd，对每个 saved local_path 单独执行
python3 /absolute/s3-upload/scripts/upload.py upload \
  --file /absolute/output/cover.png \
  --caller-skill image-2 \
  --json
```

项目可以在 `.s3-upload/config.json` 声明 `"image-2":"project:website-images"` mapping。该 mapping 只选择目的地，不会让 image-2 脚本自动调用 S3。只有明确的持久上传请求才执行第二阶段；上传 partial/ambiguous 时保留本地图片和 checkpoint，不重新生成或盲目重复 Put。

## Security / Privacy

- **联网**：是。调用 `https://api.aihubmax.com/v1/*`，从阿里云 OSS 下载生成图片
- **API Key**：必需。本 skill **不会**把完整 key 写入仓库、日志或回显；终端输出始终掩码为 `head4****tail4`，完整值仅出现在 `Authorization` HTTP header 中
- **本地文件读取**：自动读取 `$PWD/.env.local` 与 `$PWD/.env`，但**不向上递归**（不读父目录、git root、`$HOME` 的 dotenv）；持久化 key 在 `~/.config/image-2/.env`，**必须显式 `--use-local-key`** 才启用
- **本地文件写入**：默认在 `$PWD` 创建图片文件；可通过 `--no-save` 关闭
- **第三方服务**：调用前请自行评估 [aihubmax.com](https://aihubmax.com) 的可信度与合规要求
- **图片有效期**：aihubmax.com 返回的 URL **24 小时**后失效，长期保留请下载到本地（默认行为已下载）
- **计费**：每次成功创建任务（HTTP 200）都会消耗 aihubmax.com 积分；HTTP 401 不计费

## Cost

每次生成的积分消耗以 aihubmax.com 计费规则为准。实测 `gpt-image-2` 完整版、`1024x1024`、`num_outputs=1`、`quality=high`（默认）的单次调用 `credits_reserved = 121000`（Enterprise 用户组）。

提高成本的参数：

- 分辨率（4K 比 1024x1024 高出数倍）
- `num_outputs > 1`
- `quality=high`（默认）

降低成本的参数：

- `--quality low`
- 使用精简版 `gpt-image-2-limit`

## Files

```
image-2/
├── SKILL.md              # Agent 入口文档（agentskills.io 标准）
├── README.md             # 本文件（给人看）
├── scripts/
│   ├── create_task.sh    # 主脚本：创建任务 + 轮询 + 自动下载
│   ├── image_task.py     # 标准库 runtime：JSON/legacy、轮询与安全下载
│   └── set_key.sh        # 持久化 key 到 ~/.config/image-2/.env
├── references/
│   └── api-guide.md      # aihubmax.com API 完整规范
└── tests/
    ├── contracts/        # handoff schema、golden 与 validator
    ├── runtime/          # JSON/legacy 与下载安全测试
    ├── README.md         # 测试场景索引
    └── scenario-*.md     # pressure 测试场景文档
```

## Troubleshooting

| 现象 | 处理 |
|---|---|
| `Error: no API key found in any of:` | 在 `.env` 加 `X_API_KEY=...` 或 `export X_API_KEY=sk-xxx` |
| 所有 key 都返回 HTTP 401 | 去 [aihubmax.com](https://aihubmax.com) 控制台确认 key 未过期、未禁用、有调用 image API 的权限 |
| HTTP 402 `insufficient_quota` | 余额不足，去 aihubmax.com 充值 |
| HTTP 422 `validation_error` | 检查 `resolution`（精简版只支持 3 种）、`num_outputs`（精简版只能 1）、互斥字段（`mask_url` vs `background`） |
| 长时间停留 `processing` 直到超时 | 用返回的 task_id 手动查询：`curl https://api.aihubmax.com/v1/tasks/{task_id}?sync_upstream=true -H "Authorization: Bearer $X_API_KEY"` |
| 报错 `status=completed but results empty` 后继续轮询 | 这是脚本对上游竞态的保护，无需处理；几秒内会拿到 `results` |

## License

MIT，见仓库根 [LICENSE](../LICENSE)。

## Acknowledgements

- 图片生成接口：[aihubmax.com](https://aihubmax.com)
- Skill 结构遵循 [agentskills.io](https://agentskills.io) 开放标准
