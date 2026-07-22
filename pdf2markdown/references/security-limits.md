# Download And Archive Security Limits

## Public HTTPS Source

Apply these fixed limits to every public HTTPS source hop. A transport must not follow redirects, use proxies, retry a request, decompress a response, or add authentication on its own.

| Boundary | Hard limit or policy |
|---|---|
| Redirects | At most 5; accept only 301, 302, 303, 307, and 308, then revalidate the next HTTPS URL |
| TCP and TLS connect | 10 seconds total for the selected pinned endpoint |
| Response headers | 30-second socket timeout |
| Response body | 30 seconds total, with each read bounded by the remaining deadline |
| Source bytes | 256 MiB, enforced from `Content-Length` when present and again while streaming |
| Source disk budget | 256 MiB, plus a free-capacity check before each 64 KiB write |
| Content encoding | Missing or exactly `identity`; reject compressed responses |
| Content type | Exactly one `application/pdf` value, with parameters allowed |

Resolve each hop to structured TCP endpoints. Reject the entire hop if any A or AAAA result is private, loopback, link-local, reserved, unspecified, multicast, carrier-grade NAT, scoped, or IPv4-mapped IPv6. Connect directly to one validated numeric endpoint, use the URL hostname for TLS SNI and certificate verification, and verify the TLS peer equals that pinned endpoint before sending `GET`.

Send only origin-form `path?query` over the connection. Never send a fragment. Use fixed `Accept: application/pdf`, `Accept-Encoding: identity`, `Connection: close`, and `User-Agent` headers; do not forward `Authorization`, Cookie, `Referer`, proxy credentials, `AIHUB_API_KEY`, or ambient browser state.

Require all three PDF identity checks before committing the staging bundle:

1. The response has the accepted content type.
2. The first five bytes are `%PDF-`.
3. PyMuPDF opens the saved bytes as a PDF.

Do not infer identity from the URL extension. On any failure, remove the partial `source.pdf` and the temporary bundle. Persist only query- and fragment-free initial/final hop URLs, public resolver and peer evidence, final content type, redirect count, source byte hash, and the SHA-256 of the complete original input URL.

## Doc2X Result Download

Apply the same redirect, endpoint, TLS peer, connect, response-body, content-encoding, and 64 KiB streaming rules to the private Doc2X result URL. The request `Accept` value may advertise ZIP and generic binary content, but response identity is established by strict ZIP parsing rather than a content-type claim. Never send `Authorization`, `AIHUB_API_KEY`, Cookie, Referer, proxy credentials, or browser state. Only `.state/private.json` may contain the complete URL; public intent and result records contain its SHA-256.

The result boundary uses these fixed local limits:

| Boundary | Hard limit or policy |
|---|---|
| Redirects | At most 5 with full URL, DNS, and peer revalidation per hop |
| TCP and TLS connect | 10 seconds total for the selected pinned endpoint |
| Response body | 30 seconds total; 64 KiB maximum per read |
| Result ZIP bytes | 256 MiB, checked from `Content-Length` and while streaming |
| ZIP members | 65,535 (`zipfile.ZIP_FILECOUNT_LIMIT`) |
| Member path | At most 1,024 UTF-8 bytes before and after canonicalization |
| Path component | At most 255 UTF-8 bytes before and after canonicalization |
| Path depth | At most 128 components |
| Total path components | At most 65,535 across all members |
| One uncompressed member | 256 MiB |
| Total compressed member bytes | 256 MiB |
| Total uncompressed member bytes | 256 MiB |
| Staging disk budget | 512 MiB, the exact sum of the ZIP and uncompressed-tree limits, plus free-capacity checks |
| Compression | Only `ZIP_STORED` and `ZIP_DEFLATED` |
| Member types | Ordinary regular files and directories only |

These values are conservative work-bundle limits, not AIHub or Doc2X service promises. The 256 MiB archive boundary reuses the established bounded source-download limit. The same 256 MiB is used as the initial whole-tree and single-member boundary because no verified upstream result-size contract exists. The 512 MiB staging boundary is their exact sum. The member-count value comes from Python's standard ZIP implementation. The fixed 1,024-byte path, 255-byte component, and 128-component depth bounds are explicit conservative local policy. Reusing 65,535 as the total-component budget bounds namespace memory even when every member is deeply nested. All remain local policy pending live integration validation and must not be described as upstream quota.

Before extraction, reject malformed ZIP structure, encryption, unsupported compression, unsafe or non-canonical paths, raw `.` or `..` components, NUL, absolute paths, backslashes, drive prefixes, duplicates, Unicode normalization/casefold collisions across complete paths and implicit prefixes, file/directory prefix conflicts, and every symlink, device, or special member. Canonical namespace keys use `NFC(NFC(component).casefold())`; each trie node also binds the first raw component spelling, so canonical aliases cannot create separate filesystem trees. Namespace checks and memory are bounded linearly by the total-component budget. Directory entries must not carry payload bytes. A hard link is never materialized: every accepted file is created as a new `O_EXCL | O_NOFOLLOW` regular file with link count one, and every committed inspection rejects link-count drift.

Do not use `extractall`. Create each directory relative to an already verified directory descriptor with mode `0700`; create each file exclusively with mode `0600`. Stream each member, enforce declared and actual sizes, consume it to EOF so the standard ZIP reader checks CRC, fsync the archive, every file, every explicit and implicit directory, the raw root, and the attempt root, and hash the actual tree through the single canonical tree-hash implementation. A committed ZIP, tree, or raw Markdown hash, path, type, mode, or link-count mismatch is an integrity failure.

The result staging directory and final attempt directory live under the same `03-converted/attempts/` parent. Before any reservation event, the random staging name, sibling owner-marker name, and final name must all be absent. A durable reservation precedes an exclusive `0600` owner marker whose content binds the reservation hash; the marker and parent are fsynced before the staging directory is created exclusively as `0700` and the parent is fsynced again. The intent then records the staging device/inode identity and becomes durable before the marker is removed and the parent is fsynced. Reservation-only recovery may recreate the marker only if both marker and staging are absent. Once staging exists, the exact marker must validate and the directory must be empty. An unreserved orphan, foreign payload, invalid marker, preseeded final path, or replacement with a different identity is never adopted.

A durable prepared record follows complete ZIP/tree verification. The whole staging directory is renamed once only when the final name is absent, the attempts parent is fsynced again, and the final path identity and hashes are reverified before manifest/private commit. Recovery repeats the parent fsync when it sees an already-renamed final path. It may use a complete local ZIP without a live URL. Without one, result HTTP 401/403/404 can only trigger a later poll of the same task for a new private URL; it never authorizes a new conversion attempt. Incomplete data is never presented as a formal artifact, and prepared recovery requires exactly one identity- and hash-matching part or final directory.
