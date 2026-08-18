# template-preview

把一个文件夹的图片 + 文案渲染成「指定模板风格」的自包含展示页。提供**小红书**模板：个人主页 + 笔记详情页。

## 它做什么

- 输入：一条或多条笔记，每条含若干图 + 文案（标题/正文/点赞），以及可选人设（昵称/简介/头像/数据）。
- 输出：一个自包含文件夹 —— `index.html`（个人主页）+ 每条笔记一个 `note-NN.html`（详情页，多图横向轮播）+ `content.json`（本次数据，便于手改重渲）+ `assets/`（所有图片，相对引用）。
- 本地双击 `index.html` → 点笔记卡 → 跳到详情页；可整站交给 [preview-share](../preview-share/) 上传得到在线预览链接。

## 用法

```bash
# 0) 本 skill 目录（下面所有命令都用它定位；uv run --project 需要它找 pyproject.toml）
SKILL_DIR=/path/to/github-cookai-skills/template-preview

# 1) 准备 content.json（通常由 Claude 按 SKILL.md 工作流生成）
#    { "label": "iot", "notes": [
#        { "title": "标题", "images": ["c1.jpg","c2.jpg"], "body": "正文", "likes": 1234 } ] }
#    一条「封面+多图」笔记 = 一个 note，images 列出全部图（不要拆成多个 note）

# 2) 先看计划（不写盘）。默认输出到当前目录 $PWD 下、以 --name 命名的文件夹
#    统一用 uv run --project 钉死解释器（ADR 0007），SKILL_DIR = 本 skill 目录
uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/generate.py" --template xiaohongshu --content content.json --name xiaohongshu-iot --dry-run

# 3) 正式生成（--out-root 可换父目录；不传 --name 则按日期时间自动命名）
uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/generate.py" --template xiaohongshu --content content.json --name xiaohongshu-iot
# stdout 打印全部页面绝对路径（每行一个，第一行 = index.html）
```

## 配置

人设与输出位置通过环境变量 / `.env` / `.env.local` 覆盖（不配则用内置默认）。完整变量表见 [SKILL.md](SKILL.md)。例如临时换昵称：

```bash
TPL_XHS_NICKNAME='我的昵称' TPL_XHS_BIO='我的简介' \
uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/generate.py" --template xiaohongshu --content content.json --label demo
```

## 模板可插拔

每个模板在 `templates/<名字>/` 下自带：`home.html`（个人主页版式）、`note.html`（笔记详情页版式）、`defaults.env`（默认人设）、`assets/`（默认头像 + 填充卡 + `titles.json`）。新增平台模板只需多一个目录，核心脚本不变。

## 故障排查

- **图/跳转在本地能用、线上裂**：别只传 `index.html` —— 用 `preview-share`，**以 `index.html` 为入口**上传，它会沿卡片 `href` 递归带上各 `note-NN.html` 与 `assets/` 整站上传。
- **本该是 1 篇却拆成了多篇**：一条「封面+多图」笔记应是**一个** note、`images` 列出全部图；不要每张图一个 note。
- **`[error] 输出目录已存在，拒绝覆盖`**：同名文件夹已存在 —— 换个 `--name`，或（自动命名时）在 `TPL_SUBDIR_PATTERN` 保留 `{time}`。
- **填充卡太多/太少**：调 `TPL_XHS_MIN_CARDS`。
- **想换填充卡**：把 `TPL_XHS_FILLER_CARDS` 指向自己的目录（内含图片 + `titles.json`）。

## 开发 / 测试

```bash
uv run --project . --with pytest python -m pytest tests/test_generate.py -q
```

纯 Python 标准库，无运行时第三方依赖（测试用 pytest，经 `--with` 临时叠加，不进 `uv.lock`）。

运行时环境由 uv 管理（ADR 0007）：`pyproject.toml` + `uv.lock` 钉死解释器版本，venv 落 `<skill>/.venv`。
手工重建：`rm -rf <skill>/.venv && uv sync --project <skill> --no-dev`；`scripts/generate.py` 被裸 `python3` 直接执行时也会自动拉回该 venv。
