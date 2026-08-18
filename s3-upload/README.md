# s3-upload

把一个本地文件持久写入用户自己的 AWS SigV4-compatible object store，并返回严格 JSON Object Reference 与 public/presigned current-key URL。零第三方依赖，纯 Python 标准库实现。

运行时由 uv 管理（工作区 ADR 0007）：`pyproject.toml` 声明 `requires-python >= 3.9`，环境落 `<skill>/.venv`。所有命令写 `uv run --project <skill> <skill>/scripts/upload.py ...`（下文 `<skill>` = 本目录路径），**不要裸 `python3`**。忘了写也有兜底：三个入口（`upload.py`、`setup.py`、`run_oss_live_matrix.py`）启动时把进程 exec 回该 venv，缺失则按 `uv.lock` 自动重建，只有 uv 本体缺失（需 >= 0.8）才报错停下。项目配置 `.s3-upload/` 与 `.env.local` 仍按**当前工作目录**读取，与 `--project` 无关。

## Normal 支持范围

- AWS S3 (`aws-s3`)：单次 PutObject（含 `If-None-Match: *` 条件写入，支持 `collision=reject|unique`）、private presigned GET。
- Cloudflare R2 (`cloudflare-r2`)：单次 PutObject（含 `If-None-Match: *` 条件写入，支持 `collision=reject|unique`）、private presigned GET。
- `custom`：用户明确断言 exact endpoint 兼容同一单次 Put/presign 合同；条件写入不启用。
- 阿里云 OSS (`aliyun-oss`)：experimental 单次 PutObject、private current-key presign；`endpoint=null` 自动解析 `https://s3.oss-{region}.aliyuncs.com`，virtual addressing。
- 腾讯云 COS (`tencent-cos`)：experimental 单次 PutObject、private current-key presign；`endpoint=null` 自动解析 `https://cos.{region}.myqcloud.com`，bucket 必须是完整 `BucketName-APPID`，virtual addressing。
- Public Access：只从用户声明的 HTTPS Public Base URL 拼接，不修改 ACL/policy，不主动探测。
- Retention：结果保存声明值；lifecycle 执行始终是 `external-unverified`。

普通模式默认 `collision=replace`（同键覆盖）。aws-s3 / cloudflare-r2 baseline 已启用 conditional write：`--collision reject` 以 `If-None-Match: *` 原子 no-overwrite，撞 412 后经一次 presigned GET 全文比对，size+SHA-256 双等返回 `adopted`（`object_written=false`、退出码 0），不等以 collision 退出码 4 结束。`reconcile` 对 `put_unknown` checkpoint 的只读全文对账在 normal baseline 可用（零写请求）。Delete、multipart 和 assisted setup 仍是 capability-gated，当前 normal baseline 不可用。OSS/COS 的 `experimental` 表示代码依据官方 endpoint/SDK 资料和离线向量完成，但尚无足以晋级 `enabled` 的完整 release evidence；OSS 只有单账号永久凭证的 bounded run，COS 尚无 live credential。先 dry-run，不能描述成 `enabled`。

## 快速开始

先按 [配置说明](references/configuration.md) 创建完整的 scoped Credential Profile 与 Upload Target，然后从项目根目录执行：

```bash
uv run --project <skill> <skill>/scripts/upload.py upload \
  --file ./report.pdf \
  --target project:documents \
  --dry-run --json

uv run --project <skill> <skill>/scripts/upload.py upload \
  --file ./report.pdf \
  --target project:documents \
  --reference-out ./report.object-reference.json \
  --json

uv run --project <skill> <skill>/scripts/upload.py url \
  --reference-file ./report.object-reference.json \
  --json
```

非 JSON 的 upload/url 成功时 stdout 只有一个 URL。JSON 始终使用固定的 17 键闭合 result schema（v1 13 键原地扩展 + `remote`/`checkpoint`/`next_action`/`retry_safety`，不适用的值为显式 `null`）；`upload --result-out <path>` 把同一 result JSON 原子写入 caller 指定文件，与 stdout 逐字节一致，且第一次远端请求发出后不会再停留在 `not_started`（除确定性 4xx 外一律改写成带 checkpoint 的 `ambiguous`）。遇到 `partial_success` 或 `ambiguous` 时，不要重放 Put，保留 `checkpoint_id`，用 `reconcile --checkpoint <id>` 只读对账。

## 项目 Mapping

项目 `.s3-upload/config.json` 可以为不同 calling Skill 固定不同目的地：

```json
{
  "schema_version": 1,
  "default_target": "project:temporary-builds",
  "skill_targets": {
    "image-2": "project:website-images",
    "pdf2markdown": "global:shared-documents"
  }
}
```

只有明确的 Persistent Upload Request 才执行第二阶段。调用时保持 cwd 为原项目，传绝对本地路径：

```bash
uv run --project /path/to/s3-upload /path/to/s3-upload/scripts/upload.py upload \
  --file /absolute/output/cover.png \
  --caller-skill image-2 \
  --json
```

映射选择 Target，但不会自动上传，也不会让生成失败触发重新生成。

## CLI

```text
upload FILE/TARGET options    normal 单次上传；--collision reject / --result-out 见上；multipart 由 capability gate 控制
url REFERENCE|TARGET+KEY      零远端请求生成 current-key URL
delete                       capability-gated；normal baseline unavailable
resume                       capability-gated multipart recovery
reconcile                    put_unknown 的只读全文对账在 normal baseline 可用；其余路径 capability-gated
abort                        capability-gated multipart cleanup
```

运行 `uv run --project <skill> <skill>/scripts/upload.py --help` 查看稳定 parser surface。完整状态/退出码见 [API notes](references/api-notes.md)。

## 网络抖动处理（ADR 0006）

| 调用 | 语义 | 策略 |
|---|---|---|
| reconcile 的 HEAD、multipart 的 HEAD / ListParts | 读 | 瞬时传输故障与 5xx / 429 状态码均重试 3 次，退避 1s、2s（429 带 `Retry-After` 时遵循该值，钳到 60 秒）；确定性 4xx 立即定论；穷尽后传输故障仍返回「本次观测没有答案」落 `ambiguous`，5xx / 429 返回最后那个应答 |
| PutObject、DeleteObject、multipart 创建 / 分片 / 完成 / 中止 | 计费写 | **不重试**。传输异常置 `*_unknown` 检查点 + `ambiguous`，用 `reconcile` 判定是否真的写成，不要用「再跑一次」代替 |
| live evidence 采集（`live_adapter.py` / `evidence.py`） | 证据 | 按裁决豁免重试：`request_count` 是「一次逻辑操作实际发了几次物理请求」的精确证据，重试会破坏口径 |

「连接建立阶段失败（请求未发出）」没有从 ambiguous 里拆出来重试——这条链路的异常形态读不出失败发生在哪个阶段，理由写在 `scripts/s3.py` 的 `read_request_with_retry` 上方。`retry.part_max_attempts` / `retry.collision_max_attempts` 是跨 CLI 调用的业务级尝试上限，与这里的单次调用级重试无关，对照表见 [configuration.md](references/configuration.md#retry-是跨-cli-调用的尝试上限不是网络重试参数)。

## v1 迁移

无 subcommand 的旧 CLI、flat `S3_UPLOAD_ACCESS_KEY_ID`/bucket 字段合并、`--profile` 和 `set_profile.sh` 已移除。v2 不自动读取或迁移旧配置；必须先选择 project/global scope，再把 Secret 放进命名 Credential map，把位置和策略放进完整 Target。迁移例子见 [configuration.md](references/configuration.md#从-v1-flat-配置迁移)。

## 测试

```bash
uv run --project <skill> --group dev python -m pytest -q
uv run --project <skill> python -m compileall -q <skill>/scripts
```

CI 不读取真实 Key、不发云请求。OSS/COS live evidence 仍位于 maintainer/test surface；普通用户只能使用公开文档列出的 experimental 基础能力。
