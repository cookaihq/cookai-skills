# Provider rules

Normal mode only advertises operations backed by the exact Provider Capability Contract.

| Provider | Target endpoint/addressing | Normal-enabled operations |
|---|---|---|
| `aws-s3` | `endpoint=null`; AWS public endpoint derived from region; `addressing=null` resolves virtual | unconditional PutObject, presigned current-key GET |
| `cloudflare-r2` | exact HTTPS account endpoint required; `region=auto`; path addressing | unconditional PutObject, presigned current-key GET |
| `custom` | exact endpoint, region and path/virtual/bucket-bound addressing required; user asserts compatibility | unconditional PutObject, presigned current-key GET |

Public URL construction is local and user-declared; it does not prove provider GET behavior. Target header defaults are signed with PutObject. Access Mode and Retention are independent, but this Skill never installs ACL, bucket policy, lifecycle or CORS.

Normal contracts do not currently enable:

- conditional Put (`collision=unique|reject`);
- HEAD/reserved-metadata reconciliation;
- current-key or exact-version Delete and observers;
- multipart create/part/list/complete/abort;
- browser-assisted bucket/identity/policy setup.

Accordingly a normal Target uses `collision=replace` and null multipart threshold/part size. A formed dry-run for unavailable behavior returns `plan.executable=false` with capability blockers and sends zero requests.

## OSS/COS status

`aliyun-oss` and `tencent-cos` are not normal-mode presets. OSS has account-specific bounded maintainer evidence, but credential privilege remains unverified and the evidence does not generalize to another account, endpoint or operation. COS has no live credential evidence. Both remain unavailable until the separate release-evidence and human-reviewed ADR gates are complete.

Do not label either provider `custom` to bypass this boundary. Candidate endpoint formats, live interlocks and synthetic setup playbooks are maintainer-only and deliberately absent from the user workflow.

## Exact contract isolation

Explicit/custom endpoint, scheme, region, network class, addressing, signing profile and payload profile all participate in the Contract Key. Evidence for one exact combination never enables another endpoint spelling, HTTP scheme, internal/CNAME network, account or provider.
