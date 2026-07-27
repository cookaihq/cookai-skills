---
name: s3-upload
description: Use when the user explicitly wants to persist one local file in their own AWS SigV4-compatible object store and receive an Object Reference plus a public or presigned current-key URL. Do not use for hosted temporary URLs, remote/base64 input, bucket administration, or an upload inferred only from a caller mapping.
---

# s3-upload

## Overview

把一个本地文件写入用户自己的 S3-compatible bucket。v2 使用完整、带作用域的 Upload Target 和 Credential Profile；对象引用是持久身份，URL 只是访问结果。

普通模式为 AWS S3、Cloudflare R2 和用户明确断言兼容的 custom endpoint 提供已有 baseline；也提供基于官方 endpoint/SDK 文档和离线向量实现、尚无完整 release evidence 的 `aliyun-oss` / `tencent-cos` experimental preset。OSS 只有单账号永久凭证的 bounded run，COS 尚无 live credential。两类 preset 当前都只执行单次 PutObject 与 private current-key presign；Public URL 只按用户声明的 HTTPS Public Base URL 本地构造。

## When to Use

- 用户明确要求把已经落地的本地文件持久保存到自己的 bucket。
- 用户需要 Object Reference，或需要 public/presigned current-key URL。
- 其他 Skill 已产生本地文件，且用户明确要求第二阶段持久上传。

不要用于：72 小时临时托管、远程 URL/base64/stdin、list/copy/move、建桶、ACL/策略管理，或仅因为项目存在 Skill Target Mapping 就自动上传。

## Critical Rules

- 先 `upload --dry-run --json`；默认 `collision=replace`，同 Object Key 会覆盖当前对象。aws-s3 / cloudflare-r2 baseline 另支持 `--collision reject`：条件 Put 携带 `If-None-Match: *` 实现原子 no-overwrite，撞 412 后经一次 presigned GET 全文比对，size+SHA-256 双等返回 `adopted`（`object_written=false`、退出码 0、不发第二次 Put），不等以 collision 退出码 4 结束。
- `ambiguous` 或 `partial_success` 的写入不得自动重放。保留并报告 `checkpoint_id`，只使用对应恢复命令；`put_unknown` 的 `reconcile` 是只读全文对账（零写请求），双等收敛为成功但 `object_written` 保持 `null`。
- `--json` 输出 17 键闭合 result（v1 13 键原地扩展 + `remote`/`checkpoint`/`next_action`/`retry_safety`），不适用的值为显式 `null`，不省字段；`--result-out <path>` 把同一 result JSON 原子写入 caller 指定文件（与 stdout 逐字节一致，preflight 与 `--reference-out` 同级、失败时零远端请求）。
- Public Base URL 是用户声明，不做 GET/HEAD 探测；Skill 不发送 public ACL，也不修改 bucket policy/lifecycle/CORS。
- Object Reference 中的 version id 只用于 exact-version delete 选择；所有 URL 都指向 current key。
- Global Target 的间接选择必须显式 `--use-local-key`。Object Reference/checkpoint 本身不授权读取 home 配置。
- stdout 在非 JSON 的成功 upload/url/resume 中只输出一个 URL；Secret、Authorization 和签名 URL 不进入 stderr、checkpoint 或 Object Reference。
- `aliyun-oss`、`tencent-cos` 的 required capability 在 dry-run 中显示为 `experimental`，不是 `enabled` 或 live-verified。必须先检查 exact endpoint、bucket、payload profile 和 capability state；不要改成 `custom` 绕过 provider contract。
- OSS/COS assisted setup 仍不可用；experimental 数据面 preset 不授权建桶、身份、公开策略、生命周期或 CORS 变更。

## Workflow

1. 保持 `$PWD` 为原项目根目录；本地文件可以位于其他目录。
2. 解析显式 `--target`、caller mapping 或项目 default，确认 dry-run 的 bucket、Object Key、Access、Retention 和 capability blockers。
3. 若 capability 为 `experimental`，向用户说明尚未完成 provider live 验证；用户已授权该写入后执行 upload。返回 URL，需要持久引用时使用 `--reference-out`，需要把 result JSON 交给另一个进程消费时使用 `--result-out`。
4. 对 durable partial/ambiguous 结果按 checkpoint 恢复，不重新发起整个生成或上传流程。

```bash
python3 scripts/upload.py upload \
  --file /absolute/path/report.pdf \
  --target project:documents \
  --dry-run --json

python3 scripts/upload.py upload \
  --file /absolute/path/report.pdf \
  --target project:documents \
  --reference-out ./report.object-reference.json \
  --json

python3 scripts/upload.py url \
  --reference-file ./report.object-reference.json \
  --json
```

Calling Skill 使用稳定 caller id。映射只选目的地，不触发上传：

```bash
python3 /path/to/s3-upload/scripts/upload.py upload \
  --file /absolute/output/cover.png \
  --caller-skill image-2 \
  --json
```

## Capability-Gated Commands

CLI 保留 `delete`、`resume`、`reconcile` 和 `abort` 的稳定 parser surface。conditional write（`ConditionalPutObject`）已在 aws-s3 / cloudflare-r2 baseline 启用（`custom` 与 OSS/COS 仍不启用）；`reconcile` 对 `put_unknown` checkpoint 的只读全文对账在 normal baseline 可用。所有 normal baseline（包括 OSS/COS experimental preset）仍没有启用 Delete 或 multipart 的远端合同，对应命令路径在 checkpoint/网络之前即被 blocked。dry-run 会返回完整 blocked plan；不得把“命令可解析”描述为“provider 已支持”。

## Configuration

具体项目/全局文件、完整 JSON、选择顺序、Skill mapping 和 v1 迁移见 [references/configuration.md](references/configuration.md)。Provider normal 支持边界见 [references/providers.md](references/providers.md)，结果/退出码见 [references/api-notes.md](references/api-notes.md)。

## Verification Checklist

- [ ] 持久上传来自明确请求，而不是 mapping 存在
- [ ] dry-run 的 Target、key、Access/Retention 与 capability 状态已核对
- [ ] `collision=replace` 的覆盖语义已说明；使用 `reject` 时 `adopted`（退出码 0）与 collision（退出码 4）两种结局已说明
- [ ] partial/ambiguous mutation 未被自动重放
- [ ] Secret 未进入输出或持久 artifact
- [ ] OSS/COS capability 已准确表述为 `experimental`，未写成 `enabled` / live-verified
- [ ] 未把 OSS/COS assisted setup 表述为可用
