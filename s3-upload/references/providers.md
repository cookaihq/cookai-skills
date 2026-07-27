# Provider rules

Normal mode only advertises operations present in the exact Provider Capability Contract. `enabled` means complete reviewed release evidence exists; `experimental` means the implementation is constrained by official provider documentation and offline vectors but lacks that release evidence. A bounded, non-release observation does not promote a row to `enabled`.

| Provider | Target endpoint/addressing | Normal operations | State |
|---|---|---|---|
| `aws-s3` | `endpoint=null`; AWS public endpoint derived from region; `addressing=null` resolves virtual | PutObject incl. conditional `If-None-Match: *` (`collision=unique\|reject`), presigned current-key GET | `enabled` |
| `cloudflare-r2` | exact HTTPS account endpoint required; `region=auto`; path addressing | PutObject incl. conditional `If-None-Match: *` (`collision=unique\|reject`), presigned current-key GET | `enabled` |
| `custom` | exact endpoint, region and path/virtual addressing required; user asserts compatibility | unconditional PutObject, presigned current-key GET | `enabled` by explicit user assertion |
| `aliyun-oss` | `endpoint=null` derives `https://s3.oss-{region}.aliyuncs.com`; `addressing=null` resolves virtual | unconditional fixed-length PutObject, presigned current-key GET | `experimental` |
| `tencent-cos` | `endpoint=null` derives `https://cos.{region}.myqcloud.com`; complete `BucketName-APPID`; `addressing=null` resolves virtual | unconditional fixed-length PutObject, presigned current-key GET | `experimental` |

Public URL construction is local and user-declared; it does not prove provider GET behavior. Target header defaults are signed with PutObject. Access Mode and Retention are independent, but this Skill never installs ACL, bucket policy, lifecycle or CORS.

Conditional Put (`ConditionalPutObject`, backing `collision=unique|reject` via `If-None-Match: *`) is enabled on the `aws-s3` and `cloudflare-r2` presets, with the providers' official conditional-write documentation as evidence. It stays disabled for `custom` and the OSS/COS experimental presets. Under `reject`, a remote 412 is followed by one presigned full-body GET: a size+SHA-256 double match against the planned source adopts the existing object (`adopted`, exit 0), anything less is a collision (exit 4).

Read-only reconciliation of a `put_unknown` checkpoint runs on every normal baseline through the same presigned full-body GET (`PresignGetObject`); HEAD/reserved-metadata round-trips remain disabled and are not used as evidence.

Normal contracts still do not enable:

- conditional Put on `custom`, `aliyun-oss` or `tencent-cos`;
- HEAD/reserved-metadata reconciliation;
- current-key or exact-version Delete and observers;
- multipart create/part/list/complete/abort;
- browser-assisted bucket/identity/policy setup.

A normal Target defaults to `collision=replace` and null multipart threshold/part size; `reject`/`unique` are executable only where conditional Put is enabled. A formed dry-run for unavailable behavior returns `plan.executable=false` with capability blockers and sends zero requests.

## OSS/COS experimental status

Both names are selectable normal presets for the two baseline operations above. Their service endpoint, virtual addressing, region/bucket rules and SigV4 format come from official documentation. The bounded OSS run confirmed one exact permanent-credential dual-header Put/presign contract but was release-ineligible; COS payload hashing and AWS-style query presign have no live result. Neither provider has the complete permanent/temporary, privilege-reviewed release evidence required for `enabled`.

For affected Alibaba Cloud accounts activated on or after 2025-03-20, Chinese-mainland default public endpoints can return `PublicEndpointForbidden`; use of the preset does not bypass that account policy. Tencent COS requires the complete `BucketName-APPID`, and the preset never generates path-style requests.

An experimental Target must leave `endpoint` and `addressing` null. Explicitly spelling the same default endpoint or `virtual` forms a separate exact/test-only contract; other endpoint/addressing variants are rejected by the normal Target path and require a separately specified maintainer candidate. Known OSS/COS service hosts cannot inherit the `custom` baseline. Live interlocks and synthetic setup playbooks remain maintainer-only, and assisted setup is still unavailable.

## Exact contract isolation

Explicit/custom endpoint, scheme, region, network class, addressing, signing profile and payload profile all participate in the Contract Key. Evidence for one exact combination never enables another endpoint spelling, HTTP scheme, internal/CNAME network, account or provider.
