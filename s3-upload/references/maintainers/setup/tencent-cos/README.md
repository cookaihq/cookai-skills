# Tencent COS synthetic setup candidate

Retrieved 2026-07-22. This directory contains a `synthetic/docs-derived`
maintainer candidate. No COS Key or authenticated browser session was
available, so every real control-plane state and operation remains
`not-tested`. Nothing here enables assisted setup in normal mode.

The candidate keeps the full `BucketName-APPID` identity through the regional
service endpoint, virtual-hosted object address, policy scope, Target, and
Public Base URL. It never derives APPID from a credential and never generates a
path-style request.

Official sources support candidate steps for bucket creation, prefix-scoped
bucket policy, complete lifecycle/CORS configuration, and a least-privilege CAM
programmatic sub-user key. UI markers, exact before/after observations,
permissions, and recovery behavior remain hypotheses. Existing objects,
overlapping prefixes or rules, and account-level or existing-bucket bucket-wide
expansion stop before mutation.

Temporary STS credentials are process-only live-verification input. A synthetic
issuance fixture may deliver only fake SecretId/SecretKey sentinels through the
one-shot sink; an unknown issuance or local installation failure requires
manual revoke-and-reissue. Real enablement requires human-gated evidence and an
enabling ADR.
