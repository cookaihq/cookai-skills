# s3-upload

把一个本地文件持久写入用户自己的 AWS SigV4-compatible object store，并返回严格 JSON Object Reference 与 public/presigned current-key URL。Python 3.9+ 标准库实现。

## Normal 支持范围

- AWS S3 (`aws-s3`)：单次 PutObject、private presigned GET。
- Cloudflare R2 (`cloudflare-r2`)：单次 PutObject、private presigned GET。
- `custom`：用户明确断言 exact endpoint 兼容同一单次 Put/presign 合同。
- 阿里云 OSS (`aliyun-oss`)：experimental 单次 PutObject、private current-key presign；`endpoint=null` 自动解析 `https://s3.oss-{region}.aliyuncs.com`，virtual addressing。
- 腾讯云 COS (`tencent-cos`)：experimental 单次 PutObject、private current-key presign；`endpoint=null` 自动解析 `https://cos.{region}.myqcloud.com`，bucket 必须是完整 `BucketName-APPID`，virtual addressing。
- Public Access：只从用户声明的 HTTPS Public Base URL 拼接，不修改 ACL/policy，不主动探测。
- Retention：结果保存声明值；lifecycle 执行始终是 `external-unverified`。

普通模式只执行 `collision=replace`。Delete、multipart、conditional write、HEAD reconciliation 和 assisted setup 都是 capability-gated，当前 normal baseline 不可用。OSS/COS 的 `experimental` 表示代码依据官方 endpoint/SDK 资料和离线向量完成，但尚无足以晋级 `enabled` 的完整 release evidence；OSS 只有单账号永久凭证的 bounded run，COS 尚无 live credential。先 dry-run，不能描述成 `enabled`。

## 快速开始

先按 [配置说明](references/configuration.md) 创建完整的 scoped Credential Profile 与 Upload Target，然后从项目根目录执行：

```bash
python3 scripts/upload.py upload \
  --file ./report.pdf \
  --target project:documents \
  --dry-run --json

python3 scripts/upload.py upload \
  --file ./report.pdf \
  --target project:documents \
  --reference-out ./report.object-reference.json \
  --json

python3 scripts/upload.py url \
  --reference-file ./report.object-reference.json \
  --json
```

非 JSON 的 upload/url 成功时 stdout 只有一个 URL。JSON 始终使用固定 v1 result schema；遇到 `partial_success` 或 `ambiguous` 时，不要重放 Put，保留 `checkpoint_id`。

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
python3 /path/to/s3-upload/scripts/upload.py upload \
  --file /absolute/output/cover.png \
  --caller-skill image-2 \
  --json
```

映射选择 Target，但不会自动上传，也不会让生成失败触发重新生成。

## CLI

```text
upload FILE/TARGET options    normal 单次上传；multipart 由 capability gate 控制
url REFERENCE|TARGET+KEY      零远端请求生成 current-key URL
delete                       capability-gated；normal baseline unavailable
resume                       capability-gated multipart recovery
reconcile                    capability-gated read-only recovery
abort                        capability-gated multipart cleanup
```

运行 `python3 scripts/upload.py --help` 查看稳定 parser surface。完整状态/退出码见 [API notes](references/api-notes.md)。

## v1 迁移

无 subcommand 的旧 CLI、flat `S3_UPLOAD_ACCESS_KEY_ID`/bucket 字段合并、`--profile` 和 `set_profile.sh` 已移除。v2 不自动读取或迁移旧配置；必须先选择 project/global scope，再把 Secret 放进命名 Credential map，把位置和策略放进完整 Target。迁移例子见 [configuration.md](references/configuration.md#从-v1-flat-配置迁移)。

## 测试

```bash
python3 -m pytest s3-upload/tests -q
python3 -m compileall -q s3-upload/scripts
```

CI 不读取真实 Key、不发云请求。OSS/COS live evidence 仍位于 maintainer/test surface；普通用户只能使用公开文档列出的 experimental 基础能力。
