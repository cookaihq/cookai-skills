# Page Baseline And Preflight Contract

## Required Capabilities

`advance` checks every dependency before rendering and makes no network call:

| Name | Purpose |
|---|---|
| PyMuPDF (`fitz`) | Open the frozen PDF, extract structure, and render full pages |
| Pandoc executable | Parse and normalize GFM after conversion |
| BeautifulSoup4 (`bs4`) | Parse HTML structurally for the later allowlist |
| Host visual capability | Let the Agent inspect every local page PNG |

The command does not install or replace dependencies. It treats a dependency as available only when the API needed by this workflow is present: PyMuPDF must expose the rendering, color-space, form, and MuPDF-warning APIs; Pandoc must successfully parse empty GFM into a JSON AST; and BeautifulSoup4 must expose `BeautifulSoup`. Declare host visual capability with `--visual-capability available`; use `unavailable` when the host cannot inspect the PNGs. A missing or API-incompatible item commits `recoverable_error / dependency_missing` and preserves the source PDF.

The Ticket 04 runtime suite was verified with PyMuPDF 1.26.5 and Pandoc 3.8.2. Its isolated command fixtures also exercise the Pandoc 3.6.4 command contract and a BeautifulSoup4 4.13.0-compatible minimal parse contract. These observations are not invented minimum-version claims: the gate checks the required API, records the resolved version and executable identity in the operation, and refuses dependency drift while recovering that operation.

## Render And Resource Limits

- Default render resolution: 300 DPI, lossless RGB PNG with visible annotations.
- Explicit render range: 72 through 600 DPI via `--render-dpi`.
- Maximum source snapshot loaded into memory for parsing or rendering: 256 MiB. Streaming SHA-256 identity verification still covers every source byte.
- Maximum pages: 2,000.
- Maximum pixels in one page: 25,000,000.
- Maximum pixels across all pages: 250,000,000.
- Maximum serialized source inventory: 64 MiB.
- Maximum submitted preflight record: 8 MiB.
- Maximum saved preflight result: six times the submitted-record limit, plus 256 bytes per allowed source page and 64 KiB of fixed workflow metadata. The factor covers the worst legal one-byte JSON string value, U+007F, expanding to the six-byte `\u007f` representation in canonical ASCII JSON. The resulting 48.6 MiB ceiling remains below the 64 MiB history ceiling with room for the operation envelope and earlier baseline events.
- Maximum page-PNG read: four bytes per planned pixel plus 1 MiB of PNG container overhead.

The operation records the effective DPI, limits, dependency identities, predicted page dimensions, and total pixels in its write-ahead intent. Every source, including an oversized source, is hashed in 1 MiB chunks bounded by the locked regular file's size at open before authoritative state is written; concurrent growth is rejected. This complete streaming identity check is required to detect same-size source tampering. Only after the hash matches does the 256 MiB limit prevent any in-memory source load, parse, or render. Inventory bytes are budgeted exactly as each page is added, so an exceeded header or page stops before rendering later pages. Inventory, PNG, and preflight artifacts are stat-bounded before any in-memory read and remain bounded if a file grows concurrently. A limit failure is a deterministic block; raising a limit is not exposed as an invocation option.

### Limit Evidence

The limits were exercised against the verified dependency combination as follows:

| Limit | Fixture or sizing basis |
|---|---|
| 256 MiB source | Matches the public-HTTPS download and disk ceiling; a real saved PDF with a lowered ceiling proves full identity verification followed by blocking before in-memory load, parse, or render, while preserving the source. |
| 2,000 pages | A real 2,001-page PyMuPDF fixture is rejected before any page image allocation. |
| 25,000,000 pixels per page | A real oversized-page fixture predicts 434,055,556 pixels and blocks before rendering. |
| 250,000,000 total pixels | A real 30-page fixture predicts 252,450,000 pixels and blocks before rendering any page. |
| 64 MiB inventory | Exact compact-JSON accounting is applied to the fixed header and each page; a lowered-ceiling fixture proves the header stops all rendering and no temporary output survives. |
| 8 MiB submitted record | Descriptor reads stop at the limit plus one byte. The derived-result allowance covers 6x canonical ASCII expansion plus the fixed fields added to all 2,000 page records; the real serialized result is checked before intent. |
| Page PNG bytes | PyMuPDF emits RGB PNG; four bytes per predicted pixel plus 1 MiB covers raw RGB, row filters, compression framing, and PNG chunks while remaining tied to the already enforced pixel limit. |
| Disk budget | Three bytes per predicted RGB pixel; a zero-free-block fixture proves the gate runs before page writes. |

Page names use the total page-count width with a minimum width of four: `page-0001.png`, `page-0002.png`, and so on. Before writing the prepared event, the workflow verifies continuous page numbers, PNG dimensions, private `0600` files, hashes, and one image per source page. Missing generated pages commit `page_reference_count_mismatch`; a discontinuous generated plan commits `page_reference_numbering_mismatch`. Later mutation of an already committed baseline remains an `integrity_violation`, not a new claim about the source PDF.

## Source Inventory

`01-source/source-inventory.json` records the source hash, page count, render settings, dependency versions, encryption metadata, and per-page evidence. Per-page evidence includes physical and pixel dimensions, rotation, positioned text blocks, external and internal links, visible form values, annotations, actual image occurrences, drawing count, extraction issues, MuPDF warnings, and the page PNG identity.

External URI query strings, fragments, and userinfo are omitted from the ordinary inventory. Their complete original value is represented only by a SHA-256 identity. Password-widget values and overlapping extracted text are redacted without storing a value hash.

The workflow never executes PDF scripts, launch actions, attachments, or external programs. It records unsafe action presence as evidence only.

## Agent Record Schema

The `--input` file must be strict JSON with no duplicate or unknown fields:

```json
{
  "schema_version": 1,
  "summary": "pass",
  "pages": [
    {
      "page_number": 1,
      "classification": "content",
      "risk_codes": [],
      "evidence": ["The complete rendered page is readable."]
    }
  ]
}
```

Page numbers must cover `1..page_count` exactly once and in order. `classification` is one of `content`, `blank`, `risk`, or `unreadable`. Evidence is a non-empty list of bounded, non-empty strings. `risk` requires at least one stable risk code; `content` accepts none.

Supported risk codes are:

- `scanned_content`
- `handwritten_content`
- `low_resolution`
- `blurred_content`
- `abnormal_rotation`
- `small_text`
- `complex_multicolumn`
- `cross_page_table`
- `layout_form`
- `mixed_text_images`
- `partial_blank`
- `structure_extraction_incomplete`
- `unreadable_content`
- `other`

The workflow does not trust `summary`. It derives `blocked` for any unreadable page or all-blank pages, `warning` for any blank/risk page, and `pass` only when every page is unqualified content. If deterministic structure extraction was incomplete, the affected page must include `structure_extraction_incomplete` and cannot pass.

The saved `04-review/preflight.json` adds each page's PNG path, SHA-256, and pixel dimensions. Warning decisions bind the saved preflight hash. Generation, action ID, and evidence hash mismatches are rejected without consuming the pending action.

## Durable Commit

Baseline generation and records use an exclusive work-bundle lock and a write-ahead sequence: `intent -> prepared -> artifact promotion -> private generation -> manifest -> committed`. The manifest is the logical commit point. Recovery accepts only the intent-bound temporary or final artifact with the prepared hash, and never overwrites an unrelated final file. A completed local operation keeps its original one-time action across recovery.
