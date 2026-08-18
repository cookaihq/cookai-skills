# preview-share

把本地 HTML 页面（连同它**相对引用的图片 / CSS / JS**）通过 FTP 上传到预览服务器，返回一个可直接打开、可分享的预览 URL。

> 适合：把本地做好的页面发到线上看效果 / 发给别人预览。
> 不适合：正式部署、`git push`、`npm publish`、对象存储 / CDN 上传。

## 它解决什么问题

HTML 很少是孤立文件——它通过相对路径引用图片、样式、脚本。如果只把 `preview.html` 单独传上去，浏览器仍按 `out/c1.png`、`css/style.css` 这些相对路径找资源，结果就是**图全裂、样式全丢**。

本 skill 的核心是**依赖扫描**：从入口文件出发，自动解析并递归收集它引用的本地资源，**保留相对目录结构**整体上传到一个唯一子目录下，使线上页面的相对引用全部正确解析。

```
本地                              远程（FTP 根目录下）
v1/                               <时间戳>-<标签>/
├── preview.html   ──上传──►       ├── preview.html
└── out/                          └── out/
    ├── c1.png                        ├── c1.png
    └── ...                           └── ...
```

## 快速使用

前置：配置好两个环境变量（见 [服务器端配置](#服务器端配置一次性) 与 [配置读取优先级](#配置读取优先级)）。最简单的方式是在仓库根目录建 `.env`：

```ini
# .env（已 gitignore，不要提交）
# 末尾的 /preview 是可选的命名空间子目录，名字可任意取，但两行必须一致（详见下文）
PREVIEW_SHARE_FTP=ftp://用户名:密码@FTP主机:21/preview
PREVIEW_SHARE_BASEURL=https://你的域名/preview
```

然后（`<skill>` = preview-share 目录路径，相对或绝对均可）：

```bash
# 标准用法：上传 HTML 预览（自动带上它引用的图片/CSS/JS）
uv run --project <skill> <skill>/scripts/upload.py /path/to/preview.html --label my-demo

# 先看清单和 URL，不真正上传（推荐上传前先跑一次）
uv run --project <skill> <skill>/scripts/upload.py /path/to/preview.html --label my-demo --dry-run
```

> **必须走 `uv run --project <skill>`，不要裸 `python3`**（工作区 ADR 0007）：本 skill
> 自带 `pyproject.toml` + `uv.lock`，由 uv 钉死解释器版本；`uv run` 首次运行会自动
> 在 `<skill>/.venv` 建好环境。忘了写也有兜底——`upload.py` 启动时会把进程 exec 回
> 该 venv，缺失则按 `uv.lock` 自动重建，只有 uv 本体缺失（需 >= 0.8）才报错停下。
> 注意 `.env` / `.env.local` 仍按**当前工作目录**读取，与 `--project` 无关。

输出（stdout 只打印最终预览 URL，便于复制 / 管道）：

```
https://你的域名/preview/20260530-022804-my-demo/preview.html
```

### 常用选项

| 选项 | 说明 |
|------|------|
| `entry`（位置参数） | 入口文件，其 URL 会被返回（如 `preview.html`） |
| `--label TEXT` | 子目录标签，最终子目录 = `{时间戳}-{标签}`，保证唯一、避免覆盖 |
| `--subdir NAME` | 直接指定远程子目录名（覆盖 时间戳-标签） |
| `--include PATH` | 额外强制包含的文件/目录（依赖扫描漏掉的资源，可重复） |
| `--no-scan` | 关闭依赖扫描，只传 entry 与 `--include`（用于单个独立文件，如一张图） |
| `--use-local-key` | 允许读取 `~/.config/preview-share/.env` |
| `--timeout N` | FTP 超时秒数（默认 300，大文件可调大） |
| `--dry-run` | 只解析并打印清单与预览 URL，不真正上传 |

### 更多例子

```bash
# 依赖扫描漏了某个目录（如字体/额外资源），手动补
uv run --project <skill> <skill>/scripts/upload.py /path/to/index.html --label site --include assets/fonts

# 单个独立文件（一张图），不需要扫描
uv run --project <skill> <skill>/scripts/upload.py /path/to/poster.png --label poster --no-scan

# 会话级临时注入凭证（最高优先级，不写入 .env）
PREVIEW_SHARE_FTP='ftp://u:p@host:21/preview' \
PREVIEW_SHARE_BASEURL='https://example.com/preview' \
uv run --project <skill> <skill>/scripts/upload.py /path/to/preview.html --label demo
```

### 依赖扫描覆盖范围

对 `.html / .htm / .css / .svg` 入口递归扫描，提取并跟随本地相对引用：

- HTML 属性：`src` / `href` / `poster` / `data-src` / `background`、`srcset`
- `<link>`（CSS）、`<script src>`、`<img>`、`<video poster>` 等
- CSS 中的 `url(...)`（含内联 `<style>` 与外链 `.css`，并递归进 `.css` 继续找）

自动跳过：`http(s)://`、协议相对 `//`、`data:`、`mailto:`、`tel:`、`javascript:`、`#`、`blob:`、绝对文件系统路径。扫不到但实际需要的资源用 `--include` 补；扫到多余的（同目录下没被引用的文件）不会被传。

## 配置读取优先级

读取 `PREVIEW_SHARE_FTP`、`PREVIEW_SHARE_BASEURL`，每个变量**独立**按以下顺序取「首个非空来源」（与仓库根 [`CLAUDE.md`](../CLAUDE.md) 的「Skills 配置读取优先级」通用约定一致）：

1. **进程环境变量**（本轮显式注入 `PREVIEW_SHARE_FTP=... uv run --project <skill> ...` 或已 `export`）
2. **`$PWD/.env.local`**（自动读，**不向上递归**）
3. **`$PWD/.env`**（自动读，**不向上递归**）
4. **`~/.config/preview-share/.env`**（**仅 `--use-local-key` 时读**，避免静默使用持久化凭证）

`.env` 解析为极简格式（非 shell）：`KEY=value` / `KEY="value"` / `KEY='value'`、`#` 注释行、空行；同名取最后一次；不支持 shell 展开 / 命令替换 / 续行。

## 服务器端配置（一次性）

预览服务器用宝塔 / aaPanel 这类面板建一个**纯静态站点**，并为它创建一个 **FTP 目录指向站点根目录**的 FTP 账号即可。下面以面板操作为例。

### 第 1 步：进入「网站」，点「添加站点」

![进入网站，添加站点](assets/image1.png)

### 第 2 步：填写站点信息

![添加站点弹窗](assets/image2.png)

按图中红箭头与字段填写：

| 字段 | 填写 | 说明 |
|------|------|------|
| **域名** | `preview.你的域名.com` | 替换成自己的域名，需解析到本服务器 |
| **根目录** | 如 `/www/wwwroot/preview_online` | 站点的 web 根目录（document root） |
| **FTP** | 选「**创建**」 | 建站同时创建 FTP 账号，**FTP 目录指向站点根目录** |
| **FTP 账号 / 密码** | 如账号 `preview` | 面板自动生成，记下来填进 `.env` |
| **数据库** | 不创建 | 纯静态预览用不到 |
| **PHP 版本** | 选「**纯静态**」 | 只托管静态文件，无需 PHP 运行时 |

> **关键点**：FTP 选「创建」后，面板提示「为站点创建一个对应 FTP 帐户，并且 FTP 目录指向站点所在目录」。这意味着 **FTP 账号登录后的根目录 = 网站的 web 根目录**——这正是预览能被访问到的前提。若 FTP 目录与 web 根不一致，文件能上传成功但 URL 会 404（见 [故障排查](#故障排查)）。

### 第 3 步：把凭证写进 `.env`

站点 + FTP 建好后，得到：FTP 主机（服务器 IP）、端口（默认 21）、FTP 账号、密码、站点域名。填入仓库根 `.env`：

```ini
PREVIEW_SHARE_FTP=ftp://preview:你的FTP密码@FTP主机IP:21/preview
PREVIEW_SHARE_BASEURL=https://preview.你的域名.com/preview
```

### 关于末尾路径：可选的预览命名空间子目录

上面两处末尾都带一段路径（示例用 `/preview`）。它是一个**可选的命名空间子目录**：把所有预览统一收纳到 web 根下的某个子目录里，避免一堆预览目录直接堆在站点根、和 `index.html` 等混在一起。

- 名字**可任意取**（`/preview`、`/p`、`/share` 等都行），只是个目录名，无特殊含义；
- `PREVIEW_SHARE_FTP` 末尾的路径 = 上传到「web 根/该子目录/」下；
- `PREVIEW_SHARE_BASEURL` 末尾的路径 = 该子目录对外的 URL 前缀；
- 也可以**都省略**，直接把预览放在 web 根。

**一致性规则（务必遵守）**：`PREVIEW_SHARE_BASEURL` 的路径必须与 `PREVIEW_SHARE_FTP` 的远程路径指向**同一个目录**。

| FTP 路径 | BASEURL | 上传到 | 访问 URL |
|----------|---------|--------|----------|
| `…:21/preview` | `https://域名/preview` | web根/preview/… | `https://域名/preview/…` ✅ |
| `…:21/` | `https://域名` | web根/… | `https://域名/…` ✅ |
| `…:21/preview` | `https://域名`（少了 `/preview`） | web根/preview/… | 脚本拼出 `https://域名/…` ❌ 404 |

两边的子目录要么都带、且名字相同，要么都不带；不一致就会上传成功但 URL 404。

## 故障排查

| 现象 | 原因 / 处理 |
|------|------------|
| 上传成功，但 URL 全部 **404** | ① FTP 目录 ≠ web 根目录：建站时 FTP 没选「创建」或账号根目录指错——确认 FTP 登录后能看到站点的 `index.html`。② `BASEURL` 与 FTP 远程路径不一致：见上面的一致性规则表。 |
| 上传**超时**（大文件） | 调大 `--timeout`（默认 300s）；并确认服务器**被动模式（PASV）数据端口**对客户端可达。瞬时故障脚本会自动重试 3 次（退避 1s、2s），stderr 打 `[retry]` 行。 |
| 报 `[ambiguous]` 无法查询远端状态 | 文件传到一半中断，且服务器不支持 `SIZE` 命令（或控制连接断开后重连失败），脚本无法确认远端是否已写完，按工作区 ADR 0006 规则 4 **不盲重试**。确认服务器状态后手工重跑。 |
| 页面打开但**图裂 / 样式丢** | 依赖扫描有 `[warn] 引用未找到`：相对引用在本地缺文件，或脚本没识别到——用 `--include` 补齐，或检查引用路径。 |
| 提示缺 `PREVIEW_SHARE_*` | 按 [配置读取优先级](#配置读取优先级) 在某一层设置；不要把凭证硬编码进脚本。 |
| 报 scheme 非 `ftp`（如 `sftp://`） | 当前脚本只支持普通 FTP，需扩展。 |

## 安全须知

- **`.env` 已 gitignore，绝不提交**；凭证只放本地 `.env` 或 `~/.config/preview-share/.env`。
- 脚本日志对 FTP 密码做掩码（`ftp://user:***@host`），完整凭证只用于建立连接。
- 预览 URL 是**公网可访问**的临时分享链接，不要上传敏感内容。
- 本 README 中截图仅作面板操作示意，其中出现的账号 / 密码请勿当作真实可用凭证。

## 目录结构

```
preview-share/
├── README.md          # 本文件
├── SKILL.md           # skill 定义（触发条件、工作流、给 Agent 的指引）
├── pyproject.toml     # uv 运行时环境（零第三方依赖，钉死 requires-python）
├── uv.lock            # 解析结果锁文件
├── scripts/
│   └── upload.py      # 上传脚本（依赖扫描 + 分层配置 + FTP 上传）
└── assets/
    ├── image1.png     # 面板：添加站点入口
    └── image2.png     # 面板：建站弹窗
```
