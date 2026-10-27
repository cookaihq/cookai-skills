# AIHub 请求与恢复

当前实现沿用已有 AIHub Doc2X 请求合同：

| 调用 | 输入／行为 |
|---|---|
| `POST https://api.aihubmax.com/v1/files/upload/stream` | multipart 上传冻结 `source.pdf`，`auto_cleanup=false`，Bearer `AIHUB_API_KEY`；不跟随重定向、不自动重放。 |
| `POST https://api.aihubmax.com/v1/run/generations` | `model=doc2x-v3`、`pdf_url`、本机读取的 `page_count`、`convert_mode=md`、`formula_mode=dollar`、按源哈希命名的 `filename`、`merge_cross_page_forms`。 |
| `GET https://api.aihubmax.com/v1/tasks/{id}?sync_upstream=true` | 只查询已保存的任务 ID，Key 必须与上传时的来源及指纹一致。 |
| 结果 ZIP 与图片 GET | 公开 HTTPS 下载，不发送 API Key；ZIP 解压前检查路径、文件类型和资源上限。 |

跨页表合并开关来自源结构清单：相邻两页的表格分别到达前页末端和后页开头才启用。阈值与证据由脚本计算并保存，不由 Agent 猜参数。

创建请求前，程序先把已尝试提交的状态持久化。无法得到有效任务 ID 时返回 `submission_unknown`，后续 `resume` 不能再次 POST。有效任务至少验证 `id`、`model`、`type=document` 与状态；完成结果必须有唯一 ZIP URL。

轮询每个调用窗口最多 120 次 GET、600 秒，间隔至少 5 秒。瞬时网络错误、429、5xx 在窗口内重试；429 的 `Retry-After` 最多等待 60 秒。窗口耗尽可恢复同一个任务，不能据此判定任务失败并重新付费。下载默认总尝试 3 次，退避 1、2 秒，确定性 4xx 立即停止。

上传后的 72 小时是沿用的本地有效期限制，只在尚未创建任务时检查。结果 ZIP URL 不推测到期时间。真实上游 URL 只保存在私有检查点，命令结果不输出。

源 PDF 上限 256 MiB、2,000 页；每页按 144 DPI 渲染，最长边 8,192 像素、面积 40,000,000 像素。ZIP 压缩上限 512 MiB、展开上限 768 MiB、单成员 256 MiB、展开比 200:1。完整常量见 `scripts/pdf2markdown/sourcefreeze.py` 与 `zipsafe.py`。

API 资料入口：[AIHub 文档](https://docs.aihubmax.com/)。具体参数和响应判定以本 Skill 已实现的请求结构为准；不把 Doc2X 原生 API 与 AIHub 代理 API 混用。
