# Nano Banana 2 API Guide (aihubmax.com)

Model: **Nano Banana 2** (`gemini-3.1-flash-image-preview`)

Provider docs:
- Create: https://docs.aihubmax.com/pages/zh/api-manual/image-series/nanobanana/gemini-3.1-flash-image-preview
- Query: https://docs.aihubmax.com/pages/zh/api-manual/task-management/get-task-detail

## Base URL

- `https://api.aihubmax.com`

## Authentication

- Header: `Authorization: Bearer <YOUR_API_KEY>` (required)
- The local skill resolves the key via the following chain (high → low) and forwards it as a Bearer token:
  1. env `AIHUB_API_KEY`
  2. `$PWD/.env.local` (auto)
  3. `$PWD/.env` (auto)
  4. `~/.config/banana-2/.env` (only with `--use-local-key`)
- On HTTP 401, the skill automatically falls back to the next key in the chain. Other status codes do not trigger fallback. See SKILL.md "Auth & Key Handling" for details.

## Create Task

- Method: `POST`
- Path: `/v1/images/generations`
- Content-Type: `application/json`

### Request Body

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `model` | string | yes | `gemini-3.1-flash-image-preview` | Only supported value for this skill |
| `prompt` | string | yes | — | Text-to-image: describe the image. Image editing: describe the changes relative to the reference image |
| `aspect_ratio` | string | no | `1:1` | See "Aspect Ratio" below |
| `resolution` | string | no | `1K` | Quality tier (NOT pixels). See "Resolution" below |
| `image_urls` | string[] \| null | no | `null` | Reference/edit images. Presence switches the call to image editing / img2img. Public URL recommended |
| `output_format` | string \| null | no | `null` | `jpg` / `png` / `webp`. Advanced — omit unless needed |
| `google_search` | boolean \| null | no | `null` | Enable real-time web search to ground generation. Advanced — omit unless needed |
| `image_search` | boolean \| null | no | `null` | Enable image-search assistance. **Supported by this model only.** Advanced — omit unless needed |

> This model has **no** `num_outputs`, `quality`, `background`, or `mask_url` fields. It always returns a single image.

### Aspect Ratio

`gemini-3.1-flash-image-preview` supports (15 values):

`1:1`, `1:4`, `1:8`, `2:3`, `3:2`, `3:4`, `4:1`, `4:3`, `4:5`, `5:4`, `8:1`, `9:16`, `16:9`, `21:9`, `match_input_image`

- `match_input_image` keeps the input reference image's aspect ratio (only meaningful with `image_urls`).

### Resolution (quality tier, not pixels)

`gemini-3.1-flash-image-preview` supports: `512`, `0.5K`, `1K`, `2K`, `4K`

- `512` and `0.5K` both mean half-size output
- `1K` ≈ 1MP, `2K` ≈ 4MP, `4K` ≈ 16MP
- Actual pixel dimensions are determined by `aspect_ratio` × tier

### Request Examples

```bash
# text-to-image (1K)
curl -X POST 'https://api.aihubmax.com/v1/images/generations' \
  -H "Authorization: Bearer $AIHUB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "A futuristic city skyline at dusk, cyberpunk style",
    "aspect_ratio": "16:9",
    "resolution": "1K"
  }'

# image editing with image search (2K, match input aspect ratio)
curl -X POST 'https://api.aihubmax.com/v1/images/generations' \
  -H "Authorization: Bearer $AIHUB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-3.1-flash-image-preview",
    "prompt": "Replace the background with a tropical beach",
    "image_urls": ["https://example.com/photo.jpg"],
    "aspect_ratio": "match_input_image",
    "resolution": "2K",
    "image_search": true
  }'
```

### Create Response (HTTP 200)

```json
{
  "created": 1757165031,
  "id": "task-unified-1757165031-uyujaw3d",
  "model": "gemini-3.1-flash-image-preview",
  "object": "image.generation.task",
  "progress": 0,
  "status": "pending",
  "task_info": {
    "can_cancel": true,
    "estimated_time": 45
  },
  "type": "image"
}
```

Status enum at creation: `pending` / `processing` / `completed` / `failed` (typically `pending`).

## Query Task

- Method: `GET`
- Path: `/v1/tasks/{task_id}`
- Optional query: `sync_upstream=true` to force a refresh against upstream before responding (only affects in-flight tasks).

### Request Example

```bash
curl -X GET "https://api.aihubmax.com/v1/tasks/${TASK_ID}?sync_upstream=true" \
  -H "Authorization: Bearer $AIHUB_API_KEY"
```

### Query Response (completed image task)

```json
{
  "id": "task-unified-1776800000-abcd1234",
  "object": "image.generation.task",
  "type": "image",
  "model": "gemini-3.1-flash-image-preview",
  "status": "completed",
  "progress": 100,
  "created": 1776800000,
  "results": [
    { "url": "https://example.com/output-image.png" }
  ],
  "error": null,
  "usage": { "credits_reserved": 3, "user_group": "default" }
}
```

> **Important:** for `type=image` most models (including Nano Banana 2) return `results[i]` as just `{ "url": ... }` — **`content_type` is usually absent**. The download step must therefore infer the file extension from `--output-format` → response `content_type` (if any) → URL path tail → `png`. The bundled `create_task.sh` already implements this order.

### Result Extraction

- Terminal success: `status == "completed"` → iterate `results[].url`
- Terminal failure: `status == "failed"` → read `error.code`, `error.message`, `error.type`
- Non-terminal (`pending` / `processing`): continue polling

### Polling Recommendation

- Interval: 5-10 seconds
- Stop on terminal status (`completed` / `failed`)
- Cap attempts (e.g. 90) to avoid an infinite loop
- Image URLs returned in `results[].url` are valid for **24 hours**

### Race Condition: completed-but-no-results

The unified task gateway can flip `status` to `completed` (sometimes with `progress < 100` and `results: null`) before the image URL is attached. A subsequent query a few seconds later returns the populated `results` array.

The client must treat `status=completed AND results is empty/null` as **non-terminal** and keep polling until either `results` is populated or attempts are exhausted. The bundled `create_task.sh` already implements this guard.

## Error Codes

| HTTP | type | Meaning |
|------|------|---------|
| 400 | `invalid_request_error` | Malformed request |
| 401 | `authentication_error` | Invalid / missing API key |
| 402 | `insufficient_quota` | Not enough credits |
| 422 | `validation_error` | Parameter validation failed |
| 429 | `rate_limit_error` | Rate limited — do NOT auto-retry |
| 500 | `server_error` | Internal server error |
| 503 | `service_unavailable` | Service temporarily unavailable |

The task-query endpoint may also return `404 not_found` (unknown `task_id`).

Errors come back in `{ "error": { "message", "type" } }` shape (the query response's `error` object additionally carries `code` on failed tasks).

## Notes

- Creation is async; the create response returns immediately with `status=pending`.
- Each successful create call consumes credits — see "Dedup" below.
- Never log or echo the full Bearer token. Mask to `head4****tail4`.
- Image URLs expire 24 hours after generation; download or rehost if longer retention is needed.

## Dedup

The skill must prevent duplicate creation calls with identical parameters within the same conversation turn. "Identical parameters" means matching `model`, `prompt`, `image_urls`, `aspect_ratio`, `resolution`, `output_format`, `google_search`, `image_search`. Any parameter change allows a new creation.
