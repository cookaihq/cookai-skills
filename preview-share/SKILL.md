---
name: preview-share
version: 1.0.0
description: v1.0.0｜Use when the user wants to put a local HTML page (or any local file) online for preview and get a shareable URL — phrases like "在线预览"、"传到预览服务器"、"生成预览链接"、"preview.html 打不开/想发出去看"、"把这个页面发上去看效果"、"上传到 FTP 看预览". The skill auto-detects the entry file's associated assets (images/CSS/JS referenced by relative paths) and uploads them together so the online page renders correctly. Do NOT use for production deploys, npm publish, git push, or uploading to non-preview destinations.
---

# preview-share

## Overview

把本地文件（通常是一个 HTML 页面）连同它**关联引用的资源**通过 FTP 上传到预览服务器，返回一个可直接打开的预览 URL。

核心价值在于**依赖扫描**：HTML 很少是孤立文件，它通过相对路径引用图片、CSS、JS。本 skill 从入口文件出发，自动解析并递归收集这些本地引用，保留相对目录结构整体上传到一个唯一子目录下，使线上页面的相对引用全部正确解析——**绝不只上传单个孤立文件**。

上传目标与对外 URL 由两个配置决定：

- `PREVIEW_SHARE_FTP`：完整 FTP URL（`ftp://user:pass@host:port/remote/base/path`）
- `PREVIEW_SHARE_BASEURL`：FTP 远程根路径对外暴露的基础 URL

两者读取遵循仓库 `CLAUDE.md`「Skills 配置读取优先级（通用约定）」。

## When to Use

- 用户想把本地 HTML 页面发到线上看效果 / 发给别人预览
- 用户说 "preview.html 在本地打开图能显示，想传上去" —— 正是依赖扫描要解决的场景
- 用户想为某个本地文件生成一个临时可访问的 URL

## When NOT to Use

- 正式部署、发布到生产环境（这不是部署工具，预览 URL 是临时分享用途）
- `git push`、`npm publish`、对象存储/CDN 上传
- 上传到非预览服务器的其它目的地

## CRITICAL

- **必须整体上传关联资源，不能只传入口文件**。默认开启依赖扫描；只有用户明确要传单个独立文件（如一张图）时才用 `--no-scan`。
- 上传是**对外发布**动作（文件会出现在公网可访问的预览服务器上）。首次为某入口上传前，向用户确认要上传的文件清单与目标 URL；可先用 `--dry-run` 给用户看清单。
- **凭证绝不回显**：FTP 密码在任何日志中都掩码为 `ftp://user:***@host`，完整凭证只用于建立 FTP 连接。
- 远程子目录用 `{时间戳}-{标签}` 命名以**保证唯一、避免覆盖**他人已有预览。入口文件与资源**保持原文件名和相对路径**（改名会破坏 HTML 里的相对引用）。
- 不要把同一份内容在同一轮对话里重复上传；内容有实质变化（改了页面/资源）才重新上传。

## Config Reading（配置读取优先级）

读取 `PREVIEW_SHARE_FTP`、`PREVIEW_SHARE_BASEURL`，每个变量独立按以下顺序取「首个非空来源」（详见仓库 `CLAUDE.md` 通用约定）：

1. 进程环境变量（本轮显式注入 `PREVIEW_SHARE_FTP=... python3 ...` 或已 `export`）
2. `$PWD/.env.local`（自动读，**不向上递归**）
3. `$PWD/.env`（自动读，**不向上递归**）
4. `~/.config/preview-share/.env`（**仅 `--use-local-key` 时读**，避免静默使用持久化凭证）

`.env` / `.env.local` 解析规则（极简，非 shell）：支持 `KEY=value` / `KEY="value"` / `KEY='value'`、`#` 注释行、空行；同名取最后一次；**不支持** shell 展开 / 命令替换 / 续行。

## Label（标签）建议

子目录命名为 `{YYYYMMDD-HHMMSS}-{标签}`。调用脚本前，**先分析要预览的内容，给用户推荐几个简短标签（英文短横线命名或数字编号），并允许用户自定义**。例如内容是「IoT 功耗自动化测试」可推荐 `iot-power`；若目录/任务自带编号（如 `2772`）也可直接用。用户确认后用 `--label` 传入。

## Workflow

1. **定位入口文件**：确认要预览的入口（通常是 `preview.html` / `index.html`）。
2. **预扫描（dry-run）**：`--dry-run` 跑一次，向用户展示「待上传文件清单 + 远程路径 + 预览 URL」。若清单里有 `[warn] 引用在本地未找到`，提醒用户线上会裂图，确认是否补齐或忽略。
3. **建议标签并确认**：按上节推荐标签，等用户确认（或用户已指定）。
4. **正式上传**：去掉 `--dry-run` 执行。脚本逐文件建远程目录并上传，结束打印预览 URL（stdout 仅输出该 URL，便于复制/管道）。
5. **回报**：把预览 URL 交给用户。提醒预览为临时分享用途。

## Options

| 选项 | 说明 |
|------|------|
| `entry`（位置参数） | 入口文件，其 URL 会被返回（如 `preview.html`） |
| `--label TEXT` | 子目录标签，子目录 = `{时间戳}-{标签}` |
| `--subdir NAME` | 直接指定远程子目录名（覆盖 时间戳-标签） |
| `--include PATH` | 额外强制包含的文件/目录（依赖扫描漏掉的资源，可重复） |
| `--no-scan` | 关闭依赖扫描，只上传 entry 与 `--include`（用于单个独立文件） |
| `--use-local-key` | 允许读取 `~/.config/preview-share/.env` |
| `--timeout N` | FTP 超时秒数（默认 300，大文件可调大） |
| `--dry-run` | 只解析并打印清单与预览 URL，不真正上传 |

## 依赖扫描覆盖范围

脚本对 `.html/.htm/.css/.svg` 入口递归扫描，提取并跟随本地相对引用：

- HTML 属性：`src` / `href` / `poster` / `data-src` / `background`、`srcset`
- `<link>`（CSS）、`<script src>`、`<img>`、`<video poster>` 等
- CSS 中的 `url(...)`（含内联 `<style>` 与外链 `.css`，并递归进 `.css` 继续找）

自动跳过：`http(s)://`、协议相对 `//`、`data:`、`mailto:`、`tel:`、`javascript:`、`#`、`blob:`、绝对文件系统路径。扫描不到但实际需要的资源用 `--include` 补；扫到多余的（如同目录下没被引用的文件）不会被传。

## Examples

```bash
# 标准用法：上传 HTML 预览（自动带上引用的图片/CSS/JS）
python3 scripts/upload.py /path/to/preview.html --label iot-power

# 先看清单和 URL，不上传
python3 scripts/upload.py /path/to/preview.html --label demo --dry-run

# 依赖扫描漏了某个目录（如字体/额外资源），手动补
python3 scripts/upload.py /path/to/index.html --label site --include assets/fonts

# 单个独立文件（一张图），不需要扫描
python3 scripts/upload.py /path/to/poster.png --label poster --no-scan

# 用持久化凭证（~/.config/preview-share/.env）
python3 scripts/upload.py /path/to/preview.html --label demo --use-local-key

# 会话级注入凭证（最高优先级）。
# 末尾的 /preview 是「可选的命名空间子目录」——把预览统一收在 web 根的一个子目录下；
# 名字可任意取，但 FTP 远程路径与 BASEURL 必须用同一个（也可都省略，直接用 web 根）。
PREVIEW_SHARE_FTP='ftp://u:p@host:21/preview' \
PREVIEW_SHARE_BASEURL='https://example.com/preview' \
python3 scripts/upload.py /path/to/preview.html --label demo
```

## Error Handling

- 缺 `PREVIEW_SHARE_FTP` / `PREVIEW_SHARE_BASEURL`：按优先级提示用户在哪一层配置；不要臆造凭证。
- 入口文件不存在：提示用户核对路径。
- FTP 登录失败（认证/权限）：检查凭证是否正确、账号是否有写权限。
- 上传超时：增大 `--timeout`；若仍失败，确认网络与服务器被动模式数据端口是否可达。
- `[warn] 引用未找到`：相对引用在本地缺文件，线上会裂图——补齐文件或用 `--include`，或与用户确认忽略。
- scheme 非 `ftp`（如 `sftp://` / `ftps://`）：当前脚本只支持普通 FTP，需扩展。

## Pre-Response Checklist

最终响应前自检：

- 是否默认做了依赖扫描、整体上传关联资源（而非只传单文件）
- 是否对 FTP 凭证做了掩码、未回显密码
- 上传前是否（通过 dry-run 或清单）让用户知晓将要发布的文件与目标 URL
- 子目录是否用 `{时间戳}-{标签}` 保证唯一、文件是否保留原名与相对路径
- 是否把可打开的预览 URL 明确交给用户

## Directory

- `SKILL.md`
- `README.md`（人类向文档：用法 + 服务器端配置 + 故障排查）
- `scripts/upload.py`
- `assets/image1.png`、`assets/image2.png`（README 用的面板配置截图）
