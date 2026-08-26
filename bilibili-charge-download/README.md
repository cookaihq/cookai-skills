# B 站充电视频下载 Skill

使用用户已经登录的浏览器会话，下载并完整校验该账号有权观看、且用户有权下载和保存的 B 站充电视频。Skill 不读取或导出 Cookie，不绕过充电、登录、验证码、试看限制或平台对下载和保存的限制。

本 Skill 同时面向 Codex 与 Claude Code。安装后可用自然语言触发；显式调用时，Codex 使用 `$bilibili-charge-download`，Claude Code 使用 `/bilibili-charge-download`。

## 使用条件

- [`uv`](https://docs.astral.sh/uv/) >= 0.8。Skill 的 Python 版本由 `.python-version` 固定为 3.13，环境由 `pyproject.toml` + `uv.lock` 重建到 Skill 自己的 `.venv`。
- 系统中可正常执行 `ffmpeg` 和 `ffprobe`。
- 可以访问 B 站页面及其媒体 CDN。
- 浏览器工具已经连接到用户指定的已登录浏览器，并且能够执行页面 JavaScript。
- 浏览器工具的完整能力文档明确支持把 JavaScript 生成的 manifest 写到调用方指定的本地路径，同时不把文件正文序列化进工具结果。
- 磁盘空间足以同时保存下载分片和最终 MP4。

用户不需要提供 Cookie、`SESSDATA`、浏览器配置目录或 CDN 媒体地址。签名媒体 URL 只存在于浏览器写出的权限受限 manifest 和下载器进程内存中。

## 授权要求

开始前，用户必须明确确认三件事：

1. 要下载指定视频。
2. 指定浏览器中的账号能够完整播放目标视频，至少可以 seek 或播放到接近结尾。
3. 用户有权下载并保存该内容。

仅提供 URL、浏览器已登录、页面返回 `code = 0` 或两个时长一致，都不能替代以上确认。Skill 不会自动购买、登录、处理验证码，也不会把试看内容作为完整结果。

## 使用示例

Codex：

```text
使用 $bilibili-charge-download，通过当前浏览器登录状态下载这个视频：
https://www.bilibili.com/video/BVxxxxxxxxxx/

该账号可以完整播放到接近结尾，我有权下载并保存该内容。
```

Claude Code：

```text
使用 /bilibili-charge-download 下载这个已获授权保存的充电视频：
https://www.bilibili.com/video/BVxxxxxxxxxx/

使用当前已登录浏览器；该账号可以 seek 或播放到接近结尾。
```

这些句子不是固定口令。对话前文已经明确三项授权条件时，不需要重复确认。

## 执行过程

1. 选定用户指定的浏览器后，先读取该浏览器工具的完整能力文档；只有文档明确支持把结果直接写入调用方指定路径、且工具结果不包含文件正文时才继续。
2. 本地先以 `umask 077` 创建唯一的 `0700` 临时目录，并把其中固定的 `manifest.json` 路径交给浏览器工具。禁止写入默认 Downloads 目录。
3. 在用户指定的浏览器中打开目标视频页面。
4. 检查页面登录状态、可见的试看提示，并由用户确认可以完整播放，至少可以 seek 或播放到接近结尾。
5. 在页面 JavaScript 中定位并解析 `window.__playinfo__`，确认响应成功且存在视频流和音频流。
6. 默认选择最高可用清晰度，并在该清晰度优先选择 H.264/AVC；音频选择最高带宽项。
7. 浏览器工具把 manifest 直接写入指定的私有目录，只向模型返回路径和不含媒体 URL 的摘要；不得使用会把 evaluate 结果序列化进工具响应的接口。
8. 写入后立即把 manifest 权限设为 `0600`；`chmod` 失败时停止。下载器还会独立拒绝组或其他用户可读的 manifest。
9. 下载器使用 HTTP Range 下载音视频分片，复用已经完整写入的分段，并使用 `ffmpeg` 封装 MP4。
10. 下载器使用 `ffprobe` 核对时长、分辨率、音视频流和数据包，执行完整 demux 扫描并在五个时间点解码视频帧。
11. 验证通过后计算 SHA-256，删除 manifest 和已完成的下载工作目录，再报告结果。

manifest 正文、`url` 和 `backup_urls` 不得出现在模型输出、终端命令参数或进程列表中。

## 下载结果

默认输出位置：

```text
downloads/<BVID>/<BVID>-<视频高度>p.mp4
```

成功状态只有两种：

- `downloaded`：本次完成下载、封装和验证。
- `existing_verified`：目标文件已存在，本次重新验证通过，没有覆盖原文件。

Agent 应报告最终路径、文件大小、时长、视频编码、分辨率、音频编码和 SHA-256。其他状态均不能报告为成功。

## 登录、权限或下载失败

| 情况 | 结果 |
| --- | --- |
| 浏览器工具文档没有明确保证可以写入调用方指定路径，或会把写入内容放进工具结果 | 在读取 `window.__playinfo__` 前停止；不输出 manifest 或媒体 URL。 |
| 浏览器没有登录 B 站 | 停止并请用户在该浏览器中登录；不读取 Cookie，不自动登录。 |
| 用户没有确认能完整播放到接近结尾，或没有确认有权下载并保存 | 停止，不读取播放地址。 |
| 页面中没有 `window.__playinfo__` | 在同一标签页刷新一次；仍不存在时停止，不猜测接口响应。 |
| 临时播放地址过期 | 从仍处于授权状态的页面重新提取一次 manifest，再复用已验证分片继续下载。 |
| 网络中断 | 保留完整分片；不完整分片从该 byte range 起点重新下载。 |
| MP4 验证失败 | 不发布为成功文件；保留诊断文件和下载工作目录。 |

## 授权校验限制

页面登录状态、可见的试看提示、播放响应状态、音视频流存在，以及 `<video>` 时长与 `data.timelength` 一致，只能证明当前页面状态和两个运行时值彼此一致。如果页面和播放响应同时给出相同的试看时长，这些检查不能独立证明账号获得了完整播放权限。

因此，用户必须先在指定浏览器中确认视频可以完整播放，至少能够 seek 或播放到接近结尾，并确认自己有权下载和保存。Skill 不得仅凭页面检查结果推断用户已经付费或拥有保存权限。

## 维护与测试

下载失败时先按浏览器授权、播放响应、range 下载、字节校验、mux、媒体验证的顺序定位，不要关闭仍处于登录状态的视频页面。浏览器提取规则见 [references/browser-extraction.md](references/browser-extraction.md)，已验证的技术故障结论见 [references/retrospective.md](references/retrospective.md)。

测试使用合成媒体、本地 HTTP 服务和合成 BVID，不需要 B 站 Cookie：

```bash
SKILL_DIR="<absolute path to this skill directory>"
uv run --project "$SKILL_DIR" python "$SKILL_DIR/tests/test_download_media.py"
```

真实 B 站下载仍然需要用户逐次提出下载请求并满足本页的授权要求。
