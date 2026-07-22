# OSS bounded live matrix, 2026-07-22

This is an account-specific maintainer evidence summary, not a normal-mode preset release. Raw/redacted bundles remain in the Git-ignored restricted test-results directory and are not committed.

## Scope and gates

- Exact public S3-compatible endpoint family, region, virtual addressing, one project bucket and one permanent credential fixture.
- Credential file was an owned single-link `0600` regular file, untracked and effectively Git-ignored.
- Every resource key used a unique `s3-upload-live-test/<uuid>/` prefix.
- Authorized data-plane operations only: Put, HEAD, Authorization-header GET, presigned GET, current-key Delete/observer, multipart create/part/list/complete/abort/session observer, reserved metadata, response parsing and reconciliation.
- Public unsigned GET, conditional write, version Delete, temporary credentials and every control-plane action were not authorized or not tested.
- No IAM policy document was available. Credential privilege verdict is `unknown`, never least-privilege-confirmed.

## Payload/signing finding

The first implementation put `x-oss-content-sha256: UNSIGNED-PAYLOAD` into `SignedHeaders`; OSS returned a definitive `SignatureDoesNotMatch` and no object was created. An independent AWS CLI Put/current-key cleanup succeeded against the same fixture, proving endpoint and credential authentication independently of this signer.

Read-only probes then isolated the canonical boundary:

- standard signed `x-amz-content-sha256: UNSIGNED-PAYLOAD` authenticated;
- signing only the `x-oss-content-sha256` name did not;
- signed `x-amz-content-sha256` plus the documented, transmitted `x-oss-content-sha256: UNSIGNED-PAYLOAD` authenticated.

The candidate golden now freezes that dual-header profile. This was not an automatic ordinary-payload-hash fallback after an ambiguous mutation: the first response was a definitive 403, and the successful profile retained canonical `UNSIGNED-PAYLOAD` and fixed non-chunked framing.

## Final bounded run

Post-audit verification used evidence id `aliyun-oss-8136b234ebc64699a3e3895256d3cc7c` against the exact endpoint `https://s3.oss-cn-beijing.aliyuncs.com`. The restricted report remains Git-ignored.

The final run passed all authorized rows:

- PutObject -> HEAD -> Authorization-header GET -> presigned GET -> current-key Delete -> absence observer;
- CreateMultipartUpload -> UploadPart -> ListParts -> CompleteMultipartUpload;
- separate CreateMultipartUpload -> AbortMultipartUpload -> absence observer;
- reserved operation-id/source-SHA metadata round trip, response parsing and exact reconciliation observation.

Both GET bodies matched the complete source size and SHA-256. Final cleanup confirmed every known object/session absent; `residuals=[]`.

## Classification

Classification remains `evidence-not-obtained` for release because credential privilege is `unknown`. The run also provides no temporary credential, public unsigned GET, conditional collision, version-delete or other-account evidence. All OSS operations remain test-only hypotheses in the registry, and the recognizable Chinese-mainland default-public family remains disabled in normal mode pending Tickets 37, 39, 41 and the human-reviewed ADR gate.
