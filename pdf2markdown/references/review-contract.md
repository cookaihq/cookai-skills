# 逐页核验输入

CLI 返回的 `action` 包含一个核验单元：页码、完整 PNG 页图路径、Markdown 块 ID 与片段、相邻页边界信息。`source_inventory` 是源结构清单，`direct_markdown` 是完整直转稿。

宿主必须读取图片并检查整个页面，不能仅对几个文字片段做自动比对。Markdown 块的页分配是候选位置；必要时从完整 PDF 和直转稿查找实际位置。没有分配到 Markdown 块的页也需要核验，不能自动视为空白页。

记录文件是一个 JSON 对象：

```json
{
  "page_complete": true,
  "reviews": [
    {
      "block_id": "从请求复制的块ID",
      "verdict": "altered",
      "evidence_page": 1,
      "evidence_bbox": [0.1, 0.1, 0.9, 0.3],
      "replacement_text": "# Voltage: 5 V"
    }
  ],
  "page_findings": []
}
```

上例仅表示格式：假定当前请求为第 1 页，原 PDF 对应区域写着 `Voltage: 5 V`，直转稿错误写成 `Volatge: 5 V`。实际运行必须使用返回的块 ID、实际页码和查证后的区域，不能照抄示例作为证据。

每个请求块恰好有一条记录，字段必须完整：

| verdict | 含义 | replacement_text |
|---|---|---|
| `match` | 与原文一致；复核时表示保留第一次修订 | `null` |
| `altered` | 原文被错写，整块替换成证据支持的 Markdown | 必须是字符串 |
| `missing` | 当前块遗漏了原文内容，整块替换补齐 | 必须是字符串 |
| `extra` | 原文没有这段，当前流程保留并列为未解决事项 | `null` |
| `unreadable` | 无法确认原文，保留并列为未解决事项 | `null` |

`altered/missing` 必须提供 `evidence_page` 和归一化区域 `[x0,y0,x1,y1]`，值在 0 到 1 内且区域非空；证据来自本请求页或请求提供的相邻页。其他判断的证据字段可以为 `null`。替换上限是 65,536 UTF-8 字节，不允许 HTML、脚本、URL 或图片语法；这些无法通过普通文字替换修复的问题记入 `page_findings`。

`page_findings` 是非空说明字符串的列表。用于整块／整页缺失、顺序无法确认、图片或公式无法修复等问题。能够用已有块的证据替换修正时，应先完成修正。不能修正时清楚说明源页位置与影响，不能凭空新增内容。

每页首轮结束后，程序保存判断；只有首轮出现替换的页才再次核验。复核请求只包含被替换的块，片段是第一轮替换文本。复核 `match` 必须保留第一次修订。每页最多两轮，全任务最多 4,000 个单元。终轮再次替换时，最终文字尚未再次核验，报告 `final_revision_needs_review`，交付为 `needs_attention`。每轮输入均保存在报告的 `responses` 中。

`action_id` 同时绑定冻结源、直转稿、当前单元与累积核验状态。重复或过期记录被拒绝；重新 `resume` 获取当前请求。检查点保存已完成的单元，跨进程恢复不丢失前面的核验。

全部核验后程序一次性应用替换，原始直转稿保持不变。未解决的 `extra/unreadable`、页级问题和疑似漏图写入 `review.json` 与最终 `warnings`，结果为 `needs_attention`。
