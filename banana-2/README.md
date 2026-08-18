# Banana 2

通过 [aihubmax.com](https://aihubmax.com) 的 **Nano Banana 2**（模型 `gemini-3.1-flash-image-preview`）接口，让 AI Agent 自动调用、生成/编辑图片并保存到当前工作区。

支持文生图、图生图、图像编辑；用**宽高比**（15 种，含 `match_input_image`）+ **画质档**（`512`/`0.5K`/`1K`/`2K`/`4K`）描述尺寸；可选联网搜索（`google_search`）与图像搜索（`image_search`）辅助生成。任务完成后图片自动下载到工作区根目录，文件名按 `时间戳 + prompt 标签` 自动命名。

---

## What It Does

- **文生图**：一句 prompt → 一张图（默认 `aspect_ratio=1:1`、`resolution=1K`）
- **图生图 / 图像编辑**：传 1-N 张参考图 URL，让模型基于参考图重绘或局部编辑（如换背景）
- **宽高比 + 画质档分离**：
  - `aspect_ratio`：`1:1` `1:4` `1:8` `2:3` `3:2` `3:4` `4:1` `4:3` `4:5` `5:4` `8:1` `9:16` `16:9` `21:9` `match_input_image`
  - `resolution`（画质档，非像素）：`512` / `0.5K`（half-size）、`1K`（~1MP）、`2K`（~4MP）、`4K`（~16MP）
- **联网/图搜辅助**：`--google-search`（实时信息）、`--image-search`（图像搜索，本模型独有）
- **自动下载到工作区**：任务终态 `completed` 后自动 `curl` 下载，命名 `{YYYYMMDD-HHMMSS}-{≤10 字标签}.{ext}`
- **API Key 多层兜底**：环境变量 → `.env.local` → `.env` → 用户级配置文件，HTTP 401 自动 fallback 到下一层

> 与本仓库的 `image-2`（`gpt-image-2`，像素分辨率 + 多图 + quality/mask）相比，Banana 2 是单模型、恒 1 张输出、用宽高比 + 画质档描述尺寸，并多了 `google_search` / `image_search`。泛化的"生成图片"且用像素分辨率（如 `1024x1024`）请用 `image-2`。

## Supported Platforms

任何遵循 [agentskills.io](https://agentskills.io) 开放标准的 Agent 平台都可使用：

- ✅ **Claude Code**
- ⚠️ **Codex CLI / Gemini CLI / Cursor / VS Code / GitHub Copilot** 等：本 Skill 是一份标准 `SKILL.md` + 共享 `scripts/`，理论上即装即用，未逐个实测

依赖：

- macOS / Linux shell（`bash`、`curl`、`grep`、`sed`）
- [`uv`](https://docs.astral.sh/uv/) ≥ 0.8：脚本内的 Python 片段一律经 `uv run --project <skill 目录> python` 执行，解释器由本目录的 `pyproject.toml` + `uv.lock` 钉死（首次运行会自动在 `<skill>/.venv` 建环境）。未安装时脚本会报错并给出安装命令 `curl -LsSf https://astral.sh/uv/install.sh | sh`
- 互联网，可访问 `api.aihubmax.com` 与图片 URL host

## Installation

### 方式 1：项目级 symlink（推荐）

把 skill 链到当前项目，只在这个项目生效：

```bash
git clone https://github.com/cookaihq/cookai-skills.git
mkdir -p ./.claude/skills
ln -s "$(pwd)/cookai-skills/banana-2" ./.claude/skills/banana-2
```

### 方式 2：用户级 symlink

装到 `~/.claude/skills/`，所有项目都可触发：

```bash
ln -s "$(pwd)/cookai-skills/banana-2" ~/.claude/skills/banana-2
```

### 方式 3：vercel-labs/skills CLI

```bash
npx skills add cookaihq/cookai-skills --skill banana-2 -a claude-code
```

> Codex / Gemini 用户请把 `-a claude-code` 换成对应平台标识。

## Quick Start

```bash
# 1. 在当前项目根目录配置 API Key
echo 'AIHUB_API_KEY=sk-xxx' >> .env

# 2. 启动 Claude Code，发起对话
你：用 banana-2 生成一张 16:9 的科技感产品海报，prompt 是「未来感无线耳机，霓虹光效，电影构图」
```

Agent 自动识别意图 → 调用脚本 → 轮询任务 → 下载到 `./20260528-153934-未来感无线耳.png`。

获取 API Key：去 [aihubmax.com](https://aihubmax.com) 注册账号，控制台生成。该 key 与 `image-2` 通用（同一个 aihubmax.com 账号）。

## Configuration

### API Key

环境变量名固定为 **`AIHUB_API_KEY`**（值会作为 `Authorization: Bearer <key>` 提交）。

读取优先级（高 → 低）：

| 优先级 | 来源 | 触发方式 |
|---|---|---|
| 1 | shell 环境变量 `AIHUB_API_KEY` | 已 `export` |
| 2 | `$PWD/.env.local` 中的 `AIHUB_API_KEY=...` | 自动 |
| 3 | `$PWD/.env` 中的 `AIHUB_API_KEY=...` | 自动 |
| 4 | `~/.config/banana-2/.env` | 加 `--use-local-key` 启用 |

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
| env `BANANA_2_OUTPUT_DIR` | 中 | 全局默认目录 |
| 无配置 | 默认 | 落到 `$PWD`（当前工作区根目录） |

文件名：

| 配置 | 效果 |
|---|---|
| 默认 | `{YYYYMMDD-HHMMSS}-{prompt 前 10 字}.{ext}` |
| `--label TEXT` | 替换标签段：`{时间戳}-{TEXT}.{ext}` |
| `--filename NAME` | 整段替换：`{NAME}.{ext}` |
| `--no-save` | 不下载，只输出 URL |

扩展名推断顺序：`--output-format` → 响应 `content_type`（本模型通常无）→ URL 尾缀 → `png`。本模型恒 1 张输出。

## Usage Examples

直接和 Agent 对话即可：

| 你说 | Agent 行为 |
|---|---|
| 「用 banana-2 生成一张产品海报」 | 文生图，默认 `1:1` / `1K`，存到工作区根目录 |
| 「16:9 横屏」 | `--aspect-ratio 16:9` |
| 「竖屏封面」 | `--aspect-ratio 9:16` |
| 「要 4K 高清」 | `--resolution 4K` |
| 「把这张图背景换成海滩，按原图比例」 | `--image-url <URL>` + `--aspect-ratio match_input_image` |
| 「参考实时信息生成」 | `--google-search` |
| 「参考网上的图片风格」 | `--image-search` |
| 「保存到 ~/Desktop」 | `--output-dir ~/Desktop` |
| 「文件叫 banner」 | `--filename banner` |
| 「只要 URL 不下载」 | `--no-save` |

直接 CLI 调用（脚本模式）：

```bash
# 最小调用：文生图（默认 1:1 / 1K）
AIHUB_API_KEY=sk-xxx ./scripts/create_task.sh \
  --prompt "未来城市夜景海报" --aspect-ratio 16:9 --resolution 1K

# 图像编辑 + 图像搜索辅助
AIHUB_API_KEY=sk-xxx ./scripts/create_task.sh \
  --prompt "Replace the background with a tropical beach" \
  --image-url https://example.com/photo.jpg \
  --aspect-ratio match_input_image --resolution 2K \
  --image-search --output-dir ~/Pictures
```

完整参数：`./scripts/create_task.sh --help`。

## Security / Privacy

- **联网**：是。调用 `https://api.aihubmax.com/v1/*`，从返回的图片 URL host 下载图片
- **API Key**：必需。本 skill **不会**把完整 key 写入仓库、日志或回显；终端输出始终掩码为 `head4****tail4`，完整值仅出现在 `Authorization` HTTP header 中
- **本地文件读取**：自动读取 `$PWD/.env.local` 与 `$PWD/.env`，但**不向上递归**（不读父目录、git root、`$HOME` 的 dotenv）；持久化 key 在 `~/.config/banana-2/.env`，**必须显式 `--use-local-key`** 才启用
- **本地文件写入**：默认在 `$PWD` 创建图片文件；可通过 `--no-save` 关闭
- **第三方服务**：调用前请自行评估 [aihubmax.com](https://aihubmax.com) 的可信度与合规要求
- **图片有效期**：aihubmax.com 返回的 URL **24 小时**后失效，长期保留请下载到本地（默认行为已下载）
- **计费**：每次成功创建任务（HTTP 200）都会消耗 aihubmax.com 积分；HTTP 401 不计费

## Cost

每次生成的积分消耗以 aihubmax.com 计费规则为准（可在任务查询响应的 `usage.credits_reserved` 看到本次预扣额度）。

提高成本的因素：

- 更高的 `resolution` 画质档（`2K` / `4K` 比 `1K` 高）
- 开启 `google_search` / `image_search` 等辅助能力

降低成本的因素：

- 较低的画质档（`512` / `0.5K` / `1K`）

## Files

```
banana-2/
├── SKILL.md              # Agent 入口文档（agentskills.io 标准）
├── README.md             # 本文件（给人看）
├── scripts/
│   ├── create_task.sh    # 主脚本：创建任务 + 轮询 + 自动下载
│   └── set_key.sh        # 持久化 key 到 ~/.config/banana-2/.env
├── references/
│   └── api-guide.md      # aihubmax.com Nano Banana 2 API 完整规范
└── tests/
    ├── README.md         # 测试场景索引
    └── scenario-*.md     # 7 个 pressure 测试场景文档
```

## Troubleshooting

| 现象 | 处理 |
|---|---|
| `Error: no API key found in any of:` | 在 `.env` 加 `AIHUB_API_KEY=...` 或 `export AIHUB_API_KEY=sk-xxx` |
| 所有 key 都返回 HTTP 401 | 去 [aihubmax.com](https://aihubmax.com) 控制台确认 key 未过期、未禁用、有调用 image API 的权限 |
| HTTP 402 `insufficient_quota` | 余额不足，去 aihubmax.com 充值 |
| HTTP 422 `validation_error` | 检查 `aspect_ratio`（15 种）、`resolution`（只接受 `512`/`0.5K`/`1K`/`2K`/`4K` 画质档，不是像素）、`output_format`（`jpg`/`png`/`webp`） |
| 长时间停留 `processing` 直到超时 | 用返回的 task_id 手动查询：`curl https://api.aihubmax.com/v1/tasks/{task_id}?sync_upstream=true -H "Authorization: Bearer $AIHUB_API_KEY"` |
| 报错 `status=completed but results empty` 后继续轮询 | 这是脚本对上游竞态的保护，无需处理；几秒内会拿到 `results` |

## License

MIT，见仓库根 [LICENSE](../LICENSE)。

## Acknowledgements

- 图片生成接口：[aihubmax.com](https://aihubmax.com)
- Skill 结构遵循 [agentskills.io](https://agentskills.io) 开放标准
