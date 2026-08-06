# GPT Image 2 API Guide (aihubmax.com)

Provider docs:
- Create: https://docs.aihubmax.com/pages/zh/api-manual/image-series/gpt-image-2/gpt-image-2
- Query: https://docs.aihubmax.com/pages/zh/api-manual/task-management/get-task-detail

## Base URL

- `https://api.aihubmax.com`

## Authentication

- Header: `Authorization: Bearer <YOUR_API_KEY>` (required)
- The local skill resolves the key via the following chain (high → low) and forwards it as a Bearer token:
  1. env `AIHUB_API_KEY`
  2. `$PWD/.env.local` (auto)
  3. `$PWD/.env` (auto)
  4. `~/.config/image-2/.env` (only with `--use-local-key`)
- On HTTP 401, the skill automatically falls back to the next key in the chain. Other status codes do not trigger fallback. See SKILL.md "Auth & Key Handling" for details.

## Create Task

- Method: `POST`
- Path: `/v1/images/generations`
- Content-Type: `application/json`

### Request Body

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `model` | string | yes | `gpt-image-2` (full) or `gpt-image-2-limit` (lite) |
| `prompt` | string | yes | Non-empty. For img2img, describe changes relative to the reference image |
| `image_urls` | string[] \| null | no | Reference images. Presence switches the call to img2img. Accepts public URL / Data URI / Base64 |
| `num_outputs` | integer | no | Full: 1-10. Lite: must be 1. Default 1 |
| `resolution` | string \| object | no | See "Resolution" below. Default `1024x1024` |
| `quality` | enum | no | `low` / `medium` / `high`. Default `high`. **Full only** |
| `mask_url` | string \| null | no | White areas are editable. Must match dimensions of `image_urls[0]`. **Full only.** Mutually exclusive with `background` |
| `output_format` | enum | no | `png` / `jpeg` / `webp`. Default `png`. **Full only** |
| `background` | enum | no | `auto` / `opaque`. Default `auto`. **Full only.** Mutually exclusive with `mask_url` |

### Resolution

Two forms:

**Preset string**

- Full version (11 presets):
  `1024x768`, `768x1024`, `1024x1024`, `1536x1024`, `1024x1536`,
  `1920x1080`, `1080x1920`, `2560x1440`, `1440x2560`, `3840x2160`, `2160x3840`
- Lite version (3 presets):
  `1024x1024`, `1024x1536`, `1536x1024`

**Custom object** (full version only)

```json
{ "width": 1536, "height": 1024 }
```

Constraints:
- Width and height multiples of 16
- Each side in 256..3840
- Aspect ratio short-side : long-side ≤ 1 : 3
- Total pixels in 655,360 .. 8,294,400

### Request Example

```bash
curl -X POST 'https://api.aihubmax.com/v1/images/generations' \
  -H "Authorization: Bearer $AIHUB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-image-2",
    "prompt": "生成一张未来城市夜景海报，霓虹灯，电影感构图",
    "num_outputs": 1,
    "resolution": "1920x1080",
    "quality": "high"
  }'
```

### Create Response (HTTP 200)

```json
{
  "id": "task-unified-1757165031-uyujaw3d",
  "created": 1757165031,
  "model": "gpt-image-2",
  "object": "image.generation.task",
  "type": "image",
  "status": "pending",
  "progress": 0,
  "task_info": {
    "can_cancel": true,
    "estimated_time": 45
  }
}
```

Status enum at creation: `pending` / `processing` / `completed` / `failed` (typically `pending`).

## Query Task

- Method: `GET`
- Path: `/v1/tasks/{task_id}`
- Optional query: `sync_upstream=true` to force refresh against upstream before responding.

### Request Example

```bash
curl -X GET "https://api.aihubmax.com/v1/tasks/${TASK_ID}?sync_upstream=true" \
  -H "Authorization: Bearer $AIHUB_API_KEY"
```

### Query Response

```json
{
  "id": "task-unified-...",
  "object": "image.generation.task",
  "type": "image",
  "model": "gpt-image-2",
  "status": "completed",
  "progress": 100,
  "created": 1757165031,
  "results": [
    { "url": "https://...", "content_type": "image/png" }
  ],
  "error": null,
  "usage": { "credits_reserved": 0, "user_group": "default" }
}
```

### Result Extraction

- Terminal success: `status == "completed"` → iterate `results[].url`
- Terminal failure: `status == "failed"` → read `error.code`, `error.message`, `error.type`
- Non-terminal (`pending` / `processing`): continue polling

### Polling Recommendation

- Interval: 5-10 seconds
- Stop on terminal status (`completed` / `failed`)
- Cap attempts (e.g. 90) to avoid infinite loop
- Image URLs returned in `results[].url` are valid for **24 hours**

### Race Condition: completed-but-no-results

Observed in practice: the upstream sometimes flips `status` to `completed` and returns `progress: 50` with `results: null` before the image URL is attached. A subsequent query (a few seconds later) returns the same task with `progress: 100` and a populated `results` array.

The client must therefore treat `status=completed AND results is empty/null` as **non-terminal** and keep polling until either `results` is populated or attempts are exhausted. The bundled `create_task.sh` already implements this guard.

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

Errors come back in the same shape as the query response's `error` object.

## Notes

- Creation is async; the create response returns immediately with `status=pending`.
- Each create call consumes credits — see "Dedup" below.
- Never log or echo the full Bearer token. Mask to `head4****tail4`.
- Image URLs expire 24 hours after generation; download or rehost if longer retention is needed.

## Dedup

The skill must prevent duplicate creation calls with identical parameters within the same conversation turn. "Identical parameters" means matching `model`, `prompt`, `image_urls`, `resolution`, `num_outputs`, `quality`, `mask_url`, `output_format`, `background`. Any parameter change allows a new creation.
