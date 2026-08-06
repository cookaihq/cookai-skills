# pdf2markdown

把**一个** PDF 转成一份可复核的 Markdown 工作目录（work bundle）：冻结来源、逐页建立基线、经 Doc2X 转换、对全文做内容语义复核、只允许证据绑定的修正，最后选出一个本地 final Markdown 指针。全部测试已在 Python 3.9 上跑通；页渲染与结构检查依赖 PyMuPDF、Pandoc、BeautifulSoup4，`advance` 会检查它们的 API 是否可用，但**从不自动安装**——缺依赖记为 `recoverable_error / dependency_missing`，装好后用新返回的 generation 重跑同一条命令即可。

**适用**：转换结果需要被复核和追责的场景——正式文档、需要保留原始产物、需要说清「哪一处改了、依据是什么」、中途会中断需要续跑。

**不适用**：想要一条命令拿到 Markdown 就走。那种场景用 [pdf2md_docx](../pdf2md_docx/)。本 skill 的每一步都要求显式确认、写入历史、可回溯，流程明显更长。

## 它解决什么

一次性转换工具的问题不在转得准不准，而在**转完之后无从追责**：哪些页漏了、表格是不是被合并错了、公式有没有掉、某一处措辞是转换器改的还是原文如此——都答不上来。重转一次还要再付一次费。

所以这里不是一个转换命令，而是一个**带状态的工作目录**：

- **来源被冻结**：`source.pdf` 及其 SHA-256 是唯一事实来源，后续每一步都绑定它
- **原始产物不可变**：Doc2X 返回的 ZIP 经完整校验后原子采纳为 `03-converted/attempts/conversion-attempt-NNNN/`，此后只读；修正写在别处，原始基线永远留着
- **每一步都进 append-only 历史**：`.state/history.ndjson` 只追加不改写，任何一次崩溃都能从中间状态恢复到确定结果
- **付费动作必须显式授权**：上一次尝试出现提交结果不明、明确失败、结果数量异常、结果版式不可用时，**不会**自动重试——必须拿一个一次性 `action_id` 显式决策，避免重复扣费
- **修正必须有证据**：每处修改绑定到源页、Markdown 块、字节锚点和原文哈希；风格改写、总结、翻译、无依据补全一律拒绝

## 快速开始

```bash
# 1. 冻结来源，建立工作目录
python3 scripts/workflow.py start --source ./paper.pdf

# 2. 渲染每页 PNG 基线（需显式声明宿主能看图）
python3 scripts/workflow.py advance \
  --work-bundle <dir> --expected-generation <n> --visual-capability available

# 3. 逐页提交 preflight 结论（JSON 输入，schema 见 references/preflight-contract.md）
python3 scripts/workflow.py record preflight \
  --work-bundle <dir> --expected-generation <n> \
  --action-id <id> --evidence-hash <sha256> --input preflight-record.json

# 4. 之后的来源暂存、创建 Doc2X 任务、轮询、采纳结果，全部由 resume 推进
python3 scripts/workflow.py resume --work-bundle <dir> --expected-generation <n>

# 5. 采纳完成后开复核轮次，提交复核记录
python3 scripts/workflow.py resume \
  --work-bundle <dir> --expected-generation <n> --visual-capability available
python3 scripts/workflow.py record review \
  --work-bundle <dir> --expected-generation <n> \
  --action-id <id> --evidence-hash <sha256> --input review-record.json

# 随时只读查看状态
python3 scripts/workflow.py inspect --work-bundle <dir>
```

每个命令在 stdout 输出**恰好一个**带版本的 JSON 对象；stderr 只是诊断日志，不要从中推断成功。每次调用后保存返回的 `generation`、一次性 `action_id` 和 `evidence_hash`，下一条命令要用。

来源接受一个本地普通文件，或一个**无需鉴权的公开 HTTPS** PDF 链接（不接受符号链接、目录、特殊文件，也不接受带 userinfo / cookie / 自定义请求头的 URL）。

输出根目录按 `--output-dir` → `PDF2MARKDOWN_OUTPUT_DIR` → `$PWD/pdf2markdown-output` 顺序决定。

## 工作目录结构

```text
<work-bundle>/
├── manifest.json                 # 权威状态：generation、conversion_state、settings 快照、artifacts
├── .state/
│   ├── private.json              # 私有数据：临时上传 URL、结果 URL（不进 manifest、不进 stdout）
│   ├── history.ndjson            # append-only 事件历史，崩溃恢复的依据
│   └── lock                      # 写锁
├── 01-source/
│   ├── source.pdf                # 冻结的来源，SHA-256 即身份
│   └── source-inventory.json     # 非视觉证据：链接目标、表单值、旋转、图片、注释
├── 02-pages/                     # 每页一张无损全页 PNG（默认 300 DPI，可 72-600）
├── 03-converted/attempts/
│   └── conversion-attempt-NNNN/  # 每次尝试的 result.zip + 解压出的原始树，采纳后不可变
├── 04-review/                    # 复核证据、修正后 Markdown、diff、裁剪图
└── 05-published/                 # 预留；当前实现不做发布
```

不要直接编辑 `manifest.json`、`.state/private.json`、`.state/history.ndjson`——所有写入都必须经命令边界，否则下次 `inspect` 会以 `invalid_bundle`（退出码 4）拒绝。

## 配置

**交互模式**决定遇到需要拍板的岔口时怎么办：

```bash
python3 scripts/workflow.py settings init
python3 scripts/workflow.py settings set-mode confirm   # 默认：返回一次性动作，等显式决策
python3 scripts/workflow.py settings set-mode auto      # 自动接受 warning；但不明 / 失败结果仍然停住
```

`auto` 不等于全自动：它只自动接受 preflight warning，**不会**替你承担任何可能重复扣费的决定。

**发布模式**默认 `skip`。当前实现不制定也不执行发布计划，`publishing.mode=upload` 在没有完整 publisher 绑定时视为 blocked。

**非 Secret 值**按此顺序逐项独立解析（取首个非空）：命令行选项 → 进程环境变量 → `$PWD/.env.local` → `$PWD/.env` → 经显式授权的 home `.env` → `settings.json` → 内置默认。可用变量：`PDF2MARKDOWN_INTERACTION_MODE`、`PDF2MARKDOWN_PUBLISH_MODE`、`PDF2MARKDOWN_UPLOADER`、`PDF2MARKDOWN_UPLOAD_TARGET`、`PDF2MARKDOWN_OUTPUT_DIR`。dotenv 值按字面处理，不做 shell 展开。

**Secret**（`AIHUB_API_KEY`）只走：进程环境变量 → `$PWD/.env.local` → `$PWD/.env` → `~/.config/pdf2markdown/.env`（**仅在显式传 `--use-local-key` 时**）。该授权只对当前这一次调用生效，不会被保存、也不会被工作目录继承。不要把任何 key、凭证、签名 URL 放进 `settings.json`。

来源暂存与后续轮询会记录所用凭证的**位置和指纹**（不记录 key 本身）；401 之后不会回退到优先级更低的来源，轮询也不会改用另一个账户。

## 中断与续跑

`resume` 是唯一的推进命令，也是唯一的恢复命令——它按保存的状态判断该做什么，不需要你告诉它走到哪了：

```bash
python3 scripts/workflow.py resume --work-bundle <dir> --expected-generation <n>
```

- `--expected-generation` 传 `start` 或 `inspect` 最近返回的值。不匹配返回 `generation_conflict`（退出码 5），先 `inspect` 再重试
- `bundle_locked` 说明有别的写入者在跑，等它结束
- 默认从工作目录里冻结的设置快照恢复，**忽略**之后环境变量、dotenv、持久设置的漂移；显式覆盖会产生新 generation 并追加证据，旧历史不变
- 待处理的复核 / 修正 / 歧义动作**不会**被 `resume` 消费掉——必须走对应的 `record` 命令

**付费重试为什么要显式确认**：Doc2X 任务按次计费，而「提交结果不明」意味着上游可能已经建了任务并已计费。此时自动重试等于替用户承担一次未经同意的扣费。所以这些情况一律停住并返回一个绑定的一次性动作，只有 `record conversion --decision retry --basis <理由>` 能授权新的一次付费尝试；它追加一条新 attempt，绝不改写旧的。`auto` 模式下这些情况同样停住，不会有隐式的第二次扣费。

已经付过钱的产物不会因为后续步骤失败而丢弃：即使 ZIP 的 Markdown 版式不符合预期（`unexpected_result_layout`），通过安全校验的 ZIP 和原始树仍然被采纳保存，只是不声称 `raw_markdown` 产物。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 命令完成——看 `outcome` / `conversion_state` / `action_required` / `errors` 判断实际进展 |
| `2` | 命令参数错 |
| `3` | 来源不合格：给一个可解析的本地 PDF 或满足安全约束的公开 HTTPS PDF |
| `4` | 工作目录完整性 / schema 失败——停下来修复或恢复，不要绕过 |
| `5` | generation 过期或有并发写入者 |
| `6` | 持久设置无效或显式覆盖无效 |

## references

| 文档 | 管什么 |
|---|---|
| [api-guide.md](references/api-guide.md) | AIHub 来源暂存与 Doc2X 的上游合同：哪些是已验证的上游事实、哪些是本地的保守策略，以及结果采纳合同 |
| [preflight-contract.md](references/preflight-contract.md) | 页基线与 preflight：依赖用途、渲染与资源上限、source inventory 字段、Agent 提交记录的 schema |
| [review-contract.md](references/review-contract.md) | 复核与修正：复核证据格式、复核记录、歧义决策、修正记录——**构造任何复核 / 决策 / 修正输入前必读** |
| [security-limits.md](references/security-limits.md) | 公开 HTTPS 来源下载与 Doc2X 结果下载的安全边界与固定上限 |

## 安全边界

来源下载和结果下载走同一套保护：校验每一次重定向、所有解析出的 A/AAAA 地址、已连接的 TLS 对端，限制响应时间与字节数，校验 `application/pdf` 与 `%PDF-` 签名。全程不发送 `AIHUB_API_KEY`、Cookie、Referer、代理凭证或浏览器状态到来源地址。URL 的 query 和 fragment 按敏感数据处理：query 只发给已验证的目标，fragment 从不发送，落盘的只有去掉 query 的 URL 加上完整原始 URL 的 SHA-256。

ZIP 采纳只接受普通文件和目录、仅 stored / deflated 压缩。加密条目、符号链接、设备文件、绝对路径、逃逸路径、反斜杠、盘符、NUL 文件名、重复项、Unicode/casefold 冲突、文件与目录前缀冲突一律拒绝。从不调用 `extractall`；通过 no-follow 目录描述符以 `0700` / `0600` 创建，逐项校验 CRC、声明大小、EOF、文件哈希和整树哈希。

## 测试

```bash
python3 -m pytest pdf2markdown/tests -q
```

测试不读真实 key、不发网络请求。

## 范围边界

当前实现**不做**：制定或执行发布计划、调用外部图片上传器、生成引用在线资源的 Markdown。也不要在本流程内改用 `pdf2md_docx`、`upload-for-url` 或 `s3-upload` 替代其中任何一步。
