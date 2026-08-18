---
name: template-preview
version: 1.0.0
description: v1.0.0｜Use when the user wants to turn a folder of images + copy into a styled showcase page that mimics a known app's UI — phrases like "把这个文件夹做成小红书预览"、"做成小红书个人主页"、"生成小红书风格展示页"、"把这些图做成 XX 风格的页面". v1 ships the xiaohongshu (小红书) personal-homepage template. Generates a self-contained output folder (index.html + assets/) that opens locally and can be uploaded by the preview-share skill. Do NOT use for real deploys, or for uploading/publishing (that's preview-share's job).
---

# template-preview

## Overview

把用户的内容（一组图片 + 文案）渲染成**指定模板风格的自包含展示 HTML 页**。交付**小红书**模板：

- **个人主页** `index.html`：头像 + 昵称 + 简介 + 关注/粉丝/获赞 + 笔记双列瀑布流；每张真实笔记卡可点击。
- **笔记详情页** `note-NN.html`（每条笔记一个）：横向轮播（多图）+ 作者行 + 标题 + 正文 + 互动栏；返回栏回主页。主页卡片点击即跳到对应详情页。

输出是一个自包含文件夹（`index.html` + 若干 `note-NN.html` + `content.json` + `assets/`），本地双击 `index.html` 即可点开浏览；所有图片/页面互链用相对路径，可被 `preview-share` 依赖扫描（沿 `href` 递归）整站上传 —— 移交时以 `index.html` 为入口即可。

**分工**：Claude 负责「理解」（读文件夹、看图、**推断笔记分组**、补齐标题/正文等内容字段）；薄脚本 `scripts/generate.py` 负责「机械活」（解析配置、建输出目录、拷素材、套模板渲染主页与各详情页）。

## When to Use

- 用户想把一个文件夹的图片+文案做成小红书风格的展示/预览页
- 用户说「做成小红书个人主页」「生成小红书风格页面」

## When NOT to Use

- 像素级 App 克隆（本 skill 只做「神似版式」）
- 发布/上传（那是 `preview-share` 的职责；本 skill 只生成）
- 正式部署到生产环境

## Config Reading（配置读取优先级）

每个变量独立按以下顺序取「首个非空来源」（详见仓库 `CLAUDE.md` 通用约定，本 skill **不读 `~/.config`**）：

1. 进程环境变量（本轮显式注入 `TPL_XHS_NICKNAME=... python3 ...` 或已 export）
2. `$PWD/.env.local`（自动读，不向上递归）
3. `$PWD/.env`（自动读，不向上递归）
4. 内置默认（模板级在 `templates/<t>/defaults.env` 与内置素材；skill 级写在 `generate.py`）

`.env` 解析与 `preview-share` 一致：极简、非 shell。

### 配置变量

skill 级（输出位置）：

| 变量 | 含义 | 默认 |
|------|------|------|
| `TPL_OUTPUT_ROOT` | 输出父目录（相对则相对 `$PWD`，也可填绝对路径） | 空 = 项目根 `$PWD` 本身 |
| `TPL_SUBDIR_PATTERN` | **自动命名**时的文件夹名（仅 `--name` 未给时用），占位符 `{date}`/`{time}`/`{label}` | `{date}-{time}-{label}` |

xiaohongshu 模板级（人设，前缀 `TPL_XHS_`，均有内置默认）：

| 变量 | 含义 |
|------|------|
| `TPL_XHS_NICKNAME` | 昵称 |
| `TPL_XHS_BIO` | 简介 |
| `TPL_XHS_RED_ID` | 小红书号 |
| `TPL_XHS_AVATAR` | 头像图路径（留空用内置默认） |
| `TPL_XHS_FOLLOWING` | 关注数 |
| `TPL_XHS_FOLLOWERS` | 粉丝数 |
| `TPL_XHS_LIKES` | 获赞与收藏数 |
| `TPL_XHS_FILLER_CARDS` | 填充卡素材目录（留空用内置默认） |
| `TPL_XHS_MIN_CARDS` | 网格补足到的卡片数（默认 6） |

## Workflow（照做）

1. 用户：「把 `<文件夹>` 做成小红书预览」。
2. 列目录、看图。
3. **推断笔记分组**（核心）：判断这个文件夹是「**1 篇多图**」还是「**多篇**」——
   - 线索：是否有明显**封面图** + 内页是否**同系列版式 / 带 01·02… 编号**（同系列 → 多半是 1 篇多图）；各图彼此独立、主题不同 → 多半是多篇。
   - **确认门槛**：若推断结果 **>1 篇，或分组有歧义**，**先停下**向用户列出「共几篇 / 每篇含哪些图（按顺序）/ 封面是哪张 / 拟用标题」，等用户确认或修正后再继续。单篇且无歧义可直接继续。
4. 补齐内容字段（**不在对话中追问，能自动就自动**）：
   - **标题/正文**：优先在文件夹内找文案文件（`.md`/`.txt` 等）取标题+正文；找不到则看图提炼。**仍取不到就留空** —— `generate.py` 会自动回落为固定文案「暂无标题」/「暂无正文」，**不要为此问用户**。
   - **人设**（昵称/头像/简介/数据）：**直接用配置或内置默认，不在对话中跟用户确认**。用户主动要改时才用 `TPL_XHS_*` 覆盖。
5. 写出 `content.json`（见下「数据契约」）：每条 note 带 `images[]`（按顺序，详情页轮播）与可选 `body`；路径用绝对或相对 `$PWD`。
6. **生成前用 AskUserQuestion 提示用户两件事**（这两个提示必须有；**不要**提示人设）：
   - **① 文件夹名**：默认/推荐项 = 自动命名 `{YYYYMMDD-HHMMSS}-{label}`（对齐 `preview-share` 的子目录规则，带时间戳、重跑不撞名，如 `20260530-213000-iot-power`）。也给用户「自定义干净名字」（如 `iot-power`）的选项。
   - **② 创建位置（根目录）**：默认/推荐项 = **当前项目根 `$PWD`**（Agent 运行所在目录）。允许用户填别的根目录。**默认绝不放到源图片文件夹的父目录或其它位置。**
   - 映射：自定义名 → `--name`；默认/自动命名 → **不传 `--name`**（让脚本按 `TPL_SUBDIR_PATTERN={date}-{time}-{label}` 生成，与 preview-share 同款）；自定义根 → `--out-root`。
7. 跑（默认/自动命名，落在 `$PWD`）：
   `python3 scripts/generate.py --template xiaohongshu --content content.json --label <label>`
   - 用户自定义了名字：加 `--name <文件夹名>`；自定义了根：加 `--out-root <根>`。
   - 先 `--dry-run` 看将生成的全部页面路径与素材清单，确认无误再正式生成。
8. 把脚本打印的**全部页面路径**（主页 + 各笔记页，每行一个，第一行为 `index.html`）交给用户。
9. **生成完成后，给一个「下一步」菜单**（不要只用一句弱文字，也**不要弹 AskUserQuestion 卡片**）。在对话里打印数字菜单 + 一条可复制提示词，让用户**回数字**或**复制提示词**触发：

   ```
   下一步要做什么？回复数字，或直接复制下面的提示词：
     1. 生成在线预览链接（整站上传，返回可分享 URL）
     2. 先看要传哪些文件（dry-run，只列清单不上传）
     3. 不用了，我就本地看

   可复制提示词（等价于选 1）：
     用 preview-share 上传这个文件夹做在线预览，入口文件是
     <绝对路径>/index.html（它会自动带上所有 note 页和图），label 用 <label>
   ```

   - 菜单里的 `<绝对路径>` / `<label>` 用**本次真实生成的值**填充（不是占位符），方便直接复制。
   - **回 `1`** → 调 `preview-share` skill，**以 `index.html`（第一行）为入口**上传；其依赖扫描会沿主页卡片 `href` 递归带上各笔记页与 `assets/`，**整站一并上传**；把同一 `--label` 透传过去。
   - **回 `2`** → 先 `preview-share --dry-run` 列清单 + 预览 URL，看完再问是否真传。
   - **回 `3`**（或不回） → 到此为止，**不擅自上传**（上传是对外发布动作）。
   - ⚠️ 提示词必须写明「**入口 index.html / 整站 / 自动带上 note 页和图**」——避免被误解成只上传单个 HTML 文件。绝不要改用某个 `note-NN.html` 当入口（那样会漏主页和其它笔记页）。

## content.json 数据契约

```json
{
  "label": "iot-power",
  "notes": [
    {
      "title": "笔记标题",
      "images": ["/abs/or/$PWD-relative/c1.jpg", "c2.jpg", "c3.jpg"],
      "body": "详情页正文（可空；空行分段）",
      "likes": 1234
    }
  ]
}
```

- `notes`：每个元素 = **一条笔记**（一个详情页 + 主页一张真实卡）；按顺序排在主页网格前部；作者头像/昵称统一用人设。
- `images`（**推荐**，非空数组）：详情页轮播的全部图，按顺序；`images[0]` 同时作为主页卡片封面。
- `cover`（向后兼容，可选）：旧字段，等价于 `images:[cover]`。`images` 与 `cover` **至少给一个**；都给时以 `images` 为准。
- `body`（可选）：详情页正文；空行（`\n\n`）分段，段内 `\n` 换行；缺省/空白则回落为固定文案「暂无正文」。
- `title` 缺省/空白则回落为固定文案「暂无标题」（仅真实笔记；填充卡标题为空时保持空）。
- `likes`（可选）：缺省脚本给确定性占位（重渲不跳数）。
- 路径：绝对路径直接用；相对路径以 `$PWD` 为基准。脚本把每条笔记的图拷进 `assets/`（命名 `note-NN-img-MM.<ext>`）并改为相对引用，扩展名保留。

> **单篇 vs 多篇**：`notes` 有几个元素就生成几个详情页。把一组「封面 + 多张内页」做成**一篇多图**时，应是**一个** note、其 `images` 列出全部图 —— 而不是把每张图拆成独立 note。

## Options（generate.py）

| 选项 | 说明 |
|------|------|
| `--template NAME` | 模板（v1 仅 `xiaohongshu`） |
| `--content PATH` | content.json 路径 |
| `--name TEXT` | **输出文件夹名**（leaf）；给定则用它命名，跳过 date-time 自动命名 |
| `--out-root DIR` | 输出**父目录**（相对则相对 `$PWD`）；优先级高于 `TPL_OUTPUT_ROOT`，默认 `$PWD` |
| `--label TEXT` | 子目录标签，`--name` 未给时参与 pattern 自动命名 |
| `--out DIR` | 直接指定**完整**输出目录（最高优先级，覆盖 root/name/pattern） |
| `--dry-run` | 只打印计划（含将生成的全部页面路径），不写盘 |

stdout 打印**全部页面绝对路径**，每行一个，**第一行 = `index.html`**（移交 `preview-share` 的入口）；其余为各 `note-NN.html`。日志/计划走 stderr。

## 与 preview-share 的关系

两个独立 skill：`template-preview` 生成自包含文件夹 → `preview-share` 扫描并上传。本 skill 自身不上传；移交是「询问 + 按需」。**以 `index.html` 为入口**：`preview-share` 的依赖扫描认 `href` 且对 `.html` 递归，会沿主页卡片 `href="note-NN.html"` 跟进、再扫各详情页里的图，**整站（主页 + 全部笔记页 + assets）一并上传**，无需逐页移交。真正的待传清单/URL 确认由 `preview-share` 自己的 dry-run 负责，移交时把同一 `--label` 透传过去。

## Pre-Response Checklist

- 是否先看了文件夹、**推断了笔记分组**（>1 篇或有歧义时先列分组让用户确认）
- 标题/正文是否尽量自动补齐（取不到留空交给固定回落，**没为此追问用户**）
- 是否**没有**在对话中跟用户确认人设（直接用配置/默认）
- 是否**生成前提示了用户「文件夹名 + 创建位置」**（推荐名 = `{时间戳}-{label}`，推荐位置 = `$PWD`）
- 输出是否默认落在 `$PWD`（项目根），**没有**放到源文件夹父目录或别处
- 是否先 `--dry-run` 看过清单（含全部页面路径）再正式生成
- 是否把**全部页面路径**（主页 + 各笔记页）交给用户
- 生成完成后是否给了**「下一步」数字菜单 + 可复制提示词**（路径/label 用真实值；不是文末一句弱文字）
- 是否**用户选了才**移交 `preview-share`（以 `index.html` 为入口、整站上传），没擅自上传
