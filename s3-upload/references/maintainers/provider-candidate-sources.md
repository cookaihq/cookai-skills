# OSS/COS provider candidate source review

Retrieved 2026-07-22. This note supports test-only candidate contracts. It does not advertise a normal-mode preset and is not live provider evidence.

## Conclusion

`https://s3.oss-{region}.aliyuncs.com` is an Alibaba Cloud documented S3-compatible public endpoint format, and `https://cos.{region}.myqcloud.com` is a Tencent Cloud documented service address for third-party S3-compatible applications. They are valid candidate inputs, but neither string by itself proves that this implementation's Put, GET, delete, multipart, metadata, or reconciliation behavior works against a specific account and bucket.

OSS has an authorized credential fixture for a separate bounded live run. COS has no credential fixture, so every COS remote item remains `not-tested`.

## Alibaba Cloud OSS

- Alibaba Cloud lists the public S3-compatible endpoint as `https://s3.oss-{region}.aliyuncs.com` and the internal endpoint as `https://s3.oss-{region}-internal.aliyuncs.com`. Its AWS SDK examples disable path-style access or select virtual addressing. [Official AWS SDK compatibility guide, Endpoint and SDK examples](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)
- The same guide says OSS supports the AWS Signature V4 algorithm, but its compatibility request uses `x-oss-content-sha256: UNSIGNED-PAYLOAD` and must not use `Transfer-Encoding: chunked`. [Official guide, FAQ on chunked encoding and signature versions](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)
- The candidate therefore uses canonical literal `UNSIGNED-PAYLOAD`, a known `Content-Length`, and no AWS streaming framing. The standard SigV4 payload header `x-amz-content-sha256: UNSIGNED-PAYLOAD` participates in `SignedHeaders`; the documented `x-oss-content-sha256: UNSIGNED-PAYLOAD` is also transmitted but is not added to `SignedHeaders`. The latter boundary was confirmed by the bounded account-specific run documented in [`oss-live-matrix-2026-07-22.md`](oss-live-matrix-2026-07-22.md): signing only the `x-oss` name failed, while the dual-header request authenticated. `Content-Length` is our deterministic way to satisfy non-chunked request framing; the source forbids chunked transfer but does not separately mandate that header for every client. [Official guide, same FAQ](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)
- A generic ordinary payload SHA-256 profile is kept separate as a hypothesis. It is not described as the official OSS requirement and cannot be tried automatically after an ambiguous mutation. [Official guide establishes the documented profile boundary](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)
- For accounts that activated OSS on or after 2025-03-20 00:00:00 UTC+8, Alibaba Cloud says Chinese-mainland default public endpoints reject data API calls with `PublicEndpointForbidden`; the documented alternatives are an eligible internal endpoint or a custom domain bound to the bucket. [Official `PublicEndpointForbidden` explanation, Cause and Solutions](https://www.alibabacloud.com/help/en/oss/user-guide/0048-00000401)
- Account age is evidence context, not a configuration bypass. Recognizable `s3.oss-cn-*` default-public contracts remain disabled in normal mode. Internal access is an `eligible-vpc` contract, and a CNAME is a bucket-bound contract with no inherited public-endpoint operation evidence.

## Tencent Cloud COS

- Tencent Cloud's third-party S3 compatibility guide gives the service address `https://cos.{region}.myqcloud.com`, virtual-hosted addressing, and AWS V2/V4 signature-format support. It also warns that COS does not guarantee complete S3 compatibility. [Official S3 compatibility guide](https://cloud.tencent.com/document/product/436/41284)
- COS bucket identity is the full `BucketName-APPID` value. The candidate requires it explicitly and never guesses APPID from a credential or another provider's configuration. [Official bucket overview, naming conventions](https://www.tencentcloud.com/zh/document/product/436/13312) [Pinned official Java SDK bucket validation](https://github.com/tencentyun/cos-java-sdk-v5/blob/499c86d7d5a658a16ea25627875b373f2469b721/src/main/java/com/qcloud/cos/internal/BucketNameUtils.java)
- Virtual addressing over the documented third-party service address yields `https://<BucketName-APPID>.cos.{region}.myqcloud.com`. This is deliberately narrower than claiming it is COS's only domain or the native SDK default. The official Java SDK uses a different native endpoint builder, which is why native SDK behavior is not substituted for S3-compatible evidence. [Official S3 compatibility guide](https://cloud.tencent.com/document/product/436/41284) [Pinned official Java SDK endpoint builder](https://github.com/tencentyun/cos-java-sdk-v5/blob/499c86d7d5a658a16ea25627875b373f2469b721/src/main/java/com/qcloud/cos/endpoint/RegionEndpointBuilder.java)
- The compatibility guide says buckets created after 2024-01-01 do not support path-style domains. The candidate takes the conservative rule and never generates path-style COS requests. [Official S3 compatibility guide, access-style note](https://cloud.tencent.com/document/product/436/41284)
- V4 format support is docs-derived; this implementation's canonical request, payload handling, presigning, temporary credentials, redirects, response parsing, and every operation remain hypotheses until an exact COS contract is run. No COS key was available, so no remote compatibility claim is made. [Official S3 compatibility guide](https://cloud.tencent.com/document/product/436/41284)

On the retrieval date, the Tencent documentation pages returned an anti-automation challenge to command-line retrieval. The exact official URLs and reviewed section identities are retained here and in the machine-readable manifest; the provider enablement review must re-open them and compare the claims before accepting live evidence.

## Machine-readable boundary

[`provider-candidate-sources.v1.json`](provider-candidate-sources.v1.json) records each claim's evidence class, source section, retrieval date, and region/network/account conditions. `docs-derived` never means live-tested; `hypothesis` and `not-tested` entries cannot enable a normal-mode capability.
