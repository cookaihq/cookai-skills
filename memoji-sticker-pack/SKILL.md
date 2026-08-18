---
name: memoji-sticker-pack
version: 1.1.0
description: v1.1.0｜从一张人物照片生成一套 Apple Memoji 风格（拟我表情）的表情贴纸包。当用户想"把这张照片/自拍做成 Memoji 表情包 / 拟我表情包 / 表情贴纸 / nimoji / Q 版头像表情"，或给一张人脸照片并想要一组不同表情（微笑/大笑/哭/惊讶/比心/点赞等）的卡通贴纸时，使用本技能——即使用户没明确说"Memoji"这个词，只要意图是"照片→一套人物表情贴纸"，也应触发。也支持只生成单张 Memoji 风格头像。不用于：视频/动态表情、OCR、给已有图做裁剪压缩水印等非生成式编辑。
---

# memoji-sticker-pack

## 这个技能做什么

输入**一张人物照片**，产出一套 **Apple Memoji 风格**的表情贴纸包：先把照片转成一张「基准 Memoji」头像锁定长相，再以它为参考逐个生成多个表情（默认 16 个），最后给出透明底 PNG + 可浏览的 `index.html` 画廊。

本技能用 **image-2 (gpt-image-2)** 的 `create_task.sh` 生成图（复用它的 key 链、轮询、下载、401 兜底），并用自身 `scripts/upload.py` 把参考图上传到 aihubmax 文件接口换成 72h 公网 URL。**参考图统一走「上传取 URL」，不再内联 base64 data URI**：传给生成接口的 `image_urls` 全是 aihubmax CDN 链接。生成与上传共用同一把 `AIHUB_API_KEY`、同一 host（`api.aihubmax.com`，可用 `AIHUBMAX_BASE_URL` 覆盖）。

## 何时使用

- "把这张照片/自拍做成 Memoji 表情包 / 拟我表情包 / 表情贴纸包"
- "用我这张照片生成一组不同表情的卡通贴纸"
- "做个 Q 版头像表情 / nimoji"
- 只要一张 Memoji 风格头像（用 `--mode single`）

## 何时不要用

- 视频 / 动态 Memoji / GIF
- OCR、文档解析
- 对已有图做裁剪、压缩、加水印等非生成式编辑

## 依赖

- 需要生成图片时，已安装 **image-2** 技能（`~/.claude/skills/image-2*/scripts/create_task.sh`）。
- 上传实现已内置在 `scripts/upload.py`，无需安装额外上传 Skill。只有 `--base-url ... --mode single` 完全不需要 image-2。
- 配好 **aihubmax.com 的 key**（生成与上传共用，环境变量 `AIHUB_API_KEY`，或工作目录下 `.env` / `.env.local`；旧名 `X_API_KEY` 仍兼容）。
  - ⚠️ 用 `--use-local-key` 时，image-2 读 `~/.config/image-2/.env`，本技能内置上传器读 `~/.config/memoji-sticker-pack/.env`（本仓约定每个 skill 各自持久化配置）。若只在其中一个配了 key，另一步会因缺 key 失败——**最省事是把 key 放进程 env 或 `$PWD/.env`，两步都能读到**。
- macOS 自带 `sips`（用于缩图；缺失时回退 `ffmpeg`）。
- **[uv](https://docs.astral.sh/uv/) >= 0.8**：本技能的 Python 运行时（`cutout.py` 用 numpy + Pillow）由 uv 按 `pyproject.toml` + `uv.lock` 钉死，venv 落 `$SKILL_DIR/.venv`，首次运行自动创建（ADR 0007）。`gen_pack.sh` 内部所有 Python 调用都走 `uv run --project "$SKILL_DIR" python`，**不要改回裸 `python3`**；单独跑某个 `.py` 时也用 `uv run --project "$SKILL_DIR" "$SKILL_DIR/scripts/<脚本>.py"`。环境损坏时手工重建：`rm -rf "$SKILL_DIR/.venv" && uv sync --project "$SKILL_DIR"`。

## ⚠️ 成本与确认（重要）

每张贴纸都是一次 `gpt-image-2` 调用、**会消耗 aihubmax 积分**：

- 不复用基准图时，pack 无重试 = **1（基准）+ N（表情）** 次调用（默认 N=16 → 17 次）。
- 默认每次失败生成最多重试一次（含基准），因此最大 = **2 + 2N**；`--no-retry` 时等于无重试次数。
- 不复用基准图时，`single` 无重试 1 次、最大 2 次。
- 复用 `--base-url` 时，pack 无重试 N 次、最大 2N 次；single 不调用生成。
- 另有**文件上传调用**（转存参考图到 aihubmax 文件接口，**非生成调用**）：正常 pack = 2 次、single = 1 次；复用基准图时 pack = 1 次、single = 0 次。上传是否计费以 aihubmax 侧为准；本技能不额外统计。

**因此运行真正生成前，必须：**

1. 先跑 `--plan` 拿到准确的调用次数。
2. 把"将生成什么 + 预计调用次数 + 会消耗积分"摘要给用户，**等用户明确确认后**再真正运行。
3. 失败重试默认开启（用户在设计时已授权）；若用户不希望重试，加 `--no-retry`。
4. **重试只对「重跑安全」的失败生效**（ADR 0006 规则 2/4）：脚本读 `create_task.sh` 打出的 `Error [<code>]` 行分类——只有 `upstream_failed`（上游明确说这次生成失败）与「没有结构化错误码」的失败才整任务重跑；`create_transport_error`（结果不明，任务可能已创建并计费）、`poll_timeout`（任务可能仍在跑）、`query_*`（任务已创建、只是没查到终态）、`create_http_error`（401/402/422 确定性失败，429/5xx 已在 image-2 内部重试过）一律**不重跑**，在 stderr 说明原因交给用户决定。因此实际调用次数可能低于上面的「最大值」。

## 使用流程（Agent 按此执行）

> 下面命令里的 `$SKILL_DIR` 指**本技能自身所在目录**（含本 SKILL.md 的目录）。Claude Code 触发技能时会告知它的 base directory；执行前先 `SKILL_DIR="<该目录>"`，或直接把 `$SKILL_DIR` 换成绝对路径。脚本内部对 `cutout.py`/`build_gallery.py` 的引用是自定位的（`dirname "$0"`），无需另配。

### 1. 收集输入

- 必须拿到一张人物照片（本地路径 / 公网 URL / data URI），或用 `--base-url` 提供已有基准图 URL。新照片会自动缩图并上传到 aihubmax 换 URL，连传入的公网 URL 也会重新转存以统一。
- 可选：人物名/包名（`--name`）、输出目录（`--outdir`）、只要单张（`--mode single`）、自定义/裁剪表情数（`--count` / `--expressions`）。

### 2. 预览成本，等确认

```bash
bash "$SKILL_DIR/scripts/gen_pack.sh" \
  --image "<照片路径>" --name "<名字>" --plan
```

把输出里的调用次数、输出目录、表情清单转述给用户，明确"会消耗积分"，**等用户确认**。

### 3. 真正生成

```bash
bash "$SKILL_DIR/scripts/gen_pack.sh" \
  --image "<照片路径>" --name "<名字>"
```

脚本会：预处理照片 → 生成 `base.png` → 逐个生成 `NN-<slug>.png`（透明底）→ 写 `manifest.json` + `index.html`。每个表情失败自动重试一次再跳过，结束时汇总失败项。

### 4. 展示结果

- 生成完用 `as_open_url` 打开 `<outdir>/index.html` 给用户看整套（画廊用棋盘格背景显示透明区域）。
- 转述成功/失败数；若有失败项，告诉用户可用 `--expressions "<slug>:<描述>"` 单独补跑。

## 关键参数

| 参数 | 说明 |
|---|---|
| `--image PATH` | 与 `--base-url` 二选一。本地路径 / 公网 URL / data URI |
| `--name NAME` | 人物名/包名，影响输出目录 `./memoji-<name>/` 与画廊标题 |
| `--outdir DIR` | 自定义输出目录 |
| `--mode pack\|single` | `pack`=整套(默认)，`single`=只出基准头像 |
| `--count N` | 只取前 N 个表情 |
| `--expressions "slug:描述;..."` | 覆盖默认表情（英文描述效果最好） |
| `--resolution WxH` | 贴纸分辨率，默认 `1024x1024` |
| `--no-retry` | 关闭失败重试 |
| `--base-url URL` | 与 `--image` 二选一。复用已有基准图，跳过基准生成 |
| `--use-local-key` | 允许 image-2 读 `~/.config/image-2/.env`，内置上传器读 `~/.config/memoji-sticker-pack/.env` |
| `--plan` | 只打印计划与调用次数，不生成、不消耗积分 |

## 默认 16 表情

微笑、大笑、狂笑、大哭、流泪、惊讶、比心、点赞、爱心眼、生气、瞪眼、思考、眨眼、翻白眼、捂脸、OK 手势。
（完整 slug 与英文描述见 `scripts/gen_pack.sh` 顶部的 `DEFAULT_EXPRESSIONS`，那是表情集的唯一真源。）

## 一致性是怎么保证的

16 个表情**不是各自从原始照片重画**（那样每张脸会漂移），而是都以**同一张基准 Memoji**为参考、只改表情/动作。基准图由脚本缩到 ≤640px 后**上传到 aihubmax 换成一个 URL**，该 URL 复用给每次调用，所以全套是同一张脸。

## 输出结构

```
memoji-<name>/
  base.png              # 基准头像
  01-smile.png … NN-*.png   # 各表情，透明底 PNG
  manifest.json         # 名称、基准图、表情列表映射
  index.html            # 画廊（浏览器/as_open_url 打开）
  .log-*.txt            # 每次调用的日志（排错用）
```

## 排错

- **"未找到 image-2 的 create_task.sh"**：先装 image-2 技能。
- **上传失败**：看 `<outdir>/.log-upload.txt`。
  - `403` + 响应体 `error code: 1010` = Cloudflare 拦截了非浏览器 UA；本技能的 `scripts/client.py` 已使用浏览器 UA，若仍出现请更新本技能并检查网关配置。
  - `401` = key 无效/缺失；确认 `AIHUB_API_KEY` 可被内置上传器读到（见「依赖」里 `--use-local-key` 的配置目录说明）。
  - `413` = 文件过大；脚本已缩到 ≤768px，正常不会触发。
  - `429` = 限流；已按 `Retry-After` / 指数退避自动重试 3 次仍未通过，稍后再跑。
  - `5xx` 或「上传结果不明」= 请求已发出但没拿到 URL，**文件可能已存入服务端**；脚本不会自动重传（避免产生重复对象），稍后重跑本次上传即可。
- **运行时依赖报错**（`运行时依赖不可用：import numpy/PIL.Image 失败`）：按报错里给出的 `rm -rf <skill>/.venv && uv sync --project <skill>` 重建环境；报错正文会带底层异常原文，便于判断是环境半残还是系统缺 uv。
- **基准生成就失败**：多半是 key/积分问题，看 `.log-base.txt`，按 image-2 的报错处理（401 key 无效 / 402 余额不足 / 429 限流）。
- **个别表情总失败**：手势类（OK/点赞/比心）偶尔不稳，可改 `--expressions` 换个描述单独补跑。
