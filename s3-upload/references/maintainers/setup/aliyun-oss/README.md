# Aliyun OSS synthetic setup candidate

Retrieved 2026-07-22. This directory contains a `synthetic/docs-derived`
maintainer candidate. It is not a console recording, does not authorize a cloud
request, and does not make assisted setup available in normal mode.

The official sources support candidate steps for regional bucket creation,
strictly prefix-scoped bucket policy, lossless lifecycle/CORS editing, and a
least-privilege RAM application-user Access Key. The UI marker, normalized
before state, exact success observation, rollback boundary, and every remote
permission remain hypotheses until a separately authorized browser session is
recorded and reviewed.

Existing objects, overlapping prefixes/rules, an existing-bucket bucket-wide
change, or an account-level public-access change stop before mutation. Chinese
mainland default-public data endpoints remain blocked by the separate
`PublicEndpointForbidden` policy boundary; declaring a Public Base URL does not
repair the API endpoint.

The OSS credentials authorized for the bounded Ticket 26 data-plane run do not
grant or imply any control-plane permission. Setup enablement requires separate
human-gated evidence and an enabling ADR.
