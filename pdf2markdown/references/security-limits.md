# Source Download Security Limits

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
