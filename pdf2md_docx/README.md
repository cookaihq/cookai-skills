# pdf2md_docx

通过 aihubmax.com 的 **Doc2X V3** 接口把 PDF 转成 **Markdown / LaTeX / DOCX**（支持公式识别、跨页表格合并）。结果是 ZIP，脚本自动下载并**解压到带日期时间前缀的文件夹**。

```bash
# 本地 PDF → Markdown（自动统计页数 + 上传换 URL + 解压到 {YYYYMMDD-HHMMSS}-{标签}/）
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py --pdf ./report.pdf

# 转 DOCX，合并跨页表格
AIHUB_API_KEY='sk-xxx' python3 scripts/convert.py --pdf ./report.pdf --convert-mode docx --merge-cross-page-forms
```

- 异步任务：创建 → 轮询 `/v1/tasks/{id}?sync_upstream=true` → 下载 ZIP → 解压
- `--convert-mode` 每次一种（`md`/`tex`/`docx`，默认 `md`），多格式＝多次调用、各扣一次积分
- 本地 `--pdf` 自动统计页数并上传；远程 `--pdf-url` 须配 `--page-count`
- 结果 ZIP 链接 **24h 过期**；输出 stdout 为解压文件夹路径
- 鉴权与 key 分层读取见 `SKILL.md`；字段 / 错误码见 `references/api-guide.md`

详见 [SKILL.md](SKILL.md)。
