# pdf2md_docx

通过 aihubmax.com 的 **Doc2X V3** 接口把 PDF 转成 **Markdown / LaTeX / DOCX**（支持公式识别、跨页表格合并）。结果是 ZIP，脚本自动下载并**解压到带日期时间前缀的文件夹**。

`<skill>` = 本目录路径（相对或绝对均可）：

```bash
# 本地 PDF → Markdown（自动统计页数 + 上传换 URL + 解压到 {YYYYMMDD-HHMMSS}-{标签}/）
AIHUB_API_KEY='sk-xxx' uv run --project <skill> <skill>/scripts/convert.py --pdf ./report.pdf

# 转 DOCX，合并跨页表格
AIHUB_API_KEY='sk-xxx' uv run --project <skill> <skill>/scripts/convert.py --pdf ./report.pdf --convert-mode docx --merge-cross-page-forms
```

> **必须走 `uv run --project <skill>`，不要裸 `python3`**（工作区 ADR 0007）：本 skill
> 自带 `pyproject.toml` + `uv.lock`，由 uv 钉死解释器版本，`uv run` 首次运行会自动在
> `<skill>/.venv` 建好环境；忘了写也有兜底——`convert.py` 启动时把进程 exec 回该 venv，
> 缺失则按 `uv.lock` 自动重建。自动统计本地 PDF 页数需可选依赖：
> `uv sync --project <skill> --extra page-count`（不装就按提示传 `--page-count <N>`）。

- 异步任务：创建 → 轮询 `/v1/tasks/{id}?sync_upstream=true` → 下载 ZIP → 解压
- 网络抖动按 ADR 0006 处理：轮询 / 下载是幂等读，瞬时故障重试 3 次且不击穿轮询总预算；创建任务 / 上传是计费写，只在「确定没被服务端处理」（429、DNS 失败、连接被拒）时重试，请求发出后的超时以退出码 4 报 ambiguous 并给查询指引
- `--convert-mode` 每次一种（`md`/`tex`/`docx`，默认 `md`），多格式＝多次调用、各扣一次积分
- 本地 `--pdf` 自动统计页数并上传；远程 `--pdf-url` 须配 `--page-count`
- 结果 ZIP 链接 **24h 过期**；输出 stdout 为解压文件夹路径
- 鉴权与 key 分层读取见 `SKILL.md`；字段 / 错误码见 `references/api-guide.md`

详见 [SKILL.md](SKILL.md)。
