# v2 configuration

v2 把三件事分开：selector 选择哪个 Upload Target；Target 保存完整目的地与策略；Credential Profile 只保存身份材料。选中一条记录后整条原子加载，不跨文件逐字段补齐。

## 项目配置

```text
<project>/
├── .s3-upload/
│   ├── config.json
│   └── targets/
│       └── website-images.json
└── .env.local
```

`.s3-upload/config.json` 是可跟踪的非 Secret selector：

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

`.s3-upload/targets/website-images.json` 是完整 Target。`collision` 取 `replace`（默认，同键覆盖）/ `reject`（`If-None-Match: *` 原子 no-overwrite + 同内容采纳）/ `unique`；`reject`/`unique` 需要 ConditionalPutObject capability，当前仅 aws-s3 与 cloudflare-r2 preset 启用。CLI `--collision` 可对单次调用覆盖该字段，**包括把配置为 `reject` 的 Target 放宽为 `replace`**（此时不发 `If-None-Match`，同键对象被直接覆盖）——该字段是默认策略，不是不可绕过的保护。普通 baseline 关闭 multipart：

```json
{
  "schema_version": 1,
  "credential": "project:aws-main",
  "provider": "aws-s3",
  "region": "us-east-1",
  "endpoint": null,
  "addressing": null,
  "bucket": "project-artifacts",
  "prefix": "website-images/",
  "access": {
    "mode": "private",
    "public_base_url": null,
    "presign_expires_seconds": 3600
  },
  "retention": {"mode": "retain", "days": null},
  "collision": "replace",
  "object_headers": {
    "cache_control": null,
    "content_disposition": null
  },
  "limits": {
    "soft_max_bytes": 104857600,
    "multipart_threshold_bytes": null,
    "part_size_bytes": null
  },
  "retry": {
    "part_max_attempts": 3,
    "collision_max_attempts": 3
  },
  "setup": {
    "exclusive_prefix": false,
    "integration_test": false,
    "cors": null
  }
}
```

### `retry` 是跨 CLI 调用的尝试上限，不是网络重试参数

`retry.part_max_attempts` 与 `retry.collision_max_attempts`（各 1–5）计的是**业务级尝试次数**，与工作区 ADR 0006 的「单次网络调用级重试」是两套东西，不要混用：

| | `retry.*`（本文件） | ADR 0006 单次调用级重试 |
|---|---|---|
| 计什么 | 一个 part 被重传了几轮、unique 策略撞名换了几次 key | 一次逻辑请求内部发了几次物理请求 |
| 跨不跨进程 | 跨。计数落在 checkpoint 里，下一次 CLI 调用继续累加 | 不跨。全在一次函数调用内完成 |
| 触发条件 | 上一轮的结果**已判定**（part 失败、key 已被占用） | 传输层瞬时故障（超时、连接重置、DNS 失败），或应答状态码为 5xx / 429 |
| 可配置 | 是，写在 target 配置里 | 否，`NET_MAX_ATTEMPTS = 3` 是脚本内具名常量 |
| 适用范围 | multipart part 重传、unique 撞名换名 | 只有**读语义**调用（HEAD / GET / ListParts）；写操作不重试，结果不明一律 `ambiguous` |

也就是说：把 `part_max_attempts` 调大不会让一次超时的 HEAD 多试几次，把 ADR 0006 的重试关掉也不会影响 part 重传上限。写操作为什么不做单次调用级重试（含「连接建立阶段失败」为什么没被拆出来），见 `scripts/s3.py` 里 `read_request_with_retry` 上方的偏离说明。

项目 Secret 只放 `.env.local` 的一个命名 map；文件必须是 owned regular `0600`、未被 Git 跟踪且实际被忽略：

```dotenv
S3_UPLOAD_PROJECT_CREDENTIALS_JSON='{"aws-main":{"access_key_id":"...","secret_access_key":"...","session_token":"","expires_at":null}}'
```

`.env` 不能包含该 Secret map。它和 `.env.local` 都只从当前 `$PWD` 读取，不向父目录递归。

## OSS/COS experimental preset

两种 preset 沿用上面的完整 Target schema，只替换 provider-specific 字段。阿里云 OSS：

```json
{
  "provider": "aliyun-oss",
  "region": "cn-hangzhou",
  "endpoint": null,
  "addressing": null,
  "bucket": "project-artifacts"
}
```

它解析为 `https://s3.oss-cn-hangzhou.aliyuncs.com` + virtual addressing。中国内地默认公网 endpoint 是否可用还受账号开通时间与云端策略影响，preset 不会绕过 `PublicEndpointForbidden`。

腾讯云 COS：

```json
{
  "provider": "tencent-cos",
  "region": "ap-guangzhou",
  "endpoint": null,
  "addressing": null,
  "bucket": "project-artifacts-1250000000"
}
```

它解析为 `https://cos.ap-guangzhou.myqcloud.com` + virtual addressing。`bucket` 必须包含完整 `BucketName-APPID`。以上片段只展示需要替换的字段，不是可单独保存的不完整 Target；Access、Retention、limits 等仍必须完整存在。

两种 preset 当前 capability state 为 `experimental`：先执行 `upload --dry-run --json` 并核对状态。只有 `endpoint=null`、`addressing=null` 继承 preset 合同；显式写出同一个默认 endpoint 或 `virtual` 会形成独立 exact/test-only 合同，其他 endpoint/addressing 变体不属于当前默认 preset 并会被拒绝。高级 operation 与 assisted setup 仍不可用。

## 全局配置

```text
~/.config/s3-upload/
├── .env
└── targets/
    └── shared-documents.json
```

`targets/shared-documents.json` 使用同一完整 Target schema，但 credential 必须同作用域，例如 `"credential":"global:archive-main"`。`.env` 保存完整全局 map：

```dotenv
S3_UPLOAD_GLOBAL_CREDENTIALS_JSON='{"archive-main":{"access_key_id":"...","secret_access_key":"...","session_token":"","expires_at":null}}'
```

配置目录和 `targets/` 为 `0700`，文件为 `0600`，且不能是 symlink。显式 CLI/process `--target global:shared-documents` 本身授权本次 home 读取；通过项目 mapping/default/dotenv 间接选中 Global Target 时必须加 `--use-local-key`。没有全局 default 或全局 Skill mapping 文件。

## 选择顺序

Target selector 按以下顺序取首个非空值：

1. CLI `--target`
2. process `S3_UPLOAD_TARGET`
3. `$PWD/.env.local` 的 `S3_UPLOAD_TARGET`
4. `$PWD/.env` 的 `S3_UPLOAD_TARGET`
5. 项目 `skill_targets` 中显式 caller id 的映射
6. 项目 `default_target`

Caller id 只来自 `--caller-skill` 或 process `S3_UPLOAD_CALLER_SKILL`，不从 dotenv、路径或 Skill 目录名猜测。CLI `--target` 绕过 mapping/default；没有 caller 时才使用 default。

项目 credential map 按 process `S3_UPLOAD_PROJECT_CREDENTIALS_JSON` → `.env.local` 选择整张 map；全局 map 按 process `S3_UPLOAD_GLOBAL_CREDENTIALS_JSON` → 经授权的 home `.env` 选择整张 map。一个 profile 的四个字段绝不跨来源合并。

## Credential Profile

每个 profile 必须恰好包含：

```json
{
  "access_key_id": "...",
  "secret_access_key": "...",
  "session_token": "",
  "expires_at": null
}
```

Permanent credential 使用空 Session Token 与 null expiry。Temporary credential 必须同时具有非空 Session Token 和 UTC 秒级 `expires_at`，例如 `2026-07-22T13:00:00Z`。每次签名前必须剩余超过 60 整秒；presigned URL 实际时长取 requested 与 `expiry - now - 60s` 的较小值。

## Access 与 Retention

- Private：`public_base_url=null`，`presign_expires_seconds=1..604800`。
- Public：显式 HTTPS Public Base URL、非空 prefix、`setup.exclusive_prefix=true`、presign expiry 为 null。Skill 只拼 URL，不验证匿名读取。
- Retain：`days=null`。
- Expire：正整数 days、非空独占 prefix。生命周期执行仍由外部系统负责，结果为 `external-unverified`。

## 使用 Mapping

保持 cwd 为项目根，即使生成文件在别处：

```bash
uv run --project /absolute/s3-upload /absolute/s3-upload/scripts/upload.py upload \
  --file /other/output/cover.png \
  --caller-skill image-2 \
  --json
```

该调用读取项目 config，将 `image-2` 映射到 `project:website-images`，原子加载该 Target，再从项目 credential map 取完整 `aws-main`。映射只选择目的地；用户未明确要求持久上传时不得调用。

## 从 v1 flat 配置迁移

v2 不自动迁移。先明确选择 project 或 global scope，然后手工拆分：

- 旧 Access Key / Secret / Session Token → 一个完整命名 Credential Profile。
- 旧 provider/region/endpoint/addressing/bucket/prefix/public base/limits → 一个完整 Target。
- 旧 profile 名或默认行为 → 明确的 scoped Target selector/mapping。

不要同时保留 flat 值期待逐字段覆盖，也不要把 Secret 写进 Target JSON。迁移后先执行 `upload --dry-run --json`，确认 resolved Target source、bucket、Object Key、Access/Retention 和 capability blockers，再执行真实写入。

Dotenv 解析不是 shell：支持 `KEY=value` 和完整单/双引号；不展开 `$VAR`、`${VAR}`、命令替换、反引号或续行。
